# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The web shell: the routes of ``docs/architecture/web.md`` over a controller, and the UI.

Every route under ``/api/`` answers for whoever ``current_user`` names: the person an API
token (``Authorization: Bearer``) or a session cookie stands for, signed in through an identity
provider by the ``/auth/`` routes, or the one user of the local development mode
(``docs/specs/sign-in.md``); with nobody, it is 401. A write that carries the session cookie
must carry ``Origin`` equal to ``public_url``, else 403; a write with a bearer alone need not.
A controller operation that is not implemented answers 501.
The shapes are the ones the frontend reads (``docs/specs/wire.md``); a turn's stream is
AG-UI over SSE, its run id is the turn's id, and its thread id the session's.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from robinauts.controller.composition import SecretLookup
from robinauts.controller.contract.domain import (
    ApiToken,
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
    Turn,
    TurnActiveError,
    UnknownAgentError,
    UnknownModelError,
    User,
)
from robinauts.controller.contract.ports import Controller, Credentials
from robinauts.web import agui
from robinauts.web.cookies import Cookies
from robinauts.web.logs import loggable
from robinauts.web.oidc import Exchange
from robinauts.web.sign_in import (
    LOCAL_PROVIDER,
    PENDING_LOGIN_LIFE,
    PROVIDER_ID_PATTERN,
    SignIn,
    SignInConfig,
    SignInError,
    SignInErrorCode,
    random_secret,
    secret_hash,
)

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

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SIGN_IN_PAGE = "/ui/#/sign-in?error="
TOKEN_LIFE = timedelta(days=90)
NOT_SIGNED_IN = "nobody is signed in: sign in at /ui/"
NOT_SAME_ORIGIN = "a write that carries the session cookie must carry Origin equal to public_url"
UNKNOWN_TOKEN = "the API token is unknown, revoked or expired"
NO_SUCH_TOKEN = "you have no API token of that id"

LOCAL_IDENTITY = Identity(provider=LOCAL_PROVIDER, subject="developer", name="Local development")
"""The one user of the local development mode, under a provider no configuration can name
(``sign_in.PROVIDER_ID_PATTERN``), so that no identity a provider vouches for can carry it."""

log = logging.getLogger(__name__)


class Refused(Exception):
    """A request answered with the wire's error body before it reaches the controller."""

    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


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


class NewApiTokenRequest(BaseModel):
    name: str


