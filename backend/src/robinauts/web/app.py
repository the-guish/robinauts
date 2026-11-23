# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Robinauts' component: the routes of ``docs/architecture/web.md`` over a controller, which
oneberth composes and serves under ``/robinauts/``.

oneberth holds who is asking and the check of each route's permission (``web/roles.py``), the
sign-in, the credentials, the checks on a request, the security headers and the interface. A
route here reads the principal oneberth found, and the controller checks whose conversation is
asked for. A person is the oneberth user, and Robinauts keeps its own row of them under the
same id (``People``). A flow's handler holds a service token of ``robinauts/handler`` and acts
for the person who started its run, whom ``RUN_HEADER`` names.

The ``/internal/worker/`` routes answer the workers, each by a token of ``robinauts/worker``
bound to its name (``web/worker_api.py``); oneberth serves them on the worker listener too.
A controller operation that is not implemented answers 501. The shapes are the ones the
interface reads (``docs/specs/wire.md``); a turn's stream is AG-UI over SSE, its run id is the
turn's id, and its thread id the session's. The ``/api/apps`` routes reach the agents' tool
servers for the side panel's MCP apps (``web/apps.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from oneberth.identity import Principal
from oneberth.identity import User as OneberthUser
from oneberth.web import Asking, Component, Health, Lifespan, Refusal, bearer_of, permit
from pydantic import BaseModel, Field

from robinauts.controller.contract.broker.ports import TurnBroker
from robinauts.controller.contract.domain import (
    Identity,
    InvalidValueError,
    MessageNotFoundError,
    NoActiveTurnError,
    NumberedEvent,
    OpenedSession,
    TurnActiveError,
    UnknownAgentError,
    UnknownModelError,
    User,
)
from robinauts.controller.contract.ports import Controller
from robinauts.orchestrator.contract.domain import (
    HANDLER_QUEUE,
    HANDLER_QUEUE_PERMISSION,
    HANDLER_TOKEN_PATH,
    FlowForm,
    Flows,
    InvalidInputsError,
    NoSuchFlowError,
)
from robinauts.shared_domain.domain import (
    Config,
    ControllerError,
    Message,
    Project,
    ProjectNotFoundError,
    ReasoningPart,
    Role,
    Session,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Turn,
)
from robinauts.web import agui, roles
from robinauts.web.apps import (
    VIEW_HEADERS,
    Apps,
    NoSuchAppError,
    SecretLookup,
    ToolResult,
    UnansweredCallError,
)
from robinauts.web.changes import FeedGate
from robinauts.web.changes import changes as changes_of
from robinauts.web.worker_api import WorkerAccess, worker_router

NAME = "robinauts"
"""The component's name, and the first segment of its addresses: ``/robinauts/…``."""

DEFAULT_PAGE = 30

CHANGES_TIMEOUT = 25.0
"""The seconds ``GET /api/changes`` waits when the client names none: under the minute after
which a proxy commonly closes a quiet request."""
MAX_CHANGES_TIMEOUT = 30.0
"""The longest ``GET /api/changes`` waits."""

RUN_HEADER = "X-Robinauts-Run"
"""The header a flow's handler names its run in: the run's id at neorc. The handler then acts
for the person who started that run."""

NOT_A_PERSON = (
    f"a service acts for a person only as a flow's handler, which names its run in {RUN_HEADER}"
)
UNKNOWN_RUN = "neorc holds no run of that id that a person started"
NOT_THE_HANDLERS_QUEUE = (
    f"the handlers that call Robinauts run on the queue {HANDLER_QUEUE!r} alone"
)

STATUS_OF: dict[type[Exception], int] = {
    ControllerError: 500,
    InvalidValueError: 422,
    SessionNotFoundError: 404,
    ProjectNotFoundError: 404,
    MessageNotFoundError: 404,
    UnknownAgentError: 404,
    UnknownModelError: 422,
    TurnActiveError: 409,
    NoActiveTurnError: 404,
    NotImplementedError: 501,
    NoSuchAppError: 404,
    UnansweredCallError: 502,
    NoSuchFlowError: 404,
    InvalidInputsError: 422,
}
"""The status of each error a route raises; a subclass takes its nearest base's."""

ERROR_NAMES: dict[type[Exception], str] = {
    NotImplementedError: "NotImplemented",
    NoSuchAppError: "NotFound",
    UnansweredCallError: "BadGateway",
    NoSuchFlowError: "NotFound",
    InvalidInputsError: "InvalidInputs",
}
"""The ``error`` of the answer, where it is not the class's own name."""


