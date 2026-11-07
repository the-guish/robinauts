# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A lifecycle whose start fails, or is cancelled, part way stops what it had started before
it raises, and its worker's process before it shuts that process out of the API; and a stop
that fails part way is finished by the next one."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest
from echo_controller import CONFIG

from robinauts.cli import lifecycle as lifecycle_module
from robinauts.cli.composition import Composed, compose
from robinauts.cli.lifecycle import Lifecycle
from robinauts.shared_domain.domain import StorageConfig, StorageKind, UnknownEngineError
from robinauts.worker.agent_engine_store import AgentEngineStore
from util.aio import asyncio_test


class CountedStore(AgentEngineStore):
    """An engines' store that holds nothing, and counts its opens and closes."""

    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0

    async def open(self) -> object:
        self.opened += 1
        return object()

    async def close(self) -> None:
        self.closed += 1


def counted_closes(composed: Composed, fail_first: bool = False) -> list[int]:
    """Every call of the controller's ``close``, the first one failing when asked."""
    calls: list[int] = []
    close = composed.controller.close

    async def counted() -> None:
        calls.append(1)
        if fail_first and len(calls) == 1:
            raise OSError("the store would not close")
        await close()

    composed.controller.close = counted  # type: ignore[method-assign]
    return calls


def an_engine_this_build_lacks(*_: Any) -> Any:
    raise UnknownEngineError("echo, which this build does not have")


@asyncio_test
async def test_a_worker_that_fails_to_start_is_stopped_with_the_controller() -> None:
    composed = compose(
        CONFIG,
        storage=StorageConfig(StorageKind.IN_PROCESS),
        secret_for={}.get,
        engines={"echo": an_engine_this_build_lacks},
    )
    store = CountedStore()
    composed.worker._agent_engine_store = store
    closes = counted_closes(composed)
    lifecycle = Lifecycle(composed)
    with pytest.raises(UnknownEngineError):
        await lifecycle.start()
    # The worker opened its engines' store before the engine failed, and closed it again.
    assert (store.opened, store.closed) == (1, 1)
    assert len(closes) == 1
    await lifecycle.stop()
    assert len(closes) == 1


@asyncio_test
async def test_a_close_that_fails_is_tried_again_by_the_next_stop() -> None:
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={}.get)
    lifecycle = Lifecycle(composed)
    await lifecycle.start()
    closes = counted_closes(composed, fail_first=True)
    with pytest.raises(OSError):
        await lifecycle.stop()
    await lifecycle.stop()
    assert len(closes) == 2
    await lifecycle.stop()
    assert len(closes) == 2


SILENT_WORKER = """
import os, sys, time
with open(sys.argv[0] + ".pid", "w") as written:
    written.write(str(os.getpid()))
time.sleep(60)
"""
"""A worker process that never says it has started."""


@pytest.mark.io
@asyncio_test
async def test_a_cancelled_start_leaves_no_worker_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = tmp_path / "silent_worker.py"
    worker.write_text(SILENT_WORKER)
    monkeypatch.setattr(lifecycle_module, "WORKER_MODULE", "silent_worker")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={}.get)
    lifecycle = Lifecycle(composed, broker_url="http://127.0.0.1:9")
    pid_file = Path(f"{worker}.pid")
    # Whether the worker's process still ran at each close of its access: one that does may
    # still be ending its turns through the API.
    still_running: list[bool] = []
    close = lifecycle.access.close

    def recorded_close() -> None:
        try:
            os.kill(int(pid_file.read_text()), 0)
            still_running.append(True)
        except (FileNotFoundError, ProcessLookupError):
            still_running.append(False)
        close()

    lifecycle.access.close = recorded_close  # type: ignore[method-assign]
    starting = asyncio.create_task(lifecycle.start())
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    try:
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert still_running == [False]
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
