# Diagrams

Diagrams of rectangles and arrows, as an MCP server. A model draws a diagram and
changes it; the server lays it out; a host that shows MCP Apps, such as the Robinauts
side panel, draws it, animated: every change is drawn step by step over 2 to 6 seconds.

It is a small server to try the platform with. It keeps its diagrams in memory, each
under an id. The diagrams are gone when the server stops.

## Run it

From this folder:

```bash
uv sync --locked
uv run --locked robinauts-diagrams
```

It listens on `http://127.0.0.1:8766/mcp`. `python -m diagrams` does the same.
`--host` and `--port`, or `DIAGRAMS_HOST` and `DIAGRAMS_PORT`, change where. It
asks for no credential: keep it on the loopback address.

[examples/diagrams.toml](../../examples/diagrams.toml) names it as a tool server
(`auth = "none"`) and gives it to an agent.

## What it offers

A model draws a diagram with `draw_diagram`, and the answer names the diagram's id.
The model then calls `show_diagram` with that id, and the host shows the diagram to
the person. A later change to a diagram on show appears by itself.

| tool | arguments | what it does |
| --- | --- | --- |
| `draw_diagram` | `nodes`, `arrows`, `direction` (`TB` or `LR`), `diagram_id` | makes a diagram and answers its id; with a `diagram_id`, replaces that diagram |
| `show_diagram` | `diagram_id` | shows the diagram to the person; it names the view |
| `add_to_diagram` | `diagram_id`, `nodes`, `arrows` | adds; a node or arrow already there takes the new label |
| `remove_from_diagram` | `diagram_id`, `ids` | removes nodes, with their arrows, and arrows (`source->target`) |
| `pin_node` | `diagram_id`, `id`, `x`, `y` | keeps a node's centre at `x`, `y`; nodes it covers move aside |
| `unpin_node` | `diagram_id`, `id` | lets the layout place it again |

A node is `{"id", "label"}`, an arrow `{"source", "target", "label"}`. A diagram
holds at most 100 nodes and 200 arrows; a label is at most 60 characters.

An id is 16 random hex digits. Whoever holds an id reaches its diagram. The server
keeps at most 200 diagrams. A new diagram beyond that takes the place of the one
created first. An id the server does not hold is a tool error that says so.

Each tool answers twice. The text is for the model: the diagram's id, and every node
with its centre in pixels from the top-left corner, so the model can place a node
where the user asks. The structured content is for the view: the diagram laid out.

```json
{
  "diagram_id": "9f1c2a3b4d5e6f70",
  "revision": 2,
  "direction": "TB",
  "width": 144,
  "height": 186,
  "nodes": [
    {"id": "web", "lines": ["Web"], "x": 72, "y": 44, "w": 96, "h": 41, "pinned": false},
    {"id": "db", "lines": ["db"], "x": 72, "y": 142, "w": 96, "h": 41, "pinned": false}
  ],
  "edges": [{"key": "web->db", "source": "web", "target": "db", "label": ""}],
  "order": ["web", "db", "web->db"],
  "changed": ["db", "web->db"]
}
```

`revision` counts the diagram's changes. `order` lists every item in the order to
draw the whole diagram. `changed` lists the items the last change touched.

`show_diagram` is the one tool a view may call: its `_meta.ui.visibility` is
`["model", "app"]`. The other tools have `["model"]`.

The view is one resource, `ui://diagrams/view.html`, of type
`text/html;profile=mcp-app`. `show_diagram` names it in `_meta.ui.resourceUri`. The
resource's title, "Diagrams", names the tab in the side panel. The tool's title,
"Diagram", names the tab's sub-tabs: "Diagram 1", "Diagram 2".

## The layout

`src/diagrams/layout.py`, after dagre's steps and far simpler: ranks by the longest
path from a source, with cycles broken; an order within each rank by barycentres; each
node pulled towards its neighbours and pushed apart to keep the gap; then the pins.
Labels wrap at 26 characters, and the server sizes each rectangle from its text.

## The view

`src/diagrams/view.html`: one document, its style and script inline, an SVG, nothing
fetched. It shows one diagram: the one the agent's call names. It speaks to the host
alone, in JSON-RPC 2.0 over `postMessage`, with no SDK:

1. It asks `ui/initialize` with `protocolVersion` `2026-01-26`, takes the theme from
   the answer's `hostContext`, and sends `ui/notifications/initialized`.
2. It keeps the `diagram_id` of `ui/notifications/tool-input`: the arguments of the
   agent's call.
3. It draws the diagram from the `structuredContent` of
   `ui/notifications/tool-result`. A later result for the same view is drawn the
   same way.
4. It reads the diagram again every 2 seconds. It asks the host `tools/call` for
   `show_diagram` with its `diagram_id`, and draws the answer.
5. It says "The diagram could not be shown." on `ui/notifications/tool-cancelled`,
   and on a first result that is an error.
6. It follows `ui/notifications/host-context-changed` to a light or dark theme.

It answers `ping` and `ui/resource-teardown`, and refuses any other request with
-32601. The teardown ends the reads.

The view reads only while it is drawn. Each read waits for an animation frame
(`requestAnimationFrame`). The browser gives no frame to a view hidden with
`display: none`, or to a view in a background tab. One read is in flight at a time.
A read that fails leaves the drawing as it is, and the next tick tries again. The
reads start once the view knows its `diagram_id`.

The view lays nothing out. Each change bumps a revision and names the items it
touched, node by node in layout order, each arrow after its two ends; the view gives
each one a slot of `clamp(2 s, 6 s, 1.5 s + 0.25 s × items)`: a new node fades in, a
node that moved glides, a new arrow is drawn from its start, and what went fades out.
The first time, the whole diagram is drawn that way. A revision already drawn is left
alone, and so is an older one. Under `prefers-reduced-motion` it is drawn at once.

## Check it

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked black --check .
```
