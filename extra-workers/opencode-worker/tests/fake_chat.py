# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``POST .../chat/completions`` on a loopback port, streamed as Chat Completions streams,
answered with the text of the last user message: the one model OpenCode can be run against
with no key and no network, so that the worker's container is tested end to end.

Bound to port 0, so the operating system picks the port; ``base_url`` is what goes in the
provider's ``base_url``. Every request body is kept in ``received``, in order. Written after the
backend's ``tests/util/fake_openai``, which this worker's environment cannot import.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any


def text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = [
        str(part.get("text", ""))
        for part in content or []
        if part.get("type") == "text" and not str(part.get("text", "")).startswith("<")
    ]
    return "\n".join(parts)


def history(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each user and assistant message's role and text, in order."""
    return [
        (str(message["role"]), text_of(message))
        for message in messages
        if message.get("role") in ("user", "assistant")
    ]


def in_order(sent: list[tuple[str, str]], expected: list[tuple[str, str]]) -> bool:
    """Whether ``expected`` is a subsequence of ``sent``."""
    remaining = iter(sent)
    return all(any(entry == message for entry in remaining) for message in expected)


class FakeChatServer:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.received: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer((host, 0), _handler_for(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> FakeChatServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()


def _chunk(model: str, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _handler_for(server: FakeChatServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._send(404, b"")

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send(404, b"")
                return
            server.received.append(body)
            users = [text for role, text in history(body.get("messages") or []) if role == "user"]
            answer = users[-1] if users else ""
            model = str(body.get("model", ""))
            if not body.get("stream"):
                message = {"role": "assistant", "content": answer}
                completion = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
                self._send(200, json.dumps(completion).encode(), "application/json")
                return
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("connection", "close")
            self.end_headers()
            chunks = [
                _chunk(model, {"role": "assistant", "content": ""}),
                _chunk(model, {"content": answer}),
                _chunk(model, {}, "stop"),
            ]
            usage = {**_chunk(model, {}), "choices": []}
            usage["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            for chunk in [*chunks, usage]:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True

        def _send(self, status: int, body: bytes, kind: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("content-length", str(len(body)))
            self.send_header("content-type", kind)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler
