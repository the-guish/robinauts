# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The conversations of the person asking: the plain JSON API.

::

    GET    /api/conversations                          the panel's list, paged
    GET    /api/conversations/{id}                     open one: its thread and the run
    PATCH  /api/conversations/{id}                     rename
    PUT    /api/conversations/{id}/model               move it to another model
    DELETE /api/conversations/{id}                     delete, for good
    POST   /api/conversations/{id}/runs/{run_id}/cancel  stop the answer

Everything a chat client does but write in a conversation: starting a turn and
watching one arrive are the streaming half of the wire and are not here
(``docs/specs/wire.md``), and the agents and models a conversation can be
started with are ``robinauts.api.agent_routes``. There is nothing browser-shaped about any
of them -- one API, for every channel (``docs/specs/channels.md``).

**Every route here may also answer 400, 405 and 500**, in the same
``ErrorResponse`` shape as every refusal it declares: a request the server
could not read at all, a method that is not served at a path that is, and a
mistake of ours, which says only that the request could not be served
(``robinauts.api.errors``). They are described
**once**, as the document's ``default`` answer
(``robinauts.api.refusals.ANYTHING_ELSE``), because they are not a route's own
answer -- any route can be asked with the wrong method, and any can meet a
bug.

**HEAD is not served.** A GET here answers a GET, and nothing turns it into a
HEAD: asking for one is a 405 naming the methods there are. Nothing calls
these routes that way -- a client that wants the size of a conversation asks
for it -- and ``robinauts.api.ui``, which serves the built interface, is where
HEAD is answered for the static side, which is the part a browser and a cache
really do ask it of (``docs/specs/frontend.md``).

**A path is a path.** The application does not redirect a trailing slash
(``robinauts.api.create_api``): ``/api/conversations/`` is not a route, so it
is 404 rather than a 307 to somewhere else. A generated client that met a
redirect would follow it with the method and body changed by whatever it was
built on, and a redirect is not something an API should need.

**Every route needs a session and acts for the person the session names.**
The ``User`` comes from the guard (``robinauts.api.access``), and it is passed
to the application, which is where ownership is decided: no route here reads
an owner, compares an id or branches on whose something is. That rule lives in
one function (``application.owner_of``) so that a route written later cannot
apply it slightly differently, and so that a conversation reached directly and
one reached through a run of it are judged the same way.

**One body for everything that is not there.** A conversation that never
existed, one that belongs to somebody else, a message that is no message of
this conversation, a run that is not this conversation's run: all of them
answer the *same* status, the *same* body and the *same* headers -- 404,
``NotFoundError``, one fixed sentence (``robinauts.api.errors``). The
difference between "no such id" and "not yours" is the whole of what an
attacker wants from an id, and it reaches the log alone. There is therefore no
route here that can tell a client an id exists.

**Paging.** ``GET /api/conversations`` answers the caller's conversations most
recently updated first, ``limit`` at a time, with ``next_cursor`` to ask for
the next page with -- ``null`` when there is no next page. The cursor is
**opaque**: a position inside this person's own listing, written and read by
the store, unsigned because it can reach nothing else, and one that does not
parse is refused as a value rather than read as a position. What the keyset
promises is that each conversation is seen once *while nothing changes*:
paging walks a list that is being written to, so a conversation written to
during a pass moves in the order and may be missed in that pass or seen twice
in it, and a client that minds folds by id (``docs/specs/conversations.md``).

**The services are read from the application's state**, as the sign-in is:
the composition root opens them in the lifespan and puts them there
(``robinauts.app``), so nothing here constructs or holds one. Before start-up
there are none, and that is a programming error rather than a state a client
can be told about -- ``access.NOT_WIRED``, answered as any other mistake of
ours is.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, Response, status

from robinauts.api.access import SignedIn, conversing, turning
from robinauts.api.protection import StrictJson, given_once
from robinauts.api.refusals import DELETING, OPENING, READING, WRITING
from robinauts.api.schemas import (
    ConversationListResponse,
    ConversationSummary,
    OpenedConversationResponse,
    RenameRequest,
    RunView,
    SetModelRequest,
)
from robinauts.application import DEFAULT_PAGE, MAX_PAGE
from robinauts.domain import InvalidCursorError, InvalidValueError

