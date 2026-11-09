# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stop asked by a signal, as a service manager asks it, stops the worker and closes the
controller before the process ends. uvicorn raises the signal again once it has shut down,
so whatever runs after its ``serve`` returns never runs on SIGTERM, and is cut short on SIGINT.
A signal while the controller and the worker start waits for the start, then stops both.
A poll of the change feed left open does not hold the stop back.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from util import stack

pytestmark = pytest.mark.io

CLOSED = b"the controller is closed"


@pytest.mark.parametrize(
    ("signum", "status"),
    [(signal.SIGTERM, -signal.SIGTERM), (signal.SIGINT, 130)],
    ids=["SIGTERM", "SIGINT"],
)
def test_a_signal_closes_the_controller_before_the_process_ends(
    signum: signal.Signals, status: int, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text("")
    environment = {**os.environ, "ROBINAUTS_CONFIG": str(config)}
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    port = stack.free_port()
    command = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    with subprocess.Popen(command, env=environment, stderr=subprocess.PIPE) as running:
        stack.wait_until_up(f"http://127.0.0.1:{port}", running)
        activity = Path(tempfile.gettempdir()) / f"robinauts-worker-{running.pid}.alive"
        assert activity.exists()
        feed = f"http://127.0.0.1:{port}/api/changes"
        version = httpx.get(feed).json()["version"]
        polled = {"after": version, "timeout": 30}
        poll = threading.Thread(target=lambda: httpx.get(feed, params=polled, timeout=60))
        poll.start()
        time.sleep(0.5)
        began = time.monotonic()
        running.send_signal(signum)
        _, said = running.communicate(timeout=30)
        assert time.monotonic() - began < 10
        poll.join(10)
    assert CLOSED in said, said.decode(errors="replace")
    assert b"Traceback" not in said, said.decode(errors="replace")
    assert running.returncode == status
    assert not activity.exists()


STARTING = b"the lifecycle is starting"

SLOW_START = f"""
import asyncio, sys
from robinauts.cli import lifecycle
from robinauts.cli.main import run

started = lifecycle.Lifecycle.start

async def slow(self):
    await started(self)
    print({STARTING.decode()!r}, flush=True)
    await asyncio.sleep(3)

lifecycle.Lifecycle.start = slow
run(sys.argv[1:])
"""
"""``robinauts`` with its lifecycle's start three seconds longer, so a signal can come in it."""


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_signal_during_the_start_waits_for_it_then_stops_cleanly(
    signum: signal.Signals, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text("")
    script = tmp_path / "slow_start.py"
    script.write_text(SLOW_START)
    environment = {**os.environ, "ROBINAUTS_CONFIG": str(config)}
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    port = stack.free_port()
    command = [sys.executable, str(script), "start", "--dev-no-sign-in", "--port", str(port)]
    with subprocess.Popen(
        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    ) as running:
        assert running.stdout is not None
        for line in running.stdout:
            if STARTING in line:
                break
        activity = Path(tempfile.gettempdir()) / f"robinauts-worker-{running.pid}.alive"
        assert activity.exists()
        running.send_signal(signum)
        said, _ = running.communicate(timeout=30)
    assert b"Application shutdown complete" in said, said.decode(errors="replace")
    assert CLOSED in said, said.decode(errors="replace")
    assert b"Traceback" not in said, said.decode(errors="replace")
    assert not activity.exists()
