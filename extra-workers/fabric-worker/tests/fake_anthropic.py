# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``POST .../v1/messages`` on a loopback port, scripted: the one Anthropic-shaped model Claude
Code can be run against, behind NeMo Fabric, with no key and no network.

- A question ``bash: <command>`` is answered with a call of Claude Code's ``Bash`` tool on that
  command; the request that carries the tool's result is answered ``ran: <the result>``.
- Any other question is answered with its own text.

Bound to port 0, so the operating system picks the port; ``base_url`` is what goes in the
provider's ``base_url``. Every request body is kept in ``received``, in order. A streamed request
is answered as the Messages API streams; an unstreamed one with the whole message. Written after
``extra-workers/claude-code-worker/tests/fake_anthropic.py``, with the tool call added.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

MESSAGE_ID = "msg_fake"
TOOL_USE_ID = "toolu_fake"
BASH_PREFIX = "bash:"
REMINDER = "<system-reminder"
"""Claude Code's own notes to the model, put in the user's message beside what was asked."""


def asked(messages: list[dict[str, Any]]) -> str:
    """What the person asked: the text of the last user message, Claude Code's notes left out."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        parts = [
            block.get("text", "")
            for block in content
            if block.get("type") == "text" and not block.get("text", "").startswith(REMINDER)
        ]
        if parts:
            return "\n".join(parts)
    return ""


def tool_result(messages: list[dict[str, Any]]) -> str | None:
    """The text of the tool result the last user message carries, or ``None``. Claude Code puts
    messages of role ``system`` of its own after it, which are passed over."""
    last = next((m for m in reversed(messages) if m.get("role") != "system"), None)
    if last is None or last.get("role") != "user":
        return None
    content = last.get("content")
    if isinstance(content, str):
        return None
    for block in content:
        if block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                return "".join(part.get("text", "") for part in inner)
            return str(inner or "")
    return None


def system_text(request: dict[str, Any]) -> str:
    """The whole system prompt, as one text."""
    system = request.get("system") or ""
    if isinstance(system, str):
        return system
    return "\n".join(block.get("text", "") for block in system)


class FakeAnthropicServer:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.keys: set[str] = set()
        """Every ``x-api-key`` the requests carried."""
        self._lock = threading.Lock()
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._http.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeAnthropicServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        self._thread.join()

    def __enter__(self) -> FakeAnthropicServer:
        return self.start()

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def conversations(self) -> list[dict[str, Any]]:
        """The requests that asked the model a question of the conversation's: Claude Code also
        asks for titles and the like, with no tools."""
        with self._lock:
            return [r for r in self.received if r.get("tools")]

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        """The content block the model answers with."""
        with self._lock:
            self.received.append(request)
        messages = request.get("messages", [])
        result = tool_result(messages)
        if result is not None:
            return {"type": "text", "text": f"ran: {result.strip()}"}
        question = asked(messages)
        if question.startswith(BASH_PREFIX) and request.get("tools"):
            command = question.removeprefix(BASH_PREFIX).strip()
            return {
                "type": "tool_use",
                "id": TOOL_USE_ID,
                "name": "Bash",
                "input": {"command": command, "description": "run it"},
            }
        return {"type": "text", "text": question}


def _message(model: str, block: dict[str, Any] | None) -> dict[str, Any]:
    stop = None if block is None else ("tool_use" if block["type"] == "tool_use" else "end_turn")
    return {
        "id": MESSAGE_ID,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [] if block is None else [block],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _events(model: str, block: dict[str, Any]) -> list[dict[str, Any]]:
    if block["type"] == "tool_use":
        start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        stop = "tool_use"
    else:
        start = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": block["text"]}
        stop = "end_turn"
    return [
        {"type": "message_start", "message": _message(model, None)},
        {"type": "content_block_start", "index": 0, "content_block": start},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]


def _handler_for(server: FakeAnthropicServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            path = self.path.split("?")[0]
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if path.endswith("/v1/messages/count_tokens"):
                self._send_json(200, {"input_tokens": 1})
                return
            if not path.endswith("/v1/messages"):
                self._send_json(404, {"type": "error", "error": {"message": f"no route {path}"}})
                return
            request = json.loads(body)
            server.keys.add(self.headers.get("x-api-key") or "")
            block = server.answer(request)
            model = str(request.get("model", ""))
            if not request.get("stream"):
                self._send_json(200, _message(model, block))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for event in _events(model, block):
                line = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()

        def do_GET(self) -> None:
            self._send_json(404, {"type": "error", "error": {"message": f"no route {self.path}"}})

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler
