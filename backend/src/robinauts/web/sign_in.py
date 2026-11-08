# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Sign-in's half of the configuration file: where the deployment is served, the identity
providers people sign in through, and who may (``docs/specs/sign-in.md``). And the flow, from
the button to a session, over the credentials, the exchange and the controller's
``ensure_user``.

A provider's client secret is the name of the environment variable that holds it. Nothing
here reads it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import math
import re
import secrets
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from robinauts.controller.contract.domain import (
    ConfigError,
    Identity,
    PendingLogin,
    User,
    UserSession,
)
from robinauts.controller.contract.ports import Controller, Credentials

if TYPE_CHECKING:
    from robinauts.web.oidc import Claims, Exchange

SIGN_IN_KEYS = frozenset(
    {"public_url", "session_hours", "device_days", "providers", "allow", "admin"}
)
"""The file's top-level keys that are sign-in's. ``admin`` is one of them so that it is
refused for what it is."""

GOOGLE_ISSUER = "https://accounts.google.com"
DEFAULT_SCOPES = ("openid", "email", "profile")
SESSION_HOURS = 12
DEVICE_DAYS = 30
"""How long a linked device lives (``docs/architecture/device-pairing.md``); 0 is no devices."""

PROVIDER_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")
"""How a provider's id is spelt. The local development mode's ``!local`` is not spelt so, and
no configuration can name it."""
LOCAL_PROVIDER = "!local"

PENDING_LOGIN_LIFE = timedelta(minutes=10)
ALLOW_CHECK_LIFE = timedelta(seconds=60)
"""How long a credential's pass of the allow list on use is believed before it is checked
again: so a person taken out of the list loses every credential within a minute."""
RECENT_CHECKS_KEPT = 4096
"""How many passes are kept before the stale ones are dropped."""


class Matcher(StrEnum):
    EVERYONE = "everyone"
    SUBJECT = "subject"
    EMAIL = "email"
    EMAIL_DOMAIN = "email_domain"
    HOSTED_DOMAIN = "hosted_domain"
    GROUP = "group"


class SecretKind(StrEnum):
    """What a bearer is, as its prefix says: the prefix picks the table it is looked up in,
    and lets a secret scanner recognise ours. A secret with no prefix is an API token minted
    before there were prefixes."""

    DEVICE = "rbd_"
    TOKEN = "rbt_"


class SignInErrorCode(StrEnum):
    """Why a sign-in did not complete, as the browser is told it."""

    EXPIRED = "expired"
    STATE_MISMATCH = "state_mismatch"
    NOT_ALLOWED = "not_allowed"
    UNKNOWN_PROVIDER = "unknown_provider"
    BUSY = "busy"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REFUSED = "provider_refused"
    INVALID_ID_TOKEN = "invalid_id_token"


