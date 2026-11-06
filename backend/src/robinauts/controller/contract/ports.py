# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller's ports: the operations of ``docs/architecture/controller.md``, as types; the
records sign-in keeps; and what a worker needs from the core."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Callable, Sequence
from datetime import datetime

from robinauts.controller.contract.domain import (
    AgentListing,
    ApiToken,
    Approval,
    Cursor,
    DeviceDescription,
    DeviceSession,
    Document,
    Identity,
    ModelListing,
    NumberedEvent,
    OpenedSession,
    Pairing,
    PairingState,
    PendingLogin,
    Session,
    SessionPage,
    StoredEvent,
    StoredMessage,
    Task,
    Turn,
    TurnStarted,
    TurnState,
    User,
    UserSession,
)


class Controller(ABC):
    @abstractmethod
    async def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def ensure_user(self, identity: Identity) -> User:
        raise NotImplementedError

    @abstractmethod
    async def list_agents(self) -> tuple[AgentListing, ...]:
        raise NotImplementedError

    @abstractmethod
    async def list_models(self) -> tuple[ModelListing, ...]:
        raise NotImplementedError

    @abstractmethod
    async def list_sessions(
        self, user: User, *, limit: int, cursor: str | None = None
    ) -> SessionPage:
        """``InvalidValueError`` for a limit out of range or a cursor that does not parse."""
        raise NotImplementedError

    @abstractmethod
    async def open_session(self, user: User, session_id: uuid.UUID) -> OpenedSession:
        """``SessionNotFoundError`` for one the user does not have."""
        raise NotImplementedError

    @abstractmethod
    async def rename_session(self, user: User, session_id: uuid.UUID, title: str) -> Session:
        """``SessionNotFoundError``; ``InvalidValueError`` for a title out of bounds."""
        raise NotImplementedError

    @abstractmethod
    async def delete_session(self, user: User, session_id: uuid.UUID) -> None:
        """``SessionNotFoundError``. The engine's state goes with the records."""
        raise NotImplementedError

    @abstractmethod
    async def fork_session(
        self, user: User, session_id: uuid.UUID, *, at_message: uuid.UUID
    ) -> Session:
        """``SessionNotFoundError``; ``MessageNotFoundError`` for a message not on it."""
        raise NotImplementedError

    @abstractmethod
    async def start_session(self, user: User, *, agent: str, model: str, text: str) -> TurnStarted:
        """``UnknownAgentError``, ``UnknownModelError``; ``InvalidValueError`` for empty text."""
        raise NotImplementedError

    @abstractmethod
    async def send_message(
        self,
        user: User,
        session_id: uuid.UUID,
        *,
        parent_id: uuid.UUID,
        model: str,
        text: str,
    ) -> TurnStarted:
        """``SessionNotFoundError``, ``MessageNotFoundError`` for the parent,
        ``UnknownModelError``, ``TurnActiveError`` while a turn runs, ``InvalidValueError``
        for empty text."""
        raise NotImplementedError

    @abstractmethod
    async def edit_message(
        self,
        user: User,
        session_id: uuid.UUID,
        *,
        message_id: uuid.UUID,
        model: str,
        text: str,
    ) -> TurnStarted:
        """A new version of a question: a message under the edited one's parent (none for
        the first question), which the visible thread then goes through.

        ``SessionNotFoundError``, ``MessageNotFoundError`` for a message that is not a
        question of this session, ``UnknownModelError``, ``TurnActiveError`` while a turn
        runs, ``InvalidValueError`` for empty text."""
        raise NotImplementedError

    @abstractmethod
    async def retry_answer(
        self, user: User, session_id: uuid.UUID, *, answer_id: uuid.UUID, model: str
    ) -> TurnStarted:
        """A new answer to the question of an answer that failed, as a regeneration is, with
        the model told about the failure (``docs/specs/ui.md``).

        ``SessionNotFoundError``, ``MessageNotFoundError`` for a message that is not a failed
        answer of this session, ``UnknownModelError``, ``TurnActiveError`` while a turn runs."""
        raise NotImplementedError

    @abstractmethod
    async def resume_answer(
        self, user: User, session_id: uuid.UUID, *, answer_id: uuid.UUID, model: str
    ) -> TurnStarted:
        """The turn of an answer that failed or was interrupted, continued from the rounds its
        engine saved, without the model told anything (``docs/specs/ui.md``). The session's
        latest turn alone: the engine continues the turn it saved last.

        ``SessionNotFoundError``, ``MessageNotFoundError`` for a message that is not the failed
        answer of the session's latest turn, ``UnknownModelError``, ``TurnActiveError`` while a
        turn runs."""
        raise NotImplementedError

    @abstractmethod
    async def regenerate_answer(
        self, user: User, session_id: uuid.UUID, *, question_id: uuid.UUID, model: str
    ) -> TurnStarted:
        """``SessionNotFoundError``, ``MessageNotFoundError`` for a question not on it,
        ``UnknownModelError``, ``TurnActiveError`` while a turn runs."""
        raise NotImplementedError

    @abstractmethod
    async def cancel_turn(
        self, user: User, session_id: uuid.UUID, turn_id: uuid.UUID | None = None
    ) -> None:
        """``SessionNotFoundError``; ``NoActiveTurnError`` when nothing runs. ``turn_id``
        ``None`` names the session's running turn."""
        raise NotImplementedError

    @abstractmethod
    def watch_turn(
        self,
        user: User,
        session_id: uuid.UUID,
        turn_id: uuid.UUID | None = None,
        *,
        after: int = 0,
    ) -> AsyncGenerator[NumberedEvent, None]:
        """Not a coroutine: the refusals happen inside the generator.

        Yields the turn's events past ``after`` until ``TurnEnded``, then stops. ``turn_id``
        ``None`` names the session's latest turn. ``SessionNotFoundError``;
        ``NoActiveTurnError`` for a turn the session does not have.
        """
        raise NotImplementedError

    @abstractmethod
    async def sweep(self) -> None:
        raise NotImplementedError


