# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An external worker: named by a ``[workers.<name>]`` table of kind ``external``, let in through
the API with the secret the variable its table names holds, and started by nobody here. The
turns of its agents wait for it, the default worker never claims them, and the health check
counts it out."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from robinauts.cli.composition import compose, external_secrets
from robinauts.cli.lifecycle import Lifecycle
from robinauts.cli.main import build_app, serving
from robinauts.controller.contract.domain import ConfigError, Identity
from robinauts.shared_domain.domain import (
    DEFAULT_WORKER,
    AgentConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    StorageConfig,
    StorageKind,
    TurnState,
    WorkerConfig,
    WorkerKind,
)
from robinauts.worker.http_turn_broker import BrokerRefusedError, HttpTurnBroker
from util.aio import asyncio_test

WORKER = "claude-container"
VARIABLE = "ROBINAUTS_CLAUDE_CONTAINER_SECRET"
SECRET = "the-secret-the-container-presents"
ENVIRON = {VARIABLE: SECRET}

CONFIG = Config(
    providers={"p": ProviderConfig("p", ProviderKind.ANTHROPIC, "P_KEY")},
    models={"m": ModelConfig("m", provider="p", name="echo", title="M")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", model="m", engine="echo"),
        WORKER: AgentConfig(
            WORKER, title="Claude", system_prompt="", model="m", engine="claude-code", worker=WORKER
        ),
    },
    worker_settings={WORKER: WorkerConfig(WORKER, WorkerKind.EXTERNAL, VARIABLE)},
)

TABLES = {
    "model_providers": {"p": {"kind": "anthropic", "api_key_env": "P_KEY"}},
    "models": {"m": {"provider": "p", "name": "echo"}},
    "workers": {WORKER: {"kind": "external", "secret_env": VARIABLE}},
    "agents": {
        WORKER: {
            "title": "Claude",
            "system_prompt": "",
            "model": "m",
            "engine": "claude-code",
            "worker": WORKER,
        }
    },
}

MISSING = (
    f"workers.{WORKER}: the secret is read from the environment variable {VARIABLE}, which is"
    " unset or empty"
)


async def started() -> tuple[Lifecycle, Any]:
    """The server's parts over the in-process store, started as ``robinauts start`` starts
    them, and its app; ``stop`` the lifecycle after."""
    composed = compose(
        CONFIG, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for=ENVIRON.get
    )
    lifecycle = Lifecycle(composed)
    app = build_app(composed, lifecycle, sign_in=None, secret_for=ENVIRON.get)
    await lifecycle.start()
    return lifecycle, app


def over(app: Any, secret: str) -> HttpTurnBroker:
    """The broker an external worker reaches the core through, over the app in the network's
    place."""
    return HttpTurnBroker("http://127.0.0.1", secret, transport=httpx.ASGITransport(app=app))


@asyncio_test
async def test_an_external_worker_is_let_in_and_started_by_nobody() -> None:
    lifecycle, _ = await started()
    try:
        composed = lifecycle.composed
        assert list(composed.workers) == [DEFAULT_WORKER]
        assert list(composed.brokers) == [DEFAULT_WORKER, WORKER]
        assert composed.external == {WORKER: SECRET}
        assert list(lifecycle.activity_files) == [DEFAULT_WORKER]
        assert lifecycle.healthy()
        assert lifecycle.access.admitted(f"Bearer {SECRET}") == WORKER
        assert lifecycle.access.admitted("Bearer something-else") is None
    finally:
        await lifecycle.stop()
    assert not lifecycle.access.is_open


@asyncio_test
async def test_its_agents_turns_wait_for_it_and_it_claims_them_over_the_api() -> None:
    lifecycle, app = await started()
    controller: Any = lifecycle.composed.controller
    try:
        user = await controller.ensure_user(Identity("local", "me"))
        asked = await controller.start_session(user, agent=WORKER, model="m", text="hello")
        now = datetime.now(UTC)
        until = now + timedelta(seconds=90)
        # The default worker, in this process, is handed nothing of it...
        assert await lifecycle.composed.brokers[DEFAULT_WORKER].claim_tasks(now, until, 10) == []
        # ...and the external worker, over the API with its secret, claims and ends it.
        broker = over(app, SECRET)
        try:
            tasks = await broker.claim_tasks(now, until, 10)
            assert [uuid.UUID(t.payload["turn_id"]) for t in tasks] == [asked.turn_id]
            assert tasks[0].worker == WORKER
            found = await broker.find_turn(asked.turn_id)
            assert found is not None
            owner, turn = found
            session = await broker.get_session(owner, turn.session_id)
            assert (session.agent, session.engine) == (WORKER, "claude-code")
            await broker.finish_turn(
                owner,
                turn.session_id,
                turn.id,
                TurnState.FAILED,
                now,
                "no engine here",
                None,
                [],
                now,
            )
        finally:
            await broker.close()
        opened = await controller.open_session(user, asked.session_id)
        assert opened.active is None
    finally:
        await lifecycle.stop()


@asyncio_test
async def test_any_other_credential_is_refused() -> None:
    lifecycle, app = await started()
    try:
        now = datetime.now(UTC)
        wrong = over(app, "not-the-secret")
        try:
            with pytest.raises(BrokerRefusedError, match="401"):
                await wrong.claim_tasks(now, now + timedelta(seconds=90), 10)
        finally:
            await wrong.close()
    finally:
        await lifecycle.stop()


def test_a_missing_secret_is_named_before_anything_starts() -> None:
    with pytest.raises(ConfigError, match=re.escape(MISSING)):
        external_secrets(CONFIG, {}.get)
    with pytest.raises(ConfigError, match=re.escape(MISSING)):
        compose(
            CONFIG, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={VARIABLE: ""}.get
        )


def test_serving_reports_the_missing_secret_with_the_other_problems() -> None:
    with pytest.raises(ConfigError) as raised:
        serving({**TABLES, "colour": {}}, {}, host="127.0.0.1", dev_no_sign_in=True)
    message = str(raised.value)
    assert "colour: unknown key" in message
    assert MISSING in message
    config, _, sign_in = serving(TABLES, ENVIRON, host="127.0.0.1", dev_no_sign_in=True)
    assert config.is_external(WORKER)
    assert sign_in is None
