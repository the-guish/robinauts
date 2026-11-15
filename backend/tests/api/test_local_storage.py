# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The local development mode on local storage: the server makes ``robinauts.db`` in the data
directory, and its worker keeps its engines' state in its folder there. A second start on that
directory finds the conversations of the first, and each engine remembers them."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from util import stack
from util.fake_openai import FakeLocalGPTServer

pytestmark = pytest.mark.io

CONFIG = """
[model_providers.echo]
kind = "anthropic"
api_key_env = "ECHO_API_KEY"

[models.echo]
provider = "echo"
name = "echo"

[agents.echo]
title = "Echo"
system_prompt = ""
models = ["echo"]
engine = "echo"
"""


@contextmanager
def api(config: Path, data: Path) -> Iterator[httpx.Client]:
    """A client of a server in the local development mode on ``data``, stopped afterwards."""
    port = stack.free_port()
    environment = {**os.environ, "ROBINAUTS_CONFIG": str(config), "ECHO_API_KEY": "not-a-key"}
    environment.update(stack.API_KEY)
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    environment["ROBINAUTS_DATA_DIR"] = str(data)
    command = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    with subprocess.Popen(command, env=environment) as running:
        url = f"http://127.0.0.1:{port}"
        try:
            stack.wait_until_up(url, running)
            with httpx.Client(base_url=url, timeout=30.0, headers={"origin": url}) as client:
                yield client
        finally:
            running.terminate()
            # uvicorn raises SIGTERM again once it has shut down and closed the store.
            assert running.wait(timeout=30) == -signal.SIGTERM


def test_a_second_start_on_the_data_directory_finds_the_first_ones_conversations(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    data = tmp_path / "data"
    with api(config, data) as first:
        started = first.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
        assert started.status_code == 200, started.text
        assert '"RUN_FINISHED"' in started.text
        cid = started.headers["x-robinauts-conversation-id"]
        said = first.get(f"/api/conversations/{cid}").json()["messages"]
        assert [m["role"] for m in said] == ["user", "assistant"]
    assert (data / "robinauts.db").is_file()

    with api(config, data) as second:
        listed = second.get("/api/conversations").json()["items"]
        assert [c["id"] for c in listed] == [cid]
        assert second.get(f"/api/conversations/{cid}").json()["messages"] == said
        another = second.post("/api/turns", json={"agent_id": "echo", "text": "again"})
        assert '"RUN_FINISHED"' in another.text
        assert len(second.get("/api/conversations").json()["items"]) == 2


def test_each_engine_remembers_a_conversation_across_a_restart(
    tmp_path: Path, local_gpt: FakeLocalGPTServer
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(stack.config_for(local_gpt.base_url))
    data = tmp_path / "data"
    answers = {}
    with api(config, data) as first:
        for agent in stack.AGENTS:
            started = first.post("/api/turns", json={"agent_id": agent, "text": "hello"})
            assert '"RUN_FINISHED"' in started.text, started.text
            cid = started.headers["x-robinauts-conversation-id"]
            answers[agent] = (cid, first.get(f"/api/conversations/{cid}").json()["messages"][1])
    folder = data / "workers" / "default"
    assert sorted(p.name for p in folder.glob("*.db")) == ["langchain.db", "pydantic-ai.db"]

    with api(config, data) as second:
        for agent, (cid, answer) in answers.items():
            local_gpt.received.clear()
            turns = f"/api/conversations/{cid}/turns"
            again = second.post(turns, json={"parent_id": answer["id"], "text": "again"})
            assert '"RUN_FINISHED"' in again.text, again.text
            assert stack.sent(local_gpt.received[-1]) == [
                ("user", "hello"),
                ("assistant", "hello"),
                ("user", "again"),
            ], agent
