# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Which platform thread is which Robinauts conversation.

The deployment names a new conversation in the ``X-Robinauts-Conversation-Id`` header of the
stream that started it, and the bridge names it in every later turn of the same person in the same
thread. A link is per person and per thread, since a conversation is private to its owner: in a
thread two people write in, each has their own.

Losing the links loses nothing but the links: the thread's next message starts a new conversation,
and the old one stays in the deployment. Kept in a file for now; the deployment's store is the
better home, so that two replicas or a lost volume do not split a thread.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from robinauts_connectors.domain import Incoming


def link_key(message: Incoming) -> str:
    """The person and the thread, as one key: ``<person id>@<platform>:<thread key>``."""
    raise NotImplementedError


class ThreadLinks(ABC):
    @abstractmethod
    def get(self, key: str) -> str | None:
        """The conversation linked to ``key``, or ``None``."""
        raise NotImplementedError

    @abstractmethod
    def set(self, key: str, conversation: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        """Forget a link the deployment no longer answers to: the conversation was deleted, or is
        another agent's since the process was last configured."""
        raise NotImplementedError


class LinkFile(ThreadLinks):
    """The links in one JSON object, read once and written whole, then renamed over the last
    good file, so that a crash half way loses nothing that was saved."""

    def __init__(self, path: Path) -> None:
        raise NotImplementedError

    def get(self, key: str) -> str | None:
        raise NotImplementedError

    def set(self, key: str, conversation: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError
