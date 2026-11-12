# The Claude Code worker

A Robinauts worker that runs its turns on Claude Code, through the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview). It is
the [base worker](../base-worker/README.md) with one package added: the
backend's own worker module, `robinauts.cli.worker_process`, in a container,
with the engine `claude-code` plugged in through the `robinauts.engines` entry
point. Each turn is a query of the SDK, which drives the Claude Code binary it
bundles, in a working directory of the conversation's own, with Claude Code's
tools (`Read`, `Edit`, `Bash`, ...) and the agent's MCP tool servers. The
conversation's state is the Claude Code session, kept as transcripts in a
volume of the container's.

The server starts it like every worker whose `[workers.<name>]` table names a
command, with `docker run -i` of its image, hands it the turns of its agents
over HTTP, and knows nothing else about it
([docs/specs/operations.md](../../docs/specs/operations.md), "Workers"). It
opens no database, and refuses to start with the database's URL in its
environment.

Before anything else, read [CREDENTIALS.md](CREDENTIALS.md): the worker runs in
one of two modes, and each has its own credential, which **you** make, never
Robinauts.

## The configuration, on the server

In the server's `robinauts.toml`:

```toml
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"   # read inside the container, in deployed mode alone

[models.opus]
provider = "anthropic"
name = "claude-opus-5-5"
title = "Claude Opus 5.5"

[agents.coder]
title = "Claude Code"
system_prompt = "You work in this conversation's own directory. Say what you did."
models = ["opus"]
engine = "claude-code"       # an engine the server does not have: allowed on a worker with a command
worker = "claude"

[workers.claude]
command = [
    "docker", "run", "-i", "--rm", "--network", "host",
    "-e", "ROBINAUTS_CONFIG", "-e", "ROBINAUTS_WORKER_NAME", "-e", "ROBINAUTS_WORKER_BROKER_URL",
    "-e", "ROBINAUTS_CLAUDE_AUTH=deployed", "-e", "ANTHROPIC_API_KEY",
    "--env-file", "/etc/robinauts/github.env",
    "-e", "ROBINAUTS_FLOWS_DIR", "-v", "/etc/robinauts/flows:/etc/robinauts/flows:ro",
    "-v", "/etc/robinauts/robinauts.toml:/etc/robinauts/robinauts.toml:ro",
    "-v", "robinauts-claude-sessions:/var/lib/claude",
    "-v", "robinauts-claude-workspaces:/workspace",
    "robinauts-claude-code-worker",
]
```

The first line of the command and the mount of the configuration are the base
worker's, explained in [its README](../base-worker/README.md). What this
worker adds:

- `-e ROBINAUTS_CLAUDE_AUTH=deployed`, or `personal`: the credential mode
  ([CREDENTIALS.md](CREDENTIALS.md)), chosen in the configuration, where the
  operator reads it. There is no default; the worker refuses to start without
  it.
- `-e ANTHROPIC_API_KEY`, in deployed mode: passes on the key of the provider
  the agents' models are on, by the name its `api_key_env` gives, from the
  server's environment, where the server's own agents on Anthropic read it
  too. In personal mode, `-e CLAUDE_CODE_OAUTH_TOKEN` passes on the token
  instead, from the shell that started the server. A `--env-file` names a file
  of the worker's own for either.
- The two named volumes: Claude Code's transcripts, the conversations' memory,
  and the conversations' workspaces. Without them nothing outlives the
  container ("What is kept, and what is lost").
- Every knob of "Configuration" goes in the same way, `-e NAME=value`.
- `-e ROBINAUTS_FLOWS_DIR` and `-v /etc/robinauts/flows:/etc/robinauts/flows:ro`,
  optional: the flows' settings, whose `[repositories]` each session gets a
  worktree of, in its directory, on the branch `rb/<session id>`, before its
  agent starts; the clones are under `/workspace/repos`
  ([../worker-common/README.md](../worker-common/README.md), "Repositories and
  worktrees").
- `--env-file /etc/robinauts/github.env`, optional: `GITHUB_TOKEN`, for the
  worker's own clones (`robinauts_worker_common.clone`), checked at the start
  like every extra worker's
  ([../worker-common/README.md](../worker-common/README.md)). Claude Code
  inherits it, so the agent's `Bash` and `git` can use it too; keeping it from
  the agent is listed under that README's "Enhancements".
- Not passed, and not to be: `ROBINAUTS_DATABASE_URL`. The worker refuses to
  start with it.

The server starts and the container with it; a worker that refuses to start
says why on the server's standard error, every problem at once, and the server
does not start either. The container's name is Docker's to pick; add `--name`
to choose one.

## Personal use (one person, their own machine)

One person, their own machine, their own subscription. The server runs in the
local development mode, the container beside it.

1. Install Claude Code, log in, and make your token in your own terminal:

       claude setup-token

   Copy what it prints. Robinauts never runs this command for you.
2. Build the image, from a checkout of the repository
   ("Build").
3. Write the tables above into a `robinauts.toml` of yours, with
   `"-e", "ROBINAUTS_CLAUDE_AUTH=personal", "-e", "CLAUDE_CODE_OAUTH_TOKEN"`
   in place of the deployed mode's two flags, and the file's own path in the
   `-v` mount.
4. Start the server, with the token exported in that shell and nothing else
   that authenticates (CREDENTIALS.md, "Personal"):

       export CLAUDE_CODE_OAUTH_TOKEN=<what claude setup-token printed>
       ROBINAUTS_CONFIG=~/robinauts.toml robinauts start --dev-no-sign-in --port 8000

   The server starts the container, waits for it to say `ready`, and listens.
5. Open `http://127.0.0.1:8000/`, pick the agent *Claude Code*, and hold a
   conversation. Each conversation gets `/workspace/<id>` in the container.

The worker refuses to start if a key is set beside the token, if a provider is
not plain Anthropic, or if the server is not on this machine: the broker URL
the server hands it is on loopback, which is. The token lasts a year; when
Anthropic refuses it, a turn fails with an error that says to run
`claude setup-token` again. On Docker Desktop, where there is no host network,
see the base worker's README: the server must listen on an address the
container reaches, and `-e ROBINAUTS_WORKER_BROKER_URL=http://host.docker.internal:8000`
names it, which the worker still counts as this machine.

Without Docker, the same package runs as a process on your machine with your
own Claude Code: a command of
`["uv", "run", "--project", "<this folder>", "python", "-m", "robinauts_claude_worker"]`,
with `ROBINAUTS_CLAUDE_AUTH`, `ROBINAUTS_CLAUDE_WORKSPACES` somewhere of
yours, and `ROBINAUTS_CLAUDE_CLI_PATH` pointing at your `claude` if you prefer
it to the bundled one, exported where the server starts. The same rules apply.

## A deployment

An instance with sign-in, several people, run as
[docs/deployment.md](../../docs/deployment.md) installs it.

1. The operator creates an API key in the Claude Console and puts it in
   `/etc/robinauts/environment`, root-owned, mode `0600`, under the name the
   provider's `api_key_env` gives, as for the server's own agents on
   Anthropic; or in a file of the worker's own that `--env-file` names. No
   subscription token (CREDENTIALS.md, "Deployed").
2. On the server's machine, from a checkout of the **same commit** the
   server's wheel was built from, build the image ("Build").
3. The tables above go into `/etc/robinauts/robinauts.toml`; restart the
   service. The server starts the container as the service's user, which
   needs the right to run `docker`.
4. The server's log says `worker 'claude' starts with the command: docker run
   ...` at its start, and `/health` answers `ok` while the worker is heard
   from. A worker that refused to start left its reasons in the same log.

## Build

From the repository root, so that the backend is installed into the image from
this checkout:

```bash
docker build -f extra-workers/claude-code-worker/Dockerfile -t robinauts-claude-code-worker .
```

The image carries the same build of `robinauts` as the server it serves: the
two parse the same configuration file and speak a wire with no version
handshake yet. Rebuild it when the server is rebuilt. It sends nothing but the
turns to Anthropic: Claude Code's telemetry, error reports and update checks
are off in the image (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`).

## Configuration

Every knob is an environment variable, passed on by the command with
`-e NAME=value`. The one without a default is required.

| variable | default | meaning |
|---|---|---|
| `ROBINAUTS_CLAUDE_AUTH` | required | `personal` or `deployed` (CREDENTIALS.md) |
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
| `CLAUDE_CONFIG_DIR` | `/var/lib/claude` in the image | where Claude Code keeps the transcripts |

The server's own variables, `ROBINAUTS_CONFIG`, `ROBINAUTS_WORKER_NAME` and
`ROBINAUTS_WORKER_BROKER_URL`, are the worker module's to read, as for every
worker; the command passes them on by name. The agent's `system_prompt`,
`model`, `tools` and `turn_timeout_seconds` come from the server's
configuration, as for every agent. Its `tools` become MCP servers over HTTP
for Claude Code, with the credential each table's `auth` implies, and a
table's `exclude` becomes tools Claude Code is denied. The built-in tools of
the server's (the search of a person's conversations) are offered to Claude
Code as one in-process MCP server, `robinauts`. Every agent of this worker runs
on `claude-code`: the credential checks cover that engine alone, so an agent on
another engine gets another worker, the base worker say.

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
- Both are volumes in the command above. They outlive the container, which
  `--rm` removes at every stop, and an image rebuild; they do not outlive
  `docker volume rm`, and a command without them keeps nothing past the
  container. A conversation whose transcripts are gone refuses its next turn,
  saying so, and a new conversation starts from nothing. Durable, shared
  transcripts (the SDK's session store on PostgreSQL) are later work.

## How a turn maps onto Claude Code

A conversation is one Claude Code session, started on its first turn. The
checkpoint the server stores on every answer names the session and the message
the turn ended on. The next turn resumes the session. An edit or a regenerate
under an earlier answer resumes the session *at that message* and forks it, so
the two branches are two sessions and neither loses anything. A cancelled or
failed turn leaves what it wrote; the next turn resumes before it, which cuts
it off. The server's `turn_timeout_seconds`, stall detection, cancels and
leases work as for every engine, since the worker is the server's own.

Before Claude Code starts, the session's worktrees are made in its directory
([worker-common](../worker-common/README.md), "Repositories and worktrees"):
at its first turn, of every repository of the flows' settings, or, when the
question carries a `code_task/1` task payload, of the repositories it names,
each from its branch ("A code task" there). A code task this worker cannot set
up ends the turn with the reason, before the agent starts. The agent sees the
question's text alone: the payload is the worker's.

## Checks

`scripts/check-extra-workers.sh` runs this folder's lint, tests and, where
Docker is present, the image build; CI runs it on every pull request that
touches `backend/` or `extra-workers/`. The unit tests run no Claude Code
process: the SDK is exercised against hand-built messages, and the engine's
decisions (new session, resume, fork, refuse) against a stand-in transcript.
`tests/test_container.py` runs the whole thing: a server with no database
starts the worker from the image, and Claude Code in the container holds a
two-turn conversation against a stand-in of Anthropic's Messages API on this
machine, in deployed mode on a key that is nothing but set. It needs Docker on
Linux and is skipped elsewhere; it builds the image unless
`ROBINAUTS_CLAUDE_CODE_WORKER_IMAGE` names one already built.
