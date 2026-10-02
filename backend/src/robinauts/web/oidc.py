# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The OpenID Connect exchange with an identity provider, over ``httpx``: discovery, the
authorization URL, the code exchange and the ID token's claims (``docs/specs/sign-in.md``,
"Providers").

The ID token's signature is not verified. The token is read out of the token endpoint's own
answer, over TLS, which OpenID Connect Core 3.1.3.7 allows for the code flow. A token taken
from anywhere else would need a JWT library and the provider's keys.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx

from robinauts.controller.composition import SecretLookup
from robinauts.web.sign_in import (
    GOOGLE_ISSUER,
    ProviderConfig,
    SignInConfig,
    SignInError,
    SignInErrorCode,
    is_loopback,
)

DISCOVERY_PATH = "/.well-known/openid-configuration"
CONNECT_SECONDS = 5.0
READ_SECONDS = 10.0
TOTAL_SECONDS = 20.0
"""The bound on one request, its body included: a provider that sends a byte every few
seconds passes the read timeout for as long as it likes."""
CLOCK_SKEW_SECONDS = 60


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class Claims:
    """Who the provider vouched for, as the allow list and ``ensure_user`` read it."""

    subject: str
    name: str | None
    email: str | None
    """Only an address the provider verified; at Google, one in a Workspace or at Gmail."""
    hosted_domain: str | None
    groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Endpoints:
    authorization: str
    token: str


class Exchange:
    """The two requests a sign-in makes of its provider, on a client of its own: timeouts,
    no redirect followed, TLS verified."""

    def __init__(self, config: SignInConfig, secret_for: SecretLookup) -> None:
        self._config = config
        self._secret_for = secret_for
        self._endpoints: dict[str, _Endpoints] = {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(READ_SECONDS, connect=CONNECT_SECONDS), follow_redirects=False
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def begin(self, provider_id: str, *, state: str, nonce: str, verifier: str) -> str:
        """The provider's authorization URL to send the browser to."""
        provider = self._provider(provider_id)
        endpoints = await self._discovered(provider)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": provider.client_id,
                "redirect_uri": self._redirect_uri(provider),
                "scope": " ".join(provider.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": s256(verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoints.authorization}?{query}"

    async def complete(
        self, provider_id: str, *, code: str, nonce: str, verifier: str, now: datetime
    ) -> Claims:
        """The checked claims of the ID token the code is exchanged for."""
        provider = self._provider(provider_id)
        endpoints = await self._discovered(provider)
        secret = self._secret_for(provider.client_secret_env)
        if not secret:
            raise SignInError(
                SignInErrorCode.PROVIDER_UNAVAILABLE,
                f"{provider.client_secret_env}, the client secret of {provider.id}, is not set",
            )
        # client_secret_basic form-encodes each half before joining them (RFC 6749 2.3.1).
        pair = f"{quote_plus(provider.client_id)}:{quote_plus(secret)}".encode()
        what = f"the token endpoint of {provider.id}"
        response = await self._send(
            "POST",
            endpoints.token,
            what,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri(provider),
                "code_verifier": verifier,
            },
            headers={"authorization": f"Basic {base64.b64encode(pair).decode('ascii')}"},
        )
        answer = _token_answer(response, what)
        return checked_claims(read_id_token(answer.get("id_token")), provider, nonce=nonce, now=now)

    def _provider(self, provider_id: str) -> ProviderConfig:
        provider = self._config.providers.get(provider_id)
        if provider is None:
            raise SignInError(
                SignInErrorCode.UNKNOWN_PROVIDER, f"no provider {provider_id!r} is configured"
            )
        return provider

    def _redirect_uri(self, provider: ProviderConfig) -> str:
        return f"{self._config.public_url}/auth/callback/{provider.id}"

    async def _discovered(self, provider: ProviderConfig) -> _Endpoints:
        if provider.id not in self._endpoints:
            what = f"discovery for {provider.id}"
            response = await self._send("GET", provider.issuer.rstrip("/") + DISCOVERY_PATH, what)
            document = _object(response) if response.status_code == 200 else None
            if document is None:
                raise SignInError(
                    SignInErrorCode.PROVIDER_UNAVAILABLE,
                    f"{what} answered {response.status_code} with no JSON object",
                )
            if document.get("issuer") != provider.issuer:
                raise SignInError(
                    SignInErrorCode.PROVIDER_UNAVAILABLE,
                    f"{what} names issuer {document.get('issuer')!r}, not {provider.issuer!r}",
                )
            self._endpoints[provider.id] = _Endpoints(
                authorization=_endpoint(document, "authorization_endpoint", what),
                token=_endpoint(document, "token_endpoint", what),
            )
        return self._endpoints[provider.id]

    async def _send(self, method: str, url: str, what: str, **request: Any) -> httpx.Response:
        try:
            async with asyncio.timeout(TOTAL_SECONDS):
                return await self._client.request(method, url, **request)
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError) as error:
            raise SignInError(
                SignInErrorCode.PROVIDER_UNAVAILABLE, f"{what} could not be reached: {error!r}"
            ) from error


