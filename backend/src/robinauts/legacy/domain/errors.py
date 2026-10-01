# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Every error the platform raises, rooted at ``RobinautsError``.

A sign-in failure carries one of the fixed codes of
``docs/specs/sign-in.md``: the browser is told the code and nothing else,
while ``detail`` says what really happened and goes to the log.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum


class RobinautsError(Exception):
    """The root of the platform's error hierarchy."""


class InvalidValueError(RobinautsError, ValueError):
    """A value that no part of the platform can work with."""


class ConfigError(RobinautsError):
    """Configuration that cannot be used; ``problems`` lists every one found.

    The operator gets the whole list at once, as ``docs/specs/operations.md``
    asks: a start-up that fixed one problem at a time would take as many
    restarts as there are mistakes.
    """

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        if not self.problems:
            raise InvalidValueError("a ConfigError lists at least one problem")
        super().__init__("invalid configuration:\n" + "\n".join(self.problems))


class UnsupportedContentError(InvalidValueError):
    """Content the format names and this build does not carry.

    An image, a file, a tool call, a tool result, a message of the ``tool``
    role: every one of them has its place in the conversation format
    (``docs/specs/conversations.md``) and none of them is built yet. Meeting
    one is not a broken record -- it is a record from a version of the
    platform that has more of the format than this one -- so it is refused by
    name rather than read as something else.
    """


class UnsupportedFormatError(InvalidValueError):
    """Data recorded in a version of the conversation format this build cannot read.

    A build that guessed at the fields of a version it does not know would
    write back a conversation it had misread. It stops instead.

    **Only our own rows raise it.** A version is written and read by the
    platform; a request carries the fields of a message and never a
    ``format_version``, and the api that takes one must not accept one -- a
    browser naming a version would be choosing how its message is read. So
    this always means a row of ours, which is why it answers as a fault of
    ours (500) and is read through ``StoredDataError`` on the paths that go to
    the database.
    """


class StoredDataError(RobinautsError):
    """A row of our own database that this build cannot read.

    Whatever the reason -- a version above this build's, a kind of content it
    does not carry, a tree that is no tree, a field of the wrong type -- it is
    **not** something a request did, and the browser learns nothing from it:
    it is answered like any other mistake of ours, and the whole of it goes to
    the log. The cause is chained, so the log says what was really wrong.

    ``robinauts.legacy.core`` raises it from the ``*_stored`` readers, which is what
    the stores and the application read rows through.
    """


class NotFoundError(RobinautsError):
    """Something was asked for by id and there is no such thing.

    Also what someone is told when the thing exists and is not theirs: a
    conversation nobody may see and a conversation that never existed answer
    the same, because the difference is one only an attacker has a use for.
    """


class ConversationNotFoundError(NotFoundError):
    """No conversation of that id, or none this person may see."""


class MessageNotFoundError(NotFoundError):
    """No message of that id in this conversation."""


class RunNotFoundError(NotFoundError):
    """No run of that id, or none in this person's conversation."""


class CheckpointNotFoundError(NotFoundError):
    """No checkpoint of that id in that conversation, for the engine asked.

    A checkpoint id is the engine's own, handed back at the end of a turn
    and stored on the answer (``robinauts.legacy.ports.agent_engine``). One the
    engine does not hold for the conversation -- another conversation's, a
    turn that did not finish, another engine's, or one since forgotten -- is
    this, and nothing is done with it.
    """


class UnknownAgentError(NotFoundError):
    """No agent of that id is configured in this deployment.

    Under ``NotFoundError`` because that is what it is -- a name that reaches
    nothing -- and so that it answers the same fixed body as everything else
    that is not there (``robinauts.legacy.api.errors``). An agent's id is not a
    secret: the picker lists the ones there are (``docs/specs/agents.md``). It
    is also what a conversation bound to an agent the operator has since
    removed meets, and the detail, which reaches the log alone, says which of
    the two it was.
    """


