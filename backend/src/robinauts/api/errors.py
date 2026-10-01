# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""How a platform error crosses HTTP: one status per class, one body shape.

Every ``RobinautsError`` answers with the status its class is listed under and
a body naming the class::

    {"error": "AuthenticationError", "detail": "sign in at /ui/"}

The class name is what a client branches on -- several classes share a status,
and "403" does not say whether to sign in again or to give up -- so it is sent
rather than left to be guessed from the status. Starlette's own refusals, a
404 and a 405, are put in the same shape, so that a client has one kind of
error body to understand and not two.

**One answer for everything that is not there.** A conversation that does not
exist, a message that does not, a run that does not, and one that exists and
belongs to somebody else all answer the *same* body -- ``NotFoundError`` and
one fixed sentence -- with the same status and the same headers. The
difference between "no such id" and "not yours" is the whole of what an
attacker wants from an id, and a body that named the class would hand it over
while the status hid it. Which of them it really was goes to the log.

**A body never repeats what the request carried.** Three rules, and each of
them exists because something once did:

- **nothing that answers 5xx says why.** A 500 is a mistake of ours, and its
  message is written for an operator: it may hold a query, a row, the name of
  a variable. The browser is told that the request could not be served, and
  the whole of it goes to the log -- **with the chain of causes and the frames
  of the innermost one**, because an error of ours often says only what kind
  of thing went wrong, and because a reader that turns anything it meets into
  one sentence turns a bug of ours into it too (``chain``, ``where``). The
  same for an exception that is not ours at all, which is the ordinary way a
  bug becomes a response;
- **a sign-in failure says one fixed sentence per code.** A ``SignInError``'s
  own ``detail`` is written for the log: it repeats a provider's words and the
  values a request carried. The routes answer these with a redirect and never
  reach here, and one that does is still not going to reflect its input back;
- **a request that could not be read names the field and the rule**, never
  the value. Pydantic's error carries the ``input`` it refused, which is the
  very thing somebody submitted -- and its ``msg`` often quotes it too
  ("invalid character: found `Z` at 1"), so **no message of pydantic's is
  passed on**: each of its error types has a sentence of ours
  (``UNREADABLE_RULES``), and a type nothing here has read about gets the one
  fallback sentence rather than its own words. The **name of a field nobody
  knows is the request's too** -- a body may be sent with a key that is a
  token -- so those are counted and not named (``unknown_fields``).

The status table is exhaustive on purpose, and a test says so: a new error
class in ``robinauts.domain`` that nobody gave a status to fails the suite
rather than quietly answering 500.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# The one walk of what an application serves lives in ``access``, which reads
# every list a router keeps routes in and goes through everything that holds
# more of them. A second walk here would be a second thing to keep in step
# with the framework, and the two would disagree the day one of them learnt
# about something the other had not.
from robinauts.api.access import api_routes
from robinauts.api.logs import shown
from robinauts.domain import (
    MAX_LOGGED,
    AuthenticationError,
    CheckpointNotFoundError,
    ConfigError,
    ConversationNotFoundError,
    CrossSiteRequestError,
    DatabaseUnreachableError,
    IllegalTransitionError,
    InvalidCursorError,
    InvalidIdTokenError,
    InvalidMessageTreeError,
    InvalidValueError,
    MessageNotFoundError,
    ModelNotOfferedError,
    NotAllowedError,
    NotFoundError,
    NotTheOwnerError,
    PayloadTooLargeError,
    PositionTakenError,
    ProviderUnavailableError,
    RobinautsError,
    RunAlreadyActiveError,
    RunNotFoundError,
    RunQuietError,
    SchemaError,
    SignInError,
    SignInErrorCode,
    StoredDataError,
    UnknownAgentError,
    UnknownModelError,
    UnknownProviderError,
    UnsupportedContentError,
    UnsupportedFormatError,
    UnsupportedMediaTypeError,
    chain,
    where,
)

_log = logging.getLogger(__name__)

