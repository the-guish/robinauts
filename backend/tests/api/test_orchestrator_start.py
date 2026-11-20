# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc composed into the backend ``robinauts start`` serves
(``docs/architecture/orchestration.md``): neorc's manager is a component, served under
``/neorc/`` behind oneberth's checks, beside Robinauts' own. With a flows folder, the flows are
uploaded once the backend serves, with a token of ``neorc/ci``; neorc's scheduler runs in the
server and its worker in a process of its own, against the worker listener. A run of
``guess_number`` started on neorc's API plays the game between two conversations of the local
user, on the echo engine: its handlers act for the person who started the run.

neorc is the real one. Its storage follows the server's: in the process without
``ROBINAUTS_DATABASE_URL``, and on that PostgreSQL in the schema ``neorc`` with it."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
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
from util.desktop import appeared, gone

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
models = ["haiku"]
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
def started(
    config: Path, environment: dict[str, str]
) -> Iterator[tuple[stack.Server, list[bytes]]]:
    """A server started on ``config`` in the local mode, once it says it is ready, and once it
    has stopped, what it said on its standard error, read as it comes."""
    said: list[bytes] = []
    running, url = stack.start_local(environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with running:
        assert running.stderr is not None
        errors = running.stderr
        reading = threading.Thread(target=lambda: said.append(errors.read()), daemon=True)
        reading.start()
        try:
            stack.wait_until_ready(running)
            yield url, said
            running.terminate()
            running.wait(timeout=60)
            reading.join(10)
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


def play_a_game(url: stack.Server) -> None:
    """Start ``guess_number`` on neorc's API, as neorc's own form does, and check what it did:
    the run ends, and its two conversations are the local user's."""
    with stack.client(url) as api:
        flows = api.get("/neorc/flows")
        assert flows.status_code == 200, flows.text
        assert "guess_number" in [f["name"] for f in flows.json()["flows"]]
        before = {c["id"] for c in api.get("/robinauts/api/conversations").json()["items"]}
        begun = api.post("/neorc/flows/guess_number/runs", json={"inputs": {}})
        assert begun.status_code == 201, begun.text
        run = finished_run(api, begun.json()["id"])
        assert run["status"] == "succeeded", run
        after = api.get("/robinauts/api/conversations").json()["items"]
        opened = [c for c in after if c["id"] not in before]
        # The game, on the Haiku model, and the guesser.
        assert len(opened) == 2, opened


def test_neorc_is_composed_in_the_server_and_a_run_of_its_flow_plays_the_game(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    with started(config, environment_for(config)) as (url, said):
        with stack.client(url) as api:
            session = api.get("/auth/session").json()
            assert {"neorc/user", "robinauts/user"} <= set(session["roles"])
            assert api.get("/health").json() == stack.HEALTHY
        play_a_game(url)
    assert b"neorc is up" in said[0], said[0].decode(errors="replace")
    assert b"in this process" in said[0]


def test_neorc_s_worker_ends_with_a_server_that_is_killed(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    mark = uuid.uuid4().hex
    environment = {**environment_for(config), "ROBINAUTS_TEST_MARK": mark}
    running, _ = stack.start_local(environment, stdout=subprocess.PIPE)
    with running:
        try:
            stack.wait_until_ready(running)
            workers = appeared("ROBINAUTS_TEST_MARK", mark, 30, beside=[running.pid])
            assert len(workers) == 1, workers
            ((worker, shown),) = workers.items()
            assert shown["ONEBERTH_WORKER_NAME"] == "neorc"
            running.kill()
            assert gone([worker], 30)
        finally:
            if running.poll() is None:
                running.kill()


def test_neorc_s_api_is_checked_as_every_route_of_the_backend(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    with started(config, environment_for(config)) as (url, _):
        with httpx.Client(base_url=url, timeout=30.0) as anyone:
            nobody = anyone.get("/neorc/flows")
        with stack.client(url) as api:
            listed = api.get("/neorc/flows")
            elsewhere = api.get("/neorc/flows", headers={"host": "evil.example"})
            cross_origin = api.post(
                "/neorc/flows/guess_number/runs",
                json={"inputs": {}},
                headers={"origin": "http://evil.example"},
            )
            page = api.get("/neorc/ui/")
    assert nobody.status_code == 401
    assert listed.status_code == 200
    assert listed.headers.get_list("x-frame-options") == ["DENY"]
    # The local mode's checks reach neorc's routes as they reach every path.
    assert (elsewhere.status_code, cross_origin.status_code) == (403, 403)
    # neorc's interface is in the backend's own bundle, under /ui/: neorc serves none here.
    assert page.status_code == 404


def test_a_start_whose_flows_are_refused_is_refused_before_it_says_ready(
    tmp_path: Path,
) -> None:
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "broken.yaml").write_text("name: broken\nversion: 1.0.0\nsteps: 3\n")
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    port = stack.free_port()
    start = [str(stack.ROBINAUTS), "start", "--local", "--port", str(port)]
    environment = environment_for(config, flows)
    refused = subprocess.run(
        start,
        env=environment,
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert refused.returncode == 1
    assert b"neorc refused the flows in" in refused.stderr, refused.stderr


ASK = """
name: ask
version: 1.0.0
inputs:
  question: string
output: tasks.answer
steps:
  answer:
    queue: editors
    handler: answer-the-question
    params:
      question: inputs.question
"""
"""A flow whose one task waits for a person, on an external queue."""


def test_a_task_of_a_queue_of_queues_toml_waits_for_a_person_s_answer(tmp_path: Path) -> None:
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "ask.yaml").write_text(ASK)
    (flows / "queues.toml").write_text('[queues.editors]\nkind = "external"\n')
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    with started(config, environment_for(config, flows)) as (url, _):
        with stack.client(url) as api:
            queues = {q["name"]: q["kind"] for q in api.get("/neorc/queues").json()["queues"]}
            assert queues == {"default": "worker", "editors": "external"}
            begun = api.post("/neorc/flows/ask/runs", json={"inputs": {"question": "Ready?"}})
            assert begun.status_code == 201, begun.text
            deadline = time.monotonic() + RUN_SECONDS
            waiting: list[dict[str, Any]] = []
            # As a person reads the queue's page: its tasks, then one with its inputs.
            while not waiting and time.monotonic() < deadline:
                listed = api.get("/neorc/queues/editors/tasks/recent", params={"status": "pending"})
                assert listed.status_code == 200, listed.text
                waiting = listed.json()["tasks"]
                time.sleep(0.2)
            assert len(waiting) == 1, waiting
            shown = api.get(f"/neorc/tasks/{waiting[0]['task']['id']}/inputs")
            assert shown.status_code == 200, shown.text
            assert shown.json()["inputs"] == {"question": "Ready?"}, shown.text
            # The run waits for the person, and ends with what they answer.
            assert api.get(f"/neorc/runs/{begun.json()['id']}").json()["status"] == "active"
            answered = api.post(
                f"/neorc/tasks/{waiting[0]['task']['id']}/complete", json={"result": "Ready."}
            )
            assert answered.status_code == 204, answered.text
            run = finished_run(api, begun.json()["id"])
            assert (run["status"], run["output"]) == ("succeeded", "Ready."), run


@pytest.mark.parametrize(
    ("queues", "said"),
    [
        # The worker's queue stays a worker queue.
        ('[queues.default]\nkind = "external"\n', b"queue 'default' is a worker queue"),
        # A step on a queue nobody declared.
        ("", b"editors"),
        ('[queues.editors]\nkind = "sometimes"\n', b"queues.toml [queues.editors]"),
    ],
)
def test_a_start_whose_queues_do_not_fit_is_refused_before_it_says_ready(
    tmp_path: Path, queues: str, said: bytes
) -> None:
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "ask.yaml").write_text(ASK)
    (flows / "queues.toml").write_text(queues)
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    start = [str(stack.ROBINAUTS), "start", "--local", "--port", str(stack.free_port())]
    refused = subprocess.run(
        start,
        env=environment_for(config, flows),
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert refused.returncode == 1
    assert b"neorc refused the flows in" in refused.stderr, refused.stderr
    assert said in refused.stderr, refused.stderr


def test_a_folder_of_one_s_own_flows_is_served_as_its_flows_toml_says(
    tmp_path: Path,
) -> None:
    """A folder anywhere, with the shipped handlers as its default."""
    flows = tmp_path / "my-flows"
    flows.mkdir()
    (flows / "guess_number.yaml").write_text((SHIPPED / "guess_number.yaml").read_text())
    (flows / "flows.toml").write_text(f'code_location = "{SHIPPED.parent}"\n')
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    with started(config, environment_for(config, flows)) as (url, _):
        with stack.client(url) as api:
            listed = api.get("/neorc/flows").json()["flows"]
    assert [f["name"] for f in listed] == ["guess_number"]


def test_a_flows_toml_that_is_not_toml_is_refused_before_it_binds(
    tmp_path: Path,
) -> None:
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "flows.toml").write_text("ui = \n")
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    start = [str(stack.ROBINAUTS), "start", "--local", "--port", str(stack.free_port())]
    environment = environment_for(config, flows)
    refused = subprocess.run(
        start,
        env=environment,
        capture_output=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
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
        with stack.client(url) as api:
            listed = api.get("/robinauts/api/flows").json()["items"]
            wrong = api.post(
                "/robinauts/api/flows/intake_probe/runs",
                json={"inputs": {**inputs, "model": "gpt"}},
            )
            missing = api.post(
                "/robinauts/api/flows/intake_probe/runs", json={"inputs": {"task": "x"}}
            )
            unknown = api.post("/robinauts/api/flows/nothing/runs", json={"inputs": {}})
            begun = api.post("/robinauts/api/flows/intake_probe/runs", json={"inputs": inputs})
            assert begun.status_code == 201, begun.text
            run = api.get(f"/neorc/runs/{begun.json()['id']}").json()
    [probe] = listed
    assert (probe["name"], probe["title"], probe["has_form"]) == (
        "intake_probe",
        "Probe",
        True,
    )
    fields = {field["name"]: field for field in probe["inputs"]}
    assert list(fields) == ["repo_branch", "model", "agent", "task"]
    assert fields["repo_branch"]["options"] == [
        {"value": "app@main", "label": "app · main", "models": None},
        {"value": "app@next", "label": "app · next", "models": None},
    ]
    assert fields["model"]["options"] == [{"value": "haiku", "label": "Echo Haiku", "models": None}]
    assert fields["agent"]["options"] == [
        {"value": "echo", "label": "Echo · default", "models": ["haiku"]}
    ]
    assert fields["task"] == {
        "name": "task",
        "label": "task",
        "type": "string",
        "lov": None,
        "options": None,
        "live": False,
        "of": None,
    }
    assert wrong.status_code == 422
    assert "'gpt' is not one of its model values" in wrong.json()["detail"]
    assert missing.status_code == 422
    assert unknown.status_code == 404
    assert run["inputs"] == inputs


LIVE_FLOW = """
name: ship_probe
version: 0.1.0
inputs:
  repo: string
  branch: string
  project: string
output: tasks.pick
steps:
  pick:
    queue: default
    handler: robinauts_tasks:pick_number
"""
LIVE_FORM = """
title = "Ship"
[inputs.repo]
lov = "repository"
[inputs.branch]
lov = "branch"
of = "repo"
[inputs.project]
lov = "project"
"""


def bare_repository(path: Path, *branches: str) -> Path:
    """A repository on disk with one commit, and ``branches`` at it."""
    identity = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com"}
    environment = {
        **os.environ,
        **identity,
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def git(*args: str, given: str = "") -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=path,
            input=given,
            capture_output=True,
            text=True,
            check=True,
            env=environment,
        )
        return done.stdout.strip()

    path.mkdir()
    git("init", "--bare", "--quiet")
    commit = git("commit-tree", git("hash-object", "-t", "tree", "-w", "--stdin"), "-m", "first")
    add_branches(path, commit, *branches)
    return path


def add_branches(path: Path, commit: str, *branches: str) -> None:
    lines = "".join(f"create refs/heads/{name} {commit}\n" for name in branches)
    subprocess.run(["git", "update-ref", "--stdin"], cwd=path, input=lines, text=True, check=True)


def test_a_branch_and_a_project_are_searched_as_they_are_now_and_checked_at_the_start(
    tmp_path: Path,
) -> None:
    """A branch is one of a repository's own, among a thousand, that its settings offer; a
    project is one of the person's. Both lists change while the server runs, and are searched
    and checked as they are, with no restart."""
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    app = bare_repository(
        tmp_path / "app.git", "main", "other", *(f"feature/{n:04}" for n in range(1000))
    )
    docs = bare_repository(tmp_path / "docs.git", "main", "draft")
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "ship_probe.yaml").write_text(LIVE_FLOW)
    (flows / "ship_probe.form.toml").write_text(LIVE_FORM)
    (flows / "flows.toml").write_text(
        f'[repositories.app]\nurl = "file://{app}"\nbranches = ["main", "feature/*"]\n'
        f'[repositories.docs]\nurl = "file://{docs}"\nbranches = ["main"]\n'
    )
    with started(config, environment_for(config, flows)) as (url, _):
        with stack.client(url) as api:

            def options(kind: str, **query: Any) -> httpx.Response:
                return api.get(f"/robinauts/api/lovs/{kind}/options", params=query)

            def start(**inputs: str) -> httpx.Response:
                return api.post("/robinauts/api/flows/ship_probe/runs", json={"inputs": inputs})

            [form] = api.get("/robinauts/api/flows").json()["items"]
            narrowed = options("branch", after="app", q="feature/09", limit=5).json()
            first = options("branch", after="app").json()["items"][0]
            unoffered = options("branch", after="app", q="other").json()
            no_repository = options("branch", after="nowhere")
            no_list = options("model")
            project = api.post("/robinauts/api/projects", json={"name": "Ship it"}).json()
            projects = options("project", q="ship").json()
            commit = subprocess.run(
                ["git", "rev-parse", "main"], cwd=app, capture_output=True, text=True, check=True
            ).stdout.strip()
            add_branches(app, commit, "feature/pushed-now")
            refused = {
                "a branch its settings do not offer": start(
                    repo="app", branch="other", project=project["id"]
                ),
                "a branch the repository does not have": start(
                    repo="app", branch="feature/never", project=project["id"]
                ),
                "a branch of the other repository": start(
                    repo="docs", branch="feature/0001", project=project["id"]
                ),
                "a name that is no branch's": start(
                    repo="app", branch="feature/../main", project=project["id"]
                ),
                "a project that is not the person's": start(
                    repo="app", branch="main", project=str(uuid.uuid4())
                ),
            }
            inputs = {"repo": "app", "branch": "feature/pushed-now", "project": project["id"]}
            begun = start(**inputs)
            assert begun.status_code == 201, begun.text
            run = api.get(f"/neorc/runs/{begun.json()['id']}").json()
    fields = {field["name"]: field for field in form["inputs"]}
    assert fields["repo"]["options"] == [
        {"value": "app", "label": "app", "models": None},
        {"value": "docs", "label": "docs", "models": None},
    ]
    assert (fields["branch"]["live"], fields["branch"]["of"], fields["branch"]["options"]) == (
        True,
        "repo",
        None,
    )
    assert (fields["project"]["live"], fields["project"]["options"]) == (True, None)
    assert [o["value"] for o in narrowed["items"]] == [f"feature/09{n:02}" for n in range(5)]
    assert narrowed["more"] is True
    assert first["value"] == "main"
    assert unoffered == {"items": [], "more": False}
    assert no_repository.status_code == 404
    assert no_list.status_code == 404
    assert projects["items"] == [{"value": project["id"], "label": "Ship it", "models": None}]
    for case, answer in refused.items():
        assert answer.status_code == 422, (case, answer.text)
    assert "not one of your projects" in refused["a project that is not the person's"].text
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
        with stack.client(url) as api:
            [form] = api.get("/robinauts/api/flows").json()["items"]
            begun = api.post("/robinauts/api/flows/code_and_review/runs", json={"inputs": inputs})
            assert begun.status_code == 201, begun.text
            run = finished_run(api, begun.json()["id"])
            tasks = api.get(f"/neorc/runs/{run['id']}/tasks").json()["tasks"]
            conversations = api.get("/robinauts/api/conversations").json()["items"]
            opened = api.get(f"/robinauts/api/conversations/{conversations[0]['id']}").json()
            api_messages = opened["messages"]
    assert form["title"] == "Code and review"
    assert [field["name"] for field in form["inputs"]] == [
        "task",
        "repo_branch",
        "agent",
        "model",
    ]
    assert run["status"] == "failed", run
    [implement] = tasks
    assert implement["handler"] == "robinauts_tasks:implement"
    assert "names no pull request" in (implement["error"] or ""), implement
    [implementing] = conversations
    assert implementing["agent"] == "echo"
    # The code task went beside the prompt, for the engine: the echo engine says it back.
    [question, _] = api_messages
    assert question["task_payload"] == {
        "task_key": "code_task/1",
        "repositories": {"app": {"branch": "main"}},
    }


def test_a_form_that_does_not_fit_its_flow_is_refused_before_it_binds(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    flows = probe_folder(tmp_path, form='[inputs.colour]\nlov = "model"\n')
    start = [str(stack.ROBINAUTS), "start", "--local", "--port", str(stack.free_port())]
    environment = environment_for(config, flows)
    refused = subprocess.run(
        start,
        env=environment,
        capture_output=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert refused.returncode == 1
    assert b"intake_probe declares no input 'colour'" in refused.stderr, refused.stderr


def test_the_flows_settings_in_the_agents_file_are_refused_with_where_they_went(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG + "\n[orchestration]\n")
    start = [str(stack.ROBINAUTS), "start", "--local", "--port", str(stack.free_port())]
    refused = subprocess.run(
        start,
        env=environment_for(config),
        capture_output=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert refused.returncode == 1
    assert b"[orchestration]: no longer read from this file" in refused.stderr
    assert b"ROBINAUTS_FLOWS_DIR" in refused.stderr


@pytest.mark.database
@pytest.mark.skipif(os.environ.get("ROBINAUTS_TEST_DATABASE_URL") is None, reason=NO_DATABASE)
def test_on_postgresql_neorc_keeps_its_tables_in_a_schema_of_its_own(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    with stack.new_database("robinauts_neorc_") as database_url:
        environment = environment_for(config)
        environment["ROBINAUTS_DATABASE_URL"] = database_url
        subprocess.run([str(stack.ROBINAUTS), "db", "init"], env=environment, check=True)
        subprocess.run([str(stack.ROBINAUTS), "store", "init"], env=environment, check=True)
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
