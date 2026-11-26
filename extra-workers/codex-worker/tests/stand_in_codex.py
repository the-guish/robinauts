# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stand-in for ``codex app-server``: the part of its JSON-RPC the engine speaks, one JSON
object per line on standard input and output, with threads kept in ``$CODEX_HOME`` between
processes, as Codex keeps them. A fake at the edge, like the backend's echo engine: the engine
runs against it unchanged, and no model is called.

Each answer says what was asked and what the thread held before, so a test reads from the
answer whether a turn continued, forked or started anew:

    You said <prompt>; before: [<the thread's earlier prompts>]

A prompt can ask for more:

- ``tool <name> <json>``: a call of the dynamic tool ``name``, answered by the engine, whose
  output is the answer;
- ``shell``: a command execution, completed, before the answer;
- ``sleep``: no answer until the turn is interrupted;
- ``unauthorized``: the turn fails as on a refused key.

Every ``-c`` override of the command line, every request's method and params, and the names of
the environment's variables that start with ``ROBINAUTS_CODEX_TOOL_`` are appended to
``$CODEX_HOME/stand-in.log.jsonl``, for the tests to read.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

HOME = Path(os.environ["CODEX_HOME"])
STATE = HOME / "stand-in.json"
LOG = HOME / "stand-in.log.jsonl"

_out = threading.Lock()
_answers: dict[Any, Any] = {}
_answered = threading.Condition()
_interrupted: set[str] = set()


def send(message: dict[str, Any]) -> None:
    with _out:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def log(entry: dict[str, Any]) -> None:
    with LOG.open("a") as file:
        file.write(json.dumps(entry) + "\n")


def load() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text())
    except OSError:
        return {"threads": {}}


def save(state: dict[str, Any]) -> None:
    STATE.write_text(json.dumps(state))


def notify(method: str, **params: Any) -> None:
    send({"method": method, "params": params})


def ask(method: str, params: dict[str, Any]) -> Any:
    """A request of the server's own, and the client's answer."""
    id_ = f"s-{uuid.uuid4()}"
    send({"id": id_, "method": method, "params": params})
    with _answered:
        _answered.wait_for(lambda: id_ in _answers)
        return _answers.pop(id_)


def run_turn(thread_id: str, turn_id: str, prompt: str) -> None:
    state = load()
    thread = state["threads"][thread_id]
    before = [turn["prompt"] for turn in thread["turns"] if turn["status"] == "completed"]
    ids = {"threadId": thread_id, "turnId": turn_id}
    notify("turn/started", threadId=thread_id, turn={"id": turn_id, "status": "inProgress"})
    status, error, answer = "completed", None, f"You said {prompt}; before: {before}"
    if prompt.startswith("tool "):
        _, name, arguments = prompt.split(" ", 2)
        item = {
            "type": "dynamicToolCall",
            "id": "call-1",
            "tool": name,
            "arguments": json.loads(arguments),
            "status": "inProgress",
        }
        notify("item/started", item=item, **ids)
        result = ask(
            "item/tool/call",
            {**ids, "callId": "call-1", "tool": name, "arguments": json.loads(arguments)},
        )["result"]
        answer = result["contentItems"][0]["text"]
        notify("item/completed", item={**item, "status": "completed", **result}, **ids)
    elif prompt == "shell":
        item = {"type": "commandExecution", "id": "call-2", "command": "ls", "cwd": "/w"}
        notify("item/started", item={**item, "status": "inProgress"}, **ids)
        notify(
            "item/completed",
            item={**item, "status": "completed", "aggregatedOutput": "a\n", "exitCode": 0},
            **ids,
        )
    elif prompt == "sleep":
        with _answered:
            _answered.wait_for(lambda: turn_id in _interrupted)
        status, answer = "interrupted", ""
    elif prompt == "unauthorized":
        error = {
            "message": "401 Unauthorized",
            "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}},
        }
        notify("error", error=error, willRetry=False, **ids)
        status, answer = "failed", ""
    items = []
    if answer:
        notify("item/reasoning/summaryTextDelta", itemId="r-1", delta="pondering", **ids)
        half = len(answer) // 2
        for delta in (answer[:half], answer[half:]):
            notify("item/agentMessage/delta", itemId="m-1", delta=delta, **ids)
        items = [{"type": "agentMessage", "id": "m-1", "text": answer, "phase": "final_answer"}]
        notify("item/completed", item=items[0], **ids)
    state = load()
    for turn in state["threads"][thread_id]["turns"]:
        if turn["id"] == turn_id:
            turn["status"] = status
    save(state)
    notify(
        "turn/completed",
        threadId=thread_id,
        turn={"id": turn_id, "status": status, "items": items, "error": error},
    )


def handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """The result of a request, or ``{"error": ...}``."""
    state = load()
    threads = state["threads"]
    if method == "initialize":
        return {"userAgent": "stand-in", "codexHome": str(HOME)}
    if method == "thread/start":
        thread_id = str(uuid.uuid4())
        # A thread runs on the provider it was given, or on Codex's default.
        provider = params.get("modelProvider") or "openai"
        threads[thread_id] = {"turns": [], "forkedFrom": None, "provider": provider}
        save(state)
        return {"thread": {"id": thread_id}}
    thread_id = params.get("threadId")
    if thread_id not in threads:
        return {"error": {"code": -32600, "message": f"no rollout found for thread {thread_id}"}}
    thread = threads[thread_id]
    if method == "thread/resume":
        return {"thread": {"id": thread_id, "turns": []}}
    if method == "thread/turns/list":
        turns = [{"id": turn["id"], "status": turn["status"]} for turn in reversed(thread["turns"])]
        return {"data": turns, "nextCursor": None}
    if method == "thread/fork":
        ids = [turn["id"] for turn in thread["turns"]]
        last = params["lastTurnId"]
        if last not in ids:
            return {"error": {"code": -32602, "message": "no such turn"}}
        forked = str(uuid.uuid4())
        threads[forked] = {
            "turns": thread["turns"][: ids.index(last) + 1],
            "forkedFrom": thread_id,
            "provider": params.get("modelProvider") or "openai",
        }
        save(state)
        return {"thread": {"id": forked, "forkedFromId": thread_id}}
    if method == "thread/delete":
        if any(other["forkedFrom"] == thread_id for other in threads.values()):
            return {"error": {"code": -32600, "message": "forked history still references it"}}
        del threads[thread_id]
        save(state)
        return {}
    if method == "turn/start":
        turn_id = str(uuid.uuid4())
        prompt = params["input"][0]["text"]
        thread["turns"].append({"id": turn_id, "prompt": prompt, "status": "inProgress"})
        save(state)
        threading.Thread(target=run_turn, args=(thread_id, turn_id, prompt), daemon=True).start()
        return {"turn": {"id": turn_id, "status": "inProgress", "items": []}}
    if method == "turn/interrupt":
        with _answered:
            _interrupted.add(params["turnId"])
            _answered.notify_all()
        return {}
    return {"error": {"code": -32601, "message": f"no method {method}"}}


def main() -> None:
    arguments = sys.argv[1:]
    overrides = [arguments[i + 1] for i, a in enumerate(arguments) if a == "-c"]
    variables = sorted(v for v in os.environ if v.startswith("ROBINAUTS_CODEX_TOOL_"))
    log({"argv": arguments[:1], "overrides": overrides, "variables": variables})
    for line in sys.stdin:
        message = json.loads(line)
        if "method" not in message:
            with _answered:
                _answers[message["id"]] = message
                _answered.notify_all()
            continue
        if "id" not in message:
            continue
        log({"method": message["method"], "params": message.get("params") or {}})
        result = handle(message["method"], message.get("params") or {})
        if "error" in result:
            send({"id": message["id"], "error": result["error"]})
        else:
            send({"id": message["id"], "result": result})


if __name__ == "__main__":
    main()
