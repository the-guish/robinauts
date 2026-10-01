# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Signing in, and out: the flow, from the button to a session.

Three calls make a sign-in, and the browser is between them:

1. ``begin`` -- where to send the person, and the ``state`` their browser is
   to carry. What the callback will need is stored under the SHA-256 of that
   ``state``, for ten minutes, once.
2. ``complete`` -- the provider sent the browser back. The ``state`` in the
   query must be the one in the cookie, the sign-in is taken (which is what
   makes it single use), the code is exchanged, the ID token's claims are
   checked, the allow list is asked, a user is found or made and a session is
   opened.
3. ``resolve_session`` on every request afterwards, until ``sign_out`` or the
   session's fixed life ends it.

Every refusal is a ``SignInError`` carrying one of the spec's eight codes
(``docs/specs/sign-in.md``). The code is all the browser is told; the detail
is for the log, and may repeat what a provider said.

This is the flow alone. The rules it applies are ``core``'s -- the claims, the
allow list, the published issuer -- and everything it touches outside the
process is a port: the clock, the source of secrets, the credential store and
the identity provider, all handed in, none constructed here
(``docs/layout.md``).

The design is neorc's, re-implemented for this layout; recorded in
``docs/legal/ip-clearance.md``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from robinauts.legacy.core import (
    AUTHORIZATION_PARAMETERS,
    DEFAULT_RETURN_TO,
    MAX_CODE_CHARS,
    MAX_PKCE_CHARS,
    TOKEN_PARAMETERS,
    UNRESERVED,
    authorization_url,
    check_published_issuer,
    checked_secret,
    identity_from_id_token,
    is_allowed,
    is_secret_shaped,
    normalise_endpoint,
    parameters_taken,
    safe_return_to,
    same_secret,
    secret_hash,
    verified_email,
)
from robinauts.legacy.domain import (
    LOCAL_PROVIDER,
    MAX_PENDING_LOGINS,
    PENDING_LOGIN_MINUTES,
    InvalidIdTokenError,
    InvalidValueError,
    NotAllowedError,
    PendingLogin,
    ProviderConfig,
    ProviderUnavailableError,
    RobinautsError,
    Session,
    SignInConfig,
    SignInError,
    SignInErrorCode,
    User,
)
from robinauts.legacy.ports import Clock, CredentialStore, IdentityProvider, SecretSource

SWEEP_SECONDS = 60.0
"""How often, at most, expired sessions and sign-ins are deleted.

They go as people sign in: the deployment needs no job of its own, and a flood
of sign-ins does not become a flood of deletes.
"""

PENDING_LOGIN_LIFE = timedelta(minutes=PENDING_LOGIN_MINUTES)
"""How long a sign-in may take between the button and the callback."""

_ENDPOINTS = ("authorization_endpoint", "token_endpoint")

_SHOWN = 120
"""How much of what came from outside a message repeats; the bound ``core`` uses.

A query parameter, a provider's error, a URL in a discovery document: each is
as long as whoever sent it cared to make it, and each ends up in a log line.
"""


@dataclass(frozen=True, slots=True)
class ProviderEndpoints:
    """The two URLs of a provider, out of a discovery document that passed its checks."""

    authorization: str
    """Where the browser is sent to sign in."""
    token: str
    """Where the authorization code is exchanged; the ID token comes from here."""


@dataclass(frozen=True, slots=True)
class BegunSignIn:
    """A sign-in waiting for its callback: where to send the browser, and with what.

    Only ``expires_at`` survives into the repr. The other two are secret, and
    the second of them is not obviously so: the authorization URL carries the
    ``state``, the ``nonce`` and the PKCE challenge in its query, so printing
    it prints them.
    """

    authorization_url: str = field(repr=False)
    """Where to send the browser; it carries this sign-in's state and nonce."""
    state: str = field(repr=False)
    """The raw ``state``, for the browser's cookie; the store has its hash alone.

    Out of the repr: a log line or a traceback carrying it would let whoever
    reads it finish someone else's sign-in.
    """
    expires_at: datetime
    """When the sign-in stops being usable; also how long its cookie lives."""


