# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The everyday operations of ``tests/e2e/master``, step for step, over the HTTP API in process.

The configuration is the one of ``util.stack``, the store is in memory, and each engine is a
``create_autospec`` of ``AgentEngine`` injected into ``compose``. The engine answers with its
prompt and a checkpoint numbered after its call, so the checkpoint a turn is given shows what the
engine was asked to remember. To "alpha three" it also reasons, and calls a tool in the middle
of its text.
"""

from __future__ import annotations

import json
import tomllib
import uuid
from collections.abc import AsyncIterator
from itertools import groupby
from typing import Any
from unittest.mock import MagicMock, create_autospec

import httpx
import pytest

from robinauts.agent_engines.contract.domain import (
    Done,
    ProviderKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine
from robinauts.controller.composition import compose, configure
from robinauts.controller.contract.domain import StorageConfig, StorageKind
from robinauts.web.app import LOCAL_IDENTITY, create_app
from util import stack
from util.aio import asyncio_test


def mock_engine() -> MagicMock:
    engine = create_autospec(AgentEngine, spec_set=True, instance=True)
    engine.kinds.return_value = frozenset(ProviderKind)

    async def echo(session_id: uuid.UUID, agent: Any, prompt: str, **_: Any) -> AsyncIterator[Any]:
        if prompt == "alpha three":
            yield ReasoningDelta("adding")
            yield TextDelta("alpha ")
            yield ToolCall("c1", "add", {"a": 1, "b": 2})
            yield ToolResult("c1", "add", "3")
            yield TextDelta("three")
        else:
            yield TextDelta(prompt)
        yield Done(prompt, checkpoint_id=f"cp{engine.stream.call_count}")

    engine.stream.side_effect = echo
    return engine


class Api:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.streamed: list[str] = []
        """The AG-UI event types of the last turn, repeats collapsed."""
        self.run_id = ""
        """The run of the last turn."""

    async def get(self, path: str) -> Any:
        response = await self.http.get(path)
        assert response.status_code == 200, response.text
        return response.json()

    async def turn(self, path: str, **body: Any) -> str:
        """The conversation's id. Every turn streams its own run, its events numbered from one."""
        response = await self.http.post(path, json=body)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        cid, run = (response.headers[f"x-robinauts-{h}-id"] for h in ("conversation", "run"))
        lines = response.text.splitlines()
        events = [json.loads(x.removeprefix("data: ")) for x in lines if x.startswith("data: ")]
        ids = [int(x.removeprefix("id: ")) for x in lines if x.startswith("id: ")]
        assert ids == list(range(1, len(ids) + 1))
        assert run != cid
        assert (events[0]["threadId"], events[0]["runId"]) == (cid, run)
        self.streamed = [t for t, _ in groupby(e["type"] for e in events)]
        assert self.streamed[-1] == "RUN_FINISHED"
        self.run_id = run
        return cid

    async def start(self, agent: str, text: str) -> str:
        return await self.turn("/api/turns", agent_id=agent, text=text)

    async def opened(self, cid: str) -> dict[str, Any]:
        return await self.get(f"/api/conversations/{cid}")

    async def thread(self, cid: str) -> list[str]:
        """The text of each message, questions and answers in order."""
        messages = (await self.opened(cid))["messages"]
        return ["".join(p["text"] for p in m["parts"] if p["kind"] == "text") for m in messages]

    async def message(self, cid: str, index: int) -> str:
        return (await self.opened(cid))["messages"][index]["id"]

    async def history(self) -> list[str]:
        return [c["title"] for c in (await self.get("/api/conversations"))["items"]]


def echoed(*questions: str) -> list[str]:
    return [text for q in questions for text in (q, q)]


