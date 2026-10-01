# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Starting a turn and following one: the streaming half of the wire.

::

    POST /api/turns                             begin a conversation and answer
    POST /api/conversations/{id}/turns          a message in one, or one again
    GET  /api/runs/{run_id}/events              re-attach to a run

Each answers the **AG-UI event stream of a run**, over server-sent events
(``docs/specs/wire.md``). A POST creates the run and streams it from its first
event; the GET attaches to one that already exists, after the position the
caller last saw.

**The stream is a view of the run, not the run** (``docs/specs/runs.md``).
Closing it changes nothing: the run goes on, its events are kept under their
positions, and whoever comes back reads what they missed. So a client that
drops in the middle of an answer loses nothing, and neither does one that never
watched at all.

**The profile is ours, and it is documented with the API.** A stock AG-UI
server is handed the history in the request; here the server loads it from its
own store and the request names the conversation and either a new message or
the answer to produce again. That is what these bodies are, and
``docs/specs/wire.md`` is where they are written down -- **these routes are
outside the OpenAPI document** (``docs/specs/backend.md``), because a streaming
endpoint described in it would have a generated client believe it could read
the response as JSON.

**An ``id:`` is the platform's own numbering of the run's events**, and it goes
on the **last** wire event derived from each of them (``_derived``). That is
what makes re-attaching native: a browser's ``EventSource`` sends the last id
it saw back as ``Last-Event-ID``, which this reads as ``after``, and an id
therefore means "everything derived from the run's events up to here has been
sent". An event without one -- a bracket this layer derived, or an ending it
made up -- is never something to re-attach after. The two ways of saying where
to carry on from may not disagree.

**A refusal is a status, and therefore comes before the stream.** Once a byte
of a 200 has gone out there is no status left to send, so everything a request
can be refused for is decided first: who is asking, what the body says, whether
the conversation is already answering, and -- for a re-attach -- whether that
run is this person's (``application.Watch.run``, asked before a stream is
opened). What can only go wrong afterwards goes out as an AG-UI ``RUN_ERROR``
and ends the stream.

**Every stream ends with an event that says the run is over**: the run's own
end, mapped; the same thing read from the run's record where the slice held no
ending -- re-attaching at or past the last position is answered with how the
run really ended, not with an error; or ``RUN_ERROR`` with one of this
module's codes. A stream that merely closed would be read by an
``EventSource`` as a connection to make again, and a client would re-attach to
a run that ended long ago for ever.

**Heartbeats keep the connection.** A comment line every ``HEARTBEAT_SECONDS``
while nothing is arriving, which is what stops a proxy closing a quiet stream;
they are comments, so no client has to know about them.

**Nothing here creates a task for a run.** The work of a run belongs to the
application and its executor (``docs/layout.md``); the one task this opens is
the reader of the watcher's generator, and it exists only so that a heartbeat
can be written while that read is waiting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator

from ag_ui.core import BaseEvent, RunErrorEvent
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from robinauts.legacy.api.access import SignedIn, turning, watching
from robinauts.legacy.api.agui import (
    GONE_CODE,
    INTERNAL_CODE,
    QUIET_CODE,
    SSE_MEDIA_TYPE,
    AguiMapper,
    sse,
)
from robinauts.legacy.api.errors import GENERIC_DETAIL, QUIET_RUN_DETAIL
from robinauts.legacy.api.protection import StrictJson, given_once
from robinauts.legacy.api.schemas import NewChatRequest, TurnRequest
from robinauts.legacy.application import Watch
from robinauts.legacy.domain import (
    MAX_POSITION,
    InvalidValueError,
    RunEnded,
    RunEvent,
    RunNotFoundError,
    RunQuietError,
    User,
    chain,
    where,
)

_log = logging.getLogger(__name__)

stream_router = APIRouter(prefix="/api", tags=["turns"])

HEARTBEAT_SECONDS = 15.0
"""How long a stream stays silent before it says it is still there.

Proxies and load balancers close a connection that has said nothing for a
while, and a turn may think for longer than that before its first word. Short
enough to be under the idle timeout of anything ordinary in front of the
deployment, long enough that a thousand streams are not a thousand writes a
second.
"""

KEEP_ALIVE = ": keep-alive\n\n"
"""The heartbeat: an SSE **comment**, which every client ignores by the spec.

A comment rather than an event of our own, so that nothing on the other side
has to know this exists, and so that it can never be mistaken for a position or
for something the run did.
"""

