# Robinauts in Docker

One command builds the images and starts Robinauts in three containers, and one takes them
down again:

    docker/start.sh          # build, start, and wait until it answers
    docker/stop.sh           # down again; the conversations stay
    docker/stop.sh --reset   # and delete the database's volume

`start.sh` prints `http://127.0.0.1:8000/` to open.

## The three containers

| container | what it is | networks |
|---|---|---|
| `api` | the control plane: the web server, the interface, the API, the worker API, and the server's own worker | `data`, `front`, `sandbox` |
| `sandbox` | the sandbox worker: it runs the turns of **every sandboxed agent** | `sandbox` |
| `postgres` | PostgreSQL 17, with its data in the `postgres` volume | `data` |

A conversation with a **sandboxed** agent crosses containers on every turn. The control plane
queues the turn in PostgreSQL. The sandbox worker claims it over HTTP, through the control
plane's worker API, runs the engine, and sends each event back the same way. The browser reads
those events from the control plane as it reads any turn's
([../docs/architecture/sandbox-workers.md](../docs/architecture/sandbox-workers.md)).
**The sandbox never reaches the database**: `data` is an internal network that only the
control plane and PostgreSQL are on.

The configuration, [robinauts.toml](robinauts.toml), has one agent of each kind:

- **Assistant**: its turns run in the control plane's own worker, with its memory in
  PostgreSQL, as in any deployment.
- **Coder (sandboxed)**: its turns run in the `sandbox` container, with GitHub's MCP server as
  its tools. Its engine's memory of each conversation is in the sandbox worker's process.

## What it needs

Docker with Compose v2 (`docker compose`), on Linux or macOS, and:

- **a model provider key**: Anthropic's by default, or OpenRouter's or OpenAI's after an edit
  of [robinauts.toml](robinauts.toml), which says what to change;
- **a GitHub token** for the coder, ideally fine-grained and limited to the repositories it may
  work on. Without one, the coder's turns fail until it is set.

Both go in `docker/.env`, which is never committed:

    cp docker/.env.example docker/.env
    chmod 600 docker/.env
    $EDITOR docker/.env      # ANTHROPIC_API_KEY=..., ROBINAUTS_GITHUB_TOKEN=...
    docker/start.sh

`start.sh` reads `docker/.env` only when it is mode 0600. On its first run it writes two values
of its own there: a random `ROBINAUTS_SANDBOX_TOKEN`, which the sandbox worker presents to the
worker API on every call, and a random `ROBINAUTS_POSTGRES_PASSWORD`. Each value reaches only
the containers that use it ([compose.yaml](compose.yaml)):

| value | `api` | `sandbox` | `postgres` |
|---|---|---|---|
| the model provider key | yes | yes | |
| `ROBINAUTS_GITHUB_TOKEN` | | yes | |
| `ROBINAUTS_SANDBOX_TOKEN` | yes | yes | |
| `ROBINAUTS_POSTGRES_PASSWORD` | yes | | yes |

`docker/.env` is left out of every image build (`Dockerfile.dockerignore`), and no key is
written into an image or into the configuration: the configuration names each variable, and
the container's environment holds its value.

## Sign-in

None. The control plane runs the **local development mode**: one fixed local user, no sign-in,
and a banner in the interface that says so ([../docs/specs/sign-in.md](../docs/specs/sign-in.md)).
Inside a container it has to bind the container's interfaces rather than its loopback, and it
is told so explicitly, with `--dev-container`. What keeps it on this machine is the port, which
[compose.yaml](compose.yaml) publishes on the host's loopback alone (`127.0.0.1:8000`).

This is not a deployment. For one, see [../docs/deployment.md](../docs/deployment.md).

## Conversations across containers

- **A follow-up continues from the sandbox's memory.** The sandbox that ran a conversation's
  first turn holds it from then on, and every later turn of it is claimed by that sandbox
  alone.
- **The control plane can restart.** The sandbox worker retries the worker API until it
  answers again, with its memory intact, and its conversations continue.
- **The sandbox cannot.** Its engines' memory is in its process. When the sandbox container
  stops or restarts, its conversations become **read-only** once it has gone unheard for
  `[sandbox] gone_after_seconds` (120 by default). They keep every message, and the interface
  says the sandbox is gone. A new conversation with the coder starts in the new sandbox.
- **A cancel** reaches the sandbox with its next lease renewal, every `[work]
  heartbeat_seconds` (30 by default).

## Isolation, before OpenShell

This is the first step towards the sandboxed agents of the target architecture, with no
OpenShell yet ([../docs/architecture/sandbox-workers.md](../docs/architecture/sandbox-workers.md),
"What this step does not have yet"). The sandbox container is kept apart from the rest by what
Docker offers:

- it is on the `sandbox` network alone, so it reaches the control plane and the internet, and
  not PostgreSQL;
- its requests reach the worker API and nothing else of the control plane's. A container
  network lets it reach every port, so the control plane refuses everything else from the
  sandbox's address (`--refuse-host sandbox`);
- the worker API checks everything it sends: which sessions, which events, their form, order
  and size;
- it runs as an unprivileged `sandbox` user.

What it does not have: the sandbox holds the model provider's key and the GitHub token in its
environment, its sessions share one process and one filesystem, its way out to the internet
is not filtered, and the workers' token is one for all of them. Give it a GitHub token you
would give the agent itself.

## When something goes wrong

    cd docker
    docker compose logs -f api sandbox

- **`start.sh` says the containers did not start**: `docker compose logs api` says why. A
  configuration the control plane refuses is one line there, naming the key.
- **Every coder turn fails**: no `ROBINAUTS_GITHUB_TOKEN`, or one GitHub refuses. The sandbox's
  log names it.
- **"This conversation's sandbox has been deleted"**: the sandbox container stopped or
  restarted since the conversation began. Start a new one.
- **Port 8000 is taken**: set `ROBINAUTS_PORT` in `docker/.env`.