def read_id_token(token: object) -> dict[str, Any]:
    """The claims of an ID token, its signature unverified (see the module's docstring)."""
    try:
        _, payload, _ = token.split(".") if isinstance(token, str) else ()
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, RecursionError):
        claims = None
    if not isinstance(claims, dict):
        raise SignInError(
            SignInErrorCode.INVALID_ID_TOKEN, "the token endpoint answered no readable ID token"
        )
    return claims


def checked_claims(
    claims: Mapping[str, Any], provider: ProviderConfig, *, nonce: str, now: datetime
) -> Claims:
    """The claims of this sign-in's ID token. Each claim is of whatever type the JSON held,
    and a claim that is not as it must be is ``invalid_id_token``, never another error."""
    seconds = now.timestamp()
    audience = claims.get("aud")
    expires, issued = _seconds(claims.get("exp")), _seconds(claims.get("iat"))
    subject = claims.get("sub")
    for refused, claim in (
        (claims.get("iss") != provider.issuer, "iss"),
        (provider.client_id not in (audience if isinstance(audience, list) else [audience]), "aud"),
        ("azp" in claims and claims["azp"] != provider.client_id, "azp"),
        (expires is None or expires <= seconds, "exp"),
        (issued is None or issued > seconds + CLOCK_SKEW_SECONDS, "iat"),
        (claims.get("nonce") != nonce, "nonce"),
        (not isinstance(subject, str) or not subject, "sub"),
    ):
        if refused:
            raise SignInError(
                SignInErrorCode.INVALID_ID_TOKEN, f"{claim} is {claims.get(claim)!r}, now {seconds}"
            )

    google = provider.issuer == GOOGLE_ISSUER
    hosted_domain = _text(claims.get("hd")) if google else None
    email = _text(claims.get("email"))
    # Google verifies a personal account's address whatever its domain: an address is the
    # account's own only in a Workspace, whose domains were proved, or at Gmail.
    at_gmail = email is not None and email.lower().endswith("@gmail.com")
    verified = claims.get("email_verified") is True and (
        not google or hosted_domain is not None or at_gmail
    )
    groups = claims.get(provider.groups_claim) if provider.groups_claim else None
    return Claims(
        subject=subject,
        name=_text(claims.get("name")),
        email=email if verified else None,
        hosted_domain=hosted_domain,
        groups=tuple(g for g in groups if isinstance(g, str)) if isinstance(groups, list) else (),
    )


def _endpoint(document: Mapping[str, Any], key: str, what: str) -> str:
    url = document.get(key)
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
    except ValueError:
        parts = None
    if parts is None or not (
        parts.scheme == "https" or (parts.scheme == "http" and is_loopback(parts.hostname or ""))
    ):
        raise SignInError(
            SignInErrorCode.PROVIDER_UNAVAILABLE,
            f"{what} names {key} {url!r}, which is neither https nor http on loopback",
        )
    return url


def _token_answer(response: httpx.Response, what: str) -> Mapping[str, Any]:
    status = response.status_code
    if status >= 500:
        raise SignInError(SignInErrorCode.PROVIDER_UNAVAILABLE, f"{what} answered {status}")
    answer = _object(response)
    said = answer or {}
    if status >= 400 or "error" in said:
        raise SignInError(
            SignInErrorCode.PROVIDER_REFUSED,
            f"{what} answered {status}: error={said.get('error')!r}"
            f" error_description={said.get('error_description')!r}",
        )
    if status != 200 or answer is None:
        raise SignInError(
            SignInErrorCode.PROVIDER_UNAVAILABLE, f"{what} answered {status} with no JSON object"
        )
    return answer


def _object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        value = json.loads(response.content)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _seconds(value: object) -> float | None:
    """A time claim as a number; not a ``bool``, which is an ``int``, nor a NaN or an infinity,
    which compare as nothing else does."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if isinstance(value, int) or math.isfinite(value) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
