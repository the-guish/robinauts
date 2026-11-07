# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A checkpoint of this engine's: the Claude Code session that holds the conversation, and the
message its turn ended on. Encoded as one string the caller keeps and never looks inside, so the
engine keeps no index of its own: the transcripts are the state."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from robinauts.agent_engines.contract.domain import CheckpointNotFoundError

SEPARATOR = ":"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    session: str
    """The Claude Code session id, a uuid: where the next turn resumes."""
    message: str
    """The uuid of the assistant message the turn ended on, in that session."""


def encode(checkpoint: Checkpoint) -> str:
    return f"{checkpoint.session}{SEPARATOR}{checkpoint.message}"


def decode(text: str) -> Checkpoint:
    """``CheckpointNotFoundError`` for a string that is not one of this engine's."""
    session, separator, message = text.partition(SEPARATOR)
    try:
        if not separator:
            raise ValueError(text)
        uuid.UUID(session)
        uuid.UUID(message)
    except ValueError:
        raise CheckpointNotFoundError(f"checkpoint {text!r} is not one of this engine's") from None
    return Checkpoint(session, message)
