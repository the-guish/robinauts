# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The web shell: the routes of ``docs/architecture/web.md`` over a controller, and the UI.

No sign-in yet: every request runs as one local user, named after the operating
system's. A controller operation that is not implemented answers 501. The shapes are the
ones the frontend reads (``docs/specs/wire.md``); a turn's stream is AG-UI over SSE, its
run id is the turn's id, and its thread id the session's.
"""

from __future__ import annotations

import getpass
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from robinauts.controller.contract.domain import (
    ControllerError,
    Identity,
    InvalidValueError,
    Message,
    MessageNotFoundError,
    NoActiveTurnError,
    NumberedEvent,
    OpenedSession,
    ReasoningPart,
    Role,
    Session,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    TurnActiveError,
    UnknownAgentError,
    UnknownModelError,
)
from robinauts.controller.contract.ports import Controller
from robinauts.web import agui

LOCAL_PROVIDER = "local"
DEFAULT_PAGE = 30

STATUS_OF: dict[type[ControllerError], int] = {
    InvalidValueError: 422,
    SessionNotFoundError: 404,
    MessageNotFoundError: 404,
    UnknownAgentError: 404,
    UnknownModelError: 422,
    TurnActiveError: 409,
    NoActiveTurnError: 404,
}


def local_identity() -> Identity:
    """The one user of a local start: the operating system's."""
    subject = getpass.getuser()
    return Identity(provider=LOCAL_PROVIDER, subject=subject, name=subject)


# --- what the frontend reads ---------------------------------------------------


class ErrorResponse(BaseModel):
    error: str
    detail: str


class ProviderSummary(BaseModel):
    id: str
    title: str


class UserSummary(BaseModel):
    id: uuid.UUID
    name: str | None = None
    email: str | None = None
    provider: str


class UserSessionResponse(BaseModel):
    sign_in: bool
    local_development: bool = False
    public_url: str | None = None
    providers: list[ProviderSummary] = []
    user: UserSummary | None = None


class AgentSummary(BaseModel):
    id: str
    title: str
    engine: str
    model: str


class AgentListResponse(BaseModel):
    items: list[AgentSummary]


class ModelSummary(BaseModel):
    id: str
    title: str


class ModelListResponse(BaseModel):
    items: list[ModelSummary]


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str
    agent: str
    model: str
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]
    next_cursor: str | None


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


class TurnRequest(BaseModel):
    text: str | None = None
    parent_id: uuid.UUID | None = None
    regenerate: uuid.UUID | None = None
    model_id: str | None = None


