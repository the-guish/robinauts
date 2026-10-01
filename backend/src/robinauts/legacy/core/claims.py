# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Reading an ID token, and checking that it belongs to this sign-in.

**The signature is not verified, deliberately.** The token is read out of the
token endpoint's own answer, over TLS, to a request that carried the client
credentials and the PKCE verifier; OpenID Connect Core 3.1.3.7 item 6 allows
the signature to be skipped in exactly that case. It holds only as long as
that stays true: if a token ever has to be accepted from anywhere else -- a
front channel, another service, a cached token -- this module is not enough,
and a JWT library with JWKS is needed, which is a decision of its own
(``docs/specs/sign-in.md``).

What is checked, on claims already decoded, with ``now`` given rather than
read from a clock: ``iss``, ``aud``, ``azp``, ``exp`` and ``iat`` within a
minute's skew, ``nonce``, and a ``sub`` that is there.

Everything here reads what a provider sent, which is to say what an attacker
may have sent: every claim is of whatever type and size the JSON held. No
input reaches this module that can leave it as anything but an
``InvalidIdTokenError`` -- never a ``TypeError``, a ``UnicodeEncodeError``, an
``OverflowError`` or a value that quietly skips a check.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from robinauts.legacy.core.urls import normalise_issuer
from robinauts.legacy.domain import (
    GOOGLE_BARE_ISSUER,
    Identity,
    InvalidIdTokenError,
    InvalidValueError,
    ProviderConfig,
    ProviderUnavailableError,
)

CLOCK_SKEW_SECONDS = 60.0
"""How far ``exp`` and ``iat`` may be off this machine's clock."""

GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
"""Google's own domains: an address at one is nobody else's, ever."""

_SHOWN = 120
"""How much of a claim a message repeats; a token may hold megabytes."""

MAX_ID_TOKEN_CHARS = 64 * 1024
"""The longest ID token that is read at all.

Generous: a token carrying a long list of groups runs to a few kilobytes,
and nothing legitimate approaches this. Past it the token is refused
unread, so no amount of what a provider -- or whoever answered for it --
sent is decoded, parsed or repeated.
"""

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")
"""The base64url alphabet, RFC 7515 2: no ``+``, no ``/``, and JWTs carry no padding."""


def decode_id_token(id_token: str) -> Mapping[str, Any]:
    """The claims of a JWT, **without verifying its signature**.

    Reads the second of the three parts as base64url-encoded JSON, nothing
    more. See this module's docstring for why that is enough here, and for
    when it stops being.

    ``NaN``, ``Infinity`` and ``-Infinity`` are refused: they are no part of
    JSON (RFC 8259), Python's parser accepts them as an extension, and a
    ``NaN`` in ``exp`` compares false against everything, which is to say it
    would pass for a token that never expires.

    The encoding is checked rather than guessed at.
    ``base64.urlsafe_b64decode`` drops whatever is not in its alphabet, so
    ``eyJ...fQ`` and ``ey J\n..(.fQ`` decode alike, and it takes ``+`` and
    ``/`` as well; two spellings of one token are two chances to slip a claim
    past a check that read the other. Only the alphabet of RFC 7515 is
    accepted here, padded correctly or, as a JWT is, not at all.
    """
    if not isinstance(id_token, str):
        raise InvalidIdTokenError(f"the id_token is not text: {_shown(id_token)}")
    if len(id_token) > MAX_ID_TOKEN_CHARS:
        raise InvalidIdTokenError(
            f"the id_token is {len(id_token)} characters, over the {MAX_ID_TOKEN_CHARS} read"
        )
    pieces = id_token.split(".")
    if len(pieces) != 3:
        raise InvalidIdTokenError(f"the id_token is not a JWT: {len(pieces)} parts")
    try:
        claims = json.loads(_decoded(pieces[1]), parse_constant=_no_constant)
    except (binascii.Error, ValueError) as exc:
        raise InvalidIdTokenError(f"the id_token cannot be read: {_shown(exc)}") from None
    if not isinstance(claims, dict):
        raise InvalidIdTokenError("the id_token's claims are not an object")
    return claims


