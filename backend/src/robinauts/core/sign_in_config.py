# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Checking the sign-in configuration, from raw tables into domain records.

The operator writes TOML (``docs/specs/sign-in.md``); an adapter parses the
file and hands the result here, so that reading a file and judging what is in
it stay apart and this can be tested with a dict.

Two rules run through it. **Unknown keys are errors**: a misspelt key that was
ignored would be a rule the operator believes is in force and is not. **Every
problem is reported at once**, so that a deployment is fixed in one pass
rather than in as many restarts as there are mistakes.

    public_url = "https://robinauts.example.com"
    session_hours = 12

    [providers.google]
    title = "Google"
    issuer = "https://accounts.google.com"
    client_id = "1234.apps.googleusercontent.com"
    client_secret_env = "ROBINAUTS_GOOGLE_SECRET"

    [[allow]]
    provider = "google"
    hosted_domain = "example.com"

Secrets are never in the file: a provider names the environment variable its
client secret is read from, and the variable is read elsewhere, by the layer
that may touch the environment.

**One file, two parsers.** The same file holds the model tables
(``core.models_config``), and each parser is handed the whole of it and reads
its own share. So "unknown key" means unknown to *both*, which is what
``TOP_LEVEL_KEYS`` below is for: a misspelt table is still refused, and a
table that is simply the other parser's is not.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from robinauts.core.urls import normalise_issuer, normalise_origin
from robinauts.domain import (
    DEFAULT_SCOPES,
    DEFAULT_SESSION_HOURS,
    MAX_PROVIDER_ID_CHARS,
    MAX_SESSION_HOURS,
    TOKEN_ENDPOINT_AUTH_METHODS,
    AllowEntry,
    ConfigError,
    InvalidValueError,
    Matcher,
    ProviderConfig,
    SignInConfig,
    is_google_issuer,
    is_provider_id,
)

SIGN_IN_KEYS = frozenset({"public_url", "session_hours", "providers", "allow"})
"""The top-level tables sign-in is written in."""

MODEL_KEYS = frozenset({"model_providers", "models", "agents", "mcp_servers"})
"""The top-level tables the model half is written in (``core.models_config``).

Named here, beside sign-in's own, because **one file holds both** and each
parser is handed the whole of it: a table that belongs to the other parser is
not an unknown key, and the one way for the two to agree about that is for
both sets to be written down once. ``core.models_config`` imports these rather
than repeating them.

They are ``model_providers`` and not ``providers`` because ``providers`` is
already taken, by the identity providers people sign in with: one file, two
kinds of provider, and a table that meant one of them in one place and the
other elsewhere would be the worst of both (``docs/specs/agents.md``).
"""

TOP_LEVEL_KEYS = SIGN_IN_KEYS | MODEL_KEYS
"""Every table a configuration file may hold: what neither parser refuses."""
PROVIDER_KEYS = frozenset(
    {
        "title",
        "issuer",
        "client_id",
        "client_secret_env",
        "scopes",
        "groups_claim",
        "token_endpoint_auth",
    }
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MATCHERS = {matcher.value: matcher for matcher in Matcher}

_COMPARED_AS_ADDRESSES = frozenset({Matcher.EMAIL, Matcher.EMAIL_DOMAIN, Matcher.HOSTED_DOMAIN})
"""The matchers whose value is an address or a domain, and is shaped like one."""


def parse_sign_in_config(data: Mapping[str, Any]) -> SignInConfig:
    """The sign-in configuration these tables describe.

    Raises ``ConfigError`` whose ``problems`` names every fault found, each
    saying where it is and what was expected.
    """
    problems: list[str] = []
    _unknown(data, TOP_LEVEL_KEYS | {"admin"}, "", problems)
    if "admin" in data:
        problems.append(
            "admin: roles are not in this release -- everyone who may sign in is a user"
        )

    public_url = _public_url(data, problems)
    session_hours = _session_hours(data, problems)

    providers: dict[str, ProviderConfig] = {}
    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, Mapping):
        problems.append("providers: a table of providers by id")
        raw_providers = {}
    elif not raw_providers:
        problems.append("providers: name at least one identity provider, or nobody could sign in")
    for provider_id, table in raw_providers.items():
        provider = _provider(provider_id, table, problems)
        if provider is not None:
            providers[provider_id] = provider

    allow: list[AllowEntry] = []
    raw_allow = data.get("allow", [])
    if not isinstance(raw_allow, list):
        problems.append("allow: an array of [[allow]] tables")
        raw_allow = []
    elif raw_providers and not raw_allow:
        # Entries that are there and wrong say so themselves, below.
        problems.append("allow: no entry, so nobody could sign in")
    for index, entry in enumerate(raw_allow, start=1):
        parsed = _allow(index, entry, raw_providers, problems)
        if parsed is not None:
            allow.append(parsed)

    if problems:
        raise ConfigError(problems)
    return SignInConfig(
        public_url=public_url,
        providers=providers,
        allow=tuple(allow),
        session_hours=session_hours,
    )


