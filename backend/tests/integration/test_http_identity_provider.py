# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The OIDC adapter against a real provider on a loopback port.

Marked ``io``: every test here opens a socket. No fixed port is used anywhere
-- the stand-in binds to port 0 and the operating system picks -- and nothing
sleeps: the one test that needs a request to be in flight holds it on an event
the test sets afterwards.

"Google-like" and "Okta-like" mean what they can mean here. Google's own rules
-- ``hd``, ``email_verified``, the bare issuer -- are ``core``'s, decided by
issuer and tested where they live; a provider on ``127.0.0.1`` is Google to
nobody. What differs at *this* layer is what the adapter actually does
differently: the scopes asked for, the groups claim, and above all which of
the two client authentication methods is used.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import ssl
import time
import traceback
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit

import httpx
import pytest

from aio import asyncio_test
from robinauts.legacy.adapters import (
    DISCOVERY_PATH,
    RETRYABLE_STATUSES,
    USER_AGENT,
    HttpIdentityProvider,
    identity_provider,
    open_client,
    ssl_context,
)
from robinauts.legacy.core import pkce_challenge
from robinauts.legacy.domain import (
    ProviderConfig,
    ProviderUnavailableError,
    SignInError,
    SignInErrorCode,
)
from standin import Misbehaviour, StandInProvider, redirect_from

pytestmark = pytest.mark.io

SECRET_ENV = "ROBINAUTS_STAND_IN_SECRET"

AWKWARD_SECRET = "s:e c&r=e+t%/? 秘密"
"""A client secret holding everything form-encoding exists for.

A colon, because ``client_secret_basic`` joins the two halves with one; a
space, an ampersand, an equals sign, a percent and a slash, because each means
something in a form; and two characters outside ASCII, because the encoding is
of UTF-8 bytes. A provider really can issue one of these.
"""

REDIRECT_URI = "https://robinauts.example.com/auth/callback/okta"


@pytest.fixture
def stand_in() -> Iterator[StandInProvider]:
    """A provider of our own, on a port the operating system chose."""
    with StandInProvider(client_secret=AWKWARD_SECRET) as provider:
        yield provider


def google_like(stand_in: StandInProvider) -> ProviderConfig:
    """Default scopes, no groups claim, ``client_secret_basic``."""
    return ProviderConfig(
        id="google",
        title="Google",
        issuer=stand_in.issuer,
        client_id=stand_in.client_id,
        client_secret_env=SECRET_ENV,
    )


def okta_like(stand_in: StandInProvider) -> ProviderConfig:
    """Groups asked for and read, and ``client_secret_post``."""
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


KINDS = {"google-like": google_like, "okta-like": okta_like}


def adapter_for(
    stand_in: StandInProvider,
    *,
    secret: str | None = None,
    total_timeout: float = 10.0,
    read_timeout: float = 10.0,
    max_response_bytes: int = 256 * 1024,
) -> HttpIdentityProvider:
    """An adapter whose environment holds exactly what this test says it does.

    ``secret_for`` is a dictionary's ``get``: no variable of the real process
    is set or read, so nothing here depends on the environment a developer
    happens to have, and two tests never see each other's secret.
    """
    held = {SECRET_ENV: stand_in.client_secret if secret is None else secret}
    return HttpIdentityProvider(
        secret_for=held.get,
        read_timeout=read_timeout,
        total_timeout=total_timeout,
        max_response_bytes=max_response_bytes,
        trust_env=False,
    )


VERIFIER = "the-pkce-verifier-nothing-may-ever-repeat"


async def signed_in_code(stand_in: StandInProvider, provider: ProviderConfig) -> str:
    """Walk the browser's half of the flow: authorize, and come back with a code.

    Only ``core.pkce_challenge`` is borrowed, because the stand-in computes
    the challenge for itself and the two have to agree about something.
    """
    query = urlencode(
        {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": REDIRECT_URI,
            "state": "state-1",
            "nonce": "nonce-1",
            "code_challenge": pkce_challenge(VERIFIER),
            "code_challenge_method": "S256",
        }
    )
    location = await redirect_from(f"{stand_in.issuer}/authorize?{query}")
    return parse_qs(urlsplit(location).query)["code"][0]


# The two things the adapter does, working.


@pytest.mark.parametrize("kind", sorted(KINDS))
@asyncio_test
async def test_discovery_comes_back_as_the_provider_published_it(
    stand_in: StandInProvider, kind: str
) -> None:
    adapter = adapter_for(stand_in)
    try:
        document = await adapter.discovery_document(KINDS[kind](stand_in))
    finally:
        await adapter.aclose()

    assert document["issuer"] == stand_in.issuer
    assert document["token_endpoint"] == f"{stand_in.issuer}/token"
    assert stand_in.paths == [DISCOVERY_PATH]


