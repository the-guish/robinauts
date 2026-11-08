# ADR 0007 — MCP apps in the side panel

- Status: accepted.
- Date: 2026-11-08

## Context

MCP Apps (the `io.modelcontextprotocol/ui` extension, SEP-1865) lets a tool
server ship a small interface with a tool. The tool carries
`_meta.ui.resourceUri`, a `ui://` resource. The resource is one HTML document
of type `text/html;profile=mcp-app`: the **view**. A **host** shows the view
in a sandboxed iframe and talks to it over `postMessage`, in JSON-RPC.

We want the agent's apps in a side panel next to the chat. Three roles meet:

- the **server**: a tool server the operator configured
  (`[tool_servers.<id>]`), reached with its credential;
- the **host**: Robinauts. The backend reads the view and relays the view's
  tool calls. The frontend frames the view and speaks the protocol;
- the **view**: a server's HTML, running in the user's browser. It talks to
  the frontend alone.

The view is code written by a third party. It runs next to a page that holds
a signed-in person's session.

## Decision

**The backend serves the view from its own origin, under a policy of its
own. The frontend frames it sandboxed. The backend relays the view's tool
calls.**

- **Same origin, sandboxed, a policy of its own.** The view is served at
  `/api/apps/<server>/<tool>/view`. The panel frames it with
  `sandbox="allow-scripts"` and never `allow-same-origin`. Its answer carries
  `sandbox allow-scripts` in its own Content-Security-Policy as well, so it
  gets an opaque origin even when opened as a page of its own. It cannot
  read the session, call the API, or script the interface. The rest of its
  policy allows inline script and style, `data:` images and fonts, and
  nothing to fetch: `connect-src 'none'`. `frame-ancestors 'self'` and
  `X-Frame-Options: SAMEORIGIN` let the interface alone frame it. The
  interface's policy gains `frame-src 'self'` and nothing else.
- **Why not another origin.** A second origin is the usual way to isolate a
  view. It needs a second host name and its certificate in every
  deployment, against goal 6 (one backend). The sandbox gives the view an
  origin of its own without one.
- **Why not a `srcdoc` or a `blob:` URL.** Either would make the interface's
  policy allow inline script, or `blob:`, for the page that holds the
  session. A view served by URL keeps that policy as strict as it was.
- **The backend relays the tool calls.** The view asks the frontend; the
  frontend asks the backend; the backend asks the server. The server's
  credential stays on the backend, as for the engines (goal 5). The backend
  calls only the servers the agent names, and only the tools the agent is
  given and a view may call (`_meta.ui.visibility`). The browser never
  reaches a tool server, so nothing new leaves the hosted environment
  (goal 3).
- **The panel is the host, and reads only its frame.** The frontend
  (`frontend/src/side/AppTab.tsx`, `bridge.ts`) reads a message only when
  its `source` is the frame's window and it is JSON-RPC it understands.
  An opaque origin has no name to post to, so the panel posts to `"*"`,
  and sends only what the view may see: the theme, and what the view's
  own server answered. A tool call the backend refuses is answered with
  fixed words, not the backend's.
- **The backend speaks MCP itself.** `mcp` is the engines' alone
  ([rules.md](../architecture/rules.md)). Web needs three requests over
  streamable HTTP, so it has a small client of its own on httpx, which web
  may already import.

## Not offered

- **Remote origins for a view.** A server may ask for them in
  `_meta.ui.csp`. They are not given: nothing phones home.
- **What a view may ask of the host beyond tool calls.** `ui/open-link`,
  `ui/message` and the rest are answered "method not found". A view cannot
  open a page, write in the chat, or reach the model.
- **Views in the chat.** A tool call in the transcript shows no view. The
  panel is the only place apps run.
- **Resources other than the view, and a view's own resource reads.**

## Consequences

- An app is a tool with a view that a view may call. A server that cannot be
  reached is left out of the list, and logged without what it said.
- Each request opens an MCP session and ends it. That is a few round trips
  per call, and fine for a panel a person clicks in.
- The view's policy is in `web/headers.py`, next to the interface's, and a
  test holds each to its text.
