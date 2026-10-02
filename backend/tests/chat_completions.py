# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""OpenAI's Chat Completions on the wire, for both engines' tests: a vendor a test writes.

The engines' own tests run a turn over a chat model they script, which proves
the agent, the loop, the stream and the mapping and says nothing about the one
thing a scripted model skips: **what the vendor's client really sends, and
what it makes of what really comes back**. This is that half for the OpenAI
kinds. The engine builds its client exactly as a deployment does
(``chat_model``), and the test then swaps the client's HTTP transport for one
that answers from a script and keeps every request -- so the request is the
real one, byte for byte, the stream is parsed by the real SDK and the real
framework, and nothing leaves the process.

Written without either framework, and named after the protocol rather than an
engine: both engines' tests use it, and the discard test is that deleting an
adapter breaks its own tests and nothing else (``docs/layout.md``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2

MODEL_NAME = "gpt-5.5"
"""The vendor's name the scripted stream says it came from."""

PATH = "/chat/completions"
"""What OpenAI's client appends to a base URL: the whole of the protocol's path."""


def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    """One ``chat.completion.chunk``, carrying that delta."""
    return {
        "id": "chatcmpl-robinauts",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def said(*pieces: str) -> list[dict[str, Any]]:
    """An answer's text, streamed in those pieces; the first opens the message."""
    return [
        chunk({"role": "assistant", "content": piece} if at == 0 else {"content": piece})
        for at, piece in enumerate(pieces)
    ]


def calling(call_id: str, name: str, arguments: dict[str, Any], *, index: int = 0) -> list[Any]:
    """A tool call as the vendor streams one: its id and name, then its JSON in pieces."""
    written = json.dumps(arguments)
    middle = len(written) // 2
    return [
        chunk(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": ""},
                    }
                ]
            }
        ),
        *(
            chunk({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
            for piece in (written[:middle], written[middle:])
        ),
    ]


def finished(reason: str = "stop") -> list[dict[str, Any]]:
    """The chunk that ends the answer, and the usage chunk after it.

    The usage chunk is what ``stream_options.include_usage`` asks for, and both
    engines ask: it has no choices at all, which is what a framework reading the
    stream has to pass over.
    """
    return [
        chunk({}, reason),
        {
            "id": "chatcmpl-robinauts",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": MODEL_NAME,
            "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
        },
    ]


def streamed(*chunks: dict[str, Any]) -> bytes:
    """Those chunks as the server-sent events the vendor writes, ``[DONE]`` last."""
    events = "".join(f"data: {json.dumps(each)}\n\n" for each in chunks)
    return (events + "data: [DONE]\n\n").encode()


@dataclass
class Vendor:
    """An OpenAI-protocol endpoint that answers from a script and keeps what it was sent.

    ``body`` answers every request; ``bodies`` answers them in turn, one each,
    which is what a turn with a tool round makes -- the framework asks again
    with the result -- and the last of them answers whatever follows.
    ``status`` other than 200 answers with ``error`` as the body, which is how
    the vendor refuses a key, a model or a request, and what the SDK raises its
    own exception from.
    """

    body: bytes = b""
    bodies: list[bytes] = field(default_factory=list)
    status: int = 200
    error: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    sent: list[httpx2.Request] = field(default_factory=list)

    def answer(self, request: httpx2.Request) -> httpx2.Response:
        self.sent.append(request)
        if self.status != 200:
            return httpx2.Response(
                self.status, json={"error": self.error or {}}, headers=self.headers
            )
        body = self.bodies[min(len(self.sent), len(self.bodies)) - 1] if self.bodies else self.body
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    def plugged_into(self, client: Any) -> None:
        """Answer every request that vendor client makes from here on.

        The SDK's HTTP client is its ``_client``, and replacing it is the one
        private thing this does: everything the SDK decides about the request --
        the URL, the headers, the body -- is decided before it reaches it.
        """
        client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.answer))

    @property
    def request(self) -> httpx2.Request:
        """The one request a turn made."""
        assert len(self.sent) == 1, f"one request per turn, not {len(self.sent)}"
        return self.sent[0]

    @property
    def body_sent(self) -> dict[str, Any]:
        """That request's JSON."""
        return self.body_of(self.request)

    @staticmethod
    def body_of(request: httpx2.Request) -> dict[str, Any]:
        """One request's JSON."""
        parsed = json.loads(request.content)
        assert isinstance(parsed, dict)
        return parsed
