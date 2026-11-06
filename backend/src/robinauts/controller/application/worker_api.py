# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker API: what the server answers a sandbox worker, over the store.

A sandbox runs code its agent wrote, and the agent may have been turned by what it read, so
everything a sandbox worker sends is checked here before the store sees it
(``docs/architecture/sandbox-workers.md``, "What the server checks"):

- **scope**: a sandbox reaches the turns of the sessions it holds, which a claim bound to it,
  and nothing else; a turn of another session is not found;
- **what it may write**: the events of an answer, in order, from the position after the last
  one stored; the end of the turn and the answer itself only with the finish, an assistant's
  answer to the turn's question, of the turn's session and naming the turn;
- **form**: every document is decoded with the data model's decoders and encoded again, so
  that no key the schemas do not know is kept;
- **size**: each document and each call has a limit.

Every time is the server's: a worker's clock decides no lease and dates no record.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from robinauts.controller.application.turns import RETENTION
from robinauts.controller.contract.domain import (
    RUN_TURN,
    Config,
    InvalidValueError,
    LoadedTurn,
    Message,
    MessageStarted,
    Role,
    SandboxConfig,
    Session,
    SessionNotFoundError,
    Task,
    Turn,
    TurnState,
    WorkerRefusedError,
)
from robinauts.controller.contract.ports import WorkerApi
from robinauts.controller.core.documents import (
    clean_text,
    event_from_document,
    event_to_document,
    message_from_document,
    stored_message,
)
from robinauts.controller.core.engine_settings import SecretLookup
from robinauts.controller.ports.store import Store, StoredEvent

MAX_DOCUMENT_BYTES = 1024 * 1024
"""The largest event a worker may send, as JSON: a tool's result is the large one."""

MAX_ANSWER_BYTES = 8 * 1024 * 1024
"""The largest answer, which carries every tool call and result of the turn."""

MAX_EVENTS_PER_CALL = 1000

MAX_ERROR_CHARACTERS = 4000

STREAMED = frozenset(
    {
        "message_started",
        "text_piece",
        "reasoning_piece",
        "call_started",
        "arguments_piece",
        "call_completed",
        "result_landed",
    }
)
"""The kinds a worker appends while a turn runs. The end of a message and of the turn come
with the finish, which stores the answer in the same operation."""

LAST = frozenset({"message_completed", "turn_ended"})
"""The kinds a finish may carry."""

ENDED = frozenset(TurnState) - {TurnState.ACTIVE}

SAYS_WHY = frozenset({TurnState.FAILED, TurnState.INTERRUPTED})
"""The states an error goes with: the turn's record keeps one for no other."""


def _size(document: Mapping[str, Any]) -> int:
    return len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode())


