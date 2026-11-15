# Web

The operations Robinauts' backend offers the interface, the phone and the workers.

## The component

Robinauts' backend is a oneberth composition (oneberth's `docs/specs/composition.md`).
Robinauts gives oneberth a component named `robinauts`: its routes, written on FastAPI, its
roles, and what it starts and stops. `new_robinauts_webapp` in `web/app.py` builds it, and
`robinauts start` composes it with neorc's manager (`docs/architecture/orchestration.md`).

| address | served by |
|---|---|
| `/robinauts/api/…` | Robinauts' routes, below |
| `/robinauts/internal/worker/…` | the workers' routes, on both listeners |
| `/robinauts/openapi.json` | Robinauts' OpenAPI document, `backend/openapi.json` |
| `/neorc/…` | neorc's manager |
| `/auth/…` | oneberth: sign-in, the session, tokens, pairing, devices, service tokens |
| `/ui/` | the built interface, or a page that says it is not built |
| `/health` | oneberth, from each component's state |

oneberth holds who is asking, the check of a route's permission, the checks on a request, the
security headers and the shape of an error, `{"error": …, "detail": …}`. A write is
`application/json`, a `POST` without a body too. The checks on ownership, such as whose
conversation this is, are the controller's.

### The start and the stop

The component has two moments of oneberth's. Its lifespan opens neorc's storage and the
controller, and starts the workers. Its serving begins once both listeners serve, before the
server says `ready`. It uploads the flows and starts neorc's worker, since both call the
backend's own listeners. On the desktop the launcher starts every worker, neorc's included, and
the backend starts none: `robinauts desktop-backend` is the backend there. A stop ends the serving first, while the listeners still serve: the
workers stop there, and end their turns through the listeners. Then the listeners stop, and
then the lifespan closes the controller and neorc's storage.

The tokens the backend mints for its own parts live in its memory alone: its workers', neorc's
worker's, the flows' upload's and the handlers'. The tokens the launcher has it mint live
there too. Each ends with the process, at a stop or a crash. The store holds none of them.

A checkout whose interface is not built still serves the API. `/ui/` then answers 404 with a
page that says the interface is not built, and the log says to run `npm run build` in
`frontend/`. A wheel always holds the interface.

## Roles and permissions

Robinauts declares its roles as data in `web/roles.py`. Every route declares one permission,
and oneberth refuses a principal that does not hold it with 403.

A worker's grant is bound to the worker's name. The worker routes name no worker in their
path: each declares `permit(…, bound=True)` and acts on the worker the token is bound to.
Every other route names its resource or serves every one, and refuses a bound grant.

| role | given to | resource | holds |
|---|---|---|---|
| `robinauts/user` | every person who may sign in | none | every permission of `/api/` |
| `robinauts/admin` | the people the configuration names | none | the same: no route is an admin's alone yet |
| `robinauts/worker` | a service token | one worker name | the routes of `/internal/worker/` |
| `robinauts/handler` | a service token | none | `agents:read`, `conversations:read`, `turns:start` |

| permission | routes |
|---|---|
| `robinauts/agents:read` | the agents and the models |
| `robinauts/apps:use` | the MCP apps |
| `robinauts/projects:read`, `robinauts/projects:write` | the projects |
| `robinauts/conversations:read` | the list, a conversation, the change feed, a turn's stream |
| `robinauts/conversations:write` | rename, archive, delete, fork, the model |
| `robinauts/turns:start` | a new conversation, a message, a cancel |
| `robinauts/flows:read`, `robinauts/flows:start` | the flows as forms, and a run's start |
| `robinauts/tasks:work` | a worker's tasks, its cancels, a turn's owner |
| `robinauts/sessions:read` | the sessions and messages a worker's turn reads |
| `robinauts/turns:write` | the events and the end of a worker's turn |
| `neorc/tasks:claim`, on the queue `default` | the handlers' token, for neorc's worker |

## Who a request is for

- A person is the oneberth user. Robinauts keeps its own row of them, under the same id, in
  its `users` table: their projects, conversations and change version hang from it. The row is
  written the first time the person asks.
- A flow's handler holds a token of `robinauts/handler` and names its run in
  `X-Robinauts-Run`. The server acts for the person who started that run through
  `POST /robinauts/api/flows/{flow}/runs`. In the local mode, a run started elsewhere is the
  one person's. Any other service is refused with 403.

## Sign-in, devices, tokens

oneberth's (oneberth's `docs/specs/identity.md`, "Routes"). `GET /auth/session` says who is
asking, their roles and the providers. Personal tokens, pairing a phone, the linked devices and
the service tokens are under `/auth/`. The local mode signs its one person in with a ticket at
`/auth/local`.

## Catalogue

- `list_agents`
- `list_models`

## Apps