def _decoded(piece: str) -> bytes:
    """One part of a JWT, decoded strictly; ``InvalidIdTokenError`` if it is not one."""
    if not piece or not _BASE64URL.fullmatch(piece):
        raise InvalidIdTokenError("the id_token is not base64url")
    body = piece.rstrip("=")
    padding = len(piece) - len(body)
    # A correctly padded part is a multiple of four; an unpadded one is
    # anything but one character past a multiple, which decodes to nothing.
    if (padding and len(piece) % 4) or len(body) % 4 == 1:
        raise InvalidIdTokenError("the id_token's padding is not base64url's")
    return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))


def accepted_issuers(provider: ProviderConfig) -> frozenset[str]:
    """The exact strings this provider's ``iss`` may be.

    The configured issuer, which is normalised, and for Google the bare host
    as well, because Google documents both forms and sends both. Nothing the
    provider says about itself is added here: what discovery publishes is
    checked against the configured issuer by ``check_published_issuer``, and a
    provider that names another issuer is unusable, not authoritative.
    """
    issuers = {provider.issuer}
    if provider.is_google:
        issuers.add(GOOGLE_BARE_ISSUER)
    return frozenset(issuers)


def check_published_issuer(provider: ProviderConfig, published: object) -> None:
    """Raise unless discovery names the issuer this provider is configured with.

    The specification is plain: "The issuer it names must equal the configured
    one". Equal after normalisation, so a trailing slash, a default port or
    the case of the host are not a mismatch -- and anything else is
    ``provider_unavailable``: the deployment cannot tell whether it is talking
    to the provider it was configured for.
    """
    if not isinstance(published, str) or not published:
        raise ProviderUnavailableError(
            f"discovery names issuer {_shown(published)}, not {provider.issuer!r}"
        )
    try:
        normalised = normalise_issuer(published)
    except InvalidValueError:
        raise ProviderUnavailableError(
            f"discovery names issuer {_shown(published)}, which is not a URL; "
            f"{provider.id} is configured with {provider.issuer!r}"
        ) from None
    if normalised != provider.issuer:
        raise ProviderUnavailableError(
            f"discovery names issuer {_shown(published)}, not {provider.issuer!r}"
        )


