# The Codex worker

A Robinauts worker that runs its turns on OpenAI's
[Codex](https://github.com/openai/codex), through its app server. It is the
[base worker](../base-worker/README.md) with one package added: the backend's
own worker module, `robinauts.cli.worker_process`, in a container, with the
engine `codex` plugged in through the `robinauts.engines` entry point. Each
turn starts a `codex app-server` and runs one turn of a Codex thread over its
JSON-RPC, in a working directory of the conversation's own. The agent has
Codex's own tools (shell, file edits, web search) and the agent's MCP tool
servers. The conversation's state is Codex's threads, kept in a volume of the
container's.

The server starts it like every worker whose `[workers.<name>]` table names a
command, with `docker run -i` of its image
([docs/specs/operations.md](../../docs/specs/operations.md), "Workers"). It
opens no database, and refuses to start with the database's URL in its
environment.

Read [CREDENTIALS.md](CREDENTIALS.md) first. The worker runs in one of two
modes, and each has its own credential, which **you** make.

## The configuration, on the server

In the server's `robinauts.toml`:

```toml
[model_providers.openai]
kind = "openai"
api_key_env = "OPENAI_API_KEY"   # read inside the container, in deployed mode alone

[models.gpt]
provider = "openai"
name = "gpt-5.5"
title = "GPT-5.5"

[agents.coder]
title = "Codex"
system_prompt = "You work in this conversation's own directory. Say what you did."
models = ["gpt"]
engine = "codex"            # an engine the server does not have: allowed on a worker with a command
worker = "codex"

[workers.codex]
command = [
    "docker", "run", "-i", "--rm", "--network", "host",
    "-e", "ROBINAUTS_CONFIG", "-e", "ROBINAUTS_WORKER_NAME", "-e", "ROBINAUTS_WORKER_BROKER_URL",
    "-e", "ROBINAUTS_CODEX_AUTH=deployed", "-e", "OPENAI_API_KEY",
    "--env-file", "/etc/robinauts/github.env",
    "-e", "ROBINAUTS_FLOWS_DIR", "-v", "/etc/robinauts/flows:/etc/robinauts/flows:ro",
    "-v", "/etc/robinauts/robinauts.toml:/etc/robinauts/robinauts.toml:ro",
    "-v", "robinauts-codex-home:/var/lib/codex",
    "-v", "robinauts-codex-workspaces:/workspace",
    "robinauts-codex-worker",
]
```

The first line of the command and the mount of the configuration are the base
worker's, explained in [its README](../base-worker/README.md). This worker adds:

- `-e ROBINAUTS_CODEX_AUTH=deployed`, or `personal`: the credential mode
  ([CREDENTIALS.md](CREDENTIALS.md)). It has no default. The worker refuses to
  start without it.
- `-e OPENAI_API_KEY`, in deployed mode: the key of the provider the agents'
  models are on, by the name its `api_key_env` gives. Personal mode passes no
  key. Its login is in the home volume.
- The two named volumes: Codex's home, which holds the threads, and the
  conversations' workspaces. Without them nothing outlives the container.
- `-e ROBINAUTS_FLOWS_DIR`, the flows mount and `--env-file`, optional: the
  repositories each session gets a worktree of, and the GitHub token, as for
  every extra worker ([../worker-common/README.md](../worker-common/README.md)).
  Codex inherits the token, so the agent's shell can use it too.
- Not passed: `ROBINAUTS_DATABASE_URL`. The worker refuses to start with it.

Codex speaks OpenAI's Responses API alone. A provider of kind `openai`, or
`openai-compatible` whose endpoint serves `/responses`, can serve this worker.
An endpoint that serves Chat Completions alone cannot: Codex has dropped it.

## Personal use (one person, their own machine)

One person, their own machine, their own ChatGPT plan. The server runs in
oneberth's local mode, with the container beside it.

1. Build the image ("Build").
2. Sign in to Codex yourself, into the home volume, with a device code:

       docker run -it --rm -v robinauts-codex-home:/var/lib/codex \
           --entrypoint codex robinauts-codex-worker login --device-auth

   Open the address it prints and enter the code. Robinauts never runs this
   command for you.
3. Write the tables above into a `robinauts.toml` of yours, with
   `"-e", "ROBINAUTS_CODEX_AUTH=personal"` in place of the deployed mode's two
   flags, and the file's own path in the `-v` mount.
4. Start the server:

       ROBINAUTS_CONFIG=~/robinauts.toml robinauts start --local --port 8000

5. Open the address it prints, pick the agent *Codex*, and hold a conversation.
   Each conversation gets `/workspace/<id>` in the container.

The worker refuses to start without the login, with a provider that is not plain
OpenAI, or with a server that is not on this machine. When OpenAI refuses the
login, a turn fails with an error that says to sign in again.

### On the desktop, with Docker

`robinauts desktop` starts the worker as a container of oneberth's launcher
when its table names the image
([docs/deployment.md](../../docs/deployment.md), "The desktop with Docker"):

```toml
[workers.codex]
image = "robinauts-codex-worker"
environment = ["ROBINAUTS_CODEX_AUTH"]
```

The container sees the worker's folder of the data directory,
`workers/codex`. It keeps Codex's home in `codex/` there and the workspaces in
`workspaces/`. Sign in into that home: run step 2 with
`-v <data directory>/workers/codex/codex:/var/lib/codex` in place of the
named volume.

## A deployment

1. The operator creates an API key with the provider and puts it in
   `/etc/robinauts/environment`, root-owned, mode `0600`, under the name the
   provider's `api_key_env` gives. No ChatGPT login (CREDENTIALS.md).
2. On the server's machine, build the image from a checkout of the **same
   commit** the server's wheel was built from ("Build").
3. Put the tables above in `/etc/robinauts/robinauts.toml`, and restart the
   service. The service's user needs the right to run `docker`.
4. The server's log says `worker 'codex' starts with the command: docker run
   ...`, and `/health` answers `ok` while the worker is heard from.

## Build

From the repository root:

```bash
docker build -f extra-workers/codex-worker/Dockerfile -t robinauts-codex-worker .
```

The image carries the same build of `robinauts` as the server it serves. Rebuild
it when the server is rebuilt. It carries Codex at the version the Dockerfile's
`CODEX_VERSION` pins, the static Linux binary of that release. Bump it with
care: the app server's protocol changes between releases. Codex's analytics,
feedback, OpenTelemetry and update check are off on its command line.

## Configuration

Every knob is an environment variable, passed on with `-e NAME=value`.

| variable | default | meaning |
|---|---|---|
| `ROBINAUTS_CODEX_AUTH` | required | `personal` or `deployed` (CREDENTIALS.md) |
| `ROBINAUTS_CODEX_WORKSPACES` | `/workspace` | one directory per conversation under it |
| `ROBINAUTS_CODEX_SANDBOX` | `danger-full-access` | Codex's sandbox: `read-only`, `workspace-write` or `danger-full-access` |
| `ROBINAUTS_CODEX_SYSTEM_PROMPT` | `preset` | Codex's instructions with the agent's prompt as developer instructions, or `custom` for the agent's alone |
| `ROBINAUTS_CODEX_EFFORT` | the model's | `minimal`, `low`, `medium`, `high`, `xhigh` |
| `ROBINAUTS_CODEX_REASONING_SUMMARY` | `auto` | `auto`, `concise`, `detailed` or `none` |
| `ROBINAUTS_CODEX_CLI_PATH` | `codex` | another Codex to run |
| `CODEX_HOME` | `/var/lib/codex` in the image | where Codex keeps the threads and the login |

**The sandbox.** The container is the sandbox. Codex's own needs user
namespaces, which a container lacks, so the default runs every command with no
sandbox of Codex's. The other two modes work where the host allows them.

**Permissions.** Nobody can answer a question from the chat. So the approval
policy is `never`, MCP tools run without asking, and what Codex asks anyway is
declined. The question tool is off.

**Tools.** The agent's `tools` become Codex's MCP servers over HTTP. Each
server's credential goes in Codex's environment, never on its command line, and
a table's `exclude` becomes the server's disabled tools. The built-in tools of
the server's (the search of a person's conversations) are Codex's dynamic
tools: Codex calls them back over the app server, and the worker runs them.

## What is kept, and what is lost

- **Threads**: Codex's rollouts and state database, under `CODEX_HOME`. They
  are the conversation's memory. The engine lists the threads each conversation
  made in `CODEX_HOME/robinauts-threads/`.
- **Workspaces**: `/workspace/<conversation id>`, the files the agent made.
- The volumes outlive the container and an image rebuild. They do not outlive
  `docker volume rm`. A conversation whose threads are gone refuses its next
  turn, saying so.
- A deleted conversation deletes its threads with Codex's own `thread/delete`,
  newest first, and then its directory.

## How a turn maps onto Codex

A conversation's first turn starts a Codex thread. The checkpoint the server
stores on every answer names the thread and the turn. The next turn resumes the
thread. An edit or a regenerate under an earlier answer forks the thread through
that turn, so the two branches are two threads. A cancelled or failed turn stays
in its thread; the next turn forks before it, which cuts it off. A cancel or the
deadline interrupts the turn, and the app server stops with it.

Before Codex starts, the session's worktrees are made in its directory, as in
the Claude Code worker ([worker-common](../worker-common/README.md),
"Repositories and worktrees" and "A code task").

## Checks

`scripts/check-extra-workers.sh` runs this folder's lint, tests and image build.
The tests run no Codex:

- `tests/test_units.py` covers the pure parts: checkpoints, settings,
  credentials, the command line and the translation of notifications.
- `tests/test_engine.py` runs the engine against `tests/stand_in_codex.py`, a
  stand-in app server with threads kept on disk. One conversation continues,
  forks, calls tools, fails, times out and is forgotten.
- `tests/test_container.py` runs the whole thing. A server with no database
  starts the worker from the image, and the real Codex holds a two-turn
  conversation against `tests/fake_responses.py`, a stand-in of the Responses
  API on this machine. It needs Docker on Linux. It builds the image unless
  `ROBINAUTS_CODEX_WORKER_IMAGE` names one already built.
