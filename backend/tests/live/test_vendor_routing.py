# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where a turn's request really goes, asked of the vendor rather than of a mock.

One turn per engine against a real ``anthropic-compatible`` endpoint --
OpenRouter's -- with a **bogus key**. The answer is a 401, and that is the
point: a refusal from ``openrouter.ai`` is proof that the request was built
from the configuration and arrived at ``https://openrouter.ai/api/v1/messages``,
which is the one thing no test with a stubbed client can show. Everything else
about the two engines is proved without a network
(``tests/unit/test_langgraph_engine.py``,
``tests/unit/test_pydantic_ai_engine.py``); what is proved here is that
``base_url`` plus the vendor SDK's own path really compose into the endpoint
``docs/specs/agents.md`` tells an operator to expect.

**No ordinary test run reaches a provider, and this is why it is here.**
``tests/live/`` is not collected by a plain ``pytest`` (``norecursedirs`` in
``backend/pyproject.toml``), so CI -- which runs a plain one -- never sees it,
and the property the engine steps recorded holds: nothing in a normal run
leaves this machine for a vendor. It is asked for by name, and only with the
switch below::

    ROBINAUTS_LIVE_ROUTING=1 uv run pytest tests/live/test_vendor_routing.py

**It costs nothing and needs no key.** The key is key-shaped and is nobody's,
so the vendor refuses the request before it reads the body; the body holds one
word. There is no ``ROBINAUTS_LIVE_*_KEY`` here and no branch that would read
one, so unlike its neighbours in this directory it cannot spend anybody's
credit even by accident -- which is why it is marked ``io`` and **not**
``live``, a marker that means a real key and a real bill.

**It skips rather than fails for anything that is not the answer it is
reading.** No route to the vendor is a skip, and so is any status but the 401 --
a rate limit, a block, a bad gateway -- with the status in the reason: what
would be wrong with the build is the request arriving *somewhere else*, and
that is what is asserted whatever the vendor then says.
"""

from __future__ import annotations

import datetime
import os
import uuid
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx
import pytest

from aio import asyncio_test
from robinauts.adapters import ProviderKeys
from robinauts.adapters.agents.langgraph import LangGraphAgent
from robinauts.adapters.agents.langgraph import chat_model as langgraph_chat_model
from robinauts.adapters.agents.pydantic_ai import PydanticAIAgent
from robinauts.adapters.agents.pydantic_ai import chat_model as pydantic_ai_chat_model
from robinauts.domain import (
    AgentDefinition,
    Engine,
    Message,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    Role,
    TextPart,
)
from robinauts.ports import Agent

pytestmark = pytest.mark.io

SWITCH = "ROBINAUTS_LIVE_ROUTING"
"""Set to anything non-empty to run this. Naming the file is not enough.

Two doors rather than one: the directory keeps it out of every ordinary run,
and this keeps it out of a run that named the directory to get at the tests
beside it. Nothing else in the repository reads it.
"""

PROVIDER = "openrouter"
MODEL = "sonnet-via-openrouter"
AGENT = "assistant"

BASE_URL = "https://openrouter.ai/api"
"""What an operator writes, exactly as ``docs/specs/agents.md`` prints it."""

ENDPOINT = "https://openrouter.ai/api/v1/messages"
"""Where the SDK therefore sends the request: the base URL plus its own path.

The claim this module exists to check. A query string may be added by a
framework (Pydantic AI asks for the beta), so it is the scheme, the host and
the path that are compared and not the whole of the URL.
"""

MODEL_NAME = "anthropic/claude-sonnet-5"
"""OpenRouter's own name for the model. It is never reached: the key is refused
first. It is a real id all the same, so that the 401 is about the credential
and not about a model that does not exist.
"""

BOGUS_KEY = "sk-or-v1-" + "0" * 64
"""Key-shaped, and nobody's.

