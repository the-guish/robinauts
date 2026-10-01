# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Wiring the api for a test: the fakes, an application, and a client on it.

The routes are exercised over the real ASGI application through
``httpx.ASGITransport`` -- no socket, no server -- with the in-memory
credential store, the settable clock and the counting secret source standing
in for a deployment's own. ``httpx`` is confined to ``robinauts.legacy.adapters`` in
the package; a test is free to import it.

``base_url`` is the deployment's ``public_url``, so that the client stores the
cookies the way a browser at that origin would: a ``Secure`` cookie is kept
for an ``https`` deployment and dropped for an ``http`` one, which is half of
what the cookie tests are about.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from fakes import (
    CountingSecretSource,
    FakeClock,
    MemoryCredentialStore,
    ScriptedIdentityProvider,
)
from robinauts.legacy.api import create_api
from robinauts.legacy.application import Conversations, LocalAccess, SignIn, Turns, Watch
from robinauts.legacy.core import secret_hash
from robinauts.legacy.domain import (
    MAX_PENDING_LOGINS,
    AllowEntry,
    LocalMode,
    Matcher,
    ProviderConfig,
    SignInConfig,
    User,
)

PUBLIC_URL = "https://robinauts.example.com"
"""An https deployment: ``__Host-`` cookies, ``Secure``, an origin check."""

LOOPBACK_URL = "http://127.0.0.1:8080"
"""The other shape a deployment may have, and the only ``http`` one allowed."""

LOCAL_URL = "http://127.0.0.1:8000"
"""Where the local development mode is served in these tests, port and all.

A client on this ``base_url`` sends ``Host: 127.0.0.1:8000`` and, through
``ASGITransport``, answers on the same address -- which is what the mode's own
check reads (``robinauts.legacy.api.protection``).
"""

SECRET_VARIABLE = "ROBINAUTS_TEST_SECRET"

GOOGLE = ProviderConfig(
    id="google",
    title="Google",
    issuer="https://accounts.google.com",
    client_id="a-client",
    client_secret_env=SECRET_VARIABLE,
)

JSON = {"content-type": "application/json"}
"""What a write must be sent as; the shortest way for a test to say so."""


def cookie(name: str, value: str) -> dict[str, str]:
    """One cookie as a header, rather than through the client's jar.

    Sent by hand where a test is about what the server makes of a cookie it is
    given; the jar is left to do its own work where a test follows a sign-in
    and the cookies are the ones the server set.
    """
    return {"cookie": f"{name}={value}"}


def sign_in_config(
    *,
    public_url: str = PUBLIC_URL,
    providers: Iterable[ProviderConfig] = (GOOGLE,),
    allow: Iterable[AllowEntry] | None = None,
) -> SignInConfig:
    """A deployment's sign-in configuration, with each provider's own people let in.

    For Google that is its Workspace, ``example.com``, and for any other
    provider everyone: the configuration refuses ``everyone`` for Google, and a
    test's deployment is one it would have started. A Gmail account, with no
    ``hd``, is not let in.
    """
    named = {provider.id: provider for provider in providers}
    return SignInConfig(
        public_url=public_url,
        providers=named,
        allow=(
            tuple(allow)
            if allow is not None
            else tuple(
                (
                    AllowEntry(name, Matcher.HOSTED_DOMAIN, "example.com")
                    if provider.is_google
                    else AllowEntry(name, Matcher.EVERYONE)
                )
                for name, provider in named.items()
            )
        ),
    )


@dataclass
class Wired:
    """A sign-in over fakes, with each fake still in reach of the test."""

    config: SignInConfig
    store: MemoryCredentialStore = field(default_factory=MemoryCredentialStore)
    clock: FakeClock = field(default_factory=FakeClock)
    secrets: CountingSecretSource = field(default_factory=CountingSecretSource)
    provider: ScriptedIdentityProvider = field(default_factory=ScriptedIdentityProvider)
    max_pending_logins: int = MAX_PENDING_LOGINS
    sign_in: SignIn = field(init=False)

    def __post_init__(self) -> None:
        self.sign_in = SignIn(
            self.config,
            credentials=self.store,
            provider=self.provider,
            clock=self.clock,
            secrets=self.secrets,
            max_pending_logins=self.max_pending_logins,
        )
        for provider in self.config.providers.values():
            self.provider.publishes(provider)


