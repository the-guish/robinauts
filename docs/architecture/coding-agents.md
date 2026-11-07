# Coding agents

An agent marked `sandboxed` runs each of its sessions in a virtual machine of its own, with
a filesystem and a shell its tools work on. Everything else is as for any agent: its
instructions, its model, its engine, and its transcript in the database.

The virtual machine is a Linux guest that [OpenShell](https://github.com/NVIDIA/OpenShell)
creates and runs. Inside it runs a worker of its own, the sandbox worker, which runs the
session's turns and reaches the server over HTTP. It reaches nothing else but the model
providers and tool servers its agent is configured with.

## Two kinds of agent

- **In process.** The agent's turns run in the server's workers, as described in
  `the-path-of-one-message.md`. Its tools are MCP tool servers and the tools of this build.
- **Sandboxed.** `sandboxed = true` in its `[agents.<id>]` table. Each of its sessions gets
  one sandbox, and its turns run in that sandbox's worker. Its tools are the file and shell
  tools of the sandbox, and the MCP tool servers it names. The tools of this build read the
  store, so a sandboxed agent may not name them: the configuration is refused.

A session records whether it is sandboxed when it is created, as it records its engine. The
agent's flag decides the mode of new sessions only. A session never changes mode.

## The parts

```
browser ──▶ web ──▶ controller ──▶ PostgreSQL
                       │   ▲
                       │   └── worker API (HTTPS, long-poll) ◀──────────────────┐
                       ▼                                                        │
              sandbox manager ──▶ OpenShell gateway ──▶ supervisor ──▶ sandbox VM
                (Sandboxes port)    (on the host)       (applies      ├─ sandbox worker
                                                         policy, adds │   └─ engine, in memory
                                                         credentials) └─ /sandbox, the workspace
```

- **The sandbox manager** is the controller's view of sandboxes, behind the `Sandboxes`
  port: create one for a session, delete it, and say whether it is still there. Its adapter
  calls the OpenShell gateway through OpenShell's Python SDK. No other part of Robinauts
  talks to OpenShell.
- **The OpenShell gateway** runs on the host, with the `vm` compute driver: each sandbox is
  a microVM, on KVM on Linux or Hypervisor.framework on macOS. A virtual machine has no
  network interface. All of its traffic leaves through the OpenShell supervisor on the host,
  which checks it against the sandbox's policy and adds the credentials.
- **The sandbox worker** is the worker of `controller.md`, composed on the HTTP store
  instead of PostgreSQL. It runs the turns of one session, one at a time.
- **The worker API** is the part of web that the sandbox workers call. It is the only way a
  sandbox reaches the server.

## A sandbox's life

A sandbox lives as long as its sandbox worker's process does, and no longer.

1. **Creation.** The first turn of a sandboxed session asks the sandbox manager for the
   session's sandbox. The manager:
   - mints the session's worker token and stores its SHA-256;
   - creates the sandbox from the configured image, with the session's policy (below) and
     its credentials as OpenShell providers;
   - writes the session's agent, model and tool server configuration into the sandbox, with
     no secrets in it;
   - starts the sandbox worker, and records the sandbox's id on the session.
2. **Turns.** The sandbox worker runs each turn of the session as it is queued. Between
   turns it waits on the worker API. The engine's memory stays in the worker's process, so
   each turn continues from the last.
3. **Deletion.** The manager deletes the sandbox when:
   - the session has had no turn for `[sandbox] idle_seconds`, counted from the end of its
     last turn, never during one;
   - the session is deleted;
   - its sandbox worker's process has exited, or the sandbox has failed.

   The worker token is revoked with it. Everything in the sandbox is gone: the workspace,
   and the engine's memory of the session.

4. **After.** The session is read-only. Its transcript stays in the database, and opening it
   shows every message and a notice that its sandbox was deleted, and that continuing the
   work needs a new conversation. A follow-up message, an edit, a regenerated answer, a
   resumed answer and a fork are refused with `SandboxGoneError`, which web answers with
   409.

A server that restarts does not touch its sandboxes. Their workers retry the worker API
until it answers, with their memory intact, and their sessions continue.

## A turn in a sandboxed session

The steps of `the-path-of-one-message.md` hold, except where the runner is.

1. **The UI sends the turn**, and web works out the user and the model, as for any session.
2. **The controller queues the turn.** `queue_turn` stores the question, the task and the
   turn in one transaction. The task names the session's sandbox as its target. The
   in-process workers claim only tasks with no target, so this one waits for its sandbox.
   On a session's first turn, the controller asks the sandbox manager for the sandbox
   first. If OpenShell cannot create it, the turn fails with that error and nothing is
   queued.
3. **The sandbox worker claims the task.** It holds a long-poll on the worker API,
   `GET /api/worker/v1/task?wait=30`. The server claims the next task targeted at that
   sandbox with the usual `UPDATE … FOR UPDATE SKIP LOCKED` and a lease, and answers with
   it. With no task queued, the server holds the request until `robinauts_queued` names
   one, or until `wait` seconds pass, and then answers 204; the worker asks again at once.
   A turn starts as soon as it is queued, with no polling interval.
4. **The worker loads the turn**, `GET /api/worker/v1/turns/{turn}`: the session, its
   messages, and the checkpoint id of the nearest answer above the question.
5. **The engine runs in the sandbox.** It calls the model provider, the MCP tool servers,
   and the file and shell tools, all from inside the virtual machine. The supervisor adds
   the provider's key and the tool servers' secrets on the way out; the engine holds only
   placeholders.
6. **The events go back.** The worker merges the text that arrives within about 50 ms into
   one event, and sends the turn's events in order to
   `POST /api/worker/v1/turns/{turn}/events`. The server checks each of them (below) and
   appends it with the same conditioned insert and `NOTIFY` as an in-process runner. A turn
   the worker has lost is 409, and the worker stops the engine.
7. **The lease is kept.** Every `heartbeat_seconds`, the worker calls
   `POST /api/worker/v1/lease`. The answer renews the task's lease, and says whether the turn
   has been asked to stop.
8. **The turn finishes.** The worker sends the answer and its last events to
   `POST /api/worker/v1/turns/{turn}/finish`. The server stores them, ends the turn and
   moves the session's `updated_at` in one transaction, as `finish_turn` does.

The watchers in web read the events from PostgreSQL as for any turn. To the browser, a
sandboxed turn looks the same as any other.

## The worker API

| Call | What it does |
|---|---|
| `GET /api/worker/v1/task?wait=<s>` | Claims the next task targeted at this sandbox, waiting up to `wait` seconds; 204 when there is none |
| `GET /api/worker/v1/turns/{turn}` | The turn's session, its messages, and the checkpoint id to start from |
| `POST /api/worker/v1/turns/{turn}/events` | Appends events, in order, from the next position |
| `POST /api/worker/v1/lease` | Renews the lease of this sandbox's running task, and returns whether it was asked to stop |
| `POST /api/worker/v1/turns/{turn}/finish` | Stores the answer and the last events, and ends the turn |

Each call carries the session's worker token as a bearer token. The server resolves the
token to one sandbox and one session by its SHA-256, and takes the owner, the session and
the sandbox from it. It never takes them from the request. A turn id in a path that is not a
turn of that session is 404.

No call reads another session, a user, the configuration, or another sandbox, and none
changes a sandbox's policy, image or life. Those belong to the sandbox manager, which the
worker API does not reach.

The server holds a long-poll for at most `[sandbox] long_poll_seconds`, kept under the
supervisor's request timeout, so the supervisor never cuts one.

## What the server checks

The sandbox runs code the agent wrote, and the agent may have been turned by what it read.
So the server treats the sandbox worker as untrusted, and checks everything it sends.

- **Scope.** The token's session alone, as above.
- **What it may write.** The events of an answer: text, tool calls and their results, and
  how the turn ended. A message as the user, a change to the session, and an event of
  another turn are refused.
- **Form.** Every event and the answer are decoded with the data model's schemas
  (`data-model.md`) and encoded again before they are stored. A document that does not
  decode is refused, and no field the schemas do not know is kept.
- **Size and pace.** Each event, each answer and each request has a size limit, and each
  token a rate limit. Past them, the request is refused, and the turn fails.
- **Order.** Positions follow on from the last one stored, as for an in-process runner.

The browser renders a sandboxed agent's text and tool results as it renders any agent's:
as untrusted content.

## The sandbox

- **Image.** A Linux image with Python, Robinauts with its engines, and the command-line
  tools a coding agent expects, such as `git`. `[sandbox] image` names it. The sandbox
  worker runs as the image's `sandbox` user.
- **Workspace.** `/sandbox`. The file tools work there, and the shell tool starts there.
- **Network.** The sandbox's policy allows, and nothing else:
  - the worker API, at `[sandbox] api_url`, for the paths under `/api/worker/v1/` alone;
  - the model provider of the session's model;
  - the MCP tool servers the agent names;
  - the package mirrors `[sandbox] mirrors` lists, read-only.

  The supervisor resolves every name; the virtual machine has no DNS of its own. A server
  on the same host as the gateway is reached as `host.openshell.internal`.
- **Credentials.** OpenShell providers hold:
  - the provider's key, added as `x-api-key` for Anthropic and as a bearer token for
    OpenAI and OpenRouter;
  - each tool server's secret, in the header its `auth` names;
  - the session's worker token, added on requests to the worker API.

  The sandbox's environment holds only placeholders. The supervisor adds a credential on
  requests from the sandbox worker's binary to that credential's host, and on nothing else.

## The tools of a sandboxed agent

- `read_file`, `write_file`, `edit_file`: one file under `/sandbox`.
- `list_files`, `grep`: the workspace, with a limit on what they return.
- `bash`: one command, from `/sandbox`, with a timeout and a limit on its output.

They run in the sandbox worker's process, so they need no protocol: the sandbox is the
machine they run on. The agent may also name MCP tool servers, which it reaches through the
supervisor as above.

## Configuration

```toml
[sandbox]
gateway = "https://localhost:17670"
image = "registry.example.com/robinauts/sandbox:1.4.0"
api_url = "https://robinauts.example.com"
idle_seconds = 1800
long_poll_seconds = 30
mirrors = ["https://npm.mirror.example.com", "https://pypi.mirror.example.com"]

[agents.coder]
title = "Coder"
system_prompt = "You are a coding assistant. Your workspace is /sandbox."
model = "claude-sonnet"
engine = "langchain"
tools = ["github"]
sandboxed = true
```

The configuration is refused when:

- an agent is `sandboxed` and there is no `[sandbox]` table;
- a sandboxed agent names a tool of this build;
- `long_poll_seconds` is not shorter than `[work]`'s `lease_seconds`.

The gateway's credentials are the server's, from the environment, like the providers' keys.
A deployment with no `[sandbox]` table runs no sandbox and needs no OpenShell: one
application and one PostgreSQL database, as before.

## When it does not go straight through

- **The sandbox worker dies during a turn.** Its lease runs out, and the turn ends as
  `interrupted` by the next reader or the sweep, as any turn whose runner died. The
  sandbox manager finds the worker gone and deletes the sandbox, and the session is
  read-only.
- **Cancel.** `cancel_turn` sets the flag as for any turn. The worker reads it in the answer
  to its next lease call, cancels the engine's stream, and the turn ends as `cancelled`.
- **The server is unreachable.** The worker retries with backoff. A turn whose lease runs
  out meanwhile ends as `interrupted`, but the sandbox and its memory stay, and the next
  turn continues from the last answer that has a checkpoint.
- **OpenShell is unreachable when a session starts.** The first turn fails with that error,
  and the session has no sandbox. Its next message tries again.
- **The session is deleted.** `delete_session` stops a running turn as usual, and the
  sandbox manager deletes the sandbox.
