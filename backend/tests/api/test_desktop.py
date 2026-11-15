# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The desktop, as processes: ``robinauts desktop`` on a data directory of the test's own, with a
stand-in browser in the window's place (``util/desktop.py``).

oneberth's launcher starts the backend, each worker and neorc's worker as processes of their
own, then the window, signed in with a ticket. A turn runs on a worker's process, whose engines
keep their state in its folder of the data directory, and the backend opens none of it. A flow
the person starts runs on neorc's worker, and its handler's conversation is the person's. A
second start brings the window forward and starts nothing. Closing the window stops every
process, a running turn included; a start again remembers the conversations. A backend that is
killed takes its workers down with it, neorc's included. No process outlives a test.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from util.desktop import Desktop, gone, open_files
from util.fake_openai import FakeLocalGPTServer, FakeModel, HoldingEchoModel
from util.stack import API_KEY, CONFIG

pytestmark = pytest.mark.io

REPO = Path(__file__).resolve().parents[3]

FLOW = """
name: greet
version: 0.1.0
output: tasks.open_guesser
steps:
  open_guesser:
    queue: default
    handler: robinauts_tasks:open_guesser
    params:
      run: neorc.flow_run_id
"""
"""One step of the shipped handlers: a conversation opened for the run's starter."""

RUN_SECONDS = 120.0


@pytest.fixture
def fake_model() -> FakeModel:
    return HoldingEchoModel()


@pytest.fixture
def desktop(tmp_path: Path, local_gpt: FakeLocalGPTServer) -> Iterator[Desktop]:
    # Pydantic AI's agent runs on a worker of its own.
    config = CONFIG.format(base_url=local_gpt.base_url, system_prompt="")
    config = config.replace('engine = "pydantic-ai"\n', 'engine = "pydantic-ai"\nworker = "w2"\n')
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "greet.yaml").write_text(FLOW)
    made = Desktop(tmp_path, config, {**API_KEY, "ROBINAUTS_FLOWS_DIR": str(flows)})
    try:
        yield made
    finally:
        model = local_gpt.model
        if isinstance(model, HoldingEchoModel):
            model.release.set()
        made.stop_all()


def turn(client: httpx.Client, agent: str, text: str) -> str:
    started = client.post("/robinauts/api/turns", json={"agent_id": agent, "text": text})
    assert '"RUN_FINISHED"' in started.text, started.text
    return started.headers["x-robinauts-conversation-id"]


def conversations(client: httpx.Client) -> dict[str, dict[str, Any]]:
    listed = client.get("/robinauts/api/conversations")
    assert listed.status_code == 200, listed.text
    return {c["id"]: c for c in listed.json()["items"]}


def finished_run(client: httpx.Client, run: str) -> dict[str, Any]:
    deadline = time.monotonic() + RUN_SECONDS
    while time.monotonic() < deadline:
        found = client.get(f"/neorc/runs/{run}").json()
        if found["status"] != "active":
            return found
        time.sleep(0.5)
    raise AssertionError(f"the run did not end within {RUN_SECONDS:g} s")


def test_the_desktop_runs_each_part_as_a_process_and_stops_them_with_the_window(
    desktop: Desktop, local_gpt: FakeLocalGPTServer
) -> None:
    first = desktop.launch()
    opened = desktop.heard("opened")
    assert (opened["origin"], opened["at"]) == (desktop.front_url, "/ui/")
    parts = desktop.parts()
    assert sorted(parts) == ["backend", "default", "neorc", "w2", "window"]
    data = desktop.data

    with desktop.client(opened) as client:
        session = client.get("/auth/session").json()
        assert (session["mode"], session["principal"]["kind"]) == ("local", "user")
        langchain = turn(client, "langchain", "hello")
        pydantic_ai = turn(client, "pydantic_ai", "hello")
        assert client.get("/health").json() == {
            "status": "ok",
            "components": {"robinauts": "ok", "neorc": "ok"},
        }

        # Each worker's engines keep their state in its own folder, which its process alone
        # opens; the backend opens the records.
        assert (data / "workers" / "default" / "langchain.db").is_file()
        assert (data / "workers" / "w2" / "pydantic-ai.db").is_file()
        held = {part: open_files(pid) for part, pid in parts.items()}
        within = {part: [f for f in held[part] if f.is_relative_to(data)] for part in held}
        assert {f.name for f in within["backend"]} >= {"robinauts.db", "neorc.db", "oneberth.db"}
        assert not [f for f in within["backend"] if f.is_relative_to(data / "workers")]
        for worker in ("default", "w2"):
            assert within[worker], worker
            assert all(f.is_relative_to(data / "workers" / worker) for f in within[worker])
        assert within["neorc"] == []

        # A flow the person starts runs on neorc's worker, and its handler's conversation is
        # the person's.
        before = conversations(client)
        started = client.post("/neorc/flows/greet/runs", json={"inputs": {}})
        assert started.status_code == 201, started.text
        run = finished_run(client, started.json()["id"])
        assert run["status"] == "succeeded", run
        opened_by_flow = conversations(client).keys() - before.keys()
        assert len(opened_by_flow) == 1

        # A second start brings the window forward and starts nothing.
        second = desktop.launch()
        assert desktop.ended(second) == 0
        forwarded = desktop.heard("forwarded")
        assert forwarded["line"].startswith(f"forwarded {desktop.front_url}/auth/local?ticket=")
        assert desktop.parts() == parts

        # A turn that runs when the window closes ends, and so does every process.
        holding = local_gpt.model
        assert isinstance(holding, HoldingEchoModel)
        streamed: list[str] = []

        def stream() -> None:
            with httpx.Client(base_url=opened["origin"], headers=client.headers) as other:
                answer = other.post(
                    "/robinauts/api/turns",
                    json={"agent_id": "langchain", "text": "hold"},
                    timeout=120,
                )
                streamed.append(answer.text)

        streaming = threading.Thread(target=stream, daemon=True)
        streaming.start()
        assert holding.asked.wait(60)
    os.kill(parts["window"], signal.SIGTERM)
    assert desktop.ended(first) == 0
    assert desktop.left_behind() == []
    streaming.join(30)
    assert len(streamed) == 1
    assert '"RUN_FINISHED"' not in streamed[0]

    # A start again on the data directory remembers the conversations, and the turn ended.
    again = desktop.launch()
    reopened = desktop.heard("opened")
    assert reopened["origin"] == opened["origin"]
    with desktop.client(reopened) as client:
        remembered = conversations(client)
        assert {langchain, pydantic_ai, *opened_by_flow} < remembered.keys()
        assert len(remembered) == 4
        assert not any(c["running"] for c in remembered.values())
        again_parts = desktop.parts()
        # A backend killed takes the workers down, neorc's included, and the launcher stops.
        os.kill(again_parts["backend"], signal.SIGKILL)
    assert desktop.ended(again) == 1
    assert gone(list(again_parts.values()), 30)
    assert desktop.left_behind() == []


def test_a_killed_launcher_takes_every_part_down(desktop: Desktop) -> None:
    launched = desktop.launch()
    desktop.heard("opened")
    parts = desktop.parts()
    assert sorted(parts) == ["backend", "default", "neorc", "w2", "window"]
    launched.kill()
    launched.wait()
    # The window is the launcher's to stop: a browser does not watch its parent.
    os.kill(parts.pop("window"), signal.SIGTERM)
    assert gone(list(parts.values()), 60), desktop.parts()
    assert desktop.left_behind() == []
