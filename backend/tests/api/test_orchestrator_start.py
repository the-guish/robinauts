# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestrator ``robinauts start`` embeds in the local development mode
(``docs/architecture/orchestration.md``): with a flows folder, neorc's manager is
mounted at ``/neorc``, its UI at ``/neorc/ui/``, behind this server's checks and port; the flows
are uploaded at the start, its scheduler runs in the server and its worker in a process of its
own, against ``/neorc``. A run of ``guess_number`` started through the mount plays the game
between two conversations of the local user, on the echo engine.

neorc is the real one. Its storage follows the server's: in the process without
``ROBINAUTS_DATABASE_URL``, and on that PostgreSQL in the schema ``neorc`` with it."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from util import stack
from util.controller_db import NO_DATABASE

pytestmark = pytest.mark.io

REPO = Path(__file__).resolve().parents[3]

CONFIG = """
[model_providers.echo]
kind = "anthropic"
api_key_env = "ECHO_API_KEY"

[models.haiku]
provider = "echo"
name = "echo-haiku"
title = "Echo Haiku"

[agents.echo]
title = "Echo"
system_prompt = ""
model = "haiku"
engine = "echo"
"""

SHIPPED = REPO / "orchestration" / "flows"
"""The flows folder Robinauts ships, with no settings file: the handlers beside it."""

RUN_SECONDS = 120.0
"""Ten rounds of the game on the echo engine, with a margin for a slow machine."""


def environment_for(config: Path, flows: Path = SHIPPED) -> dict[str, str]:
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ROBINAUTS_FLOWS_DIR": str(flows),
        "ECHO_API_KEY": "not-a-real-key",
        # The worker imports the handlers from the checkout: it must leave nothing there.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    return environment


@contextmanager
def started(config: Path, environment: dict[str, str]) -> Iterator[tuple[str, list[bytes]]]:
    """The URL of a server started on ``config``, and once it has stopped, what it said."""
    port = stack.free_port()
    start = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    said: list[bytes] = []
    with subprocess.Popen(start, env=environment, stderr=subprocess.PIPE) as running:
        try:
            url = f"http://127.0.0.1:{port}"
            stack.wait_until_up(url, running)
            yield url, said
            running.terminate()
            _, stderr = running.communicate(timeout=60)
            said.append(stderr)
        finally:
            if running.poll() is None:
                running.kill()


def finished_run(api: httpx.Client, run_id: str) -> dict[str, Any]:
    """The run once it has ended, waiting on neorc's events through the mount: the long-poll
    goes through this server's port like every other request."""
    deadline = time.monotonic() + RUN_SECONDS
    after = 0
    while time.monotonic() < deadline:
        run = api.get(f"/neorc/runs/{run_id}").json()
        if run["status"] != "active":
            return run
        events = api.get("/neorc/events", params={"after": after, "timeout": 5}, timeout=30)
        assert events.status_code == 200, events.text
        if news := events.json()["events"]:
            after = news[-1]["sequence"]
    raise AssertionError(f"the run did not end within {RUN_SECONDS:g} s")


def play_a_game(url: str) -> None:
    """Start ``guess_number`` through the mount, as the framed UI does, and check what it did:
    the run ends, and its two conversations are the local user's."""
    origin = {"origin": url}
    with httpx.Client(base_url=url, timeout=30.0) as api:
        flows = api.get("/neorc/flows")
        assert flows.status_code == 200, flows.text
        assert "guess_number" in [f["name"] for f in flows.json()["flows"]]
        before = {c["id"] for c in api.get("/api/conversations").json()["items"]}
        begun = api.post("/neorc/flows/guess_number/runs", json={"inputs": {}}, headers=origin)
        assert begun.status_code == 201, begun.text
        run = finished_run(api, begun.json()["id"])
        assert run["status"] == "succeeded", run
        after = api.get("/api/conversations").json()["items"]
        opened = [c for c in after if c["id"] not in before]
        # The game, on the Haiku model, and the guesser.
        assert len(opened) == 2, opened


