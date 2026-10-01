# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""How an error crosses HTTP, and what a 500 is careful not to say.

Two things are worth a test of their own. One: the table of statuses is
exhaustive, so that an error class added to ``domain`` without a status is a
failing test rather than a route quietly answering 500. Two: nothing a bug
knows reaches the browser -- not the message, not the type, not the traceback
-- while all of it reaches the log.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from aio import asyncio_test
from robinauts.legacy.api import (
    GENERIC_DETAIL,
    INTERNAL_ERROR,
    MAX_DETAIL_CHARS,
    NO_LONGER_OFFERED_DETAIL,
    NOT_FOUND_DETAIL,
    NOT_FOUND_ERROR,
    NOT_OFFERED_DETAIL,
    QUIET_RUN_DETAIL,
    SIGN_IN_DETAIL,
    STATUS_OF,
    UNREADABLE_RULE,
    UNREADABLE_RULES,
    create_api,
    public,
    status_of,
    unreadable_detail,
)
from robinauts.legacy.domain import (
    AuthenticationError,
    ConfigError,
    ConversationNotFoundError,
    InvalidValueError,
    MessageNotFoundError,
    ModelNotOfferedError,
    NotTheOwnerError,
    RobinautsError,
    RunNotFoundError,
    RunQuietError,
    SchemaError,
    SignInError,
    SignInErrorCode,
    StoredDataError,
    UnknownModelError,
    UnsupportedFormatError,
    reading_stored,
)
from webapp import PUBLIC_URL, wired

SECRET_IN_A_BUG = "a-row-nobody-outside-should-see"


def every_error() -> list[type[RobinautsError]]:
    """Every error class the platform defines, however deeply nested."""
    found: list[type[RobinautsError]] = []

    def walk(cls: type[RobinautsError]) -> None:
        for subclass in cls.__subclasses__():
            if subclass.__module__.startswith("robinauts."):
                found.append(subclass)
                walk(subclass)

    walk(RobinautsError)
    return found


def test_every_platform_error_has_a_status_of_its_own() -> None:
    """Not "resolves to one through a base class": one written down for it.

    A class left out would answer 500 through ``RobinautsError``, which is how
    a refusal becomes an outage.
    """
    listed = set(STATUS_OF)

    assert set(every_error()) <= listed
    assert listed <= {RobinautsError, *every_error()}