class UnknownModelError(RobinautsError):
    """No model of that id is configured in this deployment.

    What a request naming a model that is not offered meets: a new chat's
    model, or the one a conversation is moved to. **Not** under
    ``NotFoundError``, unlike ``UnknownAgentError``: a model's id is not a
    secret -- the picker lists the ones there are -- so there is nothing to
    hide by answering it like everything else that is not there, and a person
    told "not found" about a model would be told nothing they could act on.
    It answers with its own name, so that a client can say which it was, and
    a fixed sentence, since the id is the request's (``robinauts.legacy.api.errors``).
    """


class ModelNotOfferedError(RobinautsError):
    """The conversation's model is one this deployment no longer offers.

    What a conversation whose model the operator has since removed meets at
    its next turn -- a question, an edit, a regeneration. The turn is refused
    rather than answered by another model, because the point of choosing is
    knowing who answers (``docs/specs/agents.md``); its author moves the
    conversation to a model that is offered. Its own class because nothing
    about the request was wrong: it is the conversation that stands in the
    way, and a client that could not tell this from a model it named itself
    would not know what to say.

    A sibling of ``UnknownModelError`` and not a kind of it: the two answer
    different statuses, and a handler written for the one must not catch the
    other.

    Only ever raised for a conversation its caller may see, and after that
    was decided: somebody else's conversation is still the one answer for
    everything that is not there.
    """


class NotTheOwnerError(RobinautsError):
    """A conversation belongs to somebody else.

    Raised where ownership is decided; what reaches the browser is a
    ``NotFoundError``'s answer, so that an id cannot be probed for existence.
    """


class RunAlreadyActiveError(RobinautsError):
    """The conversation already has a run going, and may have only one.

    The person cancels it or waits (``docs/specs/runs.md``); a second run
    would have two answers writing into one conversation.
    """


class IllegalTransitionError(RobinautsError):
    """A run cannot go from the state it is in to the one asked for."""


class RunQuietError(RobinautsError):
    """A run said nothing for so long that watching it was given up on.

    Not a failure of the run and not a fault of the request: the run is still
    active as far as the store is concerned, and nothing has been stored under
    it for longer than a whole turn may take. That is what a run whose end
    could not be written looks like, and what one whose process was killed
    looks like (``docs/specs/runs.md``, known limits), and a watcher that
    waited on either for ever would hold a request open for ever.

    **It is raised only by the giving up**, and by nothing else: a watcher
    whose stream simply ends has sent everything there is -- the run's
    ``RunEnded``, or everything stored after the position it asked for of a
    run that is over or is no longer there. So "the stream ended" and "the
    stream was given up on" are told apart by what was raised rather than by
    what was missing.
    """


class PositionTakenError(InvalidValueError):
    """An event was offered a position of its run that is not the next one.

    A run's events are numbered from ``FIRST_POSITION`` with no gaps, and the
    application is the single writer of them: it reads the run's last position
    and offers the one after it. So meeting this means the run moved on
    between the two -- somebody ended it, or another process took it up -- and
    the right answer is to stop writing into it, not to renumber and try
    again.

    Never something a request did: no browser names a position.
    """


class InvalidCursorError(InvalidValueError):
    """The text offered as a listing's cursor is no cursor of this store's.

    A cursor is a **position** inside the caller's own listing, written by a
    store and read back by it, and it goes out to a browser and comes back --
    so one that does not parse is refused as a value like any other, and one
    that parses and was never issued is simply a position
    (``docs/specs/conversations.md``).

    Its own class because it is the one refusal of a listing that names a
    **field**: whoever answers a request knows which parameter carried it
    (``robinauts.legacy.api.conversation_routes``), and a class is how that is known
    for certain rather than by being the only thing a call could have refused.
    """


