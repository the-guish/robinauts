# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The sign-in flow over the fakes: from the button to a session, and every refusal."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from aio import asyncio_test
from fakes import (
    CountingSecretSource,
    FakeClock,
    MemoryCredentialStore,
    ScriptedIdentityProvider,
    StuntedSecretSource,
    discovery_for,
)
from robinauts.legacy.application import (
    PENDING_LOGIN_LIFE,
    SWEEP_SECONDS,
    BegunSignIn,
    OpenedSession,
    SignIn,
)
from robinauts.legacy.core import MAX_CODE_CHARS, MAX_SECRET_CHARS, pkce_challenge, secret_hash
from robinauts.legacy.domain import (
    AllowEntry,
    InvalidValueError,
    Matcher,
    PendingLogin,
    ProviderConfig,
    SignInConfig,
    SignInError,
    SignInErrorCode,
)

GOOGLE = ProviderConfig(
    id="google",
    title="Google",
    issuer="https://accounts.google.com",
    client_id="cid.apps.googleusercontent.com",
    client_secret_env="ROBINAUTS_GOOGLE_SECRET",
)
OKTA = ProviderConfig(
    id="okta",
    title="Okta",
    issuer="https://example.okta.com/oauth2/default",
    client_id="okta-client",
    client_secret_env="ROBINAUTS_OKTA_SECRET",
    scopes=("openid", "email", "profile", "groups"),
    groups_claim="groups",
)
CONFIG_FIELDS: dict[str, Any] = {
    "public_url": "https://robinauts.example.com",
    "providers": {"google": GOOGLE, "okta": OKTA},
    "allow": (
        AllowEntry("google", Matcher.HOSTED_DOMAIN, "example.com"),
        AllowEntry("okta", Matcher.GROUP, "robinauts-users"),
    ),
    "session_hours": 12.0,
}
CONFIG = SignInConfig(**CONFIG_FIELDS)


@dataclass(slots=True)
class World:
    """One deployment's sign-in, with every port a fake the test can move."""

    sign_in: SignIn
    clock: FakeClock
    secrets: CountingSecretSource
    store: MemoryCredentialStore
    provider: ScriptedIdentityProvider


def world(config: SignInConfig = CONFIG, **changes: Any) -> World:
    clock, secrets = FakeClock(), CountingSecretSource()
    store, provider = MemoryCredentialStore(), ScriptedIdentityProvider()
    for configured in config.providers.values():
        provider.publishes(configured)
    return World(
        sign_in=SignIn(
            config,
            credentials=store,
            provider=provider,
            clock=clock,
            secrets=secrets,
            **changes,
        ),
        clock=clock,
        secrets=secrets,
        store=store,
        provider=provider,
    )