@asyncio_test
async def test_a_bug_inside_our_own_reader_can_still_be_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`reading_stored` gives a bug of ours the same sentence as a bad row.

    So the frames are the only thing that says which it was, and where.
    """
    app = create_api(wired().sign_in)

    @app.get("/reader-bug", dependencies=[public()])
    async def reader_bug() -> dict[str, str]:
        with reading_stored("a stored message cannot be read by this build"):
            raise TypeError(f"a reader of ours: {SECRET_IN_A_BUG}\nand a second line")

    with caplog.at_level(logging.ERROR):
        async with quiet(app) as client:
            answered = await client.get("/reader-bug")

    assert answered.status_code == 500
    assert SECRET_IN_A_BUG not in answered.text
    assert "in reader_bug" in caplog.text
    assert "TypeError" in caplog.text
    assert len(caplog.text.splitlines()) == 1


def test_the_statuses_are_what_they_should_be() -> None:
    assert status_of(AuthenticationError("no")) == 401
    # A version is written and read by us: a request carries the fields of a
    # message and never a `format_version`, so meeting one of these means a
    # row of ours, which is a fault of ours and not of the request.
    assert status_of(UnsupportedFormatError("a row of ours")) == 500
    assert status_of(StoredDataError("a row of ours")) == 500
    assert status_of(InvalidValueError("no")) == 422
    assert status_of(ConfigError(["no"])) == 500
    # Somebody else's server, not this deployment: a bad gateway, beside the
    # identity provider that cannot be reached.
    assert status_of(SchemaError.missing(expected=1)) == 500
    # A model the request named is a value refused; a conversation's model
    # that is gone is the conversation's state, and neither is "not there".
    assert status_of(UnknownModelError("no")) == 422
    assert status_of(ModelNotOfferedError("no")) == 409
    # A subclass nobody listed still resolves, through its nearest base.
    assert status_of(type("Later", (AuthenticationError,), {})("no")) == 401


class Counted(BaseModel):
    how_many: int


def leaking_app() -> FastAPI:
    """An application whose routes fail in the ways a route can."""
    app = create_api(wired().sign_in)

    @app.get("/bug", dependencies=[public()])
    async def bug() -> dict[str, str]:
        raise RuntimeError(f"the query returned {SECRET_IN_A_BUG}")

    @app.get("/ours", dependencies=[public()])
    async def ours() -> dict[str, str]:
        raise ConfigError([f"the deployment is misconfigured: {SECRET_IN_A_BUG}"])

    @app.get("/refused", dependencies=[public()])
    async def refused() -> dict[str, str]:
        raise InvalidValueError("a value nobody can work with")

    @app.get("/missing/{which}", dependencies=[public()])
    async def missing(which: str) -> dict[str, str]:
        """Everything that is not there, however it is not there."""
        raise {
            "conversation": ConversationNotFoundError,
            "message": MessageNotFoundError,
            "run": RunNotFoundError,
            "owner": NotTheOwnerError,
        }[which](f"conversation {SECRET_IN_A_BUG} is not this user's")

    @app.get("/unreadable-row", dependencies=[public()])
    async def unreadable_row() -> dict[str, str]:
        """As `core` raises it: a fixed sentence, and the particulars beneath.

        The message of a `StoredDataError` says nothing on purpose. Which row
        was broken, and how, is only in what it was raised from.
        """
        try:
            raise InvalidValueError(f"message {SECRET_IN_A_BUG} has no parts\nsecond line")
        except InvalidValueError as cause:
            raise StoredDataError("a stored message cannot be read by this build") from cause

    @app.get("/given-up-on", dependencies=[public()])
    async def given_up_on() -> dict[str, str]:
        """A watcher that stopped following a run that was storing nothing."""
        raise RunQuietError(f"run {SECRET_IN_A_BUG} stored nothing for 600.0s")

    @app.get("/sign-in-failed", dependencies=[public()])
    async def sign_in_failed() -> dict[str, str]:
        raise SignInError(
            SignInErrorCode.PROVIDER_REFUSED,
            f"the provider said {SECRET_IN_A_BUG} about {SECRET_IN_A_BUG}",
        )

    @app.get("/model/{which}", dependencies=[public()])
    async def model(which: str) -> dict[str, str]:
        """A model that is not offered: named by the request, or the conversation's."""
        raise {"named": UnknownModelError, "conversation's": ModelNotOfferedError}[which](
            f"no model {SECRET_IN_A_BUG!r} is configured in this deployment"
        )

    @app.get("/number/{value}", dependencies=[public()])
    async def number(value: int) -> dict[str, int]:
        return {"value": value}

    @app.post("/counted", dependencies=[public()])
    async def counted(body: Counted) -> dict[str, int]:  # pragma: no cover -- always refused
        return {"how_many": body.how_many}

    return app


def quiet(app: FastAPI) -> httpx.AsyncClient:
    """A client that lets the application answer a bug rather than re-raising."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=PUBLIC_URL,
    )


@pytest.mark.parametrize("path", ["/bug", "/ours"])
@asyncio_test
async def test_a_500_says_nothing_about_what_went_wrong(
    path: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug of ours and an error of ours both answer the same empty 500."""
    with caplog.at_level(logging.ERROR):
        async with quiet(leaking_app()) as client:
            answered = await client.get(path)

    assert answered.status_code == 500
    assert answered.json() == {"error": INTERNAL_ERROR, "detail": GENERIC_DETAIL}
    assert SECRET_IN_A_BUG not in answered.text
    assert "RuntimeError" not in answered.text
    assert "ConfigError" not in answered.text
    assert "Traceback" not in answered.text
    # And all of it is in the log, which is where an operator reads it.
    assert SECRET_IN_A_BUG in caplog.text


