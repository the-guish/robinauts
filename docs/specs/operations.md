# Operations

What an internal platform team deploys and controls.

## Deployment

- One or more identical backend processes, each of which serves the
  frontend and runs turns; one PostgreSQL. Nothing else (goal 6). Any
  process serves any conversation: a turn's stream, its Stop and its
  conversation's delete work whichever process a request lands on, and no
  load balancer affinity is needed ([deployment.md](../deployment.md),
  "Several processes, and Kubernetes").
- Installed from one Python wheel. A container image is planned.
- PostgreSQL is always required. A documented one-command local Postgres
  covers development and demos.
- A local development mode runs without sign-in, on the loopback interface
  only ([sign-in.md](sign-in.md)). It is not a way to deploy.
- Served at the root of an origin, over https. `public_url` is mandatory
  ([sign-in.md](sign-in.md)).
- Upgrades: install the new wheel, bring the schema up to date, restart.
  Until the first release the schema is edited in place and the database
  is recreated; after that, migrations upgrade it in place.
- **A stop drains.** On `SIGTERM` the process stops being ready (`/ready`
  answers 503), refuses new turns (503, which another process takes), and
  ends every open stream with a hint to reconnect, so its clients attach
  again through another process and nothing long is left to wait for. The
  turns it runs get `[work] drain_seconds` (30 s) to finish; those still
  running then end as interrupted, each keeping what it had answered as a
  failed answer that Retry starts over. The wait for them to write their
  end, and the closing of the database connections, are bounded too, so a
  database that does not answer never holds a stop until it is killed.
  `/health` answers while the process does; `/ready` also needs the
  database to answer within a second and the connections that hold leases
  and listen for signals to be up. Several backend processes may run
  against the one database.
- Outbound traffic: the identity providers at sign-in, the model providers
  the operator configured, and the MCP tool servers the operator configured.
  Nothing else.
- **What a tool server sees is one identity per deployment.** A server's
  credential is the operator's, read from the environment variable its table
  names, so every user's turns act as that service account and the server's
  own audit log names it and not the person. That is the scope of this
  iteration; an operator who needs the server to know the person waits for
  per-user credentials. What an agent may do through a server is bounded by the credential's scopes and by the
  server's own admin gates, and by nothing here: every tool the agent's
  servers offer runs without asking. A server configured with `auth =
  "none"` — a public one, such as Microsoft Learn's — is sent no credential
  at all: what it sees is the deployment's address and the arguments the
  model wrote for the call, and nothing that names anyone. The client that
  connects is the agent framework's own MCP client, built from the server's
  table and nothing else.
- **How a tool server's credential is sent** is its table's `auth`, and the
  secret itself is always the value of the variable `secret_env` names, read
  when a turn connects and never written in the file:
  - `bearer` (the default): `Authorization: Bearer <secret>`.
  - `basic`: `Authorization: Basic`, with the table's `user` and the secret
    as the password.
  - `header`: the secret as it is, in the header the table's `header` key
    names, for a server that does not read `Authorization` at all, such as
    Composio's, which takes `x-api-key`. A `header` table without a
    `header` key, or with one that is not an HTTP header name, is refused at
    start-up, and so is a `header` key on any other `auth`: it names where
    the secret goes, and only this mode puts it anywhere but
    `Authorization`. Headers sent beside the credential, such as a user id
    some servers want next to a bearer, are not configurable yet.
  - `none`: nothing.

## Configuration

- Files, version-controllable, with no secret in them: a secret is always
  given as the *name* of an environment variable.
- **One file names the deployment**, and `ROBINAUTS_CONFIG` names the file.
  It was `ROBINAUTS_AUTH_CONFIG` while sign-in was all the file held; that
  name is still read, with a warning at start-up, and is deprecated. If both
  are set, `ROBINAUTS_CONFIG` is what is read and the start-up log says so.
- What the operator configures: sign-in providers, the allow list and the
  admins ([sign-in.md](sign-in.md)); model providers, models, agents and
  the MCP tool servers agents may use; limits and retention (below).
- The **local development mode** ([sign-in.md](sign-in.md)) may be given the
  same file and reads only its model tables; a file that also holds sign-in
  tables is a start-up refusal there.
- Unknown keys are errors, and all problems are reported at once, at
  start-up.

## Limits

All optional, all set by the operator:

- requests per minute per user;
- a maximum attachment size;
- timeouts for a model call (`models.<id>.timeout_seconds`, with the vendor
  SDK's `max_retries`), a tool call (`tool_servers.<id>.timeout_seconds`) and
  a whole turn (`[work] max_turn_seconds`), and the bound on the calls to
  the model one turn may make (`[work] max_model_calls`);
- a model's `context_window`, in tokens, which is what the frameworks keep
  a conversation's history within
  ([ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md)):
  optional, since the frameworks know the windows of the vendors' own
  models, and worth setting for a gateway's model ids, which they do not;
- a token budget per user per period, which refuses new turns once spent
  and is shown to the user. It depends on usage recording and arrives with
  it.

## Retention

- An optional retention period, after which conversations are deleted
  automatically. By default nothing expires
  ([privacy.md](privacy.md)).
- The trash is a fixed 30 days and is not configurable.

## Audit

- Read and exported by admins through the API
  ([privacy.md](privacy.md)).

## Usage reporting (planned)

All of it — recording, API, screens — is planned and not specified. What
is settled:

- it records the input and output tokens of every model call, against the
  model, the agent, the conversation and the user. Tokens, not prices: a
  company feeds the records into its own cost tooling;
- an API exports the records;
- the records hold no message content, outlive the conversation they refer
  to, and keep their reference to the user even after a purge;
- the admin's view of token usage, and token budgets, arrive with it.

## Details likely to change

- Whether the configuration is one file or several, and the key names.
  Sketches are in [sign-in.md](sign-in.md).
- The `robinauts` command: `start`, `db init`, `version`, later
  `db migrate`, and what else it needs. `start` runs uvicorn with
  `--proxy-headers` on and `--forwarded-allow-ips` naming the reverse proxy
  in front, since that is where the scheme and the client address of a
  request come from; `--dev-no-sign-in` is the local development mode and
  refuses any bind address that is not loopback.
- `--uds` binds a unix socket instead of an address, for a reverse proxy on
  the same machine. A socket is a file, so it is on no network at all — and
  **who on this machine may open it is the file's mode and its directory's**,
  not something the socket gives for free. The command binds it itself, with
  mode `0600` (this user alone) before it listens; `--uds-mode` widens that
  for a proxy running as another user, which then belongs in a directory only
  those two can enter. It cannot be given with `--host` or `--port`, the path
  is refused rather than written over if something is already there, and it is
  removed when the server stops. It satisfies the local development mode's
  loopback rule.
- With `--uds`, `--forwarded-allow-ips` defaults to `*`. A connection over a
  socket has no address to compare with anything, and the only thing that can
  connect is whatever the mode lets open the file — which is the proxy. **That
  assumes the default mode**: a deployment that widens `--uds-mode` has
  widened who may connect, and says `--forwarded-allow-ips` for itself.
- The database is named by `ROBINAUTS_DATABASE_URL` and by nothing on a
  command line: a url holds a password, and a command line is a shell
  history. A database that cannot be opened — no server there, no such
  database, credentials refused — is one line naming what the driver said,
  and never the url it was given. A start with sign-in and no
  `ROBINAUTS_DATABASE_URL` is refused: a replica that kept its records in
  memory would answer for the others' conversations with nothing. Only the
  local development mode keeps them in memory.