def _public_url(data: Mapping[str, Any], problems: list[str]) -> str:
    raw = data.get("public_url")
    if not isinstance(raw, str) or not raw:
        problems.append("public_url: missing, or not a non-empty string")
        return ""
    try:
        return normalise_origin(raw)
    except InvalidValueError as exc:
        problems.append(f"public_url: {exc}")
        return ""


def _session_hours(data: Mapping[str, Any], problems: list[str]) -> float:
    if "session_hours" not in data:
        return DEFAULT_SESSION_HOURS
    hours = data["session_hours"]
    if (
        isinstance(hours, bool)
        or not isinstance(hours, int | float)
        or not math.isfinite(hours)
        or not 0 < hours <= MAX_SESSION_HOURS
    ):
        problems.append(
            f"session_hours: a number of hours over 0 and at most "
            f"{MAX_SESSION_HOURS:g}, not {hours!r}"
        )
        return DEFAULT_SESSION_HOURS
    return float(hours)


def _provider(provider_id: object, table: object, problems: list[str]) -> ProviderConfig | None:
    where = f"providers.{provider_id}"
    if not is_provider_id(provider_id):
        problems.append(
            f"{where}: an id is up to {MAX_PROVIDER_ID_CHARS} lower-case letters,"
            f" digits, _ and -"
        )
        return None
    if not isinstance(table, Mapping):
        problems.append(f"{where}: a table")
        return None
    before = len(problems)
    _unknown(table, PROVIDER_KEYS, where, problems)

    title = _string(table, "title", where, problems, default=provider_id)
    client_id = _string(table, "client_id", where, problems)

    issuer = ""
    raw_issuer = _string(table, "issuer", where, problems)
    if raw_issuer:
        try:
            issuer = normalise_issuer(raw_issuer)
        except InvalidValueError as exc:
            problems.append(f"{where}.issuer: {exc}")

    secret_env = _string(table, "client_secret_env", where, problems)
    if secret_env and not _ENV_NAME.fullmatch(secret_env):
        problems.append(
            f"{where}.client_secret_env: the NAME of an environment variable "
            f"holding the secret, not the secret itself"
        )
        secret_env = ""

    scopes = DEFAULT_SCOPES
    if "scopes" in table:
        raw = table["scopes"]
        if not (isinstance(raw, list) and raw and all(isinstance(s, str) and s for s in raw)):
            problems.append(f"{where}.scopes: an array of scope names")
        elif "openid" not in raw:
            problems.append(f"{where}.scopes: must include 'openid'")
        else:
            scopes = tuple(raw)

    groups_claim = None
    if "groups_claim" in table:
        groups_claim = _string(table, "groups_claim", where, problems) or None

    auth = "client_secret_basic"
    if "token_endpoint_auth" in table:
        auth = _string(table, "token_endpoint_auth", where, problems)
        if auth and auth not in TOKEN_ENDPOINT_AUTH_METHODS:
            problems.append(
                f"{where}.token_endpoint_auth: one of " f"{', '.join(TOKEN_ENDPOINT_AUTH_METHODS)}"
            )

    if len(problems) > before:
        return None
    return ProviderConfig(
        id=provider_id,
        title=title,
        issuer=issuer,
        client_id=client_id,
        client_secret_env=secret_env,
        scopes=scopes,
        groups_claim=groups_claim,
        token_endpoint_auth=auth,
    )


