# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The shapes the JSON API sends, and therefore what OpenAPI describes.

They are the api's own, not the domain's: a ``User`` carries rows a browser
has no business with, and a response that was a domain object would make every
column added later a change to the wire. These say exactly what goes out, and
``backend/openapi.json`` is the committed snapshot of what they add up to
(``docs/specs/backend.md``).

**Every id crosses as text and every time as ISO-8601 in UTC.** A uuid is a
string in JSON, and ``utc`` is what makes one instant have one spelling
whatever time zone a store's session was in.

**A field that is always sent is required, and nullable where it can be
empty**: ``run_id``, ``next_cursor``, ``resume`` and the rest are declared
``X | None`` with **no default**, so a generated client types them ``T | null``
and not "may be absent". There is one shape for every answer, and a client
that has read one field has read them all. (``SessionResponse`` and
``UserSummary`` predate this and are left as they are.)

**A request body forbids what it does not know** (``extra="forbid"``): a field
spelt wrong, or one from a newer client, is a refusal naming it rather than a
write that quietly did something else. What goes out is not held to that -- a
client reads the fields it knows and reads past the rest, which is how a field
is added without a new version of the wire.

**Two of these are not in the document**: the bodies of the streaming
endpoints (``NewChatRequest``, ``TurnRequest``), whose routes are outside
OpenAPI and are described in ``docs/specs/wire.md`` instead
(``docs/specs/backend.md``). They are written here with the others all the
same, because what a request body may be is one subject and because the rules
this module states -- the bounds that are the record's own, and a field nobody
knows being a refusal -- are exactly as true of them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from robinauts.legacy.application import OpenedConversation
from robinauts.legacy.domain import (
    MAX_CONFIG_ID_CHARS,
    MAX_MESSAGE_CHARS,
    MAX_TITLE_CHARS,
    AgentDefinition,
    Channel,
    Conversation,
    Engine,
    InvalidValueError,
    Message,
    MessagePart,
    ModelConfig,
    Provenance,
    ReasoningPart,
    Run,
    RunState,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    User,
)

SentKind = Literal["text", "tool_call", "tool_result"]
"""The kinds of content this build **sends**, which are three.

Not ``PartKind``, which names every kind the stored format has a
discriminator for: a document that offered ``reasoning`` here would describe
the one value ``MessageView.of`` guarantees is absent, and every client
generated from it would have a branch for content it can never be sent. The
day a kind is sent, this grows a value and the committed snapshot shows it --
which is what the snapshot is for.

``test_the_wire_declares_exactly_the_values_it_sends`` holds it to
``domain.SUPPORTED_PART_KINDS``.
"""

SentRole = Literal["assistant", "user", "tool"]
"""The roles a message can be sent with: ``domain.SUPPORTED_ROLES``.

A ``tool`` message is the results of one batch of the calls its parent made
(``docs/specs/conversations.md``): it is served in the thread like any other
message, holding ``tool_result`` parts and nothing else.
"""

SentBadEnd = Literal["cancelled", "failed", "interrupted"]
"""How a run can have ended badly: ``domain.FAULTED_RUN_STATES``.

A run that finished is not an ``ended_badly`` and neither is one still going,
so the three are the whole of what that field can say.
"""


class ProviderSummary(BaseModel):
    """One provider to offer a sign-in button for. No secret, no endpoint."""

    id: str
    title: str


class UserSummary(BaseModel):
    """Who is signed in, as the interface shows them in the profile block."""

    id: uuid.UUID
    name: str | None = None
    email: str | None = None
    """Their address, only where the provider verified it."""
    provider: str

    @classmethod
    def of(cls, user: User) -> UserSummary:
        """The summary of a user; the fields the browser is told about."""
        return cls(id=user.id, name=user.name, email=user.email, provider=user.provider)


class SessionResponse(BaseModel):
    """What ``GET /auth/session`` answers, signed in or not.

    ``sign_in`` is whether this deployment has a sign-in configuration at all;
    without one there are no providers and nobody to be.

    ``local_development`` is the exception to that last part: the local
    development mode has no sign-in and no providers, and yet somebody *is*
    signed in -- the one local user everything runs as. It is what the
    interface shows its permanent banner for (``docs/specs/frontend.md``), and
    it is why the two are separate fields rather than one: "nothing is
    configured" and "sign-in is deliberately off" are different things to say
    to a person, and only the second one has a user with it.
    """

    sign_in: bool
    local_development: bool = False
    public_url: str | None = None
    providers: list[ProviderSummary] = []
    user: UserSummary | None = None


class HealthResponse(BaseModel):
    """What ``GET /health`` answers. It holds no data and says nothing else."""

    status: str


