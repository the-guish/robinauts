# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The platform's own tools over HTTP, in both engines: an agent that names ``sessions`` is
offered ``search_sessions`` like any other tool, and a search from a new conversation finds the
earlier one, of the same person with the same agent alone. An agent that names ``basic_edit``
is offered the eight file tools, and what it writes in one conversation it reads in the next,
from the folder of that person with that agent alone, on the disk where ``[files]`` says."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import CallTool, FakeLocalGPTServer
from util.fake_openai.echo_model import last_question

pytestmark = requires_postgres

RIDDLE = "Why is a raven like a writing-desk?"
NO_IDEA = "No idea"
ASKED_AGAIN = "what's the deal about that raven?"
FOUND = "You asked me that riddle before, and I had no idea."

SEARCHING = """
[agents.langchain_sessions]
title = "LangChain, with its sessions"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "langchain"
tools = ["sessions"]

[agents.pydantic_ai_sessions]
title = "Pydantic AI, with its sessions"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "pydantic-ai"
tools = ["sessions"]
"""


class RavenModel:
    """Has no idea about the riddle. Asked about the raven again, it searches the earlier
    sessions for "raven", then answers ``FOUND``. Anything else, it echoes."""

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        question = last_question(messages)
        if question == RIDDLE:
            return NO_IDEA
        if question == ASKED_AGAIN:
            if messages[-1]["role"] == "tool":
                return FOUND
            return CallTool("search_sessions", {"query": "raven"}, call_id="call_search")
        return question


@pytest.fixture(scope="module")
def searching(
    model_server: FakeLocalGPTServer, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    config = tmp_path_factory.mktemp("sessions") / "robinauts.toml"
    agents = SEARCHING.format(system_prompt=stack.SYSTEM_PROMPT)
    config.write_text(stack.config_for(model_server.base_url) + agents)
    with (
        stack.database(config) as url,
        stack.server(config, url, env=stack.API_KEY) as server,
    ):
        yield server


@pytest.fixture
def raven_api(searching: str, model_server: FakeLocalGPTServer) -> Iterator[httpx.Client]:
    model_server.model = RavenModel()
    model_server.received.clear()
    with httpx.Client(base_url=searching, timeout=30.0) as client:
        yield client


def started(api: httpx.Client, agent: str, text: str) -> str:
    """A new conversation with that agent, its turn finished: its id."""
    response = api.post("/api/turns", json={"agent_id": agent, "text": text})
    assert response.status_code == 200, response.text
    data = [line for line in response.text.splitlines() if line.startswith("data: ")]
    ended = json.loads(data[-1].removeprefix("data: "))
    assert (ended["type"], ended.get("outcome")) == ("RUN_FINISHED", None), ended
    return response.headers["x-robinauts-conversation-id"]


def messages_of(api: httpx.Client, cid: str) -> list[dict[str, Any]]:
    opened = api.get(f"/api/conversations/{cid}")
    assert opened.status_code == 200, opened.text
    return opened.json()["messages"]


@pytest.mark.parametrize("engine", ["langchain", "pydantic_ai"])
def test_a_new_conversation_searches_the_earlier_one_with_the_same_agent(
    raven_api: httpx.Client, model_server: FakeLocalGPTServer, engine: str
) -> None:
    agent = f"{engine}_sessions"

    # The riddle, asked once of this agent, and once of the same engine's agent without the
    # tool: the search must find the first alone.
    riddle = started(raven_api, agent, RIDDLE)
    [question, answer] = messages_of(raven_api, riddle)
    assert [p["text"] for p in question["parts"]] == [RIDDLE]
    assert [p["text"] for p in answer["parts"]] == [NO_IDEA]
    started(raven_api, engine, RIDDLE)

    # A new conversation: the model is offered the search like any other tool, and calls it.
    model_server.received.clear()
    again = started(raven_api, agent, ASKED_AGAIN)
    offered = [tool["function"] for tool in model_server.received[0]["tools"]]
    [search] = [tool for tool in offered if tool["name"] == "search_sessions"]
    assert search["parameters"]["required"] == ["query"]

    # The call and its result are in the transcript, as any tool's are, before the answer.
    [_, answer] = messages_of(raven_api, again)
    call, result, text = answer["parts"]
    assert (call["kind"], call["name"], call["arguments"]) == (
        "tool_call",
        "search_sessions",
        {"query": "raven"},
    )
    assert (result["kind"], result["call_id"], result["is_error"]) == (
        "tool_result",
        call["call_id"],
        False,
    )
    assert (text["kind"], text["text"]) == ("text", FOUND)

    # What it found: the riddle's conversation alone, quoting the question. Not the one with
    # the other agent, nor the conversation the search was made in.
    found = json.loads(result["text"])
    assert [s["session_id"] for s in found["sessions"]] == [riddle]
    assert [(m["role"], m["excerpt"]) for m in found["sessions"][0]["matches"]] == [
        ("user", RIDDLE)
    ]

    # And the model was handed that same result before it answered.
    [tool_message] = [m for m in model_server.received[-1]["messages"] if m["role"] == "tool"]
    assert json.loads(tool_message["content"]) == found


# --- the files ----------------------------------------------------------------------

NOTE = "note in "
RECALL = "read "
NOTED = "Noted."
NOTHING_NOTED = "Nothing noted."

FILE_TOOLS = [
    "read_file",
    "list_folder",
    "write_file",
    "replace_in_file",
    "replace_lines",
    "move_file",
    "copy_file",
    "delete_file",
]

NOTING = """
[agents.langchain_notes]
title = "LangChain, with its notes"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "langchain"
tools = ["basic_edit"]

[agents.pydantic_ai_notes]
title = "Pydantic AI, with its notes"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "pydantic-ai"
tools = ["basic_edit"]

[files]
path = "{files}"
"""


class NotesModel:
    """On ``note in <path>: <text>`` it writes the text to that file and answers ``NOTED``. On
    ``read <path>`` it reads the file and answers its content, or ``NOTHING_NOTED`` when the
    tool refused. Anything else, it echoes. The tests share one server and one files root, so
    each names files of its own."""

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        question = last_question(messages)
        answered = messages[-1]["role"] == "tool"
        if question.startswith(NOTE):
            if answered:
                return NOTED
            path, _, text = question.removeprefix(NOTE).partition(": ")
            return CallTool("write_file", {"path": path, "content": text}, call_id="call_write")
        if question.startswith(RECALL):
            if not answered:
                path = question.removeprefix(RECALL)
                return CallTool("read_file", {"path": path}, call_id="call_read")
            # A refused read is an error the engine words its own way: no `content` in it.
            try:
                return json.loads(messages[-1]["content"])["content"]
            except (ValueError, KeyError, TypeError):
                return NOTHING_NOTED
        return question


@pytest.fixture(scope="module")
def noting(
    model_server: FakeLocalGPTServer, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, Path]]:
    """A server whose agents keep files under a root of this test's, and that root."""
    here = tmp_path_factory.mktemp("notes")
    files = here / "files"
    config = here / "robinauts.toml"
    agents = NOTING.format(system_prompt=stack.SYSTEM_PROMPT, files=files)
    config.write_text(stack.config_for(model_server.base_url) + agents)
    with (
        stack.database(config) as url,
        stack.server(config, url, env=stack.API_KEY) as server,
    ):
        yield server, files


