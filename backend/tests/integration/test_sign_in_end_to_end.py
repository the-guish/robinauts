# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Signing in for real: the application, the HTTP adapter and a provider on a socket.

Everything that is not the database and not a route: ``application.SignIn``
over ``HttpIdentityProvider``, ``SystemClock`` and ``OsSecretSource``, against
a stand-in provider listening on loopback, with the in-memory store standing
in for PostgreSQL. What the other suites prove separately -- the rules in
``core``, the flow over fakes, the adapter's HTTP -- this proves fits
together: a person is sent to a provider, comes back with a code, and ends up
with a session that resolves to them.

The clock is the real one, because the ID token the stand-in issues carries
real ``iat`` and ``exp`` claims and the claim check compares them with what
the ``Clock`` port says. Nothing sleeps all the same: the only waiting here is
for a socket to answer.

Marked ``io``: it opens sockets. The redirect back from the authorization
endpoint is followed by hand (``standin.redirect_from``), because a browser is
the one part of the flow that is not ours.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest

from aio import asyncio_test
from fakes import MemoryCredentialStore
from robinauts.legacy.adapters import HttpIdentityProvider, OsSecretSource, SystemClock
from robinauts.legacy.application import SignIn
from robinauts.legacy.core import secret_hash
from robinauts.legacy.domain import (
    AllowEntry,
    Matcher,
    ProviderConfig,
    SignInConfig,
    SignInError,
    SignInErrorCode,
)
from standin import Misbehaviour, StandInProvider, redirect_from

pytestmark = pytest.mark.io

PUBLIC_URL = "https://robinauts.example.com"
SECRET_ENV = "ROBINAUTS_STAND_IN_SECRET"
GROUP = "robinauts-users"


@pytest.fixture
def stand_in() -> Iterator[StandInProvider]:
    with StandInProvider() as provider:
        yield provider


def google_like(stand_in: StandInProvider) -> ProviderConfig:
    return ProviderConfig(
        id="google",
        title="Google",
        issuer=stand_in.issuer,
        client_id=stand_in.client_id,
        client_secret_env=SECRET_ENV,
    )


def okta_like(stand_in: StandInProvider) -> ProviderConfig:
    return ProviderConfig(
        id="okta",
        title="Okta",
        issuer=stand_in.issuer,
        client_id=stand_in.client_id,
        client_secret_env=SECRET_ENV,
        scopes=("openid", "email", "profile", "groups"),
        groups_claim="groups",
        token_endpoint_auth="client_secret_post",
    )


ALLOW = {
    "google": AllowEntry("google", Matcher.EMAIL, "ada@example.com"),
    "okta": AllowEntry("okta", Matcher.GROUP, GROUP),
}
KINDS = {"google-like": google_like, "okta-like": okta_like}


class Deployment:
    """A whole sign-in, wired as the composition root will wire it."""

    def __init__(self, stand_in: StandInProvider, provider: ProviderConfig) -> None:
        self.stand_in = stand_in
        self.provider = provider
        self.store = MemoryCredentialStore()
        self.adapter = HttpIdentityProvider(
            secret_for={SECRET_ENV: stand_in.client_secret}.get, trust_env=False
        )
        self.sign_in = SignIn(
            SignInConfig(
                public_url=PUBLIC_URL,
                providers={provider.id: provider},
                allow=(ALLOW[provider.id],),
            ),
            credentials=self.store,
            provider=self.adapter,
            clock=SystemClock(),
            secrets=OsSecretSource(),
        )
        # Whoever the provider says signed in; the group is what the Okta-like
        # allow entry matches on, and is ignored by a provider with no groups
        # claim configured.
        stand_in.person["groups"] = [GROUP]

    async def aclose(self) -> None:
        await self.adapter.aclose()


@pytest.fixture(params=sorted(KINDS))
def deployment(request: pytest.FixtureRequest, stand_in: StandInProvider) -> Deployment:
    return Deployment(stand_in, KINDS[request.param](stand_in))