@asyncio_test
async def test_a_watcher_that_gave_up_says_so_and_is_not_an_internal_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one 5xx with something to say, and the one that is not logged as a bug.

    Nothing about the request was wrong and the run is not over: a person told
    "internal error" would think the deployment broken and start the turn
    again. ``application.Watch`` has already said at WARNING which run it was,
    so this is not reported a second time as a fault of ours.
    """
    with caplog.at_level(logging.DEBUG):
        async with quiet(leaking_app()) as client:
            answered = await client.get("/given-up-on")

    assert answered.status_code == 504
    assert answered.json() == {"error": "RunQuietError", "detail": QUIET_RUN_DETAIL}
    # The run's id is in the log and never in the answer, like every other
    # particular of one of ours.
    assert SECRET_IN_A_BUG not in answered.text
    said = [record for record in caplog.records if SECRET_IN_A_BUG in record.getMessage()]
    assert [record.levelno for record in said] == [logging.WARNING]


@asyncio_test
async def test_a_500_still_carries_the_security_headers() -> None:
    """Even the answer Starlette builds outside our middleware carries them."""
    async with quiet(leaking_app()) as client:
        answered = await client.get("/bug")

    assert answered.headers["x-content-type-options"] == "nosniff"


@asyncio_test
async def test_a_refusal_names_its_class_and_says_why() -> None:
    async with quiet(leaking_app()) as client:
        answered = await client.get("/refused")

    assert answered.status_code == 422
    assert answered.json() == {
        "error": "InvalidValueError",
        "detail": "a value nobody can work with",
    }


@asyncio_test
async def test_an_unreadable_parameter_is_refused_in_the_same_shape() -> None:
    """FastAPI's own validation error, answered the way everything else is."""
    async with quiet(leaking_app()) as client:
        answered = await client.get("/number/not-a-number")

    assert answered.status_code == 422
    assert answered.json()["error"] == "InvalidValueError"
    assert "value" in answered.json()["detail"]


@asyncio_test
async def test_a_sign_in_failure_says_one_fixed_sentence_and_nothing_of_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``SignInError``'s detail is written for the log: it holds a provider's
    words and the values a request carried. What crosses is the sentence the
    code stands for, which is the same closed set the sign-in page knows.
    """
    with caplog.at_level(logging.WARNING):
        async with quiet(leaking_app()) as client:
            answered = await client.get("/sign-in-failed")

    assert answered.status_code == 403
    assert answered.json() == {
        "error": "SignInError",
        "detail": SIGN_IN_DETAIL[SignInErrorCode.PROVIDER_REFUSED],
    }
    assert SECRET_IN_A_BUG not in answered.text
    assert SECRET_IN_A_BUG in caplog.text


@asyncio_test
async def test_a_model_not_offered_is_named_and_says_one_fixed_sentence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its class is what a client branches on; the id is the request's, or the
    conversation's, and goes to the log alone."""
    with caplog.at_level(logging.WARNING):
        async with quiet(leaking_app()) as client:
            named = await client.get("/model/named")
            gone = await client.get("/model/conversation's")

    assert named.status_code == 422
    assert named.json() == {"error": "UnknownModelError", "detail": NOT_OFFERED_DETAIL}
    assert gone.status_code == 409
    assert gone.json() == {"error": "ModelNotOfferedError", "detail": NO_LONGER_OFFERED_DETAIL}
    assert SECRET_IN_A_BUG not in named.text + gone.text
    assert SECRET_IN_A_BUG in caplog.text


def test_every_code_has_a_sentence_of_its_own() -> None:
    """An unlisted code would be a ``KeyError`` in a handler, which is a 500."""
    assert set(SIGN_IN_DETAIL) == set(SignInErrorCode)
    assert len(set(SIGN_IN_DETAIL.values())) == len(SignInErrorCode)


@asyncio_test
async def test_a_path_that_is_not_routed_answers_in_the_project_shape() -> None:
    async with quiet(leaking_app()) as client:
        answered = await client.get("/nowhere-at-all")

    assert answered.status_code == 404
    assert answered.json() == {"error": "NotFound", "detail": "not found"}
    assert answered.headers["x-content-type-options"] == "nosniff"


@asyncio_test
async def test_a_method_that_is_not_allowed_says_which_are() -> None:
    """The same shape, and the ``Allow`` header a client reads is kept."""
    async with quiet(leaking_app()) as client:
        answered = await client.request(
            "DELETE", "/health", headers={"content-type": "application/json"}
        )

    assert answered.status_code == 405
    assert answered.json() == {"error": "MethodNotAllowed", "detail": "method not allowed"}
    assert answered.headers["allow"] == "GET"
    assert answered.headers["referrer-policy"] == "same-origin"


