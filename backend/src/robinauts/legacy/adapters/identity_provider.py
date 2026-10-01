# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Talking to an OpenID Connect provider: one GET and one POST, over ``httpx``.

This is the whole of the platform's outbound sign-in traffic, and the whole of
what ``docs/specs/agents.md`` means by nothing phoning home: two requests, to
the issuer the operator configured and to the token endpoint that issuer's own
discovery document named. Nowhere else in the package may import an HTTP
client (``docs/layout.md``; an import-linter contract in
``backend/pyproject.toml`` keeps ``httpx`` under ``robinauts.legacy.adapters``).

**It decides nothing.** It hands back the mappings the provider sent, exactly
as ``ports.IdentityProvider`` describes: an adapter may not import ``core``,
and every rule of sign-in -- whose document this is, whether the endpoints are
usable, what the ID token's claims are worth -- lives in ``core`` and is
applied by the application above this line. What is decided here is only how
to speak HTTP safely:

- **timeouts**, one for the connection, one for a quiet socket, and one for
  the whole exchange, because a provider that accepts a connection and then
  says nothing for an hour is a request handler held for an hour;
- **no redirects**, ever, set on the client and again on every send. A token
  endpoint that answers ``302`` is not a token endpoint to follow: the next
  hop would be posted the authorization code, and the hop is chosen by
  whoever answered;
- **a bounded body, uncompressed**. Every request asks for
  ``Accept-Encoding: identity``, an answer that carries a content encoding all
  the same is refused unread, and what is read is the **raw** stream, counted
  and abandoned past ``max_response_bytes``. Counting after decompression
  would be counting the wrong thing: half a megabyte of gzip is sixty-seven
  megabytes of memory before anybody looks at the number. A declared
  ``Content-Length`` over the bound is refused before a byte of body is read;
- **TLS verified**, with no argument anywhere to weaken it. The client is
  made here, by this object, out of an ``ssl_context`` built here: there is
  nothing to hand in and therefore nothing to hand in wrongly. The only
  unencrypted requests possible are to loopback, which has no TLS to verify
  and which ``core.normalise_endpoint`` is what permits;
- **the client secret read at the moment it is used**, from wherever
  ``secret_for`` reads it -- the environment, in a deployment.

**Nothing raised from an exchange can print the credential.** A traceback
captured with locals -- which error reporters do -- prints every frame's
variables, and the frames of a code exchange hold the client secret, the
authorization code and the PKCE verifier; an ``httpx`` exception holds the
``Request``, whose body holds all three, and stays reachable through
``__context__`` even when it is raised ``from None``. So the exchange raises
nothing from inside itself: ``_exchanged`` **returns** either the provider's
answer or a ``_Refused`` describing what went wrong, unbinds every credential
it was given on the way out, and the error is raised in ``exchange_code``,
from a frame that holds none of them and while no exception is being handled
-- which is what leaves ``__cause__`` and ``__context__`` empty.

The client's lifetime is the deployment's, not a request's: it is made once,
in the constructor, and closed through ``aclose`` when the process stops -- as
the database pool is opened once and closed once
(``robinauts.legacy.datastore.pool``). A client per request would throw away every
connection and every TLS handshake.

Written fresh for this layout from neorc's ``neorc/auth/_oidc.py`` (Apache-2.0,
the same authors); recorded in ``docs/legal/ip-clearance.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import httpx

from robinauts.legacy.adapters.config_file import SecretLookup, environment
from robinauts.legacy.domain import (
    ProviderConfig,
    ProviderUnavailableError,
    SignInError,
    SignInErrorCode,
)
from robinauts.legacy.ports import IdentityProvider

DISCOVERY_PATH = "/.well-known/openid-configuration"
"""Where OpenID Connect Discovery 1.0 puts a provider's document, under its issuer."""

USER_AGENT = "robinauts (+https://github.com/open-shipyard/robinauts)"
"""What this deployment calls itself to a provider.

Fixed and honest: it names the program and where to read it, claims to be no
browser, and carries no version, host name or anything else about the
deployment. A provider's logs learn that Robinauts asked, and nothing more.
"""