@pytest.mark.parametrize("agent", stack.AGENTS)
@asyncio_test
async def test_everyday_use(agent: str) -> None:
    tables = tomllib.loads(stack.config_for("http://model.invalid/v1"))
    config, secret_for = configure(tables, dict(stack.API_KEY))
    engines = {name: mock_engine() for name in ("langchain", "pydantic-ai")}
    engine = engines[config.agents[agent].engine]
    composed = compose(
        config,
        storage=StorageConfig(StorageKind.IN_MEMORY),
        secret_for=secret_for,
        engines={name: lambda *_, built=built: built for name, built in engines.items()},
    )
    app = create_app(composed, sign_in=None, secret_for=secret_for)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        api = Api(http)

        def last_call(model: str, prompt: str, checkpoint: str | None) -> None:
            args, kwargs = engine.stream.call_args
            assert args[2] == prompt
            assert (kwargs["model"], kwargs["checkpoint_id"]) == (model, checkpoint)

        # --- Opening the app ---------------------------------------------------------------

        # 0. The local user, and the agents and models of the configuration.
        session = await api.get("/auth/session")
        assert (session["sign_in"], session["local_development"]) == (False, True)
        assert (session["providers"], session["user"]["provider"]) == ([], LOCAL_IDENTITY.provider)
        assert (await http.get("/auth/login/okta")).status_code == 404
        agents = (await api.get("/api/agents"))["items"]
        assert [(a["id"], a["title"], a["model"]) for a in agents] == [
            ("langchain", "LangChain", "local_gpt"),
            ("pydantic_ai", "Pydantic AI", "local_gpt"),
        ]
        models = (await api.get("/api/models"))["items"]
        assert [(m["id"], m["title"]) for m in models] == [
            ("local_gpt", "Local GPT"),
            ("local_gpt_2", "Local GPT 2"),
        ]

        # --- Conversation A -----------------------------------------------------------------

        # 1-2. A first message on the agent: answered by its engine, on this run, and listed
        #      under its title.
        a = await api.start(agent, "alpha one")
        opened = await api.opened(a)
        assert opened["run_id"] is None
        provenance = opened["messages"][1]["provenance"]
        assert (provenance["engine"], provenance["run_id"]) == (
            config.agents[agent].engine,
            api.run_id,
        )
        assert await api.thread(a) == echoed("alpha one")
        assert await api.history() == ["alpha one"]
        last_call("local_gpt", "alpha one", None)
        engine.create.assert_awaited_once_with(uuid.UUID(a))

        # 3. Two more, each from the checkpoint of the answer before it.
        await api.turn(
            f"/api/conversations/{a}/turns", text="alpha two", parent_id=await api.message(a, 1)
        )
        await api.turn(
            f"/api/conversations/{a}/turns", text="alpha three", parent_id=await api.message(a, 3)
        )
        assert await api.thread(a) == echoed("alpha one", "alpha two", "alpha three")
        last_call("local_gpt", "alpha three", "cp2")

        # 3b. That answer streamed its reasoning, then its text around a tool round, and keeps
        #     its parts in that order, without the reasoning.
        assert api.streamed == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "REASONING_MESSAGE_START",
            "REASONING_MESSAGE_CONTENT",
            "REASONING_MESSAGE_END",
            "TEXT_MESSAGE_CONTENT",
            "TOOL_CALL_START",
            "TOOL_CALL_ARGS",
            "TOOL_CALL_END",
            "TOOL_CALL_RESULT",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]
        answer = (await api.opened(a))["messages"][5]
        assert answer["parts"] == [
            {"kind": "text", "text": "alpha "},
            {"kind": "tool_call", "call_id": "c1", "name": "add", "arguments": {"a": 1, "b": 2}},
            {"kind": "tool_result", "call_id": "c1", "text": "3", "is_error": False},
            {"kind": "text", "text": "three"},
        ]

        # 4. Edit the middle message: everything after it is cut, and it goes on from the
        #    answer before it.
        await api.turn(
            f"/api/conversations/{a}/turns", text="alpha TWO", edit=await api.message(a, 2)
        )
        assert await api.thread(a) == echoed("alpha one", "alpha TWO")
        last_call("local_gpt", "alpha TWO", "cp1")
        assert engine.stream.call_count == 4

        # 5. Regenerate the last answer: a new answer, from the same checkpoint.
        replaced = await api.message(a, 3)
        await api.turn(f"/api/conversations/{a}/turns", regenerate=replaced)
        assert await api.message(a, 3) != replaced
        assert await api.thread(a) == echoed("alpha one", "alpha TWO")
        assert engine.stream.call_count == 5
        last_call("local_gpt", "alpha TWO", "cp1")

        # --- Conversation B -----------------------------------------------------------------

        # 6. A second conversation, listed above A, starting from nothing.
        b = await api.start(agent, "beta one")
        assert await api.thread(b) == echoed("beta one")
        assert await api.history() == ["beta one", "alpha one"]
        last_call("local_gpt", "beta one", None)

        # 7. Change B's model: the next message goes to the other model, from B's checkpoint.
        moved = await http.put(f"/api/conversations/{b}/model", json={"model_id": "local_gpt_2"})
        assert moved.json()["model"] == "local_gpt_2"
        await api.turn(
            f"/api/conversations/{b}/turns",
            text="beta two",
            parent_id=await api.message(b, 1),
            model_id="local_gpt_2",
        )
        assert await api.thread(b) == echoed("beta one", "beta two")
        last_call("local_gpt_2", "beta two", "cp6")

        # --- Back to A ----------------------------------------------------------------------

        # 8. A has its edited thread and its own model.
        assert await api.thread(a) == echoed("alpha one", "alpha TWO")
        assert (await api.opened(a))["conversation"]["model"] == "local_gpt"

        # 9. A message in A goes on from the regenerated answer, to A's model.
        await api.turn(
            f"/api/conversations/{a}/turns", text="alpha four", parent_id=await api.message(a, 3)
        )
        assert await api.thread(a) == echoed("alpha one", "alpha TWO", "alpha four")
        last_call("local_gpt", "alpha four", "cp5")

        # --- Managing conversations ---------------------------------------------------------

        # 10. Delete B: it leaves the list, and the engine forgets it.
        assert (await http.delete(f"/api/conversations/{b}")).status_code == 204
        assert await api.history() == ["alpha one"]
        engine.forget.assert_awaited_once_with(uuid.UUID(b))

        # 11. Rename A: the title is its first line, trimmed.
        renamed = await http.patch(
            f"/api/conversations/{a}", json={"title": "  Project alpha\nignored"}
        )
        assert renamed.json()["title"] == "Project alpha"
        assert await api.history() == ["Project alpha"]

        # --- As left ------------------------------------------------------------------------

        # 12. A opens as it was left.
        opened = await api.opened(a)
        assert opened["conversation"]["title"] == "Project alpha"
        assert opened["conversation"]["model"] == "local_gpt"
        assert await api.thread(a) == echoed("alpha one", "alpha TWO", "alpha four")

        # 13. B is not found, and no turn ran beyond the eight above.
        assert (await http.get(f"/api/conversations/{b}")).status_code == 404
        assert engine.stream.call_count == 8

        # 14. Signing out changes nothing: every route still answers as the local user.
        assert (await http.post("/auth/logout")).status_code == 204
        assert await api.history() == ["Project alpha"]

        # 15. Each engine was built and set up once, and only the agent's engine ran a turn.
        for built in engines.values():
            built.setup.assert_awaited_once()
            assert built.stream.called is (built is engine)
