# The OpenCode worker

A Robinauts worker that runs its turns on [OpenCode](https://opencode.ai),
through the HTTP server it serves. It is the
[base worker](../base-worker/README.md) with one package added: the backend's
own worker module, `robinauts.cli.worker_process`, in a container, with the
engine `opencode` plugged in through the `robinauts.engines` entry point. The
worker starts one `opencode serve` on loopback, and each turn is one prompt of
an OpenCode session, in a directory of the conversation's own. The agent has
OpenCode's own tools (`bash`, `edit`, `read`, `grep`, ...) and the agent's MCP
tool servers. The conversation's state is OpenCode's sessions, kept in a
volume of the container's.

The server starts it like every worker whose `[workers.<name>]` table names a
command, with `docker run -i` of its image
([docs/specs/operations.md](../../docs/specs/operations.md), "Workers"). It
opens no database, and refuses to start with the database's URL in its
environment.

The worker runs on API keys alone ([CREDENTIALS.md](CREDENTIALS.md)). Any
provider kind of Robinauts' can serve it: `anthropic`, `anthropic-compatible`,
`openai` and `openai-compatible`.

## The configuration, on the server

In the server's `robinauts.toml`:

```toml
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"   # read inside the container

[models.sonnet]
provider = "anthropic"
name = "claude-sonnet-5-5"
title = "Claude Sonnet 5.5"

[agents.builder]
title = "OpenCode"
system_prompt = "You work in this conversation's own directory. Say what you did."
models = ["sonnet"]
engine = "opencode"         # an engine the server does not have: allowed on a worker with a command
worker = "opencode"

[workers.opencode]
command = [
    "docker", "run", "-i", "--rm", "--network", "host",
    "-e", "ROBINAUTS_CONFIG", "-e", "ROBINAUTS_WORKER_NAME", "-e", "ROBINAUTS_WORKER_BROKER_URL",
    "-e", "ANTHROPIC_API_KEY",
    "--env-file", "/etc/robinauts/github.env",
    "-e", "ROBINAUTS_FLOWS_DIR", "-v", "/etc/robinauts/flows:/etc/robinauts/flows:ro",
    "-v", "/etc/robinauts/robinauts.toml:/etc/robinauts/robinauts.toml:ro",
    "-v", "robinauts-opencode-state:/var/lib/opencode",
    "-v", "robinauts-opencode-workspaces:/workspace",
    "robinauts-opencode-worker",
]
```

The first line of the command and the mount of the configuration are the base
worker's, explained in [its README](../base-worker/README.md). This worker adds:

- `-e ANTHROPIC_API_KEY`: the key of each provider the agents' models are on,
  by the name its `api_key_env` gives.
- The two named volumes: OpenCode's state, which holds the sessions, and the
  conversations' workspaces. Without them nothing outlives the container.
- `-e ROBINAUTS_FLOWS_DIR`, the flows mount and `--env-file`, optional: the
  repositories each session gets a worktree of, and the GitHub token, as for
  every extra worker ([../worker-common/README.md](../worker-common/README.md)).
  OpenCode inherits the token, so the agent's `bash` can use it too.
- Not passed: `ROBINAUTS_DATABASE_URL`. The worker refuses to start with it.

On the desktop, `robinauts desktop` starts the worker as a container of
oneberth's launcher when its table names the image
([docs/deployment.md](../../docs/deployment.md), "The desktop with Docker"):

```toml
[workers.opencode]
image = "robinauts-opencode-worker"
environment = ["ANTHROPIC_API_KEY"]
```

The container then keeps OpenCode's state in `opencode/` of the worker's folder
of the data directory, `workers/opencode`, and the workspaces in `workspaces/`.

## Build

From the repository root:

```bash
docker build -f extra-workers/opencode-worker/Dockerfile -t robinauts-opencode-worker .
```

The image carries the same build of `robinauts` as the server it serves. Rebuild
it when the server is rebuilt.

## Versions

The image carries OpenCode at the version the Dockerfile's `OPENCODE_VERSION`
pins, the Linux binary of that release. The worker speaks the HTTP API of the
1.x line: `/session`, `/event` and the rest. OpenCode 2.x serves another API,
under `/api`, and this worker does not speak it yet. Keep the pin on 1.x.

## Configuration

Every knob is an environment variable, passed on with `-e NAME=value`.

| variable | default | meaning |
|---|---|---|
| `ROBINAUTS_OPENCODE_WORKSPACES` | `/workspace` | one directory per conversation under it |
| `ROBINAUTS_OPENCODE_STATE` | `~/.local/share/robinauts-opencode` | the engine's own files: which sessions each conversation made |
| `ROBINAUTS_OPENCODE_MAX_STEPS` | OpenCode's | the most model calls one turn may take |
| `ROBINAUTS_OPENCODE_START_SECONDS` | `30` | how long `opencode serve` has to say it listens |
| `ROBINAUTS_OPENCODE_CLI_PATH` | `opencode` | another OpenCode to run |
| `XDG_DATA_HOME` | `/var/lib/opencode/data` in the image | where OpenCode keeps its sessions |

**The server.** The worker starts `opencode serve` at its own start. It listens
on loopback, behind a password the worker makes at every start. The worker
starts it again if it stops. Its configuration is built from the server's
configuration, in memory, and handed in `OPENCODE_CONFIG_CONTENT`: the
providers with their keys, every tool server, and one agent, `robinauts`. A
repository's own `opencode.json` is not read.

**The prompt.** OpenCode's own system prompt comes first, and the agent's
`system_prompt` is appended to it on every turn.

**Permissions.** Nobody can answer a question from the chat. So every tool runs
without asking, and the question tool is off.

**Tools.** Every configured tool server is an MCP server of OpenCode's. Each
turn sets the session's rules: the agent's `tools` are allowed, every other
server's tools are denied, and so is every tool a table's `exclude` names. The
built-in tools of the server's (the search of a person's conversations) are
served by the worker itself, as an MCP server on loopback
([../worker-common/README.md](../worker-common/README.md)). Each turn adds it
to its directory, under a token of the turn's.

