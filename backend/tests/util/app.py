# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A backend over a composition, composed and started as ``robinauts start --local`` composes
and starts it, in this process: Robinauts' component and neorc's, oneberth's store in memory,
and a client on its front listener with the local user signed in."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from oneberth.config import Config as OneberthConfig
from oneberth.config import local_config
from oneberth.web import testing

from robinauts.cli.composition import Composed, SecretLookup
from robinauts.cli.lifecycle import Lifecycle
from robinauts.cli.main import Backend, store_of
from robinauts.cli.orchestration import orchestrator_of
from robinauts.web.app import NAME

PORT = 8000
PUBLIC_URL = f"http://127.0.0.1:{PORT}"
WORKER_URL = f"http://127.0.0.1:{PORT + 1}"


@dataclass(frozen=True, slots=True)
class Up:
    """A started backend, and a client on its front listener as the local user."""

    http: httpx.AsyncClient
    backend: Backend


def backend_of(
    composed: Composed,
    *,
    secret_for: SecretLookup,
    environ: Mapping[str, str] | None = None,
    oneberth: OneberthConfig | None = None,
) -> Backend:
    """The backend ``robinauts start --local`` composes over ``composed``, with oneberth's and
    neorc's stores in this process, or in the data directory ``environ`` names; with
    ``oneberth``, the backend ``robinauts start`` composes with that sign-in configuration
    instead. It starts no neorc worker, as a backend the launcher starts: a test plays the
    worker's part."""
    orchestrator = orchestrator_of(
        environ or {},
        composed.config,
        default_code=Path("."),
        public_url=PUBLIC_URL,
        api_url=PUBLIC_URL,
        worker_url=WORKER_URL,
        supervised=True,
    )
    return Backend(
        composed,
        Lifecycle(composed),
        orchestrator,
        oneberth=local_config(NAME, port=PORT) if oneberth is None else oneberth,
        secret_for=secret_for,
        store=store_of(environ or {}),
        local=oneberth is None,
    )


@asynccontextmanager
async def running(composed: Composed, *, secret_for: SecretLookup) -> AsyncIterator[Up]:
    backend = backend_of(composed, secret_for=secret_for)
    async with testing.running(backend.composition) as http:
        await testing.signed_in(backend.composition, http)
        yield Up(http, backend)