def check_id_token_claims(
    claims: Mapping[str, Any],
    provider: ProviderConfig,
    *,
    nonce: str,
    now: datetime,
) -> None:
    """Raise ``InvalidIdTokenError`` unless these claims are this sign-in's.

    ``now`` is passed in, never read here: core has no clock. It must carry a
    time zone, or the comparison with ``exp`` would silently mean local time.
    ``nonce`` is the one handed to the provider when this sign-in began; an
    empty one is a mistake in the caller, not a token to refuse.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise InvalidValueError("now must be an aware datetime, or exp means nothing")
    if not nonce:
        raise InvalidValueError("a sign-in's nonce is never empty: nothing to compare with")
    seconds = now.timestamp()

    issuer = claims.get("iss")
    # A str first: a list or an object cannot be looked up in a set.
    if not isinstance(issuer, str) or issuer not in accepted_issuers(provider):
        raise InvalidIdTokenError(f"iss {_shown(issuer)}")

    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or not _holds(audiences, provider.client_id):
        raise InvalidIdTokenError(f"aud {_shown(audience)}")

    # azp says which client the token was issued for; it is required when
    # there are several audiences, and honoured whenever it is there.
    azp = claims.get("azp")
    if (len(audiences) > 1 or azp is not None) and not _same(azp, provider.client_id):
        raise InvalidIdTokenError(f"azp {_shown(azp)}")

    expires, issued = _number(claims.get("exp")), _number(claims.get("iat"))
    if expires is None or expires + CLOCK_SKEW_SECONDS <= seconds:
        raise InvalidIdTokenError(f"exp {_shown(claims.get('exp'))}, now {seconds}")
    if issued is None or issued - CLOCK_SKEW_SECONDS > seconds:
        raise InvalidIdTokenError(f"iat {_shown(claims.get('iat'))}, now {seconds}")

    got = claims.get("nonce")
    if not isinstance(got, str) or not hmac.compare_digest(_bytes(got), _bytes(nonce)):
        raise InvalidIdTokenError("nonce is not this sign-in's")

    if not _text(claims.get("sub")):
        raise InvalidIdTokenError(f"sub {_shown(claims.get('sub'))}")


def identity_from_claims(claims: Mapping[str, Any], provider: ProviderConfig) -> Identity:
    """Who the claims describe. Check them first: this trusts what it reads."""
    subject = _text(claims.get("sub"))
    if subject is None:
        raise InvalidIdTokenError(f"sub {_shown(claims.get('sub'))}")

    email = _text(claims.get("email"))
    verified = _flag(claims.get("email_verified"))

    # hd is Google's, and means a Workspace only there: another provider may
    # send a claim of that name holding anything at all, even what its users
    # typed, so it is not read from one.
    hosted_domain = _text(claims.get("hd")) if provider.is_google else None

    if provider.is_google and email is not None:
        # Google verifies personal accounts registered with any address: an
        # address is someone's own only in a Workspace, whose domains the
        # administrators proved, or at Gmail, which reassigns none.
        domain = email.rpartition("@")[2].lower()
        verified = verified and (hosted_domain is not None or domain in GMAIL_DOMAINS)

    groups: frozenset[str] = frozenset()
    if provider.groups_claim is not None:
        raw = claims.get(provider.groups_claim)
        if isinstance(raw, str):
            # One name, as a provider that has a single group sends it; an
            # empty one is no name at all.
            groups = frozenset({raw}) if raw else frozenset()
        elif isinstance(raw, list):
            groups = frozenset(group for group in raw if isinstance(group, str) and group)

    return Identity(
        provider=provider.id,
        subject=subject,
        name=_text(claims.get("name")),
        email=email,
        email_verified=verified,
        hosted_domain=hosted_domain,
        groups=groups,
    )


def identity_from_id_token(
    id_token: str,
    provider: ProviderConfig,
    *,
    nonce: str,
    now: datetime,
) -> Identity:
    """Read an ID token, check its claims, and say who signed in.

    The whole of what an ID token is worth, in one call, so that no caller can
    read one without checking it.
    """
    claims = decode_id_token(id_token)
    check_id_token_claims(claims, provider, nonce=nonce, now=now)
    return identity_from_claims(claims, provider)


def _no_constant(name: str) -> Any:
    """Refuse ``NaN`` and the infinities, which JSON does not have."""
    raise ValueError(f"{name} is not JSON")


def _number(value: object) -> float | None:
    """``value`` as a number of seconds; ``None`` if it is not one.

    ``None`` for anything that cannot be compared with a clock: a string, a
    ``bool`` (which is an ``int`` in Python and would pass for the first
    second of 1970), a NaN or an infinity (which make every comparison say
    what an attacker wants), and an integer too large to be a float at all.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: object) -> str | None:
    """``value`` if it is a non-empty string, ``None`` if it is anything else."""
    return value if isinstance(value, str) and value else None


def _flag(value: object) -> bool:
    """Whether a claim says yes: ``true``, or the word, as some providers send it.

    Nothing else counts -- not ``1``, not ``"1"``, not ``"yes"``. A claim that
    a provider did not plainly assert is not one to act on.
    """
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


def _same(value: object, expected: str) -> bool:
    """Whether a claim is exactly ``expected``; anything not a string is not."""
    return isinstance(value, str) and value == expected


def _holds(values: list[Any], expected: str) -> bool:
    """Whether the list holds ``expected`` as a string of its own."""
    return any(_same(value, expected) for value in values)


def _bytes(text: str) -> bytes:
    """``text`` as bytes for a constant-time comparison, lone surrogates and all.

    A claim is whatever JSON held, and JSON can hold ``"\\ud800"``, which plain
    UTF-8 refuses to encode. Refusing to encode it must not become an error of
    another kind escaping a claim check.
    """
    return text.encode("utf-8", "surrogatepass")


def _shown(value: object) -> str:
    """A claim as a message may repeat it: its repr, kept short."""
    try:
        text = repr(value)
    except Exception:  # pragma: no cover -- no claim from JSON has a repr that fails
        return "<unreadable>"
    return text if len(text) <= _SHOWN else text[:_SHOWN] + "..."