GENERIC_DETAIL = "the request could not be served"
"""What a 5xx tells the browser. Everything else about it is in the log."""

INTERNAL_ERROR = "InternalError"
"""The ``error`` of a body that names no class, because none would be true."""

NOT_FOUND_ERROR = "NotFoundError"
"""The one class name everything that is not there answers with."""

NOT_FOUND_DETAIL = "there is nothing here of that id"
"""The one sentence it answers with. It says nothing about whose it is."""

QUIET_RUN_DETAIL = "the run stored nothing for a long time and watching it was given up on"
"""The one sentence a watcher that gave up on a run answers with.

Fixed, like every other detail here: nothing of the request in it. It is the
one 5xx that says what happened, because nothing about the request **was**
wrong and the run is not over -- a person told "internal error" would think
the deployment broken and start the whole turn again, rather than opening the
conversation and seeing where the run got to (``docs/specs/runs.md``, known
limits).
"""

NOT_OFFERED_DETAIL = "body.model_id: is not a model this deployment offers"
"""What a request naming a model the deployment does not offer is told.

Naming the field and not the id: the id is the request's. The two routes that
take a ``model_id`` -- a new chat and a conversation's move -- both take it
under that name, so one sentence is true of either.
"""

NO_LONGER_OFFERED_DETAIL = (
    "this conversation's model is no longer offered here; move the conversation to another"
)
"""What a turn in a conversation whose model has been removed is told.

Nothing of the request is in it, nor the model's id: the person has the
conversation, and its model, in front of them.
"""

UNREADABLE_DETAIL = "the request could not be read"
"""What a request nobody could parse is told, when there is nothing safe to add."""

METHOD_NOT_ALLOWED = 405
"""The one refusal of Starlette's whose header this module builds itself."""

MAX_DETAIL_CHARS = 300
"""How long a detail built out of a request may be. It is a sentence, not a dump."""

_LOGGED = MAX_LOGGED
"""How much of one of our own messages a log line carries.

``domain.MAX_LOGGED``, kept under its old name for the callers in this module.
"""

MAX_REPORTED_FIELDS = 5
"""How many fields a "could not be read" answer names before it stops."""

MAX_LOCATION_CHARS = 40
"""How much of one piece of a field's location a detail carries.

The location is ours -- ``body.title``, ``query.limit``, ``path.run_id`` --
because the fields are ours; it is bounded all the same, since a list index
inside one is as long as the list somebody sent.
"""

UNREADABLE_RULES: Mapping[str, str] = {
    "missing": "is required",
    "json_invalid": "is not readable JSON",
    "model_type": "is not an object",
    "model_attributes_type": "is not an object",
    "dict_type": "is not an object",
    "list_type": "is not a list",
    "string_type": "is not text",
    "string_unicode": "is not text",
    "string_too_short": "is shorter than this field allows",
    "string_too_long": "is longer than this field allows",
    "int_type": "is not a whole number",
    "int_parsing": "is not a whole number",
    "int_from_float": "is not a whole number",
    "float_type": "is not a number",
    "float_parsing": "is not a number",
    "bool_type": "is not true or false",
    "bool_parsing": "is not true or false",
    "greater_than": "is below what this field allows",
    "greater_than_equal": "is below what this field allows",
    "less_than": "is above what this field allows",
    "less_than_equal": "is above what this field allows",
    "uuid_type": "is not a uuid",
    "uuid_parsing": "is not a uuid",
    "datetime_type": "is not a time",
    "datetime_parsing": "is not a time",
    "enum": "is not one of the values this field takes",
    "literal_error": "is not one of the values this field takes",
}
"""One sentence of **ours** per kind of thing pydantic refuses.

Keyed on its error ``type``, which is a stable, closed vocabulary of
pydantic's, unlike its ``msg``, which is prose and quotes what it refused.
A type that is not here is answered with ``UNREADABLE_RULE``: a build that
printed an unknown message rather than saying less would be one input away
from reflecting a request back.
"""

