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
adapter breaks its own tests and nothing else (``docs/layout.md``). Beside the
vendor are the configuration a turn over it runs on, and the shapes of stream
the tests write: what the tests of both engines -- each engine's own, and the
ones that hold both to one behaviour
(``tests/unit/test_engines_over_chat_completions.py``) -- would otherwise each
write out again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2
import openai

from conversations import AGENT, agent_definition
from robinauts.legacy.domain import (
    AgentDefinition,
    Engine,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
)

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


# --- the configuration a turn over it runs on ---------------------------------


KEY = "not-a-real-key"
"""What the engines are handed. Nothing here reaches a provider."""

SYSTEM_PROMPT = "Play fair."

GPT = "gpt"
"""The platform's id for the OpenAI model the tests configure."""

OPENAI_PROVIDER = ModelProviderConfig(
    id="openai", kind=ProviderKind.OPENAI, api_key_env="ROBINAUTS_OPENAI_KEY"
)

GATEWAY_ENDPOINT = "https://gateway.example.test/v1"
"""An endpoint that speaks OpenAI's Chat Completions, spelt as an operator writes it.

A **prefix** OpenAI's client appends ``/chat/completions`` to, which is why it
ends in the version and not in ``/api`` (``docs/specs/agents.md``).
"""

GATEWAY_PROVIDER = ModelProviderConfig(
    id="gateway",
    kind=ProviderKind.OPENAI_COMPATIBLE,
    api_key_env="ROBINAUTS_GATEWAY_KEY",
    base_url=GATEWAY_ENDPOINT,
)


def gpt(provider: ModelProviderConfig = OPENAI_PROVIDER, **changes: Any) -> ModelConfig:
    """The OpenAI model, on that provider, with what the test changes about it."""
    return ModelConfig(id=GPT, provider=provider.id, name=MODEL_NAME, **changes)


def gpt_agent(engine: Engine) -> AgentDefinition:
    """The one agent, on that engine, whose default is the OpenAI model."""
    return agent_definition(id=AGENT, model=GPT, engine=engine, system_prompt=SYSTEM_PROMPT)


def openai_models(
    engine: Engine, provider: ModelProviderConfig = OPENAI_PROVIDER, **changes: Any
) -> ModelsConfig:
    """One OpenAI-protocol provider, one model on it and one agent on that engine."""
    return ModelsConfig(
        providers={provider.id: provider},
        models={GPT: gpt(provider, **changes)},
        agents={AGENT: gpt_agent(engine)},
    )


REDIRECTING_VARIABLES = {
    # Read by the SDK, by ChatOpenAI and by Pydantic AI's provider when no base
    # URL is passed.
    "OPENAI_BASE_URL": "https://evil.example.test/v1",
    "OPENAI_API_BASE": "https://evil.example.test/v1",
    # Read by all three when no key is passed.
    "OPENAI_API_KEY": "sk-somebody-elses-key",
    # The LangSmith gateway, which langchain-core reaches for when neither a
    # key nor an endpoint was given, and Pydantic AI's own gateway, which a
    # provider named by string would reach.
    "LANGSMITH_GATEWAY": "https://evil.example.test",
    "LANGSMITH_GATEWAY_API_KEY": "somebody-elses-gateway-key",
    "PYDANTIC_AI_GATEWAY_API_KEY": "somebody-elses-gateway-key",
    "PYDANTIC_AI_GATEWAY_BASE_URL": "https://evil.example.test",
    # A proxy only ChatOpenAI would obey. An operator's is HTTPS_PROXY.
    "OPENAI_PROXY": "http://evil.example.test:3128",
    # The shape an answer is stored in, a stream's own timeout, and the
    # sockets' options: each a default ChatOpenAI takes from the environment.
    "LC_OUTPUT_VERSION": "v1",
    "LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S": "1",
    "LANGCHAIN_OPENAI_TCP_KEEPALIVE": "1",
    "LANGCHAIN_OPENAI_TCP_USER_TIMEOUT_MS": "1",
}
"""An environment doing everything it can to redirect or reshape an OpenAI turn."""

UNCONFIGURED = {
    "OPENAI_CUSTOM_HEADERS": "X-Nobody-Configured: this",
    "OPENAI_ORG_ID": "org-somebody-elses",
    "OPENAI_PROJECT_ID": "proj-somebody-elses",
    "OPENAI_ADMIN_KEY": "sk-admin-somebody-elses",
}
"""What the OpenAI SDK reads whenever an argument is ``None``, which is how
"none" is said -- so no argument keeps it out, and building an engine does.
(``ChatOpenAI`` reads ``OPENAI_ORGANIZATION`` besides, which the LangGraph
engine's own tests hold it to.)"""


def headers_of(client: Any) -> dict[str, str]:
    """The headers an OpenAI client would really send, built without sending one."""
    request = client._build_request(
        openai._models.FinalRequestOptions.construct(method="post", url=PATH, json_data={})
    )
    return dict(request.headers.items())


def never_tokenised(*args: Any, **kwargs: Any) -> Any:
    """What a test puts in place of tiktoken's encoding functions."""
    raise AssertionError("a turn asked tiktoken for an encoding")


# --- shapes of stream and history ---------------------------------------------


def call_delta(index: int | None, **given: Any) -> dict[str, Any]:
    """One streamed tool-call delta, with whatever of the call it carries.

    ``None`` is a delta with no ``index`` at all, which OpenAI never sends and
    a compatible server may.
    """
    call: dict[str, Any] = {"type": "function"} if index is None else {"index": index}
    call.setdefault("type", "function")
    function: dict[str, Any] = {}
    for key in ("name", "arguments"):
        if key in given:
            function[key] = given.pop(key)
    call.update(given)
    call["function"] = function
    return chunk({"tool_calls": [call]})
