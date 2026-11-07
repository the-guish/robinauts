# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A worker whose table names a command is started with that command in place of this build's
worker module, and differs in nothing else: the server writes the secret on its standard
input, waits for ``ready`` on its standard output and logs what else it says, counts its
requests as signs of life for the health check, and sends it SIGTERM at the stop. A command
that cannot be started, or ends without saying ready, fails the start. A lifecycle with such a
worker and no URL for it to reach the core at is refused.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import pytest
from echo_controller import CONFIG

from robinauts.cli.composition import Composed, compose
from robinauts.cli.lifecycle import Lifecycle, WorkerStartError
from robinauts.shared_domain.domain import AgentConfig, Config, StorageConfig, StorageKind
from util.aio import asyncio_test

FAKE_WORKER = """
import os, signal, sys
secret = sys.stdin.buffer.read().decode()
with open(os.environ["FAKE_SECRET_FILE"], "w") as out:
    out.write(secret)
print("hello from the fake worker", flush=True)
print("ready", flush=True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
signal.pause()
"""
"""A worker in nothing but the hand-off: it keeps the secret where the test reads it, says
one thing before it is ready, and ends at SIGTERM."""

NOWHERE = "http://127.0.0.1:9"
"""Where the fake worker would reach the core; it never does."""

SIX_MINUTES = 360


def composed_with(command: tuple[str, ...]) -> Composed:
    """The echo configuration with one more agent, on the worker ``special``, started with
    ``command``."""
    special = AgentConfig(
        "special", title="Special", system_prompt="", model="echo", engine="echo", worker="special"
    )
    config = Config(
        CONFIG.providers,
        CONFIG.models,
        {},
        {**CONFIG.agents, "special": special},
        CONFIG.work,
        {"special": command},
    )
    return compose(config, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={}.get)


@asyncio_test
async def test_the_command_is_handed_the_secret_and_heard_through_the_api(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    kept = tmp_path / "secret"
    monkeypatch.setenv("FAKE_SECRET_FILE", str(kept))
    composed = composed_with((sys.executable, str(script)))
    lifecycle = Lifecycle(composed, broker_url=NOWHERE, in_process=True)
    with caplog.at_level(logging.INFO, logger="robinauts.cli.lifecycle"):
        await lifecycle.start()
    try:
        secret = kept.read_text()
        assert secret
        assert lifecycle.access.admitted(f"Bearer {secret}") == "special"
        assert lifecycle.access.admitted("Bearer not-it") is None
        assert "worker 'special' says: hello from the fake worker" in caplog.text
        assert lifecycle.healthy()
        # Its requests are its signs of life: six minutes without one and it is gone, one
        # more and it is back.
        activity = lifecycle.activity_files["special"]
        six_minutes_ago = time.time() - SIX_MINUTES
        os.utime(activity, (six_minutes_ago, six_minutes_ago))
        assert not lifecycle.healthy()
        lifecycle.access.admitted(f"Bearer {secret}")
        assert lifecycle.healthy()
    finally:
        await lifecycle.stop()
    assert not activity.exists()
    assert lifecycle.access.admitted(f"Bearer {secret}") is None


@pytest.mark.parametrize(
    ("command", "problem"),
    [
        ((sys.executable, "-c", "print('oops'); import sys; sys.exit(3)"), "ended before"),
        (("/nowhere/robinauts-worker",), "could not be started"),
    ],
    ids=["ends", "missing"],
)
@asyncio_test
async def test_a_command_that_does_not_say_ready_fails_the_start(
    command: tuple[str, ...], problem: str
) -> None:
    lifecycle = Lifecycle(composed_with(command), broker_url=NOWHERE, in_process=True)
    with pytest.raises(WorkerStartError, match=f"worker 'special' {problem}"):
        await lifecycle.start()
    assert not lifecycle.access.is_open


def test_a_worker_with_a_command_needs_a_url_to_reach_the_core_at() -> None:
    with pytest.raises(ValueError, match="special run in processes of their own"):
        Lifecycle(composed_with(("worker",)))
