# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tool servers an agent may use, as the application reaches them.

Two questions, both of one server (``domain.ToolServerConfig``): what tools it
offers, and what one of them answers. The application asks the first once per
run, of every server the agent names, and hands the named and sorted list to
the agent port for the whole turn; it asks the second for every call the model
makes, in parallel, and stores the answers as one tool message
(``docs/specs/agents.md``, "Tools"; ``docs/specs/runs.md``, "Tools").

**The names are the server's.** A tool is listed under the name the server
gave it and called by that name: the platform's ``<prefix>__<name>`` is put on
by ``robinauts.core.tools`` above this port and taken off before a call comes
through it, so that an implementation knows nothing of prefixes.

**A tool's error is a result, not a failure.** A server answering that the
call failed, or a call that ran out of the server's ``timeout_seconds``, comes
back as a ``ToolResult`` marked as an error, and the model is told. Only a
server that cannot be reached at all -- or that will not list its tools -- is
a ``ToolServerError``, which fails the run, naming the server. Nothing an
implementation raises or returns carries the credential it sent.

**The credential is the implementation's.** An implementation is built with
what start-up read (``robinauts.adapters.config_file.ToolServerSecrets``) and
looks the server's up by id; nothing above the port holds a secret.

The contract suite an implementation is held to is
``backend/tests/contracts/tool_servers.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

from robinauts.domain import ListedTool, ToolResult, ToolServerConfig


class ToolServers(ABC):
    """What a deployment can ask of the tool servers it configured."""

    @abstractmethod
    async def list_tools(self, server: ToolServerConfig) -> Sequence[ListedTool]:
        """The tools that server offers, under its own names, in its own order.

        Raises ``ToolServerError`` when the server cannot be reached, refuses
        the credential or does not answer the question, naming the server.
        A tool the server lists in a shape this build does not carry is left
        out here rather than failing the list.
        """
        raise NotImplementedError

    @abstractmethod
    async def call_tool(
        self, server: ToolServerConfig, name: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """Call that tool of that server with those arguments, and say what came back.

        ``name`` is the server's own name for the tool. A server that says the
        call failed, one that does not answer inside its ``timeout_seconds``,
        and one that does not know the tool all come back as a ``ToolResult``
        marked as an error whose text says so and names the tool where the
        tool is what went wrong; ``ToolServerError`` is for a
        server that cannot be reached at all. ``CancelledError`` is let
        through, and what the call holds is released on the way out.
        """
        raise NotImplementedError