Shaped like the vendor's keys so that it is refused by the account behind it
rather than by a parser: ``User not found`` is an answer about a credential,
which is what makes it evidence that the request arrived.
"""

REFUSAL = "User not found."
"""What OpenRouter answers a key-shaped key it has never issued."""

UNAUTHORIZED = 401

UNREACHABLE = (anthropic.APIConnectionError, httpx.TransportError, OSError)
"""What "there is no route to the vendor from here" arrives as. See ``refusal_in``."""

TURN_SECONDS = 30.0
"""Generous for one refused request; nothing here waits for a model."""

MAX_TOKENS = 16
"""A ceiling on an answer that never comes, because every request needs one."""


@dataclass(frozen=True)
class Wiring:
    """How one engine is built, and what its factory for a vendor client is called.

    The two adapters spell the seam differently (``chat_model_for``,
    ``model_for``), and the test needs it for one reason only: the client a turn
    builds is released to the garbage collector by both engines and nothing
    closes it (``docs/working-notes/poc-progress.md``, step 16). Everywhere else
    that costs nothing, because no test opens a connection; **here a real one is
    opened**, and a socket closed by a finaliser is a ``ResourceWarning`` in
    whichever later test happens to be running when the collector gets to it.
    So the real factory is wrapped, what it built is kept, and the connection is
    given back by the test that made it.
    """

    adapter: type[Agent]
    factory: Any
    """The adapter's own ``chat_model``, wrapped rather than replaced: what is
    being tested is the client the deployment really builds."""
    seam: str


ENGINES: dict[Engine, Wiring] = {
    Engine.LANGGRAPH: Wiring(LangGraphAgent, langgraph_chat_model, "chat_model_for"),
    Engine.PYDANTIC_AI: Wiring(PydanticAIAgent, pydantic_ai_chat_model, "model_for"),
}
"""Both engines, which reach an ``anthropic-compatible`` provider by
different routes through the same SDK. Written out here rather than imported
from ``robinauts.app``: this is a test of the two adapters, and the table the
composition root keeps is a claim of its own.
"""


def clients_of(built: object) -> list[anthropic.AsyncAnthropic | anthropic.Anthropic]:
    """The vendor clients inside whatever an engine's factory produced.

    Read off the framework object by name, since that is where each framework
    keeps it: ``ChatAnthropic`` holds an async and a sync client,
    ``AnthropicModel`` the one it was given.
    """
    found = [getattr(built, name, None) for name in ("_async_client", "_client", "client")]
    return [
        client
        for client in found
        if isinstance(client, anthropic.AsyncAnthropic | anthropic.Anthropic)
    ]


def models_for(engine: Engine) -> ModelsConfig:
    """A deployment of one OpenRouter provider, one model and one agent on it."""
    return ModelsConfig(
        providers={
            PROVIDER: ModelProviderConfig(
                id=PROVIDER,
                kind=ProviderKind.ANTHROPIC_COMPATIBLE,
                api_key_env="ROBINAUTS_OPENROUTER_KEY",
                base_url=BASE_URL,
            )
        },
        models={
            MODEL: ModelConfig(
                id=MODEL,
                provider=PROVIDER,
                name=MODEL_NAME,
                timeout_seconds=TURN_SECONDS,
                max_output_tokens=MAX_TOKENS,
            )
        },
        agents={
            AGENT: AgentDefinition(
                id=AGENT, title="Assistant", system_prompt="", model=MODEL, engine=engine
            )
        },
    )


def question() -> Message:
    """The one message a turn is asked with. One word, and never answered."""
    return Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        parent_id=None,
        role=Role.USER,
        parts=(TextPart(text="hi"),),
        created_at=datetime.datetime.now(datetime.UTC),
    )


def answer_in(raised: BaseException) -> anthropic.APIStatusError:
    """The vendor's own answer from anywhere in that exception's chain.

    Each engine wraps what the SDK raised in its framework's own error
    (``AnthropicAuthenticationError``, ``ModelHTTPError``), so the chain is
    walked rather than the outermost exception read: what is being asked about
    is the **request the SDK made**, and only the SDK's own exception carries
    it.

    **Any** status is handed back, not the 401 alone: a rate limit or a bad
    gateway is still an answer from the host the request was sent to, and the
    caller reads the URL out of it before it decides whether the rest of the
    test can be run.

    A connection failure instead of an answer is a machine with no route to the
    vendor, and it skips: there is nothing wrong with the build. Three shapes of
    it are looked for, because which one arrives depends on how far the request
    got and on which framework wrapped it -- the SDK's own
    ``APIConnectionError`` (a timeout is one), the transport's, and the
    operating system's.
    """
    causes: list[BaseException] = []
    cause: BaseException | None = raised
    while cause is not None and cause not in causes:
        causes.append(cause)
        if isinstance(cause, anthropic.APIStatusError):
            return cause
        cause = cause.__cause__ or cause.__context__
    if any(isinstance(cause, UNREACHABLE) for cause in causes):
        pytest.skip(f"{ENDPOINT} could not be reached from this machine")
    raise raised


@pytest.mark.skipif(not os.environ.get(SWITCH), reason=f"set {SWITCH}=1 to run this")
@pytest.mark.parametrize("engine", sorted(ENGINES), ids=lambda engine: engine.value)
@asyncio_test
async def test_an_anthropic_compatible_turn_reaches_the_configured_endpoint(
    engine: Engine,
) -> None:
    wiring = ENGINES[engine]
    models = models_for(engine)
    opened: list[object] = []

    def remembering(*arguments: Any) -> Any:
        built = wiring.factory(*arguments)
        opened.append(built)
        return built

    agent = wiring.adapter(
        models, ProviderKeys({PROVIDER: BOGUS_KEY}), **{wiring.seam: remembering}
    )
    try:
        with pytest.raises(Exception) as raised:  # noqa: B017 -- each engine's own
            async for _ in agent.run_turn(models.agents[AGENT], (question(),), (), model=MODEL):
                pass
    finally:
        # The connection this test really opened, given back here rather than
        # left to a finaliser in the middle of another test (`Wiring`).
        for built in opened:
            for client in clients_of(built):
                if isinstance(client, anthropic.AsyncAnthropic):
                    await client.close()
                else:
                    client.close()

    answered = answer_in(raised.value)
    # Where the request went, which is the whole claim: the configured base URL
    # with the SDK's own path under it. Asserted for **every** answer, whatever
    # its status, because arriving at the wrong host is the failure this test
    # exists to catch. The query is left out of the comparison -- one engine
    # asks the vendor for a beta -- and the key is not in a URL at all, being a
    # header.
    sent = answered.response.request.url
    assert f"{sent.scheme}://{sent.host}{sent.path}" == ENDPOINT
    # What came back should be a refusal about the credential. Anything else is
    # the vendor having a view of its own today -- a rate limit, a block, a bad
    # gateway -- and is not this build's business, so it is a skip that says
    # what was answered rather than a red build.
    if answered.status_code != UNAUTHORIZED:
        pytest.skip(f"{ENDPOINT} answered {answered.status_code}, not {UNAUTHORIZED}")
    assert REFUSAL in str(answered.body)
