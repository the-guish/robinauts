# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stop asked by a signal, as a service manager asks it, stops the worker and closes the
controller before the process ends. uvicorn raises the signal again once it has shut down,
so whatever runs after its ``serve`` returns never runs on SIGTERM, and is cut short on SIGINT.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from pathlib import Path

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
    port = stack.free_port()
    command = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    with subprocess.Popen(command, env=environment, stderr=subprocess.PIPE) as running:
        stack.wait_until_up(f"http://127.0.0.1:{port}", running)
        activity = Path(tempfile.gettempdir()) / f"robinauts-worker-{running.pid}.alive"
        assert activity.exists()
        running.send_signal(signum)
        _, said = running.communicate(timeout=30)
    assert CLOSED in said, said.decode(errors="replace")
    assert not activity.exists()
