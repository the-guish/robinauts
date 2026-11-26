# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The configured tool servers, as a coding agent's own MCP client reaches them: each over HTTP
with the header its table's ``auth`` implies, and the tools its ``exclude`` leaves out.

An agent's CLI (Codex, OpenCode) holds its own MCP client, so the worker hands it each server's
address and header, and the names of the tools it may not call. The secret is asked for when
the header is built, and goes nowhere but into what the CLI is handed.
"""

from __future__ import annotations

import base64

from robinauts.agent_engines.contract.domain import ToolServerAuth, ToolServerConfig
from robinauts.agent_engines.contract.ports import ToolSecretLookup


def headers_for(table: ToolServerConfig, secrets: ToolSecretLookup) -> dict[str, str]:
    """The HTTP headers a request to the server carries: its credential, or none."""
    if table.auth is ToolServerAuth.BEARER:
        return {"Authorization": f"Bearer {secrets.secret_for(table.id)}"}
    if table.auth is ToolServerAuth.BASIC:
        pair = f"{table.user}:{secrets.secret_for(table.id)}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode()}
    if table.auth is ToolServerAuth.HEADER:
        return {table.header: secrets.secret_for(table.id)}
    return {}