def error_body(error: Exception) -> Mapping[str, Any]:
    name = next((n for cls, n in ERROR_NAMES.items() if isinstance(error, cls)), None)
    return {"error": name or type(error).__name__, "detail": str(error)}


# --- what the interface reads ----------------------------------------------------


class HandlerToken(BaseModel):
    token: str


class AgentSummary(BaseModel):
    id: str
    title: str
    engine: str
    model: str
    """Its default model."""
    models: list[str]
    """The models its turns may run on, in order, its default first."""


class AgentListResponse(BaseModel):
    items: list[AgentSummary]


class ModelSummary(BaseModel):
    id: str
    title: str


class ModelListResponse(BaseModel):
    items: list[ModelSummary]


class FlowOptionSummary(BaseModel):
    value: str
    label: str
    models: list[str] | None = None
    """An agent's: the models its turns may run on, in order, its default first."""


class FlowInputSummary(BaseModel):
    name: str
    label: str
    type: str
    """The type the flow declares: ``string``, ``number``, ``boolean`` or ``datetime``."""
    lov: str | None = None
    """The list of values it takes its value from: ``repo_branch``, ``model`` or ``agent``."""
    options: list[FlowOptionSummary] | None = None
    """The values it may take, when it has a list of values."""


class FlowSummary(BaseModel):
    name: str
    title: str
    has_form: bool
    """Whether the flows folder holds a form for it; without one, every input is a plain
    field."""
    inputs: list[FlowInputSummary]


class FlowListResponse(BaseModel):
    items: list[FlowSummary]


class StartFlowRunRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


class FlowRunStarted(BaseModel):
    id: str
    """The run's id at neorc, which neorc's API at ``/neorc/`` shows it under."""


def flow_summary(form: FlowForm) -> FlowSummary:
    return FlowSummary(
        name=form.name,
        title=form.title,
        has_form=form.has_form,
        inputs=[
            FlowInputSummary(
                name=field.name,
                label=field.label,
                type=field.type,
                lov=None if field.lov is None else str(field.lov),
                options=(
                    None
                    if field.options is None
                    else [
                        FlowOptionSummary(
                            value=o.value,
                            label=o.label,
                            models=None if o.models is None else list(o.models),
                        )
                        for o in field.options
                    ]
                ),
            )
            for field in form.inputs
        ],
    )


class AppSummary(BaseModel):
    id: str
    """``<server>/<tool>``."""
    server: str
    tool: str
    title: str
    description: str


class AppListResponse(BaseModel):
    apps: list[AppSummary]


class AppToolCallRequest(BaseModel):
    agent: str
    arguments: dict[str, Any] = {}


class AppToolCallResponse(BaseModel):
    """The MCP ``CallToolResult``, as the tool server gave it, under MCP's own names."""

    content: list[Any]
    structuredContent: dict[str, Any] | None
    isError: bool


def tool_call_response(result: ToolResult) -> AppToolCallResponse:
    return AppToolCallResponse(
        content=result.content,
        structuredContent=result.structured_content,
        isError=result.is_error,
    )


def stored_result_response(part: ToolResultPart) -> AppToolCallResponse:
    """A tool result of the transcript as an MCP ``CallToolResult``: its text as one text
    block, and its structured content."""
    return AppToolCallResponse(
        content=[{"type": "text", "text": part.text}],
        structuredContent=None if part.structured is None else dict(part.structured),
        isError=part.is_error,
    )