UNREADABLE_RULE = "is not what this field takes"
"""What a field refused for a reason nothing here has a sentence for says."""

UNKNOWN_FIELD = "a field nobody knows"
"""What a body with one unknown key is told. Never which key it was."""

SIGN_IN_DETAIL: Mapping[SignInErrorCode, str] = {
    SignInErrorCode.EXPIRED: "the sign-in took too long, or was already used",
    SignInErrorCode.STATE_MISMATCH: "the sign-in did not begin in this browser",
    SignInErrorCode.NOT_ALLOWED: "this account may not sign in to this deployment",
    SignInErrorCode.UNKNOWN_PROVIDER: "there is no such sign-in provider",
    SignInErrorCode.BUSY: "too many sign-ins are in progress; try again shortly",
    SignInErrorCode.PROVIDER_UNAVAILABLE: "the sign-in provider could not be reached",
    SignInErrorCode.PROVIDER_REFUSED: "the sign-in provider refused the sign-in",
    SignInErrorCode.INVALID_ID_TOKEN: "the sign-in provider's answer was not for this sign-in",
}
"""One fixed sentence per code, which is all a browser is ever told.

The same closed set the sign-in page knows (``docs/specs/sign-in.md``), said
in words instead of as a query parameter. Nothing here is built out of a
request or out of what a provider said.
"""

STATUS_OF: dict[type[RobinautsError], int] = {
    RobinautsError: 500,
    # What was sent cannot be taken as it is.
    InvalidValueError: 422,
    UnsupportedMediaTypeError: 415,
    # More than this deployment reads of a body, refused on what the request
    # said it was sending where it said so (``robinauts.api.protection``).
    PayloadTooLargeError: 413,
    # Who is asking is not known, or is not let in.
    AuthenticationError: 401,
    CrossSiteRequestError: 403,
    # A sign-in that did not complete. The auth routes answer these with a
    # redirect to the sign-in page and never reach this table; it is here so
    # that one escaping from anywhere else is still a refusal rather than a
    # 500, and so that the table stays exhaustive.
    SignInError: 403,
    NotAllowedError: 403,
    InvalidIdTokenError: 403,
    UnknownProviderError: 404,
    ProviderUnavailableError: 502,
    # Content of a kind the format names and this build does not carry: the
    # request asked for something that is not there yet, not something wrong.
    UnsupportedContentError: 422,
    InvalidMessageTreeError: 422,
    # A listing's cursor that is no cursor of ours: a value like any other,
    # and its own class so that whoever answers knows which field carried it.
    InvalidCursorError: 422,
    # A conversation nobody may see and one that never existed answer the
    # same body, not merely the same status (see `error_body`), so that an id
    # cannot be probed for existence.
    NotFoundError: 404,
    CheckpointNotFoundError: 404,
    ConversationNotFoundError: 404,
    MessageNotFoundError: 404,
    RunNotFoundError: 404,
    NotTheOwnerError: 404,
    # An agent id that names no configured agent. Not a secret -- the picker
    # lists the agents there are -- and it answers the same body all the same,
    # since it is under `NotFoundError` and there is one answer for everything
    # that is not there.
    UnknownAgentError: 404,
    # A model the request names that the deployment does not offer -- a new
    # chat's, or the one a conversation is moved to: a value this deployment
    # cannot take, answered under its own name so a client can say so.
    UnknownModelError: 422,
    # A conversation whose model the operator has since removed, at its next
    # turn: nothing about the request was wrong, the conversation's state is
    # what refuses, and moving it to another model is what makes the turn
    # work. Raised only after the conversation was found to be the caller's,
    # so it tells nobody anything about somebody else's.
    ModelNotOfferedError: 409,
    # The conversation is busy answering, or the run has moved on: the state
    # of something else is what refuses, and trying again may well work.
    RunAlreadyActiveError: 409,
    IllegalTransitionError: 409,
    # A run's events are numbered by the application alone, so a position
    # that is not the next one means the run moved on under it. No request
    # names a position; it is here because the table is exhaustive.
    PositionTakenError: 409,
    # Watching a run that said nothing for longer than a turn may take was
    # given up on. A gateway timeout is the honest one: nothing about the
    # request was wrong, the run is not over, and what the deployment could
    # not do is wait any longer for something behind it. It is the one 5xx
    # here that answers a body of its own (`QUIET_RUN_DETAIL`), because the
    # generic one would have a person believe the deployment broken.
    RunQuietError: 504,
    # Start-up, not a request: a deployment in this state does not serve.
    ConfigError: 500,
    SchemaError: 500,
    DatabaseUnreachableError: 500,
    # Our own rows, unreadable by this build. Nothing the request did, so the
    # browser is told nothing and the whole of it goes to the log.
    UnsupportedFormatError: 500,
    StoredDataError: 500,
}
"""The status each platform error answers with; every class of the hierarchy."""


