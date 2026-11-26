# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A checkpoint of this engine's: the Codex thread that holds the conversation, and the turn of
it the answer ended. Encoded as one string the caller keeps and never looks inside, so the
engine keeps no index of turns: Codex's threads are the state."""

from __future__ import annotations

import re
from dataclasses import dataclass

from robinauts.agent_engines.contract.domain import CheckpointNotFoundError

SEPARATOR = ":"
_ID = re.compile(r"[A-Za-z0-9_-]+")
"""What a Codex thread or turn id is made of: UUIDv7s today, opaque to this engine."""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    thread: str
    """The Codex thread id: where the next turn continues, or what it forks."""
    turn: str
    """The id of the turn the answer ended, in that thread."""


def encode(checkpoint: Checkpoint) -> str:
    return f"{checkpoint.thread}{SEPARATOR}{checkpoint.turn}"


def decode(text: str) -> Checkpoint:
    """``CheckpointNotFoundError`` for a string that is not one of this engine's."""
    thread, separator, turn = text.partition(SEPARATOR)
    if not separator or not _ID.fullmatch(thread) or not _ID.fullmatch(turn):
        raise CheckpointNotFoundError(f"checkpoint {text!r} is not one of this engine's")
    return Checkpoint(thread, turn)
