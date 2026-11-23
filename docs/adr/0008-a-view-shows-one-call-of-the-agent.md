# ADR 0008 — A view shows one call of the agent

- Status: accepted. It supersedes
  [ADR 0007](0007-mcp-apps-in-the-side-panel.md). How a view is isolated and
  how its tool calls are relayed stand as 0007 decided them. What a view
  shows, and who calls its tool, changed.
- Date: 2026-11-23

## Context

MCP Apps (`io.modelcontextprotocol/ui`, SEP-1865, version `2026-01-26`) ties
a view to one tool call. The host tells the view the arguments of that call
and its result.

Under ADR 0007 the panel called an app's tool itself, with no arguments. It
called it again after every turn, and after each tool of the app's server
that answered in the chat.

- **The agent could not choose what the panel showed.** A tab showed
  whatever its tool answered with no arguments.
- **A view was told no arguments.** A server could keep one thing for
  everyone, and no more.
- **The protocol was the panel's own.** A view written for the standard
  expects the call it belongs to.

[architecture/mcp-apps.md](../architecture/mcp-apps.md) describes the system
as it is now.

## Decision

**A view shows one call of the agent. The panel tells the view that call's
arguments and its result, and calls no tool of its own.**

- **The result comes from the transcript.** The engine reports a result's
  structured content beside its text, the transcript keeps both, and the
  panel reads one call's result from the backend. A reopened conversation
  shows the same result, on any device.
- **Why not call the tool again with the agent's arguments.** A second call
  repeats the tool's side effects, and may answer something else. The agent
  would have read one result and the person would see another.
- **A tab is one view of one server, and a sub-tab is one call.** A call with
  the same tool and arguments uses the sub-tab of the earlier call. The
  standard gives each call a view of its own. A model often shows the same
  thing again, and a strip of identical sub-tabs helps nobody.
- **A view keeps its data fresh by polling.** It calls a tool of its server
  again, through the backend, while it is drawn. The standard has no message
  that tells a view its data changed.
- **Why not the panel's own calls after each turn.** A view written for
  another host does not expect them, and the panel would again choose what a
  view shows.
- **From ADR 0007, unchanged.** The backend serves the view from its own
  origin, under a policy of the view's own, and the panel frames it with
  `sandbox="allow-scripts"` alone. There is no second origin, no `srcdoc`
  and no `blob:` URL. The backend relays a view's tool calls with the
  server's credential, and speaks MCP itself, over httpx. ADR 0007 holds the
  reasons.

## Not offered

- **Remote origins and permissions for a view**, as in ADR 0007.
- **What a view may ask of the host beyond tool calls**, as in ADR 0007.
- **Views in the chat.** The panel is the only place a view runs.
- **A sandbox proxy on another origin**, for the reason ADR 0007 gives
  against a second origin.
- **`ui/resource-teardown`.** The panel removes a frame in the step that
  closes its tab. A message posted in that step never reaches the frame. To
  send it, a closed frame would have to stay in the page until the view
  answers.

## Consequences

- **A server still cannot tell which conversation or person asked.** It sees
  its configured credential alone, on the agent's calls and on a view's. A
  server keeps things apart by an id that the agent's call carries, as
  `mcp/diagrams` does. Credentials per person are planned
  ([specs/core.md](../specs/core.md)).
- **A view that polls costs four requests to its server per read.** Each
  relayed request opens an MCP session and ends it. ADR 0007 accepted that
  cost for a panel a person clicks in. A view now reads on a timer, so the
  cost grows with the views on screens. Reuse of connections is not built.
- **A call names its tool and not its server.** A tool name that two of an
  agent's servers give is shown in no view.
- **The engines' own MCP clients make the agent's calls**
  ([ADR 0005](0005-the-framework-owns-the-loop-and-the-memory.md)). A
  result's text and structured content reach a view. Its other content
  blocks and its `_meta` do not. The engines do not read
  `_meta.ui.visibility`, and Robinauts does not advertise the extension to
  servers.
- **Structured content is kept in the transcript**, up to 1 MiB of JSON for
  each result. The conversation's read and the chat's stream leave it out.
