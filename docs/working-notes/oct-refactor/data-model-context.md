# Context for the data model and the serialization

For the session that designs the controller's data model: the tables, their keys and
relationships, and how a message and a turn event are written as data. It is block 5
of `master-plan.md`, after the sessions rename (`sessions-plan.md`), before PostgreSQL.
The session decides; this note is what it needs to decide with. The names below use
"session" for what the code still calls a conversation until the rename lands.

## What exists

The controller's contract (`backend/src/robinauts/controller/contract/domain.py`) has
the records: `User` (id, provider, subject, name, email, created_at); the session (id,
owner_id, agent, title, created_at, updated_at); `Message` (id, session id, parent_id,
role, parts, created_at, agent, model, checkpoint_id), with four part kinds: text,
reasoning, tool call with its arguments as a mapping, tool result; the ten turn events,
from `TurnStarted` to `TurnEnded` with a `TurnState`; `NumberedEvent` (position, event);
`ActiveTurn` (follows, position); the opened session (record, the visible thread, the
active turn). `docs/architecture/controller.md` names the operations over them.

The store port (`controller/ports/store.py`) is what the application asks of storage
today, written for dicts: users by identity; add, get, update, delete a session and
list an owner's newest first; add a message and list a session's; `start_turn(session,
follows)`, `append_event` giving the next position, `events_after(position)`,
`end_turn`, `active_turn`, and `wait_for_events`, which blocks until events past a
position exist or no turn is active. `controller/adapters/memory.py` implements it;
`controller/application/turns.py` is the one writer of events and answers, and
`controller/application/controller.py` the reader. Positions restart at 1 with each
turn and only the last turn's events are kept, which `watch_turn` and web's re-attach
rely on today.

What the UI reads, through web (`web/app.py`): a session list newest first, a page at
a time with a cursor; one opened session with the visible thread, oldest first, and
when a turn is going the run id and the position to attach after; a turn's events from
a position as AG-UI events, with the position as the SSE id; and after a bad end,
which end it was (`ended_badly`, not served yet). `docs/specs/wire.md` says it.

The engines keep their own memory in tables of their own, named so that engines never
collide, with no foreign key to anything of the controller's, and `forget` is the only
way that memory is deleted (`docs/specs/agent-engines.md`). The controller stores only
the checkpoint id, on the answer.

## What is fixed

- PostgreSQL is the one database (`docs/specs/legacy/backend.md`, kept): asyncpg, no
  ORM, hand-written SQL in the controller's adapters, the schema as a SQL file shipped in
  the package and applied by a command, a recorded schema version the server checks at
  start-up. Before the first release the schema is one definition edited in place, no
  migrations.
- The in-memory store stays, for tests and the local start, and must hold the same
  model, so the application cannot tell them apart.
- Ids are uuids the controller mints; times are aware UTC datetimes the controller
  sets; a store keeps no clock and no id source.
- The user session of sign-in (block 7) is a separate table, `user_sessions`, with a
  hashed secret, a user and an expiry; and a pending-login table. Not this block's, but
  the names are taken.
- Every dependency passes the licence policy; `asyncpg` is already in the lock.

## What to consider

- **A turn as a row.** Today the active turn is one entry per session. A `turns` table
  (id, session, follows, state, started_at, ended_at, error) makes the active turn "the
  row in state running" with a unique partial index, gives the UI `ended_badly`, keeps a
  history, and lets events key on the turn so positions never restart. It also lets
  `TurnStarted` carry a turn id that web can use as AG-UI's run id. This is what legacy
  arrived at (`runs`). The alternative is a nullable column on the session.
- **The message tree.** `parent_id` points at a message of the same session; legacy
  enforced that with a composite foreign key `(session, parent) -> (session, id)`. The
  visible thread is the path from the root to the newest message; nothing else is
  stored for it today. Edits and regenerations are new messages under an earlier parent.
- **Parts and events as documents or as rows.** A message's parts nest; an event's
  fields differ by kind. Either goes into one `jsonb` document the store never reads
  inside, written by a pure function of the controller with a version number, or into
  rows and typed columns (a `message_parts` table, an events table with one column per
  field). Documents are what legacy did (ADR 0002) and what versioning is for; rows are
  queryable and need no version.
- **Which fields are columns.** Whatever is filtered, sorted or joined on: ids, the
  owner, the parent, created_at and updated_at, the position, the turn state. The rest
  may stay in the document.
- **Listing and paging.** Newest updated first, a cursor that survives a rename of an
  earlier item: `(updated_at, id)` as the key, an index to match.
- **Deletion.** A session's messages, turns and events go with it (cascade), the engine's
  memory goes by `forget`, called by the controller, never by the database. Trash and
  retention (stage two) may want a `deleted_at` rather than a hard delete; decide now
  whether the column exists.
- **Waking watchers across processes.** The in-memory store notifies a condition;
  PostgreSQL will `NOTIFY` on append and on end, carrying the session or turn id and
  the position, and `wait_for_events` will `LISTEN`. The model should make the payload
  obvious.
- **Bounds.** Legacy bounded every stored text (a part at one million characters, a
  title, a checkpoint id at 256). Stage two; the columns should not forbid them.
- **Serverless hosting.** `aws-serverless.md` argues for a port that also fits
  DynamoDB (and Aurora DSQL) and for running web and turns on Lambda. It adds rules to
  this list: positions from the runner, waits with a timeout, cancel and lease through
  the store, `deleted_at` with a purge, and a turn dispatcher port. `gcp-serverless.md`
  and `azure-serverless.md` add what differs there: Azure asks for the owner's id on
  session operations.
- **The engine's memory is not here.** No table of the controller's references an
  engine's, and the checkpoint id is text the controller never reads.

## Alternatives to start from

- **A. Documents.** `users`, `sessions`, `messages` (columns for the keys and the
  timestamps, the parts and provenance in one `jsonb` document), `turns`, `turn_events`
  (turn, position, one `jsonb` document). Two encode/decode pairs with a version per
  document. Smallest SQL, one place to version.
- **B. Rows.** The same five plus `message_parts` (message, index, kind, text, call id,
  name, arguments as `jsonb`, is_error) and typed columns on `turn_events` (kind,
  message id, call id, text, is_error, state, error). No document versioning; every
  field a column; more SQL and more rows per message.
- **C. Hybrid.** Messages as in A, events as in B or the reverse, by which side is read
  piecemeal. Events are read by position in order and never queried by field, which
  argues for a document there; parts are read whole with their message, which argues
  the same. The question is only whether anything will ever be queried inside.
- **D. Events only.** Messages derived from the turn events. Rejected by legacy: every
  read of a session replays its turns, and the question message of a turn is written
  before any event.

For the serialization itself: a dict per record with a `"v"` field and a `"kind"` on
each part and event, built from the dataclasses by hand (not `dataclasses.asdict`, so
that uuids and datetimes are written as text on purpose and a renamed field is noticed);
read back by kind, upgraded by version, never rewritten in place.

## What the session delivers

- The data model in words in `docs/architecture/controller.md` or a note beside it:
  the tables, their columns, keys, relationships and indexes, and what is a document.
- The store port adjusted to the model (a turn with an id, positions per turn), the
  in-memory store following, the contract fields it needs (a turn id on `TurnStarted`
  and on the active turn is the expected addition), and the tests.
- The two encode/decode pairs under `controller/application`, with round-trip tests,
  if alternative A or C is taken.
- Not the SQL, not the pool, not `db init`: those are block 6, written from this.