class ApiTokenSummary(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    expires_at: datetime


class NewApiTokenResponse(ApiTokenSummary):
    secret: str


class ApiTokenListResponse(BaseModel):
    items: list[ApiTokenSummary]


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
    failed: bool = False
    """An answer whose turn failed: what it streamed before the failure."""


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
    edit: uuid.UUID | None = None
    regenerate: uuid.UUID | None = None
    retry: uuid.UUID | None = None
    model_id: str | None = None


def user_summary(user: User) -> UserSummary:
    return UserSummary(id=user.id, name=user.name, email=user.email, provider=user.provider)


def token_summary(token: ApiToken) -> ApiTokenSummary:
    return ApiTokenSummary(
        id=token.id, name=token.name, created_at=token.created_at, expires_at=token.expires_at
    )


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
        failed=message.failed,
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


def create_app(
    controller: Controller,
    *,
    credentials: Credentials,
    sign_in: SignInConfig | None,
    secret_for: SecretLookup,
    ui_dir: Path | None = None,
) -> FastAPI:
    """``sign_in`` is ``None`` in the local development mode."""
    exchange: Exchange | None = None
    flow: SignIn | None = None
    if sign_in is not None:
        exchange = Exchange(sign_in, secret_for)
        flow = SignIn(sign_in, credentials=credentials, exchange=exchange, controller=controller)
        cookies = Cookies(sign_in.public_url)
    local_user: User | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal local_user
        await controller.open()
        if sign_in is None:
            local_user = await controller.ensure_user(LOCAL_IDENTITY)
        yield
        if exchange is not None:
            await exchange.aclose()
        await controller.close()

    # --- who is asking -----------------------------------------------------------

    def same_origin(request: Request) -> None:
        if (
            sign_in is not None
            and request.method not in SAFE_METHODS
            and cookies.session in request.cookies
            and request.headers.get("origin") != sign_in.public_url
        ):
            raise Refused(403, "Forbidden", NOT_SAME_ORIGIN)

    async def session_user(request: Request) -> User | None:
        secret = request.cookies.get(cookies.session)
        return await flow.resolve(secret, datetime.now(UTC)) if secret else None

    async def token_user(secret: str) -> User | None:
        user = await credentials.resolve_api_token(secret_hash(secret), datetime.now(UTC))
        # The local development mode mints tokens for its user too: as with a session, a token
        # naming that user signs nobody in.
        return None if user is None or user.provider == LOCAL_PROVIDER else user

    async def current_user(request: Request) -> User:
        if flow is None:
            return local_user
        scheme, _, secret = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            user = await token_user(secret)
            if user is None:
                raise Refused(401, "Unauthorized", UNKNOWN_TOKEN)
            return user
        user = await session_user(request)
        if user is None:
            raise Refused(401, "Unauthorized", NOT_SIGNED_IN)
        return user

    asking = Depends(current_user)

    app = FastAPI(
        lifespan=lifespan,
        title="Robinauts",
        version="0",
        docs_url=None,
        redoc_url=None,
        responses={"default": {"model": ErrorResponse, "description": "A refusal"}},
        dependencies=[Depends(same_origin)],
    )

    @app.exception_handler(Refused)
    async def not_let_in(request: Request, exc: Refused) -> JSONResponse:
        return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(NotImplementedError)
    async def not_implemented(request: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse({"error": "NotImplemented", "detail": str(exc)}, status_code=501)

    @app.exception_handler(ControllerError)
    async def refused(request: Request, exc: ControllerError) -> JSONResponse:
        status = next((s for cls, s in STATUS_OF.items() if isinstance(exc, cls)), 500)
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=status)

    async def default_model(agent: str) -> str:
        """The agent's default model; "" for an agent this process's configuration does not
        name, which a turn on it then refuses, as a rolling change of configuration can leave
        one replica behind the others."""
        return next((a.default_model for a in await controller.list_agents() if a.id == agent), "")

    async def watched(
        user: User, session_id: uuid.UUID, turn_id: uuid.UUID, after: int
    ) -> StreamingResponse:
        events = controller.watch_turn(user, session_id, turn_id, after=after)
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

    # --- sign-in -----------------------------------------------------------------

    def not_signed_in(refusal: SignInError) -> RedirectResponse:
        log.warning("sign-in refused, %s: %s", refusal.code, refusal.detail)
        return RedirectResponse(SIGN_IN_PAGE + refusal.code, status_code=302)

    malformed_provider = SignInError(
        SignInErrorCode.UNKNOWN_PROVIDER, "the provider in the URL is not a valid id"
    )

    @app.get("/auth/session")
    async def current_user_session(request: Request) -> UserSessionResponse:
        if sign_in is None:
            return UserSessionResponse(
                sign_in=False, local_development=True, user=user_summary(local_user)
            )
        user = await session_user(request)
        return UserSessionResponse(
            sign_in=True,
            public_url=sign_in.public_url,
            providers=[ProviderSummary(id=p.id, title=p.title) for p in sign_in.providers.values()],
            user=None if user is None else user_summary(user),
        )

    @app.post("/auth/logout", status_code=204)
    async def sign_out(request: Request, response: Response) -> None:
        if flow is None:
            return
        secret = request.cookies.get(cookies.session)
        if secret:
            user = await flow.resolve(secret, datetime.now(UTC))
            if await flow.sign_out(secret) and user is not None:
                log.info("user %s signed out", user.id)
        cookies.clear(response, cookies.session)

    if flow is not None:
        # Navigations, not calls: outside the OpenAPI document.

        @app.get("/auth/login/{provider}", include_in_schema=False)
        async def begin_sign_in(provider: str, return_to: str | None = None) -> Response:
            if not PROVIDER_ID_PATTERN.fullmatch(provider):
                return not_signed_in(malformed_provider)
            try:
                url, state = await flow.begin(provider, return_to=return_to, now=datetime.now(UTC))
            except SignInError as refusal:
                return not_signed_in(refusal)
            response = RedirectResponse(url, status_code=302)
            cookies.set(response, cookies.login, state, PENDING_LOGIN_LIFE)
            return response

        @app.get("/auth/callback/{provider}", include_in_schema=False)
        async def finish_sign_in(
            request: Request,
            provider: str,
            code: str | None = None,
            state: str | None = None,
            error: str | None = None,
            error_description: str | None = None,
        ) -> Response:
            if not PROVIDER_ID_PATTERN.fullmatch(provider):
                return not_signed_in(malformed_provider)
            if error is not None or code is None:
                return not_signed_in(
                    SignInError(
                        SignInErrorCode.PROVIDER_REFUSED,
                        f"{provider} answered error={error!r}"
                        f" error_description={error_description!r}",
                    )
                )
            now = datetime.now(UTC)
            try:
                done = await flow.complete(
                    provider,
                    state=state,
                    cookie_state=request.cookies.get(cookies.login),
                    code=code,
                    now=now,
                )
            except SignInError as refusal:
                return not_signed_in(refusal)
            log.info("user %s signed in with %s", done.user.id, loggable(provider))
            response = RedirectResponse(sign_in.public_url + done.return_to, status_code=302)
            cookies.set(response, cookies.session, done.secret, done.expires_at - now)
            cookies.clear(response, cookies.login)
            return response

    # --- API tokens: shown once, kept as their SHA-256 ---------------------------

    @app.post("/auth/tokens", status_code=201)
    async def mint_api_token(body: NewApiTokenRequest, user: User = asking) -> NewApiTokenResponse:
        secret = random_secret()
        now = datetime.now(UTC)
        token = ApiToken(
            uuid.uuid4(), user.id, body.name, secret_hash(secret), now, now + TOKEN_LIFE
        )
        await credentials.add_api_token(token)
        return NewApiTokenResponse(**token_summary(token).model_dump(), secret=secret)

    @app.get("/auth/tokens")
    async def list_api_tokens(user: User = asking) -> ApiTokenListResponse:
        tokens = await credentials.api_tokens_of(user.id)
        return ApiTokenListResponse(items=[token_summary(t) for t in tokens])

    @app.delete("/auth/tokens/{token_id}", status_code=204)
    async def revoke_api_token(token_id: uuid.UUID, user: User = asking) -> None:
        if not await credentials.delete_api_token(user.id, token_id):
            raise Refused(404, "NotFound", NO_SUCH_TOKEN)

    # --- catalogue -------------------------------------------------------------

    @app.get("/api/agents", dependencies=[asking])
    async def list_agents() -> AgentListResponse:
        agents = await controller.list_agents()
        return AgentListResponse(
            items=[
                AgentSummary(id=a.id, title=a.title, engine="", model=a.default_model)
                for a in agents
            ]
        )

    @app.get("/api/models", dependencies=[asking])
    async def list_models() -> ModelListResponse:
        models = await controller.list_models()
        return ModelListResponse(items=[ModelSummary(id=m.id, title=m.title) for m in models])

    # --- sessions --------------------------------------------------------------

    @app.get("/api/conversations")
    async def list_sessions(
        limit: int = DEFAULT_PAGE, cursor: str | None = None, user: User = asking
    ) -> ConversationListResponse:
        page = await controller.list_sessions(user, limit=limit, cursor=cursor)
        defaults = {a.id: a.default_model for a in await controller.list_agents()}
        return ConversationListResponse(
            items=[summary(c, defaults.get(c.agent, "")) for c in page.sessions],
            next_cursor=page.cursor,
        )

    @app.get("/api/conversations/{conversation_id}")
    async def open_session(
        conversation_id: uuid.UUID, user: User = asking
    ) -> OpenedConversationResponse:
        opened = await controller.open_session(user, conversation_id)
        return opened_view(opened, await default_model(opened.session.agent))

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_session(
        conversation_id: uuid.UUID, body: RenameRequest, user: User = asking
    ) -> ConversationSummary:
        renamed = await controller.rename_session(user, conversation_id, body.title)
        return summary(renamed, await default_model(renamed.agent))

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    async def delete_session(conversation_id: uuid.UUID, user: User = asking) -> None:
        await controller.delete_session(user, conversation_id)

    @app.put("/api/conversations/{conversation_id}/model")
    async def set_model(
        conversation_id: uuid.UUID, body: SetModelRequest, user: User = asking
    ) -> ConversationSummary:
        # The model goes with each turn now: this stores nothing, and answers the
        # conversation as the picker will send its next turn.
        opened = await controller.open_session(user, conversation_id)
        moved = summary(opened.session, body.model_id)
        moved.updated_at = datetime.now(UTC)
        return moved

    @app.post("/api/conversations/{conversation_id}/fork", status_code=201)
    async def fork_session(
        conversation_id: uuid.UUID, body: ForkRequest, user: User = asking
    ) -> ConversationSummary:
        forked = await controller.fork_session(user, conversation_id, at_message=body.at_message)
        return summary(forked, await default_model(forked.agent))

    # --- turns: AG-UI over SSE, outside the OpenAPI document -------------------

    @app.post("/api/turns", include_in_schema=False)
    async def start_session(body: NewChatRequest, user: User = asking) -> StreamingResponse:
        model = body.model_id or await default_model(body.agent_id)
        if not model:
            raise UnknownAgentError(body.agent_id)
        started = await controller.start_session(
            user, agent=body.agent_id, model=model, text=body.text
        )
        return await watched(user, started.session_id, started.turn_id, 0)

    @app.post("/api/conversations/{conversation_id}/turns", include_in_schema=False)
    async def send_message(
        conversation_id: uuid.UUID, body: TurnRequest, user: User = asking
    ) -> StreamingResponse:
        opened = await controller.open_session(user, conversation_id)
        model = body.model_id or model_of(opened, await default_model(opened.session.agent))
        if body.regenerate is not None:
            # The frontend names the answer to produce again; the controller, its question.
            at = next((i for i, m in enumerate(opened.messages) if m.id == body.regenerate), None)
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
        elif body.edit is not None:
            started = await controller.edit_message(
                user, conversation_id, message_id=body.edit, model=model, text=body.text
            )
        elif body.parent_id is not None:
            started = await controller.send_message(
                user, conversation_id, parent_id=body.parent_id, model=model, text=body.text
            )
        else:
            raise InvalidValueError("a message names the parent_id it answers, or the one it edits")
        return await watched(user, conversation_id, started.turn_id, 0)

    @app.get("/api/conversations/{conversation_id}/runs/{run_id}/events", include_in_schema=False)
    async def watch_turn(
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        after: int | None = None,
        last_event_id: Annotated[str | None, Header()] = None,
        user: User = asking,
    ) -> StreamingResponse:
        position = after if after is not None else int(last_event_id or 0)
        return await watched(user, conversation_id, run_id, position)

    @app.post("/api/conversations/{conversation_id}/runs/{run_id}/cancel", status_code=204)
    async def cancel_turn(
        conversation_id: uuid.UUID, run_id: uuid.UUID, user: User = asking
    ) -> None:
        await controller.cancel_turn(user, conversation_id, run_id)

    # --- liveness --------------------------------------------------------------

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        # That this process answers, and nothing about the database or the providers.
        return {"status": "ok"}

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