class SignInError(Exception):
    """A sign-in that does not complete. The code is what the browser is told; the detail,
    which may hold the provider's own words, is for the log alone."""

    def __init__(self, code: SignInErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    id: str
    title: str
    issuer: str
    client_id: str
    client_secret_env: str
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    groups_claim: str | None = None


@dataclass(frozen=True, slots=True)
class AllowEntry:
    provider: str
    matcher: Matcher
    value: str | None
    """What the matcher compares with; ``None`` for ``everyone``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SignInConfig:
    public_url: str
    """The deployment's origin, ``scheme://host[:port]``."""
    session_hours: float = SESSION_HOURS
    device_days: float = DEVICE_DAYS
    """How long a linked device lives; 0 is a deployment with no devices."""
    providers: Mapping[str, ProviderConfig]
    allow: tuple[AllowEntry, ...]


def is_loopback(host: str) -> bool:
    """A literal loopback address, or exactly ``localhost``. Any other name is what this
    machine's resolver makes of it."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_days(value: object) -> bool:
    """A number of days, 0 or more: not a ``bool``, which is an ``int``, nor a NaN or an
    infinity."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and value >= 0


def parse_sign_in(raw: Mapping[str, Any]) -> SignInConfig | None:
    """The sign-in configuration of the file's tables, or ``None`` when they hold none."""
    if not SIGN_IN_KEYS & raw.keys():
        return None
    problems: list[str] = []
    if "admin" in raw:
        problems.append("admin: roles are not in this release, so there is no admin list")

    url = raw.get("public_url", "")
    origin = urlsplit(url)
    on_loopback = origin.scheme == "http" and is_loopback(origin.hostname or "")
    if origin.scheme != "https" and not on_loopback:
        problems.append(f"public_url: {url!r} is not https, which only a loopback host may skip")
    if origin.path.strip("/") or origin.query or origin.fragment:
        problems.append(f"public_url: {url!r} has a path, a query or a fragment; it is an origin")

    device_days = raw.get("device_days", DEVICE_DAYS)
    if not is_days(device_days):
        problems.append(f"device_days: {device_days!r} is not a number of days, 0 or more")

    tables = raw.get("providers", {})
    known = {field.name for field in dataclasses.fields(ProviderConfig)} - {"id"}
    providers = {}
    for id_, table in tables.items():
        if not PROVIDER_ID_PATTERN.fullmatch(id_):
            problems.append(f"providers.{id_}: an id is lower-case letters, digits, - and _")
        elif unknown := sorted(set(table) - known):
            problems.append(f"providers.{id_}: unknown key(s) {', '.join(unknown)}")
        else:
            scopes = tuple(table.get("scopes", DEFAULT_SCOPES))
            try:
                providers[id_] = ProviderConfig(id=id_, **{**table, "scopes": scopes})
            except TypeError as error:
                problems.append(f"providers.{id_}: {error}")

    entries = raw.get("allow", [])
    if tables and not entries:
        problems.append("allow: no entry, so nobody could sign in")
    allow = []
    for number, entry in enumerate(entries, start=1):
        where = f"allow {number}"
        given = [matcher for matcher in Matcher if matcher in entry]
        matcher = given[0] if len(given) == 1 else None
        provider = entry.get("provider")
        # Google is its issuer spelt exactly: a sign-in refuses a discovery document that names
        # another issuer than the configured one, so no other spelling signs anybody in.
        google = provider in tables and tables[provider].get("issuer") == GOOGLE_ISSUER
        if unknown := sorted(set(entry) - {"provider", *Matcher}):
            problems.append(f"{where}: unknown key(s) {', '.join(unknown)}")
        elif matcher is None:
            problems.append(f"{where}: one matcher of {', '.join(Matcher)}, not {len(given)}")
        elif provider not in tables:
            problems.append(f"{where}: provider {provider!r} is not configured")
        elif google and matcher in (Matcher.EVERYONE, Matcher.EMAIL_DOMAIN):
            problems.append(
                f"{where}: {matcher} is refused for Google, whose accounts anybody may open:"
                " hosted_domain names a Workspace, email one person"
            )
        elif not google and matcher is Matcher.HOSTED_DOMAIN:
            problems.append(f"{where}: hosted_domain is Google's hd claim, Google only")
        elif matcher is Matcher.EVERYONE and entry[matcher] is not True:
            problems.append(f"{where}: everyone = true, or no everyone")
        else:
            value = None if matcher is Matcher.EVERYONE else entry[matcher]
            allow.append(AllowEntry(provider, matcher, value))

    if problems:
        raise ConfigError("\n".join(problems))
    return SignInConfig(
        public_url=f"{origin.scheme}://{origin.netloc}",
        session_hours=raw.get("session_hours", SESSION_HOURS),
        device_days=device_days,
        providers=providers,
        allow=tuple(allow),
    )


# --- the flow -------------------------------------------------------------------


def random_secret() -> str:
    """A URL-safe random value of 32 bytes: a ``state``, a ``nonce``, a PKCE verifier or a
    session's secret."""
    return secrets.token_urlsafe(32)


def secret_hash(secret: str) -> str:
    """The SHA-256 hex of the whole secret, its prefix included."""
    return hashlib.sha256(secret.encode()).hexdigest()


def secret_of_kind(kind: SecretKind) -> str:
    """A random secret, prefixed with its kind."""
    return kind + random_secret()


def kind_of(secret: str) -> SecretKind:
    """A device's secret by its prefix; anything else is an API token, with ``rbt_`` or, minted
    before there were prefixes, with none."""
    return SecretKind.DEVICE if secret.startswith(SecretKind.DEVICE) else SecretKind.TOKEN


def safe_return_to(target: str | None) -> str:
    """``target`` when it is a path of this origin, else ``/``."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


def allows(entry: AllowEntry, claims: Claims) -> bool:
    value = entry.value or ""
    email = claims.email.lower() if claims.email else None
    match entry.matcher:
        case Matcher.EVERYONE:
            return True
        case Matcher.SUBJECT:
            return claims.subject == value
        case Matcher.EMAIL:
            return email is not None and email == value.lower()
        case Matcher.EMAIL_DOMAIN:
            return email is not None and email.rpartition("@")[2] == value.lower()
        case Matcher.HOSTED_DOMAIN:
            return claims.hosted_domain == value
        case Matcher.GROUP:
            return value in claims.groups


def is_allowed(allow: Iterable[AllowEntry], provider_id: str, claims: Claims) -> bool:
    return any(entry.provider == provider_id and allows(entry, claims) for entry in allow)


class RecentChecks:
    """The credentials whose user passed the allow list lately, by the credential's hash, in
    this process. Only a pass is kept: a failure deletes the credential."""

    def __init__(self, life: timedelta = ALLOW_CHECK_LIFE, kept: int = RECENT_CHECKS_KEPT):
        self._life = life
        self._kept = kept
        self._passed: dict[str, datetime] = {}

    def passed(self, key: str, now: datetime) -> bool:
        """Whether that credential passed less than ``life`` before ``now``."""
        at = self._passed.get(key)
        return at is not None and timedelta(0) <= now - at < self._life

    def record(self, key: str, now: datetime) -> None:
        self._passed[key] = now
        if len(self._passed) > self._kept:
            for stale in [k for k, at in self._passed.items() if not self.passed(k, now)]:
                del self._passed[stale]

    def __len__(self) -> int:
        return len(self._passed)


@dataclass(frozen=True, slots=True)
class SignedIn:
    """A completed sign-in: the user, the session's secret for the cookie, and where to land."""

    user: User
    secret: str
    expires_at: datetime
    return_to: str


class SignIn:
    """Signing in and out, and who a session cookie stands for. Every call is given ``now``."""

    def __init__(
        self,
        config: SignInConfig,
        *,
        credentials: Credentials,
        exchange: Exchange,
        controller: Controller,
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._exchange = exchange
        self._controller = controller

    async def begin(
        self, provider_id: str, *, return_to: str | None, now: datetime
    ) -> tuple[str, str]:
        """The provider's authorization URL, and the ``state`` for the login cookie."""
        state, nonce, verifier = random_secret(), random_secret(), random_secret()
        # The exchange refuses an unknown provider, and one it cannot reach, before anything
        # is stored.
        url = await self._exchange.begin(provider_id, state=state, nonce=nonce, verifier=verifier)
        login = PendingLogin(
            state_hash=secret_hash(state),
            provider=provider_id,
            nonce=nonce,
            verifier=verifier,
            return_to=safe_return_to(return_to),
            created_at=now,
            expires_at=now + PENDING_LOGIN_LIFE,
        )
        await self._credentials.add_pending_login(login, now)
        return url, state

    async def complete(
        self,
        provider_id: str,
        *,
        state: str | None,
        cookie_state: str | None,
        code: str,
        now: datetime,
    ) -> SignedIn:
        if state is None or state != cookie_state:
            raise SignInError(
                SignInErrorCode.STATE_MISMATCH,
                "the callback's state is not the one in this browser's login cookie",
            )
        login = await self._credentials.take_pending_login(secret_hash(state), now)
        if login is None:
            raise SignInError(
                SignInErrorCode.EXPIRED, "the sign-in is unknown, used already or expired"
            )
        if login.provider != provider_id:
            raise SignInError(
                SignInErrorCode.STATE_MISMATCH,
                f"the sign-in was begun with {login.provider!r}, not {provider_id!r}",
            )
        claims = await self._exchange.complete(
            provider_id, code=code, nonce=login.nonce, verifier=login.verifier, now=now
        )
        if not is_allowed(self._config.allow, provider_id, claims):
            # The provider has just vouched for somebody the allow list no longer lets in: the
            # moment to end every credential they still hold. Only this refusal does it.
            await self._credentials.delete_credentials_of_identity(provider_id, claims.subject)
            raise SignInError(
                SignInErrorCode.NOT_ALLOWED,
                f"no allow entry of {provider_id!r} lets in {claims.email or claims.subject!r}",
            )
        user = await self._controller.ensure_user(
            Identity(
                provider_id,
                claims.subject,
                name=claims.name,
                email=claims.email,
                hosted_domain=claims.hosted_domain,
                groups=claims.groups,
            )
        )
        secret = random_secret()
        expires_at = now + timedelta(hours=self._config.session_hours)
        await self._credentials.add_user_session(
            UserSession(uuid.uuid4(), user.id, secret_hash(secret), now, expires_at)
        )
        return SignedIn(user, secret, expires_at, login.return_to)

    async def resolve(self, secret: str, now: datetime) -> tuple[User, UserSession] | None:
        """The user a session cookie stands for, and the session, whose ``created_at`` is how
        fresh the sign-in is."""
        found = await self._credentials.resolve_user_session(secret_hash(secret), now)
        # No sign-in makes the local development mode's user, so a session naming it came from
        # elsewhere: a database kept from a run of the mode hands nobody that account.
        return None if found is None or found[0].provider == LOCAL_PROVIDER else found

    async def sign_out(self, secret: str) -> bool:
        return await self._credentials.delete_user_session(secret_hash(secret))