@pytest.mark.parametrize("kind", sorted(KINDS))
@asyncio_test
async def test_a_code_is_exchanged_for_a_token_response(
    stand_in: StandInProvider, kind: str
) -> None:
    provider = KINDS[kind](stand_in)
    adapter = adapter_for(stand_in)
    try:
        document = await adapter.discovery_document(provider)
        code = await signed_in_code(stand_in, provider)
        response = await adapter.exchange_code(
            provider,
            token_endpoint=document["token_endpoint"],
            code=code,
            verifier=VERIFIER,
            redirect_uri=REDIRECT_URI,
        )
    finally:
        await adapter.aclose()

    assert response["token_type"] == "Bearer"
    assert isinstance(response["id_token"], str)
    posted = stand_in.exchanged[-1]
    assert posted["grant_type"] == "authorization_code"
    assert posted["code"] == code
    assert posted["code_verifier"] == VERIFIER
    assert posted["redirect_uri"] == REDIRECT_URI


@asyncio_test
async def test_client_secret_basic_form_encodes_each_half_before_joining_them(
    stand_in: StandInProvider,
) -> None:
    # RFC 6749 2.3.1. Without it, a secret holding a colon would be split in
    # the middle by whatever server reads the header.
    provider = google_like(stand_in)
    assert ":" in stand_in.client_secret

    adapter = adapter_for(stand_in)
    try:
        document = await adapter.discovery_document(provider)
        code = await signed_in_code(stand_in, provider)
        await adapter.exchange_code(
            provider,
            token_endpoint=document["token_endpoint"],
            code=code,
            verifier=VERIFIER,
            redirect_uri=REDIRECT_URI,
        )
    finally:
        await adapter.aclose()

    posted = stand_in.exchanged[-1]
    assert posted["authorization"].startswith("Basic ")
    # The provider accepted it, which is the real assertion; and nothing of
    # the secret travelled in the form.
    assert "client_secret" not in posted
    assert "client_id" not in posted


@asyncio_test
async def test_client_secret_post_sends_the_credentials_in_the_form(
    stand_in: StandInProvider,
) -> None:
    provider = okta_like(stand_in)

    adapter = adapter_for(stand_in)
    try:
        document = await adapter.discovery_document(provider)
        code = await signed_in_code(stand_in, provider)
        await adapter.exchange_code(
            provider,
            token_endpoint=document["token_endpoint"],
            code=code,
            verifier=VERIFIER,
            redirect_uri=REDIRECT_URI,
        )
    finally:
        await adapter.aclose()

    posted = stand_in.exchanged[-1]
    assert posted["authorization"] == ""
    assert posted["client_id"] == stand_in.client_id
    assert posted["client_secret"] == stand_in.client_secret


@asyncio_test
async def test_a_wrong_secret_is_the_provider_refusing(stand_in: StandInProvider) -> None:
    provider = google_like(stand_in)

    adapter = adapter_for(stand_in, secret="not-the-secret")
    try:
        document = await adapter.discovery_document(provider)
        code = await signed_in_code(stand_in, provider)
        with pytest.raises(SignInError) as raised:
            await adapter.exchange_code(
                provider,
                token_endpoint=document["token_endpoint"],
                code=code,
                verifier=VERIFIER,
                redirect_uri=REDIRECT_URI,
            )
    finally:
        await adapter.aclose()

    assert raised.value.code is SignInErrorCode.PROVIDER_REFUSED
    assert "invalid_client" in raised.value.detail


@asyncio_test
async def test_the_adapter_judges_nothing_it_is_told(stand_in: StandInProvider) -> None:
    # An adapter may not import `core`, so it has no opinion: a document
    # naming somebody else's issuer and no endpoints comes back as it is, and
    # the application is what refuses it.
    stand_in.discovery = {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": None,
        "token_endpoint": None,
    }

    adapter = adapter_for(stand_in)
    try:
        document = await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert document["issuer"] == "https://accounts.google.com"
    assert "token_endpoint" not in document


@asyncio_test
async def test_the_request_says_what_it_is_and_what_it_accepts(
    stand_in: StandInProvider,
) -> None:
    adapter = adapter_for(stand_in)
    try:
        await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    # Read off the server, not off the client: what matters is what arrived.
    (request,) = stand_in.received
    assert request.method == "GET"
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == USER_AGENT
    # Honest: it names the program and where to read it, and claims to be no
    # browser and no version of anything.
    assert "Mozilla" not in USER_AGENT


# Everything that can go wrong with discovery is "unavailable".


@asyncio_test
async def test_a_provider_that_is_not_listening_is_unavailable(
    stand_in: StandInProvider,
) -> None:
    provider = google_like(stand_in)
    stand_in.close()

    adapter = adapter_for(stand_in)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(provider)
    finally:
        await adapter.aclose()

    assert "discovery for google" in raised.value.detail


