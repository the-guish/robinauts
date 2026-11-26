# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A checkpoint of this engine's: the OpenCode session that holds the conversation, and the last
assistant message of the turn the answer ended. Encoded as one string the caller keeps and never
looks inside, so the engine keeps no index of messages: OpenCode's sessions are the state."""

from __future__ import annotations

import re
from dataclasses import dataclass

from robinauts.agent_engines.contract.domain import CheckpointNotFoundError

SEPARATOR = ":"
_SESSION = re.compile(r"ses_[A-Za-z0-9]+")
_MESSAGE = re.compile(r"msg_[A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    session: str
    """The OpenCode session id: where the next turn continues, or what it forks."""
    message: str
    """The id of the last assistant message of the turn, in that session."""


def encode(checkpoint: Checkpoint) -> str:
    return f"{checkpoint.session}{SEPARATOR}{checkpoint.message}"


def decode(text: str) -> Checkpoint:
    """``CheckpointNotFoundError`` for a string that is not one of this engine's."""
    session, separator, message = text.partition(SEPARATOR)
    if not separator or not _SESSION.fullmatch(session) or not _MESSAGE.fullmatch(message):
        raise CheckpointNotFoundError(f"checkpoint {text!r} is not one of this engine's")
    return Checkpoint(session, message)