RUN_ID_HEADER = "x-robinauts-run-id"
CONVERSATION_ID_HEADER = "x-robinauts-conversation-id"
"""What the run and the conversation are named in, on the response itself.

A client that got the headers and then lost the connection -- before the first
event, which is where a turn is most likely to be interrupted -- has everything
it needs to re-attach: the run to ask about and the conversation to reload.
Reading them out of the first event instead would mean there is a moment in
which a started turn cannot be found again.
"""

STREAM_HEADERS = {
    # A stream of one person's conversation is not a thing to keep anywhere:
    # not in a browser's cache, not in a proxy's.
    "cache-control": "no-store",
    # nginx buffers a proxied response by default, which turns a stream into
    # one answer at the end of the turn. This is how it is told not to.
    "x-accel-buffering": "no",
}
"""What every stream carries besides its content type and the security headers."""

AFTER = "after"
"""The one query parameter these routes take, and the one ``given_once`` asks
about: a name written once for the route and the test that holds them together."""

LAST_EVENT_ID = "last-event-id"
"""The header a browser's ``EventSource`` re-attaches with, by itself."""

MAX_POSITION_DIGITS = len(str(MAX_POSITION))
"""How long the ``Last-Event-ID`` of a stream of ours can be, in characters.

Derived from the bound itself, so it cannot drift from it. It is checked
**before** the header is read as a number: CPython refuses to convert a decimal
integer of more than a few thousand digits and raises a ``ValueError`` that is
none of ours, which would make a header somebody chose the length of into a
500.
"""

ONE_FORM = (
    "a turn carries either a message to send, with the message it hangs under,"
    " or the answer to produce again -- not both and not neither"
)
"""What a body that is neither shape of a turn is refused with.

Checked here rather than by a discriminated union, whose tag pydantic writes
into the ``location`` of its error -- and a location is built out of pieces
that are **ours** (``robinauts.legacy.api.errors``).
"""

POSITIONS_DISAGREE = (
    f"{AFTER} and {LAST_EVENT_ID} are two ways of saying one thing and these say two"
)
"""What a request that asks from two different positions is refused with."""

POSITION_AHEAD = "the position is past what this run has stored"
"""What a re-attach to somewhere a **running** run has not reached is refused with.

Not a stream that waits: everything after the position is nothing, so the
watcher would sit out the whole silence a run is given and then give up on it,
putting a line in the log about a run that is answering perfectly well
(``application.Watch``). The client asked for something that is not there yet,
and it is told so.
"""

UNREADABLE_POSITION = f"{LAST_EVENT_ID}: is not a position of this stream"
"""What an unreadable ``Last-Event-ID`` is refused with; never what was in it.

It arrives from a browser like anything else, and this build sends only whole
numbers in an ``id:``. One that is not a position it could have sent is refused
rather than read as the beginning of the run, which would replay a whole answer
to somebody who had already seen it.
"""

GONE_DETAIL = "this run is no longer there; open the conversation again to see what is"
"""What ends the stream of a run that is not there any more.

**Only** that: a run whose conversation was deleted under the watcher. A run
that ended before the position asked from is not this -- its outcome is known
and is sent (``_outcome``) -- and a stream that said ``gone`` for one would
show a failure over an answer that is complete.
"""


@stream_router.post("/turns", include_in_schema=False)
async def begin_chat(
    request: Request, user: SignedIn, asked: NewChatRequest, read_once: StrictJson
) -> StreamingResponse:
    """Begin a conversation with an agent, and stream the run answering it.

    The conversation, its first question and the run are one write of the
    application's (``application.Turns.start``), and the answer to this request
    is the stream of that run from its first event. The conversation's id is in
    the response's headers, since there was none to name in the request.

    The conversation runs on ``model_id``, or on the agent's default when the
    body names none (``schemas.NewChatRequest``). An agent this deployment
    has not got is 404, like everything else that is not there, and is looked
    for first; a model it does not offer is 422 (``UnknownModelError``), and so
    is a message with nothing in it.

    **Both services are taken before the turn is begun.** A deployment whose
    lifespan has not run has neither (``access.NOT_WIRED``), and finding that
    out after a run had been created would leave a run nobody asked for
    answering a question nobody was told about.
    """
    watch, turns = watching(request), turning(request)
    started = await turns.begin(
        user, agent_id=asked.agent_id, model_id=asked.model_id, text=asked.text
    )
    return _streaming(watch, user, started.run.id, started.conversation.id, after=0)