def test_neorc_is_mounted_in_the_server_and_a_run_of_its_flow_plays_the_game(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    with started(config, environment_for(config)) as (url, said):
        with httpx.Client(base_url=url, timeout=30.0) as api:
            session = api.get("/auth/session").json()
            assert session["flows_url"] == "/neorc/ui/"
            assert api.get("/neorc/health").json() == {"status": "ok"}
        play_a_game(url)
    assert b"neorc is up" in said[0], said[0].decode(errors="replace")
    assert b"in this process" in said[0]


def test_the_ui_is_framable_by_this_server_alone_and_the_api_by_nothing(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    with started(config, environment_for(config)) as (url, _):
        with httpx.Client(base_url=url, timeout=30.0) as api:
            page = api.get("/neorc/ui/")
            api_answer = api.get("/neorc/flows")
            elsewhere = api.get("/neorc/flows", headers={"host": "evil.example"})
            ui_elsewhere = api.get("/neorc/ui/", headers={"host": "evil.example"})
            cross_site = api.post(
                "/neorc/flows/guess_number/runs",
                json={"inputs": {}},
                headers={"origin": "http://evil.example"},
            )
    assert page.status_code == 200, page.text
    assert page.headers["content-type"].startswith("text/html")
    policy = page.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in policy
    assert "'none'" not in policy.split("frame-ancestors")[1]
    # neorc's own policy otherwise: scripts and connections from this origin alone.
    assert "script-src 'self'" in policy
    assert "connect-src 'self'" in policy
    assert page.headers.get_list("x-frame-options") == ["SAMEORIGIN"]
    assert api_answer.status_code == 200
    assert api_answer.headers.get_list("x-frame-options") == ["DENY"]
    assert "frame-ancestors 'none'" in api_answer.headers["content-security-policy"]
    # The local development mode's checks reach the mount as they reach every path.
    assert (elsewhere.status_code, ui_elsewhere.status_code) == (403, 403)
    assert cross_site.status_code == 403


def test_a_start_whose_flows_are_refused_is_refused_before_it_binds(tmp_path: Path) -> None:
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "broken.yaml").write_text("name: broken\nversion: 1.0.0\nsteps: 3\n")
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    port = stack.free_port()
    start = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    environment = environment_for(config, flows)
    refused = subprocess.run(start, env=environment, capture_output=True, timeout=60)
    assert refused.returncode == 1
    assert b"neorc refused the flows in" in refused.stderr, refused.stderr


def test_a_folder_of_one_s_own_flows_is_served_as_its_flows_toml_says(tmp_path: Path) -> None:
    """A folder anywhere, with the shipped handlers as its default, and the UI turned off."""
    flows = tmp_path / "my-flows"
    flows.mkdir()
    (flows / "guess_number.yaml").write_text((SHIPPED / "guess_number.yaml").read_text())
    (flows / "flows.toml").write_text("ui = false\n")
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    with started(config, environment_for(config, flows)) as (url, _):
        with httpx.Client(base_url=url, timeout=30.0) as api:
            listed = api.get("/neorc/flows").json()["flows"]
            page = api.get("/neorc/ui/")
    assert [f["name"] for f in listed] == ["guess_number"]
    assert page.status_code == 404


def test_a_flows_toml_that_is_not_toml_is_refused_before_it_binds(tmp_path: Path) -> None:
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "flows.toml").write_text("ui = \n")
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    start = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(stack.free_port())]
    environment = environment_for(config, flows)
    refused = subprocess.run(start, env=environment, capture_output=True, timeout=30)
    assert refused.returncode == 1
    assert b"flows.toml: not TOML" in refused.stderr, refused.stderr


PROBE_FLOW = """
name: intake_probe
version: 0.1.0
inputs:
  task: string
  repo_branch: string
  model: string
  agent: string
output: tasks.pick
steps:
  pick:
    queue: default
    handler: robinauts_tasks:pick_number
"""
PROBE_FORM = """
title = "Probe"
[inputs.repo_branch]
label = "Repository"
lov = "repo_branch"
[inputs.model]
lov = "model"
[inputs.agent]
lov = "agent"
"""
PROBE_SETTINGS = """
[repositories.app]
url = "https://git.example/app"
branches = ["main", "next"]
"""


def probe_folder(tmp_path: Path, form: str = PROBE_FORM) -> Path:
    """A flows folder of one flow with four string inputs, three of them from lists of values."""
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "intake_probe.yaml").write_text(PROBE_FLOW)
    (flows / "intake_probe.form.toml").write_text(form)
    (flows / "flows.toml").write_text(PROBE_SETTINGS)
    return flows


def test_a_flow_with_a_form_is_listed_with_its_values_and_started_with_them(
    tmp_path: Path,
) -> None:
    """The form's lists come from the flows folder's repositories and the agents'
    configuration; a run starts once its inputs fit them, with them as they came."""
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    inputs = {
        "task": "Fix the bug",
        "repo_branch": "app@next",
        "model": "haiku",
        "agent": "echo",
    }
    with started(config, environment_for(config, probe_folder(tmp_path))) as (url, _):
        with httpx.Client(base_url=url, timeout=30.0, headers={"origin": url}) as api:
            listed = api.get("/api/flows").json()["items"]
            wrong = api.post(
                "/api/flows/intake_probe/runs", json={"inputs": {**inputs, "model": "gpt"}}
            )
            missing = api.post("/api/flows/intake_probe/runs", json={"inputs": {"task": "x"}})
            unknown = api.post("/api/flows/nothing/runs", json={"inputs": {}})
            begun = api.post("/api/flows/intake_probe/runs", json={"inputs": inputs})
            assert begun.status_code == 201, begun.text
            run = api.get(f"/neorc/runs/{begun.json()['id']}").json()
    [probe] = listed
    assert (probe["name"], probe["title"], probe["has_form"]) == ("intake_probe", "Probe", True)
    fields = {field["name"]: field for field in probe["inputs"]}
    assert list(fields) == ["repo_branch", "model", "agent", "task"]
    assert fields["repo_branch"]["options"] == [
        {"value": "app@main", "label": "app · main"},
        {"value": "app@next", "label": "app · next"},
    ]
    assert fields["model"]["options"] == [{"value": "haiku", "label": "Echo Haiku"}]
    assert fields["agent"]["options"] == [{"value": "echo", "label": "Echo · default"}]
    assert fields["task"] == {
        "name": "task",
        "label": "task",
        "type": "string",
        "lov": None,
        "options": None,
    }
    assert wrong.status_code == 422
    assert "'gpt' is not one of its model values" in wrong.json()["detail"]
    assert missing.status_code == 422
    assert unknown.status_code == 404
    assert run["inputs"] == inputs


