# Operations

What an internal platform team deploys and controls.

## Deployment

- One backend process, which also serves the frontend; one PostgreSQL.
  Nothing else (goal 6).
- Installed from one Python wheel. The repository's `Dockerfile` builds Robinauts' image,
  which runs the backend in the desktop's Docker shape. A server's image is planned.
- A deployment requires PostgreSQL. A documented one-command local Postgres
  covers development and demos.
- The backend is a oneberth composition of Robinauts' component and neorc's
  manager ([architecture/web.md](../architecture/web.md)). It has a front
  listener, `--host` and `--port`, and a worker listener, `--worker-host` and
  `--worker-port`, the front's port plus one by default.
- oneberth's local mode, `--local`, signs the one person at the machine in
  with a ticket, on a loopback address ([sign-in.md](sign-in.md)).
  It is the desktop's mode and the mode for development.
- Local storage: the records are then in files of the data directory, which
  `ROBINAUTS_DATA_DIR` names: Robinauts' in `robinauts.db`, oneberth's in
  `oneberth.db` and neorc's in `neorc.db`. The server makes them at its first
  start, and refuses a file of another schema version. Local storage serves
  both sign-in modes.
- On local storage each worker keeps its engines' state in a folder of its
  own, `workers/<worker name>/` in the data directory. Each engine has one
  file there: `langchain.db`, `pydantic-ai.db`. An engine makes its file at
  its first start, and refuses a file of another schema version. A
  conversation from before a restart takes new turns.
- On PostgreSQL, oneberth's store is the schema `oneberth` and neorc's the
  schema `neorc`, beside Robinauts' tables. `robinauts db init` makes
  Robinauts' tables, and `robinauts store init` oneberth's.
- Served at the root of an origin, over https, behind sign-in through
  identity providers. `public_url` is mandatory, in oneberth's file
  ([sign-in.md](sign-in.md)).
- Upgrades: install the new wheel, bring the schema up to date, restart.
  Until the first release the schema is edited in place and the database
  is recreated; after that, migrations upgrade it in place.
- A restart ends the runs that are in flight: each is marked interrupted,
  and its author retries it by sending the message again. Letting them
  **drain** for a bounded time first is planned; the bounded window a
  shutdown has today is for ending them and giving back what the process
  holds, not for finishing them. Several backend processes may run against
  the one database.
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
  servers offer, but those a table's `exclude` leaves out (below), runs
  without asking. A server configured with `auth =
  "none"` — a public one, such as Microsoft Learn's — is sent no credential
  at all: what it sees is the deployment's address and the arguments the
  model wrote for the call, and nothing that names anyone. The client that
  connects is the agent framework's own MCP client, built only from the
  server's table.
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
- **A tool server's `exclude`** lists tools of the server, by the server's
  own names, that agents are not given: both engines leave them out of
  the tools the model is offered. One use is a tool whose input schema a
  model provider refuses, which fails every turn of every agent that uses
  the server; Composio's `COMPOSIO_MANAGE_SKILL` has `anyOf` at the top of
  its schema, which Anthropic's API refuses. A value that is not a list of
  names is refused at start-up.

## The desktop

- `robinauts desktop` starts the desktop with oneberth's launcher (oneberth's
  `docs/specs/shapes.md`). The launcher starts the backend, each worker, neorc's worker when
  there is a flows folder, and the window ([deployment.md](../deployment.md), "The desktop").
- The backend is `robinauts desktop-backend`: the local mode, local storage in the data
  directory, its listeners where the launcher says, and the launch secret on its standard
  input. It starts no worker.
- Each worker is a process of its own. It reads its token on its standard input and the
  launcher's variables, and keeps its engines' state in its own folder of the data directory.
  The backend's process opens none of a worker's files.
- The data directory is `$XDG_DATA_HOME/robinauts` on Linux, `~/.local/share/robinauts` by
  default, and `~/Library/Application Support/Robinauts` on macOS. `--data-dir` or
  `ROBINAUTS_DATA_DIR` names another.