@stream_router.post("/conversations/{conversation_id}/turns", include_in_schema=False)
async def begin_turn(
    request: Request,
    user: SignedIn,
    conversation_id: uuid.UUID,
    asked: TurnRequest,
    read_once: StrictJson,
) -> StreamingResponse:
    """A turn in a conversation that exists: a new message, or one made again.

    The two shapes and the one rule about them are ``schemas.TurnRequest``:
    ``text`` with its ``parent_id``, or ``regenerate``. A ``parent_id`` beside
    ``regenerate`` is the same refusal, because a regeneration hangs where the
    turn it replaces hung and nothing else can be asked of it.

    **A conversation that is already answering is 409**
    (``RunAlreadyActiveError``): one run at a time, decided inside the write
    that would have created the second one. Its author cancels the first
    (``POST .../runs/{run_id}/cancel``) and asks again.

    A conversation that is not there, one that is somebody else's, and a parent
    or an answer that is no message of it all answer the same 404: the rule is
    the application's and no route here looks at an owner. **A conversation
    whose model the operator has since removed is 409**
    (``ModelNotOfferedError``), decided after the conversation was found to be
    this person's: the turn is refused rather than answered by another model,
    and its author moves it to one that is offered
    (``PUT /api/conversations/{id}/model``).
    """
    if (asked.text is None) == (asked.regenerate is None):
        raise InvalidValueError(ONE_FORM)
    # Both services first, as in ``begin_chat`` and for the same reason.
    watch, turns = watching(request), turning(request)
    if asked.text is not None:
        started = await turns.begin(
            user,
            conversation_id=conversation_id,
            text=asked.text,
            parent_id=asked.parent_id,
        )
    else:
        if asked.parent_id is not None:
            raise InvalidValueError(ONE_FORM)
        started = await turns.begin_again(
            user, conversation_id=conversation_id, message_id=asked.regenerate
        )
    return _streaming(watch, user, started.run.id, started.conversation.id, after=0)


@stream_router.get("/runs/{run_id}/events", include_in_schema=False)
async def run_events(
    request: Request,
    user: SignedIn,
    run_id: uuid.UUID,
    after: int | None = Query(None, ge=0, le=MAX_POSITION),
) -> StreamingResponse:
    """Re-attach to a run: everything after a position, then the rest of it.

    ``after`` is the position the caller last saw -- ``0``, or nothing at all,
    asks for the run from its beginning, and what a client that has just opened
    the conversation sends is the ``resume.after`` it was given there, so that
    it is replayed the message still being produced and none it already has
    (``docs/specs/runs.md``).

    A browser's ``EventSource`` says the same thing in ``Last-Event-ID`` by
    itself, which is why an event carries its position as its id. The header is
    **always read** -- it is what a request carrying no ``after`` is answered
    from, and the two saying different things is refused rather than settled: a
    client that sent both means one of them, and which one this preferred would
    be a rule built on a coincidence.

    The refusal for a run that is not there and for one in somebody else's
    conversation is the same 404, and it is answered **before the stream
    begins** (``application.Watch.run``).

    **A stream that carries on from somewhere is seeded with what came
    before.** The events AG-UI wants around thinking are derived from the
    sequence, so a stream beginning in the middle of a stretch of thinking
    would not close what the client holds open. ``Watch.before`` is read here,
    where a refusal is still a status, and the mapper is given it
    (``AguiMapper.seed``); a stream from the beginning has nothing to be
    seeded with.

    **A position a run that is still going has not reached is refused** (422,
    ``POSITION_AHEAD``) rather than waited on. One that has **ended** is not:
    past its end there is nothing to wait for, and how it ended is what comes
    back.
    """
    given_once(request, AFTER)
    position = _position(request, after)
    watch = watching(request)
    run = await watch.run(user, run_id)
    seed = await watch.before(user, run_id, upto=position) if position else ()
    if run.is_active and position and (not seed or seed[-1].seq < position):
        # **A position the run has not reached.** Positions are numbered from
        # one with no gaps, so the events up to ``position`` are all of them
        # when there are fewer: this asks to carry on from somewhere the run
        # has not got to. Watching it would send nothing and then give up on a
        # perfectly healthy run, saying in the log that the run had gone quiet
        # -- about a client that asked the wrong thing. A run that has
        # **ended** past its end is not this: there is nothing to wait for and
        # its outcome is what comes back.
        raise InvalidValueError(POSITION_AHEAD)
    return _streaming(watch, user, run.id, run.conversation_id, after=position, seed=seed)


