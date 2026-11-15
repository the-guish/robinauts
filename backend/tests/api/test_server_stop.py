# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stop asked by a signal, as a service manager asks it, stops the worker and closes the
controller before the process ends, which then exits 0: oneberth's server handles the signal.
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


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_signal_closes_the_controller_before_the_process_ends(
    signum: signal.Signals, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text("")
    environment = {**os.environ, "ROBINAUTS_CONFIG": str(config)}
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    running, url = stack.start_local(environment, stderr=subprocess.PIPE)
    with running, httpx.Client(base_url=url, timeout=60) as client:
        stack.wait_until_up(url, running)
        stack.signed_in(client, url)
        activity = Path(tempfile.gettempdir()) / f"robinauts-worker-{running.pid}.alive"
        assert activity.exists()
        feed = "/robinauts/api/changes"
        version = client.get(feed).json()["version"]
        polled = {"after": version, "timeout": 30}
        poll = threading.Thread(target=lambda: client.get(feed, params=polled))
        poll.start()
        time.sleep(0.5)
        began = time.monotonic()
        running.send_signal(signum)
        _, said = running.communicate(timeout=30)
        assert time.monotonic() - began < 10
        poll.join(10)
    assert CLOSED in said, said.decode(errors="replace")
    assert b"Traceback" not in said, said.decode(errors="replace")
    assert running.returncode == 0
    assert not activity.exists()


STARTING = b"the lifecycle is starting"

SLOW_START = f"""
import asyncio, sys
from robinauts.cli import lifecycle
from robinauts.cli.main import run

started = lifecycle.Lifecycle.start

async def slow(self, *args, **kwargs):
    await started(self, *args, **kwargs)
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
    running, _ = stack.start_local(
        environment,
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    with running:
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
