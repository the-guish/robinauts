# Robinauts on AWS, serverless, paid by consumption

For the data-model session (block 5) and whoever deploys after block 6. The question:
can web, the API and the controller run on AWS products that charge only for what is
used, and can one data model serve both PostgreSQL and such a store? The answer is
yes, if the store port is shaped by a few rules now. This note gives those rules, a
model that follows them, and the AWS layout it would run on. It decides nothing that
`data-model-context.md` leaves to the session. It adds constraints and argues for one
answer.

This note is the reference for serverless hosting. `gcp-serverless.md` and
`azure-serverless.md` list only what differs on those clouds.

## What this repository ships

**Nothing in `open-shipyards/robinauts` depends on AWS, Azure or Google Cloud.** No
SDK, driver, checkpoint saver, store adapter, container image or infrastructure code
for any of them is in this repository, and none will be. Examples and integrations
for a cloud may be contributed in repositories of their own.

This repository ships the ports, the in-memory and PostgreSQL implementations, and
the vendor-neutral rules in this note. Those rules are what let a package outside
the repository implement the ports for a cloud. Every adapter, driver and saver named
in these three notes belongs to such a package.

Letting such a package plug in is stage three of `master-plan.md`, after the
product is workable and hardened. Nothing in stages one and two is built for it
beyond keeping the rules in this note. When it comes, three things follow for this
repository:

- **The extension points are reachable from outside.** An installed package must be
  able to supply the store, the turn dispatcher and an engine's storage without this
  repository naming it. Composition should take them from a registry such as Python
  entry points, not from a list here.
- **The engines' contract gets no cloud's name.** Instead of `DYNAMODB`, `COSMOS` or
  `FIRESTORE` members, `StorageKind` needs one kind for storage the caller supplies.
  Its options would carry what each engine documents that it accepts: a
  `BaseCheckpointSaver` for LangGraph, and an object implementing the engine's memory
  interface for Pydantic AI.
- **The store's contract tests can be imported.** An external adapter proves itself
  against the same suite as the PostgreSQL one.

"Paid by consumption" means no charge while nobody is using it: requests,
GB-seconds, read and write units, bytes stored. Storage at rest costs pennies and
can't be avoided. Anything billed by the hour or the month whether or not it is used
is excluded.

## What the code assumes today that serverless breaks

1. **A turn is an asyncio task in the web process** (`controller.py`, `_start_turn`).
   Lambda freezes the process once the response is finished, so a task outliving
   its request is not run. The turn has to run in an invocation of its own.
2. **Cancelling cancels that task** (`cancel_turn` reads `self._turns`). The process
   that runs the turn is not the one that receives the cancel request.
3. **Engine memory is in the process** (`InMemorySaver`, a dict in the Pydantic AI
   engine). One invocation's memory is gone in the next, so engine memory must be
   durable even for a trial.
4. **Watchers are woken by an `asyncio.Condition`**, and the plan is `LISTEN` and
   `NOTIFY` next. Neither reaches across Lambda invocations. `NOTIFY` needs a long-held
   connection, which neither DynamoDB nor Aurora DSQL nor the RDS Data API offers.
5. **"End the turns a process left active" runs in `open`**. In Lambda, no process
   start means anything, and many processes run at once.
6. **The run id is the session id**, and positions restart at 1 with each turn. This
   is already in `data-model-context.md`: a turn row with its own id fixes it.

None of these is AWS-specific. Running two web processes behind a load balancer on
PostgreSQL breaks 1, 2, 4 and 5 the same way. The fixes below make the controller
correct with more than one process, whatever hosts it.

## The rules the store port follows

These rules let one logical model fit PostgreSQL, DynamoDB and the in-memory store.

- **Every read is a key lookup or one range query on a key.** No joins at read time
  and no query inside a document. Each read in the port names a key: the user by
  identity, a session by id, an owner's sessions by `(updated_at, id)`, a session's
  messages, a turn's events after a position.
- **Each invariant is one port operation that holds it atomically.** "One running turn
  per session", "a user per identity", "end a turn only if it is running". PostgreSQL
  enforces these with a transaction or a unique index, DynamoDB with a conditional
  write or `TransactWriteItems`. The application never reads, checks and then writes.
- **Each operation touches a few records.** DynamoDB transactions are bounded at 100
  items and 4 MB, and a DSQL transaction has bounds of its own.
- **The store never relies on the database to cascade or to notify.** The adapter
  carries out `delete_session` and `wait_for_events` itself. PostgreSQL may use
  `ON DELETE CASCADE` and `NOTIFY` inside its adapter, but the port only requires that
  the effect happens.
- **Parts and events are opaque documents, encoded as JSON text by the controller.**
  The store keeps them as `jsonb` (PostgreSQL), a string attribute (DynamoDB) or text
  (DSQL, which has no JSON column type), and never reads inside them. This is
  alternative A of `data-model-context.md`. DynamoDB stores documents in any case, so
  rows would gain nothing there.
- **Text that keys or sorts is fixed-width.** Times are written
  `2026-10-01T12:00:00.000000Z` and uuids lowercase with hyphens, so that string order
  matches time order in a DynamoDB sort key or a GSI.
- **The controller assigns positions.** `run_turn` is the only writer of a turn's
  events, so it counts them itself and appends `(turn, position)`. The store refuses a
  position it already has, which can only mean two runners, a bug that should surface.
  This saves a round trip per event, and no store keeps a counter.
- **Waiting is the adapter's business, with a timeout.** `wait_for_events(turn, after,
  timeout)` returns when there is something new, when the turn has ended, or when the
  timeout passes. The memory store waits on a condition, PostgreSQL on `LISTEN`, and
  DynamoDB or DSQL poll. The timeout also lets web send the `: keep-alive` line the
  wire requires.
- **No clocks and no ids in the store** (already fixed). Leases rely on this too: the
  controller writes `lease_until`, and the controller compares it.

## The logical model

The same in all three stores. The **document** column is the versioned JSON;
everything else is a column (PostgreSQL) or an attribute (DynamoDB).

| record | key | other columns | document |
|---|---|---|---|
| user | `id` | `provider`, `subject` (unique together), `created_at` | — (`name`, `email` as columns; they are few) |
| session | `id` | `owner_id`, `agent`, `engine`, `title`, `created_at`, `updated_at`, `deleted_at` | — |
| message | `(session_id, id)` | `parent_id`, `role`, `created_at` | the whole message (`docs/architecture/data-model.md`) |
| turn | `(session_id, id)` | `follows`, `state`, `started_at`, `ended_at`, `error`, `cancel_requested_at`, `lease_until` | — |
| turn event | `(turn_id, position)` | `expires_at` | the event, by kind |

Decisions inside it, each with the reason:

- **A turn is a row**, as `data-model-context.md` proposes. `TurnStarted` and
  `ActiveTurn` carry the turn id, and web uses it as AG-UI's run id.
- **The running turn is looked up, not pointed at.** "At most one running turn per
  session" is held by `start_turn`, and each store holds it its own way. PostgreSQL
  uses a partial unique index on `turns (session_id) WHERE state = 'running'`. A
  store with no partial unique index writes a marker keyed by the session (DynamoDB:
  `SESSION#<id>` / `ACTIVE`) with the turn, on condition that it does not exist, and
  deletes it when the turn ends. That turns the rule into the uniqueness of a key,
  which every store guarantees. An earlier draft of this note put an
  `active_turn_id` pointer on the session instead; it is dropped, since it made two
  records that had to agree.
- **Cancelling goes through the store.** `cancel_turn` writes `cancel_requested_at`.
  The runner checks for it and cancels its own engine stream, so the existing
  `CancelledError` path ends the turn as `cancelled`. In one process, the in-process
  dispatcher may also cancel the task directly to save latency.
- **A turn holds a lease.** The runner extends `lease_until` (say 30 s ahead, every
  10 s), and the same write reads back `cancel_requested_at`, so one call per tick is
  both the heartbeat and the cancel check (the renewal is stage two; stage one writes
  the lease once, as the turn's bound plus a margin). A running turn whose lease has
  passed was left by a runner that died, and the next reader to find it
  (`open_session`, `watch_turn`, `start_turn`) ends it as `interrupted`, in the record
  alone: no event is written, and a watcher supplies `turn_ended` from the record. A
  runner that outlived its lease finds its next conditional write refused and stops.
  This replaces the sweep in `open` and works with any number of processes.
- **`deleted_at` exists.** Deleting a session sets it, on condition that no turn is
  running (no `ACTIVE` marker), and `start_turn` refuses a deleted session, so
  nothing writes under a purge. From then on every read treats the session as not
  found. A purge removes the records and calls `forget` on the engine the session
  names. In PostgreSQL the purge may run right away and cascade. In DynamoDB,
  deleting a session is many batch writes and can't be atomic, so a single
  conditional write that hides the session, followed by a purge, is the only honest
  shape. Trash in stage two is then a delay before the purge, not a schema change.
- **Events can expire; messages are the record.** The events of an ended turn are
  needed only to re-attach and to answer how the turn ended (and the turn row answers
  that). Each event carries `expires_at`. DynamoDB TTL deletes expired items for free,
  and PostgreSQL deletes them in the sweep. This keeps history without keeping every
  token forever.
- **Large documents are the adapter's problem.** A DynamoDB item is at most 400 KB, and
  legacy bounded a part at a million characters. The DynamoDB adapter stores a
  document over about 350 KB in S3 under `sessions/<session>/<record>` and keeps the
  pointer in the item. That is the same pattern `langgraph-checkpoint-aws` uses for
  checkpoints. The port is unchanged and the bound can stay where stage two sets it.
- **The paging cursor is the logical key.** The controller encodes `(updated_at, id)`
  of the last item into an opaque string, and each store asks for the items strictly
  before it. Paging moves into the store (`sessions_of(owner, limit, before)`), and
  `list_sessions` stops slicing a full list.
- **The wake-up payload is `turn_id` and `position`**, or `turn_id` and `end`. On
  PostgreSQL it is one channel, `robinauts_turns`, with that text as the payload.
- **The message tree is checked by the controller.** Same-session parents were a
  composite foreign key in legacy. DSQL has no foreign keys and DynamoDB has none at
  all, so `send_message` checks that the parent belongs to the session; it already
  loads the session's messages to do so. PostgreSQL may keep the key as a guard.

### The port, adjusted

What follows from the above, as a sketch for the session to settle. Names are
suggestions.

- `add_user_if_absent(user) -> User`: the user stored for that identity, created or
  found, atomically.
- `sessions_of(owner, limit, before) -> list[Session]`: the store pages, and deleted
  sessions are left out.
- `delete_session(session, at)` and `purge_session(session)`.
- `start_turn(turn)`: raises `TurnActiveError` if the session already has one.
- `append_event(turn_id, numbered)`: position given, duplicate refused.
- `events_after(turn_id, position)`, `get_turn(session, turn_id)`.
- `end_turn(turn_id, state, ended_at, error)`: only if the turn is running.
- `request_cancel(turn_id, at)`, `renew_lease(turn_id, until) -> bool` (`True` when a
  cancel was requested).
- `wait_for_events(turn_id, after, timeout)`.
- Session operations take the owner's id as well as the session's. Cosmos DB needs it
  for a point read (`azure-serverless.md`). Here it is a check, and it costs nothing.

And one new controller port, the **turn dispatcher**: `dispatch(session_id, turn_id)`.
The in-process implementation creates an asyncio task, as today. The Lambda
implementation invokes the turn worker asynchronously. `run_turn` then takes ids and
loads the session, the question, the model and the checkpoint from the store, so what
crosses an invocation is two uuids. Legacy had the same seam
(`legacy/ports/run_executor.py`).

### The documents

As `data-model-context.md` describes: a dict per record, with `"v"`, and `"kind"` on
each part and event, built by hand. The encoder lives in `controller/application` and
is shared by every durable adapter, so PostgreSQL and DynamoDB write the same bytes.
One more rule: **the encoder writes JSON text, not a store's native map type**.
DynamoDB's map attribute would turn floats in tool arguments into `Decimal` and lose
the document's exact bytes. A string attribute avoids both.

## DynamoDB, one table

The AWS store. On-demand capacity, so it is billed per read and write unit and per
GB stored, with no capacity to provision. It has a public endpoint with IAM, so no VPC.
TTL deletes expired items at no cost, and Streams can drive the purge.

| item | `PK` | `SK` | `GSI1PK` | `GSI1SK` |
|---|---|---|---|---|
| user | `USER#<id>` | `USER` | | |
| identity | `IDENT#<provider>#<subject>` (escaped) | `IDENT` | | |
| session | `SESSION#<id>` | `SESSION` | `OWNER#<owner>` | `<updated_at>#<id>` |
| message | `SESSION#<id>` | `MSG#<created_at>#<id>` | | |
| turn | `SESSION#<id>` | `TURN#<id>` | | |
| running-turn marker | `SESSION#<id>` | `ACTIVE` | | |
| turn event | `SESSION#<id>` | `EVT#<turn>#<position, zero-padded>` | | |
| user session (block 7) | `USESS#<hashed secret>` | `USESS` | | |
| pending login (block 7) | `LOGIN#<state>` | `LOGIN` | | |

- **A session is one partition.** Its record, messages, turns and events share
  `PK = SESSION#<id>`, so opening a session is one query, and the purge is one query
  plus batch deletes. A turn writes tens of events a second, far below the 1,000 writes
  a second one partition takes.
- **The listing is GSI1, kept sparse.** The session item carries the GSI keys while it
  is visible. Deleting removes them, so the session drops out of the listing in the
  same write that sets `deleted_at`. A GSI is eventually consistent, which is acceptable
  for a list. Opening a session reads the table itself, with consistent reads.
- **`ensure_user`** is one transaction: put the user and the identity item, each on
  condition that it does not already exist. If that fails, read the identity item.
- **`start_turn`** is one transaction: check that the session has no `deleted_at`,
  put the question, put the `ACTIVE` marker on condition that it does not exist,
  and put the turn. `end_turn` deletes the marker in the transaction that ends the
  turn.
- **Events are written one by one and read with a consistent query.** `SK` is between
  `EVT#<turn>#<after+1>` and `EVT#<turn>#~`. Waiting means polling that query every
  250 ms or so. That costs about four read units a second for each open stream, around
  a thousandth of a cent a minute.
- **The text pieces are coalesced.** One item per token is affordable, but each write
  is a round trip of several milliseconds inside the turn. The runner should merge
  the text pieces that arrive within about 50 ms into one event. This helps on
  PostgreSQL as well, and the wire is unaffected because a client reads one delta as
  it reads several.
- **Engines keep their own tables** (`robinauts-langgraph`, `robinauts-pydantic-ai`),
  with nothing pointing into the controller's table, as `agent-engines.md` requires.
  The LangGraph engine could use `langgraph-checkpoint-aws`'s `DynamoDBSaver`, which
  offloads to S3 too. Its licence and dependency tree still need checking against
  `DEPENDENCIES.md`. The Pydantic AI engine writes its `ModelMessage` list as JSON
  items. Both would reach the engine through the supplied-storage kind of stage
  three, not through a
  `DYNAMODB` member of the contract.
- **The driver** would be `aiobotocore` (Apache-2.0), in the external package. Its
  tests would run against `moto` (Apache-2.0) or DynamoDB Local, and they would also
  run the store's contract suite imported from this repository.

### Or Aurora DSQL, to keep SQL

DSQL is serverless PostgreSQL-compatible SQL, billed per DPU and per GB, with no
VPC needed. At the time of writing it has no foreign keys, no JSON column types, no
`LISTEN`/`NOTIFY`, no partial indexes and no triggers. Its concurrency is optimistic,
so a conflicting transaction fails and is retried. The rules above already avoid
every one of those. A schema written to the common subset (documents as `text`, no
foreign keys or cascades the port relies on, polling as the wait, a nullable unique
column set to the session's id while a turn runs in place of the partial index) would run on both, with one SQL adapter and a
dialect switch for the wait.

Two things are still open. Does asyncpg work against DSQL's IAM tokens and protocol
as it stands? And does the SQL get worse on plain PostgreSQL for DSQL's sake? The
recommendation is **DynamoDB**: it is mature, fully pay-per-request, and has TTL and
Streams for housekeeping. DSQL stays open because the port allows it, and choosing it
later is a new adapter, not a new model.

### Not chosen

- **Aurora Serverless v2.** It can now pause at zero ACUs, but resuming takes
  seconds. It also needs a VPC, so Lambda would need a NAT gateway (billed hourly) to
  reach the model providers. RDS Proxy, the usual fix for Lambda connection storms, is
  billed hourly too.
- **The RDS Data API** gets around the VPC but not `LISTEN`, and it adds a hop to
  every event write.

## The AWS layout

```
browser ── CloudFront ──┬── /ui/*                ── S3 (the built frontend)
   (one origin)         └── /api/*, /auth/*      ── Lambda function URL, streaming
                                                     "web": FastAPI under the Lambda Web Adapter
                                                       │  async invoke (session id, turn id)
                                                       ▼
                                                     Lambda "turn worker": run_turn, the engines
                                                       │
                         DynamoDB (controller, engines) + S3 (large documents)
                         EventBridge Scheduler ── Lambda "sweep": purges, forget, expiry
```

- **One origin.** CloudFront serves the UI from S3 and the API from a Lambda function
  URL, so the wire stays same-origin and the `__Host-` cookie of block 7 works. Avoid
  OAC with IAM auth on the function URL: it requires every POST to carry the body's
  SHA-256 in `x-amz-content-sha256`, which the frontend doesn't send. Use a secret
  header set by CloudFront and checked by web, or plan for that header.
- **Web is the FastAPI app as it is, under the Lambda Web Adapter.** The adapter is a
  layer that runs `robinauts start` inside Lambda and forwards requests to it. It
  supports response streaming on a function URL (invoke mode `RESPONSE_STREAM`), so
  the SSE endpoints stream. A streamed response may be 200 MB, and the first 6 MB go
  out at full speed. The `lifespan` runs on a cold start. `create_app` hardly changes.
- **A stream lasts at most 15 minutes**, Lambda's limit. The wire already copes: the
  stream ends, the client re-attaches with `Last-Event-ID`, and nothing is lost or
  repeated. Keep-alives every 15 s keep CloudFront from closing a quiet stream (its
  origin read timeout is 30 s by default).
- **The turn worker** is a second handler in the same image. Web stores the question
  and the turn, then invokes the worker with `InvocationType=Event`. Set the worker's
  retries to zero and its maximum event age below the lease. That is not at most
  once: an async invocation may be delivered twice, and a throttled one is held for
  up to the event age. A second delivery is refused at position 1, the runner's claim,
  and returns without running the engine; one delivered after the lease finds the
  turn `interrupted` and is refused the same way. The lease turns a lost worker into
  `interrupted`. A turn is bounded at 120 s today, well inside 15 minutes. If
  agent loops ever outgrow that, Lambda durable functions (Python 3.13 and later)
  checkpoint and resume a long run. They would fit the engines' `resume`, which
  continues an interrupted turn without repeating its tool calls.
- **Engines are built only where they are used.** Web never runs a turn, so it should
  not import LangChain or Pydantic AI on a cold start. Move the engine's `create`
  into the turn worker's first turn and `forget` into the purge, and web needs no
  engine at all. That gives a small, fast web function and a heavier worker. Both come
  from one container image built from the wheel, which avoids Lambda's 250 MB zip
  limit.
- **Housekeeping** runs on EventBridge Scheduler, billed per invocation: purge
  sessions marked deleted, call `forget`, end expired leases nobody has read. DynamoDB
  TTL deletes expired events, user sessions and pending logins without code.
- **Configuration and secrets.** The TOML goes in the image or in S3. The
  `*_env` variables become Lambda environment variables, filled at deploy time from
  SSM Parameter Store `SecureString`s (standard tier: no charge for storage or for
  normal use) and encrypted with the AWS-managed key. Secrets Manager costs a fixed
  amount per secret per month, and a customer-managed KMS key costs a fixed amount per
  month too.
- **No VPC.** Lambda reaches DynamoDB, S3, the model providers and the MCP servers
  over the public internet with IAM and TLS. A VPC would bring a NAT gateway or
  interface endpoints, and both are billed by the hour. A deployment whose MCP servers
  are private is the one case that needs a VPC, and it pays that fixed cost.

### What a turn costs

Approximate list prices, x86, us-east-1. Check them before quoting. Assume a
30-second turn, a 1 GB worker, a 512 MB web stream, and 250 writes after coalescing:

| | |
|---|---|
| turn worker, 30 GB-s | ≈ $0.0005 |
| the stream in web, 15 GB-s | ≈ $0.00025 |
| DynamoDB, ~250 writes and ~120 polling reads | ≈ $0.0002 |
| requests, CloudFront, logs | less than the above |
| **per turn, before the model** | **≈ $0.001** |

The model's tokens cost cents per turn, so they dominate. With no traffic, the bill
is storage: S3, DynamoDB, the container image in ECR, and the logs kept. A Route 53
zone and a WAF web ACL, if wanted, add small fixed monthly fees.

The stream's GB-seconds are the one cost that comes from the design and not from the
work, since the web function waits while the worker works. If that ever matters,
AppSync Events or an API Gateway WebSocket can push the events: the worker publishes
each one after storing it, and nothing waits. That would change the wire (re-attach
becomes "read from the store, then subscribe"), and the wire is a seam built for
exactly that. It isn't needed to start.

## What this asks of block 5

Beyond `data-model-context.md`'s list:

- The port follows the rules above. Specifically: positions assigned by the runner,
  `wait_for_events` with a timeout, paging in the store, `start_turn` and `end_turn`
  as conditional operations, cancel and lease through the store, and `deleted_at`
  with a purge.
- The turn dispatcher port, with the in-process implementation, and `run_turn` taking
  ids.
- The memory store follows all of it, so the contract tests prove the rules before
  any durable store exists. Those tests should be a suite that every store must pass,
  as the engines have.
- The encoder writes fixed-width times and JSON text, as above.

Not in this repository at all: the DynamoDB adapter, the engines' DynamoDB storage,
the image, the infrastructure code, and the deployment documentation for AWS. They
belong to an external integration. What this repository owes them is the extension
points above: the store, the dispatcher and the engines' storage, supplied from
outside, and an importable contract suite. Those are stage three.

## Sources checked

- [Aurora DSQL: unsupported PostgreSQL features](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-postgresql-compatibility-unsupported-features.html)
- [Lambda response streaming](https://docs.aws.amazon.com/lambda/latest/dg/configuration-response-streaming.html),
  [200 MB payloads](https://aws.amazon.com/about-aws/whats-new/2025/07/aws-lambda-response-streaming-200-mb-payloads),
  [with the Lambda Web Adapter](https://aws.amazon.com/blogs/compute/using-response-streaming-with-aws-lambda-web-adapter-to-optimize-performance/)
- [Lambda durable functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
- [CloudFront OAC for function URLs and `x-amz-content-sha256`](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-lambda.html)
- [DynamoDB as a LangGraph checkpoint store](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ddb-langgraph-checkpoint.html)
