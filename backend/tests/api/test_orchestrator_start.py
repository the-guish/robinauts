# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestrator ``robinauts start`` runs beside itself in the local development mode
(``docs/architecture/orchestration.md``): with an ``[orchestration]`` table, neorc's manager,
the upload of the flows, its scheduler and its worker are started in that order before the
server listens, and stopped the other way round with it. The worker's task reaches the API with
no credential, as everything does in that mode, and starts a conversation with the guessing
prompt, which the browser then sees as its own. neorc is a stand-in here
(``util/fake_neorc.py``) that runs that task as neorc's worker would: the real one needs its own
PostgreSQL."""

from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from util import stack

pytestmark = pytest.mark.io

REPO = Path(__file__).resolve().parents[3]
FAKE_NEORC = Path(__file__).resolve().parents[1] / "util" / "fake_neorc.py"


def guessing_prompt() -> str:
    """The handlers' prompt, read rather than imported: an import would leave a
    ``__pycache__`` in the checkout's ``orchestration/``."""
    tree = ast.parse((REPO / "orchestration" / "robinauts_tasks.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "GUESSING_PROMPT":
            return str(ast.literal_eval(node.value))
    raise AssertionError("robinauts_tasks.py has no GUESSING_PROMPT")


CONFIG = """
[model_providers.echo]
kind = "anthropic"
api_key_env = "ECHO_API_KEY"

[models.echo]
provider = "echo"
name = "echo"
title = "Echo"

[agents.echo]
title = "Echo"
system_prompt = ""
model = "echo"
engine = "echo"

[orchestration]
command = {command}
code_location = "{code}"
port = {port}
"""


def entries(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def environment_for(config: Path, log: Path) -> dict[str, str]:
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "NEORC_DATABASE_URL": "postgresql://127.0.0.1/neorc-unused",
        "ECHO_API_KEY": "not-a-real-key",
        "FAKE_NEORC_LOG": str(log),
        # The stand-in imports the handlers from the checkout: it must leave nothing there.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    return environment


def test_neorc_starts_with_the_server_and_its_task_opens_a_conversation(tmp_path: Path) -> None:
    config, log = tmp_path / "robinauts.toml", tmp_path / "neorc.jsonl"
    command = json.dumps([sys.executable, str(FAKE_NEORC)])
    code = REPO / "orchestration"
    config.write_text(CONFIG.format(command=command, code=code, port=stack.free_port()))
    port = stack.free_port()
    start = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    environment = environment_for(config, log)
    with subprocess.Popen(start, env=environment, stderr=subprocess.PIPE) as running:
        try:
            url = f"http://127.0.0.1:{port}"
            stack.wait_until_up(url, running)
            deadline = time.monotonic() + 30
            while not (done := [e for e in entries(log) if "result" in e]):
                assert time.monotonic() < deadline, entries(log)
                time.sleep(0.1)
            [task] = done
            with httpx.Client(base_url=url, timeout=30.0) as api:
                opened = api.get(f"/api/conversations/{task['result']['conversation_id']}")
                assert opened.status_code == 200, opened.text
                first = opened.json()["messages"][0]
                assert first["role"] == "user"
                assert first["parts"][0]["text"] == guessing_prompt()
            running.send_signal(signal.SIGTERM)
            _, said = running.communicate(timeout=60)
        finally:
            if running.poll() is None:
                running.kill()
    assert task["api_url"] == url
    assert task["visible"] == []
    log_entries = entries(log)
    started = [e["step"] for e in log_entries if "arguments" in e]
    # The scheduler and the worker are started one after the other without waiting for either,
    # so which one writes first is theirs to race.
    assert started[:2] == ["manager", "flows"], log_entries
    assert sorted(started[2:]) == ["scheduler", "worker"], log_entries
    stopped = [e["step"] for e in log_entries if e.get("stopped")]
    assert stopped == ["worker", "scheduler", "manager"], log_entries
    assert b"neorc is up" in said, said.decode(errors="replace")


def test_a_start_whose_neorc_does_not_start_is_refused_before_it_binds(tmp_path: Path) -> None:
    config, log = tmp_path / "robinauts.toml", tmp_path / "neorc.jsonl"
    code = REPO / "orchestration"
    command = json.dumps([sys.executable, "-c", "raise SystemExit(3)"])
    config.write_text(CONFIG.format(command=command, code=code, port=stack.free_port()))
    port = stack.free_port()
    start = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    started = subprocess.run(
        start, env=environment_for(config, log), capture_output=True, timeout=60
    )
    assert started.returncode == 1
    assert b"neorc's manager ended with code 3 before it answered" in started.stderr


def test_a_start_with_orchestration_and_no_neorc_database_is_refused(tmp_path: Path) -> None:
    config, log = tmp_path / "robinauts.toml", tmp_path / "neorc.jsonl"
    command = json.dumps([sys.executable, str(FAKE_NEORC)])
    code = REPO / "orchestration"
    config.write_text(CONFIG.format(command=command, code=code, port=stack.free_port()))
    environment = environment_for(config, log)
    environment.pop("NEORC_DATABASE_URL")
    start = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(stack.free_port())]
    started = subprocess.run(start, env=environment, capture_output=True, timeout=30)
    assert started.returncode == 1
    assert b"no database for neorc: set NEORC_DATABASE_URL" in started.stderr
    assert not log.exists()
