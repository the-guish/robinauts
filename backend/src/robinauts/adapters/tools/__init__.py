# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The adapters that reach tool servers (``docs/specs/agents.md``, "Tools").

One so far, ``mcp``: the ``ToolServers`` port over remote MCP servers. It is
a sub-package of its own, as each agent framework's adapter is, so that the
import rule confining the protocol's SDK to it can be written for the
directory (``backend/pyproject.toml``, ``docs/layout.md``).
"""