def summary(session: Session, model: str) -> ConversationSummary:
    return ConversationSummary(
        id=session.id,
        title=session.title,
        agent=session.agent,
        model=model,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def content(part: TextPart | ToolCallPart | ToolResultPart) -> BaseModel:
    if isinstance(part, ToolCallPart):
        return ToolCallContent(
            kind="tool_call", call_id=part.call_id, name=part.name, arguments=dict(part.arguments)
        )
    if isinstance(part, ToolResultPart):
        return ToolResultContent(
            kind="tool_result", call_id=part.call_id, text=part.text, is_error=part.is_error
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
        ended_badly=None,
    )


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


def create_app(controller: Controller, *, ui_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await controller.open()
        app.state.user = await controller.ensure_user(local_identity())
        yield
        await controller.close()

    app = FastAPI(
        lifespan=lifespan,
        title="Robinauts",
        version="0",
        docs_url=None,
        redoc_url=None,
        responses={"default": {"model": ErrorResponse, "description": "A refusal"}},
    )

    @app.exception_handler(NotImplementedError)
    async def not_implemented(request: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse({"error": "NotImplemented", "detail": str(exc)}, status_code=501)

    @app.exception_handler(ControllerError)
    async def refused(request: Request, exc: ControllerError) -> JSONResponse:
        status = next((s for cls, s in STATUS_OF.items() if isinstance(exc, cls)), 500)
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=status)

    async def default_model(agent: str) -> str:
        return next(a.default_model for a in await controller.list_agents() if a.id == agent)

    async def watched(session_id: uuid.UUID, turn_id: uuid.UUID, after: int) -> StreamingResponse:
        events = controller.watch_turn(app.state.user, session_id, turn_id, after=after)
        # The refusals happen inside the generator: ask for the first event here, so that
        # they answer with a status rather than a broken stream.
        first = await anext(events, None)

        async def chained() -> AsyncIterator[NumberedEvent]:
            if first is None:
                return
            yield first
            async for event in events:
                yield event

        return event_stream(session_id, turn_id, chained())

    # --- sign-in: none yet ----------------------------------------------------

    @app.get("/auth/session")
    async def current_user_session() -> UserSessionResponse:
        user = app.state.user
        return UserSessionResponse(
            sign_in=False,
            local_development=True,
            user=UserSummary(id=user.id, name=user.name, email=user.email, provider=user.provider),
        )

    @app.post("/auth/logout", status_code=204)
    async def sign_out() -> None:
        return None

    # --- catalogue -------------------------------------------------------------

    @app.get("/api/agents")
    async def list_agents() -> AgentListResponse:
        agents = await controller.list_agents()
        return AgentListResponse(
            items=[
                AgentSummary(id=a.id, title=a.title, engine="", model=a.default_model)
                for a in agents
            ]
        )

    @app.get("/api/models")
    async def list_models() -> ModelListResponse:
        models = await controller.list_models()
        return ModelListResponse(items=[ModelSummary(id=m.id, title=m.title) for m in models])

    # --- sessions --------------------------------------------------------------

    @app.get("/api/conversations")
    async def list_sessions(
        limit: int = DEFAULT_PAGE, cursor: str | None = None
    ) -> ConversationListResponse:
        page = await controller.list_sessions(app.state.user, limit=limit, cursor=cursor)
        defaults = {a.id: a.default_model for a in await controller.list_agents()}
        return ConversationListResponse(
            items=[summary(c, defaults[c.agent]) for c in page.sessions],
            next_cursor=page.cursor,
        )

    @app.get("/api/conversations/{conversation_id}")
    async def open_session(conversation_id: uuid.UUID) -> OpenedConversationResponse:
        opened = await controller.open_session(app.state.user, conversation_id)
        return opened_view(opened, await default_model(opened.session.agent))

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_session(
        conversation_id: uuid.UUID, body: RenameRequest
    ) -> ConversationSummary:
        renamed = await controller.rename_session(app.state.user, conversation_id, body.title)
        return summary(renamed, await default_model(renamed.agent))

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    async def delete_session(conversation_id: uuid.UUID) -> None:
        await controller.delete_session(app.state.user, conversation_id)

    @app.put("/api/conversations/{conversation_id}/model")
    async def set_model(conversation_id: uuid.UUID, body: SetModelRequest) -> ConversationSummary:
        # The model goes with each turn now: this stores nothing, and answers the
        # conversation as the picker will send its next turn.
        opened = await controller.open_session(app.state.user, conversation_id)
        moved = summary(opened.session, body.model_id)
        moved.updated_at = datetime.now(UTC)
        return moved

    @app.post("/api/conversations/{conversation_id}/fork", status_code=201)
    async def fork_session(conversation_id: uuid.UUID, body: ForkRequest) -> ConversationSummary:
        forked = await controller.fork_session(
            app.state.user, conversation_id, at_message=body.at_message
        )
        return summary(forked, await default_model(forked.agent))

    # --- turns: AG-UI over SSE, outside the OpenAPI document -------------------

    @app.post("/api/turns", include_in_schema=False)
    async def start_session(body: NewChatRequest) -> StreamingResponse:
        model = body.model_id or await default_model(body.agent_id)
        started = await controller.start_session(
            app.state.user, agent=body.agent_id, model=model, text=body.text
        )
        return await watched(started.session_id, started.turn_id, 0)

    @app.post("/api/conversations/{conversation_id}/turns", include_in_schema=False)
    async def send_message(conversation_id: uuid.UUID, body: TurnRequest) -> StreamingResponse:
        opened = await controller.open_session(app.state.user, conversation_id)
        model = body.model_id or model_of(opened, await default_model(opened.session.agent))
        if body.regenerate is not None:
            # The frontend names the answer to produce again; the controller, its question.
            at = next(i for i, m in enumerate(opened.messages) if m.id == body.regenerate)
            question = next(m for m in reversed(opened.messages[: at + 1]) if m.role is Role.USER)
            started = await controller.regenerate_answer(
                app.state.user, conversation_id, question_id=question.id, model=model
            )
        else:
            started = await controller.send_message(
                app.state.user,
                conversation_id,
                parent_id=body.parent_id,
                model=model,
                text=body.text,
            )
        return await watched(conversation_id, started.turn_id, 0)

    @app.get("/api/conversations/{conversation_id}/runs/{run_id}/events", include_in_schema=False)
    async def watch_turn(
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        after: int | None = None,
        last_event_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        position = after if after is not None else int(last_event_id or 0)
        return await watched(conversation_id, run_id, position)

    @app.post("/api/conversations/{conversation_id}/runs/{run_id}/cancel", status_code=204)
    async def cancel_turn(conversation_id: uuid.UUID, run_id: uuid.UUID) -> None:
        await controller.cancel_turn(app.state.user, conversation_id, run_id)

    # --- the interface ---------------------------------------------------------

    @app.get("/", include_in_schema=False)
    @app.get("/ui", include_in_schema=False)
    async def to_the_interface() -> RedirectResponse:
        return RedirectResponse("/ui/")

    if ui_dir is not None and (ui_dir / "index.html").is_file():
        app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")
    else:

        @app.get("/ui/", include_in_schema=False)
        async def no_interface() -> HTMLResponse:
            return HTMLResponse(
                "<!doctype html><title>Robinauts</title>"
                "<p>The interface is not built. Run <code>npm run build</code> in"
                " <code>frontend/</code> and start again.</p>"
            )

    return app