**Nothing phones home.** OpenCode starts with its models fetch, update check,
web interface proxy, sharing and login plugins off. It may still try to
install its plugin package in the background at its start; offline, that fails
quietly.

## What is kept, and what is lost

- **Sessions**: OpenCode's SQLite database, under `XDG_DATA_HOME`. They are the
  conversation's memory. The engine lists the sessions each conversation made
  in `ROBINAUTS_OPENCODE_STATE/sessions/`.
- **Workspaces**: `/workspace/<conversation id>`, the files the agent made.
- The volumes outlive the container and an image rebuild. They do not outlive
  `docker volume rm`. A conversation whose sessions are gone refuses its next
  turn, saying so.
- A deleted conversation deletes its sessions, newest first, and then its
  directory.

## How a turn maps onto OpenCode

A conversation's first turn creates an OpenCode session. The checkpoint the
server stores on every answer names the session and the turn's last assistant
message. The next turn prompts the same session. An edit or a regenerate under
an earlier answer forks the session, keeping the messages up to that answer, so
the two branches are two sessions. A cancelled or failed turn stays in its
session; the next turn forks before it, which cuts it off. A cancel or the
deadline aborts the session's run.

Before OpenCode runs, the session's worktrees are made in its directory, as in
the Claude Code worker ([worker-common](../worker-common/README.md),
"Repositories and worktrees" and "A code task").

## Checks

`scripts/check-extra-workers.sh` runs this folder's lint, tests and image build.
The tests run no OpenCode:

- `tests/test_units.py` covers the pure parts: checkpoints, settings,
  credentials, the configuration, the rules and the translation of events.
- `tests/test_engine.py` runs the engine against `tests/stand_in_opencode.py`,
  a stand-in server with the same API, password and event stream. One
  conversation continues, forks, calls tools, fails, times out and is
  forgotten.
- `tests/test_container.py` runs the whole thing. A server with no database
  starts the worker from the image, and the real OpenCode holds a two-turn
  conversation against `tests/fake_chat.py`, a stand-in of a Chat Completions
  endpoint on this machine. It needs Docker on Linux. It builds the image unless
  `ROBINAUTS_OPENCODE_WORKER_IMAGE` names one already built.