class ProjectSummary(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class ProjectListResponse(BaseModel):
    items: list[ProjectSummary]
    """Oldest first: the ``Default`` one is always among them."""


class NewProjectRequest(BaseModel):
    name: str


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str
    agent: str
    model: str
    project_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ListedConversation(ConversationSummary):
    running: bool
    """The conversation has an active turn."""


class ConversationListResponse(BaseModel):
    items: list[ListedConversation]
    next_cursor: str | None


class ChangesResponse(BaseModel):
    version: int
    """The signed-in user's change version (``docs/specs/wire.md``, "The change feed")."""


class TextContent(BaseModel):
    kind: Literal["text"]
    text: str


class ToolCallContent(BaseModel):
    kind: Literal["tool_call"]
    call_id: str
    name: str
    arguments: dict[str, object]


class ToolResultContent(BaseModel):
    kind: Literal["tool_result"]
    call_id: str
    text: str
    is_error: bool


class ProvenanceView(BaseModel):
    agent: str
    engine: str
    model: str
    run_id: uuid.UUID


class MessageView(BaseModel):
    id: uuid.UUID
    role: Literal["assistant", "user", "tool"]
    channel: Literal["web"]
    created_at: datetime
    parts: list[TextContent | ToolCallContent | ToolResultContent]
    provenance: ProvenanceView | None
    failed: bool = False
    """An answer whose turn failed: what it streamed before the failure."""
    task_payload: dict[str, Any] | None = None
    """A question's structured data beside its text, as it was asked with it."""


class ResumeView(BaseModel):
    after: int
    follows: uuid.UUID | None


class EndedBadlyView(BaseModel):
    run_id: uuid.UUID
    state: Literal["cancelled", "failed", "interrupted"]
    ended_at: datetime


class OpenedConversationResponse(BaseModel):
    conversation: ConversationSummary
    messages: list[MessageView]
    run_id: uuid.UUID | None
    resume: ResumeView | None
    ended_badly: EndedBadlyView | None


class RenameRequest(BaseModel):
    title: str


class SetModelRequest(BaseModel):
    model_id: str


class ForkRequest(BaseModel):
    at_message: uuid.UUID


class NewChatRequest(BaseModel):
    agent_id: str
    model_id: str | None = None
    text: str
    project_id: uuid.UUID | None = None
    """The project the conversation goes in; left out, the user's ``Default`` one."""
    task_payload: dict[str, Any] | None = None
    """Structured data beside the text, for the engine: a JSON object whose ``task_key`` names
    the kind of task (``docs/specs/wire.md``, "Task payloads")."""


class TurnRequest(BaseModel):
    text: str | None = None
    task_payload: dict[str, Any] | None = None
    """With a new question or an edit: as ``NewChatRequest``'s. An edit without one keeps the
    edited question's."""
    parent_id: uuid.UUID | None = None
    edit: uuid.UUID | None = None
    regenerate: uuid.UUID | None = None
    retry: uuid.UUID | None = None
    resume: uuid.UUID | None = None
    model_id: str | None = None


def project_summary(project: Project) -> ProjectSummary:
    return ProjectSummary(id=project.id, name=project.name, created_at=project.created_at)


def summary(session: Session, model: str) -> ConversationSummary:
    return ConversationSummary(
        id=session.id,
        title=session.title,
        agent=session.agent,
        model=model,
        project_id=session.project_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def content(part: TextPart | ToolCallPart | ToolResultPart) -> BaseModel:
    if isinstance(part, ToolCallPart):
        return ToolCallContent(
            kind="tool_call",
            call_id=part.call_id,
            name=part.name,
            arguments=dict(part.arguments),
        )
    if isinstance(part, ToolResultPart):
        return ToolResultContent(
            kind="tool_result",
            call_id=part.call_id,
            text=part.text,
            is_error=part.is_error,
        )
    return TextContent(kind="text", text=part.text)


def message_view(message: Message, session: Session) -> MessageView:
    provenance = None
    if message.role is Role.ASSISTANT:
        provenance = ProvenanceView(
            agent=message.agent or session.agent,
            engine=message.engine or "",
            model=message.model or "",
            run_id=message.turn_id or session.id,
        )
    return MessageView(
        id=message.id,
        role=message.role.value,
        channel="web",
        created_at=message.created_at,
        parts=[content(p) for p in message.parts if not isinstance(p, ReasoningPart)],
        provenance=provenance,
        failed=message.failed,
        task_payload=(None if message.task_payload is None else dict(message.task_payload)),
    )


def model_of(opened: OpenedSession, default: str) -> str:
    """A session's model is its last message's; the agent's default before any."""
    return next((m.model for m in reversed(opened.messages) if m.model), default)


def opened_view(opened: OpenedSession, default: str) -> OpenedConversationResponse:
    session = opened.session
    # The turn stores its one answer when it ends, so a watcher that has the thread
    # attaches at the start of the turn's events.
    active = opened.active
    return OpenedConversationResponse(
        conversation=summary(session, model_of(opened, default)),
        messages=[message_view(m, session) for m in opened.messages],
        run_id=None if active is None else active.turn_id,
        resume=None if active is None else ResumeView(after=0, follows=active.follows),
        ended_badly=ended_badly_view(opened.ended_badly),
    )


def ended_badly_view(turn: Turn | None) -> EndedBadlyView | None:
    """How the last turn ended, when it ended badly. Its error is for the operator only."""
    if turn is None or turn.ended_at is None:
        return None
    return EndedBadlyView(run_id=turn.id, state=turn.state.value, ended_at=turn.ended_at)


def event_stream(
    session_id: uuid.UUID, turn_id: uuid.UUID, events: AsyncIterator[NumberedEvent]
) -> StreamingResponse:
    return StreamingResponse(
        agui.stream(str(session_id), str(turn_id), events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Robinauts-Run-Id": str(turn_id),
            "X-Robinauts-Conversation-Id": str(session_id),
        },
    )


class People:
    """Robinauts' row of each person, under the id oneberth gives them.

    oneberth holds the user; Robinauts keeps what is its own about them: their projects,
    conversations and change version, keyed by that id. The row is written the first time a
    person asks, and again when their name or email changes, not at every request."""

    def __init__(self, controller: Controller) -> None:
        self._controller = controller
        self._known: dict[uuid.UUID, tuple[tuple[object, ...], User]] = {}

    async def of(self, user: OneberthUser) -> User:
        identity = Identity(
            provider=user.provider,
            subject=user.subject,
            name=user.name,
            email=user.email,
            hosted_domain=user.hosted_domain,
            groups=tuple(sorted(user.groups)),
        )
        key = (identity.name, identity.email, identity.hosted_domain, identity.groups)
        known = self._known.get(user.id)
        if known is not None and known[0] == key:
            return known[1]
        stored = await self._controller.ensure_user(identity, id=user.id)
        self._known[user.id] = (key, stored)
        return stored


def credential_of(request: Request) -> str | None:
    """The secret the request came with: its bearer, or its session cookie. What a request of
    this backend's own sends on as the person's bearer (``docs/specs/identity.md``, "Between
    two backends")."""
    bearer = bearer_of(request.headers.get("authorization"))
    if bearer is not None:
        return bearer
    return request.cookies.get(request.app.state.oneberth.cookies.session)


def new_robinauts_webapp(
    controller: Controller,
    brokers: Mapping[str, TurnBroker],
    *,
    worker_access: WorkerAccess,
    config: Config,
    secret_for: SecretLookup,
    lifespan: Lifespan | None = None,
    serving: Lifespan | None = None,
    health: Health | None = None,
    flows: Flows | None = None,
    starter_of: Callable[[str], Awaitable[uuid.UUID | None]] | None = None,
    feed: FeedGate | None = None,
) -> Component:
    """Robinauts' component, for ``oneberth.web.compose_app``.

    ``brokers``, one per worker by name, and ``worker_access`` serve the workers at
    ``/internal/worker/``. ``config`` names the agents' tool servers, whose secrets
    ``secret_for`` reads. ``lifespan`` is what starts and stops with the backend: the
    controller and the workers (``cli/lifecycle.py``). ``serving`` is what runs while the
    backend serves: the parts that call its own listeners. ``health`` says whether the workers
    are heard from. ``flows`` are the orchestrator's flows as forms, which ``/api/flows`` lists
    and starts: none without a flows folder. ``starter_of`` reads who started a run at neorc:
    the person a flow's handler acts for. ``feed`` ends the waits of ``GET /api/changes`` once
    whoever stops the server closes it."""
    gate = FeedGate() if feed is None else feed
    apps = Apps(config, secret_for)
    people = People(controller)
    router = APIRouter()

    async def person(request: Request, principal: Asking) -> User:
        """The person a request is for: the user who asks, or, for a flow's handler, the
        person who started the run it names."""
        if principal.user is not None:
            return await people.of(principal.user)
        return await people.of(await run_owner(request, principal))

    async def run_owner(request: Request, principal: Principal) -> OneberthUser:
        access = request.app.state.oneberth.access
        run = request.headers.get(RUN_HEADER)
        if run is None or roles.HANDLER_ROLE not in principal.roles:
            raise Refusal(403, "Forbidden", NOT_A_PERSON)
        owner = None if starter_of is None else await starter_of(run)
        found = None if owner is None else await access.store.user(owner)
        if found is None:
            raise Refusal(403, "Forbidden", UNKNOWN_RUN)
        return found

    asking = Depends(person)

    async def default_model(agent: str) -> str:
        return next(a.default_model for a in await controller.list_agents() if a.id == agent)

    async def stream_turn(
        user: User, session_id: uuid.UUID, turn_id: uuid.UUID, after: int
    ) -> StreamingResponse:
        events = controller.watch_turn(user, session_id, turn_id, after=after)
        # The refusals happen inside the generator: ask for the first event here, so that
        # they answer with a status rather than a broken stream.
        first = await anext(events, None)

        async def chain_events() -> AsyncIterator[NumberedEvent]:
            if first is None:
                return
            yield first
            async for event in events:
                yield event

        return event_stream(session_id, turn_id, chain_events())

    # --- catalogue -------------------------------------------------------------

    @router.get("/api/agents", dependencies=[permit(roles.AGENTS_READ)])
    async def list_agents() -> AgentListResponse:
        agents = await controller.list_agents()
        return AgentListResponse(
            items=[
                AgentSummary(
                    id=a.id,
                    title=a.title,
                    engine="",
                    model=a.default_model,
                    models=list(a.models),
                )
                for a in agents
            ]
        )

    @router.get("/api/models", dependencies=[permit(roles.AGENTS_READ)])
    async def list_models() -> ModelListResponse:
        models = await controller.list_models()
        return ModelListResponse(items=[ModelSummary(id=m.id, title=m.title) for m in models])

    # --- flows, as forms: the orchestrator's (orchestrator/) ---------------------

    @router.get("/api/flows", dependencies=[permit(roles.FLOWS_READ)])
    async def list_flows() -> FlowListResponse:
        forms = () if flows is None else await flows.forms()
        return FlowListResponse(items=[flow_summary(form) for form in forms])

    @router.post(
        "/api/flows/{flow}/runs",
        status_code=201,
        dependencies=[permit(roles.FLOWS_START)],
    )
    async def start_flow_run(
        request: Request, flow: str, body: StartFlowRunRequest, user: User = asking
    ) -> FlowRunStarted:
        if flows is None:
            raise NoSuchFlowError(f"no flow {flow!r}: this server runs no orchestrator")
        # Sent on with the person's own credential: the run is then theirs at neorc.
        credential = credential_of(request)
        started = await flows.start(flow, body.inputs, credential=credential)
        return FlowRunStarted(id=started)

    # --- MCP apps, for the side panel: web/apps.py -------------------------------

    @router.get("/api/apps", dependencies=[permit(roles.APPS_USE)])
    async def list_apps(agent: str) -> AppListResponse:
        found = await apps.of(agent)
        return AppListResponse(
            apps=[
                AppSummary(
                    id=a.id,
                    server=a.server,
                    tool=a.tool,
                    title=a.title,
                    description=a.description,
                )
                for a in found
            ]
        )

    # Framed by the panel, not called: outside the OpenAPI document.
    @router.get(
        "/api/apps/{server}/{tool}/view",
        dependencies=[permit(roles.APPS_USE)],
        include_in_schema=False,
    )
    async def open_app_view(server: str, tool: str, agent: str) -> HTMLResponse:
        html = await apps.view(agent, server, tool)
        return HTMLResponse(html, headers=dict(VIEW_HEADERS))

    @router.post("/api/apps/{server}/tools/{tool}", dependencies=[permit(roles.APPS_USE)])
    async def call_app_tool(
        server: str, tool: str, body: AppToolCallRequest
    ) -> AppToolCallResponse:
        return tool_call_response(await apps.call(body.agent, server, tool, body.arguments))

    # --- projects --------------------------------------------------------------

    @router.get("/api/projects", dependencies=[permit(roles.PROJECTS_READ)])
    async def list_projects(user: User = asking) -> ProjectListResponse:
        projects = await controller.list_projects(user)
        return ProjectListResponse(items=[project_summary(p) for p in projects])

    @router.post("/api/projects", status_code=201, dependencies=[permit(roles.PROJECTS_WRITE)])
    async def create_project(body: NewProjectRequest, user: User = asking) -> ProjectSummary:
        """The user's project of that name: a new one, or the one they already had."""
        return project_summary(await controller.create_project(user, body.name))

    # --- sessions --------------------------------------------------------------

    @router.get("/api/conversations", dependencies=[permit(roles.CONVERSATIONS_READ)])
    async def list_sessions(
        user: User = asking,
        limit: int = DEFAULT_PAGE,
        cursor: str | None = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
    ) -> ConversationListResponse:
        page = await controller.list_sessions(user, limit=limit, cursor=cursor, query=q)
        defaults = {a.id: a.default_model for a in await controller.list_agents()}
        return ConversationListResponse(
            items=[
                ListedConversation(
                    **summary(c, defaults[c.agent]).model_dump(),
                    running=c.id in page.running,
                )
                for c in page.sessions
            ],
            next_cursor=page.cursor,
        )

    @router.get("/api/changes", dependencies=[permit(roles.CONVERSATIONS_READ)])
    async def changes(
        request: Request,
        user: User = asking,
        after: Annotated[int | None, Query(ge=0)] = None,
        timeout: Annotated[float, Query(ge=0, le=MAX_CHANGES_TIMEOUT)] = CHANGES_TIMEOUT,
    ) -> ChangesResponse:
        """The user's change version: at once without ``after``, else once it differs from
        ``after`` or ``timeout`` seconds have passed. It signals and carries nothing else.
        """
        version = await changes_of(controller, user, after, timeout, request, gate)
        return ChangesResponse(version=version)

    @router.get(
        "/api/conversations/{conversation_id}",
        dependencies=[permit(roles.CONVERSATIONS_READ)],
    )
    async def open_session(
        conversation_id: uuid.UUID, user: User = asking
    ) -> OpenedConversationResponse:
        opened = await controller.open_session(user, conversation_id)
        return opened_view(opened, await default_model(opened.session.agent))

    @router.get(
        "/api/conversations/{conversation_id}/messages/{message_id}/tool-result",
        dependencies=[permit(roles.CONVERSATIONS_READ)],
    )
    async def read_tool_result(
        conversation_id: uuid.UUID, message_id: uuid.UUID, call_id: str, user: User = asking
    ) -> AppToolCallResponse:
        """One tool call's result, whole, for the view of that call: the conversation's own
        read carries its text alone."""
        part = await controller.tool_result(
            user, conversation_id, message_id=message_id, call_id=call_id
        )
        return stored_result_response(part)

    @router.patch(
        "/api/conversations/{conversation_id}",
        dependencies=[permit(roles.CONVERSATIONS_WRITE)],
    )
    async def rename_session(
        conversation_id: uuid.UUID, body: RenameRequest, user: User = asking
    ) -> ConversationSummary:
        renamed = await controller.rename_session(user, conversation_id, body.title)
        return summary(renamed, await default_model(renamed.agent))

    @router.post(
        "/api/conversations/{conversation_id}/archive",
        status_code=204,
        dependencies=[permit(roles.CONVERSATIONS_WRITE)],
    )
    async def archive_session(conversation_id: uuid.UUID, user: User = asking) -> None:
        await controller.archive_session(user, conversation_id)

    @router.delete(
        "/api/conversations/{conversation_id}",
        status_code=204,
        dependencies=[permit(roles.CONVERSATIONS_WRITE)],
    )
    async def delete_session(conversation_id: uuid.UUID, user: User = asking) -> None:
        await controller.delete_session(user, conversation_id)

    @router.put(
        "/api/conversations/{conversation_id}/model",
        dependencies=[permit(roles.CONVERSATIONS_WRITE)],
    )
    async def set_model(
        conversation_id: uuid.UUID, body: SetModelRequest, user: User = asking
    ) -> ConversationSummary:
        # The model goes with each turn now: this stores nothing, and answers the
        # conversation as the picker will send its next turn.
        opened = await controller.open_session(user, conversation_id)
        agent = opened.session.agent
        listed = next((a.models for a in await controller.list_agents() if a.id == agent), ())
        if body.model_id not in listed:
            raise UnknownModelError(f"{body.model_id} is not one of agent {agent}'s models")
        moved = summary(opened.session, body.model_id)
        moved.updated_at = datetime.now(UTC)
        return moved

    @router.post(
        "/api/conversations/{conversation_id}/fork",
        status_code=201,
        dependencies=[permit(roles.CONVERSATIONS_WRITE)],
    )
    async def fork_session(
        conversation_id: uuid.UUID, body: ForkRequest, user: User = asking
    ) -> ConversationSummary:
        forked = await controller.fork_session(user, conversation_id, at_message=body.at_message)
        return summary(forked, await default_model(forked.agent))

    # --- turns: AG-UI over SSE, outside the OpenAPI document -------------------

    @router.post("/api/turns", include_in_schema=False, dependencies=[permit(roles.TURNS_START)])
    async def start_session(body: NewChatRequest, user: User = asking) -> StreamingResponse:
        model = body.model_id or await default_model(body.agent_id)
        started = await controller.start_session(
            user,
            agent=body.agent_id,
            model=model,
            text=body.text,
            project=body.project_id,
            task_payload=body.task_payload,
        )
        return await stream_turn(user, started.session_id, started.turn_id, 0)

    @router.post(
        "/api/conversations/{conversation_id}/turns",
        include_in_schema=False,
        dependencies=[permit(roles.TURNS_START)],
    )
    async def send_message(
        conversation_id: uuid.UUID, body: TurnRequest, user: User = asking
    ) -> StreamingResponse:
        opened = await controller.open_session(user, conversation_id)
        model = body.model_id or model_of(opened, await default_model(opened.session.agent))
        again = (body.regenerate, body.retry, body.resume) != (None, None, None)
        if body.task_payload is not None and again:
            # A regeneration, a retry or a resume answers the question as it was asked.
            raise InvalidValueError("a task_payload goes with a new question or an edit")
        if body.regenerate is not None:
            # The frontend names the answer to produce again; the controller, its question.
            at = next(
                (i for i, m in enumerate(opened.messages) if m.id == body.regenerate),
                None,
            )
            if at is None:
                raise MessageNotFoundError(str(body.regenerate))
            question = next(m for m in reversed(opened.messages[: at + 1]) if m.role is Role.USER)
            started = await controller.regenerate_answer(
                user, conversation_id, question_id=question.id, model=model
            )
        elif body.retry is not None:
            started = await controller.retry_answer(
                user, conversation_id, answer_id=body.retry, model=model
            )
        elif body.resume is not None:
            started = await controller.resume_answer(
                user, conversation_id, answer_id=body.resume, model=model
            )
        elif body.edit is not None:
            started = await controller.edit_message(
                user,
                conversation_id,
                message_id=body.edit,
                model=model,
                text=body.text,
                task_payload=body.task_payload,
            )
        elif body.parent_id is not None:
            started = await controller.send_message(
                user,
                conversation_id,
                parent_id=body.parent_id,
                model=model,
                text=body.text,
                task_payload=body.task_payload,
            )
        else:
            raise InvalidValueError("a message names the parent_id it answers, or the one it edits")
        return await stream_turn(user, conversation_id, started.turn_id, 0)

    @router.get(
        "/api/conversations/{conversation_id}/runs/{run_id}/events",
        include_in_schema=False,
        dependencies=[permit(roles.CONVERSATIONS_READ)],
    )
    async def watch_turn(
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        user: User = asking,
        after: int | None = None,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        position = after if after is not None else int(last_event_id or 0)
        return await stream_turn(user, conversation_id, run_id, position)

    @router.post(
        "/api/conversations/{conversation_id}/runs/{run_id}/cancel",
        status_code=204,
        dependencies=[permit(roles.TURNS_START)],
    )
    async def cancel_turn(
        conversation_id: uuid.UUID, run_id: uuid.UUID, user: User = asking
    ) -> None:
        await controller.cancel_turn(user, conversation_id, run_id)

    # --- the workers -------------------------------------------------------------

    router.include_router(worker_router(brokers, worker_access))

    # Outside the OpenAPI document: neorc's worker calls it, not a client of the API.
    @router.post(
        HANDLER_TOKEN_PATH,
        status_code=201,
        dependencies=[permit(HANDLER_QUEUE_PERMISSION, "queue")],
        include_in_schema=False,
    )
    async def handler_token(request: Request, queue: str) -> HandlerToken:
        """A token of ``robinauts/handler``, for the worker that takes the tasks of the
        handlers' queue: what its handlers call Robinauts with. It lives in this backend's
        memory, and ends when the backend stops."""
        if queue != HANDLER_QUEUE:
            raise Refusal(404, "Not Found", NOT_THE_HANDLERS_QUEUE)
        access = request.app.state.oneberth.access
        secret, _ = await access.create_own_token(f"robinauts-handlers-{queue}", roles.HANDLER_ROLE)
        return HandlerToken(token=secret)

    async def well() -> bool:
        return True if health is None else bool(await health())

    return Component(
        name=NAME,
        roles=roles.ROBINAUTS_ROLES,
        router=router,
        lifespan=lifespan,
        serving=serving,
        health=well,
        errors=STATUS_OF,
        error_body=error_body,
        title="Robinauts",
        version="0",
    )