def status_of(exc: RobinautsError) -> int:
    """The status for ``exc``: its class's, or its nearest listed base class's."""
    for cls in type(exc).__mro__:
        if issubclass(cls, RobinautsError) and cls in STATUS_OF:
            return STATUS_OF[cls]
    raise AssertionError("unreachable: RobinautsError itself has a status")


def error_body(exc: BaseException, status: int) -> dict[str, Any]:
    """The JSON body an error crosses as, with nothing in it that came from outside."""
    if isinstance(exc, RunQuietError):
        # The 5xx that is not a bug of ours and not a secret: see
        # `QUIET_RUN_DETAIL`. The message it carries names the run and the
        # bound, so the fixed sentence is what crosses instead.
        return {"error": type(exc).__name__, "detail": QUIET_RUN_DETAIL}
    if status >= 500:
        return {"error": INTERNAL_ERROR, "detail": GENERIC_DETAIL}
    if isinstance(exc, NotFoundError | NotTheOwnerError):
        # Identical in every byte, on purpose: see this module's docstring.
        return {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}
    if isinstance(exc, SignInError):
        return {"error": type(exc).__name__, "detail": SIGN_IN_DETAIL[exc.code]}
    if isinstance(exc, UnknownModelError | ModelNotOfferedError):
        # Its message names the model, which is the request's or the
        # conversation's: the log has it, and the body a fixed sentence.
        detail = (
            NO_LONGER_OFFERED_DETAIL
            if isinstance(exc, ModelNotOfferedError)
            else NOT_OFFERED_DETAIL
        )
        return {"error": type(exc).__name__, "detail": detail}
    return {"error": type(exc).__name__, "detail": str(exc)}


def http_refusal(status: int, *, headers: Mapping[str, str] | None = None) -> JSONResponse:
    """The response a status the framework raised becomes: one shape, one place.

    Starlette answers a path that is not routed, a method that is not allowed
    and the rest in a shape of its own (``{"detail": ...}``); this is the
    project's. The handler below is the usual caller. The other is
    ``robinauts.api.ui``, which answers for a file that is not there **inside**
    its own mount, so that such an answer carries the interface's headers like
    every other answer at that path -- an exception handler runs above the
    mount and could not know it was for one.
    """
    body = {"error": http_error_name(status), "detail": http_error_detail(status)}
    return JSONResponse(body, status_code=status, headers=dict(headers or {}))


def refusal(exc: RobinautsError) -> JSONResponse:
    """The response one of our errors becomes.

    Used by the handlers below and by the request protection, which runs as
    middleware -- outside the reach of an exception handler -- and so has to
    build its refusal rather than raise it.

    A 401 carries no ``WWW-Authenticate``: the browser must not be offered its
    own sign-in dialogue, since signing in here is a page of ours.

    Where the body says less than the exception does, the difference goes to
    the log; otherwise nobody would ever see what really happened.
    """
    status = status_of(exc)
    body = error_body(exc, status)
    if isinstance(exc, RunQuietError):
        # Not an internal error, and not news: ``application.Watch`` has
        # already said at WARNING which run it gave up on and why. One line
        # here, at the same level, rather than the whole chain of a 5xx as
        # though something had gone wrong with the deployment.
        _log.warning("%s", chain(exc))
    elif status >= 500:
        # The whole chain: see `chain`. A 5xx of ours says nothing to the
        # browser, so if what it was raised from does not reach the log,
        # nothing anywhere says which row or which version was the trouble.
        _log.error("%s | at %s", chain(exc), where(exc))
    elif body["detail"] != str(exc):
        _log.warning("%s", chain(exc))
    return JSONResponse(body, status_code=status)


