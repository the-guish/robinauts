# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Remote MCP servers over Streamable HTTP: the one place the protocol is spoken.

The adapter itself arrives with the port it implements
(``docs/working-notes/mcp-plan.md``, step 5). What is here already is the
sub-package and the rule about it: **the MCP Python SDK, if it is ever
adopted, is imported here and nowhere else** (``backend/pyproject.toml``).
It is not adopted today. Its dependency tree fails the licence gate --
``cffi`` states ``MIT-0``, which is on no list of ``DEPENDENCIES.md``, and
``pywin32`` states a licence family and no licence -- so this adapter is a
client of our own over ``httpx``, for the three calls a client needs:
``initialize``, ``tools/list`` and ``tools/call`` (``DEPENDENCIES.md``,
"Known exclusions"; ``docs/specs/agents.md``, "Tools").
"""