- A worker whose table names an `image` runs in a container of that image. One such worker
  puts the whole desktop in the Docker shape. The backend then runs in a container of
  Robinauts' image, `ROBINAUTS_IMAGE`, with the data directory mounted at `/data`.
- A worker's container sees its own folder of the data directory, and a copy of the
  configuration in it. It gets the keys of its agents and the variables its table names, and
  nothing else of the desktop's environment. Every worker of this version is a trusted worker:
  its container joins the `app` network, which has a way out.
- neorc's worker and the workers without an `image` stay processes of the host. They reach the
  backend's listeners on the loopback address.
- The launcher refuses to start while Docker is down, and names the workers that need it.
- Closing the window stops the application: the workers first, each ending its turns, then the
  backend. One launcher runs per data directory: a second start brings its window forward.
- A server keeps its own shape: PostgreSQL, sign-in through identity providers, and
  `robinauts start`, which starts its own workers.

## Configuration

- Files, version-controllable, with no secret in them: a secret is always
  given as the *name* of an environment variable.
- **Two files configure the backend.** `ROBINAUTS_CONFIG` names Robinauts'
  own: the **agents'** configuration. `ONEBERTH_CONFIG` names oneberth's: the
  sign-in, which every backend of the deployment reads
  ([sign-in.md](sign-in.md)). The local mode reads no oneberth file. The **flows'** is a folder of
  its own, apart from it, which `ROBINAUTS_FLOWS_DIR` names: the flow files
  and their settings, `flows.toml`; without it there is no orchestrator
  ([architecture/orchestration.md](../architecture/orchestration.md)).
- What the operator configures: in oneberth's file, the sign-in providers,
  the allow list and the grants ([sign-in.md](sign-in.md)); in Robinauts',
  model providers, models, agents and the MCP tool servers agents may use;
  limits and retention (below). A sign-in key in Robinauts' file is refused,
  with where it went.
- Unknown keys are errors, and all problems are reported at once, at
  start-up.

## Workers

- A server runs its agents' turns in **workers**. An agent's optional `worker` key names
  the one its turns run on; an agent without it runs on `default`, which always runs.
  There may be as many workers as names the agents give: lowercase letters, digits, `-`
  and `_`, starting with a letter or a digit, at most 63 characters.

  ```toml
  [agents.agent1]
  worker = "special"   # runs on the worker "special"
  [agents.agent2]       # runs on the worker "default"
  [agents.agent3]
  worker = "special"   # runs on the worker "special"
  [agents.agent4]       # runs on the worker "default"
  ```

- On PostgreSQL each worker is a **process of its own**, which the server spawns at its
  start and stops at its stop. On the desktop, oneberth's launcher starts each worker, and
  the backend starts none: it lets each in by the token the launcher had it mint. The server's broker API hands each worker the turns of
  its own agents: a worker is known by the secret the server gave it, not by
  anything it says. A worker builds the engines of its own agents alone.
- A worker's optional `[workers.<name>]` table names the **command** that starts it, in
  place of this build's worker module: a container, a machine away, an engine this build
  does not ship. Nothing else sets it apart, and it runs in a process of its own even where
  the rest run in the server's.

  ```toml
  [workers.claude]
  command = ["docker", "run", "-i", "--rm", "--network", "host",
             "-e", "ROBINAUTS_CONFIG", "-e", "ROBINAUTS_WORKER_NAME",
             "-e", "ROBINAUTS_WORKER_BROKER_URL",
             "-v", "/etc/robinauts/robinauts.toml:/etc/robinauts/robinauts.toml:ro",
             "robinauts-claude-code-worker"]

  [agents.coder]
  engine = "claude-code"       # an engine this build does not have: allowed here alone
  worker = "claude"
  ```

  The server does with the command what it does with its own worker module. It runs it with
  the server's environment and three variables more, `ROBINAUTS_WORKER_NAME`,
  `ROBINAUTS_WORKER_BROKER_URL` and `ROBINAUTS_WORKER_PARENT`; writes the worker's secret,
  and nothing else, on its standard input and closes it; waits a minute at most for the line
  `ready` on its standard output, logging whatever else it says; and sends it SIGTERM at the
  stop. The command passes on only what the worker needs: `docker run -i` carries the
  standard streams, the signal and the variables its `-e` flags name, and the database's URL
  is not among them. An agent of such a worker may name an engine this build does not have,
  since that worker runs it: one a package registers under the `robinauts.engines`
  entry-point group, beside the `robinauts` package the worker module comes from
  ([extra-workers/](../../extra-workers/README.md)). Refused at start-up: a table without
  `command`, `image` or `external`, one no agent names, and a command that cannot be started or ends without saying
  `ready`.
- A `[workers.<name>]` table may name an **image** in the place of a command, or beside it.
  On the desktop, the worker then runs in a container of that image ("The desktop"). Its
  `environment` names the variables of the desktop's environment that the container gets
  beside the keys of its agents, such as the token of a GitHub account. A server starts a
  worker with its command alone: a table with an image and no command is refused there.

  ```toml
  [workers.claude]
  image = "robinauts-claude-code-worker"
  environment = ["ROBINAUTS_CLAUDE_AUTH", "CLAUDE_CODE_OAUTH_TOKEN"]
  ```

- A `[workers.<name>]` table may say `external = true` in the place of a command or an image.
  Something else starts that worker, such as a container of its own or a pod. The server
  starts none, and lets it in by a token of `robinauts/worker` bound to its name, which the
  operator creates on the store:

  ```sh
  robinauts tokens create claude-worker --role robinauts/worker --resource claude
  ```

  The worker reads that token from the file `ONEBERTH_TOKEN_FILE` names, or from its
  standard input. It reaches the worker listener at `ROBINAUTS_WORKER_BROKER_URL`, the
  component's address, `http://<worker listener>/robinauts`, and names itself in
  `ROBINAUTS_WORKER_NAME`. The desktop's launcher does not start it either.

  ```toml
  [workers.claude]
  external = true
  ```

- `max_running_tasks_per_worker` is per worker: each runs that many turns at once.
- `/health` answers 503 when any of the server's workers has not been heard from: one in the
  server's process at its heartbeat, one in a process of its own at its requests to the
  broker API.
- A turn is bound to its worker when it is queued. A turn queued for a worker the
  configuration no longer names, after a restart, waits until it is ended as queued too
  long.

## Limits

All optional, all set by the operator:

- requests per minute per user;
- a maximum attachment size;
- timeouts for a model call, a tool call and a whole turn (`[work]`
  `max_turn_seconds`, 20 minutes by default, or an agent's
  `turn_timeout_seconds`);
- a model's `context_window`, in tokens, which is what the frameworks keep
  a conversation's history within
  ([ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md)):
  optional, since the frameworks know the windows of the vendors' own
  models, and worth setting for a gateway's model ids, which they do not;
- a token budget per user per period, which refuses new turns once spent
  and is shown to the user. It depends on usage recording and arrives with
  it.

Two bounds are not settings. A turn may make one model call per second of
its timeout, which stops a runaway tool loop. A model call that fails with a
429, a 5xx, a timeout or a lost connection is retried with growing waits, or
the waits the vendor asks for. The retries last about two minutes before the
turn fails, and the turn's deadline ends them first.

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
- The `robinauts` command: `start`, `db init`, `version`, oneberth's
  `store`, `tokens`, `sessions` and `devices`, later `db migrate`, and what
  else it needs. `start` serves with oneberth's server; `--local` is
  oneberth's local mode and refuses any bind address that is not loopback.
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
- `ROBINAUTS_DATABASE_URL` and `ROBINAUTS_DATA_DIR` together are refused.
- The database is named by `ROBINAUTS_DATABASE_URL` and by nothing on a
  command line: a url holds a password, and a command line is a shell
  history. A database that cannot be opened — no server there, no such
  database, credentials refused — is one line naming what the driver said,
  and never the url it was given.