class InvalidMessageTreeError(InvalidValueError):
    """A collection of messages that is no conversation.

    A parent that is not there, a cycle, two conversations mixed, an answer
    to an answer: none of them can be shown, sent to a model or branched
    from, so they are refused where they are read rather than where they are
    drawn.
    """


class AuthenticationError(RobinautsError):
    """The request carries no credential this deployment accepts.

    Nobody is signed in, or the session cookie names a session that has ended.
    The browser is told that and nothing more: which of the two it was is a
    difference only an attacker has a use for.
    """


class CrossSiteRequestError(RobinautsError):
    """A write that a page on another site sent, or may have sent.

    A cookie goes with every request the browser makes, whoever asked for it,
    so a write that arrives with one has to prove where it came from --
    ``Origin``, or ``Sec-Fetch-Site`` (``docs/specs/sign-in.md``, "Request
    protection"). There is no CSRF token; this is what stands in its place.
    """


class PayloadTooLargeError(RobinautsError):
    """A request body larger than this deployment reads.

    A bound on **what is read**, which is not the bound on what a record may
    hold: the conversation format carries a message of sixty-four million
    characters, and a wire that accepted one would let anybody signed in make
    the deployment buffer and parse that much twice over
    (``robinauts.legacy.api.protection``). What the two numbers mean is different --
    one is what a store keeps, the other is what a request may spend -- and an
    operator moves the second without the first moving
    (``docs/specs/operations.md``).
    """


class UnsupportedMediaTypeError(RobinautsError):
    """A write sent as something other than JSON.

    A form, and the handful of types a page may ``fetch`` without a preflight,
    cannot be ``application/json``: insisting on it is what keeps another
    site's page from writing here at all, since asking for that type makes the
    browser ask us first, and nothing here answers a preflight.
    """


DB_INIT_COMMAND = "robinauts db init"
"""The command that creates the schema. The server never creates it itself.

Named here because ``SchemaError`` is what an operator reads when the
database is not the one this build was written against, and an error that
says what is wrong without saying what to type is half an error.
"""


REDACTED = "<redacted>"
"""What takes the place of anything in a driver's words that could be a secret."""

_A_SECRET = re.compile(
    # A connection string, whole: everything from the scheme to the first run
    # of whitespace, since the password is inside it and so is the host.
    r"[A-Za-z][A-Za-z0-9+.-]*://\S*"
    # A password given as a field of an object -- JSON, a repr of a mapping, a
    # driver quoting the arguments it was handed. The name may be quoted, the
    # separator may be `=` or `:`, and the value may be quoted, in which case
    # it runs to the closing quote and **may hold spaces**.
    r'|(?i:["\']?(?:password|passfile|pgpassword)["\']?\s*[=:]\s*)'
    r"""(?:"[^"]*"|'[^']*'|\S+)"""
)
"""What is taken out of a message before it is shown to anybody.

Fails **safe rather than exact**: it takes out more than it has to -- a whole
url, including the host somebody might have wanted to read -- because the
alternative is a password in a log. What is left is the driver's own sentence
about what went wrong, which is the part an operator acts on.

The field rule is **quote-aware** on both halves. A driver that prints what it
was handed prints it the way its own language spells a mapping, so the name
arrives as ``password``, ``"password"`` or ``'password'`` and the value as a
bare word or a quoted string -- and a quoted one runs to its closing quote,
because a password with a space in it is still a password.
"""


def without_secrets(said: str) -> str:
    """``said`` with anything that could be a connection string taken out.

    The one rule about showing what a database driver said. A url is where a
    password lives, and a failure to connect is exactly the moment somebody
    copies the message into a ticket.
    """
    return _A_SECRET.sub(REDACTED, said)


