# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A run is the person's who started it, through Robinauts or on neorc's own form, and so are
the conversations its handlers open for it (``docs/architecture/orchestration.md``, "Whose a
run is"). neorc records the starter on the run. A handler holds a token of
``robinauts/handler`` and names its run in ``X-Robinauts-Run``; the server acts for the person
neorc records, and for nobody else, after a restart too.

On a backend that signs two people in through a provider, with neorc's manager composed; the
handler's requests are made here, as neorc's worker would make them."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from neorc_core import QueueKind
from neorc_core.flows import read_flows
from neorc_core.local import DirectManagerClient
from oneberth.identity import Identity
from oneberth.web import testing

from robinauts.cli.composition import DATA_DIR_VARIABLE, compose
from robinauts.cli.main import Backend
from robinauts.shared_domain.domain import (
    AgentConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    StorageConfig,
    StorageKind,
)
from robinauts.web import roles
from robinauts.web.app import NAME, RUN_HEADER
from util.aio import asyncio_test
from util.app import backend_of

CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", models=("echo",), engine="echo")
    },
)

FLOW = """
name: probe
version: 0.1.0
steps:
  pick:
    queue: default
    handler: robinauts_tasks:pick_number
"""


@asyncio_test
async def test_a_handler_s_conversation_belongs_to_the_person_who_started_its_run(
    tmp_path: Path,
) -> None:
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "probe.yaml").write_text(FLOW)
    environ = {"ROBINAUTS_FLOWS_DIR": str(flows), "PATH": os.environ.get("PATH", "")}
    assert Path(sys.executable).with_name("neorc").is_file()
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_PROCESS), secret_for={}.get)
    backend = backend_of(
        composed, secret_for={}.get, environ=environ, oneberth=testing.config_for(NAME)
    )
    composition = backend.composition
    manager = backend.orchestrator.manager  # type: ignore[attr-defined]
    async with (
        testing.running(composition) as ada,
        testing.client_of(composition) as bob,
        testing.client_of(composition) as handler,
    ):
        await manager.declare_queue("default", QueueKind.WORKER)
        await DirectManagerClient(manager).upload_flows(read_flows(flows))
        await testing.signed_in(composition, ada, Identity("test", "ada"))
        await testing.signed_in(composition, bob, Identity("test", "bob"))
        token = testing.bearer(await testing.service_token(composition, roles.HANDLER_ROLE))
        worker = testing.bearer(
            await testing.service_token(composition, roles.WORKER_ROLE, "default")
        )

        started = await ada.post(f"/{NAME}/api/flows/probe/runs", json={"inputs": {}})
        assert started.status_code == 201, started.text
        run = started.json()["id"]
        # Started at neorc with Ada's own credential: hers to read there too.
        at_neorc = await ada.get(f"/neorc/runs/{run}")
        assert at_neorc.status_code == 200, at_neorc.text

        turn = {"agent_id": "echo", "text": "for the run"}
        opened = await handler.post(
            f"/{NAME}/api/turns", json=turn, headers={**token, RUN_HEADER: run}
        )
        assert opened.status_code == 200, opened.text
        cid = opened.headers["x-robinauts-conversation-id"]

        adas = (await ada.get(f"/{NAME}/api/conversations")).json()["items"]
        assert [c["id"] for c in adas] == [cid]
        assert (await bob.get(f"/{NAME}/api/conversations")).json()["items"] == []
        assert (await bob.get(f"/{NAME}/api/conversations/{cid}")).status_code == 404
        read = await handler.get(
            f"/{NAME}/api/conversations/{cid}", headers={**token, RUN_HEADER: run}
        )
        assert read.status_code == 200, read.text

        # Without a run, with a run nobody started here, or with a token of another role:
        # the server acts for nobody.
        for headers in (token, {**token, RUN_HEADER: "not-a-run"}, {**worker, RUN_HEADER: run}):
            refused = await handler.post(f"/{NAME}/api/turns", json=turn, headers=headers)
            assert refused.status_code == 403, refused.text
        # A handler starts no flow, and lists nothing of a person's but through its run.
        assert (
            await handler.post(f"/{NAME}/api/flows/probe/runs", json={"inputs": {}}, headers=token)
        ).status_code == 403


@asyncio_test
async def test_a_run_started_on_neorc_s_own_form_is_its_starter_s_after_a_restart_too(
    tmp_path: Path,
) -> None:
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "probe.yaml").write_text(FLOW)
    data = tmp_path / "data"
    data.mkdir()
    storage = StorageConfig(StorageKind.LOCAL, path=str(data))
    environ = {DATA_DIR_VARIABLE: str(data)}
    turn = {"agent_id": "echo", "text": "for the run"}

    def a_backend() -> Backend:
        composed = compose(CONFIG, storage=storage, secret_for={}.get)
        return backend_of(
            composed, secret_for={}.get, environ=environ, oneberth=testing.config_for(NAME)
        )

    first = a_backend()
    composition = first.composition
    manager = first.orchestrator.manager  # type: ignore[attr-defined]
    async with (
        testing.running(composition) as ada,
        testing.client_of(composition) as bob,
        testing.client_of(composition) as handler,
    ):
        await manager.declare_queue("default", QueueKind.WORKER)
        await DirectManagerClient(manager).upload_flows(read_flows(flows))
        await testing.signed_in(composition, ada, Identity("test", "ada", name="Ada"))
        await testing.signed_in(composition, bob, Identity("test", "bob", name="Bob"))
        token = testing.bearer(await testing.service_token(composition, roles.HANDLER_ROLE))

        # neorc's own route, as its form posts it.
        started = await ada.post("/neorc/flows/probe/runs", json={"inputs": {}})
        assert started.status_code == 201, started.text
        run = started.json()["id"]
        assert started.json()["started_by"]["name"] == "Ada"

        opened = await handler.post(
            f"/{NAME}/api/turns", json=turn, headers={**token, RUN_HEADER: run}
        )
        assert opened.status_code == 200, opened.text
        cid = opened.headers["x-robinauts-conversation-id"]
        adas = (await ada.get(f"/{NAME}/api/conversations")).json()["items"]
        assert [c["id"] for c in adas] == [cid]
        assert (await bob.get(f"/{NAME}/api/conversations")).json()["items"] == []
        assert (await bob.get(f"/{NAME}/api/conversations/{cid}")).status_code == 404

    second = a_backend()
    composition = second.composition
    async with (
        testing.running(composition) as ada,
        testing.client_of(composition) as handler,
    ):
        await testing.signed_in(composition, ada, Identity("test", "ada", name="Ada"))
        token = testing.bearer(await testing.service_token(composition, roles.HANDLER_ROLE))
        again = await handler.post(
            f"/{NAME}/api/turns", json=turn, headers={**token, RUN_HEADER: run}
        )
        assert again.status_code == 200, again.text
        adas = (await ada.get(f"/{NAME}/api/conversations")).json()["items"]
        assert {c["id"] for c in adas} == {cid, again.headers["x-robinauts-conversation-id"]}