class Credentials(ABC):
    """Where sign-in keeps its records: user sessions, sign-ins in progress, API tokens,
    device sessions and pairings (``docs/specs/sign-in.md``,
    ``docs/architecture/device-pairing.md``). A secret never reaches it: a record holds the
    SHA-256 hex of one, and a lookup is by that hash. Every id and every time comes from the
    caller, and no clock is read here. Every operation is one statement, so two callers at once
    never both take what is taken once."""

    @abstractmethod
    async def add_user_session(self, session: UserSession) -> None:
        """Its user is one the store has."""
        raise NotImplementedError

    @abstractmethod
    async def resolve_user_session(
        self, secret_hash: str, now: datetime
    ) -> tuple[User, UserSession] | None:
        """The user of the session with that hash and the session, whose ``created_at`` says
        how fresh the sign-in is; ``None`` when there is none or its ``expires_at`` is not
        after ``now``."""
        raise NotImplementedError

    @abstractmethod
    async def delete_user_session(self, secret_hash: str) -> bool:
        """Delete the session with that hash: true when there was one."""
        raise NotImplementedError

    @abstractmethod
    async def add_pending_login(self, login: PendingLogin, now: datetime) -> None:
        """Store it after deleting every pending login whose ``expires_at`` is not after
        ``now``, so the table holds the last ten minutes and no sweep is needed yet."""
        raise NotImplementedError

    @abstractmethod
    async def take_pending_login(self, state_hash: str, now: datetime) -> PendingLogin | None:
        """Delete it and return it, once; ``None`` when it is gone or its ``expires_at`` is not
        after ``now``, and an expired one is deleted either way."""
        raise NotImplementedError

    @abstractmethod
    async def add_api_token(self, token: ApiToken) -> None:
        """Its user is one the store has."""
        raise NotImplementedError

    @abstractmethod
    async def resolve_api_token(self, secret_hash: str, now: datetime) -> User | None:
        """The owner of the token with that hash, or ``None`` when there is none or its
        ``expires_at`` is not after ``now``."""
        raise NotImplementedError

    @abstractmethod
    async def api_tokens_of(self, user_id: uuid.UUID) -> list[ApiToken]:
        """The user's tokens, oldest first, ties broken by id."""
        raise NotImplementedError

    @abstractmethod
    async def delete_api_token(self, user_id: uuid.UUID, token_id: uuid.UUID) -> bool:
        """Only the owner's: true when that user had that token."""
        raise NotImplementedError

    # --- pairings -----------------------------------------------------------

    @abstractmethod
    async def add_pairing(self, pairing: Pairing, now: datetime, *, at_most: int) -> bool:
        """Store a waiting pairing, after deleting every pairing of its user whose
        ``expires_at`` is not after ``now``; unless the user already has ``at_most`` that are
        not expired, in which case nothing is stored and the answer is false. The cap bounds
        the table: two mints at once may both pass it."""
        raise NotImplementedError

    @abstractmethod
    async def claim_pairing(
        self,
        code_hash: str,
        claim_hash: str,
        number: str,
        device: DeviceDescription,
        now: datetime,
        new_expires_at: datetime,
    ) -> Pairing | None:
        """The pairing with that code, moved from ``waiting`` to ``claimed`` with the claim
        secret's hash, the number, the device's description and ``new_expires_at``; ``None``
        when there is none, it is not waiting, or its ``expires_at`` is not after ``now``. Of
        two claims at once, one gets it."""
        raise NotImplementedError

    @abstractmethod
    async def pairing_of(self, user_id: uuid.UUID, pairing_id: uuid.UUID) -> Pairing | None:
        """Only the owner's, expired or not: the caller judges the expiry."""
        raise NotImplementedError

    @abstractmethod
    async def approve_pairing(
        self,
        user_id: uuid.UUID,
        pairing_id: uuid.UUID,
        number: str,
        *,
        device_id: uuid.UUID,
        device_expires_at: datetime,
        now: datetime,
    ) -> tuple[Approval, DeviceSession | None]:
        """Decide the owner's claimed pairing by the number its phone shows.

        The right number, on a claimed pairing whose ``expires_at`` is after ``now``, inserts a
        device session of that id, created and last used at ``now``, expiring at
        ``device_expires_at``, with the pairing's device description and its claim hash as
        the secret hash; clears the claim hash, so the hash moves rather than copies; marks
        the pairing approved; and answers it. A wrong number counts a wrong try, and the
        ``PAIRING_WRONG_TRIES``-th deletes the pairing, which then answers ``GONE``. A pairing
        that is waiting or approved is ``NOT_CLAIMED``; one that is unknown, another's or
        expired is ``GONE``. Of two approves at once, one decides first and the other sees
        what it did."""
        raise NotImplementedError

    @abstractmethod
    async def delete_pairing(self, user_id: uuid.UUID, pairing_id: uuid.UUID) -> bool:
        """Only the owner's: true when that user had it. An approved one goes and its device
        stays."""
        raise NotImplementedError

    @abstractmethod
    async def resolve_claim(self, claim_hash: str, now: datetime) -> PairingState | None:
        """The state of the pairing whose claim has that hash, for the phone's poll; ``None``
        when there is none, as after the approve moved the hash, or its ``expires_at`` is not
        after ``now``."""
        raise NotImplementedError

    # --- device sessions ----------------------------------------------------

    @abstractmethod
    async def add_device_session(self, device: DeviceSession) -> None:
        """Its user is one the store has."""
        raise NotImplementedError

    @abstractmethod
    async def resolve_device_session(
        self, secret_hash: str, now: datetime
    ) -> tuple[User, DeviceSession] | None:
        """The user of the device with that hash and the device, or ``None`` when there is
        none or its ``expires_at`` is not after ``now``. Its ``last_used_at`` becomes ``now``
        when it is ``LAST_USED_EVERY`` old or older, in the same statement, and the device
        answered carries what was written."""
        raise NotImplementedError

    @abstractmethod
    async def device_sessions_of(self, user_id: uuid.UUID) -> list[DeviceSession]:
        """The user's devices, oldest first, ties broken by id."""
        raise NotImplementedError

    @abstractmethod
    async def delete_device_session(self, user_id: uuid.UUID, device_id: uuid.UUID) -> bool:
        """Only the owner's: true when that user had that device."""
        raise NotImplementedError

    @abstractmethod
    async def all_device_sessions(self) -> list[tuple[User, DeviceSession]]:
        """Every device of the deployment with its user, oldest first, ties broken by id: what
        the operator's command lists."""
        raise NotImplementedError

    @abstractmethod
    async def users_with_email(self, email: str) -> list[User]:
        """The users whose verified email is that one, compared case-insensitively, oldest
        first: how the operator names a person."""
        raise NotImplementedError

    @abstractmethod
    async def delete_device_sessions(
        self,
        *,
        user_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        every: bool = False,
    ) -> list[DeviceSession]:
        """Delete one user's devices, one device whoever's it is, or every device, and answer
        what was deleted, oldest first. Exactly one of the three is given, else
        ``ValueError``. The operator's: the routes delete by owner."""
        raise NotImplementedError

    # --- every credential of a person ---------------------------------------

    @abstractmethod
    async def delete_credentials_of(self, user_id: uuid.UUID) -> int:
        """Delete the user's sessions, devices, tokens and pairings at once, and answer how
        many rows went: what a failed allow-list check on use does."""
        raise NotImplementedError

    @abstractmethod
    async def delete_credentials_of_identity(self, provider: str, subject: str) -> int:
        """As ``delete_credentials_of``, for the user of that identity, if there is one: what
        an allow-list refusal at sign-in does."""
        raise NotImplementedError