async def callback(deployment: Deployment, url: str) -> tuple[str | None, str | None]:
    """What the browser brings back from the authorization endpoint.

    The provider redirects to the registered ``redirect_uri``, which is a URL
    of the deployment and is not served here: the query is read off the
    ``Location`` header, which is exactly what the callback route will be
    given.
    """
    location = await redirect_from(url)
    assert location.startswith(f"{PUBLIC_URL}/auth/callback/{deployment.provider.id}")
    query = parse_qs(urlsplit(location).query)
    return _first(query, "code"), _first(query, "state")


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


@asyncio_test
async def test_a_whole_sign_in_ends_in_a_session_that_resolves(
    deployment: Deployment,
) -> None:
    try:
        begun = await deployment.sign_in.begin(deployment.provider.id, return_to="/chat/42")
        code, state = await callback(deployment, begun.authorization_url)
        opened = await deployment.sign_in.complete(
            deployment.provider.id, state=state, cookie_state=begun.state, code=code
        )

        who = await deployment.sign_in.resolve_session(opened.secret)
    finally:
        await deployment.aclose()

    assert state == begun.state
    assert opened.user.email == "ada@example.com"
    assert opened.user.name == "Ada Lovelace"
    assert opened.user.provider == deployment.provider.id
    assert opened.user.subject == "248289761001"
    assert opened.return_to == "/chat/42"
    assert who == opened.user


@asyncio_test
async def test_the_provider_was_asked_exactly_what_the_flow_asks_for(
    deployment: Deployment,
) -> None:
    try:
        begun = await deployment.sign_in.begin(deployment.provider.id)
        code, state = await callback(deployment, begun.authorization_url)
        await deployment.sign_in.complete(
            deployment.provider.id, state=state, cookie_state=begun.state, code=code
        )
    finally:
        await deployment.aclose()

    asked = deployment.stand_in.authorized[-1]
    assert asked["response_type"] == "code"
    assert asked["client_id"] == deployment.provider.client_id
    assert asked["redirect_uri"] == f"{PUBLIC_URL}/auth/callback/{deployment.provider.id}"
    assert asked["code_challenge_method"] == "S256"
    assert asked["scope"] == " ".join(deployment.provider.scopes)
    # Only the challenge is sent; the verifier is what proves, at the token
    # endpoint, that this is the browser that asked.
    posted = deployment.stand_in.exchanged[-1]
    assert posted["code_verifier"] not in asked.values()
    assert posted["redirect_uri"] == asked["redirect_uri"]


@asyncio_test
async def test_nothing_secret_is_left_in_the_store(deployment: Deployment) -> None:
    try:
        begun = await deployment.sign_in.begin(deployment.provider.id)
        code, state = await callback(deployment, begun.authorization_url)
        opened = await deployment.sign_in.complete(
            deployment.provider.id, state=state, cookie_state=begun.state, code=code
        )
    finally:
        await deployment.aclose()

    written = deployment.store.everything()
    assert opened.secret not in written
    assert begun.state not in written
    assert deployment.stand_in.client_secret not in written
    # What is kept of the cookie is its hash, and that is how it is found.
    assert secret_hash(opened.secret) in written


@asyncio_test
async def test_signing_out_ends_the_session(deployment: Deployment) -> None:
    try:
        begun = await deployment.sign_in.begin(deployment.provider.id)
        code, state = await callback(deployment, begun.authorization_url)
        opened = await deployment.sign_in.complete(
            deployment.provider.id, state=state, cookie_state=begun.state, code=code
        )

        assert await deployment.sign_in.sign_out(opened.secret) is True
        after = await deployment.sign_in.resolve_session(opened.secret)
        again = await deployment.sign_in.sign_out(opened.secret)
    finally:
        await deployment.aclose()

    assert after is None
    assert again is False


@asyncio_test
async def test_a_sign_in_cannot_be_used_twice(deployment: Deployment) -> None:
    try:
        begun = await deployment.sign_in.begin(deployment.provider.id)
        code, state = await callback(deployment, begun.authorization_url)
        await deployment.sign_in.complete(
            deployment.provider.id, state=state, cookie_state=begun.state, code=code
        )

        with pytest.raises(SignInError) as raised:
            await deployment.sign_in.complete(
                deployment.provider.id, state=state, cookie_state=begun.state, code=code
            )
    finally:
        await deployment.aclose()

    assert raised.value.code is SignInErrorCode.EXPIRED


@asyncio_test
async def test_two_sign_ins_by_one_person_are_one_user(deployment: Deployment) -> None:
    try:
        for _ in range(2):
            begun = await deployment.sign_in.begin(deployment.provider.id)
            code, state = await callback(deployment, begun.authorization_url)
            opened = await deployment.sign_in.complete(
                deployment.provider.id, state=state, cookie_state=begun.state, code=code
            )
    finally:
        await deployment.aclose()

    assert len(deployment.store.users) == 1
    assert len(deployment.store.sessions) == 2
    assert opened.user.id == deployment.store.users[0].id


@asyncio_test
async def test_somebody_the_allow_list_does_not_have_is_refused(
    deployment: Deployment,
) -> None:
    deployment.stand_in.person.update(
        {"sub": "999", "email": "mallory@example.org", "groups": ["nobody"]}
    )

    try:
        begun = await deployment.sign_in.begin(deployment.provider.id)
        code, state = await callback(deployment, begun.authorization_url)
        with pytest.raises(SignInError) as raised:
            await deployment.sign_in.complete(
                deployment.provider.id, state=state, cookie_state=begun.state, code=code
            )
    finally:
        await deployment.aclose()

    assert raised.value.code is SignInErrorCode.NOT_ALLOWED
    assert deployment.store.users == []
    assert deployment.store.sessions == {}


@asyncio_test
async def test_an_id_token_for_another_sign_in_is_refused(deployment: Deployment) -> None:
    # The nonce is what ties the token to the browser that began the sign-in.
    deployment.stand_in.tamper = {"nonce": "some-other-sign-in"}

    try:
        begun = await deployment.sign_in.begin(deployment.provider.id)
        code, state = await callback(deployment, begun.authorization_url)
        with pytest.raises(SignInError) as raised:
            await deployment.sign_in.complete(
                deployment.provider.id, state=state, cookie_state=begun.state, code=code
            )
    finally:
        await deployment.aclose()

    assert raised.value.code is SignInErrorCode.INVALID_ID_TOKEN


@asyncio_test
async def test_a_provider_that_refuses_the_code_stops_the_sign_in(
    deployment: Deployment,
) -> None:
    try:
        begun = await deployment.sign_in.begin(deployment.provider.id)
        code, state = await callback(deployment, begun.authorization_url)
        deployment.stand_in.token_misbehaves(Misbehaviour.oauth_error("invalid_grant"))
        with pytest.raises(SignInError) as raised:
            await deployment.sign_in.complete(
                deployment.provider.id, state=state, cookie_state=begun.state, code=code
            )
    finally:
        await deployment.aclose()

    assert raised.value.code is SignInErrorCode.PROVIDER_REFUSED
    assert deployment.store.sessions == {}


@asyncio_test
async def test_a_provider_that_is_down_leaves_no_sign_in_behind(
    deployment: Deployment,
) -> None:
    # Discovery comes before anything is stored, so a provider that cannot be
    # reached leaves no row behind a sign-in that could never finish.
    deployment.stand_in.close()

    try:
        with pytest.raises(SignInError) as raised:
            await deployment.sign_in.begin(deployment.provider.id)
    finally:
        await deployment.aclose()

    assert raised.value.code is SignInErrorCode.PROVIDER_UNAVAILABLE
    assert deployment.store.pending_logins == {}


@asyncio_test
async def test_a_failed_discovery_is_not_remembered_as_a_success(
    deployment: Deployment,
) -> None:
    # A success is cached for the life of the process; a failure is not, so
    # the next sign-in tries again and works.
    deployment.stand_in.discovery_misbehaves(Misbehaviour.server_error())

    try:
        with pytest.raises(SignInError):
            await deployment.sign_in.begin(deployment.provider.id)

        begun = await deployment.sign_in.begin(deployment.provider.id)
        code, state = await callback(deployment, begun.authorization_url)
        opened = await deployment.sign_in.complete(
            deployment.provider.id, state=state, cookie_state=begun.state, code=code
        )
    finally:
        await deployment.aclose()

    assert opened.user.subject == "248289761001"
    # And a success is remembered: the third sign-in asked for no document.
    assert deployment.stand_in.paths.count("/.well-known/openid-configuration") == 2