class SandboxWorkerApi(WorkerApi):
    def __init__(
        self,
        config: Config,
        *,
        store: Store,
        secret_for: SecretLookup,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._sandbox = config.sandbox or SandboxConfig()
        self._store = store
        self._secret_for = secret_for
        self._now = now or (lambda: datetime.now(UTC))

    # --- who asks ----------------------------------------------------------------

    def accepts(self, token: str) -> bool:
        if self._config.sandbox is None:
            return False
        expected = self._secret_for(self._sandbox.token_env)
        if not expected or not token:
            return False
        # Equal lengths, whatever was sent: the comparison takes the same time either way.
        return hmac.compare_digest(
            hashlib.sha256(token.encode()).digest(), hashlib.sha256(expected.encode()).digest()
        )

    async def _scoped(self, sandbox: uuid.UUID, turn: uuid.UUID) -> tuple[Session, Turn]:
        """The turn and its session, when the session is sandboxed and that sandbox's."""
        found = await self._store.find_turn(turn)
        if found is not None:
            owner, held = found
            try:
                session = await self._store.get_session(owner, held.session_id)
            except SessionNotFoundError:
                session = None
            if session is not None and session.sandboxed and session.sandbox_id == sandbox:
                return session, held
        raise SessionNotFoundError(f"no turn {turn} of this sandbox")

    # --- the calls -----------------------------------------------------------------

    async def claim(self, sandbox: uuid.UUID, wait: float) -> Task | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(wait, self._sandbox.long_poll_seconds))
        while True:
            now = self._now()
            await self._store.hear_from_sandbox(sandbox, now)
            until = now + timedelta(seconds=self._config.work.lease_seconds)
            claimed = await self._store.claim_tasks(now, until, 1, sandbox)
            if claimed:
                return claimed[0]
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            await self._store.wait_for_queued(now, remaining, sandbox)

    async def load_turn(self, sandbox: uuid.UUID, turn: uuid.UUID) -> LoadedTurn:
        session, held = await self._scoped(sandbox, turn)
        messages = await self._store.messages_of(session.owner_id, session.id)
        return LoadedTurn(held, session, tuple(messages))

    async def append_events(
        self, sandbox: uuid.UUID, turn: uuid.UUID, events: Sequence[Mapping[str, Any]]
    ) -> None:
        session, held = await self._scoped(sandbox, turn)
        checked = [self._event(held, document, STREAMED) for document in self._bounded(events)]
        await self._follows_on(session, held, [position for position, _ in checked])
        for position, document in checked:
            now = self._now()
            await self._store.append_event(
                session.owner_id, session.id, held.id, position, document, now, now + RETENTION
            )

    async def renew(self, sandbox: uuid.UUID, tasks: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        now = self._now()
        await self._store.hear_from_sandbox(sandbox, now)
        own = [task for task in tasks[:MAX_EVENTS_PER_CALL] if await self._runs(sandbox, task)]
        if not own:
            return []
        until = now + timedelta(seconds=self._config.work.lease_seconds)
        return await self._store.renew_leases(own, now, until)

    async def finish(
        self,
        sandbox: uuid.UUID,
        turn: uuid.UUID,
        state: TurnState,
        error: str | None,
        answer: Mapping[str, Any] | None,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        session, held = await self._scoped(sandbox, turn)
        if state not in ENDED:
            raise WorkerRefusedError(f"a turn does not end as {state.value!r}")
        if error is not None:
            if state not in SAYS_WHY:
                raise WorkerRefusedError(f"a turn that ends as {state.value!r} says no error")
            error = clean_text(error)[:MAX_ERROR_CHARACTERS]
        checked = [self._event(held, document, LAST) for document in self._bounded(events)]
        ends = [e for _, e in checked if e["kind"] == "turn_ended"]
        if ends and (ends != [checked[-1][1]] or ends[0]["state"] != state.value):
            raise WorkerRefusedError("a turn_ended event comes last, with the turn's state")
        await self._follows_on(session, held, [position for position, _ in checked])
        stored = None if answer is None else stored_message(self._answer(session, held, answer))
        now = self._now()
        await self._store.finish_turn(
            session.owner_id,
            session.id,
            held.id,
            state,
            now,
            error,
            stored,
            [StoredEvent(position, document, now + RETENTION) for position, document in checked],
            now,
        )

    # --- the checks ----------------------------------------------------------------

    def _bounded(self, events: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
        if len(events) > MAX_EVENTS_PER_CALL:
            raise WorkerRefusedError(f"more than {MAX_EVENTS_PER_CALL} events in one call")
        return events

    def _event(
        self, turn: Turn, document: Mapping[str, Any], kinds: frozenset[str]
    ) -> tuple[int, dict[str, Any]]:
        """The event decoded and encoded again, at its position, when it is of ``kinds`` and
        of this turn."""
        if not isinstance(document, Mapping):
            raise WorkerRefusedError("an event is not a document")
        if _size(document) > MAX_DOCUMENT_BYTES:
            raise WorkerRefusedError(f"an event over {MAX_DOCUMENT_BYTES} bytes")
        try:
            numbered = event_from_document(document)
        except InvalidValueError as refused:
            raise WorkerRefusedError(str(refused)) from refused
        if document.get("turn_id") != str(turn.id):
            raise WorkerRefusedError("an event of another turn")
        kind = document["kind"]
        if kind not in kinds:
            raise WorkerRefusedError(f"a {kind} event, which this call does not take")
        event = numbered.event
        if isinstance(event, MessageStarted) and (
            event.role is not Role.ASSISTANT or event.parent_id != turn.follows
        ):
            raise WorkerRefusedError("a message started that is not the answer to the turn")
        return numbered.position, event_to_document(turn.id, numbered.position, event)

    async def _follows_on(self, session: Session, turn: Turn, positions: list[int]) -> None:
        """Positions in order, the first one after the last stored or one stored already."""
        if not positions:
            return
        if positions != list(range(positions[0], positions[0] + len(positions))):
            raise WorkerRefusedError("events out of order")
        first = positions[0]
        if first == 1:
            return
        before = await self._store.events_after(session.owner_id, session.id, turn.id, first - 2)
        if not before or before[0][0] != first - 1:
            raise WorkerRefusedError(f"an event at {first} with none at {first - 1}")

    def _answer(self, session: Session, turn: Turn, document: Mapping[str, Any]) -> Message:
        if _size(document) > MAX_ANSWER_BYTES:
            raise WorkerRefusedError(f"an answer over {MAX_ANSWER_BYTES} bytes")
        try:
            message = message_from_document(document)
        except InvalidValueError as refused:
            raise WorkerRefusedError(str(refused)) from refused
        if (
            message.role is not Role.ASSISTANT
            or message.session_id != session.id
            or message.parent_id != turn.follows
            or message.turn_id != turn.id
        ):
            raise WorkerRefusedError("an answer that is not the assistant's to this turn")
        return message

    async def _runs(self, sandbox: uuid.UUID, task: uuid.UUID) -> bool:
        """Whether the task is a turn of a session that sandbox holds."""
        found = await self._store.get_task(task)
        if found is None or not found.sandboxed or found.name != RUN_TURN:
            return False
        try:
            await self._scoped(sandbox, uuid.UUID(found.payload["turn_id"]))
        except (SessionNotFoundError, KeyError, ValueError):
            return False
        return True
