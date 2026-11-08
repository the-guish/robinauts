# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An app over a composition, run as ``robinauts start`` runs it: the lifecycle started before
the app's own start-up, and stopped after its shut-down."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from robinauts.cli.composition import Composed
from robinauts.cli.lifecycle import Lifecycle
from robinauts.cli.main import build_app
from robinauts.web.oidc import SecretLookup
from robinauts.web.sign_in import SignInConfig


@asynccontextmanager
async def running(
    composed: Composed, *, sign_in: SignInConfig | None, secret_for: SecretLookup
) -> AsyncIterator[FastAPI]:
    lifecycle = Lifecycle(composed)
    app = build_app(composed, lifecycle, sign_in=sign_in, secret_for=secret_for)
    await lifecycle.start()
    try:
        async with app.router.lifespan_context(app):
            yield app
    finally:
        await lifecycle.stop()
