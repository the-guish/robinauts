# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine's state: each conversation's finished turns, as a tree, in one JSON file per
session under the state folder.

NeMo Fabric's Claude adapter keeps a conversation's memory in the Claude Code process of a live
runtime, and can neither resume one after the process ends nor fork it at an earlier message. So
the engine keeps what was asked and answered itself: a checkpoint is the id of a turn here, its
chain of parents is the conversation up to it, and a runtime started at a checkpoint is seeded
with that chain. Each turn also keeps the events it streamed, so that a resume of a turn that
had finished gives the same events again.

Written whole and replaced at once, so a turn is either there or not; one turn at a time per
session, as the caller promises.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robinauts.agent_engines.contract.domain import (
    CheckpointNotFoundError,
    SessionExistsError,
    SessionNotFoundError,
)


@dataclass(frozen=True, slots=True)
class Turn:
    id: str
    parent: str | None
    prompt: str
    answer: str
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    """What the turn streamed, encoded (``events.encode``)."""


@dataclass(slots=True)
class Session:
    turns: dict[str, Turn]
    latest: str | None
    """The turn saved last: the one a resume continues."""

    def chain(self, checkpoint_id: str | None) -> list[Turn]:
        """The turns from the conversation's first to the checkpoint's, oldest first; none for
        no checkpoint. ``CheckpointNotFoundError`` for a turn this session does not hold."""
        chain: list[Turn] = []
        current = checkpoint_id
        while current is not None:
            turn = self.turns.get(current)
            if turn is None or len(chain) > len(self.turns):
                raise CheckpointNotFoundError(f"checkpoint {checkpoint_id!r} is not this session's")
            chain.append(turn)
            current = turn.parent
        chain.reverse()
        return chain


class TurnLog:
    def __init__(self, folder: Path) -> None:
        self._folder = folder

    def setup(self) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: uuid.UUID) -> Path:
        return self._folder / f"{session_id}.json"

    def exists(self, session_id: uuid.UUID) -> bool:
        return self.path(session_id).is_file()

    def create(self, session_id: uuid.UUID) -> None:
        if self.exists(session_id):
            raise SessionExistsError(f"session {session_id} exists")
        self._write(session_id, Session({}, None))

    def load(self, session_id: uuid.UUID) -> Session:
        try:
            data = json.loads(self.path(session_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SessionNotFoundError(f"session {session_id} was not created") from None
        turns = {
            turn_id: Turn(
                turn_id,
                turn.get("parent"),
                turn["prompt"],
                turn["answer"],
                tuple(turn.get("events", ())),
            )
            for turn_id, turn in data.get("turns", {}).items()
        }
        return Session(turns, data.get("latest"))

    def add(
        self,
        session_id: uuid.UUID,
        parent: str | None,
        prompt: str,
        answer: str,
        events: list[dict[str, Any]],
    ) -> Turn:
        """Save a finished turn under a new id, as the session's latest."""
        session = self.load(session_id)
        turn = Turn(str(uuid.uuid4()), parent, prompt, answer, tuple(events))
        session.turns[turn.id] = turn
        session.latest = turn.id
        self._write(session_id, session)
        return turn

    def forget(self, session_id: uuid.UUID) -> None:
        self.path(session_id).unlink(missing_ok=True)

    def _write(self, session_id: uuid.UUID, session: Session) -> None:
        data = {
            "latest": session.latest,
            "turns": {
                turn.id: {
                    "parent": turn.parent,
                    "prompt": turn.prompt,
                    "answer": turn.answer,
                    "events": list(turn.events),
                }
                for turn in session.turns.values()
            },
        }
        target = self.path(session_id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, target)


SEED_OPENING = (
    "This conversation began before this session of yours, which starts with no memory of it."
    " What was said so far, oldest first; the next message is the person's new question."
)


def seed(chain: list[Turn]) -> str:
    """The conversation so far, as text for the system prompt of a runtime that starts at a
    checkpoint: what each turn asked and answered. Its tool calls are not carried over; their
    effects on the workspace are there."""
    if not chain:
        return ""
    parts = [SEED_OPENING]
    for turn in chain:
        parts.append(f"<user>\n{turn.prompt}\n</user>\n<assistant>\n{turn.answer}\n</assistant>")
    return "\n\n".join(parts)
