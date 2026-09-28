# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The MCP adapter: the one ``ToolServers`` implementation, and the only place MCP is spoken.

A client of our own over ``httpx`` (``docs/layout.md``): the MCP Python SDK's
tree fails the licence gate (``DEPENDENCIES.md``, "Known exclusions"), and the
import contract that would confine the SDK here is written all the same, for
the day its tree passes. Importing this imports nothing beyond what the
adapters already have.
"""

from robinauts.adapters.tools.mcp.client import (
    CLIENT_INFO,
    CONNECT_TIMEOUT_SECONDS,
    MAX_PAGES,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    QUIET_CLIENT_LEVEL,
    QUIET_CLIENT_LOGGERS,
    READ_TIMEOUT_SECONDS,
    SUPPORTED_VERSIONS,
    McpToolServers,
    open_client,
    quiet_client_logging,
)

__all__ = [
    "CLIENT_INFO",
    "CONNECT_TIMEOUT_SECONDS",
    "MAX_PAGES",
    "MAX_RESPONSE_BYTES",
    "PROTOCOL_VERSION",
    "QUIET_CLIENT_LEVEL",
    "QUIET_CLIENT_LOGGERS",
    "READ_TIMEOUT_SECONDS",
    "SUPPORTED_VERSIONS",
    "McpToolServers",
    "open_client",
    "quiet_client_logging",
]