conversation_router = APIRouter(prefix="/api", tags=["conversations"])

PAGING = ("limit", "cursor")
"""Every query parameter the listing takes, and therefore every one
``given_once`` is asked about. One name for the route and the test that holds
the two together (``test_every_query_parameter_is_given_once``), so a third
one added later is not quietly left unchecked."""


@conversation_router.get("/conversations", responses=READING)
async def list_conversations(
    request: Request,
    user: SignedIn,
    limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    cursor: str | None = None,
) -> ConversationListResponse:
    """The caller's conversations, most recently updated first, one page at a time.

    ``limit`` is how many are wanted, and its bound is **in the document**:
    between 1 and ``application.MAX_PAGE``, which is what a store will page.
    A client can therefore hold itself to it instead of finding out by being
    refused, and one outside it -- or not a whole number at all, an empty
    ``?limit=`` included -- is refused naming the field and the rule and never
    the number.

    ``cursor`` is what the page before this one handed back as
    ``next_cursor``, and is left out for the first page; an **empty** one is
    read as left out, since a client that sends no cursor and one that sends
    nothing in it are asking the same thing. One that does not parse is
    refused as a value, naming the field.

    Either parameter **given twice** is refused rather than quietly read once
    (``given_once``).

    The order is total: ties on ``updated_at`` are broken by id, so two
    conversations written in the same millisecond cannot swap places between
    two pages. What that does and does not promise is in this module's
    docstring.
    """
    given_once(request, *PAGING)
    try:
        page = await conversing(request).list_for(user, limit=limit, cursor=cursor or None)
    except InvalidCursorError as refused:
        # Caught by its own class rather than as any refused value: a store
        # says what is wrong with a cursor, and this route is what knows the
        # parameter it came in on. Nothing else a listing could refuse is
        # relabelled as the cursor by standing in the way.
        raise InvalidCursorError(f"query.cursor: {refused}") from refused
    return ConversationListResponse(
        items=[ConversationSummary.of(conversation) for conversation in page.conversations],
        next_cursor=page.cursor,
    )


@conversation_router.get("/conversations/{conversation_id}", responses=OPENING)
async def open_conversation(
    request: Request, user: SignedIn, conversation_id: uuid.UUID
) -> OpenedConversationResponse:
    """One conversation, read at **one moment**: its thread and its run.

    ``messages`` is the one thread the conversation shows, oldest first: the
    path to its newest message, and nothing an edit or a regeneration put
    aside (``docs/specs/conversations.md``). What was put aside is kept in
    the store and never sent.

    When a run is in flight, ``run_id`` and ``resume`` say which run and where
    to attach to its stream, so that a client which has just loaded every
    complete message is replayed exactly the message still being produced and
    none it already has (``docs/specs/runs.md``). When no run is in flight and
    the last one failed, was cancelled or was interrupted, ``ended_badly``
    says so, so that a reload after an answer went wrong is told rather than
    shown a turn that stops in the middle.

    The three come from one read of the store, because two reads would
    disagree with each other and the gap between them is exactly where an
    answer is.
    """
    return OpenedConversationResponse.of(await conversing(request).open(user, conversation_id))


@conversation_router.patch("/conversations/{conversation_id}", responses=WRITING)
async def rename_conversation(
    request: Request,
    user: SignedIn,
    conversation_id: uuid.UUID,
    asked: RenameRequest,
    read_once: StrictJson,
) -> ConversationSummary:
    """Give a conversation a new title; the conversation as it then is.

    A renamed title is never overwritten afterwards
    (``docs/specs/conversations.md``). How long a title may be is in the
    document (``schemas.RenameRequest``); what a length cannot say -- one
    line, printable, and something other than spaces -- is the application's,
    and what it says is the rule. Which field it is about is this route's to
    name, since the title is the one value it passes.
    """
    try:
        renamed = await conversing(request).rename(user, conversation_id, asked.title)
    except InvalidValueError as refused:
        # The title is the only value this route hands ``rename``: the id is a
        # uuid the document parsed, and a conversation that is not there is a
        # ``NotFoundError`` and not this. So a refused value is the title's.
        raise InvalidValueError(f"body.title: {refused}") from refused
    return ConversationSummary.of(renamed)