def test_the_shipped_code_and_review_runs_its_form_s_inputs_to_the_implementing_agent(
    tmp_path: Path,
) -> None:
    """On the echo engine, whose answer is no pull request: the run starts from the form with
    the chosen agent and model, the implementing conversation opens on them, and the run fails
    there, saying why. A real agent's run is for the metered tests."""
    flows = tmp_path / "flows"
    flows.mkdir()
    for name in ("code_and_review.yaml", "code_and_review.form.toml"):
        (flows / name).write_text((SHIPPED / name).read_text())
    (flows / "flows.toml").write_text(PROBE_SETTINGS)
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    inputs = {
        "task": "Fix the bug",
        "repo_branch": "app@main",
        "model": "haiku",
        "agent": "echo",
    }
    with started(config, environment_for(config, flows)) as (url, _):
        with httpx.Client(base_url=url, timeout=30.0, headers={"origin": url}) as api:
            [form] = api.get("/api/flows").json()["items"]
            begun = api.post("/api/flows/code_and_review/runs", json={"inputs": inputs})
            assert begun.status_code == 201, begun.text
            run = finished_run(api, begun.json()["id"])
            tasks = api.get(f"/neorc/runs/{run['id']}/tasks").json()["tasks"]
            conversations = api.get("/api/conversations").json()["items"]
    assert form["title"] == "Code and review"
    assert [field["name"] for field in form["inputs"]] == ["task", "repo_branch", "agent", "model"]
    assert run["status"] == "failed", run
    [implement] = tasks
    assert implement["handler"] == "robinauts_tasks:implement"
    assert "names no pull request" in (implement["error"] or ""), implement
    [implementing] = conversations
    assert implementing["agent"] == "echo"


def test_a_form_that_does_not_fit_its_flow_is_refused_before_it_binds(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    flows = probe_folder(tmp_path, form='[inputs.colour]\nlov = "model"\n')
    start = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(stack.free_port())]
    environment = environment_for(config, flows)
    refused = subprocess.run(start, env=environment, capture_output=True, timeout=30)
    assert refused.returncode == 1
    assert b"intake_probe declares no input 'colour'" in refused.stderr, refused.stderr


def test_the_flows_settings_in_the_agents_file_are_refused_with_where_they_went(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG + "\n[orchestration]\n")
    start = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(stack.free_port())]
    refused = subprocess.run(start, env=environment_for(config), capture_output=True, timeout=30)
    assert refused.returncode == 1
    assert b"[orchestration]: no longer read from this file" in refused.stderr
    assert b"ROBINAUTS_FLOWS_DIR" in refused.stderr


@contextmanager
def a_database() -> Iterator[str]:
    """A new database on the test PostgreSQL, dropped afterwards. neorc's schema has a fixed
    name, so a schema of the test's own, as the other tests use, would not keep two runs
    apart: a database of its own does."""
    given = os.environ["ROBINAUTS_TEST_DATABASE_URL"]
    name = "robinauts_neorc_" + uuid.uuid4().hex
    asyncio.run(stack.run_statement(given, f'CREATE DATABASE "{name}"'))
    try:
        parts = httpx.URL(given)
        yield str(parts.copy_with(path=f"/{name}"))
    finally:
        asyncio.run(stack.run_statement(given, f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.mark.database
@pytest.mark.skipif(os.environ.get("ROBINAUTS_TEST_DATABASE_URL") is None, reason=NO_DATABASE)
def test_on_postgresql_neorc_keeps_its_tables_in_a_schema_of_its_own(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    with a_database() as database_url:
        environment = environment_for(config)
        environment["ROBINAUTS_DATABASE_URL"] = database_url
        subprocess.run([str(stack.ROBINAUTS), "db", "init"], env=environment, check=True)
        with started(config, environment) as (url, said):
            play_a_game(url)
        tables = asyncio.run(_tables(database_url))
    assert b"on PostgreSQL" in said[0]
    neorc = {name for schema, name in tables if schema == "neorc"}
    assert "neorc_flow_tasks" in neorc, tables
    # None of neorc's tables is in the server's schema, and none of the server's in neorc's.
    assert not [name for schema, name in tables if schema == "public" and "neorc" in name]
    assert "tasks" not in neorc


async def _tables(url: str) -> list[tuple[str, str]]:
    connection = await stack.asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT table_schema, table_name FROM information_schema.tables"
            " WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )
    finally:
        await connection.close()
    return [(row["table_schema"], row["table_name"]) for row in rows]