The MCP apps of an agent's tool servers, for the side panel
([ADR 0007](../adr/0007-mcp-apps-in-the-side-panel.md), `web/apps.py`). An app is a tool whose
`_meta.ui.resourceUri` names a `ui://` resource of type `text/html;profile=mcp-app`: its view.
Each route needs `robinauts/apps:use`, and names the agent. The agent's apps are
those of the tool servers it names, less what a server's `exclude` keeps from it. Web reaches a
server with its configured credential, read from `secret_env` as the worker reads it, within its
`timeout_seconds`, over streamable HTTP with a client of its own (`web/mcp_client.py`): `mcp` is
the engines' alone.

- `list_apps`: `GET /robinauts/api/apps?agent=<agent>`, answered
  `{"apps": [{"id": "<server>/<tool>", "server", "tool", "title", "description"}]}`, server by
  server in the agent's order. An app is listed when a view may call its tool. A server that
  cannot be reached is left out and logged by its id and the kind of failure. An unknown agent
  is 404 `UnknownAgentError`.
- `open_app_view`: `GET /robinauts/api/apps/<server>/<tool>/view?agent=<agent>`, the view's HTML, which the
  panel frames. Its answer alone carries `Content-Security-Policy: default-src 'none';
  script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:;
  connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self';
  sandbox allow-scripts`, `X-Frame-Options: SAMEORIGIN` and `Cache-Control: no-store`. The
  origins a server asks for in `_meta.ui.csp` are not given. Outside the OpenAPI document: it is
  a frame's address, not a call.
- `call_app_tool`: `POST /robinauts/api/apps/<server>/tools/<tool>` with `{"agent", "arguments"}`, a tool
  call of the view's, answered with the server's `CallToolResult` as it gave it:
  `{"content", "structuredContent", "isError"}`. A tool that failed is `isError` and 200. A tool
  whose `_meta.ui.visibility` does not include `app` is refused.
- What the agent is not given is refused alike, 404 `NotFound`: a server it does not name, a
  tool the server does not offer it, a tool with no view, and, for a view, a server that cannot
  be reached. A call that a server does not answer with a result is 502 `BadGateway`. No answer
  carries a server's credential, and no refusal its error text.

## Sessions

- `list_sessions`: most recently updated first, a page at a time.
- `open_session`: one moment of a session, its messages and its active turn if any.
- `rename_session`
- `archive_session`
- `delete_session`
- `fork_session`: a new session from a message of another, independent from then on.

## Turns

- `start_session`: the first message, with the agent and the model; mints the session.
- `send_message`: a message under a chosen parent, with the model. An edit is this under an earlier parent.
- `regenerate_answer`: the answer to a question again, under the same question.
- `resume_answer`: the turn of the session's latest answer, when it failed, continued from what its engine saved.
- `cancel_turn`: stop the session's active turn.
- `watch_turn`: the events of the session's active turn, from a position, as they happen.

## Worker

What a worker in a process of its own reaches the core through: the operations of
`TurnBroker` (`docs/architecture/controller.md`, "Worker"), one route each under
`/robinauts/internal/worker/`, served over the controller's store. They are not the
interface's and are not in the OpenAPI document.

- There is one worker process per worker the configuration names (`AgentConfig.worker`,
  `default` for an agent that names none). The server, or on the desktop the launcher, mints a
  oneberth service token of `robinauts/worker` for each at its start, bound to the worker's
  name, and writes it on that worker's process's standard input, which the process reads to its end at its own start; it
  is in no environment variable and no file. The token lives in the backend's memory, and
  ends with it ([The start and the stop](#the-start-and-the-stop)). A worker started with its table's command is let in the same way: the command carries
  the standard input to wherever the worker runs.
- oneberth serves these routes on the worker listener, `--worker-host` and `--worker-port`,
  where they are the only routes; they are on the front listener too. A worker reaches
  `http://<worker listener>/robinauts`. The launcher names the listener in
  `ONEBERTH_WORKER_URL`, and a server names the whole address in `ROBINAUTS_WORKER_BROKER_URL`.
- Every route needs one of the three worker permissions, and a person or another service is
  403. Each route is bound: it reads the worker's name from the token's grant
  (`Access.bound_resource`). With no worker process of that name running, the routes answer 404. Each admitted
  request is a sign of life for `/health`, which hears a worker in a process of its own
  through nothing else.
- The worker name the token is bound to says which worker asks, and each is served over a
  broker of its own: its claims and waits see the tasks of its own agents alone, whatever the
  request says. Cancels are every worker's alike; a worker ignores those of tasks it does not
  run.
- A turn lost is 409 `turn_lost`, a session not found 404 `session_not_found`: the worker
  raises the core's own errors for them.
- `listen_for_cancels` is a long poll past a cursor. A cancel the worker misses is named by
  its next renewal of the leases.
- A server stops its workers before it stops listening, since a worker ends its turns
  through these routes; the waits they hold end at once when the workers have stopped.
