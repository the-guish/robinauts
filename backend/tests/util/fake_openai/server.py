# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``POST .../chat/completions`` on a loopback port, streamed or not, answered by a ``FakeModel``.

Bound to port 0, so the operating system picks the port; ``base_url`` is what a
provider's ``base_url`` should be set to. Every request body is kept in
``received``, in order. A model answers with text, or with a ``CallTool`` that
asks the client to run a tool and send its result back. A model that raises is
answered ``400``, as the vendor refuses a call for good. A model that raises ``Overloaded`` is
answered ``503``, a hiccup the client retries. A model that raises ``RateLimited`` is answered
``429``, with a ``retry-after-ms`` of 10 so that the client retries at once.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Protocol

COMPLETION_ID = "chatcmpl-fake"


class Overloaded(Exception):
    """Raised by a model to have the server answer ``503``, as an overloaded vendor."""


class RateLimited(Exception):
    """Raised by a model to have the server answer ``429``, as a vendor over its rate limit."""


@dataclass(frozen=True)
class CallTool:
    """An answer that is a call of the tool ``name`` with ``arguments``, not text."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = "call_fake"


class FakeModel(Protocol):
    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        """The answer, given the request's ``messages`` as the client sent them."""
        ...


class FakeLocalGPTServer:
    def __init__(self, model: FakeModel, host: str = "127.0.0.1") -> None:
        """``host`` is the address it listens on: every address, ``0.0.0.0``, for a container
        that reaches it through the host's gateway name."""
        self.model = model
        self.received: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._http = ThreadingHTTPServer((host, 0), _handler_for(self))
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._http.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def port(self) -> int:
        return int(self._http.server_address[1])

    def start(self) -> FakeLocalGPTServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        self._thread.join()

    def __enter__(self) -> FakeLocalGPTServer:
        return self.start()

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def answer(self, request: dict[str, Any]) -> str | CallTool:
        with self._lock:
            self.received.append(request)
        return self.model.reply(request["messages"])


def _chunk(model: str, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": COMPLETION_ID,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _usage(text: str) -> dict[str, int]:
    completion = len(text.split())
    return {"prompt_tokens": 1, "completion_tokens": completion, "total_tokens": 1 + completion}


def _tool_call(call: CallTool) -> dict[str, Any]:
    return {
        "id": call.call_id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
    }


def _handler_for(server: FakeLocalGPTServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if not self.path.endswith("/chat/completions"):
                self._send_json(404, {"error": {"message": f"no route {self.path}"}})
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = request.get("model", "fake-gpt")
            try:
                answer = server.answer(request)
            except Overloaded as busy:
                self._send_json(503, {"error": {"message": str(busy), "type": "server_error"}})
                return
            except RateLimited as limited:
                error = {"error": {"message": str(limited), "type": "rate_limit_exceeded"}}
                self._send_json(429, error, {"retry-after-ms": "10"})
                return
            except Exception as refused:  # a model that raises is the vendor refusing the call
                self._send_json(
                    400, {"error": {"message": str(refused), "type": "invalid_request_error"}}
                )
                return
            if request.get("stream"):
                self._stream(request, answer, model)
            else:
                self._send_json(200, self._completion(answer, model))

        def _completion(self, answer: str | CallTool, model: str) -> dict[str, Any]:
            if isinstance(answer, CallTool):
                message = {"role": "assistant", "content": None, "tool_calls": [_tool_call(answer)]}
                finish, usage = "tool_calls", _usage("")
            else:
                message = {"role": "assistant", "content": answer}
                finish, usage = "stop", _usage(answer)
            return {
                "id": COMPLETION_ID,
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage,
            }

        def _stream(self, request: dict[str, Any], answer: str | CallTool, model: str) -> None:
            if isinstance(answer, CallTool):
                # The call's id and name first, then its arguments, as the vendor streams one.
                call = _tool_call(answer)
                arguments = call["function"]["arguments"]
                call["function"]["arguments"] = ""
                chunks = [
                    _chunk(model, {"role": "assistant", "content": None}),
                    _chunk(model, {"tool_calls": [{"index": 0, **call}]}),
                    _chunk(
                        model, {"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]}
                    ),
                    _chunk(model, {}, "tool_calls"),
                ]
                text = ""
            else:
                # One chunk per word, each keeping the whitespace after it, so they join back.
                pieces = re.findall(r"\s*\S+\s*|\s+", answer)
                chunks = [
                    _chunk(model, {"role": "assistant", "content": ""}),
                    *(_chunk(model, {"content": piece}) for piece in pieces),
                    _chunk(model, {}, "stop"),
                ]
                text = answer
            if (request.get("stream_options") or {}).get("include_usage"):
                chunks.append({**_chunk(model, {}), "choices": [], "usage": _usage(text)})
            body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            self._send(200, "text/event-stream", body.encode())

        def _send_json(
            self, status: int, payload: dict[str, Any], headers: Mapping[str, str] = {}
        ) -> None:
            self._send(status, "application/json", json.dumps(payload).encode(), headers)

        def _send(
            self, status: int, content_type: str, body: bytes, headers: Mapping[str, str] = {}
        ) -> None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler
