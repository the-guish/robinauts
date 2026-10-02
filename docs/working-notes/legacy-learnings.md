# What legacy still knows: the survey before block 9

Block 8 of `master-plan.md` is the plan for stage two, "capturing every learning still held in
legacy's code, tests and specs, so that nothing is lost when legacy goes". This note is the
survey that plan is written from. Five readers each took one slice of
`backend/src/robinauts/legacy/` (27,000 lines), its tests (40,000 lines) and
`docs/specs/legacy/`, read the module docstrings and the tests whole, and compared each
finding with the new layers at `refactor/cleanup` (`52a4ebf`, 2026-10-02) and with
`stage-two-plan.md`. Where a claim about the new layers mattered, it was checked by running
the new code, not by reading it; those are marked "verified".

The slices were the API layer, the application and domain core, the adapters and engines,
the datastore with the test infrastructure, and the specs, docs and scripts. Their full
reports, with every entry and pointer, are in the session's scratchpad under
`legacy-survey/`; this note keeps what block 8 and block 9 need.

How to read a pointer: `L api/protection.py:51` is `backend/src/robinauts/legacy/api/
protection.py` line 51; `tests/...` is `backend/tests/...`; `web/`, `controller/` and
`agent_engines/` are the new layers under `backend/src/robinauts/`. Stage-two items are cited
as the plan numbers them: `#N` for the data-model section, and `CC`, `LC`, `PA`, `C`, `W` and
`P` with a position for the cross-component, langchain_engine, pydantic_ai_engine,
controller, web and packaging sections (CC1 is the first bullet of "Cross-component
features", W1 the first of "web").

## The short version

1. **Eleven things are live defects in the new layers today**, found by probing them, and
   none is in `stage-two-plan.md`. Five are security: the sign-in exchange keeps the client
   secret, the code and the verifier reachable from its exceptions; an environment variable
   can replace the configured API key in both engines; the vendor SDKs' debug logging writes
   whole conversations to stderr; the local development mode answers a rebound `Host`; and
   `/ui/.env` is served. The others are robustness: a turn's end is written after the
   engine is released, so a stuck engine holds a cancel and a shutdown for ever; a stream
   that ends without `Done` leaves the turn running until its lease; LangChain has no bound
   on tool rounds; the output ceiling goes in the wrong field for `openai-compatible`;
   `Last-Event-ID` and `limit` give 500s on bad input; and a start without
   `ROBINAUTS_DATABASE_URL` runs sign-in over in-memory storage.
2. **Six things break on the day legacy is deleted**, three of them silently: the CI gate
   that fails a run whose database tests did not run, the wheel check, the test that pins
   the wheel's contents, the deployment rehearsal, the two tests that guard the `tiktoken`
   and `logfire` promises, and `backend/main.py`. They move first.
3. **The pattern most worth keeping is the one that cannot be forgotten**: a check that walks
   what the framework serves and fails closed, a contract suite kept honest by a twin that
   must fail it, an oracle run at the end of every scenario, a closed set enumerated at run
   time against values copied by hand. Legacy had one of each and the new layers have none.
4. **Most of what legacy's specs say that no current spec says is product, not legacy**: the
   model half of the configuration, the vendor quirks found in live turns, the listing and
   title rules, the channels. Block 9 writes three specs and one ADR before it deletes
   anything.
5. **`stage-two-plan.md` needs a fourth round**: the items this note lists under "What the
   plan is missing", the corrections to P1 and W7, and the six things `auth-plan.md`
   deferred to "stage two, web" that the web section never received.

## Before block 9: what breaks on deletion

Ordered by how quietly it breaks.

1. **The CI database gate.** `tests/postgres.py` imports `robinauts.legacy.datastore`, and
   `conftest.py`'s `pytest_sessionfinish` and `tests/unit/test_database_requirement.py`
   import from it. The gate counts tests marked `database` and fails a run that was asked
   for a database (`ROBINAUTS_REQUIRE_POSTGRES`) and ran none, or was narrowed with `-m`,
   `-k`, a path or `-n`. The assertion that CI's PostgreSQL is not on UTC lives in a legacy
   test (`tests/integration/test_postgres_credential_store.py:164`). Deleting legacy takes
   all of it, and CI goes green with no database. Move `database_required`,
   `database_tests_missing` and `server_settings` into `tests/controller_db.py`, repoint the
   conftest and the requirement test, port the not-UTC assertion.
2. **The deployment rehearsal.** `scripts/rehearse_deployment.py` imports legacy inside a
   function (`:513`), passes `--forwarded-allow-ips` (`:441`), writes `engine = "langgraph"`
   (`:452`), uses the old events route (`:640`, `:650`), expects `agents[0]["engine"]`
   (`:603`, `:671`) and exit 2 with its refusals (`:533-545`). No check runs it, so it
   breaks without anything going red. Its grep of every secret across the log, every
   response and every database row (`:467-499`) is the end-to-end proof that nothing leaks,
   and should dump every table of the schema, since the engines now own tables that hold
   conversation content.
3. **The wheel check.** `scripts/check-wheel.sh:95` wants `robinauts/legacy/datastore/
   schema.sql` and `:100-103` copies legacy's `HASHED` asset rule. It does not check
   `robinauts/controller/adapters/postgres/schema.sql` at all today; the new file ships
   only because hatch includes package data. `tests/integration/test_wheel_contents.py`
   imports `robinauts.legacy.api` and `legacy.datastore`, and it is what pins
   `hatch_build.py`'s sdist-or-wheel inference and checks `requirements.txt` against the
   lock. Port it; do not delete it with legacy's tests. The rule it keeps: "a wheel carrying
   a different `schema.sql` would be worse than one carrying none: the version check would
   pass and the tables would not match" (`test_wheel_contents.py:133`).
4. **Two promises held only by legacy tests.** No `tiktoken` encoding is ever asked for, so
   its BPE assets need no licence entry (`DEPENDENCIES.md:275` names the legacy test as the
   guard; `tests/unit/test_engines_over_chat_completions.py:362-368` replaces both entry
   points with functions that fail the test). `logfire` is absent from the lock, because
   `pydantic-graph` opens spans through `logfire_api`, a shim that becomes the real thing
   the moment it is importable (`test_logfire_is_not_installed_at_all` in
   `tests/unit/test_pydantic_ai_engine.py`). Port both to the new engines' tests and repoint
   `DEPENDENCIES.md`.
5. **`backend/main.py:11`** imports `robinauts.legacy.cli.run`; point it at
   `robinauts.web.cli.run`. `backend/pyproject.toml` names legacy in its import-linter
   contracts and exceptions (`:199-200`, `:211`, `:243`, `:312-320`, `:348-349`,
   `:369-370`, `:385-388`, `:405-406`, `:423-424`, `:437-440`) and in comments
   (`:11-21` is the FastAPI `<0.142` pin, whose stated reason is legacy's route walk;
   `:57-67`, `:113-121`, `:147-161`).
6. **`docs/deployment.md` is wrong today**, not only on deletion: its systemd `ExecStart`
   (`:343`) passes `--forwarded-allow-ips`, which the new command rejects with exit 2, and
   `RestartPreventExitStatus=2` (`:349`) then keeps the service down. See "The command".
7. **Three specs and one ADR to write first**, because current files cite them:
   `docs/specs/agents.md` (the model half of the configuration, the known findings),
   `docs/specs/conversations.md` (the product half of the legacy one), `docs/specs/
   channels.md` (moved), and ADR 0006 (the engines keep their memory in tables of their
   own, superseding ADR 0005's memory clause). Those three names resolve 38 of the 53
   citations in 17 frontend files that already point at `docs/specs/{agents,conversations,
   runs}.md`; the 15 that cite `runs.md` repoint to `wire.md`.
8. **Tag the last commit that has legacy** (`legacy-final`), so the fourteen dated working
   notes that cite legacy paths, and the stage-two items that do (#22's `legacy/domain/
   values.py`, #16's schema tests), keep a target without being edited.

## Verified gaps in the new layers

Each was found by reading legacy and confirmed by running the new code. None is in
`stage-two-plan.md`. They are the first items block 8 adds.

### Security

- **The sign-in exchange leaks its credentials through exceptions.** `web/oidc.py:103-131`
  holds `secret`, `code` and `verifier` as locals, and `_send` (`:165-172`) raises
  `SignInError ... from error`; the `httpx` cause carries the `Request`, whose body holds the
  code and the verifier and whose `Authorization` header decodes to the client secret.
  Verified with `capture_locals` printing, which is what error reporters do. Nothing logs
  these with `exc_info` today; the first error reporter added would. Legacy's rule
  (`L adapters/identity_provider.py:42-52`, `:327-580`): the frames that touch a secret
  return a value, never raise; the secret, the form and the headers are cleared in a
  `finally`; the token request keeps no `httpx` cause; a `CancelledError` is re-raised with
  its chain stripped. The test is the traceback walker (T15 below).
- **An environment variable replaces the configured API key.** `ANTHROPIC_CUSTOM_HEADERS=
  "x-api-key: ..."` made both new engines send that key (verified). `OPENAI_ORG_ID`,
  `OPENAI_PROJECT_ID` and `OPENAI_ADMIN_KEY` reach every OpenAI client too, and for a
  protocol kind with no `base_url` the SDKs read `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL`.
  Legacy (`L adapters/agents/langgraph/engine.py:242-306`, `:351-367`, `:510-530`; the same
  in the Pydantic AI engine) passed the endpoint and the key as arguments, passed the key
  again as a default header so the caller's header wins, and removed at engine construction
  the variables no argument can refuse. The legacy spec's rule
  (`docs/specs/legacy/agents-engines-models.md:195-240`) is promised to operators in
  `docs/deployment.md:233-243` and `:582-586`, and `docs/specs/agent-engines.md` says only
  "build the vendor's client".
- **Vendor SDK debug logging writes every conversation to stderr.** `ANTHROPIC_LOG=debug`
  is read at import, and an operator who turns the root logger to DEBUG, "which is a thing an
  operator does", gets the system prompt and every message. The new engines pin no logger
  (`grep getLogger` under `agent_engines/` finds nothing); legacy's negative controls
  (`tests/unit/test_langgraph_engine.py:1291-1300`, `:1380-1391`) still pass, so the leak is
  real on the installed SDKs. Legacy pinned six loggers at WARNING by their own level, not
  their effective one (`L adapters/agents/langgraph/engine.py:308-349`, `:533-580`):
  `anthropic`, `anthropic._base_client`, `openai`, `openai._base_client`, `httpx2`,
  `httpcore2`, the emitting child as well as the parent, because a `dictConfig` entry on the
  child walks past a pin on the parent.
- **The local development mode answers a rebound `Host`.** A page on the internet points a
  name of its own at `127.0.0.1`; the browser connects here and every same-origin rule holds
  for that page. With `Host: evil.example` and a matching `Origin`, `POST /api/turns` started
  a turn and `GET /api/conversations` listed them (verified). Legacy
  (`L api/protection.py:51-71`, `:422-524`; `tests/unit/test_local_mode.py:364-560`)
  required on every request, reads included, exactly one `Host` naming a loopback host and a
  server address that is loopback or a unix socket, and judged every write as credentialed
  against the origin of the `Host` it named. `auth-plan.md` deferred this to stage two's web
  section, which never received it, and `docs/specs/sign-in.md:117-140` still states it in
  the present tense.
- **`GET /ui/.env` serves the file.** `web/app.py` mounts a bare `StaticFiles`
  (verified: 200 with the contents). Legacy's `NothingHidden` (`L api/ui.py:255-347`)
  answered 404 for any path with a segment starting with a dot, the same answer as a missing
  file: "a `.env` somebody put beside the bundle" is a deployment's database URL.
- **Authorization codes and `state` reach the access log.** The new command runs uvicorn's
  default access log, which writes the full query, and `GET /auth/callback/{provider}?code=
  ...&state=...` is a sign-in. Legacy's `NoQueryStrings` (`L cli.py:233`, `:259`) cut every
  access-log line at the `?`, for every path rather than a list of sign-in paths to keep in
  step with the routes. `docs/deployment.md` section 8 promises it.
- **Validation errors echo the request back.** A `PATCH` with `{"title": {"sk-secret": 1}}`
  answered FastAPI's `{"detail": [{"type": ..., "input": {"sk-secret": 1}}]}` (verified),
  which the frontend cannot read and which repeats whatever was sent. Legacy
  (`L api/errors.py:156-224`, `:417-478`) turned a `RequestValidationError` into one
  `InvalidValueError("<location>: <rule>")`, the rule a sentence of ours keyed on pydantic's
  error `type`, unknown keys counted and never named, locations clipped to their string
  pieces, "because a secret pasted into the wrong tool arrives as a key as readily as as a
  value".
- **5xx bodies carry `str(exc)`.** `web/app.py` maps 7 of the 12 contract error classes and
  sends the exception's text for every class, 500s included; a `ConfigError` from the pool
  can read "the database could not be opened: ..." with the driver's sentence. There is no
  handler for bare `Exception`, no `nosniff` or `Referrer-Policy` on any answer (verified on
  `/health`), and a 405 says `Allow: GET` where DELETE and PATCH exist (verified). Legacy
  (`L api/errors.py:243-398`, `:481-578`) answered every 5xx with one fixed body, every
  not-there and not-yours with one byte-identical body, gave the catch-all handler the
  security headers because Starlette builds that response beyond our middleware, and rebuilt
  `Allow` from every route whose pattern matches.
- **No body bound, and the body is read before anyone is identified.** FastAPI parses the
  body before `current_user` runs, so anyone can make the server read any amount with no
  session. Legacy's bound (`L api/protection.py:166-204`, `:384-419`, `:538-710`) was pure
  ASGI middleware on writes: the declared length refused over 1 MiB before a byte is read,
  the digit count checked before `int()`, a body with no length counted in a wrapped
  `receive` and cut without waiting, the 413 replacing whatever the app made of the cut,
  `GET` left alone because a streaming response listens on `receive` for a disconnect.
  W1 names "the one-mebibyte body bound" and nothing of this.

### Robustness

- **The turn's end is written after the engine is released.** `controller/application/
  turns.py:164` wraps the stream in `aclosing`, so the stream's close runs before the
  `CancelledError` branch writes the end. Verified: an engine whose cleanup takes 3 s makes
  `cancel_turn` take 3 s; one that never lets go keeps `close(timeout=0.1)` waiting past 3 s
  with the turn still `running`. `InProcessDispatcher.close` (`controller/adapters/
  dispatch.py:40-48`) waits on cancelled tasks without a bound, so one stuck MCP connection
  holds a shutdown for ever. Legacy's `_Pump` (`L application/turns.py:1757`, `:1334`) read
  the engine through a task of its own over a queue bounded at eight, wrote the end first,
  then cancelled the reader and waited with `asyncio.wait(timeout=5)`, abandoning an engine
  that held on with one log line. C2 says "a bounded cancel" and names neither the order nor
  `close`.
- **How a turn ended is decided in three places, and three ways fall through.** Verified: a
  stream that ends without `Done` leaves the turn `running` with no error until its lease,
  so the session answers 409 to every new turn for the model's timeout plus a minute; a
  cancel whose engine raises while closing records `failed` with the close error; a timeout
  records `failed` with the error `''`. Legacy's `_turn` (`L application/turns.py:935`)
  mapped every way out of the engine loop to one `_Ending` from a closed table: no `Done` is
  failed "the agent produced no answer"; `Done` with a message still open is failed "began
  an answer and never completed it"; the turn's own deadline is `TIMED_OUT`; an exception
  while the task is being cancelled (`task.cancelling() > 0`) is still the cancellation, the
  error only logged.
- **What a failed turn records is unbounded, often empty, and logged nowhere.** `turns.py:
  218` stores `clean_text(str(exc))`; nothing under `controller/` imports `logging`. Legacy
  (`L application/turns.py:2209`; `L core/runs.py:170`) kept one sentence, "Type: message",
  cut at 2,000 characters with a marker, never empty, and logged the chain of causes and the
  innermost frames on one line, escaped and bounded. "A run record is not a log."
- **No bound on tool rounds in LangChain.** `langchain_engine/engine.py:84-86` calls
  `astream` with no `recursion_limit`; the installed default is 10007 steps, about 3,300
  tool rounds, movable by `LANGGRAPH_DEFAULT_RECURSION_LIMIT`. Pydantic AI inherits the
  framework's 50 requests. Legacy bounded both at 25 rounds from one number translated into
  each framework's unit (`L adapters/agents/langgraph/engine.py:413-433`, `:950`; Pydantic
  AI `:396-407`, `:943`), "so that a model that keeps calling tools cannot run a turn for
  ever on the operator's account".
- **The output ceiling goes in the wrong field for `openai-compatible`, locked in by tests.**
  `tests/unit/test_langchain_engine_over_chat_completions.py:100` and the Pydantic AI twin
  assert `max_completion_tokens` to a compatible gateway; OpenRouter and older compatible
  servers take `max_tokens` only. Legacy sent `max_completion_tokens` to `openai` and
  `max_tokens` to `openai-compatible`, no ceiling to OpenAI when none is configured (its
  reasoning models count reasoning tokens against it), and 8192 to Anthropic from both
  engines (`L adapters/agents/langgraph/engine.py:217-229`, `:769-789`). The new defaults
  differ between engines: Pydantic AI sends Anthropic 4096, LangChain whatever the profile
  says, 128000 for a known model.
- **`ChatOpenAI` decides on its own.** A `gpt-5.5-pro` on a compatible gateway goes to the
  Responses API (verified), the stored memory then holding Responses-shaped blocks; a
  120 s gap between chunks ends a turn whatever `timeout_seconds` says (verified,
  `stream_chunk_timeout == 120.0`); the HTTP client comes from a process-wide cache. Legacy
  pinned `use_responses_api=False`, `stream_chunk_timeout=None`, `stream_usage=True` and its
  own clients (`L adapters/agents/langgraph/engine.py:688-766`). The legacy spec calls the
  Responses API "a decision of its own": an ADR, "the OpenAI kinds speak Chat Completions".
- **Unsigned `thinking` blocks break a LangChain conversation for good.** A model that signs
  nothing behind an Anthropic-compatible endpoint sends `thinking` without a signature; the
  Messages API refuses the whole request when such a block goes back with a tool's results.
  The new engine replays the checkpoint unchanged, so once stored every later turn with a
  tool result is refused. Legacy's middleware dropped them from what is sent and kept them
  in memory (`L adapters/agents/langgraph/engine.py:1009-1047`).
- **Numbers from the client are read with `int()`.** `Last-Event-ID: abc` and 5,000 nines
  each gave a plain-text 500 (verified); `?limit` and `?after` carry no bound, and a
  negative `limit` is a PostgreSQL error. `{"regenerate": <unknown uuid>}` is a 500 from a
  `next()` with no default (verified), the same shape as #28. Legacy
  (`L api/stream_routes.py:344-374`; `L api/protection.py:824-842`) bounded the digit count
  before `int()`, required `isascii() and isdigit()`, refused any header or query parameter
  sent twice, declared `limit` as 1 to 100 in the document and tested that the document's
  bound is the application's. #20 and W3 cover parts; a non-numeric id, a repeated parameter
  and the `limit` bound are nowhere.
- **A start without `ROBINAUTS_DATABASE_URL` runs sign-in over in-memory storage** and
  loses every conversation at restart; `operations.md` says PostgreSQL is always required.
  An unset `ROBINAUTS_CONFIG` is a `KeyError` traceback. A configuration refusal exits 1,
  so a systemd unit restarts it for ever on a misspelt key. See "The command".

### Smaller gaps, verified or read

- No heartbeat on the stream, no seeding of the mapper on a re-attach, and no terminal
  event when `watch_turn` raises in mid-stream: the client reconnects for ever. Legacy
  (`L api/stream_routes.py:398-583`; `L api/agui.py:363-419`): every refusal before the
  first byte, any exception after it is `RUN_ERROR` code `internal`, the heartbeat waits on a
  reader task with `asyncio.wait` rather than `wait_for` (a `wait_for` timeout is a
  cancellation inside the generator, "the end of the stream, not a heartbeat"), and a
  re-attach seeds the mapper by running the mapping over the events up to the position.
- `web/agui.py:101` returns `()` for an unmatched event kind, so a `TurnEvent` added later
  is dropped without a word, and `:112-116` opens a thinking block on an empty reasoning
  piece. Legacy raised on an unmapped kind and tested one sample per member of the union.
- A failed `LISTEN` connection breaks every stream (`controller/adapters/postgres/
  store.py:479` raises from `asyncpg.connect` into `watch_turn`). Legacy's watcher
  (`L application/watch.py:305`) treated the wake-up port as a hint: one WARNING per
  stream, then polling at the wait bound.
- A watcher of a purged session breaks instead of ending (`events_after` raises after the
  headers went out). Legacy (`L application/watch.py:279`): a run that is no longer there is
  over.
- A corrupt or newer stored document becomes a 422 that quotes the row, and nothing is
  logged. Legacy's `StoredDataError` (`L domain/errors.py:69`; `L domain/stored.py:74`): "a
  conversation of ours that is no tree is a fault of the deployment, answered and logged as
  one, not a 422 quoting our own ids back at a browser".
- `_read_version` (`controller/application/documents.py:180`) accepts every version from 1
  and reads all of them with the current keys. Harmless at version 1; the first bump
  misreads every older document. Legacy kept one registry of upgrades per shape, each
  handed a deep copy, required to move the version forward (`L core/conversation_format.py:
  376`).
- A sign-in never refreshes the user's name and email: `add_user_if_absent` is `ON CONFLICT
  DO NOTHING`. Legacy's `_USER_AT_SIGN_IN` (`L datastore/credentials.py:146`) wrote what the
  provider said this time, `None` included.
- The cap on pending sign-ins and the sweeps of expired sessions, sign-ins and tokens fell
  between two plans: `auth-plan.md` sent them to "stage two, housekeeping" and CC4 lists
  trash and retention only. `busy` is a code nothing raises. Legacy's cap
  (`L datastore/credentials.py:19`, `:105-124`): count and insert in one transaction under
  `pg_advisory_xact_lock` with a key space of its own, because `SELECT count(*)` sees
  committed rows only, and the hash's shape checked in Python first, because a CHECK never
  runs on a statement that inserts nothing. The new schema lock uses `1382508616`; the cap's
  space must differ.
- ID-token leniencies learned from real providers are gone: Google's bare
  `accounts.google.com` issuer, accepted from a token and never from discovery
  (`L core/claims.py:120`); `email_verified` sent as the string `"true"` (`:297`); a groups
  claim that is one string (`:235`); a minute of skew on `exp` (`:161`). Identity-provider
  answers are inflated whole with no byte bound (legacy refused a content encoding and a
  `Content-Length` over 256 KiB, "half a megabyte of gzip is sixty-seven megabytes"), and
  408, 425 and 429 now read as `provider_refused` rather than `provider_unavailable`. Do not
  carry legacy's issuer normalisation: it strips a trailing slash, and Auth0's issuer has
  one.
- `SignedIn.secret` (`web/sign_in.py:246`) is in the default repr; legacy hid every
  secret-bearing field with `field(repr=False)` and scanned every stored row and every repr
  for the raw secret (`tests/unit/test_signin_flow.py:1007`, `:1048`).
- A sign-in that fails leaves the login cookie in place and answers 302 with no `no-store`;
  the provider id reaches the log unchecked; `Max-Age` is rounded down with `int()`, and
  `Max-Age=0` is how a cookie is deleted. Legacy: `no-store` on every `/auth` answer, 303,
  the login cookie cleared on every failure including an unexpected exception, the provider
  id checked for shape first (`L api/auth_routes.py:65-79`, `:241-320`; `L api/cookies.py`).
- A repeated JSON key is accepted: `{"title": "Safe", "title": "Evil"}` "is a write that
  asked two things and was answered on one of them". Legacy's `read_once`
  (`L api/protection.py:732-856`) re-parsed the body with an `object_pairs_hook`, caught
  `JSONDecodeError` by its own class because `InvalidValueError` is a `ValueError` too (a
  trap `controller/contract/domain.py:22` still sets), and a test walked every route with a
  body to see it declared.
- Secrets are read per call, so an unset API key or tool-server secret is found in the
  middle of somebody's turn; a config file that is missing or not TOML is a raw traceback.
  Legacy checked every declared secret at start-up, reported every missing one in one
  `ConfigError`, named a variable only when its name matched `[A-Z_][A-Z0-9_]*`, and carried
  values in objects whose repr names only the ids (`L adapters/config_file.py:62-315`).
  Reading per call is defensible, since rotation works without a restart; it should be
  decided, not inherited.
- `parse_config` reports a cascade: a provider with a misspelt key also yields "models.
  sonnet: provider 'anthropic' is not configured", which is false (verified). Legacy checked
  references against what the operator declared, not what built, and checked the value rules:
  an endpoint `https` or `http` on loopback with no userinfo, query or fragment, the port read
  inside the `try`, an env name shaped like one (`L core/models_config.py:159`;
  `L domain/agents.py:256-277`). C8 names the gap generically.
- `rename_session` writes the whole record back through `update_session`, whose SQL checks
  neither owner nor `deleted_at`, against `data-model.md`'s "every operation on a session
  names its owner". Low impact; one line in C.
- The per-turn vendor client is never closed, in legacy and in both new engines; the
  spec's "let go cleanly ... the HTTP connections" is not kept.
- A tool's output has no bound in the transcript; legacy cut it to one part's worth,
  1,000,000 characters, in the transcript only, the model still seeing the whole result.

## Patterns worth keeping

Grouped by where they live. Each gives the legacy pointer, the pattern, its reason in
legacy's words where there are any, and where it should be written down.

### A. Checks that cannot be forgotten

- **Every route declares a permission, and a walk fails closed at build and at start.**
  `L api/access.py:4-55`, `:220-343`; `L api/web.py:133`, `:193`; `tests/unit/
  test_api_access.py:131-500`. A route carries `public()` or `signed_in()`, a dependency
  whose callable names the permission. `undeclared()` walks every list a router keeps routes
  in, into included routers and mounts, and reports anything that is not a declared
  `APIRoute`: a plain Starlette route, a websocket, a mounted app, a `frontend()` group, a
  declaration passed to `include_router` rather than written on the route. A hand-written
  list excuses non-API things by name and never an `APIRoute`. A backstop reports any other
  router attribute holding routes, so a framework that grows a new place for routes stops
  the deployment: "a check that only understands the routes it was written for is a check
  that stops working the day somebody adds another kind". `create_api` ran it at build and
  the lifespan at start, "so the door cannot be left open by a branch nobody ran the test
  on". The new `web/app.py` writes `user: User = asking` on each route by hand and nothing
  checks that the next one will. `docs/specs/sign-in.md:68-70` requires the walk; it is also
  the stated reason for the FastAPI `<0.142` pin. For the port: FastAPI 0.141 keeps the
  frontend group in `router._frontend_routes` as one object, not a list. Home: a W item;
  `docs/architecture/web.md` "Who may ask"; the ADR when roles arrive.
- **Write checks as pure ASGI middleware over headers folded once.** `L api/protection.py:
  4-49`, `:244-381`, `:628-729`. One pass over `scope["headers"]` lower-cases names and
  collects every value of six headers; a deciding header sent twice is 422, "answered by the
  first value here and by the second somewhere else, which is how `Sec-Fetch-Site:
  same-origin` followed by `Sec-Fetch-Site: cross-site` becomes an accepted cross-site
  write"; `Sec-Fetch-Site` is an allow list of `same-origin`, `none` or absent, not a list of
  the bad values; `application/json` on every write; `Origin` equal to `public_url` on a
  write carrying the cookie; `isascii()` before `lower()`, since U+212A folds onto `k`; a
  websocket closed with 1008 before it is accepted, "the one kind of connection that carries
  cookies and answers no preflight". The new layer has the `Origin` rule only, as a
  dependency reading the first of two values. Home: an ADR "Request protection without a
  CSRF token" (the choice and the three checks), `web.md` for the order and the folding, and
  the W1 detail.
- **The interface mount: dotfiles, headers on refusals, the exact hash rule.** `L api/
  ui.py:98-112`, `:138-239`, `:255-463`. `NothingHidden` 404s any dotted segment; `UiHeaders`
  sets the CSP, `X-Frame-Options` and `Cache-Control` with `setdefault` and builds the 404
  and 405 inside the mount so a refusal carries the policy too; an asset under `assets/`
  whose name ends in exactly eight hash characters is cached a year and immutable, "exactly
  eight, not at least: the digest's own alphabet includes the hyphen"; everything else is
  `no-store`; an installation without the interface answers a 503 page with no script. The
  caching rule is in no doc, only in `frontend/vite.config.ts:486-493` and
  `scripts/check-wheel.sh:100-103`. Home: W1 detail; `web.md` "The interface";
  `frontend.md` for the digest rule.
- **The import rules carry their reasons and a probe that proves each bites.** `docs/
  layout.md:232-272`. The source of each third-party rule is the whole package so a module
  added later is inside it; `langsmith`, `logfire`, `logfire_api` and `opentelemetry` are
  named though nothing imports them, so "nothing phones home" cannot become an import
  nobody looks for; a test wrote a probe module importing a framework into a copy of the
  package and asserted the contract breaks. Today's `test_architecture.py` only runs
  `lint-imports`; a misspelt or emptied `source_modules` makes a rule void and nothing
  notices. Home: `docs/architecture/rules.md` for the reasons; the probes restored.
- **In-process state may only be a cache, a scheduling hint or a diagnostic counter.**
  `docs/layout.md:188-202`. Never a session, a pending sign-in, an ownership or a counter
  that enforces a limit; the test is that losing it is invisible and a second process with
  its own copy disagrees about nothing that matters. It matters more now that block 6 made
  several processes on one database real. Home: `rules.md`, beside the import rules.
- **Ids and secrets come from separate sources.** `docs/layout.md:111-113`, `:139-143`: an
  id is public and may be predictable in a test; a secret never. Home: `rules.md`.

### B. Errors, logs and what reaches a browser

- **A message names the field and the rule, never the value.** `L domain/values.py:101`;
  `L core/conversation_format.py:447`; `L api/errors.py`. "Text of 12 characters", never
  its content; unknown keys counted; a value that must be quoted for the log cut at 120
  characters; the browser gets the class and a fixed sentence per class, the detail goes to
  the log. The new `documents.py` quotes `{value!r}` in seven refusals, `_decode_cursor`
  quotes the cursor back, `checked_claims` repeats a whole claim into the log. Home:
  `controller.md`, an Errors paragraph; W2 extended; the sentinel test (T7).
- **Outside text reaches the log only through one door.** `L domain/logs.py:31-122`:
  `shown()` clips to 120 characters before escaping with `ascii()`, so no newline or control
  character survives ("a server writes them percent-decoded, so `%0A` in a path arrives as
  a real newline") and appends "+N more"; `chain()` writes up to five causes on one line;
  `where()` the innermost cause's last 15 frames. `web/app.py:517` logs the provider path
  parameter raw. Home: one logging rule both controller and web follow, which is why legacy
  kept it in `domain`.
- **A stored row we cannot read is our fault, not the request's.** Two doors: a `*_stored`
  reader turns any refusal into a 500 with a fixed body and the cause chained into the log;
  an id the request named that is not there stays a 404. Home: `data-model.md` "Versions";
  W2 gains the class.
- **A status per error class, found through the MRO, with the reverse test.** `L api/
  errors.py:243-326`; `tests/unit/test_api_errors.py:64-91`: every subclass has an entry of
  its own, and a stale entry fails too. Home: W2.

### C. Streams and the wire

- **Every stream ends with a terminal event.** See the gap above. The rules for `wire.md`:
  the `internal` code, the `gone` code for a run that no longer exists, an open thinking
  block closed before any error event, events the platform did not number carrying no
  `id:`. For `web.md` "Streams": the task-and-wait heartbeat and seeding by replay.
- **The AG-UI mapping is exhaustive over a closed union and skips empty deltas.** `L api/
  agui.py:290-361`; `tests/unit/test_agui.py:603-645`. `of()` raises for a kind with no
  branch; one sample per member of `get_args(TurnEvent)`; an empty reasoning delta never
  opens a block. Home: a W item; the closed-set technique (T12).
- **Numbers in headers and queries.** See the gap above; `wire.md` gets the `Last-Event-ID`
  parsing rule and the `limit` bound with the keyset's honest promise: "paging walks a list
  that is changing; it is not a transaction over a frozen one" (`L ports/conversations.py:
  207-219`); one extra row answers "is there a next page"; the cursor need not be signed.
- **Request bodies refuse unknown fields and state the record's bounds in the document.**
  `L api/schemas.py:16-35`, `:502-589`: `extra="forbid"` on every request model, each field's
  bounds taken from the record's own constant and asserted equal, a field always sent given
  no default so a generated client types it `T | null`. `wire.md:139` lists the refusal as
  not yet served. Home: `web.md`; a W item.
- **Sign-in navigations.** `no-store` on every `/auth` answer, 303, the login cookie cleared
  on every failure, cookies `Lax` because `Strict` "would drop the `state` cookie on exactly
  that request, so no sign-in would ever complete". Home: a W item; one line in `sign-in.md`
  for `Lax`.

### D. The turn's lifecycle

- **The outcome table and the order of the end.** See the two gaps above. `controller.md`
  gains a Runner section stating: the end is written first, then the engine is released
  under a bound; every way out of the loop maps to one ending from a closed table; the end
  write is retried three times, each attempt bounded, `_to_the_end` waiting on the shielded
  write however many cancellations arrive and raising the first afterwards, a caller with a
  deadline getting `_StillWriting` and the write left to land with a callback that logs what
  became of it, the shutdown bound derived from the attempts "rather than written down
  twice" (`L application/turns.py:1549`, `:2122`, `:2024`). The new `_end` is one unshielded
  attempt; a second cancel during it leaves the turn running until its lease.
- **The single writer settles an unknown outcome against the store.** `L application/
  turns.py:1820-1965`: a refused write and a write whose await never returned are one
  question, "what is stored where I offered?": there and equal, done; the run ended, stop
  quietly; something else there, two writers; absent, offer again. Mostly carried by the
  data model's "the same document again is accepted"; the residue is that `_Writer.append`
  counts the position before the await and nothing retries, so a write lost before commit
  leaves a gap, and nothing says whether gaps are allowed. Home: #23 extended;
  `data-model.md` says whether positions may have gaps.
- **A shutdown is one deadline, spent by everything it waits for.** `L adapters/
  run_executor.py:12-43`, `:175-344`; `L app.py:299`, `:711`, `:866-879`. "A close that gave
  each of them a bound of its own would be a shutdown that takes the bound times however many
  runs were in flight, which is a process that will not stop." A task cancelled before its
  first step never runs a line of its work, so its `except CancelledError` never runs
  either; the done callback sees it and the submitter's `never_began` report runs while the
  stores are still open. The composition root opens once, refuses a second call, closes
  what it took on any failure of `open` (the new `open` leaves the pool open when building
  an engine fails), closes everything in reverse even when a closer fails, says "stopping"
  before it cancels so runs end `interrupted`, and closes the stores last because a
  cancelled run's last act is to write its end. The shutdown budget sizes `TimeoutStopSec`
  in `deployment.md:351-354` and the 30 s wait in `demo/stop.sh:20-24`. Home:
  `controller.md` open, close and the dispatcher; a C item for a bounded close.
- **The state machine as a table.** `L core/runs.py:80`, `:120`; `tests/unit/
  test_run_states.py:79-120`: the legal moves in one table, the test writing the legal set
  out by hand and checking every pair of states both ways; `transition` stamps the times and
  clamps each to the one before, "a record does not refuse to exist because a wall clock
  stepped backwards". The turn's moves are implicit in the stores' SQL today. Home:
  `data-model.md`, a table of turn states and the operations that move them; the pairwise
  test in the store contract.
- **The event grammar as an executable oracle.** `L core/runs.py:350`; `tests/turns.py:
  190`. One pure function states the order a turn's stored events must have, run at the end
  of every lifecycle scenario: "a run whose events cannot be read back is a run no watcher
  can follow, however right the record looks". `data-model.md` promises "what a person
  watched arrive is what is stored" and nothing checks it. Home: `data-model.md` "A turn's
  event" as rules; a helper shared by the runner tests and the engines' suite.
- **The wake-up channel is a hint**, a lost signal costs one wait, a position already
  passed is an answer at once, a wake-up that led to nothing is not believed twice in a row
  so the loop cannot spin (`L application/watch.py:305`; `L adapters/run_signals.py`). Home:
  one paragraph in `controller.md`; the failure list of `the-path-of-one-message.md`.

### E. The store, the schema and documents

- **The bounds, each with its reason.** `L domain/values.py:52-86`, `:285`;
  `L domain/conversation.py:83`, `:96`; `L domain/run.py:29`; `L domain/events.py:42`;
  `L domain/turn.py:290`; `L domain/tools.py:31-37`; `L domain/agents.py:39`, `:89`. A part
  1,000,000 characters, long text split into parts never cut; a message 64 parts; a title
  120; a run error 2,000; a checkpoint id 256; a position 2^53-1, "JSON has one number type
  and a browser reads it as a double"; a tool name 64 characters of `[A-Za-z0-9_-]`, which
  both vendors require, so `<server id>_<tool>` must fit it (CC6) and a 40-character server
  id leaves 23; a call id 128; a system prompt 100,000; a timeout at most 3,600 s; extras
  64 KiB, depth 32, 4,096 values, measured by an iterative walk before serialising so depth is
  a refusal and not a `RecursionError` (the new `clean_json` recurses with no limit).
  `data-model.md` "Size" defers to "stage two's bounds" and no item lists them. Home: a new
  item "Bounds" carrying this table; #20 gains the position cap.
- **Every constraint named, prefixed with its table, and the names pinned.** `tests/unit/
  test_datastore_schema.py`: cheap tests over the text of `schema.sql` with no database:
  every constraint named and starting with its table, every enum value in its CHECK, the
  partial index's states the domain's, no `timestamp` without zone, every `expires_at`
  indexed "a sweep that scans the whole table is a sweep that holds it", the version row
  last with `DO NOTHING` never `DO UPDATE` ("an update here would relabel version 0's tables
  as this version"), every refusal naming the command. A scratch run against the new
  `schema.sql` found all of it holds by care; nothing enforces it, and the four names
  `store.py` translates are string literals a rename turns from a 409 into a 500. Home: #16;
  `CONTRIBUTING.md` "Changing the database schema", which still names legacy paths.
- **The search-path refusals, six of seven tests not ported.** `tests/integration/
  test_postgres_search_path.py`: a complete schema further along the path does not make an
  empty one look finished; another application's `users` further along does not make it
  look occupied; a temporary table found first is refused by the check and by the create; a
  path naming no schema is said so without advising `db init`; a view named `users` is not
  the table. The code is ported; one test is. Home: #16.
- **Documents at the store boundary.** `L datastore/conversations.py:66`, `:1086-1149`: a
  key that is not a string at any depth is refused before the server sees it, because
  `json` coerces `1`, `True` and `None` to strings "without a word, and a mapping holding
  both `1` and `"1"` comes back with one of them gone"; NaN and infinity refused; a document
  deeper than the writer can walk refused; what is handed back is never the caller's object
  and never the store's own. The new memory store returns the stored mapping itself; the
  contract says "any mapping" and tests none of this. Home: `data-model.md` "Documents"; two
  contract tests.
- **One clock, handed in; naive times refused.** `L ports/clock.py`; `tests/fakes/
  clock.py`; `L datastore/credentials.py:366`. The runner reads `datetime.now(UTC)` in five
  places, so lease tests sleep; `documents.py` refuses a naive time and the store does not,
  though asyncpg reads one as UTC and "a clock that lost its time zone would shorten or
  lengthen every session by the deployment's offset and say nothing". CI's PostgreSQL runs
  in a quarter-hour zone on purpose (`ci.yml:73`). Home: the testing note; `data-model.md`
  "No clocks and no ids in a store".
- **Deadlocks are retried a bounded number of times and each retry is logged**, because
  "a store that silently swallowed a deadlock would turn a lock order somebody got wrong
  into a deployment that is merely slow, and nothing would ever say so" (`L datastore/
  conversations.py:148-161`). Home: a data-model item with the all-methods race (T20).
- **Upgrading old documents, with guards**, and the migrations plan: until the first
  release the schema is one file edited in place; after it, numbered SQL files applied in
  order by `robinauts db migrate`, each in a transaction, recorded in `schema_migrations`,
  no framework (`docs/specs/legacy/backend.md:49-74`; `operations.md:16-19` links there).
  Home: `data-model.md` "Versions"; `operations.md` "Deployment".

### F. Engines and vendor clients

Most of these are in `docs/specs/legacy/agents-engines-models.md` "Known findings"
(`:494-620`), dated prose that block 9 deletes. They belong in `docs/specs/agents.md` under
a dated "Known findings" of its own, each with a test in the engines' suite.

- **Vendor clients are built from the configuration, never from the environment.** The
  three layers (argument, default header, variables removed at construction) and the logger
  pins, above. `docs/specs/agent-engines.md` "Reach the model providers" and "Keep secrets
  secret" get the rule and the list; a case in `tests/contracts/engine.py`.
- **Only the model node's chunks are the answer.** `L adapters/agents/langgraph/engine.py:
  956`: the `messages` stream carries chunks from every model call in the graph, and the
  summarising middleware calls the model too. The new `events_of` turns every
  `AIMessageChunk` into deltas, wrong the day `SummarizationMiddleware` lands (CC2).
- **Context management has more rules than CC2's numbers.** Trim by whole exchanges, a tool
  call never parted from its result; the memory trimmed again on the way out so "the
  platform stores what the model saw and no more"; the trigger a token count, not a fraction,
  because a gateway's model ids have no profile; the window the configured one, then the
  profile, then 200,000; no tokenizer on a turn's path; cache placement differs at a gateway
  (Anthropic's automatic breakpoint at the vendor, a breakpoint on the last message at a
  compatible endpoint, the instructions and tools marked in both). `L adapters/agents/
  pydantic_ai/engine.py:994-1045`, `:764-798`.
- **Compatible-server quirks.** A tool name repeated on every streamed delta becomes
  `searchsearch`, fixed in Pydantic AI through `_streamed_response_cls` and recorded for
  LangChain (`L adapters/agents/pydantic_ai/engine.py:703-747`); reasoning arrives in
  `reasoning_content` or `reasoning`, read by Pydantic AI and ignored by `langchain-openai`;
  `<think>` in the text is thinking under one and answer under the other; GPT-5.6 Sol and
  GPT-6 refuse tools over Chat Completions when `reasoning_effort` is not `none`; the
  `refusal` field is not read. Pydantic AI's `MCPToolset` defaults to
  `tool_error_behavior="retry"`, so a second error from one tool fails the turn (CC7 gains
  the consequence); legacy also named each agent (`name=agent.id`) so the framework does not
  search the caller's stack frame at every turn, and set `instrument=False` per agent, which
  beats a later `logfire.instrument_pydantic_ai()` where the new process-wide
  `Agent.instrument_all(False)` does not.
- **The engine contract proves release.** `tests/contracts/agents.py:150-164`, `:307-333`:
  the scripted model counts open streams in a `try/finally`, the suite asserts zero after
  every turn, finished, failed or cancelled; the cancellation test waits for the first event
  rather than sleeping and bounds the wait, "so an engine that swallowed the cancel fails
  rather than hangs"; a failure mid-answer leaves what was yielded, cut short, with no
  `Done`; a non-streaming model is its own case. The new suite sleeps 0.2 s, awaits without
  a bound and checks nothing about the model's stream (LC8 says "extend the suite" and not
  with what).

### G. Sign-in

Beyond the gaps above: discovery is one fetch per provider shared by every waiter and a
failure is not cached (`L application/sign_in.py:208`); an endpoint is rebuilt from its
checked pieces and one whose own query would set the request's parameters is refused (Okta's
may carry a query, and `f"{endpoint}?{query}"` breaks on it); a code past `MAX_CODE_CHARS`
is refused unsent; `state` is compared in constant time; the clock is read again after the
exchange. `docs/specs/sign-in.md` "Providers" gets the leniencies; a W item the rest.

### H. The command and operations

- **The command's contract.** `L cli.py:25`, `:80`, `:259`, `:357`, `:396`, `:481`. Exit 0
  done, 2 "change something and run again" (usage and configuration, one line per problem,
  no traceback), 1 could not be done, so systemd's `RestartPreventExitStatus=2` holds.
  Logging is owned by the command: one handler on stdout, `force=True`, uvicorn's own config
  off, "a library that configured it would be deciding for whoever imported it".
  `version` prints the build, the schema version and twelve hex digits of the hash, "the
  two halves of an upgrade", with no database. The database URL is never an argument, "a
  url on a command line is a password in a shell history". `server_header=False`. A unix
  socket bound by the command (`:101-184`, `:501-605`): refuse a path that exists; `bind`,
  `chmod` to `--uds-mode` 0600, then `listen`, so the socket is never open to all (uvicorn
  chmods its own to 0666); hand uvicorn the listening socket; unlink on the way out only if
  `(st_dev, st_ino, st_mtime_ns)` is still ours; signal handlers that unlink, restore and
  re-raise. Decide per flag whether to port or to drop from `deployment.md`; today the doc
  describes a command that does not exist.
- **PostgreSQL is required for a deployment**; in-memory storage is for tests and the local
  mode. `operations.md:10-11`. A start with sign-in configured and no database URL should
  refuse.
- **The wheel and the release.** One `.dist-info`; the interface, the schema and the three
  licence files inside and listed as `License-File`; no dotfile under `ui/`; every asset name
  carries a digest; `requirements.txt` beside it with every pin hashed; installed the
  deployment's way, the locked set with hashes then the wheel with `--no-deps`. Home:
  `operations.md` "a release is the wheel plus its hashed locked set"; P1 rewritten.
- **Swagger and ReDoc are not served because both load from a CDN** (`docs/specs/legacy/
  backend.md:16-18`); the new web keeps `docs_url=None` with no reason given. Home: `web.md`.
- **Opening a database.** Legacy stripped URLs and `password=` from the driver's sentence
  (`L domain/errors.py:298`); the new `pool.py` puts the sentence in the `ConfigError`
  unredacted. A malformed URL raises a plain `ValueError` that neither catches.

## Test techniques to keep

Whatever happens to the code they test. The helpers that import nothing of legacy's can be
kept as they are: `tests/aio.py`, `tests/standin/provider.py`, `tests/sse.py` (a driver that
hands a streaming response to the test chunk by chunk and sends `http.disconnect`, which
`httpx`'s ASGI transport cannot), `tests/chat_completions.py`, and the two helpers `_raced`
and `_INTERLEAVINGS` inside `tests/contracts/conversation_runs.py`, which can be copied out.

1. **The racy twin.** `tests/unit/test_fake_conversation_store.py:43`, `test_fake_
   credential_store.py:45`. The fake is the honest store with one lock per operation and an
   `await` between each read and its write; the twin removes only the lock; the suite runs
   against the twin and the test asserts that exactly the tests about two callers at once
   fail and every other passes, an exact set, "so a concurrency test that stopped biting,
   or a plain test that started depending on atomicity, is noticed". The new memory store
   has no lock and no yield, so a twin needs the yield added first.
2. **Races by enumerated interleavings.** `tests/contracts/conversation_runs.py:1337-1355`:
   a read delayed by 0 to 3 loop turns with either side scheduled first, eight schedules,
   exhaustive over a cooperative fake and eight real attempts over PostgreSQL; `asyncio.
   gather` of N identical writes asserting exactly one success. The new store contract has
   no concurrent test; #15's fix (one `REPEATABLE READ` read-only transaction, which cannot
   raise a serialization failure, so nothing is retried and no isolation level is left on a
   pooled connection) is proven by exactly this test.
3. **The oracle after every scenario** (D above).
4. **Scripted engines with gates; nothing sleeps.** `tests/fakes/agents.py:55` `Gate` with
   `reached` and `open` events; `tests/turns.py:176` waits for what is stored, not for where
   the engine is.
5. **Fault-injecting fakes.** `tests/unit/test_turn_lifecycle.py:1293-1611`: a store that
   commits then never acknowledges, a cancel during the end write, a store unreachable N
   times, engines that never let go. The new runner tests have one slow store.
6. **The pair table** (D above) and **seeded generators that prove they vary**: every
   prefix of 60 generated runs, asserting all ended states were produced
   (`tests/unit/test_run_states.py:809`); 4,000 fragment sequences for `publishable` with a
   NUL between two halves (`tests/unit/test_turn_events.py:146`), which #22 needs as is.
7. **Sentinels and canaries.** A refusal never contains the value (`tests/unit/
   test_conversation_format.py:568`); no secret in any stored row or any repr
   (`tests/unit/test_signin_flow.py:1007`); routes raising `SECRET_IN_A_BUG`, absent from
   the body, present in the log, the log one line (`tests/unit/test_api_errors.py:140-237`).
8. **Hostile values against every claim.** `tests/unit/test_signin_claims.py:405-480`: 21
   values a provider may send, in each of 12 claims, for an Okta and a Google provider.
9. **The ownership table, as an identity and as a cost.** Every operation that names a
   resource, run as a stranger and on a missing id, must raise the same class and leave the
   store unchanged (`tests/unit/test_conversations_application.py:476-509`); every route
   generated and asked both ways with status, headers and body equal, and a counting store
   requiring zero snapshot reads for another person's conversation, "or a clock tells them
   apart" (`tests/unit/test_conversation_routes.py:115-135`, `:1395-1430`).
10. **Drop after any block, re-attach, and the stream is the same** up to the three
    documented no-ops (`tests/unit/test_stream_routes.py:175-255`, `:458-535`): "a delta
    lost, a delta twice, a thinking block never closed, fails here". The new tests
    re-attach at 0 and at the last id only.
11. **Drive the ASGI app with a raw scope** to send what a client will not: a header twice,
    mixed case, CR and LF in a path, `..` segments, a websocket, a scope with no `server`;
    a `receive` that waits on an event nobody sets, under `asyncio.timeout`, proves a path
    does not hang (`tests/unit/test_api_protection.py:626-668`).
12. **Walk the closed sets; copy the expected values by hand.** Subclasses against the
    status table and the reverse; every sign-in code has a sentence and no two the same; one
    sample per union member; the CSP copied from the spec. "A test that imported the
    constant it is checking would pass whatever the constant said."
13. **Negative controls for "nothing happens".** Every claim that something does not happen
    is paired with a test showing it would without the engine: the conversation leaks
    without the logger pin, unconfigured headers reach a client built before the engine, an
    agent without `instrument=False` is caught by the stand-in; a subprocess with
    `ANTHROPIC_LOG=debug` covers what is read at import; `langsmith`'s env cache cleared on
    the way in and out "or a test that proved nothing would pass".
14. **Live vendor routing with a key nobody owns.** `tests/live/test_vendor_routing.py`:
    one real request per engine and protocol to OpenRouter with a key-shaped bogus key,
    asserting the request's scheme, host and path for any status, "arriving at the wrong
    host is the failure this test exists to catch"; marked `io`, not `live`, because it
    cannot spend money. The new live tests need real keys.
15. **The traceback walker.** `tests/integration/test_http_identity_provider.py:848-1153`:
    walks every exception through `__cause__` and `__context__` and every frame's
    `f_locals`, reads an `httpx.Request` as an attacker would (headers, form body parsed,
    Basic header decoded), formats with `capture_locals=True` as Sentry does, parametrised
    over eight misbehaviours; `AWKWARD_SECRET` tests RFC 6749 form-encoding with a colon, a
    space, `&`, `=`, `+`, `%` and non-ASCII.
16. **One behaviour, two engines, one table.** `tests/unit/test_engines_over_chat_
    completions.py:94-162`: an `Adapter` record knows each engine's seams; every test is
    written once and parametrised by engine; a fake kind tests the refusal branch "so that
    it is exercised rather than merely written".
17. **Timing without flakiness.** `tests/unit/test_run_executor.py`, `test_run_signals.py`:
    nothing sleeps except where the bound itself is tested; a "nothing was waited for" claim
    is asserted against a bound nothing could have waited under; a shared deadline as total
    time under half the sum; no exception left unretrieved, with `gc.collect()` before the
    assertion; a complexity ceiling (25,000 announcements under 1 s) catches a quadratic
    sweep; 5,000 parked watchers cost nothing, as a ratio against the test's own baseline
    with a noise floor, which is the test C10 needs.
18. **Linear-time guards.** 50,000 events in under 2 s; ten thousand messages read once.
19. **Static schema checks** (E above).
20. **The all-methods race, a provoked deadlock, `pg_blocking_pids`.** `tests/integration/
    test_postgres_conversation_store.py:514-655`, `:843-931`: every method that meets one
    conversation together, 40 rounds, only the port's refusals out, what is left reads back
    valid, the retry log empty; an outsider connection taking two rows in the wrong order,
    waiting on `pg_blocking_pids` rather than a sleep, "a moment a sleep happens to land on
    is a flake".
21. **Contract-suite hygiene.** A refused call writes nothing, shown by a dump before and
    after; precedence of refusals unspecified, "requiring one order would be requiring an
    implementation"; `new_store` inside the `try` so a half-built store is released; the
    concurrent calls shown to have run on several backend pids; a cost test comparing the
    late cost with the early cost, "so it measures the shape and not the machine"; after a
    whole sign-in, every table searched for the cookie secret, the `state` and the client
    secret. `tests/contracts/store.py` calls `new_store` outside the `try` and leaks the
    schema if `create_schema` fails.
22. **The stand-in provider's scripted misbehaviours**: hold, 5xx, HTML, redirect,
    oversized, chunked, overstated length, gzip bomb, deep nesting, OAuth error. The new
    tests use three.

## A testing note is warranted

There is no testing guide. `CONTRIBUTING.md` has "PostgreSQL for the tests" and "Changing
the database schema", the second still naming legacy paths. Proposed:
`docs/contributing/testing.md`, linked from `CONTRIBUTING.md`, holding: contract suites
(one per port, the fake passes first, refusals unordered, a refused call writes nothing);
the racy twin and the exact failing set; interleavings; races against PostgreSQL (a warm
pool, backend pids, every method at once, `pg_blocking_pids`, an empty retry log); the
database gate and why narrowing and `-n` are refused, and why the live tests use
`norecursedirs` and not `addopts`; time handed in, never slept for, CI's PostgreSQL off UTC
on purpose; the static schema checks; cost-shape tests; the oracle; closed sets with values
copied by hand; negative controls; sentinels; raw-scope driving; `filterwarnings = ["error"]`
and the two warnings it cannot catch; the stand-in provider; and the run executor's rule
that work is a factory, not a coroutine, "a coroutine nobody ever ran, which a test run with
`-W error` fails on".

## Specs and docs: what moves where

- **Write first**: `docs/specs/agents.md` (agents, models, providers, tool servers, the
  configuration reference with `tool_servers` and `langchain`, the checks at start-up, the
  `base_url` rule that a vendor kind takes none and a protocol kind requires one, a dated
  "Known findings"); `docs/specs/conversations.md` (the product half of the legacy one:
  one error for not-there and not-yours, the listing's total order and opaque cursor,
  forking, title derivation from the first non-blank line cut at a word boundary and never
  inside a grapheme with a renamed title never overwritten, archive, search, export, trash
  of 30 days); `docs/specs/channels.md` (moved: each message records its channel, which the
  new data model dropped without a note, and nothing in the API assumes a browser); ADR
  0006 (engine-owned memory, superseding ADR 0005's memory clause; `docs/adr/README.md`
  says ADRs are history, so block 9's "rewrite the ADRs" means status lines and links in
  0002, 0004 and 0005, not rewrites).
- **Rewrite**: `docs/layout.md` for the new layers, carrying the in-process-state rule, the
  import rules' reasons, the stored-data rule, ids versus secrets, the testing strategy
  (`io` marker, live tests never in CI, contract suites both implementations pass) and
  "logging is configured by the command alone"; `docs/deployment.md` against the new
  command (eight links, `engine = "langgraph"` at `:200` and `:216`, the flags, exit codes,
  shutdown budget, start-up log lines, refusal messages, and the vendor-client promises at
  `:233-243`); `docs/architecture/rules.md` (drop the legacy rows, add the three rules).
- **Repoint**: `docs/specs/core.md` (19 links into `legacy/`, and `:124-127` "no
  checkpointer" is #13's reversal), `operations.md` (7 links; add the command's contract and
  the migrations rule), `sign-in.md` (3 links; `:68-71` names a legacy layer), `privacy.md`,
  `wire.md`, `frontend.md`, `open-source.md`; `CONTRIBUTING.md:184-191`;
  `DEPENDENCIES.md:207`, `:275`, `:388` (the orjson row and the memory sentence are now
  false); `backend/hatch_build.py` docstrings naming `robinauts.api.ui`; `scripts/
  check-wheel.sh`; `scripts/rehearse_deployment.py`; `scripts/update-openapi.sh:7` and
  `check-tests.sh:6`; `demo/README.md`, `demo/config.py:96-103` (copies bounds from
  `robinauts.domain.agents`), `demo/robinauts.toml.in`, `demo/start.sh`, `demo/stop.sh:23`;
  `.gitattributes:3`; `frontend/vite.config.ts:490`; `stage-two-plan.md` (#22 and W6 cite
  legacy paths). `docs/legal/ip-clearance.md` gets one appended entry: the neorc-derived
  paths it names moved under `legacy/` and were re-implemented in `web/` and
  `tests/standin/` with legacy's code open, same authors and licence.
- **Delete**: the five specs and the two fence files under `docs/specs/legacy/`, after
  their rules have homes. **Leave**: fourteen dated working notes, under the tag.
- **Count**: 61 files in this slice alone: 7 deleted, 4 written first, 50 repointed or
  rewritten (33 outside the frontend, 17 frontend files whose comments cite specs that
  already moved).

## What the plan is missing

For block 8's rewrite of `stage-two-plan.md`.

- **Corrections.** P1 is partly stale: the demo already uses `tool_servers` and `langchain`
  and runs on the new command, the wheel hook does not depend on legacy, and of the CI
  scripts only `check-wheel.sh` does; replace it with the concrete list under "Before block
  9". W7 is stale: `db init` is back. C6's "a plain slice with no cursor" is out of date.
  `auth-plan.md` deferred six things to "stage two, web" and W1 received two: add the
  `Host` check, the route declaration, `Sec-Fetch-Site`, the JSON type and duplicate
  headers; `docs/specs/sign-in.md` states all of them in the present tense until then.
- **New items, web.** The sign-in exchange's exception chain; the per-request `Host` check;
  the route walk; the error bodies (generic 5xx, one not-found body, the catch-all with
  headers, true `Allow`, security headers); validation errors named by rule; the log door;
  the body bound as middleware before authentication; the interface mount (dotfiles,
  headers on refusals, the exact hash rule, the 503); the terminal event after an internal
  failure and the heartbeat's wait; the exhaustive mapping; numbers bounded before `int`
  and parameters sent once; `limit` bounds and the keyset's promise; the regenerate 500;
  repeated JSON keys; strict request bodies with bounds in the document; sign-in
  navigation hygiene; the token-exchange leniencies and the bounded provider answers; the
  pending-login cap with `busy`; access-log query strings; a sign-in refreshing name and
  email.
- **New items, controller.** The end written before the engine is released, bounded; the
  outcome table with its three verified cases; the failed turn's one bounded sentence and
  its log line; the end write retried and `_StillWriting`; a bounded close with one
  deadline and a task that never began; `open` closing what it took on failure; the
  `LISTEN` fallback to polling; a watcher of a purged session ending; `StoredDataError`;
  the upgrade registry; secrets checked at start-up or a decision to read per call; the
  configuration cascade and value rules; `rename_session` by owner; the bound on tool
  output in the transcript; the clock handed to the runner.
- **New items, data model.** "Bounds" as one table; the turn states as a table with the
  pairwise test; the event grammar as an oracle; documents at the store boundary; naive
  times refused at the store; deadlock retry; the all-methods race and the interleavings
  with a racy twin; the static schema checks and the pinned constraint names; the six
  search-path tests; the sweeps of `user_sessions`, `pending_logins` and `api_tokens`
  under CC4.
- **New items, engines.** Vendor clients from the configuration only, both engines; the
  logger pins; the tool-round bound in the shared suite; the ceiling field per kind and the
  default per vendor; `ChatOpenAI` pinned (the ADR); unsigned thinking dropped from what is
  sent; the model-node rule and the context-management rules under CC2; the repeated tool
  name; the per-turn client closed; the release count, cancel-at-first-event and failure
  mid-answer in the shared suite; the `tiktoken` and `logfire` guards; the live routing test.
- **New items, packaging.** The database gate moved; the wheel check and the contents test
  on the new schema; the rehearsal's six breaks; the command's flags, exit codes and
  database refusal; `deployment.md` rewritten; the three specs and the ADR; the
  `ip-clearance.md` entry; the tag.
- **New rules, architecture.** In-process state; the import rules' reasons and probes; ids
  versus secrets; Swagger off because of the CDN.

## Already carried, do not document again

The position on the last wire event and a thinking stretch named after its opening
position; a cancel as `RUN_FINISHED` with `cancelled`; fixed sentences for failed and
interrupted turns; the stream headers; `/docs` and `/redoc` off; `/health` holding nothing;
cookie attributes with `__Host-` on https only; the `!local` user refused through a session
or a token; one running turn per session held by `start_turn`; the question and its turn in
one operation; the hide refused while a turn runs; the first append as the claim; the same
document again accepted; `CLOSE` ending a turn `interrupted`; the lease replacing the
start-up sweep and `RunQuietError`; ownership settled before the expensive read; every
configuration problem reported at once; discovery before anything is stored; `max_retries=0`
on every vendor client; base URL and key passed explicitly; `instructions=` in Pydantic AI;
LangSmith off and the Pydantic AI banner off; a retry prompt as an error result; bearer and
basic headers and MCP timeouts; OIDC timeouts, no redirects, the discovery issuer check,
`https` endpoints, `RecursionError` caught, RFC 6749 quoting; the schema check and create
with the advisory lock and `current_schema()`; the pool docstring's reasoning; the warm test
pool and a schema per test; "a notification carries no data and a lost one costs a wait";
tool names prefixed and tool errors as results (CC6, CC7); the base-URL composition rule in
`deployment.md:167-187`.

## Do not carry

Legacy's issuer normalisation (strips a trailing slash; Auth0's issuer has one).
`redirect_slashes=False` and not serving HEAD on API routes (low cost either way). The
`quiet` code and the 504 for a watcher that gave up: the lease replaces them. The OpenAPI
per-route refusal tables (`L api/refusals.py`): the single `default` entry covers what the
frontend reads. Legacy's start-up sweep: the lease. `ROBINAUTS_AUTH_CONFIG`, the old name,
read with a warning: drop the line from `deployment.md:311`. The run signals adapter itself:
watchers now read the store, and the PostgreSQL store registers its waiter before reading.
