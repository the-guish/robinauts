# MCP servers

Tool servers to try Robinauts with. Each is a folder here and a project of its
own. The platform does not need any of them: an operator configures tool servers
in `[tool_servers.<id>]` like any other
([docs/specs/operations.md](../docs/specs/operations.md)).

What they have in common:

- **It stands alone.** It imports nothing of `backend/`, and the backend names
  none of them. It speaks MCP over streamable HTTP, like any tool server.
- **It uses the MCP SDK the backend already has**, at the version
  `backend/uv.lock` pins. Its other dependencies come with the SDK.
- **It is checked like the rest of the repository**: SPDX headers on every
  file, its lock under the licence gate (`scripts/check-licences.sh`), ruff and
  black under the backend's rules, and its own tests
  (`scripts/check-mcp-servers.sh`).

The servers:

| folder | what it serves | state |
| --- | --- | --- |
| [to-do-list/](to-do-list/README.md) | a to-do list: four tools and a view for the side panel (MCP Apps) | one JSON file |