REQUEST_HEADERS: Mapping[str, str] = {
    "accept": "application/json",
    # Asked for on every request, and enforced on every answer below. An
    # identity provider's document is a few kilobytes; compressing it saves
    # nothing worth the class of bug it opens.
    "accept-encoding": "identity",
    "user-agent": USER_AGENT,
}
"""What every request this adapter makes says, whatever else a caller wants."""

CONNECT_TIMEOUT_SECONDS = 5.0
"""How long to wait for a provider to accept a connection."""

READ_TIMEOUT_SECONDS = 10.0
"""How long a socket may stay quiet mid-answer before it is given up on."""

TOTAL_TIMEOUT_SECONDS = 20.0
"""The ceiling on one whole exchange, connection and body together.

The two above bound each step; a provider that dribbles a byte every nine
seconds passes both for as long as it likes, and this is what ends it. It is
also what bounds the person's wait, and -- through the application's
single-flight discovery -- everybody else's.
"""

MAX_RESPONSE_BYTES = 256 * 1024
"""How much of an answer is read before it is abandoned.

A quarter of a megabyte. The largest discovery document in the wild is a few
kilobytes and a token response is smaller, so this is not a limit anything
real meets; it is the limit that keeps an endless answer from being read into
memory. It counts bytes **off the wire**, which is the only count a compressed
answer cannot argue with.
"""

MAX_DETAIL_CHARS = 120
"""How much of what came from outside a message repeats, as ``core`` bounds it."""

RETRYABLE_STATUSES = frozenset({408, 425, 429})
"""The ``4xx`` answers that mean "ask again", not "no".

``408 Request Timeout``, ``425 Too Early`` and ``429 Too Many Requests`` are
the provider saying it did not deal with this request, not that it refuses the
code. The port's ``provider_unavailable`` is exactly "nobody is at fault;
trying again may work", and telling a person their sign-in was refused because
a rate limit was reached would send them to fix something that is not broken.
"""


def ssl_context(*, trust_env: bool = True) -> ssl.SSLContext:
    """The TLS every provider is reached under: certificates verified, host checked.

    Built here, rather than left to a default, so that what the deployment
    verifies with is something an operator and a test can look at.

    ``trust_env`` decides **where the trust store comes from**, never whether
    there is one: with it, ``SSL_CERT_FILE`` and ``SSL_CERT_DIR`` replace the
    CA bundle ``certifi`` ships, which is how a company's internal Okta behind
    a private CA is reached; without it, ``certifi``'s bundle is used and the
    process's environment cannot touch it. Either way the certificate is
    verified and the host name is checked.

    There is no argument to turn that off, here or anywhere else in this
    module, and that is the point: an ID token's signature is **not** checked
    (``docs/specs/sign-in.md``), so the token is worth exactly the transport it
    arrived over. A deployment that could disable verification would be a
    deployment where anyone on the path signs in as anyone.
    """
    context = httpx.create_ssl_context(trust_env=trust_env)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def open_client(
    *,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = READ_TIMEOUT_SECONDS,
    trust_env: bool = True,
) -> httpx.AsyncClient:
    """The HTTP client a deployment reaches its identity providers with.

    The one place that knows how one is made. ``HttpIdentityProvider`` calls
    it for itself, so that the settings below are not something a caller can
    get wrong: a client handed in could have been made with ``verify=False``,
    and then every promise in this module's docstring would be a promise about
    a client that no longer exists.

    ``trust_env`` is one switch for both of the things a process's environment
    can decide: the proxy to go through (``HTTPS_PROXY``, ``NO_PROXY``) and the
    trust store to verify with (``SSL_CERT_FILE``, ``SSL_CERT_DIR``). On, a
    deployment behind a corporate proxy and a private CA works. Off -- which is
    what the tests use -- a developer's own variables cannot put a request to a
    loopback stand-in through a proxy, or change what it trusts.
    """
    return httpx.AsyncClient(
        verify=ssl_context(trust_env=trust_env),
        timeout=httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        ),
        follow_redirects=False,
        headers=dict(REQUEST_HEADERS),
        trust_env=trust_env,
    )


