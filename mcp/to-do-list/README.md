# The to-do list

A to-do list as an MCP server. A model can show the list, add to it, edit items,
tick them off and remove them. A host that shows MCP Apps, such as the Robinauts side
panel, also gets a view of the list that a person can use directly.

It is a small server to try the platform with. It keeps one list, for everyone
who reaches it, in one JSON file.

## Run it

From this folder:

```bash
uv sync --locked
uv run --locked robinauts-to-do-list
```

It listens on `http://127.0.0.1:8765/mcp`. `python -m to_do_list` does the
same. Each setting is a flag or an environment variable:

| flag | variable | default |
| --- | --- | --- |
| `--host` | `TO_DO_LIST_HOST` | `127.0.0.1` |
| `--port` | `TO_DO_LIST_PORT` | `8765` |
| `--file` | `TO_DO_LIST_FILE` | `$XDG_DATA_HOME/robinauts-to-do-list/todos.json`, or `~/.local/share/robinauts-to-do-list/todos.json` without the variable |

The server asks for no credential. Anyone who reaches it can read and change
the list. Keep it on the loopback address, or put something that asks in front
of it. On the loopback address it also refuses a request whose `Host` header
names another machine, so a web page cannot reach it through DNS rebinding.

A file that is not a to-do list stops the start, with a message that says why.
The file is written whole on every change, and replaced in one step.

## Use it from Robinauts

Name the server in the configuration file, and give it to an agent:

```toml
[tool_servers.todo]
url = "http://127.0.0.1:8765/mcp"
auth = "none"

[agents.planner]
title = "Planner"
system_prompt = "You keep the user's to-do list."
models = ["haiku"]
engine = "pydantic-ai"
tools = ["todo"]
```

[examples/to-do-list.toml](../../examples/to-do-list.toml) is a whole file.
`auth = "none"` sends no credential, which is what this server expects. The id
`todo` is yours to choose; the agent's `tools` names it.

## What it offers

Five tools. Each answers with the whole list, twice: as text for the model,
and as structured content, `{"items": [{"id": "1", "text": "milk", "done": false}]}`,
for the view.

| tool | arguments | what it does |
| --- | --- | --- |
| `show_todos` | none | shows the list |
| `add_todo` | `text` | adds a to-do at the end |
| `edit_todo` | `id`, `text` | replaces its text, and keeps its id and whether it is done |
| `toggle_todo` | `id` | marks it done, or not done if it was |
| `remove_todo` | `id` | removes it |

An unknown id, an empty text or a text over 500 characters is a tool error that
says why. The list holds at most 1000 to-dos. Ids are not reused.

One resource, `ui://to-do-list/app.html`, of type `text/html;profile=mcp-app`:
the view. `show_todos` names it in `_meta.ui.resourceUri`, as the MCP Apps
extension (`io.modelcontextprotocol/ui`) has it. Every tool carries
`_meta.ui.visibility = ["model", "app"]`: the model and the view may both call
it.

## The view

`src/to_do_list/app.html`, read once at start-up. It is one document with its
style and script inline. It names no other file, no address and no font, so it
works in an iframe with `sandbox="allow-scripts"` under
`default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'`.
It keeps nothing in the browser.

It speaks to the host alone, in JSON-RPC 2.0 over `postMessage`, with no SDK:

1. It asks `ui/initialize` with `protocolVersion` `2026-01-26`, takes the theme
   from the answer's `hostContext`, and sends `ui/notifications/initialized`.
2. It draws the list from the `structuredContent` of
   `ui/notifications/tool-result`: the result of the agent's call of
   `show_todos`. A later result for the same view is drawn the same way.
3. It asks the host `tools/call` for `add_todo`, `toggle_todo` and
   `remove_todo`, and draws the list again from each answer.
4. It reads the list again every 2 seconds. It asks the host `tools/call` for
   `show_todos`, and draws the answer.
5. It says "The to-do list could not be shown." on
   `ui/notifications/tool-cancelled`.
6. It follows `ui/notifications/host-context-changed` to a light or dark theme.
   Until the host names one, it follows the system's.

It answers `ping` and `ui/resource-teardown`, and refuses any other request with
-32601. The teardown ends the reads. A sandbox without `allow-forms` never
submits a form, so the button and the Enter key add a to-do themselves.

The host sends the result of the agent's call and nothing after it. The view
keeps the list fresh with its own reads, so a change the agent makes shows
within 2 seconds.

- **The view reads only while it is drawn.** Each read waits for an animation
  frame (`requestAnimationFrame`). The browser gives no frame to a view hidden
  with `display: none`, or to a view in a background tab.
- **One read is in flight at a time.** A call of the view's own holds the reads
  back until it is answered.
- **A read that fails says nothing.** The next tick tries again.
- **A list equal to the one on the screen changes nothing.** The list is not
  drawn again, and the focus stays where it is.
- **A list that differs is drawn again.** The focus stays on the same control,
  and the text typed in "New to-do" stays.

## Check it

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked black --check .
```

`scripts/check-mcp-servers.sh` runs the same from the repository root. The tests
start the server as its command and speak to it over HTTP, as a host would.