class ErrorResponse(BaseModel):
    """Every refusal, in one shape (``robinauts.legacy.api.errors``)."""

    error: str
    """The name of the error class, which is what a client branches on."""
    detail: str


def utc(when: datetime) -> datetime:
    """``when`` in UTC, which is the one spelling a time crosses the wire in.

    Every time the platform writes is UTC (``docs/specs/conversations.md``),
    but a record read back from a store can carry the offset of that session's
    time zone, and two spellings of one instant is one too many for a client
    that compares strings. So the shift happens here, where everything that
    goes out passes.
    """
    return when.astimezone(UTC)


class TextContent(BaseModel):
    """A piece of text: what a person wrote, or what a model answered."""

    kind: Literal["text"]
    text: str


class ToolCallContent(BaseModel):
    """A tool the model asked for, in the answer that asked (``ToolCallPart``).

    **Rendered as data** by a client (``docs/specs/wire.md``): the name and the
    arguments are the model's, and the arguments are whatever it wrote.
    """

    kind: Literal["tool_call"]
    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolResultContent(BaseModel):
    """What a tool answered for one call, in the tool message that holds the
    batch (``ToolResultPart``). Text, and whether the call went wrong."""

    kind: Literal["tool_result"]
    call_id: str
    text: str
    is_error: bool


ContentPart = Annotated[
    TextContent | ToolCallContent | ToolResultContent, Field(discriminator="kind")
]
"""One piece of a message's content, named by its kind.

``kind`` is the discriminator the stored format uses
(``docs/specs/conversations.md``), so a client reads the kinds it knows and
can be given another without the shape moving. The kinds this build sends are
``SentKind``'s; reasoning is never among them (``MessageView.of``).
"""


def content_of(part: MessagePart) -> TextContent | ToolCallContent | ToolResultContent:
    """That part as it is sent.

    ``kind`` is written out rather than defaulted, so that every field of a
    part is required in the document a client is generated from
    (``test_openapi_snapshot``): a client then reads a part without asking
    whether each field is there.
    """
    if isinstance(part, ToolCallPart):
        return ToolCallContent(
            kind="tool_call", call_id=part.call_id, name=part.name, arguments=dict(part.arguments)
        )
    if isinstance(part, ToolResultPart):
        return ToolResultContent(
            kind="tool_result", call_id=part.call_id, text=part.text, is_error=part.is_error
        )
    if isinstance(part, TextPart):
        return TextContent(kind="text", text=part.text)
    raise InvalidValueError(f"reasoning is not sent, and {type(part).__name__} is not a part")


class ProvenanceView(BaseModel):
    """What an answer records: the agent, the engine, the model and the run.

    On an assistant message and on no other (``domain.Message``). The model is
    the platform's own id for it -- what the configuration calls it -- and no
    vendor, endpoint or key is anywhere near the wire.
    """

    agent: str
    engine: Engine
    model: str
    run_id: uuid.UUID

    @classmethod
    def of(cls, provenance: Provenance) -> ProvenanceView:
        return cls(
            agent=provenance.agent,
            engine=provenance.engine,
            model=provenance.model,
            run_id=provenance.run_id,
        )


class MessageView(BaseModel):
    """One message of the thread a conversation shows.

    No ``parent_id``: what is sent is one path, in order, and where each
    message hangs is the message before it. **Reasoning is not here**: see
    ``of``.
    """

    id: uuid.UUID
    role: SentRole
    channel: Channel
    created_at: datetime
    parts: list[ContentPart]
    provenance: ProvenanceView | None

    @classmethod
    def of(cls, message: Message) -> MessageView:
        """The message as it is sent. **Reasoning is left out.**

        This build keeps no reasoning in a message at all: an engine may
        stream it and a watcher sees it arrive, and ``domain.kept_parts``
        drops it between the engine and the store, so there is nothing here to
        leave out today. The filter is what makes that true of the **wire**
        rather than of one write path: a row written by a build that keeps
        reasoning -- the day one does, or an operator rolling back past it --
        would otherwise have it read as part of the answer by every client
        generated from this document. When reasoning is sent, it is sent
        deliberately and as its own kind (``docs/specs/conversations.md``).
        """
        return cls(
            id=message.id,
            role=message.role.value,
            channel=message.channel,
            created_at=utc(message.created_at),
            parts=[
                content_of(part) for part in message.parts if not isinstance(part, ReasoningPart)
            ],
            provenance=(
                None if message.provenance is None else ProvenanceView.of(message.provenance)
            ),
        )


