# Plan: PostgreSQL

Block 6 of `master-plan.md`. Read `docs/architecture/data-model.md` first: it is the
model this block stores, and `controller/adapters/postgres/schema.sql` is its
PostgreSQL rendering, already written. `docs/architecture/the-path-of-one-message.md`
says what each step of a turn does on PostgreSQL.

Base branch: the one block 5 ends on. Branches `feature/postgres-N`.

Goal: `robinauts start` with `ROBINAUTS_DATABASE_URL` set serves on PostgreSQL.
Sessions, messages and turns outlive a restart. A browser re-attaches to a running turn
through a process other than the one running it. Both framework engines keep their
memory in tables of their own, so a session's next turn continues after a restart.
`robinauts db init` makes the database ready. Happy path only.

## What block 5 hands over

This block stores what block 5 decides, so it starts only when block 5 is done:

- the store port in the shape of `data-model.md`: documents for messages and events,
  sessions addressed by owner and session, turns by owner, session and turn,
  positions given by the runner, the question stored with its turn, a turn finished
  in one operation, `end_expired_turn`, `TurnLostError`, the hide refused while a
  turn runs, paging in the store, `deleted_at`, and `engine` on the session;
- the two encode/decode pairs, so this block never builds a document;
- the memory store following all of it, and the store's contract suite in
  `tests/contracts/`, importable, which the PostgreSQL store must pass unchanged;
- the engines' contract suite (`tests/contracts/engine.py`), which both engines must
  pass over PostgreSQL storage unchanged.

## Decisions

- **The schema is the file, with the edits block 5 asks for:** `sessions.engine text
  NOT NULL`, after `agent`, with a comment saying a turn runs and the purge forgets
  on it; and the comments brought to block 5's rules, where they still say `end_turn`,
  a position offered twice is "two runners", or the runner pushes the lease forward
  and reads the cancel flag. It ships in the package, beside the store.
  `SCHEMA_VERSION` is 1 and `SCHEMA_SHA256` is the file's hash, pinned by a test, so
  an edit is deliberate and visible, as legacy did (`legacy/datastore/schema.py`).
- **The server never changes the database** (`docs/deployment.md`). `robinauts db init`
  applies `schema.sql` to an empty database in one transaction, records the hash, and
  calls `setup` on **every installed engine**, so adding an agent on another engine
  later needs no second `db init`. `controller.open` on PostgreSQL checks the version
  and the hash, refuses a database that is not this build's or whose
  `server_encoding` is not `UTF8` (a cluster made under `LANG=C` is `SQL_ASCII`, and
  takes no non-ASCII document) and names the command, and does not call `setup`. On
  in-memory storage it calls `setup`, as today.
- **One asyncpg pool per process**, opened by the composition from
  `ROBINAUTS_DATABASE_URL`, used by the controller's store and handed to the engines
  as `StorageConfig(POSTGRES, {"pool": pool})`, which `controller/application/engines.py`
  already builds. `close` closes it. A `jsonb` codec on the pool turns documents into
  dicts and back, so no SQL in the store spells JSON. `jsonb` refuses a NUL and an
  unpaired surrogate, which is why block 5's encoder writes neither.
- **No schema is named.** Nothing in `schema.sql`, the store or the engines' SQL names
  one; everything lands where `search_path` points. The tests rely on it.
