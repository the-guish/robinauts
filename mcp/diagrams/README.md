# Diagrams

A diagram of rectangles and arrows, as an MCP server. A model draws it and changes
it; the server lays it out; a host that shows MCP Apps, such as the Robinauts side
panel, draws it, animated: every change is drawn step by step over 2 to 6 seconds.

It is a small server to try the platform with. It keeps one diagram, for everyone
who reaches it, in memory: the diagram is gone when the server stops.

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

| tool | arguments | what it does |
| --- | --- | --- |
| `show_diagram` | none | shows the diagram; it names the view |
| `draw_diagram` | `nodes`, `arrows`, `direction` (`TB` or `LR`) | replaces the diagram |
| `add_to_diagram` | `nodes`, `arrows` | adds; a node or arrow already there takes the new label |
| `remove_from_diagram` | `ids` | removes nodes, with their arrows, and arrows (`source->target`) |
| `pin_node` | `id`, `x`, `y` | keeps a node's centre at `x`, `y`; nodes it covers move aside |
| `unpin_node` | `id` | lets the layout place it again |

A node is `{"id", "label"}`, an arrow `{"source", "target", "label"}`. A diagram
holds at most 100 nodes and 200 arrows; a label is at most 60 characters.

Each tool answers twice: in words for the model, every node with its centre in
pixels from the top-left corner, so it can place one where the user asks; and, as
structured content, the diagram laid out, for the view. `show_diagram` is the one
tool a view may call (`_meta.ui.visibility`); the others are the model's.

## The layout

`src/diagrams/layout.py`, after dagre's steps and far simpler: ranks by the longest
path from a source, with cycles broken; an order within each rank by barycentres; each
node pulled towards its neighbours and pushed apart to keep the gap; then the pins.
Labels wrap at 26 characters, and the server sizes each rectangle from its text.

## The view

`src/diagrams/view.html`: one document, its style and script inline, an SVG, nothing
fetched, and the same handshake as the to-do list's view. It lays nothing out. Each
change bumps a revision and names the items it touched, node by node in layout order,
each arrow after its two ends; the view gives each one a slot of
`clamp(2 s, 6 s, 1.5 s + 0.25 s × items)`: a new node fades in, a node that moved
glides, a new arrow is drawn from its start, and what went fades out. The first time,
the whole diagram is drawn that way. Under `prefers-reduced-motion` it is drawn at once.

## Check it

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked black --check .
```
