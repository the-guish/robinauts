# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""One real listing and one real call against a real MCP server. Run by hand.

Everything else about the adapter is proved without a network
(``tests/unit/test_mcp_adapter.py``); what only this can show is that the
client speaks the protocol a server on the internet speaks -- the session id,
the event-stream answer, the revision -- and that a real listing and a real
result map onto the platform's records. Microsoft Learn's server is public
and takes no credential, so this costs nothing but a round trip; it is still
not collected by a plain ``pytest`` run (``tests/live`` is in
``norecursedirs``) and skips unless asked for::

    ROBINAUTS_LIVE_MCP=1 uv run pytest tests/live/test_mcp_live.py

**A server that takes no credential is sent none**: ``auth = "none"``, which
names no variable, and the adapter sends no ``Authorization`` header for it
(``docs/specs/agents.md``, "Tools"). The question step 5c left open is
answered in step 8 of the plan.
"""

from __future__ import annotations

import os

import pytest

from aio import asyncio_test
from robinauts.adapters import ToolServerSecrets
from robinauts.adapters.tools.mcp import McpToolServers
from robinauts.core import named_tools
from robinauts.domain import ToolServerAuth, ToolServerConfig

SWITCH = "ROBINAUTS_LIVE_MCP"
ENDPOINT = "https://learn.microsoft.com/api/mcp"
SEARCH = "microsoft_docs_search"
"""The tool the server documents first; its one argument is ``query``."""

LEARN = ToolServerConfig(id="learn", url=ENDPOINT, auth=ToolServerAuth.NONE)


@pytest.mark.skipif(not os.environ.get(SWITCH), reason=f"set {SWITCH}=1 to run this")
@asyncio_test
async def test_one_real_listing_and_one_real_call_against_microsoft_learn() -> None:
    servers = McpToolServers(ToolServerSecrets({}))
    try:
        listed = await servers.list_tools(LEARN)
        result = await servers.call_tool(LEARN, SEARCH, {"query": "Azure Functions triggers"})
    finally:
        await servers.aclose()

    names = [tool.name for tool in listed]
    assert SEARCH in names, names
    # What the run would be handed: named, sorted, nothing left out.
    kept, left_out = named_tools(LEARN, listed)
    assert [tool.name for tool in kept][: len(names)] and left_out == ()
    assert any(tool.name == f"learn__{SEARCH}" for tool in kept)
    assert not result.is_error, result.text[:200]
    assert "Azure" in result.text