SESSION_SECRET = "a-session-secret" + "s" * 27
"""43 characters of the URL-safe alphabet: the shape a real secret has."""


async def open_session(
    deployment: Wired,
    *,
    subject: str = "248289761001",
    name: str | None = "Ada Lovelace",
    email: str | None = "ada@example.com",
    secret: str = SESSION_SECRET,
) -> tuple[str, User]:
    """A user and a live session in the store; the secret its cookie holds.

    The routes are what these tests are about, so the session is put there
    directly rather than signed in for. ``tests/integration/test_api_sign_in``
    is where one is opened by signing in.
    """
    now = deployment.clock.now()
    user = await deployment.store.user_at_sign_in(
        "google", subject, name=name, email=email, now=now
    )
    await deployment.store.add_session(
        secret_hash(secret),
        user.id,
        created_at=now,
        expires_at=now + deployment.config.session_life,
    )
    return secret, user


def wired(*, max_pending_logins: int = MAX_PENDING_LOGINS, **changes: object) -> Wired:
    """A wired sign-in for a configuration built from ``changes``."""
    return Wired(
        sign_in_config(**changes),  # type: ignore[arg-type]
        max_pending_logins=max_pending_logins,
    )


@dataclass
class WiredLocally:
    """The local development mode over the same fakes, and its one user.

    The store is the test's: the local user is a row in it, as it is a row in
    a real database, and a test reads it there rather than being told about
    it.
    """

    mode: LocalMode = field(default_factory=LocalMode)
    store: MemoryCredentialStore = field(default_factory=MemoryCredentialStore)
    clock: FakeClock = field(default_factory=FakeClock)
    local: LocalAccess = field(init=False)

    def __post_init__(self) -> None:
        self.local = LocalAccess(self.mode, credentials=self.store, clock=self.clock)


@asynccontextmanager
async def running(app: Any) -> AsyncIterator[None]:
    """Drive an application's ASGI lifespan, as a server does.

    There is no ``asgi-lifespan`` here and there is not going to be: the
    protocol is two messages each way, and a test that drives them itself is
    a test that shows what a server does. A start-up that fails raises what
    it raised, rather than a message about it.
    """
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    replies: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def receive() -> dict[str, Any]:
        return await events.get()

    async def send(message: dict[str, Any]) -> None:
        await replies.put(message)

    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    served = asyncio.get_running_loop().create_task(app(scope, receive, send))
    await events.put({"type": "lifespan.startup"})
    started = await replies.get()
    if started["type"] == "lifespan.startup.failed":
        await served
        raise AssertionError(started.get("message") or "start-up failed silently")
    try:
        yield
    finally:
        await events.put({"type": "lifespan.shutdown"})
        await replies.get()
        await served


@asynccontextmanager
async def serving(
    sign_in: SignIn | None,
    *,
    local: LocalAccess | None = None,
    conversations: Conversations | None = None,
    turns: Turns | None = None,
    watch: Watch | None = None,
    base_url: str = PUBLIC_URL,
) -> AsyncIterator[httpx.AsyncClient]:
    """A client on the real application, wired to ``sign_in`` or to ``local``.

    Redirects are not followed: what the tests are about is the ``Location``
    and the ``Set-Cookie`` of each one.

    ``conversations``, ``turns`` and ``watch`` are the services the
    conversation and streaming routes call, handed in the way the composition
    root hands in the ones it opened (``tests/turns.py`` builds them over the
    fakes). Left out where a test is about the auth routes, which is also how a
    route that asked for one before start-up is tested.
    """
    app = create_api(sign_in, local=local, conversations=conversations, turns=turns, watch=watch)
    async with client_on(app, base_url=base_url) as client:
        yield client


@asynccontextmanager
async def client_on(app: Any, *, base_url: str = PUBLIC_URL) -> AsyncIterator[httpx.AsyncClient]:
    """A client on an application a test built itself.

    ``serving`` is this and ``create_api`` together, which is all most tests
    want. A test that also drives the application **as a server does** -- which
    is what following a stream means, since ``httpx``'s ASGI transport runs an
    application to its end before it answers -- wants the application itself as
    well, and builds it rather than reaching into a client for it
    (``tests/sse.py``).
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        yield client