@pytest.mark.parametrize(
    ("how", "expected"),
    [
        (Misbehaviour.server_error(), "answered 500"),
        (Misbehaviour.server_error(503), "answered 503"),
        (Misbehaviour.oauth_error(status=404), "answered 404"),
        (Misbehaviour.not_json(), "not JSON"),
        (Misbehaviour.redirect("/token"), "answered 302"),
    ],
    ids=["500", "503", "404", "html", "redirect"],
)
@asyncio_test
async def test_discovery_answering_badly_is_unavailable(
    stand_in: StandInProvider, how: Misbehaviour, expected: str
) -> None:
    stand_in.discovery_misbehaves(how)

    adapter = adapter_for(stand_in)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert expected in raised.value.detail


@asyncio_test
async def test_json_nested_past_the_recursion_limit_is_unavailable(
    stand_in: StandInProvider,
) -> None:
    # Forty kilobytes, well inside any size bound, and `json` gives up with a
    # RecursionError rather than a ValueError. Uncaught it would leave this
    # adapter by neither of the port's two doors.
    stand_in.discovery_misbehaves(Misbehaviour.deeply_nested(20_000))

    adapter = adapter_for(stand_in)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert "nested deeper" in raised.value.detail


@asyncio_test
async def test_json_nested_past_the_recursion_limit_at_the_token_endpoint(
    stand_in: StandInProvider,
) -> None:
    adapter = adapter_for(stand_in)
    try:
        stand_in.token_misbehaves(Misbehaviour.deeply_nested(20_000))
        with pytest.raises(ProviderUnavailableError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    assert "nested deeper" in raised.value.detail


@asyncio_test
async def test_a_deeply_nested_4xx_body_is_still_a_plain_refusal(
    stand_in: StandInProvider,
) -> None:
    # The refusal's detail parses the body too, and must give up as quietly.
    adapter = adapter_for(stand_in)
    try:
        stand_in.token_misbehaves(Misbehaviour(status=400, body=b"[" * 20_000 + b"]" * 20_000))
        with pytest.raises(SignInError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    assert raised.value.code is SignInErrorCode.PROVIDER_REFUSED
    assert "nothing that reads as an OAuth error" in raised.value.detail


@asyncio_test
async def test_a_json_array_is_not_a_discovery_document(stand_in: StandInProvider) -> None:
    stand_in.discovery_misbehaves(Misbehaviour(body=b'["not", "a", "document"]'))

    adapter = adapter_for(stand_in)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert "not a JSON object" in raised.value.detail


@asyncio_test
async def test_a_redirect_is_never_followed(stand_in: StandInProvider) -> None:
    # The strong form of the assertion: not "the client says it did not
    # follow it" but "the server it pointed at was never asked". A token
    # endpoint that redirects is choosing where an authorization code goes.
    stand_in.discovery_misbehaves(Misbehaviour.redirect(f"{stand_in.issuer}/token"))

    adapter = adapter_for(stand_in)
    try:
        with pytest.raises(ProviderUnavailableError):
            await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert stand_in.paths == [DISCOVERY_PATH]


@asyncio_test
async def test_an_oversized_answer_is_refused(stand_in: StandInProvider) -> None:
    # It declares its length, so it is refused on the header alone; the
    # chunked case is the one that has to be counted as it arrives.
    stand_in.discovery_misbehaves(Misbehaviour.oversized(8 * 1024))

    adapter = adapter_for(stand_in, max_response_bytes=1024)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert "declared 8207 bytes, over the 1024" in raised.value.detail


@asyncio_test
async def test_every_request_asks_for_an_uncompressed_answer(
    stand_in: StandInProvider,
) -> None:
    adapter = adapter_for(stand_in)
    try:
        await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    # Every request the adapter makes, not only the first: the bound is on
    # bytes off the wire, and asking for an encoding would make it a bound on
    # something else. (`/authorize` is the test playing browser, with a plain
    # client of its own; it is not this adapter's.)
    ours = [request for request in stand_in.received if request.path != "/authorize"]
    assert [request.path for request in ours] == [DISCOVERY_PATH, "/token"]
    assert [request.headers["accept-encoding"] for request in ours] == ["identity"] * 2


@asyncio_test
async def test_a_compressed_answer_is_refused_rather_than_inflated(
    stand_in: StandInProvider,
) -> None:
    # Sixty-four kilobytes of padding is a few hundred bytes of gzip. A reader
    # that inflated first and counted afterwards would be over a 1 KiB bound
    # by sixty-three kilobytes before it noticed, and a real bomb is worse by
    # a factor of a thousand.
    stand_in.discovery_misbehaves(Misbehaviour.gzip_bomb(64 * 1024))

    adapter = adapter_for(stand_in, max_response_bytes=1024)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert "content-encoding" in raised.value.detail
    assert "gzip" in raised.value.detail


@asyncio_test
async def test_a_compressed_answer_is_refused_even_when_it_would_fit(
    stand_in: StandInProvider,
) -> None:
    # The refusal is of the encoding, not of the size: a reader that only
    # refused big ones would still be inflating whatever it was sent.
    stand_in.discovery_misbehaves(Misbehaviour.gzip_bomb(16))

    adapter = adapter_for(stand_in)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert "content-encoding" in raised.value.detail


@asyncio_test
async def test_a_declared_length_over_the_bound_is_refused_unread(
    stand_in: StandInProvider,
) -> None:
    # It says ten megabytes and sends fifteen bytes. Believing the header
    # costs nothing; reading on to find out would be the whole point of the
    # bound given away.
    stand_in.discovery_misbehaves(Misbehaviour.overstated(10 * 1024 * 1024))

    adapter = adapter_for(stand_in, max_response_bytes=1024)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert "declared 10485760 bytes" in raised.value.detail


@asyncio_test
async def test_an_answer_that_declares_no_length_is_counted_as_it_arrives(
    stand_in: StandInProvider,
) -> None:
    # Chunked: there is no header to believe, so the only bound is the count.
    stand_in.discovery_misbehaves(Misbehaviour.oversized(8 * 1024, chunked=True))

    adapter = adapter_for(stand_in, max_response_bytes=1024)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        await adapter.aclose()

    assert "more than the 1024 bytes" in raised.value.detail


@asyncio_test
async def test_a_provider_that_never_answers_hits_the_whole_exchange_ceiling(
    stand_in: StandInProvider,
) -> None:
    # The socket is open and quiet, which is what the read timeout is for;
    # here the ceiling on the whole exchange is the shorter of the two, and
    # it is what ends it.
    stand_in.discovery_misbehaves(Misbehaviour.slow())

    adapter = adapter_for(stand_in, total_timeout=0.25, read_timeout=30.0)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        stand_in.release()
        await adapter.aclose()

    assert "took longer than 0.25s" in raised.value.detail


@asyncio_test
async def test_a_quiet_socket_hits_the_read_timeout(stand_in: StandInProvider) -> None:
    stand_in.discovery_misbehaves(Misbehaviour.slow())

    adapter = adapter_for(stand_in, total_timeout=30.0, read_timeout=0.25)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(google_like(stand_in))
    finally:
        stand_in.release()
        await adapter.aclose()

    # The client's own type, never its message: a failed request carries the
    # request.
    assert "raised ReadTimeout" in raised.value.detail


# And at the token endpoint, where "reached and refused" is its own answer.


async def exchanging(
    adapter: HttpIdentityProvider, stand_in: StandInProvider, provider: ProviderConfig
) -> dict[str, Any]:
    """Discover, authorize and exchange; whatever the exchange gives back."""
    document = await adapter.discovery_document(provider)
    code = await signed_in_code(stand_in, provider)
    return dict(
        await adapter.exchange_code(
            provider,
            token_endpoint=document["token_endpoint"],
            code=code,
            verifier=VERIFIER,
            redirect_uri=REDIRECT_URI,
        )
    )


@pytest.mark.parametrize(
    ("how", "expected"),
    [
        (Misbehaviour.server_error(), "answered 500"),
        (Misbehaviour.server_error(502), "answered 502"),
        (Misbehaviour.not_json(), "not JSON"),
        (Misbehaviour.redirect("/token"), "answered 302, not 200"),
    ],
    ids=["500", "502", "html", "redirect"],
)
@asyncio_test
async def test_a_token_endpoint_answering_badly_is_unavailable(
    stand_in: StandInProvider, how: Misbehaviour, expected: str
) -> None:
    adapter = adapter_for(stand_in)
    try:
        stand_in.token_misbehaves(how)
        with pytest.raises(ProviderUnavailableError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    assert expected in raised.value.detail


@asyncio_test
async def test_an_oauth_error_is_the_provider_refusing(stand_in: StandInProvider) -> None:
    adapter = adapter_for(stand_in)
    try:
        stand_in.token_misbehaves(
            Misbehaviour.oauth_error("invalid_grant", description="code already used")
        )
        with pytest.raises(SignInError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    assert raised.value.code is SignInErrorCode.PROVIDER_REFUSED
    assert "invalid_grant" in raised.value.detail
    assert "code already used" in raised.value.detail


@asyncio_test
async def test_a_4xx_with_nothing_readable_in_it_is_still_a_refusal(
    stand_in: StandInProvider,
) -> None:
    # Reached, and said no. Telling the person to try again would be a lie.
    adapter = adapter_for(stand_in)
    try:
        stand_in.token_misbehaves(Misbehaviour(status=400, body=b"<html>no</html>"))
        with pytest.raises(SignInError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    assert raised.value.code is SignInErrorCode.PROVIDER_REFUSED
    assert "nothing that reads as an OAuth error" in raised.value.detail


def test_the_statuses_that_mean_ask_again_are_the_three_of_them() -> None:
    # Written out rather than derived, so that emptying the constant fails a
    # test instead of quietly parametrizing the one below into nothing.
    assert RETRYABLE_STATUSES == {408, 425, 429}


@pytest.mark.parametrize("status", [408, 425, 429])
@asyncio_test
async def test_a_4xx_that_means_ask_again_is_unavailable_not_refused(
    stand_in: StandInProvider, status: int
) -> None:
    # 408, 425 and 429 are the provider saying it did not deal with this
    # request. "provider_refused" would send the person off to fix an account
    # that is fine, and would stop the application retrying anything.
    adapter = adapter_for(stand_in)
    try:
        stand_in.token_misbehaves(Misbehaviour.oauth_error("slow_down", status=status))
        with pytest.raises(ProviderUnavailableError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    assert raised.value.code is SignInErrorCode.PROVIDER_UNAVAILABLE
    assert f"answered {status}" in raised.value.detail


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
@asyncio_test
async def test_every_other_4xx_is_still_a_refusal(stand_in: StandInProvider, status: int) -> None:
    adapter = adapter_for(stand_in)
    try:
        stand_in.token_misbehaves(Misbehaviour.oauth_error(status=status))
        with pytest.raises(SignInError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    assert raised.value.code is SignInErrorCode.PROVIDER_REFUSED


@asyncio_test
async def test_an_oauth_error_with_a_200_is_a_refusal_too(stand_in: StandInProvider) -> None:
    adapter = adapter_for(stand_in)
    try:
        stand_in.token_misbehaves(Misbehaviour.oauth_error("invalid_request", status=200))
        with pytest.raises(SignInError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    assert raised.value.code is SignInErrorCode.PROVIDER_REFUSED
    assert "invalid_request" in raised.value.detail


@asyncio_test
async def test_the_token_endpoint_is_bounded_too(stand_in: StandInProvider) -> None:
    adapter = adapter_for(stand_in, max_response_bytes=1024)
    try:
        stand_in.token_misbehaves(Misbehaviour.oversized(8 * 1024, chunked=True))
        with pytest.raises(ProviderUnavailableError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    assert "more than the 1024 bytes" in raised.value.detail


@asyncio_test
async def test_a_token_endpoint_redirect_is_never_followed(stand_in: StandInProvider) -> None:
    adapter = adapter_for(stand_in)
    try:
        stand_in.token_misbehaves(Misbehaviour.redirect(f"{stand_in.issuer}{DISCOVERY_PATH}"))
        with pytest.raises(ProviderUnavailableError):
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        await adapter.aclose()

    # Discovery once at the start, authorize, the token endpoint -- and never
    # the place the token endpoint pointed at.
    assert stand_in.paths == [DISCOVERY_PATH, "/authorize", "/token"]


@asyncio_test
async def test_a_token_endpoint_that_never_answers_times_out(
    stand_in: StandInProvider,
) -> None:
    adapter = adapter_for(stand_in, total_timeout=0.25, read_timeout=30.0)
    try:
        stand_in.token_misbehaves(Misbehaviour.slow())
        with pytest.raises(ProviderUnavailableError) as raised:
            await exchanging(adapter, stand_in, google_like(stand_in))
    finally:
        stand_in.release()
        await adapter.aclose()

    assert "took longer than 0.25s" in raised.value.detail


# The secret: where it is read, and everywhere it must never turn up.


@asyncio_test
async def test_a_secret_that_is_not_in_the_environment_names_the_variable(
    stand_in: StandInProvider,
) -> None:
    provider = google_like(stand_in)

    adapter = HttpIdentityProvider(secret_for=lambda name: None, trust_env=False)
    try:
        document = await adapter.discovery_document(provider)
        code = await signed_in_code(stand_in, provider)
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.exchange_code(
                provider,
                token_endpoint=document["token_endpoint"],
                code=code,
                verifier=VERIFIER,
                redirect_uri=REDIRECT_URI,
            )
    finally:
        await adapter.aclose()

    assert SECRET_ENV in raised.value.detail
    # Nothing was posted: the exchange stopped before the request.
    assert stand_in.exchanged == []


@asyncio_test
async def test_the_secret_is_read_again_at_every_exchange(stand_in: StandInProvider) -> None:
    # Read where it is used, not held on the object: an operator who fixes an
    # unset variable does not have to restart the process.
    provider = google_like(stand_in)
    held: dict[str, str] = {}

    adapter = HttpIdentityProvider(secret_for=held.get, trust_env=False)
    try:
        document = await adapter.discovery_document(provider)
        code = await signed_in_code(stand_in, provider)
        with pytest.raises(ProviderUnavailableError):
            await adapter.exchange_code(
                provider,
                token_endpoint=document["token_endpoint"],
                code=code,
                verifier=VERIFIER,
                redirect_uri=REDIRECT_URI,
            )
        held[SECRET_ENV] = stand_in.client_secret
        response = await adapter.exchange_code(
            provider,
            token_endpoint=document["token_endpoint"],
            code=code,
            verifier=VERIFIER,
            redirect_uri=REDIRECT_URI,
        )
    finally:
        await adapter.aclose()

    assert "id_token" in response


@asyncio_test
async def test_the_secret_is_in_no_repr(stand_in: StandInProvider) -> None:
    adapter = adapter_for(stand_in)
    try:
        assert stand_in.client_secret not in repr(adapter)
        assert stand_in.client_secret not in repr(vars(adapter))
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "how",
    [
        Misbehaviour.server_error(),
        Misbehaviour.not_json(),
        Misbehaviour.oauth_error(),
        Misbehaviour.slow(),
        Misbehaviour.deeply_nested(20_000),
        Misbehaviour.gzip_bomb(64 * 1024),
        Misbehaviour.oversized(8 * 1024, chunked=True),
        Misbehaviour.redirect("/elsewhere"),
    ],
    ids=["500", "html", "oauth-error", "timeout", "deep", "gzip", "oversize", "redirect"],
)
@asyncio_test
async def test_no_failure_of_the_exchange_can_print_the_secret(
    stand_in: StandInProvider, how: Misbehaviour
) -> None:
    # The client secret, the authorization code and the PKCE verifier are in
    # the body of the request, so they are in every frame that built or sent
    # it and in the `Request` an httpx exception holds. An error reporter
    # captures locals and walks `__cause__` and `__context__` without caring
    # what `__suppress_context__` says, so this looks where one would look.
    provider = google_like(stand_in)
    adapter = adapter_for(stand_in, total_timeout=0.25, read_timeout=0.25, max_response_bytes=2048)
    try:
        document = await adapter.discovery_document(provider)
        code = await signed_in_code(stand_in, provider)
        stand_in.token_misbehaves(how)
        with pytest.raises(SignInError) as raised:
            await adapter.exchange_code(
                provider,
                token_endpoint=document["token_endpoint"],
                code=code,
                verifier=VERIFIER,
                redirect_uri=REDIRECT_URI,
            )
    finally:
        stand_in.release()
        await adapter.aclose()

    error = raised.value
    printed = _printed(error)
    for text in (str(error), repr(error), error.detail, printed):
        for kept in (stand_in.client_secret, VERIFIER, code, REDIRECT_URI):
            assert kept not in text, text
    # Nothing is reachable at all: not through the cause, and not through the
    # context that `raise ... from None` would have left pointing at httpx.
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not _frames_holding(error, stand_in.client_secret, VERIFIER, code)


def _printed(error: BaseException) -> str:
    """The error as a reporter that captures locals would print it.

    This module's own frames are dropped first: they are the test, standing in
    for the browser, and they hold the code and the verifier because somebody
    has to.
    """
    report = traceback.TracebackException.from_exception(error, capture_locals=True)
    seen = report
    while seen is not None:
        seen.stack[:] = [frame for frame in seen.stack if frame.filename != __file__]
        seen = seen.__cause__ or seen.__context__
    return "".join(report.format())


def _frames_holding(error: BaseException, *secrets: str) -> list[str]:
    """Every frame of every exception reachable from ``error`` that holds one.

    Walked by hand rather than trusted to the formatter: a frame's locals are
    what an error reporter reads, and a traceback that prints nothing may
    still be carrying them.
    """
    found: list[str] = []
    seen: set[int] = set()
    errors: list[BaseException | None] = [error]
    while errors:
        current = errors.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        errors += [current.__cause__, current.__context__]
        trace = current.__traceback__
        while trace is not None:
            frame = trace.tb_frame
            trace = trace.tb_next
            # This module's own frames are the test, which of course holds the
            # code and the verifier: it is playing the browser. The question
            # is what the adapter's frames hold.
            if frame.f_code.co_filename == __file__:
                continue
            for name, value in frame.f_locals.items():
                if any(secret in _exposed(value) for secret in secrets):
                    found.append(f"{frame.f_code.co_qualname}.{name}")
    return found


def _exposed(value: object) -> str:
    """Everything whoever holds this local could read out of it, not its repr alone.

    An ``httpx.Request``'s repr is the method and the URL and nothing else, so
    a frame holding one prints clean while the body and the headers are a
    single attribute away from anybody with the traceback. That is the leak,
    so that is what is looked at.
    """
    texts = [_repr(value)]
    request = value if isinstance(value, httpx.Request) else getattr(value, "request", None)
    if isinstance(request, httpx.Request):
        texts.append(_unpacked(request))
    return "\n".join(texts)


def _unpacked(request: httpx.Request) -> str:
    """A request read the way somebody holding it would read it.

    Form-encoding and base64 do not hide a secret, they spell it differently:
    a check that looked only for the literal text would pass on a request
    carrying ``client_secret=s%3Ae+c%26r%3De%2Bt``.
    """
    parts = [str(request.headers), str(request.url)]
    try:
        body = request.content.decode("utf-8", "replace")
    except Exception:  # pragma: no cover -- a streaming request, which we make none of
        body = ""
    parts.append(body)
    for values in parse_qs(body, keep_blank_values=True).values():
        parts += values
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        with suppress(ValueError, UnicodeDecodeError):
            pair = base64.b64decode(header[len("Basic ") :]).decode("utf-8")
            parts += [pair, unquote_plus(pair)]
    return "\n".join(parts)


def _repr(value: object) -> str:
    """A value as an error reporter would print it, however badly it behaves."""
    try:
        return repr(value)
    except Exception:  # pragma: no cover -- nothing here lacks a repr
        return ""


async def until(condition: Any, *, what: str, seconds: float = 5.0) -> None:
    """Wait for something a handler thread will do, and say so if it does not.

    A poll on a real condition, not a wait of a guessed length: it ends the
    moment the condition holds, and fails loudly rather than quietly passing
    if it never does.
    """
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen within {seconds}s")
        await asyncio.sleep(0.005)


@asyncio_test
async def test_a_cancelled_exchange_carries_no_frame_of_the_request(
    stand_in: StandInProvider,
) -> None:
    # The browser goes away mid-sign-in and the request handler's task is
    # cancelled. A CancelledError is not a provider failing, so it passes
    # through as itself -- but the frames it was raised in are the HTTP
    # client's, and every one of them holds the Request: the Authorization
    # header, the code and the verifier. Those frames are what is dropped.
    provider = google_like(stand_in)
    adapter = adapter_for(stand_in, total_timeout=30.0, read_timeout=30.0)
    try:
        document = await adapter.discovery_document(provider)
        code = await signed_in_code(stand_in, provider)
        stand_in.token_misbehaves(Misbehaviour.slow())
        exchange = asyncio.ensure_future(
            adapter.exchange_code(
                provider,
                token_endpoint=document["token_endpoint"],
                code=code,
                verifier=VERIFIER,
                redirect_uri=REDIRECT_URI,
            )
        )
        await until(
            lambda: "/token" in stand_in.paths, what="the token request to reach the provider"
        )
        exchange.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await exchange
    finally:
        stand_in.release()
        await adapter.aclose()

    # Still a cancellation, not a sign-in error dressed up as one.
    assert isinstance(raised.value, asyncio.CancelledError)
    assert not isinstance(raised.value, SignInError)
    assert not _frames_holding(raised.value, stand_in.client_secret, VERIFIER, code)
    assert stand_in.client_secret not in _printed(raised.value)


@asyncio_test
async def test_an_exchange_on_a_closed_client_carries_no_frame_of_the_request(
    stand_in: StandInProvider,
) -> None:
    # Using a closed client is a bug to see, so the RuntimeError is not turned
    # into a provider failure -- and httpx raises it from a frame that already
    # holds the built Request.
    provider = google_like(stand_in)
    adapter = adapter_for(stand_in)
    document = await adapter.discovery_document(provider)
    code = await signed_in_code(stand_in, provider)
    await adapter.aclose()

    with pytest.raises(RuntimeError) as raised:
        await adapter.exchange_code(
            provider,
            token_endpoint=document["token_endpoint"],
            code=code,
            verifier=VERIFIER,
            redirect_uri=REDIRECT_URI,
        )

    assert not isinstance(raised.value, SignInError)
    assert not _frames_holding(raised.value, stand_in.client_secret, VERIFIER, code)
    assert stand_in.client_secret not in _printed(raised.value)


@asyncio_test
async def test_a_failure_of_discovery_prints_nothing_of_the_exchange(
    stand_in: StandInProvider,
) -> None:
    # Discovery keeps its cause, so this is the other half of the same check:
    # the exception it keeps must itself carry nothing from a code exchange.
    provider = google_like(stand_in)
    adapter = adapter_for(stand_in)
    try:
        document = await adapter.discovery_document(provider)
        code = await signed_in_code(stand_in, provider)
        await adapter.exchange_code(
            provider,
            token_endpoint=document["token_endpoint"],
            code=code,
            verifier=VERIFIER,
            redirect_uri=REDIRECT_URI,
        )
        stand_in.discovery_misbehaves(Misbehaviour.not_json())
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(provider)
    finally:
        await adapter.aclose()

    assert not _frames_holding(raised.value, stand_in.client_secret, VERIFIER, code)


@asyncio_test
async def test_discovery_keeps_its_cause_because_it_carries_no_secret(
    stand_in: StandInProvider,
) -> None:
    # The other half of the rule: a discovery request has no credential in
    # it, so the client's own exception is worth keeping for the operator.
    provider = google_like(stand_in)
    stand_in.close()

    adapter = adapter_for(stand_in)
    try:
        with pytest.raises(ProviderUnavailableError) as raised:
            await adapter.discovery_document(provider)
    finally:
        await adapter.aclose()

    assert isinstance(raised.value.__cause__, httpx.HTTPError | OSError)
    # And the cause has a traceback, whose top frame is the adapter's own --
    # which is why the adapter drops the request and the response out of it
    # rather than leaving them for whoever reads the cause.
    assert _adapter_frames(raised.value.__cause__)
    assert _adapter_frames_holding_a_request(raised.value.__cause__) == []


def _adapter_frames(error: BaseException) -> list[str]:
    """The adapter's own frames in this exception's traceback, in order."""
    names = []
    trace = error.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_filename == identity_provider.__file__:
            names.append(trace.tb_frame.f_code.co_qualname)
        trace = trace.tb_next
    return names


def _adapter_frames_holding_a_request(error: BaseException) -> list[str]:
    """Adapter frames still holding a request, or anything pointing at one.

    The adapter cannot clear the HTTP client's frames without dropping the
    traceback; its own it can, and must, because a kept cause keeps them.
    """
    found = []
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        trace = trace.tb_next
        if frame.f_code.co_filename != identity_provider.__file__:
            continue
        for name, value in frame.f_locals.items():
            if isinstance(value, httpx.Request | httpx.Response):
                found.append(f"{frame.f_code.co_qualname}.{name}")
    return found


# The client, and the TLS it is made with.


def test_tls_is_verified_and_the_host_is_checked() -> None:
    context = ssl_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_there_is_no_way_to_ask_for_less() -> None:
    # An ID token's signature is not checked, so the token is worth exactly
    # the transport it came over. A knob here would be a deployment where
    # anyone on the path signs in as anyone.
    weakening = {"verify", "cert", "ssl", "ssl_context", "insecure", "cafile", "client"}
    for made in (open_client, ssl_context, HttpIdentityProvider):
        assert weakening.isdisjoint(inspect.signature(made).parameters), made


def test_the_adapter_makes_its_own_client() -> None:
    # `client` is deliberately not a parameter: one made with `verify=False`
    # is a keyword away, and an adapter whose TLS depended on what it was
    # handed would be promising something it cannot keep.
    taken = inspect.signature(HttpIdentityProvider).parameters
    assert set(taken) == {
        "secret_for",
        "connect_timeout",
        "read_timeout",
        "total_timeout",
        "max_response_bytes",
        "trust_env",
    }
    assert all(taken[name].kind is inspect.Parameter.KEYWORD_ONLY for name in taken)


def test_the_trust_store_follows_trust_env_and_verification_never_does() -> None:
    # `trust_env` decides where the CA bundle comes from and which proxy is
    # used -- never whether the certificate is checked.
    for trusting in (True, False):
        context = ssl_context(trust_env=trusting)

        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname is True


def test_a_rogue_ca_file_cannot_reach_a_context_that_does_not_trust_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SSL_CERT_FILE replaces the trust store wholesale. With trust_env off it
    # is not read at all, which is what makes `open_client(trust_env=False)`
    # a statement about the trust store and not only about proxies.
    rogue = tmp_path / "rogue.pem"
    rogue.write_text("not a certificate at all\n", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(rogue))

    # Unreadable as a bundle: a context that consults it cannot even be built.
    with pytest.raises(ssl.SSLError):
        ssl_context(trust_env=True)
    assert ssl_context(trust_env=False).verify_mode is ssl.CERT_REQUIRED


@asyncio_test
async def test_the_client_follows_no_redirect_of_its_own_accord() -> None:
    client = open_client()
    try:
        assert client.follow_redirects is False
    finally:
        await client.aclose()


@asyncio_test
async def test_closing_the_adapter_closes_the_client(stand_in: StandInProvider) -> None:
    adapter = adapter_for(stand_in)
    await adapter.discovery_document(google_like(stand_in))

    await adapter.aclose()

    # A closed client is a programming mistake to use, not a provider that
    # failed: it is not dressed up as one.
    with pytest.raises(RuntimeError):
        await adapter.discovery_document(google_like(stand_in))


@asyncio_test
async def test_one_client_serves_every_request(stand_in: StandInProvider) -> None:
    # The client's lifetime is the deployment's: a client per request would
    # throw away a connection and a TLS handshake each time.
    provider = google_like(stand_in)

    adapter = adapter_for(stand_in)
    try:
        for _ in range(3):
            await adapter.discovery_document(provider)
    finally:
        await adapter.aclose()

    assert stand_in.paths == [DISCOVERY_PATH] * 3
