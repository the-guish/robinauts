# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The web shell: the routes of ``docs/architecture/web.md`` over a controller, and the UI.

No sign-in yet: every request runs as one local user, named after the operating
system's. A controller operation that is not implemented answers 501.
"""

from __future__ import annotations

import dataclasses
import getpass
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from robinauts.controller.contract.domain import (
    ControllerError,
    ConversationNotFoundError,
    Identity,
    InvalidValueError,
    MessageNotFoundError,
    NoActiveTurnError,
    TurnActiveError,
    UnknownAgentError,
    UnknownModelError,
)
from robinauts.controller.contract.ports import Controller

LOCAL_PROVIDER = "local"
DEFAULT_PAGE = 30

STATUS_OF: dict[type[ControllerError], int] = {
    InvalidValueError: 422,
    ConversationNotFoundError: 404,
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


def as_data(value: Any) -> Any:
    """A record of the contract as the JSON the frontend reads."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return jsonable_encoder(value)


class RenameRequest(BaseModel):
    title: str


class ForkRequest(BaseModel):
    at_message: uuid.UUID


class StartRequest(BaseModel):
    agent: str
    model: str
    text: str


class TurnRequest(BaseModel):
    model: str
    text: str | None = None
    parent_id: uuid.UUID | None = None
    regenerate: uuid.UUID | None = None


def create_app(controller: Controller, *, ui_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Robinauts", docs_url=None, redoc_url=None)

    @app.exception_handler(NotImplementedError)
    async def not_implemented(request: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse({"error": "NotImplemented", "detail": str(exc)}, status_code=501)

    @app.exception_handler(ControllerError)
    async def refused(request: Request, exc: ControllerError) -> JSONResponse:
        status = next((s for cls, s in STATUS_OF.items() if isinstance(exc, cls)), 500)
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=status)

    @app.on_event("startup")
    async def opened() -> None:
        await controller.open()
        app.state.user = await controller.ensure_user(local_identity())

    @app.on_event("shutdown")
    async def closed() -> None:
        await controller.close()

    # --- sign-in: none yet ----------------------------------------------------

    @app.get("/auth/session")
    async def current_session() -> dict[str, Any]:
        return {
            "sign_in": False,
            "local_development": True,
            "public_url": None,
            "providers": [],
            "user": as_data(app.state.user),
        }

    @app.post("/auth/logout", status_code=204)
    async def sign_out() -> None:
        return None

    # --- catalogue -------------------------------------------------------------

    @app.get("/api/agents")
    async def list_agents() -> dict[str, Any]:
        return {"items": as_data(await controller.list_agents())}

    @app.get("/api/models")
    async def list_models() -> dict[str, Any]:
        return {"items": as_data(await controller.list_models())}

    # --- conversations ---------------------------------------------------------

    @app.get("/api/conversations")
    async def list_conversations(limit: int = DEFAULT_PAGE, cursor: str | None = None) -> Any:
        page = await controller.list_conversations(app.state.user, limit=limit, cursor=cursor)
        return {"items": as_data(page.conversations), "next_cursor": page.cursor}

    @app.get("/api/conversations/{conversation_id}")
    async def open_conversation(conversation_id: uuid.UUID) -> Any:
        return as_data(await controller.open_conversation(app.state.user, conversation_id))

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(conversation_id: uuid.UUID, body: RenameRequest) -> Any:
        return as_data(
            await controller.rename_conversation(app.state.user, conversation_id, body.title)
        )

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: uuid.UUID) -> None:
        await controller.delete_conversation(app.state.user, conversation_id)

    @app.post("/api/conversations/{conversation_id}/fork", status_code=201)
    async def fork_conversation(conversation_id: uuid.UUID, body: ForkRequest) -> Any:
        forked = await controller.fork_conversation(
            app.state.user, conversation_id, at_message=body.at_message
        )
        return as_data(forked)

    # --- turns -----------------------------------------------------------------

    @app.post("/api/turns", status_code=201)
    async def start_conversation(body: StartRequest) -> Any:
        started = await controller.start_conversation(
            app.state.user, agent=body.agent, model=body.model, text=body.text
        )
        return as_data(started)

    @app.post("/api/conversations/{conversation_id}/turns", status_code=201)
    async def send_message(conversation_id: uuid.UUID, body: TurnRequest) -> Any:
        if body.regenerate is not None:
            started = await controller.regenerate_answer(
                app.state.user, conversation_id, question_id=body.regenerate, model=body.model
            )
        elif body.text is not None and body.parent_id is not None:
            started = await controller.send_message(
                app.state.user,
                conversation_id,
                parent_id=body.parent_id,
                model=body.model,
                text=body.text,
            )
        else:
            raise InvalidValueError("a turn is text under a parent, or a regeneration")
        return as_data(started)

    @app.post("/api/conversations/{conversation_id}/cancel", status_code=204)
    async def cancel_turn(conversation_id: uuid.UUID) -> None:
        await controller.cancel_turn(app.state.user, conversation_id)

    @app.get("/api/conversations/{conversation_id}/events")
    async def watch_turn(conversation_id: uuid.UUID, after: int = 0) -> StreamingResponse:
        events = controller.watch_turn(app.state.user, conversation_id, after=after)
        # The refusals happen inside the generator: ask for the first event here, so that
        # they answer with a status rather than a broken stream.
        first = await anext(events, None)

        async def lines() -> AsyncIterator[bytes]:
            if first is None:
                return
            yield json.dumps(as_data(first)).encode() + b"\n"
            async for event in events:
                yield json.dumps(as_data(event)).encode() + b"\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

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