@conversation_router.put("/conversations/{conversation_id}/model", responses=WRITING)
async def set_model(
    request: Request,
    user: SignedIn,
    conversation_id: uuid.UUID,
    asked: SetModelRequest,
    read_once: StrictJson,
) -> ConversationSummary:
    """Move a conversation to another model; the conversation as it then is.

    Its next turn runs on that model, and so does every one after it until it
    is moved again (``docs/specs/agents.md``). **A run in flight is no reason
    to refuse**: it keeps the model it started with, which is what its answer
    records, and the change is what the next turn runs on.

    A ``PUT``, because the body is the whole of the conversation's model
    rather than a change to it.

    **A model the deployment does not offer is 422** (``UnknownModelError``,
    with a fixed sentence naming the field), as it is for a new chat: the
    model is the one value this route takes, and a 404 at a path that names
    a conversation would read as the conversation not being there.

    **The model is checked first**, before the conversation is looked up, as
    a rename checks its title: nothing is read for a value that is refused
    anyway. So a model the deployment does not offer is 422 whatever the
    conversation -- one that is not there and one that is somebody else's
    included -- which says nothing about the conversation, because the answer
    does not depend on it. With a model that is offered, a conversation that
    is not there, or not this person's, is the 404 every route here answers.
    """
    try:
        moved = await turning(request).set_model(user, conversation_id, asked.model_id)
    except InvalidValueError as refused:
        # The model's id is the only value this route hands ``set_model``, as
        # the title is ``rename``'s: the conversation's id is a uuid the
        # document parsed, and the person is the session's.
        raise InvalidValueError(f"body.model_id: {refused}") from refused
    return ConversationSummary.of(moved)


@conversation_router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=DELETING,
)
async def delete_conversation(
    request: Request, user: SignedIn, conversation_id: uuid.UUID
) -> Response:
    """Delete a conversation for good, with its messages, its runs and their events.

    There is no trash in this version
    (``docs/working-notes/poc-scope.md``).

    **Not idempotent.** A conversation that is not there -- never was, already
    deleted, or somebody else's -- is 404, like everything else that is not
    there, because the application finds the conversation and checks who is
    asking before it deletes anything. So deleting twice is 204 and then 404,
    and a client that means "make sure this is gone" reads both as success.

    **A conversation with a run going is not deleted**: it is refused with 409
    (``RunAlreadyActiveError``), and its author cancels the run first. The
    store decides that in the transaction that would have deleted, so there is
    no window for a run to begin in (``docs/specs/conversations.md``).
    """
    await conversing(request).delete(user, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@conversation_router.post(
    "/conversations/{conversation_id}/runs/{run_id}/cancel",
    tags=["runs"],
    responses=WRITING,
)
async def cancel_run(
    request: Request, user: SignedIn, conversation_id: uuid.UUID, run_id: uuid.UUID
) -> RunView:
    """Stop the run answering in that conversation; the run as it then is.

    **Asking is not the same as having happened.** A run this process is
    executing is cancelled by cancelling its work, and the end is written a
    moment later under a shield, so what comes back may still say ``running``;
    a client learns that it ended from the run's own stream, or by opening the
    conversation again. A run no process here is executing -- after a restart
    -- is ended in the store instead, and comes back ``cancelled``. A run that
    has already ended is not an error (``docs/specs/runs.md``).

    The URL names both the conversation and the run, and the application
    checks that the run is that conversation's: a run of another conversation
    answers exactly like a run that does not exist, as one of somebody else's
    does.
    """
    return RunView.of(await turning(request).cancel(user, run_id, conversation_id=conversation_id))