@pytest.fixture
def notes_api(
    noting: tuple[str, Path], model_server: FakeLocalGPTServer
) -> Iterator[tuple[httpx.Client, Path]]:
    model_server.model = NotesModel()
    model_server.received.clear()
    with httpx.Client(base_url=noting[0], timeout=30.0) as client:
        yield client, noting[1]


@pytest.mark.parametrize("engine", ["langchain", "pydantic_ai"])
def test_a_note_written_in_one_conversation_is_read_in_the_next_from_the_owners_folder(
    notes_api: tuple[httpx.Client, Path], model_server: FakeLocalGPTServer, engine: str
) -> None:
    api, files = notes_api
    agent = f"{engine}_notes"
    path = f"notes/{uuid.uuid4().hex}.md"
    text = f"the raven, told the {engine} agent"

    # The model is offered the eight file tools like any other tool, and writes the note.
    noted = started(api, agent, f"{NOTE}{path}: {text}")
    offered = [tool["function"]["name"] for tool in model_server.received[0]["tools"]]
    assert sorted(offered) == sorted(FILE_TOOLS)
    [_, answer] = messages_of(api, noted)
    call, result, said = answer["parts"]
    assert (call["name"], call["arguments"]) == ("write_file", {"path": path, "content": text})
    assert (result["is_error"], json.loads(result["text"])) == (
        False,
        {"path": path, "bytes": len(text), "created": True},
    )
    assert said["text"] == NOTED

    # Where it is: <root>/<the person's id>/<agent>/<path>.
    me = api.get("/auth/session").json()["user"]["id"]
    assert (files / me / agent / path).read_text() == text

    # A new conversation with the same agent reads it back.
    [_, answer] = messages_of(api, started(api, agent, RECALL + path))
    call, result, said = answer["parts"]
    assert (call["name"], call["arguments"]) == ("read_file", {"path": path})
    assert (result["is_error"], json.loads(result["text"])["content"]) == (False, text)
    assert said["text"] == text


def test_another_agent_of_the_same_person_sees_no_note(
    notes_api: tuple[httpx.Client, Path], model_server: FakeLocalGPTServer
) -> None:
    api, files = notes_api
    path = f"{uuid.uuid4().hex}.md"
    started(api, "langchain_notes", f"{NOTE}{path}: for the langchain agent alone")
    # The other engine's agent has a folder of its own, and the note is not in it.
    [_, answer] = messages_of(api, started(api, "pydantic_ai_notes", RECALL + path))
    call, result, said = answer["parts"]
    assert call["name"] == "read_file"
    assert result["is_error"] is True
    assert f"Nothing at {path!r}." in result["text"]
    assert said["text"] == NOTHING_NOTED
    me = api.get("/auth/session").json()["user"]["id"]
    assert (files / me / "langchain_notes" / path).is_file()
    assert not (files / me / "pydantic_ai_notes" / path).exists()
