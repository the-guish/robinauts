# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stand-in for ``opencode serve``: the part of its HTTP API the engine speaks (1.19), behind
the same basic auth, with the same readiness line, sessions, forks and event stream. A fake at
the edge, like the backend's echo engine: the engine runs against it unchanged, and no model is
called. Its sessions live as long as the process.

Each answer says what was asked and what the session held before, so a test reads from the
answer whether a turn continued, forked or started anew:

    You said <prompt>; before: [<the session's earlier prompts>]

A prompt can ask for more:

- ``tool <name> <json>``: a call of ``robinauts_<name>``, the worker's own MCP server that the
  turn added to its directory, whose output is the answer;
- ``bash``: a bash call, completed, before the answer;
- ``sleep``: no answer until the session is aborted;
- ``unauthorized``: the turn fails as on a refused key.

What it was configured with, and every request but the event stream, are appended to
``$XDG_DATA_HOME/stand-in.log.jsonl``, for the tests to read.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import queue
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

LOG = Path(os.environ.get("XDG_DATA_HOME", ".")) / "stand-in.log.jsonl"
PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
_ids = itertools.count(1)
_lock = threading.RLock()
_sessions: dict[str, dict[str, Any]] = {}
_streams: dict[str, list[queue.Queue[Any]]] = {}
_mcp: dict[str, dict[str, Any]] = {}
_aborted: set[str] = set()


def new_id(prefix: str) -> str:
    return f"{prefix}_{next(_ids):012d}"


def log(entry: dict[str, Any]) -> None:
    with _lock, LOG.open("a") as file:
        file.write(json.dumps(entry) + "\n")


def publish(directory: str, kind: str, **properties: Any) -> None:
    with _lock:
        for stream in _streams.get(directory, []):
            # Serialized now, as OpenCode does: a part updated later is another event.
            stream.put(json.loads(json.dumps({"type": kind, "properties": properties})))


def call_tool(directory: str, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    server = _mcp[directory]
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode()
    request = urllib.request.Request(
        server["url"],
        data=body,
        method="POST",
        headers={
            **server["headers"],
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read())["result"]
    return result["content"][0]["text"], bool(result.get("isError"))


def run_turn(session_id: str, prompt: str) -> None:
    session = _sessions[session_id]
    directory = session["directory"]
    before = [
        "".join(p.get("text", "") for p in m["parts"])
        for m in session["messages"]
        if m["info"]["role"] == "user"
    ]
    user = new_id("msg")
    user_info = {"id": user, "sessionID": session_id, "role": "user"}
    user_part = {
        "id": new_id("prt"),
        "sessionID": session_id,
        "messageID": user,
        "type": "text",
        "text": prompt,
    }
    session["messages"].append({"info": user_info, "parts": [user_part]})
    publish(directory, "message.updated", sessionID=session_id, info=user_info)
    publish(directory, "message.part.updated", sessionID=session_id, part=user_part)
    publish(directory, "session.status", sessionID=session_id, status={"type": "busy"})
    assistant = new_id("msg")
    info: dict[str, Any] = {
        "id": assistant,
        "sessionID": session_id,
        "role": "assistant",
        "parentID": user,
    }
    parts: list[dict[str, Any]] = []
    session["messages"].append({"info": info, "parts": parts})
    publish(directory, "message.updated", sessionID=session_id, info=info)

    def part(kind: str, **fields: Any) -> dict[str, Any]:
        body = {
            "id": new_id("prt"),
            "sessionID": session_id,
            "messageID": assistant,
            "type": kind,
            **fields,
        }
        parts.append(body)
        publish(directory, "message.part.updated", sessionID=session_id, part=body)
        return body

    def update(body: dict[str, Any], **fields: Any) -> None:
        body.update(fields)
        publish(directory, "message.part.updated", sessionID=session_id, part=body)

    answer = f"You said {prompt}; before: {before}"
    if prompt.startswith("tool "):
        _, name, arguments = prompt.split(" ", 2)
        tool = part(
            "tool",
            callID="call-1",
            tool=f"robinauts_{name}",
            state={"status": "pending", "input": {}},
        )
        update(tool, state={"status": "running", "input": json.loads(arguments)})
        answer, failed = call_tool(directory, name, json.loads(arguments))
        state = (
            {"status": "error", "error": answer}
            if failed
            else {"status": "completed", "output": answer}
        )
        update(tool, state={**state, "input": json.loads(arguments)})
    elif prompt == "bash":
        tool = part(
            "tool",
            callID="call-2",
            tool="bash",
            state={"status": "running", "input": {"command": "ls"}},
        )
        update(tool, state={"status": "completed", "input": {"command": "ls"}, "output": "a\n"})
    elif prompt == "sleep":
        while session_id not in _aborted:
            threading.Event().wait(0.05)
        _aborted.discard(session_id)
        info["error"] = {"name": "MessageAbortedError", "data": {"message": "aborted"}}
        answer = ""
    elif prompt == "unauthorized":
        info["error"] = {
            "name": "ProviderAuthError",
            "data": {"providerID": "x", "message": "invalid x-api-key"},
        }
        publish(directory, "session.error", sessionID=session_id, error=info["error"])
        answer = ""
    if answer:
        reasoning = part("reasoning", text="", time={"start": 1})
        publish(
            directory,
            "message.part.delta",
            sessionID=session_id,
            messageID=assistant,
            partID=reasoning["id"],
            field="text",
            delta="pondering",
        )
        update(reasoning, text="pondering", time={"start": 1, "end": 2})
        text = part("text", text="", time={"start": 1})
        half = len(answer) // 2
        for delta in (answer[:half], answer[half:]):
            publish(
                directory,
                "message.part.delta",
                sessionID=session_id,
                messageID=assistant,
                partID=text["id"],
                field="text",
                delta=delta,
            )
        update(text, text=answer, time={"start": 1, "end": 2})
        info["finish"] = "stop"
    publish(directory, "message.updated", sessionID=session_id, info=info)
    publish(directory, "session.status", sessionID=session_id, status={"type": "idle"})
    publish(directory, "session.idle", sessionID=session_id)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _authorized(self) -> bool:
        expected = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("authorization") == f"Basic {expected}":
            return True
        self._json(401, {"name": "Unauthorized"})
        return False

    def _json(self, status: int, body: Any = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-length", str(len(data)))
        if data:
            self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method: str) -> None:
        if not self._authorized():
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        directory = query.get("directory", [""])[0]
        parts = url.path.strip("/").split("/")
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        if url.path == "/event":
            self._events(directory)
            return
        log({"method": method, "path": url.path, "directory": directory, "body": body})
        with _lock:
            self._answer(method, parts, directory, body)

    def _answer(self, method: str, parts: list[str], directory: str, body: Any) -> None:
        if parts == ["session"] and method == "POST":
            session_id = new_id("ses")
            _sessions[session_id] = {"id": session_id, "directory": directory, "messages": []}
            self._json(200, {"id": session_id, "directory": directory})
            return
        if parts == ["mcp"] and method == "POST":
            _mcp[directory] = body["config"]
            self._json(200, {})
            return
        if len(parts) < 2 or parts[0] != "session" or parts[1] not in _sessions:
            self._json(404, {"name": "NotFoundError", "data": {"message": "no session"}})
            return
        session = _sessions[parts[1]]
        action = parts[2] if len(parts) > 2 else None
        if action is None and method == "PATCH":
            session.setdefault("permission", []).extend(body.get("permission") or [])
            self._json(200, {"id": session["id"]})
        elif action is None and method == "DELETE":
            del _sessions[session["id"]]
            self._json(200, True)
        elif action == "message" and method == "GET":
            self._json(200, session["messages"])
        elif action == "fork" and method == "POST":
            ids = [m["info"]["id"] for m in session["messages"]]
            cut = body.get("messageID")
            kept = session["messages"][: ids.index(cut)] if cut in ids else session["messages"]
            forked = new_id("ses")
            renamed = {m["info"]["id"]: new_id("msg") for m in kept}
            copies = []
            for message in kept:
                info = {
                    **message["info"],
                    "id": renamed[message["info"]["id"]],
                    "sessionID": forked,
                }
                if info.get("parentID") in renamed:
                    info["parentID"] = renamed[info["parentID"]]
                copies.append({"info": info, "parts": [dict(p) for p in message["parts"]]})
            _sessions[forked] = {
                "id": forked,
                "directory": session["directory"],
                "messages": copies,
            }
            self._json(200, {"id": forked})
        elif action == "prompt_async" and method == "POST":
            prompt = body["parts"][0]["text"]
            self._json(204)
            threading.Thread(target=run_turn, args=(session["id"], prompt), daemon=True).start()
        elif action == "abort" and method == "POST":
            _aborted.add(session["id"])
            self._json(200, True)
        else:
            self._json(404, {"name": "NotFoundError"})

    def _events(self, directory: str) -> None:
        stream: queue.Queue[Any] = queue.Queue()
        with _lock:
            _streams.setdefault(directory, []).append(stream)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        stream.put({"type": "server.connected", "properties": {}})
        try:
            while True:
                try:
                    event = stream.get(timeout=1.0)
                except queue.Empty:
                    event = {"type": "server.heartbeat", "properties": {}}
                data = f"data: {json.dumps(event)}\n\n".encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _lock:
                _streams[directory].remove(stream)

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PATCH(self) -> None:
        self._route("PATCH")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    arguments = sys.argv[1:]
    assert arguments[0] == "serve", arguments
    host = arguments[arguments.index("--hostname") + 1]
    port = int(arguments[arguments.index("--port") + 1])
    configured = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
    switches = sorted(k for k in os.environ if k.startswith("OPENCODE_DISABLE_"))
    log({"config": configured, "client": os.environ.get("OPENCODE_CLIENT"), "switches": switches})
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print("Warning: nothing", flush=True)
    print(f"opencode server listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
