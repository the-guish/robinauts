# Block 6: PostgreSQL, what was verified

Branch `design/data-model`, one commit per step or pair of steps of `postgres-plan.md`.
Every step left the unit suite and the layer rules green; the database tests ran against a
PostgreSQL 16.14 cluster (`ROBINAUTS_TEST_DATABASE_URL`), each in a schema of its own.

## The steps

1. **The schema in the package.** `schema.sql` gained `sessions.engine` and the comment
   edits block 5 asked for; `schema.py` pins the hash, applies the file under legacy's
   advisory lock, and refuses, naming `robinauts db init`, a database with no schema,
   another version, another edit, a table missing, a name of ours ahead on the search
   path, or an encoding that is not UTF8. A refusal is a `ConfigError`: no new class was
   added to the contract.
2. **The pool**, with the `jsonb` and `json` codecs; a number JSON cannot write is refused
   before the database sees it.
3. to 5. **The store, and watchers across processes.** `tests/controller_db.py` gives each
   database test its schema without importing legacy. The store passes the contract suite;
   the three races the plan names are proved deterministically: an append waits on a
   reader's end and then inserts nothing, a hide waits on a starting turn and is then
   refused, and a finish raced against a start never deadlocks and always lands. A watcher
   on one pool is woken by an append on another, and returns at its timeout with the
   events read when its listener was closed under it.
6. **Composition and the command.** `ROBINAUTS_DATABASE_URL` chooses PostgreSQL; the store
   port gained `open` and `close`; `db init` creates the schema and calls `setup` on every
   installed engine, and says the version and the hash whether it created or found it.
7. and 8. **The engines on PostgreSQL.** Pydantic AI's two tables, with the history in a
   `json` column; LangGraph's saver of our own over asyncpg, its checkpoint and metadata
   as the serializer's bytes beside their type (`metadata` is bytes, not the plan's
   `json`, for the same reason the history is `json`: the framework's own rendering,
   never re-read by us). Both pass the engines' contract suites over PostgreSQL storage,
   and a plain turn and a tool round through the PostgreSQL saver hold what the in-memory
   saver holds, checkpoint for checkpoint.

## The proof

Over `examples/echo.toml` and a database of its own on the local cluster:

- `robinauts start` before `db init` refused: "the database has no schema: run
  `robinauts db init`";
- `robinauts db init`, twice, said "the database is at schema version 1 (schema.sql
  67cd08b94fe7)" both times, and left the controller's six tables and the engines' five;
- two turns in headless Chromium, each with a run id of its own, both re-attaching from
  position 1; one user, one session, four messages, two finished turns and eighteen
  events in the tables;
- the server stopped cleanly and started again: the conversation was listed, its thread of
  four messages read back with each answer's `provenance.run_id`, and no turn running;
- a second `robinauts start` on another port over the same database replayed the first
  run from position 1 to `RUN_FINISHED`, and a conversation started through the first
  process was watched to its end and read back through the second;
- a turn on the restarted session over echo ended `failed`, with the engine's refusal
  recorded on the turn: echo keeps its memory in the process, as the plan says.

Not verified in the browser: a second turn that remembers the first after a restart,
which needs a framework engine and a model key this environment did not have; the engines'
contract suites over PostgreSQL storage prove the memory itself. A server killed during a
turn, and the lease ending it, needs a turn slower than echo's; the unit suite covers the
lease (`test_a_turn_whose_lease_has_passed_is_interrupted_and_a_new_turn_starts`) and the
store's races cover the SQL.

## Decided on the way

- Watching through another process a turn that has ended is a replay from the record;
  a live watch across processes is exercised by the store's two-pool test.
- The PostgreSQL engine suites live beside the unit tests, since they share those tests'
  scripted models; they are marked `database` and skip without the URL.
- The engines take their memory injected. `init_langchain` and `init_pydantic_ai` read
  the storage kind and build the saver and sessions, or the memory, for it; the engine
  classes know no storage kind, and a test hands them an in-process one directly.
