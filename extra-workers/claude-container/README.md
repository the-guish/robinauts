# claude-container: a Robinauts worker on Claude Code

A container that owns a conversation. It is a Robinauts
[worker](../../docs/specs/operations.md), composed from the backend's own
`robinauts` package, with one engine plugged in: each turn is a query of the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), which
drives the Claude Code binary it bundles, in a working directory of the
conversation's own, with Claude Code's tools (`Read`, `Edit`, `Bash`, ...) and
the agent's MCP tool servers. The conversation's state is the Claude Code
session, kept as transcripts inside the container's volume.

The server knows it as an **external worker**: a `[workers.<name>]` table of
kind `external` names the environment variable that holds the secret the
worker presents, the server lets it in through its broker API from its start,
spawns nothing and waits for nothing. The worker pulls the turns of its agents
over HTTP, runs them, and writes the events and the answer back, exactly as the
server's own workers do.

Before anything else, read [CREDENTIALS.md](CREDENTIALS.md): the worker runs in
one of two modes, and each has its own credential, which **you** make, never
Robinauts.

## The configuration, on the server

In the server's `robinauts.toml`:

```toml
[workers.claude-container]
kind = "external"
secret_env = "ROBINAUTS_CLAUDE_CONTAINER_SECRET"   # the server reads the secret from here

[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"                   # read inside the container, deployed mode only

[models.opus]
provider = "anthropic"
name = "claude-opus-5-5"
title = "Claude Opus 5.5"

[agents.claude-container]
title = "Claude (container)"
system_prompt = "You work in this conversation's own directory. Say what you did."
model = "opus"
engine = "claude-code"       # an engine the server does not have: allowed on an external worker
worker = "claude-container"
```

The server's environment holds `ROBINAUTS_CLAUDE_CONTAINER_SECRET=<a long random
string>`; the worker is given the same string. The server starts and is healthy
with the container down; the agent's turns wait, queued, until it connects.
The container mounts the same `robinauts.toml`, read-only: it parses the same
file the server does, which is why **the image must be the same build of
`robinauts` as the server** (the wire has no version handshake yet).

## Personal use (Robinauts desktop)

One person, their own machine, their own subscription. The server runs in the
local development mode, the container beside it.

1. Install Claude Code, log in, and make your token in your own terminal:

       claude setup-token

   Copy what it prints. Choose a secret for the worker too, for example
   `openssl rand -base64 32`.
2. In `extra-workers/claude-container/`:

       cp personal.env.example personal.env && chmod 600 personal.env

   and fill in `ROBINAUTS_WORKER_SECRET` and `CLAUDE_CODE_OAUTH_TOKEN`. Nothing
   else: no API key (CREDENTIALS.md, "Personal").
3. Start the server, with the same secret, on loopback:

       ROBINAUTS_CONFIG=~/robinauts.toml ROBINAUTS_CLAUDE_CONTAINER_SECRET=<the secret> \
           robinauts start --dev-no-sign-in --port 8000

4. Start the worker, from the repository root:

       ROBINAUTS_CONFIG_FILE=~/robinauts.toml \
           docker compose -f extra-workers/claude-container/compose.personal.yaml up --build

   The compose file uses the host's network so the container reaches the
   loopback-bound server at `http://127.0.0.1:8000`. On Docker Desktop, set
   `ROBINAUTS_BROKER_URL=http://host.docker.internal:8000` and remove
   `network_mode: host`.
5. Open `http://127.0.0.1:8000/`, pick the agent *Claude (container)*, and hold
   a conversation. Each conversation gets `/workspace/<id>` in the container.

The worker refuses to start if a key is set beside the token, if a provider is
not plain Anthropic, or if the server is not on this machine
(CREDENTIALS.md). The token lasts a year; when Anthropic refuses it, a turn
fails with an error that says to run `claude setup-token` again.

Without Docker, the same package runs as a process on your machine with your
own Claude Code: `uv run python -m robinauts_claude_worker` in this folder,
with the variables of `personal.env` exported, `ROBINAUTS_CLAUDE_CLI_PATH`
pointing at your `claude` if you prefer it to the bundled one, and
`ROBINAUTS_CLAUDE_WORKSPACES` somewhere of yours. The same rules apply.

## A deployment

An instance with sign-in, several people, run as
[docs/deployment.md](../../docs/deployment.md) installs it.

1. The operator creates an API key in the Claude Console and writes it, with
   the worker's secret, to `/etc/robinauts/claude-container.env`, root-owned,
   mode `0600` (`deployed.env.example`). No subscription token
   (CREDENTIALS.md, "Deployed").
2. The server's `/etc/robinauts/environment` gets the same secret under the
   name `secret_env` gives; the `[workers]` and `[agents]` tables above go into
   `/etc/robinauts/robinauts.toml`; restart the service.
3. From a checkout of the **same commit** the server's wheel was built from:

       docker compose -f extra-workers/claude-container/compose.deployed.yaml up --build -d

   The compose file reaches the server at `http://127.0.0.1:8000`, the address
   `robinauts start` binds, over the host's network, so the broker's traffic
   never leaves the machine. Point `ROBINAUTS_BROKER_URL` elsewhere for a
   server on another host, and have the reverse proxy deny `/internal/` from
   the outside: the routes are bearer-protected, but they are not the public's.
4. `docker compose ps` shows the container healthy once the worker has
   claimed, or waited for, work; the server's log says
   `worker 'claude-container' is external` at its start.

## Configuration

Every knob is an environment variable. The four without a default are
required.

| variable | default | meaning |
|---|---|---|
| `ROBINAUTS_CONFIG` | required | the server's TOML, mounted read-only |
| `ROBINAUTS_BROKER_URL` | required | the server's root, as the container reaches it |
| `ROBINAUTS_WORKER_SECRET` or `ROBINAUTS_WORKER_SECRET_FILE` | required | the secret, or a file holding it (a Docker secret) |
| `ROBINAUTS_CLAUDE_AUTH` | required | `personal` or `deployed` (CREDENTIALS.md) |
| `ROBINAUTS_WORKER_NAME` | `claude-container` | the `[workers.<name>]` this is |
| `ROBINAUTS_CLAUDE_WORKSPACES` | `/workspace` | one directory per conversation under it |
| `ROBINAUTS_CLAUDE_PERMISSION_MODE` | `dontAsk` | Claude Code's mode; `bypassPermissions` runs everything |
| `ROBINAUTS_CLAUDE_ALLOWED_TOOLS` | `Read,Edit,Write,Glob,Grep,Bash,WebFetch,WebSearch` | what runs without asking |
| `ROBINAUTS_CLAUDE_DISALLOWED_TOOLS` | `AskUserQuestion,ExitPlanMode` | what is never offered |
| `ROBINAUTS_CLAUDE_SETTING_SOURCES` | none | `user`, `project`, `local`: Claude Code settings files to read |
| `ROBINAUTS_CLAUDE_SYSTEM_PROMPT` | `preset` | Claude Code's prompt with the agent's appended, or `custom` for the agent's alone |
| `ROBINAUTS_CLAUDE_MAX_TURNS` | none | the most agentic turns one Robinauts turn may take |
| `ROBINAUTS_CLAUDE_MAX_BUDGET_USD` | none | stop a turn past this estimate |
| `ROBINAUTS_CLAUDE_EFFORT` | the model's | `low`, `medium`, `high`, `xhigh`, `max` |
| `ROBINAUTS_CLAUDE_PARTIAL_MESSAGES` | `yes` | stream the text as it is written, or per message |
| `ROBINAUTS_CLAUDE_CLI_PATH` | the bundled binary | another Claude Code to run |
| `ROBINAUTS_CLAUDE_ACTIVITY_FILE` | `/tmp/robinauts-claude-worker.alive` | touched at every heartbeat; the health check reads it |
| `CLAUDE_CONFIG_DIR` | `/var/lib/claude` in the image | where Claude Code keeps the transcripts |

The agent's `system_prompt`, `model`, `tools` and `turn_timeout_seconds` come
from the server's configuration, as for every agent. Its `tools` become MCP
servers over HTTP for Claude Code, with the credential each table's `auth`
implies, and a table's `exclude` becomes tools Claude Code is denied. The
built-in tools of the server's (the search of a person's conversations) are
offered to Claude Code as one in-process MCP server, `robinauts`.

**Permissions.** Nobody can answer a question from the chat, so the default
mode is `dontAsk`: what `ROBINAUTS_CLAUDE_ALLOWED_TOOLS` names runs, anything
else is denied with a message the model sees, and nothing waits for a person.
`AskUserQuestion` and `ExitPlanMode` are disallowed for the same reason. A
channel for Claude Code's questions through the chat is later work.

## What is kept, and what is lost

- **Transcripts**: Claude Code's, under `CLAUDE_CONFIG_DIR/projects/`, one
  directory per conversation's workspace. They are the conversation's memory.
  The answer the person reads, and every tool call shown in the chat, are in
  the server's database as for any engine.
- **Workspaces**: `/workspace/<conversation id>`, the files the agent made.
- Both are volumes (`claude-sessions`, `claude-workspaces`). They outlive a
  `docker compose down` and an image rebuild; they do not outlive
  `docker volume rm`, and a container run without them keeps nothing. A
  conversation whose transcripts are gone refuses its next turn, saying so,
  and a new conversation starts from nothing. Durable, shared transcripts (the
  SDK's session store on PostgreSQL) are later work.

## How a turn maps onto Claude Code

A conversation is one Claude Code session, started on its first turn. The
checkpoint the server stores on every answer names the session and the message
the turn ended on. The next turn resumes the session. An edit or a regenerate
under an earlier answer resumes the session *at that message* and forks it, so
the two branches are two sessions and neither loses anything. A cancelled or
failed turn leaves what it wrote; the next turn resumes before it, which cuts
it off. The server's `turn_timeout_seconds`, stall detection, cancels and
leases work as for every engine, since the worker is the server's own.

## Checks

`scripts/check-extra-workers.sh` runs this folder's lint, tests and, where
Docker is present, the image build; CI runs it on every pull request that
touches `backend/` or `extra-workers/`. The tests run no Claude Code process:
the SDK is exercised against hand-built messages, and the engine's decisions
(new session, resume, fork, refuse) against a stand-in transcript.
