# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""How a deployment is signed in to: the providers and who they let in.

These records are what a configuration file becomes once
``robinauts.legacy.core.sign_in_config`` has checked it. They hold no secret: a
provider's client secret is named by the environment variable it is read
from, so a configuration object can be logged, printed in a traceback or
kept in a crash report without leaking one.

Roles are deferred (``docs/working-notes/poc-scope.md``, "Out"): everyone who
may sign in is an ordinary user, and there is no admin list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import urlsplit

from robinauts.legacy.domain.errors import InvalidValueError, UnknownProviderError

GOOGLE_HOST = "accounts.google.com"
"""The host Google issues its ID tokens from; the one thing that says it is Google."""

GOOGLE_ISSUER = "https://accounts.google.com"
"""Google's issuer, normalised."""

GOOGLE_BARE_ISSUER = GOOGLE_HOST
"""The second form Google documents for ``iss``, and is seen to send."""

DEFAULT_SCOPES: tuple[str, ...] = ("openid", "email", "profile")
"""What is asked for when a provider names no scopes of its own."""

TOKEN_ENDPOINT_AUTH_METHODS: tuple[str, ...] = (
    "client_secret_basic",
    "client_secret_post",
)
"""How the client authenticates at the token endpoint; OIDC's two usual ways."""

MAX_PROVIDER_ID_CHARS = 40

_PROVIDER_ID = re.compile(rf"[a-z0-9][a-z0-9_-]{{0,{MAX_PROVIDER_ID_CHARS - 1}}}")
"""What a provider's id may be spelt with.

Here rather than in ``core`` because two layers need it and neither may import
the other: ``core`` refuses a configuration that names an id of another shape,
and ``api`` refuses a **request** naming one before it goes near the sign-in
flow. A path parameter is whatever a link said, and an id that reached a log
line or a message unchecked would be as long, and hold whatever, its author
liked.
"""


def is_provider_id(value: object) -> bool:
    """Whether ``value`` is spelt the way a provider's id is spelt."""
    return isinstance(value, str) and _PROVIDER_ID.fullmatch(value) is not None


DEFAULT_SESSION_HOURS = 12.0
MAX_SESSION_HOURS = 24.0 * 365.0
"""A year. Sessions are not renewed, so a longer one is a mistake, not a choice."""


def is_google_issuer(issuer: object) -> bool:
    """Whether ``issuer`` names Google, however it happens to be spelt.

    By host alone, case-folded, with the root label's dot and any port
    ignored, so that ``accounts.google.com``, ``https://accounts.google.com``,
    ``HTTPS://Accounts.Google.com.:443/`` and the trailing-slash form are all
    Google. Everyone who asks asks this one function, because the answer
    decides whether Google's rules hold: that ``hd`` is read, that
    ``email_verified`` is believed only with ``hd`` or at Gmail, and that
    ``email_domain`` and ``everyone`` are refused. A spelling that slipped
    through as "not Google" would quietly drop all of them.

    Anything else -- another host, a host with Google's name in the path or in
    the userinfo, something that is not a URL at all -- is not Google.
    """
    if not isinstance(issuer, str):
        return False
    text = issuer.strip()
    try:
        parts = urlsplit(text)
        if not parts.scheme:
            # Google's own second form for ``iss`` is the bare host.
            parts = urlsplit("https://" + text)
        host = parts.hostname or ""
    except ValueError:
        return False
    return host.lower().rstrip(".") == GOOGLE_HOST


class Matcher(StrEnum):
    """What an allow entry looks at in an identity.

    The value of each is also the key that names it in the configuration.
    """

    EVERYONE = "everyone"
    """Anyone the provider authenticates -- not for Google.

    At a company's own provider that is everyone in the company's tenant. At
    Google it is every Google account in the world, which is never meant:
    ``HOSTED_DOMAIN`` names a Workspace, and ``EMAIL`` one person.
    """
    SUBJECT = "subject"
    """One account, by the provider's own identifier for it."""
    EMAIL = "email"
    """A verified email address, compared without regard to case."""
    EMAIL_DOMAIN = "email_domain"
    """The domain of a verified address -- not for Google.

    Google verifies personal accounts registered with any address, a work one
    included, so the domain of an address says nothing about the account.
    ``HOSTED_DOMAIN`` is the claim that does.
    """
    HOSTED_DOMAIN = "hosted_domain"
    """Google's ``hd`` claim: the Workspace an account belongs to. Google only."""
    GROUP = "group"
    """A value of the provider's groups claim, compared exactly."""