def _position(request: Request, after: int | None) -> int:
    """Where to carry on from: the query parameter, or the header, or 0."""
    sent = request.headers.getlist(LAST_EVENT_ID)
    if len(sent) > 1:
        # Two of a header is one client and one proxy disagreeing; neither of
        # them asked for what the other did.
        raise InvalidValueError(f"{LAST_EVENT_ID}: given more than once")
    said = sent[0].strip() if sent else ""
    if not said:
        # An ``EventSource`` that has seen nothing sends no id, or an empty
        # one; both mean "from wherever the query says", which is the beginning
        # when it says nothing either.
        return after or 0
    if len(said) > MAX_POSITION_DIGITS:
        # **Before ``int``**: CPython refuses to read a decimal integer of more
        # than a few thousand digits and raises ``ValueError`` (which is not
        # one of ours, so it would be a 500). A header is as long as its sender
        # likes, and nothing here ever sent an id of more than this many
        # digits.
        raise InvalidValueError(UNREADABLE_POSITION)
    if not (said.isascii() and said.isdigit()):
        # ``isdigit`` alone is true of digits from any script, and ``int``
        # reads those: an id nothing here could have sent would then be taken
        # for a position.
        raise InvalidValueError(UNREADABLE_POSITION)
    seen = int(said)
    if seen > MAX_POSITION:
        raise InvalidValueError(UNREADABLE_POSITION)
    if after is not None and after != seen:
        raise InvalidValueError(POSITIONS_DISAGREE)
    return seen


def _streaming(
    watch: Watch,
    user: User,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    after: int,
    seed: tuple[RunEvent, ...] = (),
) -> StreamingResponse:
    """The response a stream crosses as: its headers, and the events after them."""
    return StreamingResponse(
        _sent(watch, user, run_id, conversation_id, after=after, seed=seed),
        media_type=SSE_MEDIA_TYPE,
        headers={
            **STREAM_HEADERS,
            RUN_ID_HEADER: str(run_id),
            CONVERSATION_ID_HEADER: str(conversation_id),
        },
    )


async def _sent(
    watch: Watch,
    user: User,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    after: int,
    seed: tuple[RunEvent, ...] = (),
) -> AsyncIterator[str]:
    """The run's events, as AG-UI over SSE, with a heartbeat while it is quiet.

    ``seed`` is what the run said before ``after`` and is **not sent**: it is
    run through the mapper to leave it in the state an unbroken stream would
    have reached, so that a re-attach in the middle of a stretch of thinking
    closes it (``AguiMapper.seed``).

    **The watcher is read by a task, and this waits on the task.** The watcher
    waits in bounds of its own (``application.Watch``), and a wait cut short by
    ``asyncio.wait_for`` would be a cancellation delivered inside its generator
    -- which is the end of the stream, not a heartbeat. So the read is a task,
    this waits for it with a timeout, and a timeout writes the comment line and
    **waits for the same task again**: nothing is interrupted, and the only
    thing the heartbeat costs is one task per stream.

    Everything that can go wrong from here on has a status already sent, so it
    goes out as an event and the stream ends:

    - the watcher gave up on a run that stored nothing (``RunQuietError``):
      ``quiet``, with the same fixed sentence the 504 of that carries;
    - the slice held no ending of the run's own: the run is read again and
      how it ended is sent, or ``gone`` where it is no longer there at all
      (``_outcome``);
    - anything else: ``internal``, saying no more than a 500 body does, with
      the whole chain and the frames of it in the log. **Never a traceback on
      the wire.**
    """
    mapper = AguiMapper(thread_id=conversation_id)
    events = watch.events(user, run_id, after=after)
    reading: asyncio.Task[RunEvent] | None = None
    ended = False
    try:
        # **Inside**, because the response has begun: anything the seeding
        # raises is a mistake of ours that has to leave as an event and a log
        # line rather than as a generator that stopped saying anything.
        mapper.seed(seed)
        while True:
            if reading is None:
                reading = asyncio.get_running_loop().create_task(_read(events))
            done, _ = await asyncio.wait((reading,), timeout=HEARTBEAT_SECONDS)
            if not done:
                yield KEEP_ALIVE
                continue
            read, reading = reading, None
            try:
                event = read.result()
            except StopAsyncIteration:
                break
            for chunk in _derived(mapper.of(event), event.seq):
                yield chunk
            if isinstance(event.event, RunEnded):
                ended = True
                break
    except asyncio.CancelledError:
        # The client went away, or the process is stopping. Nothing about the
        # run changes: this was a view of it (``docs/specs/runs.md``).
        raise
    except RunNotFoundError:
        # The conversation was deleted between the check this route made and
        # the watcher's own. Not a fault: the run is gone, which is a thing to
        # say rather than a line in the log (``_outcome``).
        for chunk in _ending(mapper, GONE_CODE, GONE_DETAIL):
            yield chunk
    except RunQuietError:
        # ``application.Watch`` has already said at WARNING which run it gave
        # up on and why; this is what the person watching is told.
        for chunk in _ending(mapper, QUIET_CODE, QUIET_RUN_DETAIL):
            yield chunk
    except Exception as failure:  # noqa: BLE001 -- a stream ends, it does not 500
        _log.error(
            "the stream of run %s could not be served: %s | at %s",
            run_id,
            chain(failure),
            where(failure),
        )
        for chunk in _ending(mapper, INTERNAL_CODE, GENERIC_DETAIL):
            yield chunk
    else:
        if not ended:
            for chunk in await _outcome(watch, user, run_id, mapper):
                yield chunk
    finally:
        if reading is not None:
            reading.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reading
        with contextlib.suppress(Exception):
            await events.aclose()