def http_error_name(status: int) -> str:
    """The ``error`` a Starlette refusal is named by: ``NotFound``, ``MethodNotAllowed``."""
    try:
        return HTTPStatus(status).phrase.replace(" ", "").replace("-", "")
    except ValueError:
        return INTERNAL_ERROR if status >= 500 else "HttpError"


def http_error_detail(status: int) -> str:
    """Its ``detail``: the status's own phrase, and nothing of the request."""
    try:
        return HTTPStatus(status).phrase.lower()
    except ValueError:
        return GENERIC_DETAIL if status >= 500 else UNREADABLE_DETAIL


def unknown_fields(many: int) -> str:
    """How an answer says that a body held keys nobody knows: how many, not which.

    A request body is sent with whatever keys the sender likes -- a secret
    pasted into the wrong tool arrives as a **key** as readily as as a value
    -- and a model that forbids extras reports each one with the key in its
    location. So they are counted here: the count is a fact about the request
    and the names are the request itself.
    """
    return UNKNOWN_FIELD if many == 1 else f"{many} fields nobody knows"


def unreadable_detail(exc: RequestValidationError) -> str:
    """Which fields could not be read, and why -- never with what.

    Pydantic's error carries the ``input`` it refused, which is exactly the
    value somebody submitted: a password in the wrong field, a token pasted
    where a number goes. And its ``msg`` frequently carries a piece of that
    input as well. So what is said here is built out of two things only: the
    **location**, which names fields of ours, and one sentence of **ours** per
    error type (``UNREADABLE_RULES``). Bounded, because a location can hold a
    list index and a body can be wrong in a thousand places.
    """
    said: list[str] = []
    unknown: dict[str, int] = {}
    for problem in exc.errors():
        location = tuple(problem.get("loc", ()))
        if problem.get("type") == "extra_forbidden":
            # The last piece of the location is the key somebody sent, which
            # is the one piece of a location that is theirs and not ours.
            where = _located(location[:-1])
            unknown[where] = unknown.get(where, 0) + 1
            continue
        if len(said) < MAX_REPORTED_FIELDS:
            rule = UNREADABLE_RULES.get(str(problem.get("type", "")), UNREADABLE_RULE)
            where = _located(location)
            said.append(f"{where}: {rule}" if where else rule)
    said.extend(f"{where}: {unknown_fields(many)}" for where, many in unknown.items())
    return "; ".join(said)[:MAX_DETAIL_CHARS] or UNREADABLE_DETAIL


def _located(location: tuple[Any, ...]) -> str:
    """Where a problem is, as a detail names it: ``body.title``, ``query.limit``.

    **Only the pieces that are names.** A location is ours where it names a
    field, and pydantic also puts numbers in one: the index inside a list it
    refused, and -- for a body that is not JSON at all -- the **character
    offset** it stopped at, which is a number computed from what was sent
    (``body.2012``). Those are dropped, so what is left is the field path and
    nothing measured off the request. A location with nothing but numbers in
    it becomes ``body``, which is the truth about where the problem is.

    **Its limit, said plainly.** What is kept is text, and that is a rule
    about the *type* of a piece and not about where it came from. Every name
    in a location today is one of ours, because every request model here has
    fixed fields; two things would change that -- a field typed as a
    **mapping**, whose keys are the sender's, and a **discriminated union**,
    whose tag pydantic writes into the location. Neither exists, and
    ``test_no_request_model_puts_a_senders_word_in_a_location`` is what makes
    whoever writes the first one read this paragraph.
    """
    return ".".join(piece[:MAX_LOCATION_CHARS] for piece in location if isinstance(piece, str))