class TurnBroker(ABC):
    """What a worker needs from the core, and all it reaches of it: the tasks it claims, renews
    and is asked to stop, the turn each names, the session and the messages a turn reads, and
    the events and the end it writes (``docs/architecture/controller.md``, "Worker").

    The operations are the store's of the same names, with the same refusals:
    ``SessionNotFoundError`` for a session missing, hidden or another owner's, and
    ``TurnLostError`` for a write on a turn the worker no longer holds. Every time comes from
    the worker's clock. A worker in this process has it over the controller's store; one in a
    process of its own, over HTTP, from the server that spawned it (``web/worker_api.py``).
    """

    @abstractmethod
    async def claim_tasks(self, now: datetime, until: datetime, limit: int) -> list[Task]:
        """Up to ``limit`` queued tasks, made running, with their lease until ``until``. Each
        task is claimed once, whichever worker asks."""
        raise NotImplementedError

    @abstractmethod
    async def wait_for_queued(self, now: datetime, timeout: float) -> bool:
        """Wait until a task ``claim_tasks`` would claim is queued, or ``timeout`` seconds
        have passed: true in the first case, false in the second."""
        raise NotImplementedError

    @abstractmethod
    async def renew_leases(
        self, tasks: Sequence[uuid.UUID], now: datetime, until: datetime
    ) -> list[uuid.UUID]:
        """Each of those tasks that is running, with its lease not passed ``now``, holds it
        until ``until``. The ones asked to stop, which a stop missed."""
        raise NotImplementedError

    @abstractmethod
    async def listen_for_cancels(self, stop: Callable[[uuid.UUID], object]) -> None:
        """From then on, call ``stop`` with each task asked to stop, from any process, until
        ``close``. A stop that goes astray is caught by the next ``renew_leases``."""
        raise NotImplementedError

    @abstractmethod
    async def get_task(self, task: uuid.UUID) -> Task | None:
        raise NotImplementedError

    @abstractmethod
    async def find_turn(self, turn: uuid.UUID) -> tuple[uuid.UUID, Turn] | None:
        """The turn of a visible session, by its id alone, with the session's owner: what a
        task names."""
        raise NotImplementedError

    @abstractmethod
    async def get_session(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        raise NotImplementedError

    @abstractmethod
    async def sessions_of(
        self, owner: uuid.UUID, limit: int, before: Cursor | None
    ) -> list[Session]:
        """A page of the owner's visible sessions, most recently updated first, past
        ``before``: what a built-in tool reads."""
        raise NotImplementedError

    @abstractmethod
    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        """Every message of the session, oldest first."""
        raise NotImplementedError

    @abstractmethod
    async def append_event(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        position: int,
        document: Document,
        written_at: datetime,
        expires_at: datetime,
    ) -> None:
        """The same document again at a position it has is accepted; anything else there, or
        on a turn the worker does not hold, is ``TurnLostError``."""
        raise NotImplementedError

    @abstractmethod
    async def finish_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        state: TurnState,
        ended_at: datetime,
        error: str | None,
        answer: StoredMessage | None,
        events: Sequence[StoredEvent],
        updated_at: datetime,
    ) -> None:
        """The answer, the last events, the turn's state and its task done, in one operation,
        only while the worker holds the turn: else ``TurnLostError``."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Stop listening for cancels, and release what reaching the core took."""
        raise NotImplementedError