async def _outcome(watch: Watch, user: User, run_id: uuid.UUID, mapper: AguiMapper) -> list[str]:
    """How to end a stream whose slice held no ending of the run's own.

    A watcher stops without one in two situations, and they are **not** the
    same thing to tell somebody (``docs/specs/runs.md``):

    - **the run ended at or before the position asked from**, so there was
      nothing left to send. Its outcome is not in doubt -- the record says it
      -- and answering an error over an answer that is complete would have a
      client show a failure for a finished turn. So the run is read again and
      the terminal event its state maps to is sent, through the same mapping
      as a ``RunEnded`` that did arrive (``AguiMapper.closed``);
    - **the run is no longer there at all**, its conversation having been
      deleted under the watcher. Nothing says how it ended, because nothing
      says anything about it: that is ``gone``.

    A run that is **there and still active** cannot end a stream this way --
    the watcher waits, or gives up and raises -- so it is a mistake of ours:
    the log gets a line and the client the same nothing a 500 body says.
    """
    try:
        run = await watch.run(user, run_id)
    except RunNotFoundError:
        return _ending(mapper, GONE_CODE, GONE_DETAIL)
    except Exception as failure:  # noqa: BLE001 -- a stream ends, it does not 500
        _log.error(
            "how run %s ended could not be read: %s | at %s",
            run_id,
            chain(failure),
            where(failure),
        )
        return _ending(mapper, INTERNAL_CODE, GENERIC_DETAIL)
    if run.is_active:
        _log.error(
            "the stream of run %s ended with no ending while the run is still %s",
            run_id,
            run.state.value,
        )
        return _ending(mapper, INTERNAL_CODE, GENERIC_DETAIL)
    return [sse(event) for event in mapper.closed(run)]


async def _read(events: AsyncIterator[RunEvent]) -> RunEvent:
    """The next event of a watcher, as a coroutine a task can carry.

    A task rather than an awaited call, so that a heartbeat can be written
    while it waits; a coroutine rather than the bare ``__anext__`` awaitable,
    because what a task is made of should be one plain thing.
    """
    return await anext(events)


def _derived(sending: tuple[BaseEvent, ...], position: int) -> list[str]:
    """Those events, with the position on the **last** of them and no other.

    One platform event can become more than one wire event: the brackets AG-UI
    wants around thinking are derived here and the platform numbered none of
    them. An ``id:`` says "everything derived from the run's events up to this
    position has been sent", so it belongs on the last of a group and nowhere
    inside it.

    Putting it on every one of them loses an event. A client whose
    ``Last-Event-ID`` was the id of a bracket -- a ``REASONING_MESSAGE_END``
    carrying the position of the text delta that closed the thinking -- would
    re-attach *after* that platform event and never receive the delta itself,
    silently and with nothing out of order to notice it by.

    A client that re-attaches at the last id it saw is therefore replayed no
    platform event it has had in full, and the brackets of the one it is in the
    middle of are derived again by the new stream's own mapper.
    """
    last = len(sending) - 1
    return [
        sse(event, position=position if index == last else None)
        for index, event in enumerate(sending)
    ]


def _ending(mapper: AguiMapper, code: str, message: str) -> list[str]:
    """How a stream ends when the run's own end is not what ends it.

    Whatever the mapper has left open is closed first -- a thinking block
    nothing closed would be shown for ever -- and then the one error event,
    which carries **no position**: the platform numbered nothing here, and a
    client that re-attaches asks from the last real one.
    """
    events: list[BaseEvent] = [*mapper.closing(), RunErrorEvent(message=message, code=code)]
    return [sse(event) for event in events]
