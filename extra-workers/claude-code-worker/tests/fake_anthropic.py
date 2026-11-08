# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``POST .../v1/messages`` on a loopback port, answered with the text of the last user message:
the one Anthropic-shaped model Claude Code can be run against with no key and no network, so
that the worker's container is tested end to end.

Bound to port 0, so the operating system picks the port; ``base_url`` is what goes in
``ANTHROPIC_BASE_URL``, the provider's ``base_url``. Every request body is kept in
``received``, in order. A streamed request is answered as the Messages API streams, one text
block and ``end_turn``; an unstreamed one with the whole message. Written after the backend's
``tests/util/fake_openai``, for the other vendor's shape.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

MESSAGE_ID = "msg_fake"
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
        return "\n".join(parts)
    return ""


def history(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each message's role and text, in order: what was asked for the user's, every text block
    for the assistant's. Claude Code adds messages of its own, on the environment and the like,
    so a conversation is a subsequence of this, not the whole of it."""
    return [
        (
            str(message.get("role")),
            asked([message]) if message.get("role") == "user" else _text(message),
        )
        for message in messages
    ]


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


def in_order(found: list[tuple[str, str]], wanted: list[tuple[str, str]]) -> bool:
    """Whether ``wanted`` is a subsequence of ``found``."""
    rest = iter(found)
    return all(any(entry == item for item in rest) for entry in wanted)


class FakeAnthropicServer:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
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

    def answer(self, request: dict[str, Any]) -> str:
        with self._lock:
            self.received.append(request)
        return asked(request.get("messages", []))


def _message(model: str, text: str, *, started: bool) -> dict[str, Any]:
    return {
        "id": MESSAGE_ID,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [] if started else [{"type": "text", "text": text}],
        "stop_reason": None if started else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1 if started else len(text.split()),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _events(model: str, text: str) -> list[dict[str, Any]]:
    return [
        {"type": "message_start", "message": _message(model, text, started=True)},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": len(text.split())},
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
            text = server.answer(request)
            model = str(request.get("model", ""))
            if not request.get("stream"):
                self._send_json(200, _message(model, text, started=False))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for event in _events(model, text):
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