def allowed_methods(app: Any, path: str) -> str:
    """Every method really served at ``path``, sorted, for an ``Allow`` header.

    Starlette answers a 405 out of the **first** route whose path matched, and
    a path served by more than one route -- which is what
    ``/api/conversations/{id}`` is, with a GET, a PATCH and a DELETE written as
    three routes -- therefore gets an ``Allow`` naming one of them. A client
    that read it would be told that renaming a conversation is not allowed.

    So it is built here instead, from every route whose own regular expression
    matches the path that was asked for. Empty when nothing matches, and then
    what Starlette said is kept: a 405 has to come from somewhere, and a walk
    that found nothing has nothing better to offer -- which is also the answer
    for a path served by something that is not an ``APIRoute``, a mounted
    application among them, since what it allows is its own to say.

    The walk is ``access.api_routes``, which is the one this project has: it
    reads every list a router keeps routes in and goes through an **included
    router** -- where FastAPI keeps what ``include_router`` added, rather than
    flattening it into the application's list -- and into a mounted
    application. A route inside a mount has a path of its own that does not
    begin where the request's does, so one never matches here, which is right:
    a mounted application answers its own 405.
    """
    served: set[str] = set()
    for route in api_routes(app.router):
        regex = getattr(route, "path_regex", None)
        if regex is not None and regex.match(path):
            served.update(route.methods or ())
    return ", ".join(sorted(served))


def install_handlers(app: FastAPI, *, headers: Mapping[str, str] | None = None) -> None:
    """Teach ``app`` to answer every exception in the one shape above.

    ``headers`` go on the last of the four, and only there. The others answer
    from inside the middleware stack, so whatever is put on every response is
    put on theirs as well; an unhandled exception is turned into a response by
    Starlette's outermost middleware, beyond the reach of ours, and would
    otherwise be the one answer of the deployment carrying none.
    """

    @app.exception_handler(RobinautsError)
    async def _platform_error(request: Request, exc: RobinautsError) -> JSONResponse:
        return refusal(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """A path that is not routed, a method that is not allowed, and the rest.

        Starlette answers these itself, in a shape of its own
        (``{"detail": ...}``). They are the two answers a client is most
        likely to meet after a typo, so they are put in the project's shape
        like everything else. The headers are kept: a 405 carries ``Allow``,
        and a client reads it.
        """
        if exc.status_code >= 500:
            _log.error(
                "%s %s answered %s: %s",
                shown(request.method, most=16),
                shown(request.url.path),
                exc.status_code,
                shown(exc.detail),
            )
        sent = dict(exc.headers or {})
        if exc.status_code == METHOD_NOT_ALLOWED:
            served = allowed_methods(request.app, request.url.path)
            if served:
                # Written back over whatever Starlette put there, whichever
                # way it spelt the name: one header, and the right one.
                sent = {name: value for name, value in sent.items() if name.lower() != "allow"}
                sent["allow"] = served
        return http_refusal(exc.status_code, headers=sent)

    @app.exception_handler(RequestValidationError)
    async def _unreadable_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """A parameter or a body FastAPI could not read.

        Answered in the same shape as every other refusal, naming the fields
        and the rules they broke and none of the values.
        """
        return refusal(InvalidValueError(unreadable_detail(exc)))

    @app.exception_handler(Exception)
    async def _unknown_error(request: Request, exc: Exception) -> JSONResponse:
        """Anything else: a bug. The browser learns nothing from it.

        Starlette re-raises whatever this handler was given after it has been
        turned into a response, so an ASGI server still logs it; the line here
        is what ties it to the request it came from.
        """
        _log.exception(
            "%s %s raised %s",
            shown(request.method, most=16),
            shown(request.url.path),
            type(exc).__name__,
        )
        return JSONResponse(error_body(exc, 500), status_code=500, headers=dict(headers or {}))