@dataclass(frozen=True, slots=True)
class AllowEntry:
    """The people of one provider that one matcher accepts, who may sign in."""

    provider: str
    matcher: Matcher
    value: str | None = None
    """What the matcher compares with; ``None`` for ``EVERYONE`` alone."""

    def __post_init__(self) -> None:
        if not self.provider:
            raise InvalidValueError("an allow entry names its provider")
        # A ``Matcher``, not a string that reads like one: ``Matcher`` is a
        # ``StrEnum``, so ``"everyone" == Matcher.EVERYONE`` while
        # ``"everyone" is Matcher.EVERYONE`` is false. An entry built from a
        # bare string would slip past the rules below and then be matched as
        # the branch its text names -- allowing everyone while carrying a
        # value nobody ever compared.
        if not isinstance(self.matcher, Matcher):
            raise InvalidValueError(f"an allow entry's matcher is a Matcher, not {self.matcher!r}")
        if self.matcher is Matcher.EVERYONE:
            if self.value is not None:
                raise InvalidValueError("allowing everyone compares with nothing")
        elif not self.value:
            raise InvalidValueError(f"an allow entry by {self.matcher.value} needs a value")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """One OpenID Connect provider people sign in with."""

    id: str
    """Its id in the configuration; it names the routes and keys a user."""
    title: str
    """What the sign-in button names it, such as ``Google``."""
    issuer: str
    """Normalised: lower-case scheme and host, no default port, no trailing slash."""
    client_id: str
    client_secret_env: str
    """The name of the environment variable holding the client secret.

    The secret itself is never in a configuration object: it is read where it
    is used, by the layer that may touch the environment.
    """
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    groups_claim: str | None = None
    """The claim holding the person's groups; ``None`` if the provider has none."""
    token_endpoint_auth: str = "client_secret_basic"

    def __post_init__(self) -> None:
        for name in ("id", "title", "issuer", "client_id", "client_secret_env"):
            if not getattr(self, name):
                raise InvalidValueError(f"a provider needs its {name}")
        # The shape is checked here as well as in ``core``, which refuses a
        # configuration naming an id of another shape. Twice, deliberately: it
        # is what reserves ``domain.LOCAL_PROVIDER`` for the local development
        # mode. That id is spelt so that no provider id can ever equal it, and
        # a ``ProviderConfig`` built in code rather than read from a file --
        # by a test, by a later step -- must not be the way round it.
        if not is_provider_id(self.id):
            raise InvalidValueError(
                f"a provider's id is a name: lower-case letters, digits, '-' and '_', at most "
                f"{MAX_PROVIDER_ID_CHARS} of them, not {self.id!r}"
            )
        object.__setattr__(self, "scopes", tuple(self.scopes))
        if "openid" not in self.scopes:
            # Without it the provider is not asked for an ID token at all.
            raise InvalidValueError("a provider's scopes must include 'openid'")
        if self.token_endpoint_auth not in TOKEN_ENDPOINT_AUTH_METHODS:
            raise InvalidValueError(
                f"token_endpoint_auth is one of "
                f"{', '.join(TOKEN_ENDPOINT_AUTH_METHODS)}, not "
                f"{self.token_endpoint_auth!r}"
            )
        if self.groups_claim is not None and not self.groups_claim:
            raise InvalidValueError("a groups_claim names a claim, or is left out")

    @property
    def is_google(self) -> bool:
        """Whether this is Google, which has rules of its own."""
        return is_google_issuer(self.issuer)


@dataclass(frozen=True, slots=True)
class SignInConfig:
    """Sign-in for a deployment: where it is served, with whom, for whom."""

    public_url: str
    """The deployment's origin, normalised: ``scheme://host[:port]``, no path."""
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    allow: tuple[AllowEntry, ...] = ()
    session_hours: float = DEFAULT_SESSION_HOURS

    def __post_init__(self) -> None:
        if not self.public_url:
            raise InvalidValueError("a sign-in configuration needs its public_url")
        if not 0 < self.session_hours <= MAX_SESSION_HOURS:
            raise InvalidValueError(
                f"session_hours must be over 0 and at most {MAX_SESSION_HOURS}, "
                f"not {self.session_hours!r}"
            )
        object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))
        object.__setattr__(self, "allow", tuple(self.allow))

    @property
    def secure(self) -> bool:
        """Whether the deployment is served over https, which cookies are marked for."""
        return self.public_url.startswith("https://")

    @property
    def session_life(self) -> timedelta:
        """How long a session lasts. Fixed: sessions are never renewed."""
        return timedelta(hours=self.session_hours)

    def provider(self, provider_id: str) -> ProviderConfig:
        """The provider of that id; ``UnknownProviderError`` if there is none."""
        try:
            return self.providers[provider_id]
        except KeyError:
            raise UnknownProviderError(f"no provider {provider_id!r} is configured") from None

    def redirect_uri(self, provider_id: str) -> str:
        """Where a provider sends the browser back to; the URI registered with it."""
        return f"{self.public_url}/auth/callback/{provider_id}"