def part(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def id_token(at: World, provider: ProviderConfig, nonce: str, **changes: Any) -> str:
    """An ID token as ``provider`` would issue it for this sign-in."""
    seconds = at.clock.now().timestamp()
    claims: dict[str, Any] = {
        "iss": provider.issuer,
        "aud": provider.client_id,
        "sub": "248289761001",
        "iat": seconds - 30,
        "exp": seconds + 3600,
        "nonce": nonce,
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "email_verified": True,
    }
    if provider is GOOGLE:
        claims["hd"] = "example.com"
    if provider is OKTA:
        claims["groups"] = ["robinauts-users", "everyone"]
    claims.update(changes)
    kept = {key: value for key, value in claims.items() if value is not ...}
    return f"{part({'alg': 'RS256'})}.{part(kept)}.signature"


def stored_login(at: World, begun: BegunSignIn) -> PendingLogin:
    """The pending sign-in ``begun`` left behind, found the way the store finds it."""
    return at.store.pending_logins[secret_hash(begun.state)]


async def begin(at: World, provider: ProviderConfig = GOOGLE, **changes: Any) -> BegunSignIn:
    return await at.sign_in.begin(provider.id, **changes)


async def complete(
    at: World,
    begun: BegunSignIn,
    provider: ProviderConfig = GOOGLE,
    *,
    token_response: Any = None,
    **changes: Any,
) -> OpenedSession:
    """Finish the sign-in ``begun``, with an ID token for it unless told otherwise."""
    if token_response is None:
        token_response = {"id_token": id_token(at, provider, stored_login(at, begun).nonce)}
    at.provider.answers(provider, token_response)
    call: dict[str, Any] = {
        "state": begun.state,
        "cookie_state": begun.state,
        "code": "the-code",
    }
    call.update(changes)
    return await at.sign_in.complete(provider.id, **call)


def refused(raised: pytest.ExceptionInfo[SignInError], code: SignInErrorCode) -> str:
    assert raised.value.code is code
    return raised.value.detail


def query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# Beginning a sign-in.


@asyncio_test
async def test_beginning_a_sign_in_sends_the_browser_to_the_provider() -> None:
    at = world()

    begun = await begin(at, GOOGLE, return_to="/#/chat/7")

    assert begun.authorization_url.startswith("https://accounts.google.com/authorize?")
    assert query(begun.authorization_url) == {
        "response_type": ["code"],
        "client_id": [GOOGLE.client_id],
        "redirect_uri": ["https://robinauts.example.com/auth/callback/google"],
        "scope": ["openid email profile"],
        "state": [begun.state],
        "nonce": [stored_login(at, begun).nonce],
        "code_challenge": [pkce_challenge(stored_login(at, begun).verifier)],
        "code_challenge_method": ["S256"],
    }


@asyncio_test
async def test_the_sign_in_is_stored_under_the_hash_of_its_state_for_ten_minutes() -> None:
    at = world()

    begun = await begin(at, OKTA, return_to="/#/chat/7")

    assert list(at.store.pending_logins) == [secret_hash(begun.state)]
    login = stored_login(at, begun)
    assert (login.provider, login.return_to) == ("okta", "/#/chat/7")
    assert login.created_at == at.clock.now()
    assert login.expires_at == at.clock.now() + timedelta(minutes=10)
    assert begun.expires_at == login.expires_at
    assert PENDING_LOGIN_LIFE == timedelta(minutes=10)


@asyncio_test
async def test_the_state_and_the_nonce_are_different_secrets_each_time() -> None:
    at = world()

    first, second = await begin(at), await begin(at)

    assert first.state != second.state
    logins = {first.state: stored_login(at, first), second.state: stored_login(at, second)}
    nonces = {login.nonce for login in logins.values()}
    verifiers = {login.verifier for login in logins.values()}
    assert len(nonces) == len(verifiers) == 2
    assert nonces.isdisjoint({first.state, second.state})


@asyncio_test
async def test_a_return_to_that_leaves_this_origin_is_dropped_rather_than_stored() -> None:
    at = world()

    begun = await begin(at, return_to="https://evil.example/steal")

    assert stored_login(at, begun).return_to is None


@asyncio_test
async def test_an_unknown_provider_cannot_be_signed_in_with() -> None:
    at = world()

    with pytest.raises(SignInError) as raised:
        await at.sign_in.begin("github")

    assert refused(raised, SignInErrorCode.UNKNOWN_PROVIDER)
    assert at.store.pending_logins == {}
    assert at.provider.discoveries == []


@asyncio_test
async def test_too_many_sign_ins_at_once_are_refused_as_busy() -> None:
    at = world(max_pending_logins=2)
    await begin(at)
    await begin(at)

    with pytest.raises(SignInError) as raised:
        await begin(at)

    assert "2 sign-ins" in refused(raised, SignInErrorCode.BUSY)
    assert len(at.store.pending_logins) == 2


@asyncio_test
async def test_the_cap_lets_a_sign_in_through_once_the_old_ones_expire() -> None:
    at = world(max_pending_logins=1)
    await begin(at)

    at.clock.advance(PENDING_LOGIN_LIFE)

    assert await begin(at)


# Finishing it.


@asyncio_test
async def test_a_google_sign_in_makes_a_user_and_a_session() -> None:
    at = world()
    begun = await begin(at, GOOGLE, return_to="/#/chat/7")

    opened = await complete(at, begun, GOOGLE)

    assert (opened.user.provider, opened.user.subject) == ("google", "248289761001")
    assert (opened.user.name, opened.user.email) == ("Ada Lovelace", "ada@example.com")
    assert opened.user.created_at == at.clock.now()
    assert opened.session.user_id == opened.user.id
    assert opened.session.expires_at == at.clock.now() + timedelta(hours=12)
    assert opened.return_to == "/#/chat/7"
    assert await at.sign_in.resolve_session(opened.secret) == opened.user


@asyncio_test
async def test_an_okta_sign_in_is_allowed_by_a_group() -> None:
    at = world()
    begun = await begin(at, OKTA)
    login = stored_login(at, begun)

    opened = await complete(at, begun, OKTA)

    assert opened.user.provider == "okta"
    assert opened.return_to == "/"
    # The code went to the endpoint discovery named, with the PKCE verifier
    # the challenge was made from and the redirect URI registered with Okta.
    assert at.provider.exchanges == [
        {
            "provider": "okta",
            "token_endpoint": "https://example.okta.com/oauth2/default/token",
            "code": "the-code",
            "verifier": login.verifier,
            "redirect_uri": "https://robinauts.example.com/auth/callback/okta",
        }
    ]


@asyncio_test
async def test_signing_in_again_refreshes_the_user_and_keeps_their_id() -> None:
    at = world()
    first = await complete(at, await begin(at))

    at.clock.advance(timedelta(hours=1))
    again = await complete(at, await begin(at))

    assert again.user.id == first.user.id
    assert again.user.created_at == first.user.created_at
    assert len(at.store.users) == 1
    # Two sessions: signing in again opens one rather than extending one.
    assert len(at.store.sessions) == 2
    assert await at.sign_in.resolve_session(first.secret) == first.user


@asyncio_test
async def test_an_address_the_provider_did_not_verify_is_not_kept() -> None:
    at = world()
    begun = await begin(at, OKTA)

    opened = await complete(
        at,
        begun,
        OKTA,
        token_response={
            "id_token": id_token(at, OKTA, stored_login(at, begun).nonce, email_verified=False)
        },
    )

    assert opened.user.email is None
    assert opened.user.name == "Ada Lovelace"


@asyncio_test
async def test_a_sign_in_is_finished_once() -> None:
    at = world()
    begun = await begin(at)
    token_response = {"id_token": id_token(at, GOOGLE, stored_login(at, begun).nonce)}
    await complete(at, begun, token_response=token_response)

    with pytest.raises(SignInError) as raised:
        await at.sign_in.complete(
            "google", state=begun.state, cookie_state=begun.state, code="the-code"
        )

    assert refused(raised, SignInErrorCode.EXPIRED)
    assert len(at.store.sessions) == 1


@asyncio_test
async def test_a_sign_in_that_took_too_long_is_expired() -> None:
    at = world()
    begun = await begin(at)

    at.clock.advance(PENDING_LOGIN_LIFE)

    with pytest.raises(SignInError) as raised:
        await complete(at, begun)
    assert refused(raised, SignInErrorCode.EXPIRED)


@asyncio_test
async def test_a_state_this_deployment_never_handed_out_is_expired() -> None:
    at = world()
    await begin(at)

    with pytest.raises(SignInError) as raised:
        await at.sign_in.complete(
            "google", state="invented", cookie_state="invented", code="the-code"
        )
    assert refused(raised, SignInErrorCode.EXPIRED)


@pytest.mark.parametrize(
    ("state", "cookie"),
    [
        ("begun", "another-state"),
        ("begun", None),
        (None, "begun"),
        (None, None),
        ("begun", ""),
        ("", "begun"),
    ],
)
@asyncio_test
async def test_a_callback_the_browser_did_not_begin_is_a_state_mismatch(
    state: str | None, cookie: str | None
) -> None:
    at = world()
    begun = await begin(at)
    real = {"begun": begun.state}

    with pytest.raises(SignInError) as raised:
        await at.sign_in.complete(
            "google",
            state=real.get(state, state),
            cookie_state=real.get(cookie, cookie),
            code="the-code",
        )

    assert refused(raised, SignInErrorCode.STATE_MISMATCH)
    # Nothing was taken: the sign-in the browser really began still stands.
    assert secret_hash(begun.state) in at.store.pending_logins


@asyncio_test
async def test_a_state_of_an_enormous_size_is_not_even_looked_up() -> None:
    at = world()
    huge = "s" * 100_000

    with pytest.raises(SignInError) as raised:
        await at.sign_in.complete("google", state=huge, cookie_state=huge, code="c")

    assert refused(raised, SignInErrorCode.STATE_MISMATCH)


@asyncio_test
async def test_a_sign_in_begun_with_one_provider_cannot_finish_at_another() -> None:
    at = world()
    begun = await begin(at, GOOGLE)

    with pytest.raises(SignInError) as raised:
        await at.sign_in.complete(
            "okta", state=begun.state, cookie_state=begun.state, code="the-code"
        )

    assert "google" in refused(raised, SignInErrorCode.STATE_MISMATCH)


@pytest.mark.parametrize(
    ("code", "error"), [(None, "access_denied"), ("the-code", "access_denied")]
)
@asyncio_test
async def test_a_provider_that_says_no_is_a_refusal(code: str | None, error: str | None) -> None:
    at = world()
    begun = await begin(at)

    with pytest.raises(SignInError) as raised:
        await complete(at, begun, code=code, error=error)

    assert refused(raised, SignInErrorCode.PROVIDER_REFUSED)
    assert at.provider.exchanges == []


@pytest.mark.parametrize("code", [None, ""])
@asyncio_test
async def test_a_callback_with_neither_a_code_nor_an_error_is_a_refusal(code: str | None) -> None:
    # Nothing to exchange and nothing said about why: the provider did not
    # send this. Of the spec's eight codes it is the nearest -- the sign-in
    # did not fail here, and the browser is not to be told it did.
    at = world()
    begun = await begin(at)

    with pytest.raises(SignInError) as raised:
        await complete(at, begun, code=code, error=None)

    assert refused(raised, SignInErrorCode.PROVIDER_REFUSED)
    assert at.provider.exchanges == []


@asyncio_test
async def test_an_authorization_code_of_an_absurd_size_is_refused_unsent() -> None:
    at = world()
    begun = await begin(at)

    with pytest.raises(SignInError) as raised:
        await complete(at, begun, code="c" * (MAX_CODE_CHARS + 1))

    detail = refused(raised, SignInErrorCode.PROVIDER_REFUSED)
    assert at.provider.exchanges == []
    assert len(detail) < 300


@asyncio_test
async def test_a_token_endpoint_that_refuses_the_code_is_a_refusal() -> None:
    at = world()
    begun = await begin(at)
    refusal = SignInError(SignInErrorCode.PROVIDER_REFUSED, "invalid_grant")

    with pytest.raises(SignInError) as raised:
        await complete(at, begun, token_response=refusal)

    assert refused(raised, SignInErrorCode.PROVIDER_REFUSED) == "invalid_grant"


@pytest.mark.parametrize("answer", [["not", "an", "object"], "eyJ...", 7, True])
@asyncio_test
async def test_a_token_endpoint_that_answers_no_object_is_unavailable(answer: Any) -> None:
    # Not an ID token that is wrong -- an answer that is not a token response
    # at all, which ``ports/identity_provider.py`` calls unavailable.
    at = world()
    begun = await begin(at)

    with pytest.raises(SignInError) as raised:
        await complete(at, begun, token_response=answer)

    assert refused(raised, SignInErrorCode.PROVIDER_UNAVAILABLE)


@asyncio_test
async def test_a_token_response_without_an_id_token_is_not_a_sign_in() -> None:
    at = world()
    begun = await begin(at)

    with pytest.raises(SignInError) as raised:
        await complete(at, begun, token_response={"access_token": "at", "id_token": None})

    assert refused(raised, SignInErrorCode.INVALID_ID_TOKEN)


@asyncio_test
async def test_an_id_token_for_another_sign_in_is_refused() -> None:
    at = world()
    begun = await begin(at)

    with pytest.raises(SignInError) as raised:
        await complete(
            at,
            begun,
            token_response={"id_token": id_token(at, GOOGLE, "some-other-nonce")},
        )

    assert "nonce" in refused(raised, SignInErrorCode.INVALID_ID_TOKEN)


@asyncio_test
async def test_someone_the_allow_list_does_not_have_is_not_let_in() -> None:
    at = world()
    begun = await begin(at, GOOGLE)

    with pytest.raises(SignInError) as raised:
        await complete(
            at,
            begun,
            token_response={
                "id_token": id_token(
                    at,
                    GOOGLE,
                    stored_login(at, begun).nonce,
                    hd="other.example",
                    email="ada@other.example",
                )
            },
        )

    assert refused(raised, SignInErrorCode.NOT_ALLOWED)
    assert at.store.users == []
    assert at.store.sessions == {}


# Discovery.


@asyncio_test
async def test_discovery_happens_once_per_provider_and_is_kept() -> None:
    at = world()

    await begin(at, GOOGLE)
    await begin(at, GOOGLE)
    await begin(at, OKTA)

    assert at.provider.discoveries == ["google", "okta"]


@asyncio_test
async def test_sign_ins_at_once_discover_once_between_them() -> None:
    at = world()

    await asyncio.gather(begin(at, GOOGLE), begin(at, GOOGLE), begin(at, GOOGLE))

    assert at.provider.discoveries == ["google"]
    assert len(at.store.pending_logins) == 3


@asyncio_test
async def test_sign_ins_at_once_share_one_failed_discovery() -> None:
    # Not one fetch each, behind a lock: ten people would then pay for ten
    # attempts at a provider that had already failed, one after another.
    at = world()
    at.provider.documents["google"] = SignInError(SignInErrorCode.PROVIDER_UNAVAILABLE, "no answer")

    outcomes = await asyncio.gather(*(begin(at) for _ in range(10)), return_exceptions=True)

    assert at.provider.discoveries == ["google"]
    assert len(outcomes) == 10
    assert all(
        isinstance(outcome, SignInError) and outcome.code is SignInErrorCode.PROVIDER_UNAVAILABLE
        for outcome in outcomes
    )
    # And the failure was not kept: whoever comes next asks again.
    with pytest.raises(SignInError):
        await begin(at)
    assert at.provider.discoveries == ["google", "google"]


@asyncio_test
async def test_a_cancelled_sign_in_leaves_the_others_their_discovery() -> None:
    at = world()
    at.provider.pause = asyncio.Event()
    first = asyncio.get_running_loop().create_task(begin(at))
    second = asyncio.get_running_loop().create_task(begin(at))
    for _ in range(5):
        await asyncio.sleep(0)

    # Both are waiting on one fetch that has not finished: neither is done,
    # nothing is cached to have answered them, and only one fetch was begun.
    assert at.sign_in.discovering == frozenset({"google"})
    assert at.provider.discoveries == ["google"]
    assert not first.done()
    assert not second.done()

    first.cancel()
    at.provider.pause.set()
    begun = await second

    assert begun.authorization_url
    assert at.provider.discoveries == ["google"]
    assert at.sign_in.discovering == frozenset()
    with pytest.raises(asyncio.CancelledError):
        await first


@asyncio_test
async def test_a_finished_discovery_left_in_flight_is_not_handed_out_again() -> None:
    # A task cancelled before it ever ran does not run its own ``finally``,
    # so it can be left registered as in flight although it is over. Whoever
    # came next would be told their sign-in was cancelled, for the life of
    # the process. This is that state, made by hand.
    at = world()
    over = asyncio.get_running_loop().create_task(asyncio.sleep(0))
    over.cancel()
    with pytest.raises(asyncio.CancelledError):
        await over
    at.sign_in._discovering["google"] = over  # noqa: SLF001 -- the state to recover from

    begun = await begin(at, GOOGLE)

    assert begun.authorization_url
    assert at.sign_in.discovering == frozenset()
    assert at.provider.discoveries == ["google"]


@asyncio_test
async def test_a_mistake_in_an_adapter_is_not_a_provider_being_unavailable() -> None:
    # A TypeError out of an adapter is a bug in the adapter. Dressed up as
    # provider_unavailable it would sit behind a sign-in page for as long as
    # nobody read the logs closely.
    at = world()
    at.provider.documents["google"] = TypeError("str expected, dict given")

    with pytest.raises(TypeError):
        await begin(at, GOOGLE)


@pytest.mark.parametrize(
    ("key", "own"),
    [
        ("authorization_endpoint", "redirect_uri=https%3A%2F%2Fevil.example"),
        ("authorization_endpoint", "client_id=evil"),
        ("authorization_endpoint", "response_type=token"),
        ("authorization_endpoint", "scope=openid"),
        ("authorization_endpoint", "state=fixed"),
        ("authorization_endpoint", "nonce=fixed"),
        ("authorization_endpoint", "code_challenge=x"),
        ("authorization_endpoint", "code_challenge_method=plain"),
        ("authorization_endpoint", "flavour=corp&state=fixed"),
        # A server that folds case reads this as redirect_uri; so do we.
        ("authorization_endpoint", "Redirect_URI=https%3A%2F%2Fevil.example"),
        ("authorization_endpoint", "CODE_CHALLENGE_METHOD=plain"),
        # A token endpoint is posted to, so its query is not where the form
        # goes; one that names these all the same is not one we will use.
        ("token_endpoint", "client_secret=leaked"),
        ("token_endpoint", "code=theirs"),
        ("token_endpoint", "grant_type=implicit"),
        ("token_endpoint", "Code_Verifier=x"),
        ("token_endpoint", "redirect_uri=https%3A%2F%2Fevil.example"),
    ],
)
@asyncio_test
async def test_an_endpoint_that_sets_what_its_own_request_sets_is_refused(
    key: str, own: str
) -> None:
    # Its query goes in front of ours. A provider -- or whoever answered for
    # one -- naming redirect_uri there is choosing where the code is sent.
    at = world()
    at.provider.publishes(GOOGLE, **{key: f"https://accounts.google.com/{key.split('_')[0]}?{own}"})

    with pytest.raises(SignInError) as raised:
        await begin(at, GOOGLE)

    assert refused(raised, SignInErrorCode.PROVIDER_UNAVAILABLE)


@asyncio_test
async def test_a_provider_that_cannot_be_reached_leaves_no_sign_in_behind() -> None:
    at = world()
    at.provider.documents["google"] = TimeoutError("connect timed out")

    with pytest.raises(SignInError) as raised:
        await begin(at, GOOGLE)

    # An adapter's socket error is nobody's sign-in code: it becomes one,
    # with the original kept as the cause for the log.
    assert refused(raised, SignInErrorCode.PROVIDER_UNAVAILABLE)
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert at.store.pending_logins == {}


@asyncio_test
async def test_a_token_endpoint_that_breaks_is_unavailable_rather_than_a_crash() -> None:
    at = world()
    begun = await begin(at)

    with pytest.raises(SignInError) as raised:
        await complete(at, begun, token_response=OSError("connection reset"))

    assert refused(raised, SignInErrorCode.PROVIDER_UNAVAILABLE)
    assert isinstance(raised.value.__cause__, OSError)


@asyncio_test
async def test_a_cancelled_sign_in_stays_cancelled() -> None:
    at = world()
    at.provider.documents["google"] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await begin(at, GOOGLE)


@asyncio_test
async def test_a_failed_discovery_is_not_remembered() -> None:
    at = world()
    at.provider.documents["google"] = SignInError(SignInErrorCode.PROVIDER_UNAVAILABLE, "no answer")

    with pytest.raises(SignInError) as raised:
        await begin(at, GOOGLE)
    assert refused(raised, SignInErrorCode.PROVIDER_UNAVAILABLE)
    at.provider.publishes(GOOGLE)

    assert await begin(at, GOOGLE)
    assert at.provider.discoveries == ["google", "google"]


@pytest.mark.parametrize(
    "document",
    [
        {"issuer": "https://accounts.evil.example"},
        {"issuer": ...},
        {"issuer": 7},
        {"authorization_endpoint": "http://accounts.google.com/authorize"},
        {"authorization_endpoint": ...},
        {"token_endpoint": "http://accounts.google.com/token"},
        {"token_endpoint": ...},
        {"token_endpoint": "ftp://accounts.google.com/token"},
        {"token_endpoint": ["https://accounts.google.com/token"]},
        # urlsplit drops tabs and newlines wherever they are, as a browser
        # does, so each of these parses as a perfectly good URL -- and the
        # string itself would end up in a Location header or a POST.
        {"authorization_endpoint": "https://accounts.google.com/authorize\nX-Injected: yes"},
        {"authorization_endpoint": "https://accounts.google.com/aut\thorize"},
        {"token_endpoint": "https://accounts.google.com\r\n@evil.example/token"},
        {"token_endpoint": "https://user:pass@accounts.google.com/token"},
        {"authorization_endpoint": "https://accounts.google.com/authorize#fragment"},
    ],
)
@asyncio_test
async def test_a_discovery_document_that_cannot_be_believed_is_unavailable(
    document: dict[str, Any],
) -> None:
    at = world()
    changes = {key: (None if value is ... else value) for key, value in document.items()}
    at.provider.documents["google"] = discovery_for(GOOGLE, **changes)

    with pytest.raises(SignInError) as raised:
        await begin(at, GOOGLE)

    assert refused(raised, SignInErrorCode.PROVIDER_UNAVAILABLE)


@asyncio_test
async def test_a_discovery_answer_that_is_not_a_document_is_unavailable() -> None:
    at = world()
    at.provider.documents["google"] = ["not", "a", "document"]  # type: ignore[assignment]

    with pytest.raises(SignInError) as raised:
        await begin(at, GOOGLE)

    assert refused(raised, SignInErrorCode.PROVIDER_UNAVAILABLE)


@asyncio_test
async def test_a_loopback_provider_may_speak_http() -> None:
    # The stand-in provider of a development machine, and nothing else.
    local = ProviderConfig(
        id="local",
        title="Local",
        issuer="http://127.0.0.1:9000",
        client_id="local",
        client_secret_env="ROBINAUTS_LOCAL_SECRET",
    )
    config = SignInConfig(
        public_url="http://localhost:8000",
        providers={"local": local},
        allow=(AllowEntry("local", Matcher.EVERYONE),),
    )
    at = world(config)

    begun = await begin(at, local)

    assert begun.authorization_url.startswith("http://127.0.0.1:9000/authorize?")


@asyncio_test
async def test_the_endpoint_used_is_the_one_rebuilt_from_what_was_checked() -> None:
    # Not the string discovery sent: it is written again out of the pieces
    # that passed, which is what makes an injected control character
    # impossible rather than merely unlikely.
    at = world()
    at.provider.publishes(
        GOOGLE,
        authorization_endpoint="https://ACCOUNTS.Google.com.:443/authorize",
        token_endpoint="https://Accounts.Google.com:443/token",
    )

    begun = await begin(at, GOOGLE)
    await complete(at, begun, GOOGLE)

    assert begun.authorization_url.startswith("https://accounts.google.com/authorize?")
    assert at.provider.exchanges[0]["token_endpoint"] == "https://accounts.google.com/token"


@asyncio_test
async def test_an_authorization_endpoint_with_a_query_keeps_it() -> None:
    at = world()
    at.provider.publishes(
        GOOGLE, authorization_endpoint="https://accounts.google.com/authorize?flavour=corp"
    )

    begun = await begin(at, GOOGLE)

    assert "/authorize?flavour=corp&response_type=code" in begun.authorization_url
    assert query(begun.authorization_url)["flavour"] == ["corp"]


@asyncio_test
async def test_a_return_to_that_reaches_the_callback_from_the_store_is_checked() -> None:
    # It cannot come from ``begin``, which checked it. It could come from a
    # row somebody wrote, and an open redirect out of our own callback is
    # worth more to whoever wants one than the row was.
    at = world()
    begun = await begin(at, return_to="/#/chat/7")
    tampered = replace(stored_login(at, begun), return_to="https://evil.example/steal")
    at.store.plant(secret_hash(begun.state), tampered)

    opened = await complete(at, begun)

    assert opened.return_to == "/"


# Secrets the source hands out.


@pytest.mark.parametrize("length", [8, MAX_SECRET_CHARS + 1])
@asyncio_test
async def test_a_secret_source_that_gives_the_wrong_amount_stops_the_sign_in(
    length: int,
) -> None:
    # Too little is guessable; too much is never looked up, so every callback
    # would fail its state comparison. Both are the deployment's mistake.
    at = world()
    at.sign_in._secrets = StuntedSecretSource(length)  # noqa: SLF001 -- a broken build

    with pytest.raises(InvalidValueError, match="43"):
        await begin(at)

    assert at.store.pending_logins == {}


# Sessions.


@asyncio_test
async def test_a_session_ends_at_its_fixed_life_and_is_never_renewed() -> None:
    at = world()
    opened = await complete(at, await begin(at))

    at.clock.advance(timedelta(hours=11, minutes=59))
    assert await at.sign_in.resolve_session(opened.secret) == opened.user

    at.clock.advance(timedelta(minutes=1))
    assert await at.sign_in.resolve_session(opened.secret) is None


@pytest.mark.parametrize("secret", [None, "", "not-a-session", "s" * 100_000])
@asyncio_test
async def test_a_cookie_that_is_not_a_session_signs_nobody_in(secret: str | None) -> None:
    at = world()
    await complete(at, await begin(at))

    assert await at.sign_in.resolve_session(secret) is None
    assert await at.sign_in.sign_out(secret) is False


@asyncio_test
async def test_signing_out_ends_the_session_and_only_that_one() -> None:
    at = world()
    one = await complete(at, await begin(at))
    other = await complete(at, await begin(at, OKTA), OKTA)

    assert await at.sign_in.sign_out(one.secret) is True
    assert await at.sign_in.sign_out(one.secret) is False
    assert await at.sign_in.resolve_session(one.secret) is None
    assert await at.sign_in.resolve_session(other.secret) == other.user


# Sweeping.


@asyncio_test
async def test_expired_sessions_and_sign_ins_go_as_people_sign_in() -> None:
    at = world()
    opened = await complete(at, await begin(at))
    stale = await begin(at, OKTA)
    assert len(at.store.sessions) == 1
    assert len(at.store.pending_logins) == 1

    at.clock.advance(timedelta(hours=13))
    fresh = await begin(at)

    # Both rows, named: with either sweep gone from ``sweep`` this fails.
    assert secret_hash(opened.secret) not in at.store.sessions
    assert secret_hash(stale.state) not in at.store.pending_logins
    assert list(at.store.pending_logins) == [secret_hash(fresh.state)]
    assert await at.sign_in.resolve_session(opened.secret) is None


@asyncio_test
async def test_sweeping_happens_at_most_once_a_minute() -> None:
    # Ten-second sessions, so that one can expire between two sweeps.
    at = world(SignInConfig(**{**CONFIG_FIELDS, "session_hours": 10 / 3600}))
    opened = await complete(at, await begin(at))
    kept = secret_hash(opened.secret)

    at.clock.advance(30)
    await begin(at)

    # The session has expired and the sweep was not due: the row is still
    # there, and is nobody's session all the same.
    assert kept in at.store.sessions
    assert await at.sign_in.resolve_session(opened.secret) is None

    at.clock.advance(SWEEP_SECONDS)
    await begin(at)
    assert kept not in at.store.sessions


@asyncio_test
async def test_a_store_that_cannot_sweep_does_not_stop_a_sign_in() -> None:
    at = world()
    stale = await begin(at)
    at.clock.advance(PENDING_LOGIN_LIFE)
    at.store.sweep_error = RuntimeError("canceling statement due to statement timeout")

    begun = await begin(at)

    # The sign-in went through; the sweep did not, and says so.
    assert begun.authorization_url
    assert at.sign_in.sweep_failures == 1
    assert isinstance(at.sign_in.last_sweep_failure, RuntimeError)
    assert secret_hash(stale.state) in at.store.pending_logins

    # And the failed sweep bought the store no quiet minute: the very next
    # sign-in tries again, though the clock has not moved.
    at.store.sweep_error = None
    await begin(at)
    assert secret_hash(stale.state) not in at.store.pending_logins
    assert at.sign_in.sweep_failures == 1


@asyncio_test
async def test_the_sweep_interval_is_measured_on_the_monotonic_clock() -> None:
    at = world()
    stale = await begin(at)

    # An hour on the wall clock alone -- an operator, or NTP -- and none on
    # the monotonic one: nothing has happened, so nothing is swept.
    at.clock.set(at.clock.now() + timedelta(hours=1))
    await begin(at)

    assert secret_hash(stale.state) in at.store.pending_logins


# Secrets.


@asyncio_test
async def test_no_secret_a_browser_carries_reaches_a_stored_row() -> None:
    # Over every field of every row, not a repr of them: a repr hides the
    # fields a record chose to hide, which is where a secret would be.
    at = world()
    begun = await begin(at, return_to="/#/chat/7")

    # While the sign-in is still in progress.
    held = at.store.everything()
    assert list(at.store.pending_logins) == [secret_hash(begun.state)]
    assert begun.state not in held
    assert secret_hash(begun.state) in held

    opened = await complete(at, begun)

    # And once it is a session.
    held = at.store.everything()
    assert list(at.store.sessions) == [secret_hash(opened.secret)]
    assert begun.state not in held
    assert opened.secret not in held
    assert secret_hash(opened.secret) in held


@asyncio_test
async def test_the_nonce_and_the_verifier_are_in_the_row_on_purpose() -> None:
    # The port says so: the callback compares them with what the provider
    # sent, and a hash cannot be compared with something it has not seen.
    # What makes the row worthless is that nothing can find it -- the state
    # it is stored under was never stored.
    at = world()
    begun = await begin(at)
    login = stored_login(at, begun)

    held = at.store.everything()

    assert login.nonce in held
    assert login.verifier in held
    assert login.nonce != begun.state
    assert login.verifier != begun.state


@asyncio_test
async def test_a_record_that_carries_a_secret_does_not_print_it() -> None:
    at = world()
    begun = await begin(at, return_to="/#/chat/7")
    login = stored_login(at, begun)
    opened = await complete(at, begun)

    # The authorization URL carries the state and the nonce in its query, so
    # it is out of the repr along with them.
    assert begun.state not in repr(begun)
    assert login.nonce not in repr(begun)
    assert opened.secret not in repr(opened)
    assert login.nonce not in repr(login)
    assert login.verifier not in repr(login)


# What the service refuses to be built as.


@pytest.mark.parametrize("changes", [{"max_pending_logins": 0}, {"sweep_seconds": -1.0}])
def test_a_sign_in_service_is_not_built_to_refuse_everyone(changes: dict[str, Any]) -> None:
    with pytest.raises(InvalidValueError):
        world(**changes)
