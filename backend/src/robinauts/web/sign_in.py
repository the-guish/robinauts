# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Sign-in's half of the configuration file: where the deployment is served, the identity
providers people sign in through, and who may (``docs/specs/sign-in.md``).

A provider's client secret is the name of the environment variable that holds it. Nothing
here reads it.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from robinauts.controller.contract.domain import ConfigError

SIGN_IN_KEYS = frozenset({"public_url", "session_hours", "providers", "allow", "admin"})
"""The file's top-level keys that are sign-in's. ``admin`` is one of them so that it is
refused for what it is."""

GOOGLE_ISSUER = "https://accounts.google.com"
DEFAULT_SCOPES = ("openid", "email", "profile")
SESSION_HOURS = 12

PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
"""How a provider's id is spelt. The local development mode's ``!local`` is not spelt so, and
no configuration can name it."""


class Matcher(StrEnum):
    EVERYONE = "everyone"
    SUBJECT = "subject"
    EMAIL = "email"
    EMAIL_DOMAIN = "email_domain"
    HOSTED_DOMAIN = "hosted_domain"
    GROUP = "group"


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

    tables = raw.get("providers", {})
    known = {field.name for field in dataclasses.fields(ProviderConfig)} - {"id"}
    providers = {}
    for id_, table in tables.items():
        if not PROVIDER_ID.fullmatch(id_):
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
        providers=providers,
        allow=tuple(allow),
    )