class DatabaseUnreachableError(RobinautsError):
    """The deployment's PostgreSQL could not be opened.

    Not a request's fault and not a bug: a database that is not there, a name
    that does not exist, credentials the server refused, a connection string
    the driver will not read. Every one of them is something an operator
    changes, and every one of them arrives as an exception of the **driver's**
    -- which nothing above ``datastore`` may name (``docs/layout.md``), and
    which would otherwise reach a command as a traceback.

    So ``datastore.open_pool`` turns them into this, and a command prints it
    as one line (``robinauts.legacy.cli``). The driver's own sentence is kept,
    because it is what says which of the five it was; the connection string is
    **not**, because it holds the password (``without_secrets``).
    """

    @classmethod
    def from_driver(cls, problem: BaseException) -> DatabaseUnreachableError:
        """This error for what a driver raised, with nothing quotable in it."""
        said = without_secrets(str(problem)) or type(problem).__name__
        return cls(f"the database could not be opened: {said}")


class SchemaError(RobinautsError):
    """The database is not the one this build was written against.

    Every way that can be true ends in the same two refusals -- the schema is
    not created, and the server does not start -- because a server that ran
    against a schema it does not know would write rows nothing can read back
    (``docs/specs/backend.md``). What differs is only what the operator is
    told they are looking at, so the shapes are the constructors below and
    the advice is one sentence, written once.

    Until the first release there are no migrations: a database of any
    other version is **made again**, not upgraded, and the
    command that creates the schema works on an empty database only. Saying
    that in every message is deliberate -- the alternative is an operator
    running the command on the database that already has their data in it.
    """

    ADVICE = (
        f"`{DB_INIT_COMMAND}` creates the schema, and until the first release it works"
        " on an empty database only: a database of any other version is made again"
    )

    def __init__(
        self,
        problem: str,
        *,
        expected: int,
        found: int | None = None,
        advice: str | None = None,
    ) -> None:
        self.problem = problem
        """What is wrong, without the advice: one clause, for a log line."""
        self.expected = expected
        self.found = found
        """The version in the database, or ``None`` if it has no usable one."""
        self.advice = advice or self.ADVICE
        """What to do. Recreating the database, unless something else fixes it."""
        super().__init__(f"{problem}; this build needs schema version {expected}. {self.advice}")

    @classmethod
    def missing(cls, expected: int) -> SchemaError:
        """There is nothing of ours in this database at all."""
        return cls("the database has no Robinauts schema", expected=expected)

    @classmethod
    def no_schema(cls, expected: int, path: str) -> SchemaError:
        """The connection's search path names nothing that exists.

        Then there is no schema to look in and none to create in either: an
        unqualified ``CREATE TABLE`` has nowhere to go. The command cannot
        help, so it is not the thing to suggest.
        """
        return cls(
            f"the connection's search path ({path}) names no schema that exists",
            expected=expected,
            advice=(
                "create the schema in the database, or point the search path at one that"
                " is there"
            ),
        )

    @classmethod
    def mismatch(cls, expected: int, found: int) -> SchemaError:
        """There is a schema, of a version this build was not written for."""
        return cls(f"the database is at schema version {found}", expected=expected, found=found)

    @classmethod
    def stale(cls, expected: int, recorded: str | None) -> SchemaError:
        """The right version, made from another edit of ``schema.sql``.

        Until the first release the version is 1 whatever edit a database
        was made from, so the hash ``robinauts db init`` records is what
        tells them apart. ``None`` is a file applied by hand, which recorded
        nothing, and is refused the same way: there is no telling which edit
        it was.
        """
        return cls(
            "the database was made from an older schema.sql"
            + ("" if recorded else " (or not by `robinauts db init`)"),
            expected=expected,
            found=expected,
            advice=(
                f"drop it and run `{DB_INIT_COMMAND}` again: until the first release the"
                " schema is edited in place, and a database made from an older schema.sql"
                " is made again rather than upgraded"
            ),
        )

    @classmethod
    def unversioned(cls, expected: int, tables: Iterable[str]) -> SchemaError:
        """Our tables are there and no version is recorded.

        A database somebody made by hand, or one whose creation stopped half
        way. Either way there is no telling what shape those tables are in,
        so it is not a database to add the rest of a schema to.
        """
        return cls(
            "the database holds Robinauts tables"
            f" ({', '.join(sorted(tables))}) but records no schema version",
            expected=expected,
        )

    @classmethod
    def unreadable(cls, expected: int) -> SchemaError:
        """There is a ``schema_version`` table, and it is not ours.

        Another shape, another meaning, or another project's: a version that
        cannot be read is a version that cannot be trusted, and guessing it
        is how a server ends up writing into somebody else's tables.
        """
        return cls(
            "the database has a schema_version table this build cannot read,"
            " so the schema in it is of an unknown version",
            expected=expected,
        )

    @classmethod
    def shadowed(cls, expected: int, tables: Iterable[str]) -> SchemaError:
        """The schema is right, and it is not the one the queries would reach.

        PostgreSQL resolves an unqualified table name through ``search_path``,
        and the schema checked is the one the definition was created in. If
        something earlier on the path answers to the same name, the two part
        company: the check passes, and every statement afterwards goes
        somewhere else. It is a configuration to correct, not a database.
        """
        return cls(
            "the search path reaches other tables by these names before the schema's own"
            f" ({', '.join(sorted(tables))}), so the queries would not go where the"
            " schema is",
            expected=expected,
            found=expected,
            # Not a database to make again: nothing is wrong with it.
            advice=(
                "set the connection's search path so that the schema holding the Robinauts"
                " tables is the first one on it"
            ),
        )

    @classmethod
    def incomplete(cls, expected: int, missing: Iterable[str]) -> SchemaError:
        """The version is right and the schema is not all there."""
        return cls(
            f"the database records schema version {expected} but does not have"
            f" every table this build expects (missing: {', '.join(sorted(missing))})",
            expected=expected,
            found=expected,
        )


class SignInErrorCode(StrEnum):
    """What the sign-in page is told when a sign-in does not complete.

    The set is fixed and public: it reaches the browser as a query parameter,
    so a code names a kind of failure and never the provider's own words.
    """

    EXPIRED = "expired"
    """The sign-in took longer than a pending sign-in lives, or was replayed."""
    STATE_MISMATCH = "state_mismatch"
    """The callback's ``state`` is not one this deployment handed out."""
    NOT_ALLOWED = "not_allowed"
    """The provider says who they are; the allow list does not have them."""
    UNKNOWN_PROVIDER = "unknown_provider"
    """No provider of that id is configured."""
    BUSY = "busy"
    """Too many sign-ins are already in progress."""
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """The provider could not be reached, or answered nothing usable."""
    PROVIDER_REFUSED = "provider_refused"
    """The provider refused the sign-in, or the authorization code."""
    INVALID_ID_TOKEN = "invalid_id_token"
    """The ID token is not one for this sign-in."""


class SignInError(RobinautsError):
    """A sign-in that cannot complete.

    ``code`` is what the browser is told; ``detail`` is for the log alone and
    may hold what a provider said.
    """

    def __init__(self, code: SignInErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


class NotAllowedError(SignInError):
    """The provider authenticated someone the allow list does not accept."""

    def __init__(self, detail: str) -> None:
        super().__init__(SignInErrorCode.NOT_ALLOWED, detail)


class UnknownProviderError(SignInError):
    """A sign-in was asked for with a provider id that is not configured."""

    def __init__(self, detail: str) -> None:
        super().__init__(SignInErrorCode.UNKNOWN_PROVIDER, detail)


class InvalidIdTokenError(SignInError):
    """An ID token whose claims do not belong to this sign-in."""

    def __init__(self, detail: str) -> None:
        super().__init__(SignInErrorCode.INVALID_ID_TOKEN, detail)


class ProviderUnavailableError(SignInError):
    """The provider could not be reached, or answered something unusable."""

    def __init__(self, detail: str) -> None:
        super().__init__(SignInErrorCode.PROVIDER_UNAVAILABLE, detail)
