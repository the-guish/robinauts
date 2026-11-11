# Web

The operations the web shell offers the frontend.

## Sign-in

- `sign_in`: start the sign-in with a provider, and finish it when the provider answers.
- `sign_out`
- `current_user_session`: who is signed in, if anyone.
- `mint_api_token`, `list_api_tokens`, `revoke_api_token`: the API tokens of the person signed in, sent as a bearer. Minting and revoking need the browser's session, minting one younger than ten minutes.

## Devices

How a phone is linked and ended (`docs/architecture/device-pairing.md`). Minting and removing need the browser's session; a bearer is answered 403.

- `mint_pairing`: a code for a phone, answered as its QR picture alone; it needs a session younger than ten minutes.
- `pairing_state`: what the browser polls: waiting, claimed with the device's description, or approved.
- `claim_pairing`: the phone presents the code, with no credential, and gets a claim secret and the number to show.
- `approve_pairing`: the number the phone shows, typed into the browser; links the device.
- `refuse_pairing`: No, or a code cancelled.
- `list_devices`: the person's linked devices, oldest first.
- `remove_device`: end one of them.
- `current_user_session` also answers a bearer: the device or the token it is, or the pairing a claim waits on; and `sign_out` with a device's bearer ends that device.

## Catalogue

- `list_agents`
- `list_models`

## Apps

The MCP apps of an agent's tool servers, for the side panel
([ADR 0007](../adr/0007-mcp-apps-in-the-side-panel.md), `web/apps.py`). An app is a tool whose
`_meta.ui.resourceUri` names a `ui://` resource of type `text/html;profile=mcp-app`: its view.
Each route wants a signed-in user, like `list_agents`, and names the agent. The agent's apps are
those of the tool servers it names, less what a server's `exclude` keeps from it. Web reaches a
server with its configured credential, read from `secret_env` as the worker reads it, within its
`timeout_seconds`, over streamable HTTP with a client of its own (`web/mcp_client.py`): `mcp` is
the engines' alone.

- `list_apps`: `GET /api/apps?agent=<agent>`, answered
  `{"apps": [{"id": "<server>/<tool>", "server", "tool", "title", "description"}]}`, server by
  server in the agent's order. An app is listed when a view may call its tool. A server that
  cannot be reached is left out and logged by its id and the kind of failure. An unknown agent
  is 404 `UnknownAgentError`.
- `open_app_view`: `GET /api/apps/<server>/<tool>/view?agent=<agent>`, the view's HTML, which the
  panel frames. Its answer alone carries `Content-Security-Policy: default-src 'none';
  script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:;
  connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self';
  sandbox allow-scripts`, `X-Frame-Options: SAMEORIGIN` and `Cache-Control: no-store`. The
  origins a server asks for in `_meta.ui.csp` are not given. Outside the OpenAPI document: it is
  a frame's address, not a call.
- `call_app_tool`: `POST /api/apps/<server>/tools/<tool>` with `{"agent", "arguments"}`, a tool
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
`/internal/worker/`, served over the controller's store. They are not the frontend's and are
not in the OpenAPI document.

- There is one worker process per worker the configuration names (`AgentConfig.worker`,
  `default` for an agent that names none). The server makes a secret for each at its start
  and writes it on that worker's process's standard input, which the process reads to its end
  at its own start; it is in no environment variable and no file. A worker started with its
  table's command is let in the same way: the command carries the standard input to wherever
  the worker runs. Every route wants one of them as `Authorization: Bearer`: any other
  credential is 401. With no worker process running, the routes answer 404. Each admitted
  request is a sign of life for `/health`, which hears a worker in a process of its own
  through nothing else.
- The secret says which worker asks, and each is served over a broker of its own: its claims
  and waits see the tasks of its own agents alone, whatever the request says. Cancels are
  every worker's alike; a worker ignores those of tasks it does not run.
- The worker reaches the server where it listens, on a loopback address when it listens on
  every address.
- A turn lost is 409 `turn_lost`, a session not found 404 `session_not_found`: the worker
  raises the core's own errors for them.
- `listen_for_cancels` is a long poll past a cursor. A cancel the worker misses is named by
  its next renewal of the leases.
- A server stops its workers before it stops listening, since a worker ends its turns
  through these routes; the waits they hold end at once when the workers have stopped.