class ConversationSummary(BaseModel):
    """A conversation as the panel lists it and as a write answers with it.

    No owner: it is the person asking, on every route there is
    (``docs/specs/privacy.md``).

    ``model`` is the id of the model its next turn runs on -- the one its
    author picked, or its agent's default when it began -- and may be one the
    deployment no longer offers, which is what a picker shows so that it can
    be changed (``docs/specs/agents.md``). What produced an answer already
    given is on the answer (``ProvenanceView``).
    """

    id: uuid.UUID
    title: str
    agent: str
    model: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, conversation: Conversation) -> ConversationSummary:
        return cls(
            id=conversation.id,
            title=conversation.title,
            agent=conversation.agent,
            model=conversation.model,
            created_at=utc(conversation.created_at),
            updated_at=utc(conversation.updated_at),
        )


class ConversationListResponse(BaseModel):
    """One page of the caller's conversations, and how to ask for the next.

    ``next_cursor`` is ``null`` on the last page. It is opaque: a position
    inside this person's own listing, written by the store and read by it, and
    nothing a client should take apart (``docs/specs/conversations.md``).
    """

    items: list[ConversationSummary]
    next_cursor: str | None


class ResumeView(BaseModel):
    """Where to attach to the run in flight, and what its next message follows.

    ``after`` is the position to carry on from and ``follows`` is the id the
    next message announced after it will hang under -- the two things a
    watcher of that stream is checked with, handed over together so that
    neither is guessed (``docs/specs/runs.md``).
    """

    after: int
    follows: uuid.UUID | None


class EndedBadlyView(BaseModel):
    """The last run of the conversation, when it failed, was cancelled or was interrupted.

    So that a reload after an answer went wrong says so, instead of showing a
    turn that simply stops (``docs/specs/runs.md``).

    ``state`` is the whole of the reason, and it is the only one the platform
    records as a **kind**: what else a run holds is ``error``, free text made
    out of whatever a provider or a traceback said, written for an operator.
    It stays on the record and in the log, like the detail of every other
    refusal (``robinauts.legacy.api.errors``).
    """

    run_id: uuid.UUID
    state: SentBadEnd
    ended_at: datetime

    @classmethod
    def of(cls, run: Run) -> EndedBadlyView:
        # `finished_at` is set in every ended state (`domain.Run`), and this
        # is built from an ended one alone.
        assert run.finished_at is not None
        return cls(run_id=run.id, state=run.state.value, ended_at=utc(run.finished_at))


class OpenedConversationResponse(BaseModel):
    """A conversation opened: one moment of it, with everything drawing it needs.

    ``messages`` is **the one thread the conversation shows**, oldest first:
    the path from its root to its newest message, and nothing an edit or a
    regeneration put aside (``docs/specs/conversations.md``). A client draws
    it as a list; there is no other branch to switch to. With a run in
    flight it ends at ``resume.follows``, the message the run's next one
    hangs under, so what streams in is appended to it -- a regeneration
    that has completed nothing yet has put the old answer aside already.

    ``run_id`` and ``resume`` are there when a run is in flight, and
    ``ended_badly`` instead when the last one ended badly. Never both: what
    matters about a run that is going is the run.
    """

    conversation: ConversationSummary
    messages: list[MessageView]
    run_id: uuid.UUID | None
    resume: ResumeView | None
    ended_badly: EndedBadlyView | None

    @classmethod
    def of(cls, opened: OpenedConversation) -> OpenedConversationResponse:
        return cls(
            conversation=ConversationSummary.of(opened.conversation),
            messages=[MessageView.of(message) for message in opened.messages],
            run_id=opened.run_id,
            resume=(
                None
                if opened.resume is None
                else ResumeView(after=opened.resume.after, follows=opened.resume.follows)
            ),
            ended_badly=(
                None if opened.ended_badly is None else EndedBadlyView.of(opened.ended_badly)
            ),
        )


class RunView(BaseModel):
    """A run as a client sees it: where it got to, and when.

    ``ended_at`` is what the record calls ``finished_at``: a run that failed,
    was cancelled or was interrupted did not finish, and every ended run has
    one. ``started_at`` is ``null`` until a process took the run up, and
    ``ended_at`` until it ended. What went wrong is not here, for the reason
    given on ``EndedBadlyView``.
    """

    id: uuid.UUID
    state: RunState
    started_at: datetime | None
    ended_at: datetime | None

    @classmethod
    def of(cls, run: Run) -> RunView:
        return cls(
            id=run.id,
            state=run.state,
            started_at=None if run.started_at is None else utc(run.started_at),
            ended_at=None if run.finished_at is None else utc(run.finished_at),
        )


