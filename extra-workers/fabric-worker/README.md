# The Fabric worker

A Robinauts worker that runs its turns on Claude Code through
[NVIDIA NeMo Fabric](https://docs.nvidia.com/nemo/fabric), the integration
layer that puts one interface in front of many agent harnesses. It is the
[base worker](../base-worker/README.md) with one package added: the backend's
own worker module, `robinauts.cli.worker_process`, in a container, with the
engine `fabric` plugged in through the `robinauts.engines` entry point. Each
conversation is one Fabric runtime of the Claude adapter (`nvidia.fabric.claude`),
which drives Claude Code through the Claude Agent SDK, in a working directory
of the conversation's own, with Claude Code's tools (`Read`, `Edit`, `Bash`, ...)
and the agent's MCP tool servers.

The server starts it like every worker whose `[workers.<name>]` table names a
command, hands it the turns of its agents over HTTP, and knows nothing else
about it ([docs/specs/operations.md](../../docs/specs/operations.md),
"Workers"). It opens no database, and refuses to start with the database's URL
in its environment.

Fabric 0.7 is pre-1.0 and changes from release to release: the versions are
pinned exactly, and an upgrade is a change of its own, checked by this
folder's tests.

## How it differs from the Claude Code worker

Both run Claude Code. [claude-code-worker/](../claude-code-worker/README.md)
drives the Claude Agent SDK itself; this worker goes through Fabric, so that
the same engine can later reach the other harnesses Fabric adapts. Through
Fabric, today:

- **The answer comes at the end of the turn.** Fabric's Claude adapter returns
  a turn whole: the chat shows "working" until it ends, and then the answer,
  every tool call and every result, at once. Fabric's streaming goes through
  NeMo Relay, its telemetry, which this worker does not install.
- **The conversation's memory lives in a running process.** The adapter keeps
  it in the runtime's Claude Code process and resumes nothing after it ends.
  So the engine keeps what was asked and answered in a log of its own, and a
  turn that does not continue the live runtime starts a new one, seeded with
  the conversation up to its checkpoint, as text in the system prompt (below).
- **Only an API key.** No personal mode with a subscription token: the adapter
  passes Claude Code no variable it does not name.
- **No in-process tools.** The tools the server runs in its own process (the
  search of a person's conversations) are not offered: the adapter runs Claude
  Code from a process of Fabric's.
- **No worktrees of the flows' repositories**, and no `code_task/1` payloads.

## The configuration, on the server

In the server's `robinauts.toml`:

```toml
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"   # read inside the container

[models.opus]
provider = "anthropic"
name = "claude-opus-5-5"
title = "Claude Opus 5.5"

[agents.fabric-coder]
title = "Claude through Fabric"
system_prompt = "You work in this conversation's own directory. Say what you did."
models = ["opus"]
engine = "fabric"            # an engine the server does not have: allowed on a worker with a command
worker = "fabric"

[workers.fabric]
command = [
    "docker", "run", "-i", "--rm", "--network", "host",
    "-e", "ROBINAUTS_CONFIG", "-e", "ROBINAUTS_WORKER_NAME", "-e", "ROBINAUTS_WORKER_BROKER_URL",
    "-e", "ANTHROPIC_API_KEY",
    "-v", "/etc/robinauts/robinauts.toml:/etc/robinauts/robinauts.toml:ro",
    "-v", "robinauts-fabric-claude:/var/lib/claude",
    "-v", "robinauts-fabric-state:/var/lib/robinauts-fabric",
    "-v", "robinauts-fabric-workspaces:/workspace",
    "robinauts-fabric-worker",
]
```

The first line of the command and the mount of the configuration are the base
worker's, explained in [its README](../base-worker/README.md). What this
worker adds:

- `-e ANTHROPIC_API_KEY`: passes on the key of the provider the agents' models
  are on, by the name its `api_key_env` gives, from the server's environment.
  The operator makes the key in the Claude Console; Robinauts never makes one.
  A provider of kind `anthropic-compatible`, with its `base_url`, works the
  same way.
- The three named volumes: Claude Code's configuration, the engine's turn log,
  and the conversations' workspaces. Without the state volume, a conversation
  cannot continue after the container stops ("What is kept, and what is lost").
- Every knob of "Configuration" goes in the same way, `-e NAME=value`.
- Not passed, and not to be: `ROBINAUTS_DATABASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`,
  `ROBINAUTS_FLOWS_DIR`. The worker refuses to start with any of them.

The server starts and the container with it; a worker that refuses to start
says why on the server's standard error, every problem at once, and the server
does not start either.

Without Docker, the same package runs as a process on the server's machine: a
command of `["uv", "run", "--project", "<this folder>", "python", "-m",
"robinauts_fabric_worker"]`, with `ROBINAUTS_FABRIC_WORKSPACES` and
`ROBINAUTS_FABRIC_STATE` somewhere of yours. `tests/test_server.py` runs it so.

On the desktop, `robinauts desktop` starts the image as a container of
oneberth's launcher when the worker's table names it
(`image = "robinauts-fabric-worker"`, `environment = ["ANTHROPIC_API_KEY"]`); the
three folders are then `claude/`, `state/` and `workspaces/` in the worker's
folder of the data directory.

## Build

From the repository root, so that the backend is installed into the image from
this checkout:

```bash
docker build -f extra-workers/fabric-worker/Dockerfile -t robinauts-fabric-worker .
```

The image carries the same build of `robinauts` as the server it serves; rebuild
it when the server is rebuilt. It sends nothing but the turns to Anthropic:
NeMo Relay, Fabric's telemetry, is not installed, Fabric's provenance records
are off, and the engine hands Claude Code
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, which turns off its telemetry,
error reports and update checks.

## Configuration

Every knob is an environment variable, passed on by the command with
`-e NAME=value`.

| variable | default | meaning |
|---|---|---|
| `ROBINAUTS_FABRIC_WORKSPACES` | `/workspace` | one directory per conversation under it |
| `ROBINAUTS_FABRIC_STATE` | `/var/lib/robinauts-fabric` | the turn log, and Fabric's runtime folders |
| `ROBINAUTS_FABRIC_PERMISSION_MODE` | `dontAsk` | Claude Code's mode; `bypassPermissions` runs everything |
| `ROBINAUTS_FABRIC_ALLOWED_TOOLS` | `Read,Edit,Write,Glob,Grep,Bash,WebFetch,WebSearch` | what runs without asking |
| `ROBINAUTS_FABRIC_DISALLOWED_TOOLS` | `AskUserQuestion,ExitPlanMode` | what is never offered |
| `ROBINAUTS_FABRIC_SYSTEM_PROMPT` | `preset` | Claude Code's prompt with the agent's appended, or `custom` for the agent's alone |
| `ROBINAUTS_FABRIC_MAX_TURNS` | none | the most agentic turns one Robinauts turn may take |
| `ROBINAUTS_FABRIC_MAX_BUDGET_USD` | none | stop a turn past this estimate |
| `ROBINAUTS_FABRIC_EFFORT` | the model's | `low`, `medium`, `high`, `xhigh`, `max` |
| `ROBINAUTS_FABRIC_MAX_RUNTIMES` | `4` | live runtimes, each a Claude Code process, kept between turns |
| `ROBINAUTS_FABRIC_IDLE_SECONDS` | `900` | a runtime unused this long is stopped at the next turn |
| `CLAUDE_CONFIG_DIR` | `/var/lib/claude` in the image | Claude Code's own configuration |
| `ADAPTER_PYTHON` | the image's environment | the interpreter Fabric runs its adapter host with |

The agent's `system_prompt`, `model`, `tools` and `turn_timeout_seconds` come
from the server's configuration, as for every agent. Its `tools` become MCP
servers over HTTP for Claude Code, with the credential each table's `auth`
implies, and a table's `exclude` becomes tools Claude Code is denied.

**Permissions.** Nobody can answer a question from the chat, so the default
mode is `dontAsk`: what `ROBINAUTS_FABRIC_ALLOWED_TOOLS` names runs, and so does
every tool of the agent's tool servers; anything else is denied with a message
the model sees. Fabric's own tool list names tools one by one, and a tool
server's tools are not known ahead, so the rules are Claude Code's own: the
engine writes them to `.claude/settings.local.json` in the conversation's
workspace before each runtime starts, and that file is the one settings source
the runtime reads.

**Secrets.** Fabric hands its configuration to its adapter host as JSON, so no
secret is in it. The model's key and each tool server's header go in the
worker's environment under names of its own (`ROBINAUTS_FABRIC_SECRET_...`),
which the adapter host inherits; the configuration names the key's variable,
and a header is a `${VARIABLE}` reference that the Claude adapter resolves into
Claude Code's environment. Nothing is written to a log.

## How a turn maps onto Fabric

A conversation is one Fabric runtime, started at its first turn and kept alive
between turns; a turn is one `invoke` of it, which Claude Code answers with the
memory of every turn before it on that runtime. Each finished turn is written to
the conversation's file in the turn log, with what it asked, what it answered
and the events it showed; the checkpoint the server stores on the answer is that
turn's id.

A turn whose checkpoint is the live runtime's latest answer, on the same model
and agent definition, is invoked on it. Every other turn stops the live runtime
and starts a new one whose system prompt carries the conversation up to the
checkpoint, as text: what each turn asked and answered. That covers:

- an edit or a regenerate under an earlier answer: the new branch knows the
  turns above it and nothing after;
- another model picked in the middle of a conversation;
- a runtime stopped by the worker's restart, its idle time, or the cap on live
  runtimes.

A seeded runtime does not carry the earlier turns' tool calls; their effects are
in the workspace, which the turns of a conversation share. A turn that fails,
times out or is cancelled stops its runtime and is not written: the next turn
starts again from its checkpoint. A resumed turn that had finished gives its
answer again, from the log. The server's `turn_timeout_seconds`, stall
detection, cancels and leases work as for every engine, since the worker is the
server's own.

## What is kept, and what is lost

- **The turn log**: `ROBINAUTS_FABRIC_STATE/sessions/<conversation id>.json`, the
  conversation's memory as the engine keeps it. The answer the person reads,
  and every tool call shown in the chat, are in the server's database as for
  any engine.
- **Workspaces**: `ROBINAUTS_FABRIC_WORKSPACES/<conversation id>`, the files the
  agent made.
- **Claude Code's configuration**: under `CLAUDE_CONFIG_DIR`. Its transcripts
  are not the memory here: a runtime does not resume them.
- On a server, all three are volumes in the command above. A conversation whose
  log is gone refuses its next turn, saying so.

## Checks

`scripts/check-extra-workers.sh` runs this folder's lint, tests and, where
Docker is present, the image build. The tests:

- `test_engine.py` and `test_units.py` run no Fabric and no Claude Code: the
  engine's decisions (live runtime, seeded runtime, failure, timeout, resume,
  refusals, eviction) against a stand-in runtime, and the pure parts.
- `test_fabric.py` runs the engine on Fabric itself: its Claude adapter's host
  runs Claude Code, the binary the Claude Agent SDK bundles, against a stand-in
  of Anthropic's Messages API on this machine (`tests/fake_anthropic.py`): a
  conversation, a `Bash` call, an edit, and a restart.
- `test_server.py` runs it under the server: a server with no database starts
  this worker's process, and a person's conversation goes through the server's
  API to Claude Code behind Fabric and back.