- **Constraint names are the interface** between the database and the store. The
  store translates a violation by name:
  - `turns_one_running_per_session` → `TurnActiveError`;
  - `turn_events_pkey` → after the rollback, read the row there by owner, session,
    turn and position and compare the documents decoded, `expires_at` aside (`jsonb`
    reorders keys and re-renders numbers, so never as text): the same document is the
    runner's own write, acknowledged late, and is accepted; another is
    `TurnLostError`;
  - `users_provider_subject_key` → the user exists: read it and return it.

  Three refusals are not violations but rows not written:
  - an append is `INSERT INTO turn_events … SELECT … FROM turns WHERE id = $t AND
    session_id = $s AND state = 'running' AND lease_until > $written_at FOR SHARE OF
    turns`. The lock makes it wait on a reader ending the turn and read again, which
    the foreign key's own `KEY SHARE` would not; zero rows is `TurnLostError`;
  - `finish_turn`'s `UPDATE turns` has `WHERE … AND state = 'running' AND lease_until
    > $ended_at`; zero rows is `TurnLostError`, and the transaction is rolled back;
  - `hide_session` is two statements in one transaction: `SELECT … FROM sessions
    WHERE id = $s AND owner_id = $o AND deleted_at IS NULL FOR NO KEY UPDATE` (no
    row: `SessionNotFoundError`), then `UPDATE sessions SET deleted_at = $at WHERE id
    = $s AND NOT EXISTS (SELECT 1 FROM turns WHERE session_id = $s AND state =
    'running')` (zero rows: `TurnActiveError`). The second statement's snapshot is
    taken after the lock, so a turn that `start_turn` committed under it is seen; one
    `UPDATE … AND NOT EXISTS` is not, since PostgreSQL does not re-run a subquery for
    a row that was only locked.
- **The session row first.** Every transaction that writes a session and one of its
  turns locks the session row before it touches `turns`: `start_turn` with `SELECT …
  FOR SHARE`, `finish_turn` and `hide_session` with their `UPDATE`. So `finish_turn`
  moves `updated_at` first and ends the turn last. The other way round, a `start_turn`
  waiting on `turns_one_running_per_session` and a `finish_turn` waiting on the
  session row deadlock, and PostgreSQL kills one of them: a start, which is only
  refused, or a finish, which loses the answer and leaves the turn running.
- **A turn ended by its lease is one statement.** `end_expired_turn` is `UPDATE turns
  t SET state = 'interrupted', ended_at = $now, error = 'lease expired' FROM sessions
  s WHERE s.id = t.session_id AND s.id = $s AND s.owner_id = $o AND s.deleted_at IS
  NULL AND t.state = 'running' AND t.lease_until < $now RETURNING t.*`, with no
  `NOTIFY`: a watcher reads the record when its wait returns.
- **Waking watchers is `LISTEN` and `NOTIFY`.** Channel `robinauts_turns`, payload
  `<turn> <position>` or `<turn> end`, sent as `SELECT pg_notify($1, $2)` (`NOTIFY`
  takes no bind parameter) in the transaction that writes the event, so it is
  delivered on commit. One listening connection per process, held apart from
  the pool, wakes every waiter of that process. `wait_for_events(…, timeout)` returns
  when woken, when the turn has ended, or when the timeout passes, and the caller
  reads the store again in every case, so a notification lost with a dropped
  connection costs a timeout and nothing else.
- **Deleting a session purges it at once.** `delete_session` sets `deleted_at`, on
  condition that no turn is running, calls `forget` on the engine the session's row
  names, and deletes the row, whose cascade takes the messages, turns and events. The
  scheduled purge, and trash before it, are stage two's housekeeping. `deleted_at`
  already lets them come without a change of schema.
- **The engines' tables are their own.** Each engine makes its tables in `setup` with
  `CREATE TABLE IF NOT EXISTS`, names them after itself, and references nothing of the
  controller's. `forget` deletes everything it holds for a session.
- **Pydantic AI's memory** is two tables. `pydantic_ai_sessions (session_id)` answers
  `create` and `exists`. `pydantic_ai_checkpoints (session_id, checkpoint_id,
  history json, created_at)` holds `all_messages()` after each finished turn, written
  with the framework's own `ModelMessagesTypeAdapter`, under a checkpoint id the
  engine mints. `json`, not `jsonb`: the framework's bytes are kept as written, and
  `jsonb` refuses the `\u0000` a tool result may carry, which the engine's memory does
  not clean.
- **LangGraph's memory** is a checkpoint saver of our own over asyncpg, because
  `langgraph-checkpoint-postgres` depends on `psycopg`, which is LGPL (ADR 0002). At
  langgraph-checkpoint 4.2.0, an async saver implements `aget_tuple`, `alist`, `aput`,
  `aput_writes`, `adelete_thread` and `get_next_version`. `acopy_thread` waits for fork
  in stage two. Three tables: `langgraph_sessions (session_id)` for `create` and
  `exists`; `langgraph_checkpoints (thread_id, checkpoint_ns, checkpoint_id,
  parent_checkpoint_id, checkpoint bytea, metadata json)`; and `langgraph_writes
  (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, value bytea)`. The
  values are written with the saver's own serializer (`serde.dumps_typed`), never
  pickled by us. `metadata` is `json`, as Pydantic AI's history is: the framework's
  own PostgreSQL saver has to strip `\u0000` to write it as `jsonb`.
- **Echo stays in memory** whatever the storage. It exists for tests and the local
  start, and keeps nothing worth keeping.

## Not in this block

- The sweep: deleting expired events, ending turns whose lease has passed when nobody
  reads them, and the scheduled purge. Stage two's housekeeping. Until then, events
  stay in `turn_events` after they expire, and a turn whose lease has passed is ended
  by the next reader to find it, as block 5 has it.
- Sign-in's tables, `user_sessions` and the pending logins: block 7.
- Fork, in the controller and in both engines' storage: stage two.
- Bounds on stored text: stage two.
- The extension points that let a store, a dispatcher or an engine's storage come
  from a package outside this repository: stage three.

## Rules

- One branch per step, `feature/postgres-N`, from the previous step's branch. One commit
  per branch, pushed. No pull requests, no reviews between steps.
- Minimal code for the main flows. No edge cases, no defensive checks beyond the
  refusals the port and the engines' contract name. Comments only where the code does
  not say it; no docstrings unless a line cannot be read without one.
- The layer rules of `docs/architecture/rules.md` hold. `asyncpg` is imported only under
  `controller.adapters` and the two framework engines, which the import-linter already
  allows. A new class in a contract, or a new import direction, stops the work and asks
  the owner.
- Tests that need a database are marked `database` and take
  `ROBINAUTS_TEST_DATABASE_URL`. Each runs in a schema of its own, named after a fresh
  uuid and dropped however the test ends, as `tests/postgres.py` does for legacy. A new
  helper does the same without importing legacy. With no URL they skip; CI sets
  `ROBINAUTS_REQUIRE_POSTGRES`, which makes a skip a failure.
- Before each commit, from `backend/`: `uv run --locked ruff check --config
  pyproject.toml . ../scripts ../demo ../examples`, `uv run --locked black --check
  --config pyproject.toml . ../scripts ../demo ../examples`, `uv run --locked pytest
  tests/unit -q`, the step's database tests with `ROBINAUTS_TEST_DATABASE_URL` set,
  and `uvx reuse lint` from the root, all green. Every new file starts with the two
  licence header lines. Line length 100.
- Commit messages end, after a blank line, with the attribution lines the session's
  system reminder gives.

## Steps

1. **The schema in the package.** `schema.sql` gains `sessions.engine` and the
   comment edits above. `controller/adapters/postgres/schema.py`: `SCHEMA_VERSION`,
   `SCHEMA_SHA256`, `create_schema(connection)` for an empty schema, under the
   advisory lock legacy took (`legacy/datastore/schema.py`), since two `db init` at
   once otherwise collide, and `check_schema(connection)`, which refuses a missing
   version, another version, another hash or a `server_encoding` that is not `UTF8`,
   and names `robinauts db init`. Test: the hash matches the file; the file applied
   to an empty schema twice leaves one version row; `check_schema` passes on it and
   refuses a schema with no version row and one with another hash.
2. **The pool.** `open_pool(url)` with the `jsonb` codec, and the test helper that gives
   each test a schema of its own. Test: a dict written to a `jsonb` column comes back
   equal.
3. **The store: users, sessions, messages.** `controller/adapters/postgres/store.py`
   implements the port's operations on these three, by hand-written SQL: a user by
   identity or created, with the duplicate translated; a session added, read by owner
   and id, renamed, listed a page at a time by `(updated_at, id)`, hidden unless a turn
   is running (the row locked first), and purged; a session's messages, oldest first.
   Test: the store's contract suite, for these operations.
4. **The store: turns and events.** A question stored with its turn, the duplicate
   running turn translated; an event appended at its position, with its `expires_at`,
   only while the turn runs, the same document accepted again and another refused;
   the events after a position; a turn finished in one transaction, the session row
   first; the active turn looked up; `end_expired_turn`. `lease_until` is written as
   block 5's runner gives it and read by `end_expired_turn`; `cancel_requested_at` is
   left empty and read in stage two (`data-model-plan.md`). Test: the store's contract
   suite, whole; and three races on one session from two connections, many times
   over: a `finish_turn` against a `start_turn`, where neither deadlocks, the finish
   always lands, and the start starts or is refused; a `hide_session` against a
   `start_turn`, where the session is never hidden with a running turn; and an
   `append_event` against an `end_expired_turn`, where no event lands on the ended
   turn.
5. **Watchers in other processes.** The listening connection, `NOTIFY` in the
   transactions that append an event and end a turn, and `wait_for_events` with its
   timeout. Test: a store on one pool waits while a store on a second pool appends, and
   wakes well within the timeout; the same with the listener's connection closed under
   it returns at the timeout and the events are read.
6. **Composition and the command.** `StorageKind.POSTGRES` from
   `ROBINAUTS_DATABASE_URL`, in-memory without it; `robinauts db init` creates the
   schema and calls every installed engine's `setup`; `open` checks the schema and
   refuses with the command's name; `close` closes the pool. Test: `db init` on an empty
   schema succeeds and a second run says the schema is there; `start` against an
   uninitialised schema refuses and names the command.
7. **Pydantic AI on PostgreSQL.** The two tables, `setup`, and `create`, `exists`,
   `stream` and `forget` over them. Test: the engines' contract suite over PostgreSQL
   storage.
8. **LangGraph on PostgreSQL.** The saver and its three tables, `setup`, and the
   engine's sessions over them. Test: the engines' contract suite over PostgreSQL
   storage; a plain turn and a turn with a tool round written and read back through
   the saver, checkpoint for checkpoint, as `InMemorySaver` would have them.
9. **Proof.** A PostgreSQL, `robinauts db init`, and `robinauts start` from
   `examples/pydantic-ai.toml` and from a LangChain example: a turn in the browser;
   the server restarted; the session reopened with its thread; a second turn that
   remembers the first. The server killed during a turn and started again: the
   session opens, and once the lease has passed (three minutes after the turn
   started) a new turn starts on it. A second `robinauts start` on another port over
   the same database: a turn started through one is watched to its end through the
   other. A short `postgres-progress.md` records what was verified.