@asyncio_test
async def test_a_body_that_could_not_be_read_names_the_field_and_not_the_value() -> None:
    """Pydantic's error carries the ``input`` it refused: that is what was sent.

    A password in the wrong field, a token pasted where a number goes -- the
    answer says which field and which rule, and gives the value back to
    nobody.
    """
    async with quiet(leaking_app()) as client:
        answered = await client.post("/counted", json={"how_many": SECRET_IN_A_BUG})

    assert answered.status_code == 422
    body = answered.json()
    assert body["error"] == "InvalidValueError"
    assert "how_many" in body["detail"]
    assert SECRET_IN_A_BUG not in answered.text
    assert len(body["detail"]) <= MAX_DETAIL_CHARS


def test_a_refusal_pydantic_has_no_sentence_here_for_still_says_nothing_of_it() -> None:
    """The fallback, which is the whole point of not passing ``msg`` on.

    Pydantic grows error types, and a build that printed the message of one it
    had never read about would be one input away from reflecting a request
    back -- its messages quote what they refused. So an unknown type says
    where the problem is and the one sentence that is always true.
    """
    invented = RequestValidationError(
        [
            {
                "type": "a_type_from_a_later_pydantic",
                "loc": ("body", "title"),
                "msg": f"Input should be something else, not {SECRET_IN_A_BUG}",
                "input": SECRET_IN_A_BUG,
            }
        ]
    )

    said = unreadable_detail(invented)

    assert said == f"body.title: {UNREADABLE_RULE}"
    assert SECRET_IN_A_BUG not in said
    assert UNREADABLE_RULE not in UNREADABLE_RULES.values()


@asyncio_test
async def test_a_detail_built_from_a_request_is_bounded() -> None:
    """Many wrong fields at once do not become a page of a body."""
    async with quiet(leaking_app()) as client:
        answered = await client.post(
            "/counted", json={"how_many": ["x" * 2000] * 50, "extra": "y" * 5000}
        )

    assert answered.status_code == 422
    assert len(answered.json()["detail"]) <= MAX_DETAIL_CHARS


# --- everything that is not there answers the same ---------------------------


@asyncio_test
async def test_nothing_tells_a_missing_id_apart_from_one_that_is_not_yours(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Byte for byte the same answer, headers included.

    The difference between "no such conversation" and "not yours" is the whole
    of what an attacker wants from an id: a body naming the class would hand it
    over while the status hid it.
    """
    answers = []
    with caplog.at_level(logging.WARNING):
        async with quiet(leaking_app()) as client:
            for which in ("conversation", "message", "run", "owner"):
                answers.append(await client.get(f"/missing/{which}"))

    bodies = {answer.content for answer in answers}
    statuses = {answer.status_code for answer in answers}
    headers = {tuple(sorted(answer.headers.items())) for answer in answers}
    assert statuses == {404}
    assert len(bodies) == 1
    assert len(headers) == 1
    assert answers[0].json() == {"error": NOT_FOUND_ERROR, "detail": NOT_FOUND_DETAIL}
    assert all(SECRET_IN_A_BUG not in answer.text for answer in answers)
    # What it really was is in the log, where only an operator reads it.
    logged = caplog.text
    assert "NotTheOwnerError" in logged
    assert SECRET_IN_A_BUG in logged


@asyncio_test
async def test_a_row_this_build_cannot_read_says_nothing_to_the_browser(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """And the operator is told which row, which is only in the cause."""
    with caplog.at_level(logging.ERROR):
        async with quiet(leaking_app()) as client:
            answered = await client.get("/unreadable-row")

    assert answered.status_code == 500
    assert answered.json() == {"error": INTERNAL_ERROR, "detail": GENERIC_DETAIL}
    assert SECRET_IN_A_BUG not in answered.text
    assert "StoredDataError" in caplog.text
    assert "InvalidValueError" in caplog.text
    assert SECRET_IN_A_BUG in caplog.text, "the cause is what says which row"
    # And where it was raised, which is the only way to find a bug in a
    # reader of ours, since it is relabelled with the same fixed sentence.
    assert "test_api_errors.py:" in caplog.text
    assert "in unreadable_row" in caplog.text
    # Escaped like anything else from outside: a row can hold a newline, and
    # so can a function name that came from somewhere odd.
    assert "\\n" in caplog.text
    assert len(caplog.text.splitlines()) == 1