@dataclass(frozen=True, slots=True)
class OpenedSession:
    """A finished sign-in: who it is, their session, and where they were going."""

    secret: str = field(repr=False)
    """The raw session secret, for the cookie. Never stored, never shown again.

    Out of the repr for the same reason as ``BegunSignIn.state``: with it,
    whoever holds it is signed in as this person.
    """
    session: Session
    user: User
    return_to: str
    """A relative target within this deployment, checked; never an open redirect."""


class SignIn:
    """The sign-in flow of one deployment, over the ports it was handed."""

    def __init__(
        self,
        config: SignInConfig,
        *,
        credentials: CredentialStore,
        provider: IdentityProvider,
        clock: Clock,
        secrets: SecretSource,
        max_pending_logins: int = MAX_PENDING_LOGINS,
        sweep_seconds: float = SWEEP_SECONDS,
    ) -> None:
        if max_pending_logins < 1:
            raise InvalidValueError("at least one sign-in must be allowed at once")
        if sweep_seconds < 0:
            raise InvalidValueError("a sweep interval is not negative")
        self._config = config
        self._credentials = credentials
        self._provider = provider
        self._clock = clock
        self._secrets = secrets
        self._max_pending_logins = max_pending_logins
        self._sweep_seconds = sweep_seconds
        self._swept_at: float | None = None
        self._sweep_failures = 0
        self._last_sweep_failure: BaseException | None = None
        self._endpoints: dict[str, ProviderEndpoints] = {}
        self._discovering: dict[str, asyncio.Task[ProviderEndpoints]] = {}

    @property
    def discovering(self) -> frozenset[str]:
        """The providers whose discovery is in flight right now.

        Diagnostics: what an operator would want on a health page, and what a
        test needs to tell a caller waiting on somebody else's fetch from one
        that found the answer already cached.
        """
        return frozenset(
            provider_id for provider_id, fetch in self._discovering.items() if not fetch.done()
        )

    @property
    def config(self) -> SignInConfig:
        """The deployment's sign-in configuration: its providers and its allow list."""
        return self._config

    @property
    def pending_login_life(self) -> timedelta:
        """How long a sign-in begun now may take."""
        return PENDING_LOGIN_LIFE

    @property
    def session_life(self) -> timedelta:
        """How long a session opened now lasts. Fixed: sessions are not renewed."""
        return self._config.session_life

    # Discovery.

    async def endpoints(self, provider: ProviderConfig) -> ProviderEndpoints:
        """The provider's endpoints, discovered at the first sign-in and kept.

        Lazy, as ``docs/specs/sign-in.md`` requires: a provider that is down
        keeps the deployment from starting no more than it keeps the other
        providers from working. A success is kept for the life of the process,
        a failure is not, so the next sign-in tries again.

        **One fetch at a time per provider, and everyone waiting shares it.**
        Not a lock around the fetch: behind a lock, a provider that takes ten
        seconds to fail makes the tenth person queued wait a minute and a
        half, and the hundredth a quarter of an hour, each of them paying for
        a fetch that had already failed. Here they all wait on the one
        attempt and are all told what it said -- and only a caller arriving
        **after** it has finished starts another.

        A waiter that is cancelled takes nothing with it: it waits on a
        shield, so the fetch the others are sharing goes on.
        """
        found = self._endpoints.get(provider.id)
        if found is not None:
            return found
        fetch = self._discovering.get(provider.id)
        if fetch is not None and fetch.done():
            # It ended without getting as far as its own ``finally`` -- a task
            # cancelled before its first step never runs one. Left there, it
            # would be handed to every later caller for the life of the
            # process, each of them told the sign-in was cancelled.
            self._discovering.pop(provider.id, None)
            fetch = None
        if fetch is None:
            # Created and registered with no await in between, so that
            # whoever comes next finds this one rather than starting a second.
            fetch = asyncio.get_running_loop().create_task(self._discovered(provider))
            self._discovering[provider.id] = fetch
            fetch.add_done_callback(_asked)
        return await asyncio.shield(fetch)

    async def _discovered(self, provider: ProviderConfig) -> ProviderEndpoints:
        """The one fetch the waiters share; it forgets itself however it ends.

        The entry goes in the ``finally``, which runs the moment this
        coroutine ends and before any waiter is woken: a failure is therefore
        never left behind as an in-flight fetch to join, nor cached as an
        answer.
        """
        try:
            endpoints = self._checked(provider, await self._fetched(provider))
            self._endpoints[provider.id] = endpoints
            return endpoints
        finally:
            self._discovering.pop(provider.id, None)

    async def _fetched(self, provider: ProviderConfig) -> object:
        """The provider's discovery document, whatever the port does about failing.

        The port's contract is that it raises one of two sign-in errors, and
        the adapter that will implement it does. An adapter is a place where a
        timeout, a socket error or a bad certificate is a fact of the day,
        though, and one escaping as itself would leave a sign-in with no code
        for the browser and a stack trace for the operator. Anything that is
        not ours becomes ``provider_unavailable``, with the original kept as
        the cause.

        Only what a network does: ``OSError`` (a reset connection, a refused
        one, a certificate that did not verify) and ``TimeoutError``, which is
        what ``asyncio`` raises as well. A ``TypeError`` or a ``KeyError`` out
        of an adapter is a mistake in the adapter, and dressing it up as a
        provider being unavailable would hide it behind a sign-in page for as
        long as nobody read the logs closely.

        The exception's own message is **not** repeated: an HTTP client's
        exception may carry the request that failed, and that request carries
        the authorization code and the PKCE verifier. Its type is enough for
        the log line; the whole of it is the chained cause.

        ``asyncio.CancelledError`` is a ``BaseException`` and passes straight
        through: a cancelled request is not a provider that failed.
        """
        try:
            return await self._provider.discovery_document(provider)
        except RobinautsError:
            raise
        except (OSError, TimeoutError) as exc:
            raise ProviderUnavailableError(
                f"discovery for {provider.id} raised {type(exc).__name__}"
            ) from exc

    def _checked(self, provider: ProviderConfig, document: object) -> ProviderEndpoints:
        """The endpoints of a discovery document, once it has earned being believed.

        The port fetches; this decides. The issuer must be the configured one
        (``core.check_published_issuer``), or we cannot tell whose document
        this is; and both endpoints must be ``https``, loopback excepted,
        because an ID token whose signature is not checked is worth exactly
        the transport it arrived over (``core.claims``).
        """
        if not isinstance(document, Mapping):
            raise ProviderUnavailableError(
                f"discovery for {provider.id} answered {type(document).__name__}, not an object"
            )
        check_published_issuer(provider, document.get("issuer"))
        authorization, token = (self._endpoint(provider, document, key) for key in _ENDPOINTS)
        for endpoint, key, ours in (
            (authorization, _ENDPOINTS[0], AUTHORIZATION_PARAMETERS),
            (token, _ENDPOINTS[1], TOKEN_PARAMETERS),
        ):
            taken = parameters_taken(endpoint, ours)
            if taken:
                raise ProviderUnavailableError(
                    f"discovery for {provider.id} names a {key} whose own query sets "
                    f"{', '.join(taken)}, which the request to it sets"
                )
        return ProviderEndpoints(authorization=authorization, token=token)

    @staticmethod
    def _endpoint(provider: ProviderConfig, document: Mapping[str, Any], key: str) -> str:
        """One endpoint of a discovery document, checked and rebuilt.

        ``core.normalise_endpoint`` does the deciding, and hands back the URL
        made again out of the pieces it checked -- so that what a browser is
        redirected to, or what a code is posted to, is never the string a
        provider sent.
        """
        value = document.get(key)
        if not isinstance(value, str):
            raise ProviderUnavailableError(
                f"discovery for {provider.id} names {key} {_shown(value)}, which is not a URL"
            )
        try:
            return normalise_endpoint(value)
        except InvalidValueError as exc:
            # The reason, not the exception's message: that message repeats
            # the URL whole, and the URL is as long as whoever sent it liked.
            raise ProviderUnavailableError(
                f"discovery for {provider.id} names {key} {_shown(value)}, which is not "
                f"an endpoint this deployment will use: https, or http on loopback, "
                f"with no user name, fragment or whitespace"
            ) from exc

    # Beginning.

    async def begin(self, provider_id: str, *, return_to: str | None = None) -> BegunSignIn:
        """Start a sign-in with that provider: where to send the browser, and the state.

        ``UnknownProviderError`` when no provider of that id is configured,
        ``ProviderUnavailableError`` when discovery cannot be believed, and
        ``SignInError(BUSY)`` when too many sign-ins are in progress already.

        Discovery comes before anything is stored: a provider that cannot be
        reached leaves no row behind a sign-in that could never finish.
        """
        provider = self._config.provider(provider_id)
        endpoints = await self.endpoints(provider)
        await self._swept()
        now = self._clock.now()
        state = checked_secret(self._secrets.secret(), "state")
        login = PendingLogin(
            provider=provider.id,
            nonce=checked_secret(self._secrets.secret(), "nonce"),
            verifier=checked_secret(
                self._secrets.pkce_verifier(),
                "PKCE verifier",
                most=MAX_PKCE_CHARS,
                alphabet=UNRESERVED,
            ),
            created_at=now,
            expires_at=now + PENDING_LOGIN_LIFE,
            return_to=safe_return_to(return_to),
        )
        stored = await self._credentials.add_pending_login(
            secret_hash(state), login, limit=self._max_pending_logins, now=now
        )
        if not stored:
            raise SignInError(
                SignInErrorCode.BUSY,
                f"{self._max_pending_logins} sign-ins are already in progress",
            )
        return BegunSignIn(
            authorization_url=authorization_url(
                endpoints.authorization,
                provider,
                state=state,
                nonce=login.nonce,
                verifier=login.verifier,
                redirect_uri=self._redirect_uri(provider),
            ),
            state=state,
            expires_at=login.expires_at,
        )

    # Finishing.

    async def complete(
        self,
        provider_id: str,
        *,
        state: str | None,
        cookie_state: str | None,
        code: str | None = None,
        error: str | None = None,
    ) -> OpenedSession:
        """Finish the sign-in the provider sent the browser back from.

        ``state`` is the query parameter and ``cookie_state`` the cookie set
        when the sign-in began; they are compared in constant time, and a
        callback that carries one without the other is not this browser's.
        ``error`` and ``code`` are the provider's answer: exactly one of them
        is expected.
        """
        provider = self._config.provider(provider_id)
        if state is None or cookie_state is None or not same_secret(state, cookie_state):
            raise SignInError(
                SignInErrorCode.STATE_MISMATCH,
                "the callback's state is not the one this browser began the sign-in with",
            )
        login = await self._credentials.take_pending_login(secret_hash(state))
        now = self._clock.now()
        if login is None:
            raise SignInError(
                SignInErrorCode.EXPIRED, "the sign-in is unknown, already used or expired"
            )
        if login.has_expired(now):
            raise SignInError(
                SignInErrorCode.EXPIRED,
                f"the sign-in expired at {login.expires_at.isoformat()}",
            )
        if login.provider != provider.id:
            # The state is ours, and it is not this callback's: a sign-in
            # begun with one provider cannot be finished at another's route.
            raise SignInError(
                SignInErrorCode.STATE_MISMATCH,
                f"the sign-in was begun with provider {_shown(login.provider)}, "
                f"not {provider.id!r}",
            )
        if error is not None or not code:
            raise SignInError(
                SignInErrorCode.PROVIDER_REFUSED,
                f"{provider.id} answered error={_shown(error)} with code={_shown(code)}",
            )
        if len(code) > MAX_CODE_CHARS:
            # It came out of a query parameter, so it is as long as whoever
            # wrote the link cared to make it. Refused here, rather than
            # posted to a provider and written to a log on the way.
            raise SignInError(
                SignInErrorCode.PROVIDER_REFUSED,
                f"{provider.id} answered with a code of {len(code)} characters, "
                f"over the {MAX_CODE_CHARS} accepted: {_shown(code)}",
            )
        endpoints = await self.endpoints(provider)
        response = await self._exchanged(provider, endpoints, code=code, verifier=login.verifier)
        if not isinstance(response, Mapping):
            # Not a token that is wrong: an answer that is not a token
            # response at all, which is the port's own failure to name.
            raise ProviderUnavailableError(
                f"the token endpoint of {provider.id} answered "
                f"{type(response).__name__}, not an object"
            )
        id_token = response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise InvalidIdTokenError("the token response carries no id_token")
        # The clock is read again: the exchange went over the network, and the
        # token's expiry is about now, not about when the callback arrived.
        signed_in_at = self._clock.now()
        identity = identity_from_id_token(id_token, provider, nonce=login.nonce, now=signed_in_at)
        if not is_allowed(identity, self._config.allow):
            who = identity.email or identity.subject
            raise NotAllowedError(
                f"no allow entry lets {_shown(who)} of {identity.provider!r} sign in"
            )
        user = await self._credentials.user_at_sign_in(
            identity.provider,
            identity.subject,
            name=identity.name,
            # An address the provider did not verify is not the person's: it
            # is not kept, and nothing is ever sent to it.
            email=verified_email(identity),
            now=signed_in_at,
        )
        secret = checked_secret(self._secrets.secret(), "session secret")
        session = await self._credentials.add_session(
            secret_hash(secret),
            user.id,
            created_at=signed_in_at,
            expires_at=signed_in_at + self._config.session_life,
        )
        return OpenedSession(
            secret=secret,
            session=session,
            user=user,
            # Checked again, and not only when it was stored: a target that
            # reaches here from anywhere but ``begin`` is still not allowed to
            # be another origin.
            return_to=safe_return_to(login.return_to) or DEFAULT_RETURN_TO,
        )

    async def _exchanged(
        self,
        provider: ProviderConfig,
        endpoints: ProviderEndpoints,
        *,
        code: str,
        verifier: str,
    ) -> object:
        """The token endpoint's answer, whatever the port does about failing.

        As ``_fetched``: a transport failure becomes ``provider_unavailable``
        rather than escaping as a socket error, and nothing else is caught.
        The message is the exception's type alone -- what a failed POST to a
        token endpoint carries is the authorization code.
        """
        try:
            return await self._provider.exchange_code(
                provider,
                token_endpoint=endpoints.token,
                code=code,
                verifier=verifier,
                redirect_uri=self._redirect_uri(provider),
            )
        except RobinautsError:
            raise
        except (OSError, TimeoutError) as exc:
            raise ProviderUnavailableError(
                f"the token endpoint of {provider.id} raised {type(exc).__name__}"
            ) from exc

    # Afterwards.

    async def resolve_session(self, secret: str | None) -> User | None:
        """Who a session cookie stands for; ``None`` if it stands for nobody.

        ``None`` for an unknown, ended or expired session, and for a cookie
        that is not one of ours at all. A caller has no use for the
        difference, and the browser is told none of it: a request without a
        principal is simply not signed in.
        """
        if not is_secret_shaped(secret):
            return None
        session = await self._credentials.session_by_hash(
            secret_hash(secret), now=self._clock.now()
        )
        if session is None:
            return None
        user = await self._credentials.user_by_id(session.user_id)
        if user is not None and user.provider == LOCAL_PROVIDER:
            # Nothing in a sign-in can make this row: its provider is spelt so
            # that no configuration may name it and no identity may carry it
            # (``domain.local``). A session that points at it therefore came
            # from somewhere that is not a sign-in -- a database kept from a
            # run of the local development mode, a row somebody wrote -- and
            # the local user is not somebody to be signed in as. (Nothing is
            # logged: the application has no logging port, as ``sweep`` says.)
            return None
        return user

    async def sign_out(self, secret: str | None) -> bool:
        """End the session that cookie holds; whether there was one to end.

        The provider is not signed out of: the specification says so, and
        nothing here could do it.
        """
        if not is_secret_shaped(secret):
            return False
        return await self._credentials.delete_session(secret_hash(secret))

    async def sweep(self) -> None:
        """Delete expired sessions and sign-ins, at most once every ``SWEEP_SECONDS``.

        Called as people sign in. The interval is measured on the monotonic
        clock, so a wall clock stepped backwards does not stop the sweeping
        for as long as the step.

        The interval is recorded **after** the deletes, and read from the
        clock again rather than taken from before them: a sweep that failed is
        a sweep that has not happened, and recording it first would buy the
        store a minute of not being asked again. The next one is due a minute
        after this one ended, not a minute after it started. The cost is
        that two sign-ins arriving while a slow sweep runs may each start one;
        deleting rows that have expired twice over is not a problem, and the
        deletes are the same deletes.

        Whatever the store raises comes out of here. The sign-in flow calls
        this through ``_swept``, which is where it is decided that a sweep
        failing is not a sign-in failing.
        """
        ticks = self._clock.monotonic()
        if self._swept_at is not None and ticks - self._swept_at < self._sweep_seconds:
            return
        now = self._clock.now()
        await self._credentials.delete_expired_pending_logins(now=now)
        await self._credentials.delete_expired_sessions(now=now)
        # Read again, not the reading from before the deletes: what is being
        # recorded is when the sweeping finished.
        self._swept_at = self._clock.monotonic()

    async def _swept(self) -> None:
        """Sweep, and let a sign-in go on if the sweep could not.

        Sweeping is housekeeping someone else's sign-in pays for. A store that
        refuses the two deletes -- a lock held, a statement timeout, a replica
        that is read-only for the minute -- must not be the reason a person
        cannot sign in, when the sign-in itself needs none of it.

        There is no logging port yet, so the failure is counted and the last
        one kept: ``sweep_failures`` is how an operator, and a test, sees that
        this is happening rather than nothing at all. ``CancelledError`` is a
        ``BaseException`` and is not caught.
        """
        try:
            await self.sweep()
        except Exception as exc:
            self._sweep_failures += 1
            self._last_sweep_failure = exc

    @property
    def sweep_failures(self) -> int:
        """How many sweeps have failed and been carried on from."""
        return self._sweep_failures

    @property
    def last_sweep_failure(self) -> BaseException | None:
        """What the last failed sweep raised, until there is somewhere to log it."""
        return self._last_sweep_failure

    def _redirect_uri(self, provider: ProviderConfig) -> str:
        """Where this provider sends the browser back; registered with it."""
        return self._config.redirect_uri(provider.id)


def _shown(value: object) -> str:
    """A value from outside, as a message may repeat it: its repr, kept short."""
    try:
        text = repr(value)
    except Exception:  # pragma: no cover -- nothing sent over the wire lacks a repr
        return "<unreadable>"
    return text if len(text) <= _SHOWN else text[:_SHOWN] + "..."


def _asked(fetch: asyncio.Task[ProviderEndpoints]) -> None:
    """Read a finished discovery, in case every waiter on it was cancelled.

    An exception nobody ever asks a task for is reported by the loop at
    collection as one that was never retrieved, which would be a warning about
    a provider being down rather than about anything wrong here.
    """
    if not fetch.cancelled():
        fetch.exception()
