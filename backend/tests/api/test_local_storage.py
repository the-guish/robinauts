# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""oneberth's local mode on local storage: the server makes ``robinauts.db``, ``oneberth.db`` and
``neorc.db`` in the data directory, and its worker keeps its engines' state in its folder there.
The one person signs in with a ticket. A second start on that directory finds the conversations
of the first, and each engine remembers them. Behind a forward, the front listener binds every
address and the browser reaches it on a loopback one."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from oneberth.identity import CredentialKind, new_secret

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


def environment_of(config: Path, data: Path) -> dict[str, str]:
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ECHO_API_KEY": "not-a-key",
    }
    environment.update(stack.API_KEY)
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    environment["ROBINAUTS_DATA_DIR"] = str(data)
    return environment


def command_of(port: int, *more: str) -> list[str]:
    worker = str(stack.free_port())
    return [
        str(stack.ROBINAUTS),
        "start",
        "--local",
        "--port",
        str(port),
        "--worker-port",
        worker,
        *more,
    ]


@contextmanager
def api(config: Path, data: Path, *more: str) -> Iterator[httpx.Client]:
    """A client of a server in the local mode on ``data``, the local user signed in with a
    ticket its launch secret asked for, stopped afterwards. ``more`` are more arguments of
    ``start``."""
    port = stack.free_port()
    secret = new_secret(CredentialKind.LAUNCH)
    command = command_of(port, *more)
    with stack.launched(command, environment_of(config, data), secret) as running:
        url = stack.Server(f"http://127.0.0.1:{port}", secret)
        try:
            stack.wait_until_up(url, running)
            with httpx.Client(base_url=url, timeout=30.0) as client:
                assert client.get("/robinauts/api/conversations").status_code == 401
                yield stack.signed_in(client, url)
        finally:
            running.terminate()
            # oneberth's server stops on SIGTERM, then the components and the stores.
            assert running.wait(timeout=30) == 0


def test_a_start_by_hand_prints_the_address_that_signs_the_person_in(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    data = tmp_path / "data"
    port = stack.free_port()
    with subprocess.Popen(
        command_of(port),
        env=environment_of(config, data),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
    ) as running:
        try:
            assert running.stdout is not None
            lines = [running.stdout.readline().strip() for _ in range(2)]
            assert lines[0] == "ready"
            assert lines[1].startswith(f"open http://127.0.0.1:{port}/auth/local?ticket=")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
                assert client.get(lines[1].removeprefix("open ")).status_code == 303
                session = client.get("/auth/session").json()
                assert session["mode"] == "local"
                assert "robinauts/user" in session["roles"]
                listed = client.get("/robinauts/api/conversations")
                assert listed.status_code == 200, listed.text
        finally:
            running.terminate()
            running.wait(timeout=30)
    assert sorted(p.name for p in data.glob("*.db")) == [
        "neorc.db",
        "oneberth.db",
        "robinauts.db",
    ]


def test_a_second_start_on_the_data_directory_finds_the_first_ones_conversations(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    data = tmp_path / "data"
    with api(config, data) as first:
        started = first.post("/robinauts/api/turns", json={"agent_id": "echo", "text": "hello"})
        assert started.status_code == 200, started.text
        assert '"RUN_FINISHED"' in started.text
        cid = started.headers["x-robinauts-conversation-id"]
        said = first.get(f"/robinauts/api/conversations/{cid}").json()["messages"]
        assert [m["role"] for m in said] == ["user", "assistant"]
    assert (data / "robinauts.db").is_file()

    with api(config, data) as second:
        listed = second.get("/robinauts/api/conversations").json()["items"]
        assert [c["id"] for c in listed] == [cid]
        assert second.get(f"/robinauts/api/conversations/{cid}").json()["messages"] == said
        another = second.post("/robinauts/api/turns", json={"agent_id": "echo", "text": "again"})
        assert '"RUN_FINISHED"' in another.text
        assert len(second.get("/robinauts/api/conversations").json()["items"]) == 2


def test_each_engine_remembers_a_conversation_across_a_restart(
    tmp_path: Path, local_gpt: FakeLocalGPTServer
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(stack.config_for(local_gpt.base_url))
    data = tmp_path / "data"
    answers = {}
    with api(config, data) as first:
        for agent in stack.AGENTS:
            started = first.post("/robinauts/api/turns", json={"agent_id": agent, "text": "hello"})
            assert '"RUN_FINISHED"' in started.text, started.text
            cid = started.headers["x-robinauts-conversation-id"]
            answers[agent] = (
                cid,
                first.get(f"/robinauts/api/conversations/{cid}").json()["messages"][1],
            )
    folder = data / "workers" / "default"
    assert sorted(p.name for p in folder.glob("*.db")) == [
        "langchain.db",
        "pydantic-ai.db",
    ]

    with api(config, data) as second:
        for agent, (cid, answer) in answers.items():
            local_gpt.received.clear()
            turns = f"/robinauts/api/conversations/{cid}/turns"
            again = second.post(turns, json={"parent_id": answer["id"], "text": "again"})
            assert '"RUN_FINISHED"' in again.text, again.text
            assert stack.sent(local_gpt.received[-1]) == [
                ("user", "hello"),
                ("assistant", "hello"),
                ("user", "again"),
            ], agent


def test_a_server_behind_a_forward_binds_every_address_and_signs_in_on_loopback(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    data = tmp_path / "data"
    with api(config, data, "--host", "0.0.0.0", "--public-host", "127.0.0.1") as client:
        session = client.get("/auth/session").json()
        assert session["mode"] == "local"
        # A write with the session cookie passes the Origin check: the public URL is the
        # loopback address the browser reached.
        started = client.post("/robinauts/api/turns", json={"agent_id": "echo", "text": "hi"})
        assert started.status_code == 200, started.text
        assert '"RUN_FINISHED"' in started.text
