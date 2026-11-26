# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``POST .../responses`` on a loopback port, streamed as OpenAI's Responses API streams,
answered with the text of the last user message: the one OpenAI-shaped model Codex can be run
against with no key and no network, so that the worker's container is tested end to end.

Bound to port 0, so the operating system picks the port; ``base_url`` is what goes in the
provider's ``base_url``. Every request body is kept in ``received``, in order. Any other path is
answered ``404``, which Codex takes as a provider without that feature.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

CONTEXT = ("<environment_context", "<user_instructions", "# AGENTS.md", "<permissions")
"""Codex's own notes to the model, sent as user messages beside what was asked."""


def _texts(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        str(part.get("text", ""))
        for part in content or []
        if part.get("type") in ("input_text", "output_text", "text")
    )


def history(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each message's role and text, in order, Codex's own notes left out."""
    found = []
    for item in items:
        if item.get("type", "message") != "message" or item.get("role") not in (
            "user",
            "assistant",
        ):
            continue
        text = _texts(item)
        if item["role"] == "user" and text.lstrip().startswith(CONTEXT):
            continue
        found.append((str(item["role"]), text))
    return found


def asked(items: list[dict[str, Any]]) -> str:
    """What the person asked: the text of the last user message."""
    users = [text for role, text in history(items) if role == "user"]
    return users[-1] if users else ""


def in_order(sent: list[tuple[str, str]], expected: list[tuple[str, str]]) -> bool:
    """Whether ``expected`` is a subsequence of ``sent``."""
    remaining = iter(sent)
    return all(any(entry == message for entry in remaining) for message in expected)


class FakeResponsesServer:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.received: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer((host, 0), _handler_for(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> FakeResponsesServer:
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


def _events(response_id: str, text: str) -> list[tuple[str, dict[str, Any]]]:
    item_id = f"{response_id}-msg"
    message = {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    usage = {
        "input_tokens": 1,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 1,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 2,
    }
    response = {"id": response_id, "object": "response", "status": "in_progress", "output": []}
    added = {**message, "status": "in_progress", "content": []}
    return [
        ("response.created", {"response": response}),
        ("response.output_item.added", {"output_index": 0, "item": added}),
        (
            "response.output_text.delta",
            {"item_id": item_id, "output_index": 0, "content_index": 0, "delta": text},
        ),
        ("response.output_item.done", {"output_index": 0, "item": message}),
        (
            "response.completed",
            {"response": {**response, "status": "completed", "output": [message], "usage": usage}},
        ),
    ]


def _handler_for(server: FakeResponsesServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._send(404, b"")

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
            if not self.path.rstrip("/").endswith("/responses"):
                self._send(404, b"")
                return
            server.received.append(body)
            response_id = f"resp_{len(server.received)}"
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
            for kind, data in _events(response_id, asked(body.get("input") or [])):
                payload = json.dumps({"type": kind, **data})
                self.wfile.write(f"event: {kind}\ndata: {payload}\n\n".encode())
                self.wfile.flush()
            self.close_connection = True

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler
