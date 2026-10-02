# The path of one message

One follow-up message in an existing session, from the browser to the model and back.
The example agent runs on the LangChain engine, has one MCP tool, and uses Anthropic.

Each step says how it is done on two stores:

- **PostgreSQL** is the deployment of `docs/deployment.md`: uvicorn behind a reverse
  proxy, asyncpg, `LISTEN` and `NOTIFY`. This repository ships it (block 6, planned).
- **AWS serverless** is the layout of
  `docs/working-notes/oct-refactor/aws-serverless.md`: CloudFront, a web Lambda, a
  worker Lambda, and DynamoDB. This repository does not ship it. It is an adapter
  outside the repository, over the same ports, and it is described here to show that
  the ports allow it.

The steps describe the controller after block 5: turns with their own ids, a turn
dispatcher, leases, and the runner numbering its events. Where today's code differs,
the step says so.

## The way there

### 1. The UI sends the turn

The frontend's AG-UI client sends `POST /api/conversations/{id}/turns` with
`{text, parent_id, model_id}`. The response is the turn's stream.

- **PostgreSQL:** browser → reverse proxy (TLS) → uvicorn → FastAPI.
- **AWS:** browser → CloudFront (TLS, the same origin as the UI) → a Lambda function URL
  in streaming mode → the Lambda Web Adapter → the same FastAPI app, in the web
  Lambda. On a cold start, `lifespan` runs `controller.open()` first. Web builds no
  engine, so this is quick.

### 2. Web works out who is asking and which model to use

Sign-in is block 7; today everyone is the local user. Web calls
`controller.open_session` to choose the model (`model_of`, `web/app.py`).

- **PostgreSQL:** a `SELECT` on `user_sessions` by the hashed secret. Then the session
  row, its messages and its running turn, in one round trip or a few.
- **AWS:** a `GetItem` on `USESS#<hash>`. Then one consistent `Query` on
  `PK = SESSION#<id>`, which returns the session's record, messages and turns
  together.

### 3. The controller builds the question

`send_message` checks that the parent belongs to the session. It mints the message's
id and time and encodes the whole message as a versioned document
(`docs/architecture/data-model.md`).

### 4. The controller stores the question and starts the turn, atomically

`store.start_turn(question, turn)` stores the question and creates a turn with its
own id, in state `running`, with a `lease_until`, in one operation. If the session
already has a running turn, or has been deleted, it stores neither and raises
`TurnActiveError`, which web answers with 409. Today the question is stored first by
`add_message`, and `start_turn(session, follows)` has no turn id.

- **PostgreSQL:** one transaction:
  - `SELECT 1 FROM sessions WHERE id = $s AND deleted_at IS NULL FOR SHARE`. No row
    means the session is gone.
  - `INSERT INTO messages (id, session_id, parent_id, role, created_at, document)`,
    with the document as `jsonb`.
  - `INSERT INTO turns (…, state) VALUES (…, 'running')`. A violation of
    `turns_one_running_per_session` means 409, and the transaction takes the question
    back with it.
- **AWS:** one `TransactWriteItems`:
  - a `ConditionCheck` that the session item exists without `deleted_at`;
  - a `Put` of the question, with `SK = MSG#<created_at>#<id>` and the document as a
    string. A document over about 350 KB goes to S3, and the item keeps a pointer to
    it.
  - a `Put` of a marker item `SK = ACTIVE`, on condition that it does not exist. A
    failed condition means 409. It is how a store with no partial unique index holds
    "one running turn per session".
  - a `Put` of the turn item.

### 5. The turn is dispatched

The turn dispatcher port is given `(session_id, turn_id)`.

- **PostgreSQL:** the in-process dispatcher calls `asyncio.create_task(run_turn(…))`
  in the uvicorn process that took the request, as today. With several processes, the
  one that got the request runs the turn.
- **AWS:** `lambda:Invoke` with `InvocationType=Event` on the worker Lambda, with
  retries set to zero. It returns in milliseconds. A cold worker spends a few seconds
  importing the frameworks and the vendors' SDKs.

### 6. Web opens the stream

`watched()` calls `controller.watch_turn(after=0)` and waits for the first event, so
that a refusal is still answered with a status. It then answers `text/event-stream`
and sends `RUN_STARTED` (`web/agui.py`).

- **PostgreSQL:** a `StreamingResponse`. `X-Accel-Buffering: no` keeps the proxy from
  buffering it.
- **AWS:** the Web Adapter passes the chunks through the function URL and CloudFront as
  they are written.

### 7. The runner loads what it needs and announces the answer

`run_turn` loads the session, the question, the agent's configuration and the parent
answer's checkpoint id. Today these are passed in as objects. It starts a lease tick
in the background, then appends `MessageStarted` at position 1, numbered by itself.

- **PostgreSQL:**
  - The loads are `SELECT`s.
  - The event is `INSERT INTO turn_events (turn_id, position, document)` plus
    `NOTIFY robinauts_turns, '<turn> 1'` in the same transaction, delivered on commit.
  - The lease tick is `UPDATE turns SET lease_until = … RETURNING
    cancel_requested_at`.
- **AWS:**
  - The loads are one `Query` on the session's partition.
  - The event is a `PutItem` with `SK = EVT#<turn>#0000000001`, on condition
    `attribute_not_exists`. There is nothing to notify.
  - The lease tick is an `UpdateItem` with `ReturnValues`, which reads the cancel flag
    back in the same call.

### 8. The engine loads its memory

`engine.stream(session_id, definition, prompt, model=…, checkpoint_id=…,
timeout_seconds=120)`. The LangChain engine builds `create_agent(chat_model, tools,
system_prompt, checkpointer)` and starts from that checkpoint.

- **PostgreSQL:** the engine's own tables, through the asyncpg pool it was given
  (`StorageKind.POSTGRES`). Nothing of the controller's references them. LangGraph
  needs a saver of our own: `langgraph-checkpoint-postgres` depends on `psycopg`,
  which is LGPL (ADR 0002).
- **AWS:** the engine's own DynamoDB table, through a storage the external package
  supplies; `langgraph-checkpoint-aws`'s `DynamoDBSaver` would be one. Today both
  engines keep their memory in the process.

### 9. The engine reaches the tool servers

`tools_for(agent, settings)` opens the framework's MCP client to each configured
server, with the secret the settings ask for.

- **PostgreSQL:** outbound HTTPS from the host, through `HTTPS_PROXY` if one is set.
- **AWS:** outbound HTTPS from the worker Lambda, with no VPC and so no NAT gateway.
  The secrets are environment variables filled from SSM `SecureString`s at deploy
  time.

### 10. The framework calls the model provider

LangGraph calls the chat model: the Anthropic SDK's client, built by the engine
(`clients.py`) with the key `api_key_env` names. It is a streaming HTTPS request to
the Messages API.

- **Both:** identical, apart from where the key comes from: an environment file on
  the host, or the Lambda's environment. The engine's deadline of 120 s bounds the
  whole run.

## The way back

### 11. Text comes back

The provider streams deltas. LangGraph yields `AIMessageChunk`s, the engine's
`events_of` turns them into `TextDelta`s, and `run_turn` appends a `TextPiece` at the
next position. Pieces that arrive within about 50 ms of each other should be merged
into one event.

- **PostgreSQL:** an `INSERT` and a `NOTIFY` for each event.
- **AWS:** a conditional `PutItem` for each event.

### 12. The model calls a tool

The model asks for the tool. LangGraph runs it over MCP, sends the result back to the
provider, and the provider answers with more text: a second model call inside the
same turn. The engine reports a `ToolCall` and a `ToolResult`. The runner appends
`CallStarted`, `ArgumentsPiece`, `CallCompleted` and `ResultLanded`, and keeps the
call among the answer's parts. The checkpointer saves the graph's state after each
step.

- **PostgreSQL:** the events go into the controller's tables and the checkpoints into
  the engine's: one database, separate tables.
- **AWS:** the events go into the controller's table and the checkpoints into the
  engine's, with S3 for a checkpoint too large for an item.

### 13. The watcher passes each event to the browser

This runs in web while steps 7 to 12 run in the runner. `watch_turn` calls
`events_after(turn, after)`, yields each event, and moves `after` forward. Once it has
caught up, it calls `wait_for_events(turn, after, timeout)`. `agui.stream` maps each
event to AG-UI with `id: <position>` on its frame. The client's `following()` decodes
it and the UI renders it.

- **PostgreSQL:**
  - One `LISTEN` connection in each process wakes every watcher in that process when
    a `NOTIFY` arrives.
  - Then `SELECT … FROM turn_events WHERE turn_id = $t AND position > $after ORDER BY
    position`.
- **AWS:**
  - The adapter repeats a consistent `Query` on the `EVT#<turn>#…` range every 250 ms
    or so.
  - When the timeout passes with nothing new, web sends `: keep-alive`, so that
    CloudFront does not close the stream.
  - The web Lambda is billed for the whole wait.

### 14. The turn finishes

Once its last checkpoint is saved, the engine yields `Done(text, checkpoint_id)`. The
runner then:

- stores the answer, which carries the checkpoint id;
- appends `MessageCompleted` and `TurnEnded(finished)`;
- moves the session's `updated_at` and ends the turn.

All of that is one store operation. Today it is several calls.

- **PostgreSQL:** one transaction:
  - `INSERT` the message and the two events;
  - `UPDATE turns SET state = 'finished', ended_at = … WHERE id = $turn AND state =
    'running'`. The turn leaves `turns_one_running_per_session` by itself;
  - `UPDATE sessions SET updated_at = …`;
  - `NOTIFY '<turn> end'`.
- **AWS:** one `TransactWriteItems`:
  - a `Put` of the message and of the two events;
  - an `Update` of the turn, on condition that it is `running`;
  - a `Delete` of the session's `ACTIVE` marker;
  - an `Update` of the session's `updated_at`. That changes `GSI1SK`, so the session
    moves to the top of the list.

  The worker's invocation then returns, and its billing stops.

### 15. The stream closes

The watcher reads `TurnEnded`, web sends `RUN_FINISHED` and closes the response, and
the client sees the terminal event and stops.

- **PostgreSQL:** the connection goes back to the pool.
- **AWS:** the web Lambda's invocation ends.

## When it does not go straight through

- **The browser goes away.** Nothing happens to the turn. Opening the conversation
  again, or re-attaching with `Last-Event-ID`, continues from the position it had
  reached. This is the same on both.
- **Cancel.** `POST …/cancel` calls `request_cancel`:
  - **PostgreSQL:** `UPDATE turns SET cancel_requested_at`.
  - **AWS:** an `UpdateItem`.

  The runner sees the flag at its next lease tick and cancels the engine's stream. The
  turn ends as `cancelled`.
- **The runner dies**, whether the process crashed or the Lambda was killed. Its lease
  runs out. The next read that finds the turn (`open_session`, `watch_turn`,
  `start_turn`) ends it as `interrupted`, and the UI shows it as `ended_badly`.
- **A stream reaches a limit**, such as Lambda's 15 minutes or a proxy's timeout. The
  client re-attaches after the last id it saw. Nothing is lost or repeated.
