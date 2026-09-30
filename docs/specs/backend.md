# Backend

Python. The layers, what goes where, and the enforced dependency rules are
in [layout.md](../layout.md). This document holds the component choices.

## Web

- FastAPI on uvicorn.
- FastAPI, Starlette and uvicorn are imported only in the `api` layer and
  the composition root. The application never sees a request object.
- The OpenAPI document is committed as a snapshot, `backend/openapi.json`,
  rewritten by `scripts/update-openapi.sh`; a test keeps it in step with the
  code. The streaming endpoints are outside it — the two that start a turn and
  the one that re-attaches to a run, listed in [wire.md](wire.md) — and so are
  the sign-in redirects, which are browser navigations rather than calls.
- Swagger and ReDoc are not served: both load their JavaScript from a content
  delivery network, and nothing here is served from a third-party origin
  ([frontend.md](frontend.md)). `/openapi.json` is.
- The backend also serves the built frontend
  ([frontend.md](frontend.md)).

## Background work

- Runs and housekeeping execute inside the backend process, on the event
  loop that serves requests. There is no separate worker or queue
  ([runs.md](runs.md)).
- The ASGI lifespan opens and closes what the process holds: the database
  pool, the run executor, the housekeeping tasks.

## Database

- PostgreSQL is the one database, and it is always required. There is no
  run mode without it; in-memory stores exist only as test fakes.
- Everything stored lives there: conversations, runs and their events,
  attachments, search indexes, users, sessions, projects, audit.
- No ORM. SQL is hand-written in the `datastore` layer, which implements
  the store ports and returns domain objects.
- Where expiry is involved, one clock decides: the application computes
  every deadline from the clock port and gives the stores absolute times,
  and tells them what "now" is. The stores keep no clock of their own.
- The schema is entirely the platform's. No framework creates or migrates
  tables in it ([ADR 0002](../adr/0002-conversation-persistence.md)). The
  model's memory of a conversation — the agent framework's own history, in
  its own format — is a column of the run that produced it, stored as bytes
  the platform never reads
  ([ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md)):
  the frameworks keep no tables of their own.

## Schema

- **Until the first release, the schema is one definition, edited in
  place.** There are no incremental migrations and the schema version stays
  at 1; a database created from an older definition is recreated.
- Incremental migrations start after the first release; the schema as
  released is version 1, and neither it nor a released migration is ever
  edited. From then on a deployment is upgradable in place.
- The server never creates or changes the schema on its own. A command
  does. The server refuses to start against a database whose schema does
  not match the code, and names the command that fixes it. Before the first
  release "match" is the version **and** the SHA-256 of `schema.sql`, which
  the command records when it creates the schema: a database made from an
  older edit of the file is refused and named as one.

## Details likely to change

- The driver is `asyncpg` (Apache-2.0), imported only in `datastore`.
  `psycopg` is LGPL-3.0-only and is excluded
  ([open-source.md](open-source.md)).
- The schema definition is a SQL file shipped in the package, applied by
  `robinauts db init`. It records a schema version, which is what the
  server checks at start-up.
- When migrations start: numbered SQL files shipped in the package,
  applied in order by `robinauts db migrate`, each in a transaction,
  recorded in a `schema_migrations` table. No migration framework.
- Attachments are `bytea`; search is `tsvector`.
- Licences as known today: FastAPI MIT, Starlette and uvicorn
  BSD-3-Clause, Pydantic MIT, `httpx` BSD-3-Clause, with `certifi`
  MPL-2.0 behind it. The gates decide, at the pinned versions.
- Tooling: Python 3.12, uv, hatchling, ruff, black, pytest,
  import-linter.