class AgentSummary(BaseModel):
    """One agent, as the picker on an empty chat offers it.

    Four fields, and deliberately no more: **no system prompt** -- it is the
    operator's, it is not a message and it never leaves the process
    (``docs/specs/conversations.md``) -- and no vendor, endpoint or key.

    ``model`` is the agent's **default**: the platform's id of the model a new
    conversation with it starts on unless its author picks another
    (``ModelSummary``). It was once left out, as the operator's choice and
    nothing to pick by; now that a person picks the model, the picker has to
    know which one to select when the agent is chosen.
    """

    id: str
    title: str
    engine: Engine
    model: str

    @classmethod
    def of(cls, definition: AgentDefinition) -> AgentSummary:
        return cls(
            id=definition.id,
            title=definition.title,
            engine=definition.engine,
            model=definition.model,
        )


class AgentListResponse(BaseModel):
    """Every agent this deployment is configured with, in configuration order."""

    items: list[AgentSummary]


class ModelSummary(BaseModel):
    """One model, as the picker offers it: the id to ask for, and what to call it.

    Two fields, and deliberately no more: the provider it is reached through
    and the vendor's name for it are the operator's, and so is how long a call
    to it may take (``domain.ModelConfig``). The id is the platform's own,
    the one a conversation and an answer's provenance carry.
    """

    id: str
    title: str

    @classmethod
    def of(cls, model: ModelConfig) -> ModelSummary:
        return cls(id=model.id, title=model.title)


class ModelListResponse(BaseModel):
    """Every model a conversation of this deployment may run on, in configuration order."""

    items: list[ModelSummary]


class RenameRequest(BaseModel):
    """A new title for a conversation. A renamed title is never overwritten.

    The bound is the record's own (``domain.MAX_TITLE_CHARS``), said here so
    that it is **in the document**: a client that knows how long a title may
    be can say so in its own form, instead of finding out by being refused.
    What the length cannot say -- one line, printable, and something other
    than spaces -- is the application's, and is refused there
    (``application.Conversations.rename``).
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)


class SetModelRequest(BaseModel):
    """The model a conversation's next turn is to run on.

    Its id, as ``GET /api/models`` lists it. The bound is a configured id's
    (``domain.MAX_CONFIG_ID_CHARS``), said here so that it is in the document;
    whether the deployment offers that model is the application's to say
    (``application.Turns.set_model``).
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=MAX_CONFIG_ID_CHARS)


class NewChatRequest(BaseModel):
    """A turn that **begins** a conversation: the agent, its model, and the first question.

    ``agent_id`` is required, and the route decides nothing about it: the
    application takes either the agent to begin a conversation with or the
    conversation a turn is in, never both and never neither
    (``application.Turns.begin``), so the request names the agent rather than
    having one chosen for it. The picker is drawn from ``GET /api/agents``, and
    a deployment with one agent sends that one
    (``docs/specs/agents.md``).

    ``model_id`` is optional: left out, or ``null``, the conversation starts on
    the agent's default (``AgentSummary.model``); named, on that model, which
    has to be one ``GET /api/models`` lists -- one that is not is 422
    (``UnknownModelError``), where an agent that is not there is 404.

    Every bound is the record's own -- ``domain.MAX_CONFIG_ID_CHARS`` for an
    agent's or a model's id, ``domain.MAX_MESSAGE_CHARS`` for a message (every
    part of one, full) -- so a client can hold itself to them, and no field is
    a length somebody else chooses. What a length cannot say -- the shape of a
    configured id, and that a message has something in it once what no store
    could hold has been taken out of it -- is decided below, and what it says
    is the rule.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=MAX_CONFIG_ID_CHARS)
    model_id: str | None = Field(default=None, min_length=1, max_length=MAX_CONFIG_ID_CHARS)
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class TurnRequest(BaseModel):
    """A turn in a conversation that exists: a new message, or one produced again.

    **Exactly one of the two forms** (``docs/specs/wire.md``): ``text``, with
    the ``parent_id`` it hangs under -- nothing for a conversation's first
    question, and the parent of the message being replaced for an edit -- or
    ``regenerate``, the assistant message whose turn is to be produced again,
    which appends no message at all because the question is already there.
    Both, or neither, is refused with one fixed sentence
    (``robinauts.legacy.api.stream_routes.ONE_FORM``); the check is the route's rather
    than a discriminated union's, so that no tag of the sender's reaches a
    refusal's ``location`` (``robinauts.legacy.api.errors``).

    No ``agent_id``: a conversation is begun with an agent and stays with it
    (``docs/specs/conversations.md``), and the application refuses an agent and
    a conversation named together. No ``model_id`` either: a turn runs on the
    conversation's model, which is changed by
    ``PUT /api/conversations/{id}/model`` and never by a turn, so one sent here
    is a field this body does not know.
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=MAX_MESSAGE_CHARS)
    parent_id: uuid.UUID | None = None
    regenerate: uuid.UUID | None = None
