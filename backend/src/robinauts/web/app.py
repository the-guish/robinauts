# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The web shell: the routes of ``docs/architecture/web.md`` over a controller, and the UI.

Every route under ``/api/`` answers for whoever ``current_user`` names: the person a bearer
(``Authorization: Bearer``, a linked device's secret or an API token, told apart by its prefix)
or a session cookie stands for, signed in through an identity provider by the ``/auth/``
routes, or the one user of the local development mode (``docs/specs/sign-in.md``); with nobody,
it is 401. Whatever credential asks, its user is checked against the allow list again, at most
once a minute, and one who fails it loses every credential
(``docs/architecture/device-pairing.md``). A route that mints or removes a credential is
``browsing``: the session cookie's alone, so a bearer is 403; minting also wants the session
younger than ``FRESH_MINUTES``. A write that carries the session cookie
must carry ``Origin`` equal to ``public_url``, else 403; a write with a bearer alone need not.
In the local development mode every request must name ``localhost`` or an IP address as its
``Host``, and a write that carries ``Origin`` must name that ``Host``, else 403.
A controller operation that is not implemented answers 501. The ``/internal/worker/`` routes
answer the server's own worker process alone, by the secret it was handed
(``web/worker_api.py``).
The shapes are the ones the frontend reads (``docs/specs/wire.md``); a turn's stream is
AG-UI over SSE, its run id is the turn's id, and its thread id the session's.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from robinauts.controller.contract.broker.ports import TurnBroker
from robinauts.controller.contract.domain import (
    ApiToken,
    DeviceDescription,
    DeviceSession,
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
from robinauts.controller.contract.ports import Controller, Credentials
from robinauts.shared_domain.domain import (
    ControllerError,
    Message,
    ReasoningPart,
    Role,
    Session,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Turn,
)
from robinauts.web import agui
from robinauts.web.cookies import Cookies
from robinauts.web.headers import SecurityHeaders
from robinauts.web.logs import loggable
from robinauts.web.oidc import Exchange, SecretLookup, claims_from
from robinauts.web.pairing import (
    FRESH_MINUTES,
    Pairing,
    PairingError,
    PairingErrorCode,
    Pairings,
    is_fresh,
)
from robinauts.web.sign_in import (
    DEVICE_DAYS,
    LOCAL_PROVIDER,
    PENDING_LOGIN_LIFE,
    PROVIDER_ID_PATTERN,
    RecentChecks,
    SecretKind,
    SignIn,
    SignInConfig,
    SignInError,
    SignInErrorCode,
    is_allowed,
    kind_of,
    secret_hash,
    secret_of_kind,
)
from robinauts.web.worker_api import WorkerAccess, worker_router

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
NOT_LOCAL_HOST = "the local development mode answers a Host of localhost or an IP address only"
NOT_LOCAL_ORIGIN = "a write in the local development mode must carry Origin equal to its Host"
UNKNOWN_TOKEN = "the API token is unknown, revoked or expired"
UNKNOWN_DEVICE = "the device is unknown, removed or expired: pair it again"
NO_SUCH_TOKEN = "you have no API token of that id"
NO_SUCH_DEVICE = "you have no device of that id"
BEARER_MAY_NOT = "a bearer never mints or removes a credential: do it in the browser, signed in"
NOT_FRESH = (
    f"minting a credential wants a sign-in less than {FRESH_MINUTES} minutes old: sign in again"
)

PAIRING_REFUSALS: dict[PairingErrorCode, tuple[int, str]] = {
    PairingErrorCode.NO_DEVICES: (403, "Forbidden"),
    PairingErrorCode.BUSY: (429, "Busy"),
    PairingErrorCode.NOT_FOUND: (404, "NotFound"),
    PairingErrorCode.NOT_CLAIMED: (409, "Conflict"),
    PairingErrorCode.WRONG_NUMBER: (403, "WrongNumber"),
}

LOCAL_IDENTITY = Identity(provider=LOCAL_PROVIDER, subject="developer", name="Local development")
"""The one user of the local development mode, under a provider no configuration can name
(``sign_in.PROVIDER_ID_PATTERN``), so that no identity a provider vouches for can carry it."""

log = logging.getLogger(__name__)


def local_host(values: list[str]) -> str | None:
    """The one ``Host`` header, when it names ``localhost`` or an IP address, else ``None``.

    DNS rebinding needs a name: a page on the internet points its own name at this machine,
    and the browser sends that name as ``Host``. An IP address cannot be rebound, so the
    phone on the LAN that reaches the frontend's dev server at ``192.168.1.10:5173`` passes.
    """
    if len(values) != 1:
        return None
    host = values[0].lower()
    if any(c in host for c in "/?#@\\"):
        return None
    try:
        parts = urlsplit(f"//{host}")
        _ = parts.port  # a port that is not a number raises
    except ValueError:
        return None
    if parts.hostname == "localhost":
        return host
    try:
        ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        return None
    return host


class Via(StrEnum):
    """How the asker was named."""

    COOKIE = "cookie"
    DEVICE = "device"
    TOKEN = "token"
    LOCAL = "local"
    """The local development mode's one user, who counts as the browser's."""


@dataclass(frozen=True, slots=True)
class Asker:
    """Who is asking and by which credential: ``secret_hash`` is that credential's, the key of
    the allow-list check on use; ``signed_in_at`` a session cookie's ``created_at``."""

    user: User
    via: Via
    secret_hash: str = ""
    signed_in_at: datetime | None = None
    device: DeviceSession | None = None


class Refused(Exception):
    """A request answered with the wire's error body before it reaches the controller."""

    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail

    def answer(self) -> JSONResponse:
        return JSONResponse({"error": self.error, "detail": self.detail}, status_code=self.status)


class Guard:
    """Run ``check`` on every HTTP request, and answer its ``Refused`` here.

    Plain ASGI, so the check reaches the mounted interface too: FastAPI's app dependencies
    reach routes only.
    """

    def __init__(self, app: ASGIApp, check: Callable[[Request], None]) -> None:
        self.app = app
        self.check = check

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            try:
                self.check(Request(scope))
            except Refused as refused:
                await refused.answer()(scope, receive, send)
                return
        await self.app(scope, receive, send)


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


class DeviceSummary(BaseModel):
    id: uuid.UUID
    name: str
    platform: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime


class DeviceListResponse(BaseModel):
    items: list[DeviceSummary]


class TokenBrief(BaseModel):
    id: uuid.UUID
    name: str
    expires_at: datetime


class PairingStateView(BaseModel):
    state: Literal["waiting", "claimed", "approved"]


class UserSessionResponse(BaseModel):
    sign_in: bool
    local_development: bool = False
    public_url: str | None = None
    providers: list[ProviderSummary] = []
    user: UserSummary | None = None
    device: DeviceSummary | None = None
    """The device a device bearer is."""
    token: TokenBrief | None = None
    """The token a token bearer is."""
    pairing: PairingStateView | None = None
    """The pairing the bearer of a claim waits on, before it is approved."""


class MintedPairingResponse(BaseModel):
    """The code is in the picture and nowhere else."""

    id: uuid.UUID
    expires_at: datetime
    qr_svg: str


class DeviceDescriptionView(BaseModel):
    name: str
    platform: str
    app_version: str | None


class PairingView(BaseModel):
    state: Literal["waiting", "claimed", "approved"]
    expires_at: datetime
    device: DeviceDescriptionView | None


class ApproveRequest(BaseModel):
    number: str = Field(pattern=r"^[0-9]{3} ?[0-9]{3}$")
    """Six digits, with or without the space in the middle."""


def printable(text: str | None) -> str | None:
    if text is not None and not text.isprintable():
        raise ValueError("printable text only")
    return text


class ClaimedDevice(BaseModel):
    """What the phone says of itself, bounded: data for the list."""

    name: str = Field(min_length=1, max_length=64)
    platform: Literal["android", "ios", "desktop", "other"]
    app_version: str | None = Field(default=None, max_length=32)

    _printable = field_validator("name", "app_version")(printable)


class ClaimRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    device: ClaimedDevice


class ClaimResponse(BaseModel):
    secret: str
    number: str
    expires_at: datetime


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
    resume: uuid.UUID | None = None
    model_id: str | None = None


def user_summary(user: User) -> UserSummary:
    return UserSummary(id=user.id, name=user.name, email=user.email, provider=user.provider)


def device_summary(device: DeviceSession) -> DeviceSummary:
    return DeviceSummary(
        id=device.id,
        name=device.device.name,
        platform=device.device.platform,
        created_at=device.created_at,
        last_used_at=device.last_used_at,
        expires_at=device.expires_at,
    )


def pairing_view(pairing: Pairing) -> PairingView:
    device = pairing.device
    return PairingView(
        state=pairing.state.value,
        expires_at=pairing.expires_at,
        device=None if device is None else DeviceDescriptionView(**_fields(device)),
    )


def _fields(device: DeviceDescription) -> dict[str, str | None]:
    return {"name": device.name, "platform": device.platform, "app_version": device.app_version}


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
    credentials: Credentials,
    brokers: Mapping[str, TurnBroker],
    *,
    worker_access: WorkerAccess,
    healthy: Callable[[], bool],
    sign_in: SignInConfig | None,
    secret_for: SecretLookup,
    ui_dir: Path | None = None,
) -> FastAPI:
    """The HTTP server alone. Whoever runs it opens the controller and starts the worker before
    it serves, and stops the worker and closes the controller after (``cli/lifecycle.py``).
    ``brokers``, one per worker by name, and ``worker_access`` serve the workers in processes
    of their own at ``/internal/worker/``; ``healthy`` says whether this node's workers are
    heard from, for ``/health``. ``sign_in`` is ``None`` in the local development mode."""
    exchange: Exchange | None = None
    flow: SignIn | None = None
    if sign_in is not None:
        exchange = Exchange(sign_in, secret_for)
        flow = SignIn(sign_in, credentials=credentials, exchange=exchange, controller=controller)
        cookies = Cookies(sign_in.public_url)
    pairings = Pairings(
        credentials, device_days=DEVICE_DAYS if sign_in is None else sign_in.device_days
    )
    recent = RecentChecks()
    local_user: User | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal local_user
        if sign_in is None:
            local_user = await controller.ensure_user(LOCAL_IDENTITY)
        yield
        if exchange is not None:
            await exchange.aclose()

    # --- who is asking -----------------------------------------------------------

    def same_origin(request: Request) -> None:
        if sign_in is None:
            host = local_host(request.headers.getlist("host"))
            if host is None:
                raise Refused(403, "Forbidden", NOT_LOCAL_HOST)
            origin = request.headers.get("origin")
            same = (None, f"http://{host}", f"https://{host}")
            if request.method not in SAFE_METHODS and origin not in same:
                raise Refused(403, "Forbidden", NOT_LOCAL_ORIGIN)
        elif (
            request.method not in SAFE_METHODS
            and cookies.session in request.cookies
            and request.headers.get("origin") != sign_in.public_url
        ):
            raise Refused(403, "Forbidden", NOT_SAME_ORIGIN)

    def bearer_of(request: Request) -> str | None:
        scheme, _, secret = request.headers.get("authorization", "").partition(" ")
        return secret if scheme.lower() == "bearer" else None

    async def still_allowed(asker: Asker, now: datetime) -> bool:
        """The allow list as it is now, against what the user's last sign-in said; a failure
        deletes every credential of theirs, so the matcher put back revives none."""
        if recent.passed(asker.secret_hash, now):
            return True
        user = asker.user
        if is_allowed(sign_in.allow, user.provider, claims_from(user)):
            recent.record(asker.secret_hash, now)
            return True
        deleted = await credentials.delete_credentials_of(user.id)
        log.warning(
            "user %s is not on the allow list any more: %s credentials of theirs deleted",
            user.id,
            deleted,
        )
        return False

    async def resolve(request: Request, now: datetime) -> Asker | None:
        """Who the bearer or, without one, the session cookie stands for; ``None`` for
        nobody, and for a user who failed the allow list just now."""
        secret = bearer_of(request)
        asker: Asker | None = None
        if secret is not None:
            hashed = secret_hash(secret)
            if kind_of(secret) is SecretKind.DEVICE:
                device = await credentials.resolve_device_session(hashed, now)
                if device is not None:
                    asker = Asker(device[0], Via.DEVICE, hashed, device=device[1])
            else:
                user = await credentials.resolve_api_token(hashed, now)
                if user is not None:
                    asker = Asker(user, Via.TOKEN, hashed)
        else:
            cookie = request.cookies.get(cookies.session)
            found = await flow.resolve(cookie, now) if cookie else None
            if found is not None:
                user, session = found
                asker = Asker(user, Via.COOKIE, session.secret_hash, session.created_at)
        # The local development mode links devices and mints tokens for its user too: as with
        # a session, a bearer naming that user signs nobody in.
        if asker is None or asker.user.provider == LOCAL_PROVIDER:
            return None
        return asker if await still_allowed(asker, now) else None

    async def current_asker(request: Request) -> Asker:
        if flow is None:
            return Asker(local_user, Via.LOCAL)
        asker = await resolve(request, datetime.now(UTC))
        if asker is None:
            secret = bearer_of(request)
            if secret is None:
                raise Refused(401, "Unauthorized", NOT_SIGNED_IN)
            device = kind_of(secret) is SecretKind.DEVICE
            raise Refused(401, "Unauthorized", UNKNOWN_DEVICE if device else UNKNOWN_TOKEN)
        return asker

    asked = Depends(current_asker)

    async def current_user(asker: Asker = asked) -> User:
        return asker.user

    async def browsing_user(asker: Asker = asked) -> User:
        """The browser's session alone: a bearer never mints or removes a credential."""
        if asker.via in (Via.DEVICE, Via.TOKEN):
            raise Refused(403, "Forbidden", BEARER_MAY_NOT)
        return asker.user

    async def fresh_browsing_user(asker: Asker = asked) -> User:
        """The browser's session, signed in less than ``FRESH_MINUTES`` ago."""
        user = await browsing_user(asker)
        if asker.via is Via.COOKIE and not is_fresh(asker.signed_in_at, datetime.now(UTC)):
            raise Refused(403, "FreshSignInRequired", NOT_FRESH)
        return user

    asking = Depends(current_user)
    browsing = Depends(browsing_user)
    browsing_fresh = Depends(fresh_browsing_user)

    app = FastAPI(
        lifespan=lifespan,
        title="Robinauts",
        version="0",
        docs_url=None,
        redoc_url=None,
        responses={"default": {"model": ErrorResponse, "description": "A refusal"}},
    )
    app.add_middleware(Guard, check=same_origin)
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(Refused)
    async def not_let_in(request: Request, exc: Refused) -> JSONResponse:
        return exc.answer()

    @app.exception_handler(PairingError)
    async def not_paired(request: Request, exc: PairingError) -> JSONResponse:
        status, error = PAIRING_REFUSALS[exc.code]
        return JSONResponse({"error": error, "detail": exc.detail}, status_code=status)

    @app.exception_handler(NotImplementedError)
    async def not_implemented(request: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse({"error": "NotImplemented", "detail": str(exc)}, status_code=501)

    @app.exception_handler(ControllerError)
    async def refused(request: Request, exc: ControllerError) -> JSONResponse:
        status = next((s for cls, s in STATUS_OF.items() if isinstance(exc, cls)), 500)
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=status)

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
        now = datetime.now(UTC)
        answer = UserSessionResponse(
            sign_in=True,
            public_url=sign_in.public_url,
            providers=[ProviderSummary(id=p.id, title=p.title) for p in sign_in.providers.values()],
        )
        asker = await resolve(request, now)
        secret = bearer_of(request)
        if asker is None:
            # A phone waiting for the browser to approve its claim.
            if secret is not None and kind_of(secret) is SecretKind.DEVICE:
                state = await pairings.poll(secret, now)
                if state is not None:
                    answer.pairing = PairingStateView(state=state.value)
            return answer
        answer.user = user_summary(asker.user)
        if asker.device is not None:
            answer.device = device_summary(asker.device)
        elif asker.via is Via.TOKEN:
            tokens = await credentials.api_tokens_of(asker.user.id)
            token = next((t for t in tokens if t.secret_hash == asker.secret_hash), None)
            if token is not None:
                answer.token = TokenBrief(id=token.id, name=token.name, expires_at=token.expires_at)
        return answer

    @app.post("/auth/logout", status_code=204)
    async def sign_out(request: Request, response: Response) -> None:
        if flow is None:
            return
        now = datetime.now(UTC)
        bearer = bearer_of(request)
        if bearer is not None:
            # A device ends itself; a token does not, and is revoked in the browser.
            if kind_of(bearer) is SecretKind.DEVICE:
                found = await credentials.resolve_device_session(secret_hash(bearer), now)
                if found is not None:
                    user, device = found
                    await credentials.delete_device_session(user.id, device.id)
                    log.info("device %s of user %s signed out", device.id, user.id)
            return
        secret = request.cookies.get(cookies.session)
        if secret:
            found = await flow.resolve(secret, now)
            if await flow.sign_out(secret) and found is not None:
                log.info("user %s signed out", found[0].id)
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
    async def mint_api_token(
        body: NewApiTokenRequest, user: User = browsing_fresh
    ) -> NewApiTokenResponse:
        secret = secret_of_kind(SecretKind.TOKEN)
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
    async def revoke_api_token(token_id: uuid.UUID, user: User = browsing) -> None:
        if not await credentials.delete_api_token(user.id, token_id):
            raise Refused(404, "NotFound", NO_SUCH_TOKEN)

    # --- pairing a device: docs/architecture/device-pairing.md -------------------

    def origin_of(request: Request) -> str:
        """The origin the QR names: ``public_url``, or the local development mode's host."""
        if sign_in is not None:
            return sign_in.public_url
        return f"http://{local_host(request.headers.getlist('host'))}"

    @app.post("/auth/pairings", status_code=201)
    async def mint_pairing(request: Request, user: User = browsing_fresh) -> MintedPairingResponse:
        minted = await pairings.mint(user, origin=origin_of(request), now=datetime.now(UTC))
        return MintedPairingResponse(
            id=minted.pairing.id, expires_at=minted.pairing.expires_at, qr_svg=minted.qr_svg
        )

    @app.post("/auth/pairings/claim")
    async def claim_pairing(body: ClaimRequest) -> ClaimResponse:
        device = DeviceDescription(body.device.name, body.device.platform, body.device.app_version)
        claimed = await pairings.claim(body.code, device, datetime.now(UTC))
        return ClaimResponse(
            secret=claimed.secret, number=claimed.number, expires_at=claimed.expires_at
        )

    @app.get("/auth/pairings/{pairing_id}")
    async def pairing_state(pairing_id: uuid.UUID, user: User = browsing) -> PairingView:
        return pairing_view(await pairings.pairing_of(user, pairing_id, datetime.now(UTC)))

    @app.post("/auth/pairings/{pairing_id}/approve")
    async def approve_pairing(
        pairing_id: uuid.UUID, body: ApproveRequest, user: User = browsing
    ) -> DeviceSummary:
        device = await pairings.approve(user, pairing_id, body.number, datetime.now(UTC))
        log.info("device %s linked to user %s", device.id, user.id)
        return device_summary(device)

    @app.delete("/auth/pairings/{pairing_id}", status_code=204)
    async def refuse_pairing(pairing_id: uuid.UUID, user: User = browsing) -> None:
        await pairings.refuse(user, pairing_id)
        log.info("pairing %s of user %s refused or cancelled", pairing_id, user.id)

    # --- linked devices ------------------------------------------------------------

    @app.get("/auth/devices")
    async def list_devices(user: User = asking) -> DeviceListResponse:
        devices = await credentials.device_sessions_of(user.id)
        return DeviceListResponse(items=[device_summary(d) for d in devices])

    @app.delete("/auth/devices/{device_id}", status_code=204)
    async def remove_device(device_id: uuid.UUID, user: User = browsing) -> None:
        if not await credentials.delete_device_session(user.id, device_id):
            raise Refused(404, "NotFound", NO_SUCH_DEVICE)
        log.info("device %s of user %s removed", device_id, user.id)

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
            items=[summary(c, defaults[c.agent]) for c in page.sessions],
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
        started = await controller.start_session(
            user, agent=body.agent_id, model=model, text=body.text
        )
        return await stream_turn(user, started.session_id, started.turn_id, 0)

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
        elif body.resume is not None:
            started = await controller.resume_answer(
                user, conversation_id, answer_id=body.resume, model=model
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
        return await stream_turn(user, conversation_id, started.turn_id, 0)

    @app.get("/api/conversations/{conversation_id}/runs/{run_id}/events", include_in_schema=False)
    async def watch_turn(
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        after: int | None = None,
        last_event_id: Annotated[str | None, Header()] = None,
        user: User = asking,
    ) -> StreamingResponse:
        position = after if after is not None else int(last_event_id or 0)
        return await stream_turn(user, conversation_id, run_id, position)

    @app.post("/api/conversations/{conversation_id}/runs/{run_id}/cancel", status_code=204)
    async def cancel_turn(
        conversation_id: uuid.UUID, run_id: uuid.UUID, user: User = asking
    ) -> None:
        await controller.cancel_turn(user, conversation_id, run_id)

    # --- a worker in a process of its own ------------------------------------------

    app.include_router(worker_router(brokers, worker_access))

    # --- liveness --------------------------------------------------------------

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        # This node's workers, each heard from within five minutes. Nothing is queried.
        if healthy():
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "the worker is not running"}, status_code=503)

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