@dataclass(frozen=True, slots=True)
class _Reply:
    """What one request came back with: an answer, or why there is none.

    A value rather than an exception, so that a request carrying a credential
    can fail without a traceback being made through the frames that hold it.
    """

    status: int = 0
    body: bytes = b""
    failure: str | None = None
    """Set when there is no answer at all; then ``status`` and ``body`` mean nothing."""
    cause: BaseException | None = None
    """The client's own exception, kept only where the request carried no credential."""


@dataclass(frozen=True, slots=True)
class _Refused:
    """Why an exchange produced no token, as one of the port's codes and a detail.

    Carried back out of ``_exchanged`` instead of being raised there: by the
    time it becomes an exception, every frame that held the client secret, the
    authorization code and the PKCE verifier has returned.
    """

    code: SignInErrorCode
    detail: str

    def error(self) -> SignInError:
        """The exception to raise -- built here, raised by the caller."""
        if self.code is SignInErrorCode.PROVIDER_UNAVAILABLE:
            return ProviderUnavailableError(self.detail)
        return SignInError(self.code, self.detail)


class HttpIdentityProvider(IdentityProvider):
    """``ports.IdentityProvider`` over an ``httpx.AsyncClient`` of its own making."""

    def __init__(
        self,
        *,
        secret_for: SecretLookup = environment,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
        total_timeout: float = TOTAL_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        trust_env: bool = True,
    ) -> None:
        """Make the client this adapter will use, and hold nothing secret.

        The client is **not** handed in. It is the one thing here that could
        be made insecure -- ``httpx.AsyncClient(verify=False)`` is a keyword
        argument away -- and an adapter whose TLS depended on what a caller
        passed would be promising something it cannot keep. What can be chosen
        is what a deployment legitimately differs about: the timeouts, the
        bound, whether the process's environment is trusted, and where the
        client secret is read from.

        ``secret_for`` is asked for a client secret by the **name** of the
        variable holding it, at the moment of the exchange. A callable rather
        than a read of ``os.environ`` written in here, so that a test scripts
        what the environment holds without setting a real variable -- and so
        that no test has to mutate the environment of the process it runs in.
        """
        self._client = open_client(
            connect_timeout=connect_timeout, read_timeout=read_timeout, trust_env=trust_env
        )
        self._secret_for = secret_for
        self._total_timeout = total_timeout
        self._max_response_bytes = max_response_bytes

    async def aclose(self) -> None:
        """Close the client, and with it every connection held open to a provider.

        Called once, when the process stops. The client is this object's own,
        so there is no question of closing it out from under another user of
        it.
        """
        await self._client.aclose()

    def __repr__(self) -> str:
        """What this is, and nothing that was ever secret.

        The secret is not held on the instance to begin with -- it is read
        when it is used and unbound with the request -- so this says so by
        having nothing to hide: the timeouts and the bound, which are
        settings, and the lookup's name rather than the lookup, whose own repr
        could name whatever it closes over.
        """
        return (
            f"{type(self).__name__}(total_timeout={self._total_timeout}s,"
            f" max_response_bytes={self._max_response_bytes},"
            f" secret_for={getattr(self._secret_for, '__name__', 'a callable')})"
        )

    async def discovery_document(self, provider: ProviderConfig) -> Mapping[str, Any]:
        """GET ``{issuer}/.well-known/openid-configuration`` and parse it.

        Every way this can go wrong is ``provider_unavailable``, as the port
        says: a refusal, a redirect, an error page, a timeout and a body that
        is not a JSON object are all "the provider answered nothing usable".
        There is no ``provider_refused`` here, because there is nothing for a
        provider to refuse: the document is public and the request carries no
        credential.

        Which is also why this one keeps the client's own exception as its
        cause. There is nothing in that request to leak, and an operator
        reading "raised ConnectError" is better off with the reason attached.
        """
        what = f"discovery for {provider.id}"
        url = f"{provider.issuer.rstrip('/')}{DISCOVERY_PATH}"
        reply = await self._answered("GET", url, what=what)
        if reply.failure is not None:
            raise ProviderUnavailableError(reply.failure) from reply.cause
        if reply.status != 200:
            raise ProviderUnavailableError(f"{what} answered {reply.status}")
        document = _json_object(reply.body, what)
        if isinstance(document, str):
            raise ProviderUnavailableError(document)
        return document

    async def exchange_code(
        self,
        provider: ProviderConfig,
        *,
        token_endpoint: str,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> Mapping[str, Any]:
        """POST the authorization-code grant to ``token_endpoint`` and return the answer.

        The client authenticates the way ``provider.token_endpoint_auth``
        says, and the PKCE verifier and the redirect URI go with the code, for
        the provider to check against what it was given when the sign-in
        began.

        Reached and refused is ``provider_refused``: a ``4xx``, whether or not
        the body carries an OAuth ``error``, because a token endpoint that
        answers ``400`` has been reached and has said no, and telling the
        person to try again would be a lie. The exceptions are
        ``RETRYABLE_STATUSES``, where trying again is exactly the answer. Not
        reached, or reached and incoherent -- a transport failure, a timeout,
        ``5xx``, a redirect, a compressed or oversized body, a body that is not
        a JSON object -- is ``provider_unavailable``.

        Everything that touches the secret happens in ``_exchanged``, which
        returns rather than raises. This frame drops the code and the verifier
        before it raises anything, so that the traceback of what comes out of
        here holds no credential in any frame, and raises while no exception is
        being handled, so that nothing is reachable through ``__context__``
        either.
        """
        what = f"the token endpoint of {provider.id}"
        try:
            outcome = await self._exchanged(
                provider,
                what,
                token_endpoint=token_endpoint,
                code=code,
                verifier=verifier,
                redirect_uri=redirect_uri,
            )
        finally:
            # Unbound however this ends, cancellation included: a traceback
            # through this frame must not be able to print them.
            del code, verifier, redirect_uri
        if isinstance(outcome, _Refused):
            raise outcome.error()
        return outcome

    async def _exchanged(
        self,
        provider: ProviderConfig,
        what: str,
        *,
        token_endpoint: str,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> Mapping[str, Any] | _Refused:
        """The exchange itself. It answers; it does not raise.

        Every failure comes back as a ``_Refused``, and every credential this
        frame was given or built is unbound before it returns -- so that this
        frame is worth nothing to whoever reads a traceback, whichever way the
        request ended.
        """
        secret = ""
        form: dict[str, str] = {}
        headers: dict[str, str] = {}
        try:
            secret = self._secret_for(provider.client_secret_env) or ""
            if not secret:
                # It should be unreachable: ``config_file.check_client_secrets``
                # refuses to start a deployment whose variables are unset, and
                # names every one of them at once. Getting here means the
                # environment changed under a running process, which an
                # operator fixes and after which signing in works again. The
                # variable's NAME is in the configuration and goes in the
                # message; its value is the secret.
                return _Refused(
                    SignInErrorCode.PROVIDER_UNAVAILABLE,
                    f"the client secret of {provider.id} is not in the environment:"
                    f" {provider.client_secret_env} is unset or empty",
                )
            form = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
            if provider.token_endpoint_auth == "client_secret_post":
                form["client_id"] = provider.client_id
                form["client_secret"] = secret
            else:
                headers["authorization"] = _basic(provider.client_id, secret)
            return _token_response(
                what,
                await self._answered(
                    "POST",
                    token_endpoint,
                    what=what,
                    data=form,
                    headers=headers,
                    keep_cause=False,
                ),
            )
        finally:
            secret = ""
            form.clear()
            headers.clear()
            del secret, form, headers, code, verifier, redirect_uri

    async def _answered(
        self,
        method: str,
        url: str,
        *,
        what: str,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        keep_cause: bool = True,
    ) -> _Reply:
        """One request, with every bound this adapter puts on one. It does not raise.

        The body comes back as bytes rather than parsed, because what a status
        code means depends on the caller: a ``400`` from the token endpoint is
        a refusal to be read, and a ``400`` from discovery is a provider that
        answered nothing usable.

        ``keep_cause`` is ``False`` for the token exchange alone. An ``httpx``
        exception holds the ``Request`` that failed, and that request's body
        holds the client secret, the authorization code and the PKCE verifier;
        keeping it would put all three back within reach of anything that
        walks an exception. What is lost is the client's own wording, and its
        type -- which is the useful half -- is in the message instead.

        The last clause is for what must **not** be turned into an answer. A
        cancelled task -- the browser went away mid-sign-in -- and a
        ``KeyboardInterrupt`` are not a provider failing, and a mistake such
        as using a closed client is a bug to see, not a sign-in error. Each is
        re-raised as **itself**, by a bare ``raise``: same exception, same
        type, same message, same ``__cause__``.

        What is dropped is the **traceback**, of it and of everything chained
        to it. Those frames are the HTTP client's, and each holds the
        ``Request`` -- whose repr is only a method and a URL, while its
        ``content`` and its ``headers`` are the form and the ``Authorization``
        header, one attribute away from anybody holding the traceback. It is
        reachability that is being closed off, as with ``__context__`` above,
        not something a printer happened to print.
        """
        request = None
        response = None
        try:
            async with asyncio.timeout(self._total_timeout):
                request = self._client.build_request(
                    method, url, data=data, headers={**(headers or {}), **REQUEST_HEADERS}
                )
                # Again here, and not only on the client: following a redirect
                # from a token endpoint would post the code to whoever
                # answered, and this is the line that says it never happens.
                response = await self._client.send(request, stream=True, follow_redirects=False)
                try:
                    return await self._bounded_body(response, what)
                finally:
                    await response.aclose()
        except TimeoutError as exc:
            # TimeoutError is an OSError, so it is caught before the clause
            # below; asyncio.timeout raises this one when the ceiling is hit.
            return _Reply(
                failure=f"{what} took longer than {self._total_timeout}s",
                cause=exc if keep_cause else None,
            )
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            # The exception's type, never its message: what a failed request
            # carries is the request.
            return _Reply(
                failure=f"{what} raised {type(exc).__name__}",
                cause=exc if keep_cause else None,
            )
        except BaseException as exc:
            # Neither converted nor swallowed: the bare ``raise`` re-raises
            # this very exception, so cancellation and KeyboardInterrupt go on
            # being themselves. Only the tracebacks go, and with them every
            # httpx frame holding the request.
            _stripped(exc)
            raise
        finally:
            # The form and the header are the credential, and so are the
            # request built out of them and the response that points back at
            # it. None of them stays in this frame -- which is not an idle
            # precaution: a cause kept for discovery has a traceback, and this
            # frame is the top of it.
            del data, headers, request, response

    async def _bounded_body(self, response: httpx.Response, what: str) -> _Reply:
        """The answer's body off the wire, or why it was not read.

        Three refusals before anything is read into memory, and one while it
        is. A **content encoding** we did not ask for, because inflating it is
        how a small answer becomes a large one and there is no reason for a
        provider to send one; a declared **length** over the bound, because
        there is nothing to learn by reading it; and the bound itself, on the
        raw stream, for an answer that declares nothing or declares wrongly.
        """
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            return _Reply(
                failure=f"{what} answered with content-encoding {_shown(encoding)}, which"
                f" this deployment does not ask for and will not inflate"
            )
        declared = _length(response.headers.get("content-length"))
        if declared is not None and declared > self._max_response_bytes:
            return _Reply(
                failure=f"{what} declared {declared} bytes, over the"
                f" {self._max_response_bytes} that are read from it"
            )
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > self._max_response_bytes:
                return _Reply(
                    failure=f"{what} answered more than the {self._max_response_bytes}"
                    f" bytes that are read from it"
                )
            chunks.append(chunk)
        return _Reply(status=response.status_code, body=b"".join(chunks))


def _stripped(error: BaseException) -> None:
    """Clear the traceback of ``error`` and of everything chained to it.

    Nothing else about the exception changes: not its type, not its message,
    not its ``__cause__`` or ``__context__``. What goes is where it came from,
    and that is the point -- the frames of a failed request are the HTTP
    client's, and every one of them has the ``Request`` in its locals, so a
    reporter that captures locals prints the ``Authorization`` header, the
    authorization code and the PKCE verifier out of an exception that was
    never about any of them.

    Re-raised after this, the exception's traceback starts at the frame that
    re-raised it.
    """
    seen: set[int] = set()
    chain: list[BaseException | None] = [error]
    while chain:
        current = chain.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        current.__traceback__ = None
        chain += [current.__cause__, current.__context__]


def _token_response(what: str, reply: _Reply) -> Mapping[str, Any] | _Refused:
    """What the token endpoint's answer amounts to: a token response, or a refusal.

    A plain function, so that reading the status has no frame holding a
    credential in it at all.
    """
    if reply.failure is not None:
        return _Refused(SignInErrorCode.PROVIDER_UNAVAILABLE, reply.failure)
    status = reply.status
    if status >= 500:
        return _Refused(SignInErrorCode.PROVIDER_UNAVAILABLE, f"{what} answered {status}")
    if status in RETRYABLE_STATUSES:
        return _Refused(
            SignInErrorCode.PROVIDER_UNAVAILABLE,
            f"{what} answered {status}, which asks for the request to be made again",
        )
    if status >= 400:
        return _Refused(SignInErrorCode.PROVIDER_REFUSED, _refusal(what, status, reply.body))
    if status != 200:
        return _Refused(SignInErrorCode.PROVIDER_UNAVAILABLE, f"{what} answered {status}, not 200")
    document = _json_object(reply.body, what)
    if isinstance(document, str):
        return _Refused(SignInErrorCode.PROVIDER_UNAVAILABLE, document)
    if "error" in document:
        # Some providers answer an OAuth error with 200. It is still a no.
        return _Refused(SignInErrorCode.PROVIDER_REFUSED, _refusal(what, status, reply.body))
    return document


def _basic(client_id: str, secret: str) -> str:
    """The ``Authorization`` header of ``client_secret_basic`` (RFC 6749 2.3.1).

    Each half is form-encoded **before** the two are joined by a colon and
    base64'd, which is what the specification asks for and what makes a secret
    holding a colon work at all: without it, a provider splitting on the first
    colon would read half a secret and a client id nobody registered.
    Form-encoding is the one HTML forms use, where a space is ``+``.
    """
    pair = f"{quote_plus(client_id)}:{quote_plus(secret)}"
    return "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")


def _json_object(body: bytes, what: str) -> Mapping[str, Any] | str:
    """The body as a JSON object, or -- as a string -- why it is not one.

    It returns its complaint rather than raising it, because one of its two
    callers is on the path that may not raise.

    ``RecursionError`` is caught beside ``ValueError``: forty kilobytes of
    ``[`` is well inside any sane size bound and takes ``json`` past the
    interpreter's recursion limit, and a ``RecursionError`` out of an adapter
    is not one of the port's two codes -- it is a five hundred, and a
    traceback through every frame that got there.

    What was in the body is not repeated. It came from outside and is as long
    and as strange as whoever sent it liked.
    """
    try:
        document = json.loads(body)
    except RecursionError:
        return f"{what} answered JSON nested deeper than it will be read"
    except ValueError:
        return f"{what} answered something that is not JSON"
    if not isinstance(document, dict):
        return f"{what} answered {type(document).__name__}, not a JSON object"
    return document


def _refusal(what: str, status: int, body: bytes) -> str:
    """What to log about a token endpoint that said no.

    The provider's own two words, ``error`` and ``error_description``, each
    kept short, because the detail goes to the log and the browser is told the
    code alone (``docs/specs/sign-in.md``). Nothing of the request is repeated
    -- not the secret, not the code, not the verifier -- and a body that is
    not a JSON object is not repeated either.
    """
    try:
        answer = json.loads(body)
    except (ValueError, RecursionError):
        answer = None
    if not isinstance(answer, dict):
        return f"{what} answered {status} with nothing that reads as an OAuth error"
    return (
        f"{what} answered {status}: error={_shown(answer.get('error'))}"
        f" error_description={_shown(answer.get('error_description'))}"
    )


def _length(declared: str | None) -> int | None:
    """A ``Content-Length`` header as a number, or ``None`` if it is not one."""
    if declared is None:
        return None
    try:
        return int(declared)
    except ValueError:
        return None


def _shown(value: object) -> str:
    """A value from outside, as a message may repeat it: its repr, kept short."""
    try:
        text = repr(value)
    except Exception:  # pragma: no cover -- nothing off a socket lacks a repr
        return "<unreadable>"
    return text if len(text) <= MAX_DETAIL_CHARS else text[:MAX_DETAIL_CHARS] + "..."
