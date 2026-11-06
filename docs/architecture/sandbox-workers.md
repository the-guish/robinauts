# Sandbox workers

An agent marked `sandboxed` runs its sessions' turns in a **sandbox worker**: a process apart
from the server, which reaches the server over HTTP, through the **worker API**, and never
reaches the database. Everything else is as for any agent: its instructions, its model, its
engine, and its transcript in PostgreSQL. To the browser a sandboxed turn looks the same as
any other.

This is the first step towards the sandboxed coding agents of `coding-agents.md` (the target
architecture, in review). That design gives every session a microVM of its own from
[OpenShell](https://github.com/NVIDIA/OpenShell), a worker token bound to that session, and a
network policy that adds the credentials on the way out. This step has none of OpenShell: the
sandbox workers are processes, in a container of their own (`docker/`), and what differs is
listed at the end. What it has is what the target needs first: the turns running away from the
server, over the worker API, with the server checking everything they send.

## Two kinds of agent

- **In process.** The agent's turns run in the server's workers, as described in
  `the-path-of-one-message.md`.
- **Sandboxed.** `sandboxed = true` in its `[agents.<id>]` table. Its turns run in a sandbox
  worker. It may name MCP tool servers, which the sandbox worker reaches itself; it may not
  name the tools of this build, which read the store: the configuration is refused.

A session records whether it is sandboxed when it is created, as it records its engine. The
agent's flag decides the mode of new sessions only, and a session never changes mode.

## The parts

```
browser ──▶ web ──▶ controller ──▶ PostgreSQL ◀── the server's worker (in-process agents)
             ▲
             └── worker API (HTTP, long-poll) ◀── sandbox worker ──▶ model provider, MCP
                  /api/worker/v1/                  └─ engines, their memory in the process
```

- **The sandbox worker** is the worker of `controller.md`, composed on the HTTP store
  (`controller/adapters/http/store.py`) instead of PostgreSQL, with its engines' memory in its
  own process. `robinauts sandbox-worker` runs one. It names itself with an id it mints when
  its process starts: that id is **the sandbox**.
- **The worker API** is the part of web the sandbox workers call
  (`controller/application/worker_api.py` behind the `WorkerApi` port). It is the only way a
  sandbox reaches the server.

## A turn in a sandboxed session

1. **The controller queues the turn** as for any session, with its task marked `sandboxed`.
   The server's workers never claim a sandboxed task, and are not woken by one.
2. **A sandbox worker claims it**, holding a long-poll on `GET /api/worker/v1/task?wait=30`.
   The server claims the next sandboxed task of a session that this sandbox holds, or that no
   sandbox holds yet, with the usual `UPDATE … FOR UPDATE SKIP LOCKED` and a lease. A session of
   the second kind becomes this sandbox's in the same statement, and stays so: its memory is
   in that sandbox's process. With nothing queued, the server holds the request until
   `robinauts_queued` names a task or `wait` passes, at most `[sandbox] long_poll_seconds`,
   and answers 204; the worker asks again at once.
3. **The worker loads the turn**, `GET /api/worker/v1/turns/{turn}`: the turn, its session
   without its owner, and the session's messages.
4. **The engine runs in the sandbox worker**, which calls the model provider and the MCP tool
   servers itself.
5. **The events go back**, `POST /api/worker/v1/turns/{turn}/events`, in order. The server
   checks each (below) and appends it with the same conditioned insert and `NOTIFY` as an
   in-process runner. A turn the worker has lost is 409, and the worker stops the engine.
6. **The lease is kept.** Every `heartbeat_seconds` the worker calls `POST
   /api/worker/v1/lease` with the tasks it runs. The answer renews them and names the ones
   asked to stop: that is how a cancel reaches a sandbox.
7. **The turn finishes**, `POST /api/worker/v1/turns/{turn}/finish`: the answer and the last
   events, stored with the end of the turn in one transaction, as `finish_turn` does.

The watchers in web read the events from PostgreSQL as for any turn.

## The worker API

| Call | What it does |
|---|---|
| `GET /api/worker/v1/task?wait=<s>` | Claims the next task for this sandbox, waiting up to `wait` seconds; 204 when there is none |
| `GET /api/worker/v1/turns/{turn}` | The turn, its session and the session's messages |
| `POST /api/worker/v1/turns/{turn}/events` | Appends events, in order, from the next position |
| `POST /api/worker/v1/lease` | Renews the leases of the tasks named that this sandbox runs, and names those asked to stop |
| `POST /api/worker/v1/turns/{turn}/finish` | Stores the answer and the last events, and ends the turn |

Each call carries the sandbox workers' token as a bearer token, and the sandbox's id in
`Robinauts-Sandbox`. The server takes the owner and the session from the turn, never from the
request, and a turn of a session that is not that sandbox's is 404. No call reads another
session, a user or the configuration. The calls are outside the OpenAPI document: they are not
the interface's. They are outside the browser's checks too, the origin and, in the local
development mode, the host: a page cannot hold the token, and no cookie is read there.

Every time is the server's: the claim, the lease and the records are dated by its clock, and a
worker's clock decides nothing.

## What the server checks

The sandbox runs what its agent decides, and the agent may have been turned by what it read.
So the server treats a sandbox worker as untrusted:

- **Scope.** The sessions the sandbox holds, as above.
- **What it may write.** While a turn runs, the events of its answer: the answer's start, as
  the assistant's answer to the turn's question, its text and reasoning, tool calls and their
  results. With the finish, the end of the message and of the turn, the latter last and with
  the turn's state, and the answer: an assistant's message, of the turn's session, answering
  its question and naming the turn. An error goes with a failed or interrupted turn alone.
- **Form.** Every event and the answer are decoded with the data model's decoders
  (`data-model.md`) and encoded again before they are stored: a document that does not decode
  is refused, and no key the decoders do not know is kept.
- **Order.** Positions follow on from the last one stored, with no gap.
- **Size.** Each event, each answer, each call's number of events and each request has a
  limit (`MAX_DOCUMENT_BYTES`, `MAX_ANSWER_BYTES`, `MAX_EVENTS_PER_CALL`, `MAX_WORKER_BODY`).

A refusal is 422 (`WorkerRefusedError`). There is no rate limit yet.

## A sandbox's life

A sandbox lives as long as its worker's process. Its claims and its lease renewals record when
it was last heard from (`sandboxes.heard_at`).

- **A sandbox not heard from for `[sandbox] gone_after_seconds` is gone**, and the memory of
  its sessions with it. Each of its sessions is then read-only: its transcript stays, opening it
  answers `sandbox_gone: true`, which the interface says, and a follow-up, an edit, a
  regeneration, a retry and a resume are refused with `SandboxGoneError`, 409. The work goes on
  in a new conversation.
- **A sandboxed turn waits `gone_after_seconds` queued at most**, not the 48 hours of any other:
  past that no sandbox is there to run it, and it ends as `interrupted`.
- **A session no sandbox has held yet has lost nothing**: its next message is queued again.
- **A server that restarts does not touch its sandboxes.** Their workers retry the worker API
  with backoff until it answers, with their memory intact, and their sessions continue.
- **A sandbox worker that dies during a turn** leaves the turn to its lease, which ends it as
  `interrupted`, as any turn whose runner died.

## Configuration

```toml
[sandbox]
token_env = "ROBINAUTS_SANDBOX_TOKEN"   # the variable that holds the workers' token
long_poll_seconds = 30
gone_after_seconds = 120

[agents.coder]
title = "Coder"
system_prompt = "You are a coding assistant."
model = "claude-sonnet-5"
engine = "langchain"
tools = ["github"]
sandboxed = true
```

The configuration is refused when an agent is `sandboxed` and there is no `[sandbox]` table,
when a sandboxed agent names a tool of this build, when `long_poll_seconds` is not shorter than
`[work] lease_seconds`, and when `gone_after_seconds` is not longer than `long_poll_seconds` and
`[work] heartbeat_seconds`, which are how often a sandbox worker is heard from.

The sandbox worker reads the same file, and `ROBINAUTS_API_URL`, the server as it reaches it.
A deployment with no sandboxed agent runs no sandbox worker, and the server answers the worker
API with 401.

## What this step does not have yet

Against `coding-agents.md`:

- **One sandbox worker for many sessions**, in one container, rather than a microVM per
  session. Its sessions share its process, its filesystem and its network.
- **One token for every sandbox worker**, from the environment, rather than a token minted per
  session and bound to it. Any holder of the token can name any sandbox id, so the scope above
  holds against a confused worker, not against one that holds the token and means harm.
- **The sandbox holds its own credentials**: the model provider's key and the tool servers'
  secrets are in its environment. No supervisor adds them on the way out.
- **No network policy** beyond what the container's networks allow (`docker/compose.yaml`).
- **No sandbox manager**: nothing creates a sandbox for a session or deletes an idle one. A
  sandbox is gone when its process is.
- **No file or shell tools**: a sandboxed agent's tools are its MCP tool servers.