def _allow(
    index: int, entry: object, declared: Mapping[str, Any], problems: list[str]
) -> AllowEntry | None:
    where = f"allow {index}"
    if not isinstance(entry, Mapping):
        problems.append(f"{where}: a table")
        return None
    _unknown(entry, {"provider", *_MATCHERS}, where, problems)

    provider_id = entry.get("provider")
    if not isinstance(provider_id, str) or provider_id not in declared:
        problems.append(f"{where}: provider {provider_id!r} is not one of [providers]")
        return None

    given = [name for name in _MATCHERS if name in entry]
    if len(given) != 1:
        problems.append(
            f"{where}: exactly one of {', '.join(_MATCHERS)}, not " + (", ".join(given) or "none")
        )
        return None
    (name,) = given
    matcher = _MATCHERS[name]

    raw = entry[name]
    value: str | None
    if matcher is Matcher.EVERYONE:
        if raw is not True:
            problems.append(f"{where}: everyone = true, or no everyone at all")
            return None
        value = None
    elif not isinstance(raw, str) or not raw:
        problems.append(f"{where}: {name} is a non-empty string")
        return None
    else:
        value = raw

    # Read from the provider's own table, so that these rules are checked
    # even when the provider has problems of its own and was not built. The
    # question "is this Google" is the one ``ProviderConfig.is_google`` asks,
    # by host, so that no spelling of the issuer is Google in one place and
    # not in the other.
    table = declared[provider_id]
    issuer = table.get("issuer") if isinstance(table, Mapping) else None
    if matcher is Matcher.GROUP and isinstance(table, Mapping) and not _has_groups_claim(table):
        problems.append(
            f"{where}: group needs providers.{provider_id}.groups_claim, which names "
            f"the claim the provider sends the groups in; without it the entry would "
            f"match nobody"
        )
        return None
    if matcher is Matcher.EMAIL_DOMAIN and is_google_issuer(issuer):
        problems.append(
            f"{where}: email_domain is refused for Google, which verifies personal "
            f"accounts registered with any address: use hosted_domain for a "
            f"Workspace, or email for one person"
        )
        return None
    if matcher is Matcher.EVERYONE and is_google_issuer(issuer):
        problems.append(
            f"{where}: everyone is refused for Google, where it would admit every "
            f"Google account in the world: use hosted_domain for a Workspace, or "
            f"email for one person"
        )
        return None
    if matcher is Matcher.HOSTED_DOMAIN and not is_google_issuer(issuer):
        problems.append(
            f"{where}: hosted_domain is Google's hd claim, and means nothing from "
            f"another provider: use group or email for {provider_id!r}"
        )
        return None
    shape = _shape(matcher, value, name)
    if shape is not None:
        problems.append(f"{where}: {shape}")
        return None
    if (
        matcher in (Matcher.EMAIL, Matcher.EMAIL_DOMAIN)
        and isinstance(table, Mapping)
        and not _asks_for_email(table)
    ):
        problems.append(
            f"{where}: {name} needs 'email' among providers.{provider_id}.scopes; "
            f"without it the provider sends no address and the entry would match "
            f"nobody"
        )
        return None
    return AllowEntry(provider_id, matcher, value)


def _shape(matcher: Matcher, value: str | None, name: str) -> str | None:
    """What is wrong with the value of an address or domain matcher; ``None`` if nothing.

    An entry that cannot ever match is a rule the operator believes is in
    force: ``email = "ada"`` or ``email_domain = "https://example.com"`` are
    mistakes worth a message, not entries to keep.
    """
    if value is None or matcher not in _COMPARED_AS_ADDRESSES:
        return None
    if not value.isascii():
        # Matching is ASCII-only on purpose (``robinauts.core.allow``), so a
        # value outside it would match nobody, quietly.
        return (
            f"{name} is compared as ASCII: write an internationalised domain as its "
            f"A-label (punycode, xn--...) form"
        )
    if any(character.isspace() for character in value):
        return f"{name} holds a space"
    if matcher is Matcher.EMAIL:
        local, at, domain = value.partition("@")
        if not at or not local or not domain or "@" in domain:
            return f"{name} is one address: a name, an @, and a domain"
        return None
    if "@" in value or "/" in value or ":" in value:
        return f"{name} is a bare domain, such as 'example.com': no scheme, no path, no @"
    return None


def _asks_for_email(table: Mapping[str, Any]) -> bool:
    """Whether the provider asks for the scope its addresses arrive in.

    True when the scopes are malformed: that is the provider's own problem,
    and it is reported where the provider is read.
    """
    raw = table.get("scopes", DEFAULT_SCOPES)
    if not isinstance(raw, list | tuple):
        return True
    return "email" in raw


def _has_groups_claim(table: Mapping[str, Any]) -> bool:
    """Whether the provider's table names the claim its groups come in."""
    claim = table.get("groups_claim")
    return isinstance(claim, str) and bool(claim)


def _unknown(
    table: Mapping[str, Any], known: frozenset[str] | set[str], where: str, problems: list[str]
) -> None:
    for key in table:
        if key not in known:
            prefix = f"{where}: " if where else ""
            problems.append(f"{prefix}unknown key {key!r}")


def _string(
    table: Mapping[str, Any],
    key: str,
    where: str,
    problems: list[str],
    *,
    default: str | None = None,
) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        problems.append(f"{where}.{key}: missing, or not a non-empty string")
        return ""
    return value
