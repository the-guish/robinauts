# POC progress

The plan is [poc-scope.md](poc-scope.md). The process is the three-agent
recipe (`recipes/three-agent-steps.md`, beside this repository).

## What exists

For a reader with no memory of it. Kept short; rewritten as the steps land.

- `docs/specs/` — the specs; start at `core.md`. `docs/adr/` — two
  decisions that needed a discussion. `docs/layout.md` — the backend layers
  and their enforced dependency rules.
- `backend/` — the `robinauts` package with every layer as a sub-package,
  `pyproject.toml` with the import-linter contracts, and
  `tests/unit/test_architecture.py` which runs them. Every layer has
  something in it, `api` included, and `app.py` wires them together.
  Checks: `uv run pytest`, `uv run ruff check .`, `uv run black --check .`
  from `backend/`, or `scripts/check-all.sh` from the root.
- Sign-in, domain and core (standard library only). `domain/errors.py`:
  `RobinautsError` and, under it, `InvalidValueError`, `ConfigError` (which
  carries every problem at once) and `SignInError` with the spec's fixed
  `SignInErrorCode` — plus `NotAllowedError`, `UnknownProviderError` and
  `InvalidIdTokenError`. `domain/identity.py`: `Identity` (what a provider
  asserts), `User` (keyed by `(provider, subject)`), `Session`,
  `PendingLogin`. `domain/sign_in.py`: `Matcher`, `AllowEntry`,
  `ProviderConfig` (which holds `client_secret_env`, the *name* of the
  variable, never a secret), `is_google_issuer` — the one answer, by host, to
  "is this Google", which decides whether Google's rules about `hd` and
  `email_verified` hold — and `SignInConfig` with `redirect_uri()`,
  `provider()`, `secure` and `session_life`. Roles and admin entries are
  deferred and are not there. `core/allow.py`: `is_allowed`, `matches`,
  `verified_email`, `ascii_lower` (case is ignored inside ASCII only:
  `.lower()` folds U+212A onto `k`). `core/claims.py`: `decode_id_token` (no signature
  check, and the docstring says why and when that stops holding),
  `check_id_token_claims` with `now` passed in, `identity_from_claims`,
  `identity_from_id_token`, `accepted_issuers` (the configured issuer alone,
  plus Google's bare host) and `check_published_issuer`, which is how a
  discovery document's issuer is checked against the configured one.
  `core/urls.py`: `normalise_origin`, `normalise_issuer`, `is_loopback`.
  `core/sign_in_config.py`: `parse_sign_in_config`, from the raw tables a
  TOML reader will hand it into a `SignInConfig`, unknown keys refused and
  every problem reported at once. `core/hashing.py`: `secret_hash` and
  `pkce_challenge` — making a secret needs randomness and is not here.
  The tests are the `backend/tests/unit/test_signin_*.py` modules, one per
  source module.
- Sign-in, ports, fakes and the application flow (standard library only).
  It added to `core` what is pure in a sign-in and was not there yet:
  `core/oidc.py` (`authorization_url` — the request, PKCE and all —
  `AUTHORIZATION_PARAMETERS`, `TOKEN_PARAMETERS`, `parameters_taken`,
  `MAX_CODE_CHARS`), `core/urls.py` (`normalise_endpoint`, `endpoint_query`,
  `safe_return_to`) and, in `core/hashing.py`, what a secret must be:
  `MIN_SECRET_CHARS` (43, for 256 bits), `MAX_SECRET_CHARS` (256, past which
  nothing is looked up), `MAX_PKCE_CHARS`, the two alphabets,
  `is_secret_shaped`, `same_secret` (constant time) and `checked_secret`,
  which is what a broken secret source runs into.
  `ports/`: `Clock` (aware datetimes, and a monotonic count for intervals),
  `SecretSource` (`secret`, `pkce_verifier` — the only randomness in a
  sign-in), `CredentialStore` (users get-or-created by `(provider, subject)`,
  sessions, pending sign-ins; found by the SHA-256 of a secret, never the
  secret) and `IdentityProvider`, which only fetches the discovery document
  and posts the code, returning raw data. **The store keeps no clock**: the
  application computes every expiry from `Clock` and hands it in, and every
  method that must know the time is told it, so one clock decides what has
  expired and no test sleeps. `application/sign_in.py`: `SignIn` with
  `begin`, `complete`, `resolve_session`, `sign_out`, `sweep` and
  `endpoints`. Discovery is lazy, cached on success only, and
  **single-flight**: everyone waiting on one provider shares one fetch and is
  told what it said, so ten people behind a provider that takes ten seconds
  to fail wait ten seconds, not a minute and a half; a cancelled caller waits
  on a shield and takes nobody's fetch with it. The application validates the
  discovery document and the ID token with `core`; the port decides nothing.
  Endpoints go through `core.normalise_endpoint` — https or loopback, no
  userinfo, no fragment, no whitespace — which **rebuilds** them from the
  pieces it checked, so a newline smuggled into a URL cannot reach a
  `Location` header; an endpoint whose own query sets, in any case, what its
  own request sets is refused, since its query goes in front of ours. An
  authorization code over `MAX_CODE_CHARS` is refused before it is posted
  anywhere. `core.safe_return_to` (printable ASCII, a path of this origin) is
  what keeps the return target from becoming an open redirect. Of what the
  identity provider port may raise, only `OSError` and `TimeoutError` become
  `provider_unavailable`, with the type alone in the detail and the original
  chained — an adapter's `TypeError` is a bug and propagates, and
  `CancelledError` passes through. Sweeping records its interval only after
  the deletes, and a sweep that fails is counted (`sweep_failures`) rather
  than failing the sign-in. A secret the source gave is refused at either end
  — under the 43 characters 256 bits need, over the 256 anything is looked up
  by, or spelt with what a URL would escape.
  `backend/tests/fakes/` holds an implementation of every port — a settable
  clock, counting secrets, an in-memory credential store and a scripted
  identity provider — and `backend/tests/contracts/` holds the two contract
  suites the real implementations will be held to: `credential_store.py`
  (override `new_store`, awaited inside the test's own loop, `close_store`,
  called in a `finally` by the `opened()` context manager every test uses so
  that a pool is released on the loop that made it, and `dump`, which writes
  out every field of every row so that the suite can look for a raw secret in
  it) and `secrets.py`. The store suite says in its docstring what
  a PostgreSQL implementation will need — a pool, one statement per
  operation, an advisory lock or `SERIALIZABLE` for the cap — and includes
  what a suite without it certifies wrongly: four `asyncio.gather` tests of
  what must be atomic, and two of sweeping running beside a take and a
  lookup. `test_fake_credential_store.py` proves they have teeth by running
  them against a deliberately racy store, which must fail them and does pass
  the one-caller-at-a-time ones. The other tests are `test_signin_flow.py`
  (the whole flow, both provider kinds and all eight error codes) and
  `test_fake_secret_source.py`; the pure rules are tested where they live, in
  `test_signin_urls.py`, `test_signin_hashing.py` and `test_signin_oidc.py`.
  There is no `pytest-asyncio`: `backend/tests/aio.py` has `@asyncio_test`,
  which runs an `async` test on an event loop of its own.
- The schema and the credential store, on PostgreSQL through `asyncpg` —
  the one runtime dependency there is. `datastore/schema.sql` is the whole
  schema of a deployment, shipped in the wheel: `schema_version` (one row),
  `users`, `sessions`, `pending_logins`, `timestamptz` throughout, the
  expiry columns indexed, sessions cascading from their user, and the two
  hash columns refusing anything that is not 64 lower-case hex digits. It
  is **one definition edited in place**; the conversation, message, run and
  event tables were added to the bottom of it by the conversation store's
  step, and the usage tables follow the same way. The version row is the
  file's **last** statement and is `DO NOTHING`, so a half-applied file
  records no version and applying the file can never relabel an older
  schema as this one. `datastore/schema.py`: `SCHEMA_VERSION`,
  `SCHEMA_SHA256` (the file's hash, pinned: editing the schema without
  bumping the version fails a test that says so — the guard that stands
  where a migration would), `SCHEMA_TABLES`, `schema_sql`,
  `create_schema`, which **looks before it writes**, under an advisory
  lock so that two at once make one schema — it applies the file to an
  empty database, does nothing to one already at this version, and refuses
  everything else — `schema_version` and `check_schema`. All of them judge
  **one schema**, `current_schema()`, which is where the file's
  unqualified `CREATE TABLE`s land: a table of ours further along the
  search path is not this deployment's, and a stranger's `users` there is
  none of our business. `check_schema` also requires every table to be
  there, to be a table rather than a view of the same name, and to be the
  one an unqualified statement in a store would actually reach — so the
  arrangement the rest of the code assumes, a search path whose first
  entry is the deployment's schema, is refused rather than assumed — on
  the create path too, before a statement of the file runs. A relation
  *further along* the path is harmless and explicitly allowed, which is
  how a deployment lives beside another application's `users`. Every
  refusal is the new `domain.SchemaError` (`missing`, `mismatch`,
  `unversioned`, `unreadable`, `incomplete`, `shadowed`, `no_schema`),
  naming `domain.DB_INIT_COMMAND` — `robinauts db init`, the command of a
  later step — and saying it works on an empty database only, because
  there are no migrations yet; `shadowed` and `no_schema` instead say to
  fix the connection, since nothing is wrong with the database. Each of
  the three functions asks everything on **one** connection.
  `datastore/pool.py`: `open_pool`, the one place a pool is made,
  which deliberately sets no session time zone. `datastore/credentials.py`:
  `PostgresCredentialStore` over a pool it is **given**, passing the same
  contract suite as the fake. Every method is one statement — the take is
  `DELETE … RETURNING`, get-or-create is `INSERT … ON CONFLICT … DO UPDATE
  … RETURNING`, the sweeps count in a CTE — except the cap, which holds a
  transaction-scoped advisory lock keyed on the table's own OID around the
  count and the insert, because no isolation level makes `count(*)` see a
  row another transaction has not committed. Nothing calls `now()`: times
  come in from the application's clock, and a naive datetime is refused
  rather than read as UTC by every method that takes one. Driver errors
  propagate, bar two constraints that exist to say no to a caller — a
  session hash already held and a session for a user who is not there,
  both `InvalidValueError`, told apart by the constraint's **name**, which
  `schema.sql` spells out, so that a constraint added later is not
  mistaken for one of them. A key that is not the hash of a secret is
  refused in Python before either statement, because the column's CHECK
  never runs on the path where the cap is reached and nothing is inserted.
  The contract now requires the same refusals of the in-memory fake, so
  the two stores no longer differ where the port was silent.
  `backend/tests/postgres.py` gives every test a schema of its own on the
  database named by `ROBINAUTS_TEST_DATABASE_URL`, cleaning up after
  itself if opening one fails, with a warm pool of eight connections so
  the contract's concurrency tests really race;
  `backend/tests/integration/` holds the modules that use it, plus one
  that builds a wheel and checks `schema.sql` is in it. Without the
  variable the database tests skip and the suite is green — unless
  `ROBINAUTS_REQUIRE_POSTGRES` is set, which turns a skip into a failure
  and is what stops CI going green on tests that quietly stopped running.
  It is checked on what happened, not on what was meant: a session hook in
  `backend/tests/conftest.py` counts the tests marked `database` that
  really ran and fails a required run that counted none, or that was
  narrowed with `-m`, `-k` or a path and so can prove nothing — or split
  across processes, where it could not count at all. It keeps quiet about
  a session that was interrupted or only collected, which has its own
  story. CI also waits for the database to answer a real query before the
  tests, with `scripts/wait_for_postgres.py`: a container health check
  cannot tell the server apart from the private one `initdb` runs while it
  sets the data directory up.
  An import-linter contract keeps `asyncpg` under `datastore`. CI runs the
  same tests against a PostgreSQL service container, pinned by digest,
  deliberately not on UTC, and required.
- `adapters/` — the rest of the outside world, and the second runtime
  dependency, `httpx` (BSD-3-Clause; it brings `certifi`, MPL-2.0, which has
  a row in `DEPENDENCIES.md` as an unmodified, unbundled runtime
  dependency). An import-linter contract keeps `httpx` under `adapters`, as
  `asyncpg` is kept under `datastore`. `adapters/identity_provider.py`:
  `HttpIdentityProvider`, the `IdentityProvider` port over an
  `httpx.AsyncClient` it **makes for itself** in its constructor
  (`open_client` is the one place one is made; `aclose` closes it, and the
  composition root holds the adapter for the life of the process, as it
  does the pool). The client is deliberately not a parameter: one made with
  `verify=False` is a keyword away, and an adapter whose TLS depended on
  what it was handed could promise nothing. What a deployment may choose is
  the timeouts, the bound, `trust_env` and where the secret is read from.
  It fetches and posts and decides nothing — the mappings come back as the
  provider sent them — but it is strict about the HTTP: a connect timeout,
  a read timeout and a ceiling on the whole exchange (`asyncio.timeout`);
  **no redirects**, set on the client and again on every send, because
  following one from a token endpoint would post the authorization code
  wherever the answerer chose; **a bounded body, uncompressed** — every
  request asks `Accept-Encoding: identity`, an answer that carries a content
  encoding anyway is refused unread, a declared `Content-Length` over
  `MAX_RESPONSE_BYTES` is refused before a byte of body, and what is read is
  the **raw** stream, counted off the wire (counting after decompression is
  counting the wrong thing: half a megabyte of gzip is sixty-seven of
  memory); `Accept: application/json` and a fixed, version-less
  `User-Agent`; and TLS through `ssl_context()`, built here so that it can
  be looked at, with **no argument anywhere to weaken it** — `trust_env`
  decides where the trust store and the proxy come from, never whether the
  certificate is checked; loopback `http` is the only unencrypted endpoint,
  and it is `core` that permits it. Failures are the port's two codes: a
  transport failure, a timeout, a `5xx`, a redirect, a compressed or
  oversized body, a body that is not a JSON object (or is nested past the
  recursion limit — forty kilobytes of `[` is a `RecursionError`, not a
  `ValueError`, and uncaught it would be a 500) are `provider_unavailable`,
  and so are `408`, `425` and `429`, which mean "ask again" rather than
  "no"; every other `4xx` from the token endpoint, and an OAuth `error` in a
  `200`, are `provider_refused`, with the provider's `error` and
  `error_description` bounded in the detail. The client secret is read at
  the moment it is used, through the injected `SecretLookup`, and never held
  on the object. **Nothing raised from an exchange can print it**: the
  frames of a code exchange hold the secret, the code and the verifier, and
  an `httpx` exception holds the `Request` that holds all three and stays
  reachable through `__context__` even after `raise ... from None`. So
  `_exchanged` *returns* the answer or a `_Refused`, unbinds every
  credential on the way out, and `exchange_code` raises from a frame that
  holds none of them while no exception is being handled — leaving
  `__cause__` and `__context__` both empty. Discovery, which carries no
  credential, keeps its cause for the operator. `client_secret_basic`
  form-encodes each half before joining them (RFC 6749 2.3.1), so a secret
  holding a colon works. `adapters/clock.py`: `SystemClock` (aware, UTC).
  `adapters/secrets.py`: `OsSecretSource` on `secrets.token_urlsafe`,
  passing the same `SecretSourceContract` as the fake.
  `adapters/config_file.py`: `read_toml` — raw tables, and a `ConfigError`
  naming the file (and the line, which tomllib puts in its own message) when
  it cannot be read; `environment`, where an empty variable is no secret;
  and `check_client_secrets`, which refuses to start a deployment naming
  **every** provider whose variable is unset, at once. *For the composition
  root, next:* that is every missing **variable** together, not every
  start-up problem together, which is what `operations.md` asks for and which
  only the root can assemble. A file that does not parse stops there — there
  is no configuration to check secrets against — but once `core` has accepted
  one, merge the secret check's problems into the same `ConfigError` as every
  other start-up problem the root can gather, rather than failing twice.
  Nothing here imports `core`: the reader hands raw tables to `core.parse_sign_in_config`, and
  the composition root (step 6 onwards) calls the two in turn.
  `backend/tests/standin/` is a **real** OpenID Connect provider on a
  loopback port the operating system picks (`http.server`, no new
  dependency): discovery, an authorization endpoint that redirects straight
  back with a code, and a token endpoint that checks the client
  authentication, the code, the `redirect_uri` and the PKCE verifier and
  issues an unsigned ID token carrying the claims the test scripted.
  `Misbehaviour` scripts the rest — held (behind a gate of its own, so that
  one `release()` cannot make the next hold vacuous), `5xx`, HTML, a
  redirect, an oversized body with or without a length, a length that lies,
  a gzip bomb, JSON nested past any parser, an OAuth error — and `received`
  records every request with its headers, so "the redirect was not followed"
  is a statement about what the server saw. It authenticates the client
  before it redeems a code, as a real authorization server does. It is meant to be reused by the routes and the browser
  test. `tests/integration/test_sign_in_end_to_end.py` runs the whole flow
  over it: `application.SignIn` + `HttpIdentityProvider` + `SystemClock` +
  `OsSecretSource` + the in-memory store, begin, follow the redirect,
  complete, resolve, sign out.
- `api/` — the inbound side, on FastAPI, the third runtime dependency
  (`fastapi`, MIT, bringing `starlette` BSD-3-Clause, `pydantic`,
  `pydantic-core`, `annotated-types`, `annotated-doc` and `typing-inspection`,
  all MIT) — all on the allowed list, so no row of `DEPENDENCIES.md` was
  needed. **`uvicorn` is deliberately not a dependency yet**: nothing imports
  it until `robinauts start` exists, and it arrives with the CLI step. The
  import-linter contract already names it beside FastAPI and Starlette, so
  the day the import is written the rule is already there. `api/web.py`: `create_api`, which is handed the
  application's `SignIn` (or `None`, where a deployment has no sign-in
  configuration) and constructs nothing; `/docs` and `/redoc` are **not**
  served, because they load scripts from a CDN, while `/openapi.json` is.
  `api/auth_routes.py`: the four routes of `docs/specs/sign-in.md` —
  `GET /auth/session` (never a 401: "nobody" is an answer),
  `GET /auth/login/{provider}`, `GET /auth/callback/{provider}` and
  `POST /auth/logout`, the two middle ones out of the OpenAPI document,
  being browser navigations. Every failure of a sign-in, whatever it was,
  ends as a 303 to `{public_url}/ui/#/sign-in?error=<code>` with the login
  cookie cleared and the detail in the log alone. `api/cookies.py`: the two
  cookies, `__Host-` prefixed and `Secure` on https and plain on a loopback
  `http` deployment, `HttpOnly`, `SameSite=Lax` (not `Strict`, which would
  drop the state cookie on the provider's own redirect back), `Path=/`.
  `api/access.py`: the guard — the session cookie resolved to a `User`, the
  **one place** that answers "who is this" and therefore the seam the local
  development mode goes through (`local_access`) — and the declaration, `public()` and
  `signed_in()`, which every route carries. `undeclared()` walks what an
  application serves and **fails closed**: it reads every list a router keeps
  routes in (`ROUTE_LISTS`: `routes`, and `_low_priority_routes`, where
  FastAPI's `frontend()` puts a whole served directory), recurses into
  included routers and into a mounted application's own router, and names
  anything that is not an `APIRoute` carrying a declaration — a mounted
  sub-application, a mounted directory, a plain Starlette route, a websocket
  handler, a frontend, and a permission passed to `include_router` rather than
  written on the route. The one escape hatch is `FRAMEWORK_PATHS`, keyed on
  the name the walk reports (`/openapi.json` today), which is how the step
  that serves the built interface will allow it: one line, `"/ui"`, whether it
  is served by `frontend()` or by a `StaticFiles` mount. It never lets an
  `APIRoute` off — a route with dependencies can declare a permission and
  so must. Underneath is `unknown_route_lists()`, the backstop that needs no
  knowledge of the framework at all: any attribute of a router holding routes
  under a name nothing reads stops the deployment, so a FastAPI that grows a
  third list cannot be served out of quietly. It is not only a test:
  `check_declarations` runs both when `create_api` builds the application and
  again when it starts — the two moments anything is looked at, so
  **routes are added between them and never after**.
  `tests/unit/test_api_access.py` shows it bite on every shape, and pins the
  FastAPI releases whose `APIRouter` somebody has read; the dependency is
  pinned to the same range (`fastapi>=0.141,<0.142`), so the metadata and the
  test say one thing rather than two.
  `domain.Permission` has the two levels there are while roles are deferred,
  and `domain.is_provider_id` — the shape `core` validates a configured id
  against — is what `api` checks a `{provider}` path parameter with before
  anything looks at it; the two sign-in routes are navigations, so a
  mis-shaped id lands on the sign-in page with `unknown_provider` and the
  login cookie cleared, like every other failed sign-in, rather than showing
  a browser a JSON body. `api/protection.py`: the request protection, as **middleware**
  rather than a dependency — FastAPI reads a declared body before it solves
  that route's dependencies, and a route written later could omit one — so
  a write is refused before a byte of it is read: each of the three deciding
  headers sent at most once (two values of `Sec-Fetch-Site` are read
  differently by us and by the next parser along), `application/json`, and,
  when it carries a session cookie, `Origin` equal to `public_url` or
  `Sec-Fetch-Site: same-origin`. `Sec-Fetch-Site` is read as an **allow
  list** — `same-origin`, `none`, or no header at all — rather than as the
  two words that mean another site: a proxy that folds two headers into one
  sends `same-origin, cross-site`, which is neither of them. The header names
  are folded **here**, in one pass over the scope's own list, and the cookies
  are read out of that same pass: ASGI only says a server *should* lower-case
  them, and a framework that compares what it was given finds
  `Sec-Fetch-Site: cross-site` beside `sec-fetch-site: same-origin` to be one
  header. A refusal answers a **fixed sentence** and repeats nothing that was
  sent; the particulars go to the log.
  It also decides what is **not** served: a `lifespan` scope passes through,
  a `websocket` is closed with a policy-violation code before it is accepted
  (the platform streams over SSE and has no websocket route, and a websocket
  carries cookies and answers no preflight), and a scope of any other kind is
  not served at all. Beside it, the headers every answer carries (`nosniff`,
  `Referrer-Policy: same-origin`); `Cache-Control: no-store` is set by the
  auth routes. `api/errors.py`: one exhaustive table from the error
  hierarchy to a status, the body `{"error": "<ClassName>", "detail": …}` —
  Starlette's own 404 and 405 put in that same shape, `Allow` header and all —
  and **a body that never repeats what the request carried**: a 5xx says only
  that the request could not be served; a `SignInError` says one fixed
  sentence per code (`SIGN_IN_DETAIL`), never a provider's words; and a
  request that could not be read names the field and the rule and never the
  value pydantic refused. All of it goes to the log.
  `api/logs.py`: `shown()`, the one way text from a request reaches a log —
  quoted and escaped with `ascii()`, so a `%0A` in a path (which a server
  hands over decoded) cannot forge a line, and bounded, so a log cannot be
  filled a megabyte at a time. Every log call in `api` that carries a path, an
  origin, a header, a provider id or a provider's words goes through it.
  `api/schemas.py` holds what the JSON API sends; the committed
  snapshot of it is `backend/openapi.json`, rewritten by
  `scripts/update-openapi.sh` and kept honest by
  `tests/unit/test_openapi_snapshot.py`.
- `app.py` — the composition root. `Deployment.configured(...)` reads the
  TOML file (path from `ROBINAUTS_CONFIG`; `ROBINAUTS_AUTH_CONFIG` is the old
  name, still read with a start-up WARNING, and the new name wins if both are
  set), has `core` validate **both halves** of it — sign-in and models — looks for the client secrets, the
  model providers' API keys and `ROBINAUTS_DATABASE_URL`, and reports
  **every problem it can see in one `ConfigError`** — which is the promise
  `check_client_secrets` could not make on its own. A file that does not
  parse stops there, since there is no configuration to check secrets
  against. `Deployment.open()` then does what needs a running loop, in the
  ASGI **lifespan**: the pool, `check_schema` (a database of another version
  is a server that does not start), `PostgresCredentialStore`,
  `HttpIdentityProvider`, `SystemClock`, `OsSecretSource`, `SignIn`; and
  `aclose()` gives every one of them back, logging a close that fails rather
  than stopping the rest. A `Deployment` is opened **once** — a second open
  would leave the first pool and client unreachable and held for the life of
  the process — and `aclose` is idempotent and safe on one never opened. Every collaborator may be handed in instead, which
  is how the tests wire fakes and the stand-in provider with no database,
  no file and no environment variable; the environment itself is read
  through the one injected `SecretLookup`. An identity provider that holds
  an HTTP client is closed whichever way it arrived.
  `backend/tests/webapp.py` is the test wiring (the fakes, a client over
  `httpx.ASGITransport`, and `running`, which drives an ASGI lifespan as a
  server does); `tests/integration/test_api_sign_in.py` signs in
  browser-shaped through the routes against the stand-in provider, and
  `tests/integration/test_create_app.py` does it again through `create_app`
  against the real PostgreSQL, naming its schema in the connection string,
  and checks the refusal to start on a database with no schema.
  There is no CLI yet and no UI served.
- The **local development mode** (`docs/specs/sign-in.md`): no sign-in at
  all, everything as one fixed local user, loopback only. `domain/local.py`
  holds what more than one layer needs — `LOCAL_PROVIDER` (`!local`, spelt so
  that `is_provider_id` refuses it, which is what reserves it: `core` and now
  `ProviderConfig` both hold a configured provider to that shape, so no
  configuration and no identity can ever be this person), `LOCAL_SUBJECT`,
  `is_loopback` (moved here from `core`, as `is_provider_id` was, because
  `api` asks it of a `Host` header and may not import `core`), `host_of`, and
  `LocalMode`, which **carries the bind host, normalises it and refuses a
  non-loopback one at construction** (by the stricter
  `is_loopback_bind_host`: a literal address or exactly `localhost`, since a
  bind address is not left to a resolver, while a request's `Host` may name
  this machine any way that reaches it): the root binds no socket, so the
  rule is kept at the one moment that cannot be got around later. `application/local.py`:
  `LocalAccess`, the counterpart of `SignIn`, which **reads** the one **real
  row** through the new `CredentialStore.user_by_key` (a plain indexed
  lookup that writes nothing) and falls back to the same get-or-create
  `user_at_sign_in` a sign-in uses when there is nobody there — so the user
  has an id, owns what the mode creates, is the same person after a restart,
  and an ordinary request leaves no row version behind (`user_at_sign_in` is
  an upsert). It caches nothing. `api.current_user` resolves every request to
  them with no cookie; `SignIn.resolve_session` refuses a session that names
  them, so a database kept from a run of the mode hands nobody the account.
  The request protection gains, in this mode only, a check on **every**
  request (one `Host` header naming a loopback host, and an address the
  server answered on that is loopback — a unix socket counts, a scope that
  says nothing is refused; the defence against DNS rebinding, which is the
  attack a same-origin rule cannot see, since the page really does own that
  origin),
  and it judges **every** write as credentialed: `Origin` equal to the
  loopback origin the request was addressed to, or `Sec-Fetch-Site:
  same-origin` with none. `GET /auth/session` answers `sign_in: false`,
  `local_development: true`, no providers and the local user (what the
  interface's banner is drawn from); `/auth/login/*` and `/auth/callback/*`
  redirect to `/ui/`; `POST /auth/logout` is 204. It is asked for by
  `create_app(local_development_host=...)` — the address it will be served on
  — and by nothing else: **no environment variable switches it on**, and
  asking for it together with a *sign-in* configuration (including one named
  by `ROBINAUTS_CONFIG` in the environment) is a start-up `ConfigError`, as
  is wiring both into one `create_api`. The **configuration file itself is
  allowed** in this mode and only its model tables are read, so the chat can
  be developed against a real agent; a file holding any sign-in table —
  `public_url`, `session_hours`, `providers`, `allow`, `admin` — is what that
  refusal looks for, and with no file the mode starts with no agents. `Deployment.open` logs one WARNING
  saying sign-in is off. The CLI flag (`--dev-no-sign-in`) comes with
  `robinauts start`.
  **For the CLI step:** the server must be started with the access log off, or
  with query strings stripped for `/auth/callback`, because an ordinary ASGI
  access log would otherwise write `GET /auth/callback/…?code=…&state=…` — the
  authorization code and the state, in a file, for every sign-in.
- The **conversation format**, domain and core (standard library only): the
  platform's own record, owned by nothing else (ADR 0002). `domain/agents.py`:
  `Engine` (`langgraph` | `pydantic-ai`) and `is_config_id` /
  `checked_config_id`, the shape an agent's and a model's id are written in —
  a provider id's rule, for the same reasons. `domain/conversation.py`:
  `FORMAT_VERSION`, `Role` (with `TOOL` reserved and refused), `Channel`
  (`web`), `PartKind` naming **every** kind of content the spec gives a
  message and `SUPPORTED_PART_KINDS` saying which two are built — `TextPart`
  and `ReasoningPart`; an image, a file, a tool call, a tool result and the
  tool role are refused by name with `UnsupportedContentError`, whatever else
  the content carries, so the discriminator they will be stored under is
  reserved without being built. `MessagePart` is the closed union,
  `checked_parts` the one rule about how many there may be, `Provenance` what
  an answer records (agent, engine, model, run), `Message` a node of the tree
  (id, conversation, parent, role, parts, `created_at`, channel, provenance —
  required on an assistant message and refused on any other; no token counts),
  and `Conversation` the owner, the agent, the title and the active leaf. A
  part holds up to a million characters and a message 64 of them, so no answer
  a model can produce has to be cut. `domain/values.py` holds the checks these
  records share. `checked_uuid` and `checked_text` (bounded, and no NUL and
  no unpaired surrogate, so what the format holds is storable and encodable);
  `checked_fragment`, the same bound with none of that, for a piece of a
  message still streaming; `checked_instant`, which is aware **and** inside
  the years 1970..9998 once it is UTC, so no record holds a time that could
  not be written back; `checked_line` for a title; `clean_text`, the lossy
  repair applied to what a provider sent, which joins a character that
  arrived in two halves before it replaces what is left; `publishable` and
  `flush`, which are how the application turns an engine's fragments into
  the deltas it publishes — NUL dropped first, a trailing half held back, what
  is still half replaced, so that everything published joined is exactly
  `clean_text` of everything the engine sent; and `describe`,
  which is what a refusal says **instead of** repeating the value it refused.
  `domain/run.py`: `RunState`, `ACTIVE_RUN_STATES`, `ENDED_RUN_STATES`,
  `FAULTED_RUN_STATES` and `Run`, which holds what its state asks for — an end
  when it ended, an error only when it ended badly — with `is_active` and
  `provenance`; its times are checked for being times and not against each
  other, because a wall clock that steps backwards must not make a run in
  flight unrecordable. `domain/turn.py`: what a running turn streams, as the
  platform's own events and not AG-UI's (`RunStarted`, `MessageStarted` —
  carrying the role and the parent, so a watcher can place a message before
  any of it exists — `TextDelta`, `ReasoningDelta`, `MessageCompleted`,
  `RunEnded`, `TurnEvent` as their union) and `RunEvent`, the envelope that
  gives each event its position in its run (`FIRST_POSITION`, from 1, no
  gaps): engines yield bare events, the application numbers them, and a
  watcher re-attaches by the last number it saw. New errors:
  `UnsupportedContentError`, `UnsupportedFormatError`, `StoredDataError`,
  `NotFoundError` and its three, `NotTheOwnerError`, `RunAlreadyActiveError`,
  `IllegalTransitionError`, `InvalidMessageTreeError` — each with its status in
  `api/errors.py`, which the exhaustive table there required. The whole
  not-found family **and** `NotTheOwnerError` answer one body, byte for byte,
  so an id cannot be probed for existence; `StoredDataError` is a 500 that says
  nothing, since a row of ours that cannot be read is not the request's fault.
  In `core`: `conversation_format.py` owns the **one** encoding, in both
  directions (`message_to_data` / `message_from_data`, `part_to_data` /
  `part_from_data`, `provenance_to_data`, `instant` — a time is always written in UTC, so one instant has one
  spelling). It reads the kind **before** anything else, so unsupported
  content is never reported as malformed; it refuses an unknown key, an id
  spelt any way but the canonical one, a naive time and a message with no
  parts; and it dispatches on the recorded version through one upgrade
  table **per document shape** — a message, a run event — so a build reads every version up to its own, refuses one above it, and
  an upgrade written for one shape can never be handed another (each hook is
  exercised with a stand-in version 0). `domain/stored.py` holds
  `reading_stored`, and it is **the read path**. It is in `domain` because a
  store may import `domain` and must not import `core` (`docs/layout.md`),
  and it is how a store turns its own columns into the flat records it owns
  them for — `Conversation`, `Run`, `User`: each validates itself, an
  `InvalidValueError` escaping a store would be answered as though the
  request had been at fault, and the four ways a malformed row breaks the
  code reading it (`KeyError`, `TypeError`, `AttributeError`, `ValueError`,
  and `RecursionError`) are converted too. `tree_of_stored`,
  `message_from_stored`,
  `run_event_from_stored`, `active_run_stored` and
  `check_may_start_run_stored` are that wrapping already done, in `core`, and
  **their callers are the application**. A fault of the
  data becomes `StoredDataError`, chained, which `api` answers with nothing
  and logs **with its causes** (`api.chain`); the `*_from_data` readers are
  for what the platform itself wrote, since a request carries no
  `format_version` at all. An id a request named and that is not there stays
  a `MessageNotFoundError` throughout, because nothing is wrong with the rows. `domain.text_parts` makes content out of text too long
  for one part, so an answer is never lost to a bound; it sits in `domain`
  beside `clean_text` and for the same reason, that its caller is an agent
  adapter, which may not import `core`. `domain.kept_parts` is what the
  application keeps of an answer: reasoning dropped, and one empty piece of
  text if that leaves nothing, because a message always has content and a
  turn answered with nothing should be recorded rather than left out. The format reserves one key,
  `extras`, on every document it writes and on every part of one — a message
  and each of its parts, a run event and the event inside it: an object of at
  most 64 KiB of storable text that a build which does not use it accepts and
  reads past, which is what lets a
  field be **added** without moving the version, and where vendor-specific
  extras will live. This build writes none and keeps none. `conversation_tree.py` owns the
  tree. **`ConversationTree` is a conversation read once and checked once** —
  every message by id, in order, and every parent's children in order — built
  by `tree_of` (messages a request brought) or `tree_of_stored` (rows,
  checked as stored data at that one door), and every question afterwards is
  a method on it that can fail for one reason only: an id the request named
  that is not there. `may_follow` is the whole rule of what may hang under what — a root is
  a question, a question follows an answer, an answer follows a question, an
  answer or a tool result, a tool message follows the answer that called it,
  so a turn is a chain and tools need no rewrite — with `check_parent`,
  `check_tree`, `path_to`, `children_of`, `siblings_of`,
  `leaves`, `default_leaf` (the branch below the author's last position whose
  own last message is newest), `turn_start`, `parent_for_edit`,
  `parent_for_regenerate` (which goes back to the question of the whole turn),
  `branches_along` and `branches_of` (a message with the branches beside it
  and where it falls among them — what the interface draws as "2 of 3") and
  `check_attachment` (a parent from somebody else's conversation is simply
  not among these messages). They are methods, not free functions: the only
  way to have a tree is `tree_of` or `tree_of_stored`, both of which say
  which conversation they are reading and check it first, so there is one way
  to ask and one set of rules. `check_tree` is the one wrapper kept, for a
  caller that wants the rules applied and has no further question.
  `titles.py` derives the title from the question's text as a whole
  (`title_from_text`, `derive_title`, `first_question`), cutting at a word
  boundary and never inside a character. `history.py` is the **placeholder**
  context policy (`message_chars`, `history_chars`, `trim_history` —
  characters, not tokens, dropping whole turns from the front and always
  keeping the turn being answered). `runs.py` is the state machine
  (`RUN_TRANSITIONS`, `may_transition`, `check_transition`, `transition` —
  which clamps each stamp to the one before it, gives an ending run the start
  it never recorded, and puts the error through `run_error`, so that ending a
  run never fails over the text of what went wrong: `UNSAID_ERROR` for a
  failure with nothing to say, `TRUNCATED` on the end of one that was too long
  — `active_run`, `may_start_run`, `check_may_start_run`) **For the next step:** a store never parses or builds a message or a run
  event — the format is `core`'s and a store may not import it. What crosses
  the conversation and run ports for those two is the **document** (the plain
  mapping `message_to_data` / `run_event_to_data` writes), which a store keeps
  as jsonb and hands back untouched, with the record passed beside it when it
  needs a field for one of its own indexed columns (id, conversation id,
  parent id, created at, seq); the application encodes before it writes and
  decodes with the `*_stored` readers after it reads. `Conversation` and `Run`
  cross as records, since the store owns their columns.
  `resume_point` says where a
  watcher opening a conversation with a run in flight attaches: `after`, the
  run's last completed message or the event that started it, and `follows`,
  what the next announcement will hang under — so a caller has both of the
  things a slice is checked with, and is replayed the message still being
  produced and none it already has. And
  the two order checks: `check_event_order` for what the application publishes — one
  message at a time, each hanging under the one before it, completed as it was
  announced, of this run and this conversation (both **required**), a run that
  finished leaving nothing half-written, numbered without gaps, the deltas published for a message adding up to the
  message that was stored, in one linear pass — over a whole run, a run **still going** (`ended=False`, which is what
  a watcher of a live one receives) or **the slice a re-attaching watcher
  receives**
  (`after=<the position it last saw>` and `follows`, with `open_message` for
  the message the cut fell inside); and `check_engine_events` for what an engine yields, which
  is what the shared contract suite will hold both engines to: one answer at a
  time, announced, streamed, completed; streaming is optional, and an answer
  that streamed any text completes with exactly what it streamed
  (`cut_short=True` for the failure cases, where a turn ends wherever it
  ended). What the specs left open and this step settled is written into
  `docs/specs/legacy/conversations.md` and `docs/specs/legacy/runs.md`, not only here.
  `backend/tests/conversations.py` holds the builders every later step will
  use; the tests are the `backend/tests/unit/test_conversation_*.py`,
  `test_run_*.py` and `test_turn_events.py` modules, one per source module.
  **For the run-executor step:** a `Run` records no owning process and no
  heartbeat. The POC is one process, so nothing can tell an orphan from a
  running run; `runs.md` describes both, and they arrive with the second
  process, not before.
- The **conversation store port**, its fake, its contract suite and the
  application for managing conversations (standard library only).
  `ports/conversations.py`: **one** `ConversationStore` owning conversations,
  messages, runs and run events, because they are one database and several
  operations over them are **one transaction**. Conversations and runs cross
  as records, messages and run events as **documents** with the record beside
  them, and every method that needs a time is told what `now` is: one clock,
  as with the credential store. The compound operations are single methods,
  so no caller can stop half way: `start_run` (optionally create the
  conversation, optionally append the question, create the run — refused with
  `RunAlreadyActiveError` if one is going, deciding it in the step that would
  have inserted), `complete_message` (the message *and* its
  `MessageCompleted`), `end_run` (the ended record *and* its `RunEnded`) and
  `delete_conversation` (the conversation with its messages, runs and events,
  refused while a run is active, checked inside the same transaction so
  nothing can be orphaned). Beside them: `add_conversation`,
  `conversation_by_id`, `conversations_of` (newest-updated first with a
  `limit` and a cursor — a total order, ties broken by id, the cursor a
  position inside the caller's **own** listing, unsigned because it can reach
  nothing else), `rename_conversation`, `set_active_leaf` (which deliberately
  does **not** date the conversation: navigation must not reorder the panel),
  `touch_conversation`, `append_message`, `messages_of`,
  `conversation_snapshot` (the conversation, its messages, the run in flight
  and that run's events **as of one moment** — two reads would disagree, and
  the gap between them is exactly where an answer is), `run_by_id`,
  `active_run_of`, `runs_of`, `runs_in` (for the start-up sweep, bounded by
  `MAX_SWEPT`), `update_run` (which may change a run's state, its start and
  its error, and nothing else), `append_event`, `events_of(after=…)` and
  `last_position`. The three methods that change one conversation hand back
  the record they **wrote**, so a caller never rebuilds one out of a read made
  before the write. **Who checks what** is in the port's docstring with the
  whole table of refusals: the store checks everything its own columns and the
  domain records can show — that a row exists, that one belongs to another,
  a message's role, a run's state, a position, that an event is of the kind
  its method is for and names the same run, message or state as the record
  beside it — while "the document says what its record says" and the shape of
  a stream as a whole are the application's, held by `core.check_event_order`
  in the tests. So: a parent that is no message of **that** conversation, a
  run that answers anything but a user message of its conversation or names
  an agent that is not the conversation's, a completion crossing into another
  conversation or announcing another message, an end announcing another state
  — each refused with its own error; a run that begins in a state it has
  already ended in is refused too, and **nothing at all is written into a run
  that has ended** (`IllegalTransitionError`), which is what keeps a cancelled
  run's stored stream readable. Which error a call that breaks several rules
  gets is deliberately unspecified -- a SQL store meets them in its own order
  -- and what every store owes is that a refusal writes nothing. A position that is not the next one
  is the new `domain.PositionTakenError` (409 in `api/errors.py`): the
  application is the single writer of a run's events, so a refusal means the
  run moved on. `ports/ids.py`: `IdSource` — four lines,
  because the records of a turn name each other and are built before any of
  them is stored; `adapters/ids.py` is `OsIdSource` on `uuid.uuid4`, the fake
  counts, and the service of the **next** step is what uses them.
  `application/conversations.py`: `Conversations` with `list_for`, `open`
  (the conversation, its messages as a `ConversationTree`, the leaf
  `default_leaf` resolves, and — if a run is in flight — its id and
  `core.resume_point` over the run's event documents — all of it from **one**
  `conversation_snapshot`), `rename`, `select_branch` (any message, not only a
  leaf) and `delete`, which is one store call. **Ownership is one rule in one place**: `_owned` answers a
  conversation of somebody else's exactly as one that does not exist, with
  `ConversationNotFoundError` for both, the reason in a detail that reaches
  the log alone; the local development user is a user like any other.
  `backend/tests/fakes/conversations.py` is the store in dictionaries: **one
  lock held for the whole of an operation**, where a transaction stands, with
  a yield inside it that interleaves nothing and is where a store without one
  is caught; documents deep-copied in and out so nothing a caller holds is
  the database; and "at most one active run" as an index, so two cannot exist
  by construction. `backend/tests/contracts/conversation_store.py` and
  `conversation_runs.py` are **one suite over one store**, split for length,
  and `test_fake_conversation_store.py` runs the whole of it against the same
  fake with its lock taken away — the only difference — asserting that the
  set of tests it fails is **exactly** the ten about two things at once
  -- the compound reads among them: a snapshot is slid across a write at
  every interleaving a single-threaded loop has, so a store that reads its
  four parts one after another is caught as surely as one that writes them
  that way.
  `backend/tests/contracts/ids.py` is the small suite both id sources pass
  (a real `uuid.UUID`, version 4 with its variant, never repeating), run
  against the adapter and the fake by `test_id_sources.py`.
  The service's own tests are `test_conversations_application.py`.
  **For the next step:** creating a conversation, appending a message and
  starting a run are the run lifecycle and are not built here; `start_run`,
  `complete_message` and `end_run` are defined, faked and pinned for it, and
  a test builds conversations through the store. And `open` should also
  surface the **most recent run's state and error when it ended badly** — from
  `runs_of`, which needs no change to the port — so that somebody who reloads
  a conversation after a run failed, was cancelled or was interrupted is told
  so, instead of finding a turn that simply stops.
- The **run lifecycle** in the application, against a fake engine (standard
  library only). `domain/agents.py` gains `AgentDefinition` -- an operator's
  agent as a record (id, title, system prompt, model, engine), validated in
  `__post_init__`, with **no configuration parsing**: a later step reads the
  file. `ports/agents.py`: the `Agent` port, **one method**, `run_turn(agent,
  history)`, which hands back an async iterator of `EngineEvent`s. The
  **history is the path ending in the user message being answered**, already
  trimmed (`core.trim_history`) -- so there is no second argument for "the new
  message", and a regeneration and a resumed turn look like every other turn.
  An engine reports failure by raising and must let `CancelledError` through;
  "waiting on tool calls" is a sentence in the docstring and nothing more.
  `backend/tests/contracts/agents.py` is the suite both real engines will
  subclass with a stubbed model (`new_agent(script)`): a streamed answer, an
  unstreamed one, several in a turn, what was streamed is what completed, a
  failure by raising mid-answer (`check_engine_events(cut_short=True)`), and a
  cancellation that comes through promptly. `backend/tests/fakes/agents.py` is
  the engine a test writes the script for -- events, `Gate`s the test opens
  (so nothing sleeps and a gate nobody opens is an engine that hangs), and
  `Raise` -- and it passes the suite now.
  `application/turns.py`: `Turns`, with the store, the clock, the id source, a
  mapping of agent id to `AgentDefinition` and one of `Engine` to `Agent`, plus
  two limits with defaults (`history_chars`, `turn_seconds`); an agent whose
  engine is not wired is refused **at wiring**. `start(user, agent_id | conversation_id, text,
  parent_id)` and `regenerate(user, conversation_id, message_id)` are the three
  request shapes of `wire.md` -- a new chat (titled with `core.derive_title`), a
  message in one that exists (continue or edit, told apart only by the parent,
  checked with `ConversationTree.check_attachment`), and a regeneration, which
  appends nothing and answers the question its turn began with -- and **each is
  one `start_run`**. `execute(run)` is the lifecycle a later step's executor
  will schedule as a task: the run stamped as taken up (`update_run`),
  `RunStarted` at position 1, the history read and
  trimmed, the engine consumed inside one
  `asyncio.timeout` for the whole turn, text through `publishable`/`flush`,
  reasoning published and kept **in the run's events alone** (a re-attaching
  watcher needs it; no message holds any), and each answer built with
  `kept_parts` and stored with its `MessageCompleted` in one
  `complete_message`; then `finished`, or `failed` (the engine raised -- the
  type and what it said through `core.run_error`, never a traceback, the whole
  of it escaped and bounded to the log through `domain.chain` / `domain.where`
  -- no answer at all, an answer left announced, an answer
  that did not complete with what it published, or the turn's timeout), or
  `cancelled`, with the `CancelledError` **re-raised**.
  **Ending a run is one routine for every path**, and it is the careful one:
  it is shielded and waited on to its end (a cancel landing on it must not
  leave a run `running` with its conversation blocked), each attempt is
  bounded (`ENDING_SECONDS`, so a store that never answers cannot hold a task
  nothing can reap), and a store that cannot be reached is tried again a few
  times -- after which what it says is what is true, decided by **re-reading
  the run**: one somebody else ended is not reported at all, and one still
  active gets the ERROR naming the next start-up sweep, whatever went wrong
  here. That is the known limit now written into `runs.md`. Letting go of the
  run in this process's registry happens whatever the release does, including
  being cancelled inside it, and the release itself never swallows a
  cancellation: it records the abandonment and raises it again.
  **A refused position is a question, never a repetition** (`_Stream`): every
  refusal and every write whose answer never came back is settled by reading
  what is stored **where the event was offered** -- already there (nothing to
  do), a `RunEnded` there (stop quietly), somebody else's event there (fail
  the run, `TWO_WRITERS`), or not there (offer it again from the position the
  store reports; running out of attempts is `UNWRITABLE_STREAM` and fails the
  run, never "somebody ended it"). Any `RunStarted` of the run counts as ours,
  so a cancel or a sweep racing the task cannot start one run twice.
  `_end_elsewhere` plans from the store on every attempt, and one run it
  cannot end does not stop the sweep. Something raising while the task is
  being cancelled
  (`asyncio.current_task().cancelling()`) is a cancellation and not a failure.
  **The engine is read by a task of its own** through a bounded queue
  (`_Pump`, `QUEUE_DEPTH`): the lifecycle awaits the queue, so a cancellation
  or a timeout reaches it at once however long the engine then takes to let
  go, back-pressure is kept, and what the engine raises is raised in the
  lifecycle's own frame. The engine is cancelled and closed **after** the run
  has ended, waited for `CLOSING_SECONDS` and then abandoned (its result read,
  so the loop says nothing), which is why an engine that will not let go holds
  up nothing. `cancel(user, run_id)` cancels the task if this process has one and
  otherwise ends the run in the **store**, which is what actually stops it
  across processes; `claim(run_id)` / `let_go(run_id)` are how the executor
  says a run is its **before** the task exists, so a sweep cannot interrupt a
  run that is about to be answered, and a second `execute` of one run is
  refused. `sweep_interrupted()` ends every active run this process
  does not own, skipping `waiting` (nothing interrupts a run that holds no
  process) and tolerating a run somebody ended first. Whoever ends a run whose
  stream holds nothing writes its `RunStarted` first, so **every** stored
  stream satisfies `core.check_event_order` -- which the tests assert over the
  stored documents in every scenario, re-attachment included.
  `Conversations.open` now also answers `ended_badly`: the most recent run when
  nothing is in flight and it failed, was cancelled or was interrupted, so a
  reload after a failure says so -- through the new `runs_of(..., limit=1)`, so
  that opening a conversation answered a thousand times reads one row.
  `domain/logs.py` now holds `shown`, `chain` and `where`, which `api.logs` and
  `api.errors` re-export: the rule about escaping and bounding what somebody
  else wrote is one rule, and the application logs what an engine raised by it.
  Ownership is one function,
  `application.owner_of`, shared by both services; a run is reached through its
  conversation and "not yours" is "not there" for it too
  (`RunNotFoundError`). New: `domain.UnknownAgentError` (a `NotFoundError`,
  404 in `api/errors.py`'s exhaustive table). The tests are
  `tests/unit/test_turn_start.py`, `test_turn_lifecycle.py` and
  `test_fake_agent.py`, over the wiring in `tests/turns.py`.
  **Since then:** the executor of step 12 is further down -- it `claim`s a run
  and hands the work of `Turns.execute` to the `RunExecutor` port, the registry
  of live work is the executor's, `cancel` asks it, and the start-up sweep is
  the lifespan's. The
  PostgreSQL store (11) meets `start_run`, `complete_message`, `end_run` and
  `append_event` exactly as the fake does, and the refusals of an ended run and
  of a taken position are what stops a run, so they are not optional. The api
  (13/14) hands `begin` / `begin_again` a `User` and gets the `Run` back at
  once, streams from `resume_point` and the events after it, and calls
  `cancel`; `execute` is never awaited by a request.
- The **conversation store on PostgreSQL**, over the same pool and the same
  `schema.sql` -- now at **version 2**, the first bump there has been, so a
  developer's version-1 database is made again rather than upgraded (there
  are still no migrations). Four tables added to the bottom of the file,
  every constraint named: `conversations` (owner cascading from `users` -- deleting an account
  takes its conversations, and the messages, runs and events under them, by
  the chain of foreign keys; `active_leaf_id` deliberately carries none, since
  it would point at `messages`, which points back), one index which is both
  the listing's keyset `(owner_id, updated_at DESC, id DESC)` and what the
  cascade needs; `messages` (id a primary key, so unique across the
  deployment; the document as `jsonb`; a **composite** foreign key
  `(conversation_id, parent_id)` → `(conversation_id, id)`, so a parent from
  another tree cannot be stored); `runs` (a composite key to the message it
  answers, a **partial unique index** on `(conversation_id) WHERE state IN
  ('running','waiting')` -- "at most one active run", held by the database --
  and partial indexes for the sweep and for a conversation's runs);
  `run_events` (primary key `(run_id, seq)`, which is "a position is stored
  once", a `kind` column the store writes from the record's class name and a
  partial unique index over it so a stream ends once -- written for that
  index and read back nowhere, so the check before **every** write is one
  backward walk of the primary key and a run does not cost the square of its
  own length; "has this stream ended" is the run's own state, which the write
  already holds a lock on). `timestamptz`
  throughout, CHECKs on `role`, `state` and `engine`, and on `seq >= 1`.
  `datastore/conversations.py`: `PostgresConversationStore` over a pool it is
  **given**, one transaction per method, passing the whole contract suite the
  fake passes. **Locks are taken in one order -- the conversation's row, then
  the run's** -- so two methods meeting on one conversation cannot close a
  cycle; the two constraints above are the **backstops** under the look each
  method makes while it holds the row, translated by constraint **name** into
  `RunAlreadyActiveError` and `PositionTakenError` (`CONVERSATION_REFUSALS` is
  the whole table; anything else propagates). A deadlock or serialization
  failure is retried `MAX_ATTEMPTS` times and **logged** at WARNING every
  time, because a silent retry is how a wrong lock order hides; a test
  provokes a real deadlock with an outsider connection taking the two rows
  the other way round, and pins one warning, one success and a whole stream.
  `conversation_snapshot` is one read-only `REPEATABLE READ` transaction, so
  its four reads are one moment. Documents cross as `jsonb`, encoded and
  decoded with `json` **in the store** rather than by a codec on a pool
  somebody else built, and come back equal -- unicode, a megabyte of text and
  a nested `extras` the build writes none of included. The listing is a keyset
  over `(updated_at, id)` with the fake's own cursor spelling, refused when it
  does not parse. Records are built inside `domain.reading_stored`; a naive
  `now` is refused before a connection is taken; every check the records alone
  can settle runs before the transaction opens. A `title` is held to
  `domain.checked_line` and a document to what JSON can really be written
  from -- a key that is not text would be quietly renamed and could merge two
  keys into one -- so both are `InvalidValueError` before a statement runs.
  `app.Deployment` builds it on the same pool and exposes it as
  `deployment.conversations` (nothing is wired on top yet), and it is **both
  stores or neither**: a deployment that is open has a credential store *and*
  a conversation store, so handing in one of them and naming no database is a
  start-up `ConfigError` (the tests hand in both fakes). `check_schema`
  covers the four new tables because `SCHEMA_TABLES` does. `tests/integration/test_postgres_conversation_store.py`
  runs the full `ConversationRunsContract` against a fresh schema per test with
  a warm pool of eight, and adds what only a database can be asked: the
  documents' round trip, an odd session zone, that every translated constraint
  name is really in the schema, that a user deleted takes their conversations,
  a probe running five methods at once on one conversation forty times over
  (twice: with a run in flight, and with one that has ended, where the delete
  really cascades) asserting no driver error escapes and nothing was retried,
  that appending five thousand events to one run stays flat per event, and
  one whole streamed turn of `application.Turns` against this store with the
  stored tree and the stored stream read back.
- The **run executor**, the **signals** a watcher waits on, and the
  application's **watcher** -- so a turn now runs in the background and can be
  followed. Two ports: `ports/run_executor.py` (`RunExecutor`: `submit(run_id,
  work)`, which takes a **coroutine factory** so that a refused submission
  leaves no coroutine nobody ran, `cancel(run_id) -> bool`, `running()`,
  `aclose(timeout=)`) -- about **scheduling only**, deciding nothing about
  runs -- and `ports/run_signals.py` (`RunSignals`: `announce(run_id, seq,
  ended=)` and `changed(run_id, after, timeout=) -> bool`), which carries **no
  data at all**: a watcher woken by it reads the store, every wait is bounded
  by the caller, and a signal that is lost costs a wait and never an event,
  which is what lets PostgreSQL `LISTEN`/`NOTIFY` replace it later without the
  application changing. The adapters are `adapters/run_executor.py`
  (`AsyncioRunExecutor`: tasks on the loop that serves requests, a registry
  holding a **strong reference** to each, every exception caught where it is
  run and written to the log once through `domain.chain` / `where`,
  done-callbacks that clean the registry and retrieve anything that escaped,
  and an `aclose(timeout=)` that is **one deadline for the whole stop**: the
  wait for the cancelled work and every never-began report spend what is left
  of it, and each report runs in a task of its own and is told the deadline
  (`ports.RunReport`), because what a report writes is shielded and a shielded
  write can only be left, never interrupted -- one that has not come back when
  the deadline passes is left running, with a line in the log. The bound is
  validated like every other, and `app.SHUTDOWN_SECONDS` is **added up** from
  `application.ENDING_BUDGET_SECONDS` (the attempts and backoffs of one
  ending) plus slack, so the number a process stops by cannot drift below what
  one ending needs) and `adapters/run_signals.py`
  (`MemoryRunSignals`: a position and an "ended" per run, futures for its
  watchers, and a **bounded memory** -- what was said about a run outlives it,
  so that a watcher asking about a position it already holds is answered
  rather than made to sit out the bound, but never more than
  `REMEMBERED_RUNS` (10,000) runs or `REMEMBERED_SECONDS` (60) past the last
  word about one; a run nothing is remembered about falls back to the poll.
  The records **somebody is waiting on** are kept in a dictionary of their
  own, apart from the recency order the sweep reads, so that an announcement
  costs the same whether the process has one watcher or five thousand: the
  sweep reads the head of the forgettable order and nothing else, and both
  bounds are about that order alone). `application/turns.py` gains the two ports and the API-facing
  `begin` / `begin_again` -- `start` / `regenerate`, then `claim`, then
  `executor.submit(run.id, ...)` -- so **nothing outside the application
  creates a task**, while `execute` stays a coroutine a test awaits; every
  stored event is announced by `_Stream` the moment after it is written (a
  signal that fails is a log line, never a failed write); `cancel` asks the
  executor, and only for work that has **begun** here, because cancelling work
  that has not yet started would write no ending -- everything else still ends
  the run in the store; and `stopping()` says that the **process** is going, so
  that the runs its shutdown cancels end `interrupted` rather than `cancelled`.
  A turn the executor refuses (it is closed) is ended `interrupted` at once
  rather than left for the sweep, and so is one whose work was cancelled **in
  the instant before its first step** -- the executor takes a `never_began`
  alongside the work and runs it while the process still holds its stores,
  which is the one thing a shutdown could otherwise lose. That report
  (`Turns._never_began`) is given the shutdown's deadline and respects it:
  `_to_the_end` takes an optional deadline, and when it passes the shielded
  ending is **left running** rather than killed -- a done callback says in the
  log whether it landed, and if it did not, the next start-up sweep ends the
  run. `application/watch.py` is `Watch.events(user,
  run_id, after=)`: ownership through the run's conversation ("not yours" is
  "not there"), the stored events after `after` in order, then a bounded wait
  on the signals and another read -- and it **ends after the `RunEnded`**, or
  after replaying what is there when the run has already ended, or when the
  run is **no longer there at all** (its conversation was deleted: a run that
  is gone is over, and nothing is reported). **Every turn of its loop sends
  something or waits**: the run record is re-read after every wait, and a
  wake-up that yielded nothing is not believed a second time in a row, so a
  signal that answers at once -- which is what the memory above makes common
  -- can never become a loop that starves the run it is watching. And it does
  not wait for ever: after `quiet_seconds` (the deployment's turn timeout)
  with nothing stored under a run that is **still active**, it raises
  `domain.RunQuietError` (504 in `api/errors.py`'s table, with a body of its
  own -- `QUIET_RUN_DETAIL` -- and one WARNING rather than the generic
  internal-error body a 5xx of ours answers with) rather than ending the way a
  stream that has said everything ends. Signals that keep raising are said
  **once** per watcher at WARNING and at DEBUG after that, and a `Watch` whose
  wait is longer than the silence it gives up after is refused, so the root
  passes `min(DEFAULT_WAIT_SECONDS, turn_seconds)`. A lost signal makes the
  stream slower and never wrong, and closing the generator is all a watcher
  that went away has to do. `app.py` builds the executor, the signals
  and the three services and exposes them (`deployment.conversations` is now
  the **service**; the store it is built on is `deployment.conversation_store`,
  which is what that attribute and the `conversation_store=` argument are now
  called); `agents` and `engines` are injected with empty defaults until the
  step that reads them from the configuration. It holds **one**
  `turn_seconds` (`create_app` / `Deployment.configured` /
  `Deployment(turn_seconds=)`, default `application.DEFAULT_TURN_SECONDS`) and
  gives it to `Turns(turn_seconds=)` and to `Watch(quiet_seconds=)`: the same
  question from the two sides, so a deployment that lengthens a turn lengthens
  the wait for one instead of finding out that the two numbers were copies. The lifespan **sweeps once**
  after the stores are open, logging how many runs a process that went away
  left going -- a sweep that fails is logged and does not stop the deployment
  -- and shutdown is, in order: say the process is stopping, drop the services,
  cancel the work under a bound (`adapters.DEFAULT_SHUTDOWN_SECONDS`, the one
  number there is), report whatever never began, close the pool. Nothing is
  drained (`poc-scope.md`). The tests are
  `tests/unit/test_run_executor.py`, `test_run_signals.py`,
  `test_run_watch.py` (a turn in the background with watchers at 0, mid-answer
  from `resume_point`, inside an answer and after the end, each checked with
  `core.check_event_order` in slice mode; a watcher that goes away; signals
  that are all dropped; cancel; a process that stops), the new part of
  `test_app_composition.py` (the start-up sweep, the services, a shutdown that
  interrupts, a turn whose work never began) and
  `tests/integration/test_postgres_run_executor.py` against the real store.
  `backend/pyproject.toml` now runs pytest with `filterwarnings = ["error"]`:
  any warning is a failure. It deliberately says in a comment what that does
  **not** reach -- "coroutine was never awaited" is raised inside the garbage
  collector and reaches the warnings summary and no further, and "task
  exception was never retrieved" is a line the loop logs rather than a warning
  -- which is why the executor retrieves every exception itself and its tests
  install an exception handler on the loop and assert it saw nothing.
  **For the routes and SSE (13/14):** everything they call is on the
  deployment. Start a turn with `turns.begin(user, agent_id=... | conversation_id=...,
  text=..., parent_id=...)` and regenerate with `turns.begin_again(user,
  conversation_id=..., message_id=...)` -- both answer a `StartedTurn` (the
  run, the conversation, the question) the moment the run exists, and neither
  waits for a word of the answer; stop one with `turns.cancel(user, run_id)`,
  which answers the run as it then is (`running` is a normal answer: asking is
  not having happened); open a conversation with `conversations.open(user,
  conversation_id)`, which hands back the tree, the leaf, the run in flight and
  its `resume` (`after` and `follows`); and follow it with `watch.events(user,
  run_id, after=resume.after)`, an async iterator of `RunEvent`s that ends
  after the run's `RunEnded` -- close it when the client goes away. **A `RunQuietError`
  from the watcher** is it having given up on a run that stored nothing for
  `quiet_seconds`, and the route owes the client something it can see (an
  error event on the wire) rather than a connection that simply closes. **A
  normal end without a `RunEnded`** is not that: the run is over -- it ended
  before the position asked from, or it is no longer there at all -- and the
  client already has, or can load, its final state. `execute` is never awaited by a request, and no route
  creates a task.

- The **conversation routes**, the plain JSON half of the wire
  (`docs/specs/wire.md`): `api/conversation_routes.py` with
  `GET /api/conversations` (paged), `GET|PATCH|DELETE
  /api/conversations/{id}`, `PUT /api/conversations/{id}/leaf` and
  `POST /api/conversations/{id}/runs/{run_id}/cancel`, and
  `api/agent_routes.py` with `GET /api/agents` -- its own module and router,
  because it is the **deployment's** list and holds nothing of anybody's --
  over `api/refusals.py`, the one table of what a route can refuse with, which
  both route modules import so that neither depends on the other.
  Every one of them declares `signed_in()` -- `api.SignedIn` is the person and
  the declaration in one annotation -- and **not one of them checks
  ownership**: each passes the `User` to the application, where the one rule
  lives, so a conversation of somebody else's, one that never existed, a
  message that is no message of this conversation and a run that is not this
  conversation's run all answer the same status, the same body and the same
  headers. A test asks that as an **identity** over every route that takes an
  id, rather than asserting a status twice. The services are read off
  `app.state.conversations` / `app.state.turns` at the moment of a request,
  the way the auth routes read the sign-in, and the lifespan of `create_app`
  puts them there and takes them away; a route that finds none says
  `api.NOT_WIRED` and answers as any other mistake of ours does (500, the
  generic body, the whole of it logged), because **there is no deployment
  without them** -- unlike a sign-in, which a deployment may configure away.
  `api/schemas.py` grew what the JSON API sends, each with an `of(...)` like
  `UserSummary`'s: `ConversationSummary`, `ConversationListResponse`
  (`items`, `next_cursor`), `MessageView` with `ContentPart` and
  `ProvenanceView`, `OpenedConversationResponse`, `ResumeView`,
  `EndedBadlyView`, `RunView`, `AgentSummary` / `AgentListResponse`, and the
  two request bodies, `RenameRequest` and `SelectLeafRequest`, which **forbid
  a field they do not know**. Ids cross as text and times through
  `api.utc`, so one instant has one spelling whatever zone a store's session
  was in. **Opening a conversation sends the whole tree** -- every message,
  oldest first, each with its `parent_id` -- read **after** ownership is
  settled, because a snapshot reads every message and every event and decodes
  each document: checking afterwards made somebody else's conversation cost
  measurably more than one that is not there, which is one of the ways an id is
  probed for existence, and let any signed-in caller make the deployment read a
  thousand messages of somebody else's; the check is made again on the
  snapshot, which costs nothing and is what answers a conversation deleted
  between the two reads. It comes with `leaf_id`, and the branch is
  the client's walk up the parents: sending it as well would send every
  message of it twice, and that walk is the one thing left for the frontend to
  compute. `run_id` and `resume` come with a run in flight, `ended_badly`
  (run id, state, ended_at) when the last one failed, was cancelled or was
  interrupted -- and **not its `error`**, which is free text made of whatever
  a provider or a traceback said, written for an operator and kept to the
  record and the log like the detail of every other refusal. `MessageView`
  sends **no reasoning**: this build stores none in a message
  (`domain.kept_parts`), so the filter has nothing to do today and is what
  makes that true of the wire rather than of one write path. `GET /api/agents`
  sends `id`, `title` and `engine` -- no system prompt, no model, no vendor --
  over the new read-only `Turns.agents`, so nothing reaches into the
  definitions the service keeps. `Turns.cancel` takes an optional
  `conversation_id`, which the route passes because a URL names both: a run of
  another conversation is answered exactly like a run that is not there, and
  the check is in the application beside ownership rather than in a route.
  `Conversations.list_for`'s refusal now names the field and the rule and
  **not the number it was given**, since that message is what a client is
  answered with; it is a 422, the status `api/errors.py` has always given
  `InvalidValueError`, and no error class was added. `Conversations.rename`
  refuses a title with nothing in it (spaces are printable and on one line, so
  the record cannot), as an agent's title is refused, and `RenameRequest`
  carries the record's own bound (`domain.MAX_TITLE_CHARS`) so that the
  **document** says how long a title may be. A query parameter **given twice**
  is refused rather than read once (`given_once`): FastAPI takes the last of
  `?limit=1000&limit=2` and says nothing, and which one a framework picks is
  not something to build a bound on. Deleting is **not idempotent** and both
  docstrings now say so: a conversation that is not there -- gone, or somebody
  else's -- is a 404 like everything else that is not there, and only the race
  between the ownership check and the store's own delete is tolerated.
  Two things the routes made worth fixing in `api/errors.py`, which is shared
  by every route there will ever be: **a detail built from a request now
  repeats nothing at all of it** -- pydantic's `msg` quotes what it refused
  ("invalid character: found `Z` at 1"), so each of its error **types** has a
  sentence of ours (`UNREADABLE_RULES`, with one fallback for a type nothing
  has read about), and the **name** of a field nobody knows is the request's
  too, so extras are counted and not named (`unknown_fields`: "body: 2 fields
  nobody knows"); and the `Allow` of a **405** is now built by
  `allowed_methods`, from every route whose own regular expression matches the
  path asked for, because Starlette answers out of the first route that
  matched and would tell a client that `PATCH /api/conversations/{id}` -- a
  route this build serves -- is not allowed. The walk goes through an included
  router, since that is where FastAPI keeps what `include_router` added, and
  it reuses `api.api_routes` -- the **one** walk of what an application serves,
  which reads every list a router keeps routes in, goes through an included
  router and into a mount, and is what `undeclared` and the tests use too, so
  two walks cannot come to disagree. A **location** in one of those details
  keeps only the pieces that are names: pydantic puts the character offset it
  stopped at in one, so an unreadable body used to answer `body.2012`, a
  number measured off the request. The limit of that rule -- a mapping field
  or a discriminated union would put a sender's own word in a location -- is
  written on `_located` and held by a test over every request model.
  **The enums on the wire declare what is really sent**: `SentKind`
  (`text` alone -- `reasoning` is the one value `MessageView.of` guarantees is
  absent), `SentRole` (`domain.SUPPORTED_ROLES`, so no `tool`) and
  `SentBadEnd` (`domain.FAULTED_RUN_STATES`), each held to its domain set by a
  test, so no generated client has a branch for a value it can never be sent.
  **The document says what a client can rely on**: the page bound is in it
  (`limit` is `Query(ge=1, le=MAX_PAGE)`, the store's own number reaching `api`
  through `application.MAX_PAGE`, since `api` may not import `ports`), so is
  the title's (`domain.MAX_TITLE_CHARS`); every field of every answer this step
  added is **required and nullable** rather than optional, so a generated
  client types it `T | null` instead of "may be absent"; the 400, 405 and 500
  no route decides are described once as the `default` answer
  (`refusals.ANYTHING_ELSE`); and the application **does not redirect a
  trailing slash** (`redirect_slashes=False`), since a redirect on an API is a
  call a client repeats with whatever its HTTP library does to the method and
  the body. HEAD is not served by these routes and the docstring says so: the
  step that serves the built interface decides it for the static side.
  **A field given twice is refused, in the query string and in the body.**
  `given_once` reads `request.query_params.multi_items()`;
  `protection.read_once` (declared as `StrictJson` on every route that takes a
  body, with a test that says none was forgotten) re-reads the body FastAPI
  has already cached and parses it with an `object_pairs_hook`, so a repeated
  key **at any depth** is `body: a field given more than once` -- naming no
  key, since a key is the request's own text. `json.loads` keeps the last of a
  repeated key and says nothing, which is a write that asked two things and
  was answered on one of them. Its refusal is caught as a `JSONDecodeError`
  and **not** as a `ValueError`, because `InvalidValueError` is one and the
  wider catch swallowed it; and a `RecursionError` from that second parse is a
  refusal of its own (`BODY_TOO_DEEP`), because the check runs further down
  the stack than the framework's parse did -- there is a band of nesting a few
  levels wide where one succeeds and the other runs out of stack, and a body
  whose repeated fields nobody could check is not a body to accept. **To
  watch, and written into that docstring:** nothing bounds how large a body
  may be, and it is now read twice; a size limit is an operator's
  (`docs/specs/operations.md`) and belongs in front of both parses. New
  `domain.InvalidCursorError` (422, under `InvalidValueError`), raised by both
  stores' cursor parsing, so the route catches **that** and not every refused
  value when it names `query.cursor`. `backend/openapi.json` is
  regenerated: every route also **names the statuses it can refuse with**, in
  `ErrorResponse` shape, because FastAPI would otherwise describe a 422 in its
  own shape and a generated client would fail to read the body it really gets
  (`test_the_document_says_how_every_refusal_is_shaped`). The streaming
  endpoint is still outside the document, as `wire.md` says. The tests are
  `tests/unit/test_conversation_routes.py` over the fakes and `tests/turns.py`
  -- every route, the paging, a cursor and a limit refused without being
  repeated, a title no conversation could hold, a delete refused while a run
  is going, a cancel of work the executor really is carrying and of a run only
  the store knows, the writes behind the origin protection and the media-type
  check, a 405 that names every method the path really serves, a parameter
  given twice, a field given twice in a body and inside one, a body that is no
  JSON and one nested deeper than anything can read, a title that is not text
  at all, a cursor issued in somebody else's listing, a stranger's
  conversation refused without being read at all, and the routes
  before start-up -- plus the new route rows in
  `tests/unit/test_api_access.py`'s declaration table and one test in
  `tests/integration/test_create_app.py` that lists, opens, renames and
  deletes a conversation over HTTP against the real PostgreSQL in the local
  development mode.

- The **AG-UI stream**, which is the other half of the wire and the last of
  the backend (`docs/specs/wire.md`, where the three endpoints are now written
  down as a table, since they are outside the OpenAPI document). The fourth
  runtime dependency is `ag-ui-protocol` 1.0.0 (MIT, and it brings only
  `pydantic`, which FastAPI already did, so no row of `DEPENDENCIES.md` was
  needed): its event types and its `EventEncoder` are what go out, and an
  import-linter contract keeps `ag_ui` under **`api` alone** -- more tightly
  than FastAPI, which the composition root also imports, because nothing
  outside `api` has an AG-UI event to build. `api/agui.py` is the **one
  mapping**, `AguiMapper`, a class rather than a function because AG-UI
  brackets thinking and the platform publishes bare `ReasoningDelta`s: the
  brackets are derived from the sequence, and the reasoning message is given an
  id **derived** from the answer's (`reasoning_id`), so that it is the same on
  a re-attach and can never be mistaken for the answer by a client that keys
  messages by id. `MessageCompleted` crosses as a bare `TEXT_MESSAGE_END` --
  the client has the deltas, and one that did not receive them all reloads the
  conversation -- an empty delta is skipped (AG-UI 1.0.0 would accept one, and
  it says nothing), and nothing of the platform's record crosses that the
  mapping does not name: no `raw_event`, no `metadata`, and a run that ended
  badly is **one fixed sentence per state** with the state as the `code`, never
  the stored error. `api/stream_routes.py` serves `POST /api/turns` (a turn
  that begins a conversation; `agent_id` is required, because `Turns.begin`
  takes either an agent or a conversation and the route chooses nothing),
  `POST /api/conversations/{id}/turns` (a message with its `parent_id`, or
  `regenerate` -- exactly one of the two, both or neither being one fixed
  sentence) and `GET /api/runs/{run_id}/events?after=`, where `Last-Event-ID`
  says the same thing and is read when the query is absent, the two disagreeing
  being a refusal rather than something settled by preference. **A refusal is a
  status, so it comes before the stream**: the new `Watch.run(user, run_id)` is
  the ownership check asked before a byte goes out -- the same 404 for a run
  that is not there and one that is somebody else's -- and it is also where the
  conversation comes from, since a re-attached slice holds no `RunStarted` to
  read it off and every event names the thread. An `id: <position>` goes on the
  **last** wire event derived from each of the run's events -- the thinking
  brackets are derived here and the platform numbered none of them -- so an id
  means "everything derived up to this position has been sent" and a client
  re-attaching at one loses no delta and sees none twice; the response carries
  `X-Robinauts-Run-Id` and `X-Robinauts-Conversation-Id`, so a client that
  received only the headers can still re-attach. **Every stream ends with an
  event saying the run is over** -- one that merely closed is one an
  `EventSource` opens again. A slice that held no ending -- re-attaching at or
  past the last position, where there is nothing after `after` -- **reads the
  run again** and sends how it really ended, through the same mapping
  (`AguiMapper.closed`), so a finished answer is never reported as a failure;
  besides that there are `RUN_ERROR` codes `quiet` (the watcher gave up, and
  the 504 it would have been is long past), `gone` (the run is not there any
  more, its conversation deleted under the watcher) and `internal`, which says
  no more than a 500 body does while the whole chain goes to the log -- and
  which is also what a run that is somehow still active at the end of its own
  stream gets, since that cannot happen. Heartbeats are an SSE comment every
  `HEARTBEAT_SECONDS` while nothing arrives: the watcher is read by a **task**
  and the response waits on that task with a timeout, because a wait cut short
  by `asyncio.wait_for` would be a cancellation delivered inside the watcher's
  generator -- the end of the stream rather than a heartbeat. A client that
  goes away closes the generator and nothing about the run changes.
  `create_api(watch=)` and `app.state.watch` join the other two services, and
  `access.watching` reads it like `conversing`. `domain.MAX_MESSAGE_CHARS` is
  new -- the bound `text_parts` already applied, named -- so the request body
  states it as `RenameRequest` states a title's.

  Three things the wire decides that are worth finding again. **Thinking is a
  message per stretch**, under an id made of the answer's id and the position
  the stretch opened at, so a turn that thinks twice inside one answer does not
  reopen a message it has ended; and a stream that carries on from a position
  is **seeded** with the run's events up to it (`Watch.before`,
  `AguiMapper.seed` -- the mapping run with what it answers thrown away), so it
  derives the same brackets an unbroken stream would and closes the thinking a
  client holds open (through `events_of(upto=)`, which the port, both stores
  and the contract suite gained, so replaying the beginning of a stream costs
  the beginning and not the whole of it). What re-attaching may repeat is that
  one bracket and the ending, which `wire.md` says a client reads as no-ops;
  what it may **not** ask for is a position a run that is still going has not
  reached, which is a 422 (`POSITION_AHEAD`) rather than a stream that waits
  out the silence and then says in the log that a healthy run went quiet.
  **A cancellation is no failure**:
  AG-UI 1.0 has `RUN_FINISHED` with a `cancelled` outcome for a run somebody
  stopped, and sending `RUN_ERROR` would have a stock client show a failure to
  whoever pressed stop; `failed` and `interrupted` stay errors. And **a request
  body is bounded** at `api.MAX_BODY_BYTES` (1 MiB), as
  `domain.PayloadTooLargeError` (413), in three places: the declared
  `Content-Length`, before a byte is read; **the chunks as they are handed
  over**, because a body sent without a length is buffered and parsed by the
  framework before a single dependency is solved -- before `read_once` and
  before the guard that decides whether the caller is anybody at all, so an
  anonymous POST could otherwise spend megabytes to be told it needs a session;
  and `read_once` itself, which is now the backstop. The middleware wraps
  `receive` *and* `send` for a write: the chunk that would go over is never
  handed on, and whatever the application made of a body that stopped in the
  middle is dropped in favour of the 413. That is the bound the earlier note
  under `read_once` asked for, and a bound on a *request* is not the format's
  bound on a record.

  The tests are `tests/unit/test_agui.py` (every event kind, the brackets, the
  stretches, empty deltas, the endings, the roles AG-UI has a word for, and
  that the closed set `domain.TurnEvent` names is exactly what is mapped),
  `tests/unit/test_stream_routes.py` (a whole turn parsed back, the id
  discipline, dropping after **every** block and re-attaching -- the whole
  event sequence must come back to what an unbroken stream said -- the 404
  identity before any stream, 409, the two forms, cross-site, no session, a
  quiet run, a heartbeat and a client that goes away while the run finishes),
  the body cap in `tests/unit/test_api_protection.py`, and one case in
  `tests/integration/test_create_app.py` against the real PostgreSQL.
  `tests/sse.py` is the parser and a small ASGI driver: `httpx`'s transport
  runs an application to its end before it answers, which cannot show a stream
  that is still going.

- The **model configuration** and the **first engine**, LangGraph. The same
  TOML file now holds both halves: `[model_providers.<id>]` (`kind`,
  `api_key_env`, `base_url` for `openai-compatible`), `[models.<id>]`
  (`provider`, `name`, optional `timeout_seconds` and `max_output_tokens`)
  and `[agents.<id>]` (`title`, `model`, `engine`, `system_prompt`) beside
  the sign-in tables — `model_providers`, not `providers`, which is already
  the identity providers. `core/sign_in_config.py` names both key sets
  (`SIGN_IN_KEYS`, `MODEL_KEYS`, `TOP_LEVEL_KEYS`), so each parser is handed
  the whole file and an unknown key means unknown to both.
  `core/models_config.py` is `parse_models_config(data, engines=, kinds=)`,
  written like the sign-in parser: every problem at once, references checked
  against what was **declared** so that one mistake does not cascade, and the
  two sets the composition root passes in — the engines it wired and the
  provider kinds it has a client for — because `core` cannot know either.
  `domain/agents.py` grew `ProviderKind`, `ModelProviderConfig`,
  `ModelConfig`, `ModelsConfig` (whole by construction: `model_for`,
  `provider_for`), `is_env_name` and `is_endpoint_url` (https anywhere,
  http on the loopback only, a host required, no query, no fragment and **no
  userinfo** — a key is what travels there, and `https://user:sk-live@host/v1`
  would put another one in the file; the host comes from `urlsplit`, which
  unbrackets IPv6 and separates userinfo, so `http://localhost@evil.example/`
  is not this machine). `adapters/config_file.py` grew `check_api_keys`, which names every
  unset variable at once and hands back a `ProviderKeys`: a carrier that
  holds a copy, is not iterable, answers only "the key for this provider"
  and whose `repr` names the providers and never a key.
  `adapters/agents/langgraph/` is the engine and the only place LangGraph,
  LangChain and `langsmith` may be named (import contracts):
  `LangGraphAgent` compiles a one-node graph per turn with **no
  checkpointer**, streams it with `stream_mode=["messages", "updates"]`, and
  maps chunks through langchain-core's provider-neutral `content_blocks` —
  text to `AnswerTextDelta`, `reasoning` (which is what Anthropic's
  `thinking` becomes) to `AnswerReasoningDelta`, and the completed parts from
  **what was streamed**, never from the framework's final message and never
  with reasoning in the text. `chat_model` builds `ChatAnthropic` with the
  key, the timeout, `max_retries = 0` and a ceiling; the factory is
  injectable, which is how the tests run the real engine over a chat model
  they script. `force_tracing_off()` calls `langsmith.configure(enabled=False)`
  at construction, which is the switch langchain-core consults **before** the
  environment, so `LANGSMITH_TRACING=true` changes nothing and no client is
  ever built. **Anthropic only for now**: `langchain-openai` requires
  `tiktoken` and `regex`, which do not pass the licence policy, so the
  `openai` and `openai-compatible` kinds are refused at start-up with a
  message saying so (`DEPENDENCIES.md`, "Known exclusions"). `app.py` wires
  it through one table, `ENGINES` (one entry per engine), which is the whole
  of the choice of agent framework: `WIRED_ENGINES` and
  `BUILDABLE_KINDS` are derived from it, the second by **asking each adapter**
  what it can reach (`ports.Agent.kinds`), so that knowing more about an
  engine never means importing more from its sub-package — the discard test
  is that import and that one entry. An agent asking for an engine this build
  does not construct is
  a start-up refusal naming what to do, agents handed in without an engine
  must name models the configuration has, and a file with no `[agents]` table
  is a deployment that **starts with none** — `/api/agents` is empty, and the
  start-up log says so. The **local development mode may be given the same
  file** and reads only its model tables, so the chat can be developed
  against a real engine; a file holding sign-in tables is refused there. The
  variable that names the file is now `ROBINAUTS_CONFIG` (`CONFIG_VARIABLE`),
  since it is no longer about authentication alone; `ROBINAUTS_AUTH_CONFIG`
  is still read, with a start-up WARNING. The contract suite (`tests/contracts/agents.py`) now runs over
  a real engine and gained two declarations a subclass may lower —
  `answers_per_turn` and `can_answer_without_streaming` — which say what a
  turn of that engine can be **scripted** into, not what it is held to.
  Tests: `tests/unit/test_models_config.py`, `tests/unit/test_langgraph_engine.py`
  (the contract, the messages the model is given, thinking, the client's
  fields, tracing), the key checks in `tests/integration/test_config_file.py`
  (which also parses the example in `docs/specs/legacy/agents-engines-models.md`), the wiring in
  `tests/unit/test_app_composition.py` and `tests/integration/test_create_app.py`.
  `tests/live/test_langgraph_live.py` runs one real turn against Anthropic
  when `ROBINAUTS_LIVE_ANTHROPIC_KEY` is set; `tests/live` is in
  `norecursedirs`, so a plain run — and therefore CI — never collects it.
- The **second engine**, Pydantic AI, and the **swap**. Dependency:
  `pydantic-ai-slim[anthropic]`, which brings `pydantic-graph`,
  `genai-prices`, `griffelib`, `logfire-api` and `opentelemetry-api` — all on
  the allowed list, and the last two are **installed and never imported**.
  `anthropic` is pinned as a direct dependency in the same step: both
  frameworks already brought it, and this adapter *imports* it, which is what
  makes a dependency direct (the `langsmith` precedent).
  `adapters/agents/pydantic_ai/` is the engine and the only place
  `pydantic_ai`, `pydantic_graph`, `logfire`, `logfire_api` and
  `opentelemetry` may be named (import contracts; the two adapters still do
  not import each other). `PydanticAIAgent` builds a framework agent per turn
  with the system prompt as **`instructions`** — the half of Pydantic AI that
  is not carried in `message_history`, which is what lets the platform's
  history be the messages and nothing else — the whole history as
  `ModelRequest` / `ModelResponse` with text only, `ModelSettings(timeout,
  max_tokens)`, no tools and no output type. It iterates `agent.iter(...)`
  and streams the **first model request node**, then leaves: Pydantic AI
  answers an empty response, or a call to a tool that is not declared, with a
  *retry prompt* and a second model call, and retrying is sending the message
  again. `PartStartEvent` and `PartDeltaEvent` become text and reasoning
  deltas, `PartEndEvent` is passed over (it repeats the whole part), any
  `BaseToolCallPart` raises, and the answer completes with the joined deltas.
  `chat_model` builds the `AsyncAnthropic` client itself — `max_retries = 0`
  and the key header are the client's, not the provider's — and hands it to
  `AnthropicProvider(anthropic_client=...)`, with the endpoint and the key
  pinned so that no `ANTHROPIC_*` variable, profile or federation credential
  can move a request or change what pays for it. **Nothing phones home**:
  Pydantic AI has no environment switch for tracing, so
  `TRACING_VARIABLES_REMOVED` is deliberately **empty** and the adapter sets
  `instrument = False` on every agent it builds, which beats
  `Agent.instrument_all()`; `force_tracing_off()` also sets
  `pydantic_ai.BANNER_ENABLED = False`, because the framework otherwise
  writes an advertisement for its hosted observability to a server's standard
  error on the first turn. **Anthropic only**, for the same reason as the
  other engine: the `openai` extra needs `tiktoken` and `regex`.
  `ENGINES` now has both entries, so an agent's `engine = "pydantic-ai"` is a
  line of configuration.
  Tests: `tests/unit/test_pydantic_ai_engine.py` (the contract suite over the
  real engine, the messages, thinking, the empty answer, the split character,
  the tool refusal, the client's fields against a hostile environment, and a
  whole turn with every Logfire and OpenTelemetry variable set, sockets
  blocked and anything that would build instrumentation refusing to exist —
  with the counter-test that an agent which did *not* say no is caught);
  `tests/unit/test_engine_swap.py` (one conversation over both real engines
  and the real `Turns`: every answer records its engine, each engine is given
  the same stored history with no reasoning, and the two stored documents
  differ in `provenance.engine` alone); the configuration swap across a
  restart on the real PostgreSQL in `tests/integration/test_create_app.py`;
  `tests/engines.py` holds the two scripted models both swap tests share.
  `tests/live/test_pydantic_ai_live.py` is the live turn, gated the same way.
- **Both engines, and the vendor SDK under them.** `anthropic` is now a direct
  dependency and is confined by an import contract to the two agent
  sub-packages. Its `ANTHROPIC_LOG` variable, read at *import*, puts every
  request's `json_data` — the system prompt and every message — on standard
  error; both adapters now pin the `anthropic`, `anthropic._base_client` (the
  logger that emits the record), `httpx2` and `httpcore2` loggers at `WARNING`
  when the engine is built (`quiet_client_logging`) and remove the variable.
  Each logger's **own** level is set and not merely its effective one, so an
  operator who turns their root logger up to `DEBUG` afterwards does not turn
  the vendor's logging on with it. Each engine has a subprocess test that
  reproduces the leak without the engine and shows it silenced with it, and an
  in-process pair for the root-turned-up-later case; `tests/conftest.py` puts
  the four levels back after every test, since they are process-wide. **To watch:** both engines
  build a vendor client **per turn** and neither closes it — its pool is left
  to the HTTP client's own finaliser. A shared client held for the life of the
  process and closed by the lifespan, as the identity provider's is, is a
  change to both adapters and to the composition root, and is a step of its
  own.
- Open source groundwork at the root: `NOTICE`, `AUTHORS`,
  `CONTRIBUTING.md` (DCO, AI-assisted contributions, where code may come
  from), `DEPENDENCIES.md` (licence categories, the named restricted and
  excluded packages), `REUSE.toml` and `LICENSES/`; `docs/legal/` with the
  IP clearance log, the third-party list, the name-search and assets
  records. `uvx reuse lint` passes from the root: a new root-level file
  must be added to `REUSE.toml`, and prose that quotes a licence tag or
  someone else's copyright line goes between `REUSE-IgnoreStart` /
  `REUSE-IgnoreEnd` comments.
- `scripts/` — one script per gate, all of them runnable on a laptop:
  `check-lint.sh` (ruff and black, over `backend/` and `scripts/`),
  `check-tests.sh`, `check-licences.sh`, `check-audit.sh` (`pip-audit` over
  the exported locked set, markers stripped so nothing is skipped, and a
  cross-check that every pinned package really was audited), `check-reuse.sh`,
  `check-dco.sh` (sign-off on a commit range, by default what the branch adds
  to `main`), and `check-all.sh`. `reuse` and `pip-audit` run as isolated
  `uvx` tools, pinned in `scripts/tool-versions.sh` and bumped by hand: they
  are deliberately outside the lock, because `reuse` brings a GPL dependency.
  `.github/workflows/ci.yml` runs those scripts and nothing else, one per
  job, on Python 3.12, with `contents: read`, `persist-credentials: false`
  and actions pinned to commit SHAs, on pull requests and on pushes to
  `main`, `feature/**` and `fix/**`; `.github/dependabot.yml` watches `uv`
  and `github-actions` with a 10-day cooldown. The scripts are documented in
  `CONTRIBUTING.md`.
- The licence gate is `scripts/licence_gate.py`, standard library only. It
  reads the categories and the named exceptions out of `DEPENDENCIES.md` —
  the single source of truth — and applies them to every package of
  `backend/uv.lock`, runtime and development alike. It fails closed: a
  licence it cannot classify, metadata it cannot verify, a lock it cannot
  believe, all fail. Exit 1 is "a dependency fails the policy", exit 2 is
  "the gate could not do its work". To add a dependency whose metadata is
  vague or restricted, add a row to the right table of `DEPENDENCIES.md`
  (licence, version, scope are all checked). Tests:
  `backend/tests/unit/test_licence_gate.py` (also reads the real
  `DEPENDENCIES.md` and `uv.lock`) and `test_check_scripts.py` (the shell
  scripts, against stand-in tools). `backend/tests/conftest.py` stops
  bytecode being written, so that importing from `scripts/` does not litter
  the tree.
- `frontend/` — the skeleton and its gates: Vite, React 19, TypeScript in
  strict mode, Vitest on jsdom, ESLint, Prettier. What is built on top of it
  is the bullet after this one; the history and the chat are later steps.
  `src/api/client.ts` is a typed `fetch` over the types
  `npm run generate` writes from `backend/openapi.json` into
  `src/api/schema.d.ts` — generated by every check and never committed, so a
  route that changed without the snapshot being refreshed is a failing type
  check. `src/chat/index.ts` is the seam of ADR 0001, empty for now;
  `eslint.config.js` refuses `@assistant-ui/*` anywhere but
  `src/chat/assistant-ui/`, and refuses any specifier with a path segment
  `assistant-ui` from anywhere but that directory and `src/chat/index.ts`,
  which may reach it only as `./assistant-ui/...`. It reads the segment
  rather than a prefix, and refuses beside it every specifier that is not
  written plainly (`//`, `/./`, a `..` inside the path, a trailing `/`, a
  path through `node_modules/`) and every one that cannot be read at all (a
  variable or an interpolated template in `import()` or in
  `new URL(..., import.meta.url)`, `import.meta.glob`, `require`) — so there
  is one spelling of a path for the rule to read.
  `src/test/seam-rule.test.ts` runs ESLint over files that do not exist to
  prove every one of them still fires. The JavaScript gates: `vite.config.ts` carries the
  licence allowlist (the allowed list of `DEPENDENCIES.md`, over
  `rollup-plugin-license`, failing on an unrecognised licence too), writes
  `THIRD_PARTY_LICENSES.txt` into `dist/` for the wheel to carry, writes
  `bundled-packages.txt` and — under `CHECK_BUNDLED`, which
  `scripts/check-frontend.sh` and CI set — compares it instead, and holds a
  gzipped size budget. That budget was provisional at 800 KB until there was
  a bundle with the chat in it to measure; **step 21 set it at 400 KB**
  against a measured 278.6 KB — the gate's own rule, every file of `dist/`
  gzipped with the licence text excepted (`docs/specs/frontend.md`) — which
  leaves room for the features the specs already name and none for a
  dependency an order of magnitude too big.
  **Known limit:** that record is written from rollup's module graph, so a
  package reached only through a stylesheet (`@import "pkg"`,
  `url(pkg/x.png)`, a package stylesheet's own imports) is invisible to it.
  Step 18 gave that a floor — the hand-maintained `CSS_PACKAGES` list, which
  the build reads for the licence, the record and the notice — so what is
  left of the limit is a name nobody wrote down (`DEPENDENCIES.md`, "What
  the bundle's record does not see"). A scanner for
  it was written and dropped: ten rounds of review did not get it to agree
  with Vite's own resolver about what a specifier means. What the build does
  refuse, because it needs no resolver, is a CSS Module, any stylesheet
  language but plain `.css`, and a `<style>` in `index.html`. Beside it,
  `frontend/scripts/check-licences.mjs` (Node, no dependencies)
  applies the same policy to **everything the lockfile pins and npm
  installed**: the allowed list, SPDX `AND`/`OR`, a forbidden licence no row
  may excuse, and otherwise a row in the "JavaScript build tooling" table of
  `DEPENDENCIES.md` naming the same version and the same licence — a stale
  row fails too, as does a row for something the lockfile no longer pins. It
  reads the allowed and restricted lists **out of `DEPENDENCIES.md`** rather
  than carrying a copy, and `vite.config.ts` imports them from it, so the
  policy is written once; what it forbids is shared with the Python gate
  through `scripts/licence-fixtures.json`, which both test suites read. It
  also refuses a version in `package.json` that is not exact. The frontend
  has two TypeScript projects: `tsconfig.app.json` for browser code, with no
  Node types and an ESLint rule refusing Node's globals, and
  `tsconfig.tools.json` for the build configuration, the gate and the two
  tests that drive ESLint and the gate. One script, `scripts/check-frontend.sh`, runs the lot — `npm ci`,
  the licence gate, format, lint, types, tests, the build,
  `npm audit --audit-level=low`, `npm audit signatures --omit=dev` — and the
  `frontend` job runs that script. Every package is pinned to the newest
  version published at least ten days earlier: the Dependabot cooldown holds
  at adoption too, and no script can check it afterwards because the publish
  dates are not in the lockfile. The rules for npm packages are `docs/contributing/js-dependencies.md`
  and `frontend/AGENTS.md`; Dependabot watches `/frontend` with the same
  10-day cooldown. Still outstanding, and unticked in
  `docs/oss-checklist.md`: secret scanning (`gitleaks`, which has nothing to
  do with the frontend — it is listed among the day-zero checks in
  `docs/specs/open-source.md` and simply is not done yet), the SBOM and the
  release workflow, which `poc-scope.md` puts outside the POC.
- **The shell** — the interface as far as it goes without the history and
  the chat. `src/tokens.css` holds the design tokens carried over from neorc
  (`docs/legal/ip-clearance.md`) as CSS custom properties: the light values
  and, once, the dark ones, applied by `prefers-color-scheme` unless
  `[data-theme="light"]` overrules it and by `[data-theme="dark"]`
  whatever the machine says, with `color-scheme` beside each so that form
  controls and scrollbars agree. `src/styles.css` is the only place a
  stylesheet names a package — `@import "tailwindcss"`, the case a reviewer
  checks by hand — and maps Tailwind's theme onto the tokens with
  `@theme inline`, so `bg-panel` compiles to `var(--panel)` and the tokens
  outlive Tailwind (ADR 0001). Tailwind's own sources are named rather than
  discovered (`source(none)` plus two `@source` lines), so the build does
  not depend on what else is in the tree. `src/session/session.ts` is the
  store: `GET /auth/session` asked once, read through
  `useSyncExternalStore`, `loading | signed-in | signed-out`, signing out,
  and any call answered 401 flipping the interface to signed-out through an
  `onUnauthorized` callback `src/api/client.ts` now offers — no
  data-fetching library and no state library. `src/session/SignInPage.tsx`
  stands in place of every page when nobody is in: one plain link per
  provider to `/auth/login/{id}?return_to=…`, a fixed sentence for each of
  the eight `SignInErrorCode`s read out of the hash the backend redirects
  to, a generic one for a code it does not know, and the same `return_to`
  rule as `core.safe_return_to` applied before the round trip. `src/shell/`
  is the panel and what is beside it: the collapse button and the brand,
  "New chat", the history as an empty labelled list, the profile block with
  sign-out pinned to the bottom, the rail and the theme remembered per
  browser under namespaced `localStorage` keys, an overlay drawer below the
  `md` breakpoint with Escape, a backdrop and the focus following it —
  closed, it is `inert`, because it is translated off the screen rather than
  removed and would otherwise be a handful of invisible tab stops; open, it
  is a `role="dialog"` with `aria-modal`, the page behind it does not scroll
  and Tab goes round inside it — a trap that skips what is not drawn, since
  the panel holds the other layout's controls. The breakpoint is read once
  in JavaScript (`shell/breakpoint.ts`) for exactly those things, which CSS
  cannot say, and in Tailwind's own `rem` rather than the 768px it usually
  comes to: with a default font size of 24px the two would disagree about
  which layout is on. Also: a
  light/dark/system toggle applied as `data-theme` on `<html>` (the
  remembered one applied in `main.tsx`, before the first paint), the
  permanent banner of the local development mode, and an `AgentPicker` over
  `GET /api/agents` — always shown, disabled with a reason when there are
  none, remembered otherwise, and asked for by the shell rather
  than by the picker, which "New chat" remounts. `src/ErrorBoundary.tsx` keeps a
  render that throws from leaving a blank page — **outside everything**,
  the sign-in page included, since that is the only page somebody who is
  not signed in can reach. **No projects section**: projects are out of the
  POC. Routing came in the step after this one; until then the hash was read
  for the sign-in error and the return target alone.
  Three packages, **adopted as one change** — they are the chat UI's
  styling stack, and the same reasoning `DEPENDENCIES.md` already allows for
  a framework and the client it is reached through applies: Tailwind without
  its Vite plugin builds nothing, and a shell with no icons is not the
  shell. `tailwindcss` 4.3.3 (MIT, published 2026-07-16; a runtime
  dependency, because its CSS is what ships) with `@tailwindcss/vite` 4.3.3
  (MIT, 2026-07-16, build tooling), and `lucide-react` 1.45.0 (ISC,
  2026-09-11), the one allowlisted icon package, imported by name. All three
  pins are the newest version published at least ten days before adoption;
  all three carry registry provenance and need no install script. One
  `DEPENDENCIES.md` row was added: `@tailwindcss/node` pins `lightningcss`
  at exactly 1.32.0 while Vite is on 1.33.0, so the tree holds both and the
  MPL-2.0 exception needed a second, development-scoped row.
  `frontend/scripts/fixture-server.mjs` serves a built `dist/` with fixture
  answers for looking at the states a real sign-in would otherwise be needed
  for; it is in no check and ships nowhere.
- **What ships through a stylesheet is now recorded too.** The step-17 known
  limit — rollup's module graph does not see a package a stylesheet reaches,
  so `bundled-packages.txt` and `THIRD_PARTY_LICENSES.txt` missed it —
  becomes a **hand-maintained list**, `CSS_PACKAGES` in
  `frontend/vite.config.ts`, holding `tailwindcss`. Nothing discovers the
  names (the scanner that would is still future work); once one is written
  down the build reads that package's own metadata, holds its licence to the
  allowed list, merges it into `bundled-packages.txt` and appends its
  `LICENSE` to `dist/THIRD_PARTY_LICENSES.txt`, each of which fails the
  build rather than being skipped. A stylesheet that gains an `@import` or a
  `url()` naming a package adds the name there, and that line is what a
  reviewer looks at. What is still lost is a name nobody wrote down.
- **The history, the routing and a conversation read.** No new dependency:
  `src/router.ts` is hash routing written by hand over `hashchange` and
  `useSyncExternalStore` — `#/` (the empty chat) and `#/c/<id>` (that
  conversation), everything else the empty chat, and **reading a hash never
  writes one**, so `#/sign-in?error=…` is left for the sign-in page, which
  reads it itself. A conversation id is held to the UUID shape in one place
  before it goes into a URL or a request path, so no `/` or `#` in one can
  forge a route. The hash is the one part of a URL no server sees: the
  static files need no fallback route and nothing depends on the `/ui/` they
  are served under. `src/history/` is the panel's list: `useHistory` over
  `GET /api/conversations`, thirty at a time, "Load more" with the opaque
  cursor until `next_cursor` is `null`, folding by id because paging walks a
  list that is changing (`docs/specs/legacy/conversations.md`, "Listing"), a
  numbered ask so that a page arriving after a refresh is dropped rather than
  spliced into a listing that no longer exists, and every write followed by a
  fresh first page. `HistoryList` draws it as **links** (`#/c/<id>`, so a
  conversation can be opened in a tab of its own), with the open one
  `aria-current="page"` and an empty title shown as "Untitled" — a title is
  the beginning of the first message, and a message with no text gives none.
  Renaming is inline (Enter saves, Escape cancels, the focus follows the box
  and comes back); deleting asks in the row — **no `window.confirm`** — with
  the focus on "No"; a refusal is said in the row, and a 409 becomes "still
  answering", because the API's own detail names a run for an operator's log.
  Escape inside a row shuts that row rather than the drawer around it. The
  keyboard is never left nowhere: a control in flight says `aria-disabled`
  rather than `disabled`, since a disabled control drops the focus on
  `<body>` — where Escape would reach the shell's listener instead of the
  row — and a row that the fresh page no longer holds hands the focus to the
  list itself, which carries `tabindex="-1"` for exactly that. A title is
  bounded **in code points**, as `domain.MAX_TITLE_CHARS` is, rather than by
  an HTML `maxlength`, which counts UTF-16 units and would cut 61 emoji the
  route would have taken.
  `src/conversation/` opens one: `useConversation` over
  `GET /api/conversations/{id}`, the tree walked from `leaf_id` up the
  `parent_id`s into the branch it opens on — the walk ends rather than loops
  on rows that are not a tree — and `cancelRun`. The read-only
  `ConversationView` that stood here was a stand-in, and step 21 deleted it:
  what it showed, the chat shows, and one answer — "this conversation is not
  here" — for a 404 moved there with it, since a conversation that never
  existed and one of somebody else's are the same answer.
  `src/chat/index.ts` gained the **types** the chat needs from the
  application (`ConversationId`, `AgentId`, `ChatProps`): the seam is the
  vocabulary both sides already use.
  `frontend/scripts/fixture-server.mjs` gained the conversation routes and
  the `history` and `conversation` scenes, whose renames, deletes and cancels
  really change what it serves.
- **The vendored chat components**, in
  `frontend/src/chat/assistant-ui/vendor/`: sixteen files copied from two
  shadcn-style registries — eleven from assistant-ui's
  (`https://r.assistant-ui.com/`) and the five shadcn/ui components they
  import — both MIT, both recorded in `docs/legal/ip-clearance.md` and
  `third-party.md`, with their licence texts beside them and `README.md`
  there saying which file came from which item, what was changed in it, how
  each package was judged and how to re-sync. A seventeenth file,
  `lib/utils.ts`, is **ours**: it replaces the registry's `utils` item, which
  now re-exports a `cn` package this project does not take, and it carries
  our header and its own `REUSE.toml` line. They are inside `src/`, so
  `tsc -b` and `eslint .` cover them, and `src/test/vendor.test.ts` holds the
  README's file list, the licence texts and the copy's imports to what is on
  disk. **Step 21 is what imports them**; until it did they were out of the
  bundle both ways, rollup never reaching them and `src/styles.css` saying
  `@source not "./chat/assistant-ui/vendor"` so that Tailwind's scan — which
  reads files rather than imports — did not compile 40 kB of utilities for
  markup no page rendered. That line is gone.
  Six modifications, listed in the README: relative paths instead of `@/`
  aliases (no `paths` in `tsconfig`, and the seam rules read one spelling per
  path), `lib/utils.ts` as the `clsx`/`tailwind-merge` `cn` rather than the
  three-week-old `cn` package, the attachment composer taken out (the POC has
  no attachments), one field read as optional because the cooldown pins
  `@assistant-ui/react` a release behind what the registry is written
  against, this repository's Prettier, and two ESLint rules off for that
  directory alone.
  **One dependency change** — the chat library and what its styled components
  import, adopted together with the copy because the copy cannot type-check
  without them, so they are the change rather than a feature's side effect.
  Each is the newest version published on or before 2026-09-12, which is the
  ten-day cooldown: `@assistant-ui/react` 0.15.19 and
  `@assistant-ui/react-markdown` 0.14.15 (both 2026-09-11), `remark-gfm`
  4.0.1 (2025-02-10), `radix-ui` 1.6.7 (2026-07-24),
  `class-variance-authority` 0.7.1 (2024-11-26), `clsx` 2.1.1 (2024-04-23),
  `tailwind-merge` 3.7.0 (2026-09-12), and the two Tailwind plugins the copy
  is written against, `tw-animate-css` 1.4.0 (2025-09-24) and `tw-shimmer`
  0.4.13 (2026-09-11), which are reached only through `src/styles.css` and so
  are named by hand in `CSS_PACKAGES`. Three of the nine carry no provenance
  attestation and four have a single maintainer; `docs/contributing/js-dependencies.md`
  was amended to say what is really required of each, and the judgement per
  package is in the README's vetting table. `assistant-cloud` 0.2.2 arrives
  under `@assistant-ui/react` and is configured by nothing.
- **The chat**, behind `src/chat/index.ts`, which now exports a `<Chat>`
  beside its types — a conversation id or `null`, the chosen agent, "this
  conversation was created", "the turn is over, ask for the list again", and
  a node to draw above the box on an empty chat (the shell's agent picker).
  Nothing in those types names a chat library, and the shell imports that
  file and nothing else (ADR 0001). Behind it: the client, the state, the
  runtime that hands one to the other, and the styling that makes the copied
  components and the shell one interface.
  **The AG-UI client is ours**, `src/chat/assistant-ui/agui/`, and it added
  **no dependency**: `sse.ts` is the format read off a `fetch` body as it
  arrives (`\n`, `\r\n` and a bare `\r`, a chunk that ends between the two
  halves of one of them, several `data:` lines, comments — the `: keep-alive`
  is invisible — unknown fields ignored, abortable); `events.ts` decodes the
  vocabulary `api/agui.py` really emits and **ignores what it does not know**,
  so a newer deployment's events do not stop an answer arriving; `client.ts`
  opens the three streaming routes, reads the run and the conversation out of
  the response headers, and **carries on where it left off** when a
  connection drops before the run ended — the last `id:` it saw as
  `Last-Event-ID`, a bounded number of tries with a backoff, and a terminal
  event stopping it, because every stream ends with one. **Opening is inside
  that loop**, so a re-attach whose *request* never arrives — the likeliest
  failure of a network that went — is retried like a stream that connected
  and then ended; a **refusal** (404, 422, 401) is not, because asking again
  asks the same question. The budget is spent on connections that do not
  deliver rather than on time: a stream that delivered an event the platform
  numbered resets it — **delivered**, meaning an id that moved the position
  forward, since a block replayed at a position already seen is a delta that
  would be said twice and a reconnection that would never stop. An event
  saying the run is over is read whatever its id says: every stream ends with
  one, and a watcher that dropped it would wait for an answer already given. A refusal before the stream is an `ApiError` through
  `src/api/client.ts`'s own mapping, which grew one export (`refused`) so
  that there is one of those and not two. Two bounds guard the reader: the
  two ids a stream names itself by are held to the uuid shape
  (`router.isUuid`) because both go straight into a request path, and a line
  or a block longer than four mebibytes ends the stream rather than growing a
  string without bound.
  `@assistant-ui/react-ag-ui` was **not** taken: at 0.0.60 it is still pinned
  to `@ag-ui/client ^0.0.59` while the protocol is at 1.0.0, so it would have
  installed a second, pre-1.0 client beside the 1.0 one
  (`docs/specs/wire.md`, "Details likely to change", where the decision is
  recorded).
  **The state is a reducer**, `src/chat/assistant-ui/state.ts`: no `fetch`,
  no timers, no React, so the interesting half of the chat is a thing a test
  drives one event at a time. It holds the conversation as last read **plus
  the message the run is producing** — which is in no conversation until it
  is complete — and the whole tree with every parent, because that is what a
  branch picker is. The three no-ops of `docs/specs/wire.md` are written
  there and tested by name: a `*_START` for a message already held open, a
  `*_END` for one not held, and the terminal event of a run already seen to
  end — and, defensively, a delta for a message that is already complete,
  which is the store's now and would otherwise be said twice. Every message
  is converted for assistant-ui at most once, keyed on its own identity: the
  reducer builds a new object only for a message it touched, so a delta into
  a long conversation costs one conversion and not all of them (measured at
  400 messages: 46 ms for 200 deltas, 2 ms with the cache). Thinking is a part of its own per stretch (a turn may think twice, and
  each stretch is a message of its own on the wire), and a `RUN_ERROR` is one
  fixed sentence per code — never the backend's own sentence and never the
  run's stored error.
  **The runtime is `useExternalStoreRuntime`**, `runtime.tsx`: our messages,
  our branches, and none of assistant-ui's persistence. Read from the
  adapter's source: `messages` is a flat list the runtime relinks, so the
  branches beside the one being read would exist only once it had been shown
  them — `messageRepository` takes **every message with its parent and a
  head**, which is the shape `GET /api/conversations/{id}` already answers
  in, so the picker has the whole tree from the first render. Branch
  switching is offered when `setMessages` is present, so it is present and
  does nothing, and the switch itself is heard through
  `unstable_onBranchChange`, which fires on a picker's click and on nothing
  else; where it lands is written down with `PUT .../leaf`. `onNew`,
  `onEdit` (a sibling under the parent of the message replaced), `onReload`
  (`{regenerate: <the answer's id>}`) and `onCancel` are the wire's turns.
  After a turn ends the conversation is read again — **the store is the
  truth**: real ids, the answer's provenance, the branch its author is on —
  and the panel's list is asked for again. Opening a conversation with a run
  in flight attaches to it at `resume.after` — and so does **every** read,
  including the one that ends a turn: another tab may have begun a run in the
  meantime, and taking its id without watching it would leave the thread
  answering with nothing reading the answer. A first message whose request
  lands after its sender has opened another conversation reports nothing and
  claims nothing: the shell is not taken to a conversation nobody asked to
  see, and the one that *was* opened is read rather than skipped. A second
  turn asked for while one is on its way is **told** rather than dropped
  (`ONE_AT_A_TIME`) **and its text is put back into the box**, which empties
  itself when it hands a message over; asking for an answer again is told the
  same thing without the sentence about a message, since a regeneration
  carries none. That notice is a field of its own and
  not the sentence a run leaves behind: it is about what the person just
  did, so a run starting milliseconds later must not wipe it, and what
  clears it is their next turn or leaving the conversation. A turn the server
  refuses takes off **the question that turn added and no other** -- a
  question from a turn that is still going may be on the screen too, and an
  answer may already hang under it, and taking that away would leave the
  answer with a parent the tree has not got, which assistant-ui refuses with
  an exception -- and puts the branch back exactly where it was, which is not
  the refused message's parent: a retry of a turn that went wrong hangs under
  the *parent* of the question nobody answered, and that question is a
  message the conversation really has. **A stop whose request fails changes
  nothing**: the run was not cancelled and its stream is still watching, so
  what is said is that the stop did not arrive, and the answer carries on. **A connection that went is not
  a run that ended**: the store is read once and, if the run is still in
  flight there, it is watched again with the client's budget reset; only a
  second loss is a sentence, and it leaves nothing "answering" — the box and
  the answer's "try again" work. **One turn at a time**, refused here as well
  as by the backend's 409, which is what keeps at most one question on the
  screen that the server has not been told about, so a turn that *is* refused
  can be taken back off it. The thread says it is running only for a run this
  chat knows the id of: between the send and the response's headers there is
  nothing to stop, and offering a stop there would have assistant-ui take the
  question back out of the thread and put its text into the box. What the
  runtime rewrites in its own repository — after a stop, after a branch it
  resolved to nothing — is handed straight back through `setMessages`, since
  the conversation is the server's and a head it has dropped and we still
  name is an exception thrown from inside the library. A stop before the
  first word is the one thing it cannot put right by itself: it moves the
  trailing question's text into the box and takes it back only when it can
  see that the store still holds that message, which it cannot when what it
  moved is the branch's end — so the box is cleared from this side, after
  every render, and only while it still holds exactly what was put there and
  the box was empty before the stop -- a draft somebody had already typed is
  theirs. Nothing is ever handed to the library naming a message it has not
  been given: the tree it is built from drops a child whose parent is not
  there and falls back to the branch that is, saying so once in the console,
  because an exception from inside a render is the whole interface gone over
  a message. A read of the store hands back the message objects this state
  already holds
  wherever the store still says the same thing about them, so the
  conversions, and the `createdAt`s with them, survive the read that ends a
  turn. One rule came out of running
  this against the real backend rather than a fixture: where the branch ends
  on a **question nobody answered** — which is what a run that failed, was
  cancelled or was interrupted leaves, since the answer it was producing is
  in no conversation — the next message hangs under that question's *parent*,
  because the format refuses a question under a question and asking again is
  how such a turn is retried. What the tree gains is a sibling, which the
  branch picker then shows.
  **The styling** is the copied components on Tailwind over the same tokens:
  `styles.css` maps shadcn's palette names (`background`, `foreground`,
  `muted`, `primary`, `border`, `ring`, …) onto `tokens.css` rather than
  editing the copy, and where a name was already taken — `muted` was our grey
  *ink* and is shadcn's subtle *surface*; `accent` was our primary button and
  is shadcn's hover surface — **the shell gave it up**, so there is one
  vocabulary over the tokens and not two. `dark:` is redefined as a variant
  matching `tokens.css`'s two rules, since Tailwind's own reads
  `prefers-color-scheme` alone and the interface has a toggle that overrules
  it. The heading stays the shell's (there is no top bar), the picker is
  drawn in the Thread's welcome slot, which on an empty chat sits directly
  above the box, and the chat is **not keyed by the conversation**: a first
  message creates one and the route follows it, and remounting on that would
  throw the arriving stream away.
  `frontend/scripts/fixture-server.mjs` gained four scenes that really
  stream — `streaming`, `reattach` (a run already going when the page opens),
  `drop` (the connection goes in the middle of an answer and the run does
  not) and `error` — over the real format, positions and headers, so the chat
  can be looked at doing what it is for.
- **One wheel, and the command that runs it.** `robinauts.api.ui` serves the
  built interface: `index.html` at `/ui/`, the assets under `/ui/assets/`, and
  the Content-Security-Policy of `docs/specs/frontend.md` on the interface's
  answers **and on nothing else** — the API keeps `nosniff` and
  `Referrer-Policy` and gains no policy over a document it has not got. The
  policy carries `base-uri 'none'` and `form-action 'none'` beside
  `frame-ancestors 'none'`, which are the three `default-src` does **not**
  cover: the page's own script and stylesheet are named relatively, so an
  injected `<base>` would re-point both. `X-Frame-Options: DENY` goes with it
  for a browser too old to read a policy.
  Caching is by **name**: a file under `assets/` whose name carries Vite's
  eight-character digest (`HASHED`) gets a year of `immutable`, one that does
  not gets `no-cache`, and everything else — the page above all — `no-store`.
  Exactly eight, not "at least": the digest's alphabet includes the hyphen, so
  a lower bound would read `put-here-by-hand.js` as hashed. A path with a
  **dot-segment** in it is refused (404) before the static files see it: a
  directory the build writes into is one an editor or a stray `.env` writes
  into too, and `hatch_build.py` leaves such a file out of the wheel as well.
  `/` and `/ui` answer `GET` **and `HEAD`** with a 302 to `root_path` +
  `/ui/`, declared `public()` and out of the OpenAPI document; `/ui` is a
  **redirect and not a copy of the page**, because the build names its assets
  relatively (`base: "./"`, so one bundle works under any prefix) and a
  document served at `/ui` would ask for `/assets/`. A refusal under `/ui/` is
  built **inside** the mount (`api.errors.http_refusal`, shared with the
  application's own handler) so that it carries the policy like every other
  answer at that path — a 404 for a file that is not there, and a 405 with
  `Allow: GET, HEAD` for a write, which `StaticFiles` raises bare. The request
  protection lets the interface's paths past its **write** checks
  (`ui.serves_files_only`, never past the loopback check): they read no body,
  so `DELETE /ui/index.html` is the method they have not got rather than a
  complaint about a content type.
  Everything under `/ui/` is public: the page decides what to show somebody
  who is not signed in, and a sign-in page only the signed-in could fetch
  would be a circle. The mount is the one new entry in
  `api.access.FRAMEWORK_PATHS`. An installation with **no** interface — a
  development checkout, where the built files are never committed — answers
  one 503 page at every path under `/ui/`, says the same sentence in the
  start-up log, and serves the API as usual.
  The wheel carries `frontend/dist` as `robinauts/ui/`, which
  `app.packaged_ui` reads through `importlib.resources` exactly as
  `datastore.schema_sql` reads its own file. `backend/hatch_build.py` is why
  there are two plugins: a **metadata** hook stages `LICENSE`, `NOTICE` and
  the frontend's `THIRD_PARTY_LICENSES.txt` beside `pyproject.toml` and names
  them in `license-files` — hatchling settles that field before it runs a
  single build hook, and a PEP 639 glob cannot climb out of the project
  directory — and a **build** hook adds the force-include, file by file so
  that a dotfile cannot ride along, and refuses a wheel whose interface, or
  whose notices, are missing. An editable install (`uv sync`) is exempt and is
  the only thing that is; a refusal clears the staged copies on its way out,
  since `finalize` does not run when a build raises and that failure is the
  expected one. The build hook is registered on the **sdist** target too,
  where it adds and refuses nothing and its `finalize` is what takes the
  staged copies away again; an sdist is a copy of `backend/` and carries no
  interface, so a wheel built from one is refused with a sentence saying where
  a wheel does come from. An sdist's `license-files` are this project's own
  two and **never** the bundle's notices — it carries no bundle, and listing
  them would make its metadata depend on whether somebody had run the frontend
  build; which target is being built is read from the builder module the PEP
  517 entry point imported, and pinned by tests on both sides. Nothing is
  staged or cleared outside a checkout, because in an unpacked sdist the files
  of those names are the distribution's own — told apart by `PKG-INFO`, which
  every sdist carries and no checkout has, and **not** by what is beside the
  directory: an sdist unpacked inside the repository has the repository's
  `frontend/` next to it, and a rule that read that would have cleared its
  licence files and built it a wheel out of somebody else's bundle. Builds are
  sequential: the staged copies are real files and two builds in one checkout
  would share them.
  `robinauts.cli` is `start` (`--host`, `--port`, `--uds`,
  `--dev-no-sign-in`, `--log-level`, `--forwarded-allow-ips`), `db init` and
  `version`, on `argparse`; logging is the command's — one handler on
  **stdout**, the root logger only, so the vendor loggers the engines
  quietened stay quiet — and the access log is filtered to **cut every line at
  the `?`**, because `GET /auth/callback/…?code=…&state=…` is an authorization
  code in a file. `--uds` binds a unix socket instead of an address and cannot
  be given with `--host` or `--port`. **The command binds it itself**, at
  `--uds-mode` (`0o600`) and before it listens, and hands it to uvicorn
  already listening: uvicorn chmods a socket it created to `0o666`, which for
  a deployment answering without sign-in is the API handed to every account on
  the machine. A socket is on no network, which is what the mode's loopback
  rule asks; who on this machine may open it is the file's mode and its
  directory's, and the docstrings say that rather than "stricter than
  loopback". The path is refused rather than written over if something is
  there — and a bind that **lost a race** for it leaves the winner's socket
  alone, since only a bind that succeeded makes the file ours. Removing it is
  a signal handler rather than a `finally`: uvicorn restores the handler it
  found and re-raises, so the process dies inside `server.run()` and nothing
  after it runs. The handler unlinks only while the file is still the one this
  process bound (device, inode **and** the moment it was made — a freed inode
  number is handed straight back out), and if the handler before it ignores
  the signal it exits `128 + signum` itself, because a server told to
  terminate terminates. `--forwarded-allow-ips` defaults to `*` with `--uds`,
  because a connection over a socket has no address and only what may open the
  file can connect — which assumes the default mode, and a deployment that
  widens it says so for itself.
  uvicorn is built rather than `uvicorn.run`, so a start-up that failed is
  this command's exit code and not uvicorn's: 0, 2 for a usage error or a
  configuration one (a `ConfigError` printed as its problems, one per line, no
  traceback), 1 for a failure. A **database that will not open** is one line
  too: `datastore.open_pool` turns every way asyncpg refuses to connect into
  `domain.DatabaseUnreachableError`, so the driver stays inside `datastore`
  and a command prints what it said — through `domain.without_secrets`, which
  takes any connection string out of it, because that is where the password
  is. `uvicorn` is the one new dependency, pinned a release behind the ten-day
  cooldown, and not `uvicorn[standard]`.
  `scripts/build-wheel.sh` and `scripts/check-wheel.sh` are the gate — build
  the bundle, build the wheel into an empty directory of its own, look inside
  it (the interface, the schema, the three licence files and their metadata
  lines, every asset hashed, no dotfile), install it into an empty virtual
  environment, run the command — and CI's `wheel` job runs it and uploads the
  wheel as the artifact the POC is deployed from.

- The deployment guide, [`docs/deployment.md`](../deployment.md): the
  prerequisites, getting the wheel, a system user and a virtual environment,
  the role and the database, a complete worked `robinauts.toml` (both halves,
  with the allow list and one agent), registering the redirect URI at Google
  and at Okta and which claims are read, the environment file and a systemd
  unit, nginx and Caddy, the first start and what a refusal means, verifying,
  operating and upgrading, and a table of what can go wrong. Its two TOML
  blocks are **parsed by the real parsers** in
  `backend/tests/integration/test_config_file.py`, together with the redirect
  URIs it tells an operator to paste in. `README.md` points at it.
  The release is now **two** files, not one: `scripts/build-wheel.sh` also
  writes `requirements.txt` beside the wheel — the locked runtime set of
  `backend/uv.lock`, pinned and hashed (`uv export --locked --no-dev`) —
  because `pip install robinauts-*.whl` resolves the wheel's *ranges*
  against PyPI on the day and lands somewhere the licence gate and
  `pip-audit` have never looked (it drifted on four packages the first time
  it was tried). A deployment installs that first and then the wheel with
  `--no-deps`. `scripts/check-wheel.sh` refuses a release directory without
  it, CI's artifact carries both, and
  `backend/tests/integration/test_wheel_contents.py` reads the export
  command out of the build script and holds what it produces to the lock,
  to `pyproject.toml`'s runtime dependencies and to a hash on every pin.
  [`docs/working-notes/deployment-rehearsal.md`](deployment-rehearsal.md) is
  the record of walking it here, and the one place that says where each of the
  scope's seven "Done when" items stands — what was rehearsed, what needs the
  real machine, and what needs a model key. `scripts/rehearse-deployment.sh`
  and `scripts/rehearse_deployment.py` are what it ran: a development tool
  run by no check (the Python half is linted by `check-lint.sh`; the shell
  half by nothing, there being no shellcheck here), which builds the wheel,
  installs it into an empty environment from the locked set,
  creates a throwaway database, and drives a whole deployment over https —
  two stand-in identity providers, a small TLS terminator in front, three
  people signing in, private conversations, a dropped and re-attached stream,
  and a grep of the log, of every answer it read and of every database row
  for the secrets.
- The **`anthropic-compatible` provider kind**, and with it **OpenRouter in
  this build**. `ProviderKind.ANTHROPIC_COMPATIBLE` is an endpoint that speaks
  Anthropic's Messages API at an address the operator gives, as against
  `anthropic`, which is the vendor at the constant both engines pin. The two
  kinds that name a *protocol* rather than a vendor --- this one and
  `openai-compatible` --- are `domain.KINDS_WITH_BASE_URL`: `base_url` is
  **required** for them, checked by the same `is_endpoint_url` (https, or http
  on loopback, no query, no fragment, no userinfo), and refused for the rest,
  so a vendor's endpoint is never a second answer to "where is it". Both
  engines gained the kind in `kinds` and one function, `endpoint_of`, which is
  the whole of the choice; the client is otherwise identical --- key pinned,
  proxy `None`, the key as a header, `max_retries=0`. The SDK appends
  `/v1/messages`, so an operator reaching OpenRouter writes
  `base_url = "https://openrouter.ai/api"` and model names like
  `anthropic/claude-sonnet-5`. `BUILDABLE_KINDS` therefore holds two kinds and
  the refusal reads *"this build cannot reach 'openai' providers; it was built
  with anthropic, anthropic-compatible"*. What the licence exclusion still
  costs is a vendor reachable over OpenAI's protocol alone, which OpenRouter is
  not (`DEPENDENCIES.md`, `docs/specs/legacy/agents-engines-models.md`, `docs/deployment.md`).
  `backend/tests/live/test_vendor_routing.py` proves where a turn's request
  really goes: one turn per engine at OpenRouter with a **bogus, key-shaped
  key**, asserting that it arrived at `https://openrouter.ai/api/v1/messages`
  and came back as that vendor's own `User not found` 401. It costs nothing
  and needs no key -- so, unlike its neighbours there, it is marked `io` and
  not `live` -- and it skips rather than fails when there is no route to the
  vendor or when the answer is any status but the 401, with that status in the
  reason. It is in `tests/live/` behind `ROBINAUTS_LIVE_ROUTING=1`, which keeps
  the property the engine steps recorded: **no ordinary test run reaches a
  provider**, CI included.
- **`demo/`**, beside `backend/` and `frontend/`: `demo/start.sh` brings the
  whole platform up on one machine and `demo/stop.sh` takes it down
  (`--reset` deletes the data too). It is the **local development mode** and
  says so everywhere --- no sign-in, one local user, loopback only, never a
  deployment. `start.sh` refuses to run as root, checks `uv` and Node (asking
  nvm for `frontend/.nvmrc`'s version), says so and stops if the demo is
  already up, and reads **one key** --- `OPENROUTER_API_KEY` or
  `ANTHROPIC_API_KEY`, from the environment or from a mode-0600 `demo/.env`
  that is **read and never sourced**; neither is ever printed or written into a
  file. Then: a throwaway PostgreSQL under `demo/.state/pgdata`
  (`demo/pg.py`, driving the binaries `pgserver` ships, run as a *tool* with
  `uv run --with pgserver==0.1.4` and not a dependency), the configuration
  written from `demo/robinauts.toml.in` by `demo/config.py` --- which
  substitutes literally, refuses what a TOML string cannot hold and reads the
  finished file back, because the model name is a value a person types and
  `sed` would read a `&` or a backslash in it as its own language --- one
  provider, one model, and **two agents, one per engine**, so the picker shows
  the swap; the interface built if `frontend/dist` has none; the platform
  installed from the lock **non-editably into `demo/.state/venv`**, `--no-dev`
  and on the CPython 3.12 every gate is judged on (an editable install has no
  `robinauts/ui/`, so `/ui/` would answer "not built"; `backend/.venv` is left
  alone); `robinauts db init`; and the server in the background with its pid
  and its log in `demo/.state`. **The key reaches the server and nothing
  else**: both variables are taken out of the environment as soon as they are
  read, and the one in use is put back on the server's own invocation, so
  PostgreSQL, npm and uv never hold it. Every failure is one line and a non-zero exit,
  and a server that would not start prints the end of its own log.
  `scripts/check-lint.sh` now reads `demo/` as well as `scripts/`.

## Steps

### Step 0 — documents   (feature/poc-0-documents)

Summary: the open source groundwork — `NOTICE`, `AUTHORS`,
`CONTRIBUTING.md`, `DEPENDENCIES.md`, `REUSE.toml`, `LICENSES/`, and
`docs/legal/` (IP clearance log with the neorc sign-in design as first
entry, third-party list, name-search and assets records). `reuse lint`
passes over the whole tree. Implemented by the driver. CI, `SECURITY.md`,
the code of conduct and governance files are not in this step.

Review: 1 round.
- High: 2
  - No provenance record for the backend layout convention — left as is, by
    the project owner's decision: it is his own earlier work, contributed
    under the DCO. `CONTRIBUTING.md` now states the rule that covers it
    (one's own earlier work, sole rights).
  - `DEPENDENCIES.md` claimed the build enforces the licence rules while no
    CI exists — fixed: reviewers check by hand until the gate lands
    (step 1).
- Medium: 5 (5/0)
- Low: 4 (4/0)

Checks: `uvx reuse lint` (compliant, 46 files), `uv run pytest` in
`backend/` (1 passed).
Not done / to watch: step 1 must remove the "until that gate is in place"
sentence from `DEPENDENCIES.md` once the gate exists. Branch protection and
the DCO check are GitHub settings for the project owner.
Important design decisions made / open questions: `pgserver` is not to be a
dependency (no licence metadata) — tests take the URL of a PostgreSQL they
are given. The name-search record is still to be added by the owner.

### Step 1 — ci-python   (feature/poc-1-ci-python)

Summary: every gate is a script under `scripts/` that runs locally, and
`.github/workflows/ci.yml` only calls them: lint, tests with the
architecture contracts, the licence gate over the whole locked set,
`pip-audit`, `reuse lint`, the DCO check. Dependabot for `uv` and
`github-actions` with a 10-day cooldown; actions pinned to SHAs verified
upstream. The licence gate reads its policy from `DEPENDENCIES.md` and fails
closed. JavaScript gates, `gitleaks`, SBOM and the release workflow are not
in this step.

Review: 4 rounds.
- High: 4
  - An editable path dependency was treated as this repository and passed
    unread (a GPL package passed as Apache-2.0) — fixed.
  - A named development exception covered "no licence at all" and
    non-commercial terms, which the policy forbids — fixed.
  - `check-audit.sh` passed having audited nothing when `uv export` failed
    inside a pipeline — fixed; the other scripts swept for the same bug.
  - The licence gate passed over a lock with no packages, or of an unknown
    format — fixed: such a lock is an error (exit 2).
- Medium: 11 (11/0)
- Low: 19 (19/0)

Checks: `scripts/check-all.sh` — lint, 147 tests, licences (20 packages),
audit (19 of 19 pinned packages audited), reuse, DCO — all pass. Shown to
bite: an LGPL dependency, a vendored GPL package, a removed or relicensed
row of `DEPENDENCIES.md`, an empty lock, all turn the gate red.
Not done / to watch: the workflow has never run on GitHub from here; the
first push is its first run. Making the checks required is branch
protection, for the project owner. The step is about 2,500 lines, well over
the aim: most is the licence gate and its tests.
Important design decisions made / open questions: `reuse` and `pip-audit`
run as pinned `uvx` tools outside the lock (`reuse` would bring a GPL
package into it); `uv` is pinned in `scripts/tool-versions.sh` too, bumped
by hand. `colorama` (development only) declares only "BSD License" and is
excepted by name, licence and version after a hand check. A dependency
whose metadata names only a licence family will need the same hand check. A
branch with an open pull request runs CI twice (push and pull_request).

### Step 2 — signin-core   (feature/poc-2-signin-core)

Summary: the rules of sign-in, pure and standard library only. `domain`:
the records (identity, user, session, pending sign-in, provider, allow
entry, the sign-in configuration) and the error hierarchy with the spec's
eight fixed codes. `core`: allow-list matching, ID token decoding and claim
checks, identity extraction, URL and issuer normalisation, validation of a
raw configuration into domain objects with every problem reported at once,
secret hashing and the PKCE challenge. No ports, no IO, no random
generation, no roles: those come later or are outside the POC.

Review: 2 rounds.
- High: 2
  - `exp` / `iat` accepted NaN and Infinity, which skipped the expiry check
    — fixed, in the claim check and in the token decoder.
  - A trailing dot in the configured Google issuer switched every
    Google-specific rule off (`email_domain` accepted, the `hd`/gmail rule
    on verified email skipped) — fixed: hosts are normalised, and one
    function, `domain.is_google_issuer`, decides by host.
- Medium: 5 (5/0)
- Low: 11 (11/0)

Checks: `scripts/check-all.sh` — 563 tests with the architecture contracts,
lint, licences, audit, reuse, DCO — all pass.
Not done / to watch: `now` is a timezone-aware `datetime` everywhere; the
Clock port must hand out aware datetimes. About 2,900 lines with tests,
over the aim.
Important design decisions made / open questions:
- The issuer that discovery publishes must equal the configured one
  (`core.check_published_issuer`); it is never added to the accepted `iss`
  values.
- An empty `providers` table is a configuration error (nobody could sign
  in); running without sign-in is the local development mode, which has no
  sign-in configuration at all.
- Email and domain matchers compare ASCII only; an IDN domain is written in
  its A-label form.
- `docs/layout.md` forbids adapters from importing `core`. The OIDC adapter
  will therefore only fetch and post; the application checks the discovery
  document, the endpoints and the claims above the port.
- Derived from neorc's code, written again for this layout; recorded in
  `docs/legal/ip-clearance.md`.

### Step 3 — signin-application   (feature/poc-3-signin-application)

Summary: the sign-in flow, proven with fakes and no IO. Ports: `Clock`,
`SecretSource`, `CredentialStore`, `IdentityProvider` (which only fetches
and posts). Application: `SignIn` — begin, complete (the callback), resolve
a session, sign out, sweep; discovery is lazy, single-flight, cached on
success only, and validated above the port. Pure pieces live in `core`
(`oidc.py`, `hashing.py`, `urls.py`). Fakes for every port under
`backend/tests/fakes/`, and two reusable contract suites under
`backend/tests/contracts/` that the real store and secret source must pass.
No HTTP, no database, no cookies or routes yet.

Review: 3 rounds.
- High: 1
  - The store contract suite had no concurrency test, so a store breaking
    every atomicity guarantee of the port (double take, cap bypass, two
    users for one person) passed it — fixed: `asyncio.gather` tests, and a
    deliberately racy store kept in the tests that they must fail against.
- Medium: 7 (6/1)
- Low: 21 (21/0)

Checks: `scripts/check-all.sh` — 815 tests with the architecture contracts,
lint, licences, audit, reuse, DCO — all pass; the tests are also clean
under `PYTHONASYNCIODEBUG=1 -W error`.
Not done / to watch: the medium left is the global cap on pending sign-ins
as a denial-of-service lever — no code fixes it without a rate limit; it is
now in the known limits of `docs/specs/sign-in.md`. The sweep's "tick read
after the deletes" has no test (the fake clock does not move during a
sweep). About 3,400 lines with tests, over the aim.
Important design decisions made / open questions:
- One clock: the application computes every deadline from the `Clock` port
  and tells the stores what "now" is; stores keep no clock.
  `docs/specs/legacy/backend.md` was changed to say so (it said the database
  clock).
- `CredentialStore` must be safe under concurrent calls: the take is one
  `DELETE … RETURNING`, the cap holds exactly (an advisory lock or
  SERIALIZABLE with retry), get-or-create is one `INSERT … ON CONFLICT`.
  A duplicate `state` hash is refused, not replaced.
- A contract subclass provides `new_store`, `close_store` and `dump`.
- The application wraps only `OSError` / `TimeoutError` from the identity
  provider port as `provider_unavailable`; an adapter's bug propagates.
- The application may hold in-process state only for caches of public
  data, scheduling hints and diagnostic counters (`docs/layout.md`).
- Derived from neorc; recorded in `docs/legal/ip-clearance.md`.

### Step 4 — credential-store   (feature/poc-4-credential-store)

Summary: the first real adapter. `datastore/schema.sql` — one idempotent
definition with a version row written last, named constraints, `timestamptz`
everywhere, CHECKs that a key is a SHA-256 — shipped in the wheel.
`datastore/schema.py`: `create_schema` (apply on an empty schema, no-op on
this version whole, refuse anything else and leave it untouched),
`check_schema`, `schema_version`, all pinned to `current_schema()`.
`PostgresCredentialStore` over a pool it is given, passing the whole
contract suite including the concurrency tests. `asyncpg` is the one new
dependency (Apache-2.0, brings nothing). CI runs the tests against a
PostgreSQL service pinned by digest. No CLI, no conversations or runs
tables, no wiring yet.

Review: 3 rounds.
- High: 2
  - `create_schema` stamped the current version onto an old schema, so the
    start-up check passed on a database it must refuse — fixed: the SQL
    never overwrites a version, `create_schema` looks before it writes, and
    a pinned SHA-256 of `schema.sql` fails a test when the file changes
    without the version and the hash being updated.
  - The schema checks judged the whole search path while the file writes
    only to its first schema: a foreign schema further along passed the
    check, and another application's tables were named as ours — fixed:
    every lookup is pinned to `current_schema()`, real tables only, and a
    relation the path reaches first is refused as shadowing.
- Medium: 6 (6/0)
- Low: 15 (15/0)

Checks: `scripts/check-all.sh` without a database (873 passed, 64 skipped)
and with one, `ROBINAUTS_REQUIRE_POSTGRES=1` (935 passed, 2 skipped); no
flakes over repeated and parallel runs; shown by mutation that removing
either advisory lock, the atomic take or the upsert fails the contract.
Not done / to watch: the CI service container and the wait script have not
run on GitHub before this push. About 3,000 lines with tests, over the aim.
Important design decisions made / open questions:
- The schema is edited in place (no migrations): any edit of `schema.sql`
  must update `SCHEMA_SHA256`, and bump `SCHEMA_VERSION` once anything is
  deployed; `robinauts db init` (a later step) works on an empty schema
  only.
- The connection's search path must start with the schema the tables live
  in; `check_schema` verifies that the names resolve there.
- The cap on pending sign-ins is held by a transaction-scoped advisory
  lock; `create_schema` takes another, keyed on the schema.
- Exactly four refusals are translated from constraint violations to
  `InvalidValueError`, by constraint NAME; the names in `schema.sql` are
  interface. Every other driver error propagates.
- Tests take `ROBINAUTS_TEST_DATABASE_URL`; without it they skip; with
  `ROBINAUTS_REQUIRE_POSTGRES=1` (CI) a skipped or deselected database
  suite fails the run. Each test works in a schema of its own.

### Step 5 — oidc-adapter   (feature/poc-5-oidc-adapter)

Summary: the HTTP side of sign-in. `adapters/`: `HttpIdentityProvider` on
`httpx` (it builds and owns its client: no redirects, TLS that cannot be
weakened, connect/read/total timeouts, an uncompressed raw-byte response
bound, client authentication by basic or post, and an exchange from which
no credential can be reached), `SystemClock`, `OsSecretSource`, the TOML
reader `read_toml` (raw data; `core` validates) and `check_client_secrets`.
`tests/standin/`: a real loopback identity provider that checks client
authentication, the code, the redirect URI and PKCE, and can be scripted to
misbehave. An end-to-end test drives `SignIn` through the real adapter. No
routes, cookies, wiring or CLI yet.

Review: 2 rounds.
- High: 1
  - A deeply nested JSON answer raised `RecursionError`, escaping the
    port's contract with a traceback whose frames held the client secret,
    the code and the verifier — fixed: caught on both endpoints, and the
    exchange restructured so that nothing reachable from an exception it
    raises (frames, `__cause__`, `__context__`) holds a credential,
    cancellation included.
- Medium: 5 (5/0)
- Low: 6 (6/0)

Checks: `scripts/check-all.sh` without a database (1007 passed, 64
skipped) and with one required (1069 passed, 2 skipped); the step's io
tests three times over, no flakes, no sleeps; hardening probed by a
reviewer with raw sockets (slow drip, no headers, lying lengths, encodings,
redirects, pool after 30 timeouts and 30 cancellations).
Not done / to watch: no HTTPS stand-in, so "TLS verification is on" is
proved by inspecting the SSL context and the absence of any parameter to
weaken it. About 3,300 lines with tests and the stand-in, over the aim.
Important design decisions made / open questions:
- `httpx` brought `certifi` (MPL-2.0): the first runtime row of the
  restricted table in `DEPENDENCIES.md` (unmodified, installed by the
  package manager, never inside the wheel).
- The library-confinement contracts (`httpx`, `asyncpg`, the agent
  frameworks) and the `api` contract are about DIRECT imports
  (`allow_indirect_imports`): infrastructure may import the package that
  holds a library, never the library.
- Token endpoint statuses: 408/425/429 and 5xx are `provider_unavailable`;
  other 4xx, and an OAuth `error` in a 200, are `provider_refused`.
- The client secret is looked up by variable name at the moment of the
  exchange and never kept.
- Configuration has no port: an adapter reads raw tables, `core` validates,
  the composition root calls both and must merge every start-up problem it
  can gather into one `ConfigError`.
- Derived from neorc; recorded in `docs/legal/ip-clearance.md`.

### Step 6 — auth-api   (feature/poc-6-auth-api)

Summary: the web layer of sign-in. `robinauts.api` on FastAPI: the four
auth routes and `/health`; cookies (`__Host-` on https, plain on loopback
http); request protection as ASGI middleware that reads the raw headers
itself; the guard and the per-route declaration `public()` /
`signed_in()`, checked by a test and again when the application is built
and when it starts; fixed error bodies; one helper that escapes everything
request-derived before it is logged. `robinauts.app`: `Deployment` and
`create_app`, merging every start-up problem into one `ConfigError`,
opening and closing the pool and the adapters in the ASGI lifespan, every
collaborator injectable. `backend/openapi.json` is committed and a test
keeps it in step. No CLI, no UI serving, no development mode yet.

Review: 3 rounds.
- High: 2
  - The "every route declares a permission" check saw only `APIRoute`s: a
    mounted application, a plain Starlette route and a websocket were
    served while it stayed green — fixed: the walk fails closed on anything
    it does not recognise, recurses into mounts, and runs at build and at
    start-up so such an application does not start.
  - The same check did not see FastAPI's `frontend()` routes, kept in a
    private list — fixed: every route list is read, a backstop refuses any
    route-bearing attribute it does not know, and the FastAPI range is
    pinned to the version whose internals were read.
- Medium: 8 (8/0)
- Low: 13 (13/0)

Checks: `scripts/check-all.sh` without a database (1171 passed, 67
skipped) and with one required (1236 passed, 2 skipped); the step's tests
three times over under `-W error`, no flakes. Reviewers drove the real ASGI
application with raw scopes.
Not done / to watch: `uvicorn` is not a dependency yet; it arrives with
`robinauts start`. That step must start the server with the access log off
(or query strings stripped for `/auth/callback`): an access log would
write `code` and `state`. About 4,500 lines with tests, over the aim.
Important design decisions made / open questions:
- To serve the UI, a later step adds one line, `"/ui"`, to
  `api.FRAMEWORK_PATHS`; the walk refuses it otherwise. A permission is
  declared on the route itself, not on `include_router`. Routes are never
  added after start-up.
- `fastapi>=0.141,<0.142`: the route walk reads FastAPI internals; bumping
  the range means re-reading `fastapi.routing.APIRouter`
  (`ROUTE_LISTS`, `READ_FASTAPI`).
- A state-changing request is accepted only with `Content-Type:
  application/json`, `Sec-Fetch-Site` absent or exactly `same-origin` /
  `none`, no duplicated deciding header in any casing, and — with a session
  cookie — `Origin` equal to `public_url` (or, with no `Origin`,
  `Sec-Fetch-Site: same-origin`). Websocket and unknown scopes are refused.
- Error bodies never repeat the request; sign-in errors answer one fixed
  sentence per code; no 5xx says why. Navigation routes answer every
  failure with a redirect to the sign-in page.
- `GET /auth/login` changes state by design; the consequence is in the
  known limits of `docs/specs/sign-in.md`.
- The guard (`api.access.current_user`) is the seam for the development
  mode.
- Derived from neorc; recorded in `docs/legal/ip-clearance.md`.

### Step 7 — dev-mode   (feature/poc-7-dev-mode)

Summary: the local development mode — no sign-in, one fixed local user who
is a real row, loopback only. `domain/local.py` (`LocalMode`, the reserved
key `("!local", "developer")`, the loopback predicates),
`application/local.py` (`LocalAccess`), the guard resolving every request
to that user, the protection refusing anything not addressed to this
machine, `GET /auth/session` saying `local_development: true`, and
`create_app(local_development_host=…)` as the only way to ask for it. The
CLI flag and the banner come with later steps.

Review: 1 round.
- High: 0
- Medium: 1 (1/0)
- Low: 3 (3/0)

Checks: `scripts/check-all.sh` without a database (1265 passed, 70
skipped) and with one required (1333 passed, 2 skipped). The reviewer tried
and failed to switch the mode on unasked, to combine it with sign-in, and
to get past the loopback and origin rules (trick host spellings, other
loopback ports, `null` origins, DNS rebinding shapes).
Not done / to watch: "the local user owns what it creates" can only be
shown once conversations exist. About 1,700 lines with tests, over the aim.
Important design decisions made / open questions:
- No environment variable can switch the mode on: only the explicit
  argument, which is also the bind host. Asking for it together with a
  sign-in configuration, or for neither, is a start-up `ConfigError`.
- The bind host must be a literal loopback address or exactly `localhost`
  (`domain.is_loopback_bind_host`), stored normalised; a request's `Host`
  is judged by the looser `is_loopback` (which moved to `domain`, still
  exported by `core`). Every request, reads included, needs one loopback
  `Host` and a server that answered on loopback (a unix socket counts; an
  absent `server` is refused).
- Every write in the mode is judged as credentialed: `Origin` equal to the
  host the request was addressed to (port and scheme matter), or
  `Sec-Fetch-Site: same-origin` with no `Origin`. A client that sends
  neither (curl) is refused.
- The reserved provider id `!local` cannot be configured (`ProviderConfig`
  now holds ids to their shape) and `SignIn.resolve_session` never answers
  the local user, so nothing reaches that account in normal mode.
- `CredentialStore.user_by_key` reads a user without writing; the local
  user is read per request and written once.

### Step 8 — conversation-format   (feature/poc-8-conversation-format)

Summary: the platform's own conversation format, pure, standard library
only. `domain`: `Message` (parts, provenance, channel), `Conversation`,
`Run`, `RunEvent`, the engine's events (no ids) and the platform's turn
events, the value checks and the text helpers (`clean_text`, `text_parts`,
`kept_parts`, `publishable` / `flush`), `reading_stored`. `core`: the two
canonical, versioned, strict documents (a message, a run event) with their
upgrade registries and the reserved `extras`; the stored readers;
`ConversationTree`; titles; trimming by whole turns; the run state machine
with `run_error`; the order checks `check_engine_events` and
`check_event_order`; `resume_point`. No ports, stores, application,
engines or wire yet — they are built on this.

Review: 8 rounds.
- High: 17, all fixed. The ones that shaped the contract:
  - A conversation that exists but is not yours could be told apart from
    one that does not exist — one identical body for the whole not-found
    family.
  - Exact-version matching would have made the first version bump a data
    migration — a build reads every version up to its own through
    upgraders; adding a kind, a role or data under `extras` never moves the
    version.
  - The role rule made tools impossible — a turn is a chain: a user message,
    then assistant (and later tool) messages.
  - `clean_text` destroyed a character split across two deltas, and later a
    NUL between the halves still broke it — one function, `publishable`,
    with a property test: what is published is what is stored.
  - Stored-data faults answered 422 with internal ids, then were logged
    without their cause — `StoredDataError`, a 500 that says nothing, with
    the escaped cause chain and stack frames in the log.
  - An engine could not yield "a message as it was stored" — two event
    vocabularies: the engine's (no ids) and the platform's.
  - Non-streaming answers were illegal; a reasoning-only answer left a
    message with no parts; a turn with no answer could end `finished`.
  - One upgrade registry served two document shapes; then the parts
    document was dropped: exactly two versioned documents exist.
  - The read path lived in `core` with the datastore as its caller, against
    the layering contract — documents cross the store ports, flat records
    are built in stores inside `domain.reading_stored`, the application
    decodes.
  - Run events had no canonical encoding.
- Medium: 39 (39/0)
- Low: 49 (48/1)

Checks: `scripts/check-all.sh`, with the database required: 1934 passed, 2
skipped; stable over repeated and randomised runs, no test over a second.
Reviewers property-tested `publishable` (100,000 sequences), the two order
checks end to end (4,000 answers at every re-attach prefix), and the
encoding (30,000 encode/mutate/decode rounds).
Not done / to watch: the one low left is `Run` recording no owning process
or heartbeat — single process is the POC's scope (`docs/specs/legacy/runs.md`
describes the multi-process rules). About 6,500 lines with tests: far over
the aim, and eight review rounds — this step should have been two (records
and encoding; tree, runs and events).
Important design decisions made / open questions:
- Messages and run events cross the store ports as DOCUMENTS
  (`core.message_to_data`, `core.run_event_to_data`), with the record
  beside them for a store's indexed columns; conversations and runs cross
  as records. Stores never import core.
- Completed messages are appended when complete; a message in flight lives
  in its run's numbered events. Opening a conversation with an active run
  returns the run id and `resume_point` (`after`, `follows`); attaching
  there replays exactly the message in flight.
- An engine yields `AnswerStarted`, `AnswerTextDelta`,
  `AnswerReasoningDelta`, `AnswerCompleted(parts)`; it reports failure by
  raising and is cancelled by cancelling its task. Text deltas are
  optional; if any were streamed they join to the completed text.
  Reasoning is streamed and not stored in this version.
- The application publishes through `publishable` / `flush`, stores
  `kept_parts(...)`, ends runs through `transition` and `run_error`, and
  assigns event positions; whoever marks a run `interrupted` appends its
  `RunEnded`.
- Bounds: 1,000,000 characters a part, 64 parts, 120 a title, 64 KiB of
  `extras`, positions up to 2^53 − 1, years 1970–9998.
- A turn request names the conversation and either a new user message with
  its parent, or the assistant message whose turn is produced again
  (`docs/specs/wire.md`).

### Step 9 — conversations-application   (feature/poc-9-conversations-application)

Summary: ONE `ConversationStore` port owning conversations, messages, runs
and run events — compound atomic writes (`start_run`, `complete_message`,
`end_run`, `delete_conversation`), a one-moment read
(`conversation_snapshot`), the other reads and small writes, a stated
division of duties and a table of every refusal with its error — and the
`IdSource` port with `OsIdSource`. An in-memory fake; a contract suite over
two modules with concurrency tests, and a racy store (the fake minus its
lock) that must fail exactly the named `NOT_ATOMIC` set. The
`application.Conversations` service: list, open (from the snapshot),
rename, select branch, delete, ownership. No PostgreSQL store, no turn
lifecycle, no executor yet.

Review: 4 rounds.
- High: 4
  - Deleting made two calls to two stores, so a run starting between them
    was orphaned for ever with its answer text — fixed by merging the ports
    into one store whose compound operations are single transactions.
  - `open()` read messages before events, so an answer completed in between
    was neither in the tree nor replayed — fixed: one snapshot read.
  - Nothing refused writes into a run that had already ended, leaving a
    stream nothing could read back — fixed: refused in the same step as the
    write.
  - The snapshot was not certified: a store doing four separate reads
    passed the whole suite — fixed: the fake yields between its reads, and
    two interleaving tests joined `NOT_ATOMIC`.
- Medium: 17 (17/0)
- Low: 26 (26/0)

Checks: `scripts/check-all.sh` without a database (1992 passed, 70
skipped) and with one required; the step's unit modules 30 times over, no
flakes. A reviewer built five plausible-but-wrong stores and the suite
caught each, and found no contract test a correct asyncpg store cannot
pass.
Not done / to watch: about 5,000 lines with tests, over the aim. Against a
real database the interleaving tests are attempts at a race, not a proof.
Important design decisions made / open questions:
- One store, one transaction per method. `start_run(conversation?,
  message?, run)` begins every kind of turn (new chat, continue or edit,
  regenerate); `complete_message` stores an answer and its
  `MessageCompleted` together; `end_run` the ended record and its
  `RunEnded`; `delete_conversation` refuses, inside its transaction, while
  a run is active.
- The store checks what its columns and the domain records show
  (existence, belonging, roles, states, positions, that an event is of the
  kind its method is for); that a document says what its record says, and
  a stream's order as a whole, are the application's
  (`core.check_event_order`). When several refusals apply, which is raised
  is unspecified; a refused call writes nothing.
- A run answers a USER message of its conversation, with the
  conversation's agent, and begins active; positions are consecutive and
  the application is a run's single writer; an ended run takes nothing
  more.
- Message ids are unique across the deployment. Listing is a keyset over
  `(updated_at, id)` with an opaque, unsigned cursor inside the caller's
  own listing; an item written to during paging may be missed or repeated.
- Moving between branches does not date the conversation; completing a
  message does, and moves the author onto it.
- "Not yours" and "not there" are the same `ConversationNotFoundError`.
- For the next step: opening should also surface the most recent run's
  state and error when it ended badly (`runs_of`).

### Step 10 — run-lifecycle   (feature/poc-10-run-lifecycle)

Summary: a turn, from a request to a stored answer. `AgentDefinition`; the
`Agent` port (an async generator of engine events) with `AgentContract`
and a scriptable fake; `application.Turns` — `start` / `regenerate` (the
three request shapes, one `start_run` each), `claim` / `let_go`,
`execute(run)`, `cancel`, `sweep_interrupted`. In `execute` a child task
reads the engine into a bounded queue; the lifecycle publishes numbered
events through the store, stores each completed answer with its event, and
ends the run through one shielded, bounded, retried routine.
`Conversations.open` reports a last run that ended badly. The pure log
helpers moved to `domain/logs.py`. No executor, no PostgreSQL store, no
routes, no real engines yet.

Review: 3 rounds.
- High: 4
  - A write that committed while its await was interrupted left the
    position one behind, and the ending then mistook its own gap for
    "somebody ended it" — the run stayed `running` for ever (reproduced by
    the turn timeout alone).
  - Only the cancelled ending was shielded; a cancel during any other
    ending left the run `running` with its answer stored.
  - With a second writer (cancel with no task, the sweep), re-offering a
    refused event stored two `RunStarted` and made the stream unreadable.
  - Exhausted write attempts were read as "somebody ended it first".
  All fixed by two rules: a refused or unknown-outcome write is settled by
  reading what is stored at the offered position (never re-offered
  blindly), and every ending goes through one shielded routine that
  re-plans from the store, with a timeout per attempt.
- Medium: 9 (9/0)
- Low: 15 (15/0)

Checks: `scripts/check-all.sh` with the database required: all green; this
step's modules 30 times under `-X dev -W error`, no flakes. Reviewers
fuzzed about 600 store-fault and engine-fault scenarios: one `RunEnded`,
gap-free positions, a readable stream, at most one message per answer,
deltas joining to the stored text, nothing escaping but cancellation.
Not done / to watch: about 4,500 lines with tests, over the aim. A run
whose ending cannot be written (store unreachable or silent) stays
`running` until the start-up sweep of the next restart — logged at ERROR;
a run's events are kept until its conversation is deleted; agent
definitions are fixed at start-up (all three in the specs' known limits).
Important design decisions made / open questions:
- The `Agent` port: `run_turn(definition, history)` — the history is the
  trimmed path ending in the user message being answered; failure by
  raising; release by closing the generator (bounded by the application).
- The scheduler of step 12 calls `Turns.claim(run_id)` synchronously
  BEFORE creating the task for `execute(run)`; the sweep skips claimed
  runs; `cancel` cancels a known task, otherwise ends the run in the store.
- Reasoning is kept in the run's events only (needed to re-attach), never
  in a message.
- An unknown agent is a not-found; a turn with no answer, an answer left
  announced, or an answer that does not match what was published is a
  failed run.
- The application logs failures as one escaped, bounded record
  (`domain.chain` / `domain.where`), never `exc_info`.

### Step 11 — conversation-stores   (feature/poc-11-conversation-stores)

Summary: the PostgreSQL `ConversationStore`. Four tables appended to
`schema.sql` (schema version 2): `conversations`, `messages`, `runs`,
`run_events`, every constraint named; a partial unique index for one active
run per conversation, `(run_id, seq)` as the events' key, a partial unique
index for one `RunEnded` per run. `datastore/conversations.py`: one
transaction per method, row locks in the order conversation then run,
constraint violations translated by name, deadlock and serialization
failures retried and logged, a read-only `REPEATABLE READ` snapshot,
documents stored as jsonb exactly as given. `app.Deployment` builds it on
the shared pool. It passes the whole contract suite of step 9 against a
warm pool.

Review: 1 round.
- High: 1
  - `schema.sql` was edited without bumping `SCHEMA_VERSION` (the driver's
    own instruction, and wrong): an existing version-1 database got a
    self-contradictory refusal — fixed: version 2; every edit of the file
    bumps the version, even before anything is deployed.
- Medium: 1 (1/0)
- Low: 4 (4/0)

Checks: `scripts/check-all.sh` without a database (2106 passed, 186
skipped) and with one required (2286 passed, 6 skipped); the integration
module repeatedly and in two parallel sessions, no flakes, no schema left
behind. A reviewer made about 7,000 concurrent calls against the real
database: no invariant violated, no torn snapshot, no raw driver error on a
reachable refusal, no deadlock, documents and step 10's settle-from-the-
store comparison surviving jsonb exactly.
Not done / to watch: about 2,700 lines with tests, over the aim. The
deadlock probe guards the lock order rather than proving it (no two
methods can currently hold the rows crosswise); the retry loop is tested
with a real, forced deadlock.
Important design decisions made / open questions:
- Every edit of `schema.sql` bumps `SCHEMA_VERSION` and re-pins the hash;
  a database of another version is made again (no migrations yet).
- Deleting a user cascades to their conversations, messages, runs and
  events.
- `active_leaf_id` has no foreign key (it would make a cycle); the store
  checks it inside its transaction.
- Appending an event costs the same however long the run is: the position
  is one backward index read, and "has it ended" is the run row already
  held.
- An open deployment always has both stores, or fails to open.


### Step 12 — run-executor   (feature/poc-12-run-executor)

Summary: a turn now runs in the background and can be followed. Two ports:
`RunExecutor` (`submit` with a coroutine factory and a `never_began` report,
`cancel`, `running`, `aclose(timeout=)` as one deadline for the whole stop)
and `RunSignals` (`announce`, bounded `changed`; carries no data, so a lost
signal costs a wait and never an event). Adapters `AsyncioRunExecutor`
(tasks on the serving loop, strong registry, every exception logged once
through `domain.chain`/`where`) and `MemoryRunSignals` (bounded memory:
`REMEMBERED_RUNS` 10,000 / `REMEMBERED_SECONDS` 60, watched records kept
apart from the sweep). `Turns` gains `begin`/`begin_again`, `cancel` through
the executor, `stopping()` so a shutdown ends runs `interrupted`, and the
never-began ending that respects the shutdown's deadline. `application/
watch.py`: `Watch.events` replays the store, waits bounded on the signals,
re-reads the run after every wait and raises `RunQuietError` (504, own body)
on a silent active run. `app.Deployment` wires them, sweeps at start-up and
stops in order (stopping → executor → pool), with `SHUTDOWN_SECONDS` added
up from `application.ENDING_BUDGET_SECONDS` plus slack.

Review: 3 rounds (two full, one focused). The implementer died three times
on an Anthropic outage between rounds two and three; a fresh implementer
finished from the tree state (a deviation from "continue the same
implementer", recorded here).
- High: 4
  - `aclose`'s deadline did not bound the never-began reports: the ending
    shields its write and swallows the cancel, so a stop took N × 30 s —
    fixed: the report is told the deadline, a write still going is left to
    land with a log line, measured 8 slow runs → `aclose(0.2)` in 0.20 s.
  - A watcher could miss the run's ending between the replay and the first
    wait (round one) — fixed: the run is re-read after every wait.
  - The signals' memory was unbounded across many runs (round one) —
    fixed: count and age bounds on the forgettable set.
  - Cancelling work that had not begun wrote no ending and left the run
    active until a restart (round two) — fixed: the `never_began` report.
- Medium: 7 (7/0)
- Low: 14 (14/0)

Checks: `scripts/check-all.sh` without a database (2172 passed, 188
skipped) and with one required (2354 passed, 6 skipped); this step's
modules 20× under `python -X dev -W error` in random order, no warnings, no
flakes. Reviewers drove 40×400 randomised interleavings of watchers and
announcements, and measured the sweep at 5,000 parked watchers (1.66 µs per
announce, a factor of 1.09 over none).
Not done / to watch: about 2,900 lines with tests, well over the aim — the
watcher (`watch.py`) could have been its own step. `MemoryRunSignals` is
per process; a second process polls (the `LISTEN`/`NOTIFY` adapter is for
later). The adapter's own `DEFAULT_SHUTDOWN_SECONDS` (30) is not what the
deployment uses.
Important design decisions made / open questions:
- The shutdown's bound wins over an ending's: a shielded write is never
  interrupted, but nobody waits for it past the deadline; the start-up sweep
  is the fallback for what did not land.
- Signals carry positions only; the store is the one source of events.
- `wait_seconds <= quiet_seconds` is enforced, and the root passes
  `min(DEFAULT_WAIT_SECONDS, turn_seconds)`, so a short turn timeout gives
  up on a silent run after itself.
- `RunQuietError` is a 504 with its own body, logged once at WARNING: it is
  a run that stored nothing, not an internal error.

### Step 13 — conversations-api   (feature/poc-13-conversations-api)

Summary: the plain JSON routes, described by OpenAPI. `api/
conversation_routes.py`: list (paged, `limit` bounded in the document,
opaque cursor), open (the whole tree with `parent_id`s and `leaf_id`,
`run_id` + `resume` while a run is in flight, `ended_badly` when the last
one went wrong), rename, select branch, delete, cancel a run;
`api/agent_routes.py`: the configured agents (id, title, engine — never
the prompt or the model). Every route goes through the application for
ownership, and everything that is not there or not the caller's answers
one identical body, status and headers. `api/refusals.py` holds the
declared statuses, with a `default` entry for 400/405/500 in the same
shape. `errors.py` renders every pydantic refusal as `<location>: <a
sentence of ours>` — never pydantic's message, never a key, offset or
character from the request — and builds a correct `Allow` for a 405 over
included routers. `protection.read_once` refuses a JSON body that gives a
field twice at any depth; `given_once` does the same for the query string.
Response fields that are always sent are required-nullable; wire enums
declare only the values that are sent. Snapshot regenerated. Beginning a
turn and the stream are step 14.

Review: 3 rounds (two full, one focused), no high finding in any.
- High: 0
- Medium: 4 (4/0) — the delete docstring contradicted its 404; unknown
  body-field names were reflected into 422s; opening a stranger's
  conversation read the whole tree before the 404 (a timing oracle); a
  body nested near the C recursion limit turned the strict re-parse into a
  500.
- Low: 21 (20/1) — HEAD is not served by the API routes (405); left for
  the frontend-serving step.

Checks: `scripts/check-all.sh` without a database (2222 passed, 189
skipped) and with one required (2405 passed, 6 skipped); the changed
modules 10× under `python -X dev -W error`. Reviewers sent ~90 crafted
requests (nested/duplicated/huge/unicode/surrogate/NUL bodies, bad uuids,
foreign cursors, cross-site writes) with no reflection and no state
change.
Not done / to watch: about 2,500 lines of diff, ~1,300 of them the
generated snapshot; the route tests alone are 1,257 lines (45 tests). No
request-body size limit yet, and a write body is now parsed twice — the
bound belongs to the operations limits, in front of both parses. `HEAD` on
GET routes answers 405. `redirect_slashes=False` is application-wide, so
the frontend step serves `/ui` explicitly. The implementer's scripted edit
once truncated the test module (a lone surrogate in `write_text`); it was
rebuilt in full.
Important design decisions made / open questions:
- Bad paging values are 422 (the existing `InvalidValueError` mapping),
  not 400.
- The open response is the whole tree plus `leaf_id`; the client walks
  parents for the branch. `ended_badly` carries no error text (that is
  the operator's, in the log).
- `Turns.cancel` takes the conversation id and treats a mismatch as
  not-found, beside ownership.
- `InvalidCursorError` in `domain`, raised by both stores, is the only
  refusal the list route labels `query.cursor`.
- A blank title is refused by the application; the length bound is in the
  document.

### Step 14 — agui-stream   (feature/poc-14-agui-stream)

Summary: the AG-UI stream over server-sent events, emitted by `api`.
`ag-ui-protocol` 1.0.0 (MIT) is the one new dependency, confined to `api`
by an import-linter contract. `api/agui.py`: one mapper from the platform's
turn events to AG-UI 1.0 — `RUN_STARTED`, `TEXT_MESSAGE_START/CONTENT/END`,
`REASONING_MESSAGE_*` for thinking (one reasoning message per stretch, id
derived from the message and the position so it is stable across a
re-attach), `RUN_FINISHED` (with the cancelled outcome for a cancelled
run), `RUN_ERROR` with a fixed sentence per state and never the stored
error. `api/stream_routes.py`: `POST /api/turns` (new conversation),
`POST /api/conversations/{id}/turns` (a message under a parent, or a
regeneration — exactly one form), `GET /api/runs/{id}/events?after=` with
`Last-Event-ID`; SSE `id:` is the platform position on the last wire event
derived from each stored event; heartbeats; ownership settled before any
byte; every stream ends with a terminal event (`quiet`, `gone`, `internal`
or how the run really ended); a re-attach seeds the mapper from the stored
prefix (`Watch.before`, `events_of(upto=)`) so an open thinking block is
closed. A request-body cap (1 MiB, 413) now sits in the protection
middleware in front of the framework's read. The streaming endpoints are
outside the OpenAPI snapshot and documented in `wire.md`.

Review: 4 rounds (two full, two focused).
- High: 2
  - the synthetic reasoning bracket events shared an `id:` with a real
    event, so a re-attach at that position silently lost a delta — fixed:
    only the last wire event derived from a stored event carries the
    position;
  - a re-attach right after the last reasoning delta never closed the
    client's thinking block (a fresh mapper knew nothing) — fixed: the
    mapper is seeded from the stored prefix.
- Medium: 8 (8/0) — among them: a huge `Last-Event-ID` was a 500; a
  deletion between the two ownership checks was logged as a fault;
  cancelled was sent as an error; no body cap, then the cap only after the
  framework had buffered a chunked body; the bounded receive could block
  after the client had finished sending.
- Low: 23 (23/0)

Checks: `scripts/check-all.sh` without a database (2314 passed, 191
skipped) and with one required (2499 passed, 6 skipped); the changed
modules 10× under `python -X dev -W error`. Reviewers drove the re-attach
guarantee at every cut point over finished/cancelled/failed/interrupted
runs with both re-attach forms, and the body cap at the ASGI level.
Not done / to watch: about 2,900 lines with tests, well over the aim —
the mapping, the routes and the body cap were three concerns. `HEAD` on
GET routes still answers 405. A re-attach reads the run's whole prefix
(O(run length)); fine for now, the bounded read from the last
`MessageStarted` is the improvement. No `REASONING_START/END` span (AG-UI
makes it optional). Three ownership checks per re-attach.
Important design decisions made / open questions:
- The wire is a profile of AG-UI: the server loads history; the request
  names the conversation and a message under a parent, or a regeneration.
- `id:` marks "everything derived up to this position has been sent";
  events without an id are derived or terminal, never a place to re-attach.
- Documented client no-ops: a `*_START` for an open id, a `*_END` for an
  id not open, a repeated terminal event.
- `interrupted` (the process went away) stays `RUN_ERROR`; it is not
  AG-UI's interrupt outcome.
- A position past what an active run has stored is 422; past the end of
  an ended run answers the run's outcome.

### Step 15 — first-engine   (feature/poc-15-first-engine)

Summary: the LangGraph engine and the model configuration. Dependencies:
`langgraph`, `langchain-core`, `langchain-anthropic`, `langsmith` (imported
for the one call that turns tracing off) — the whole tree passes the
licence gate; `orjson` (MPL-2.0, unmodified, unbundled) is rowed as a
restricted dependency. `[model_providers.*]`, `[models.*]`, `[agents.*]`
in the one TOML (`ROBINAUTS_CONFIG`; `ROBINAUTS_AUTH_CONFIG` kept as a
deprecated alias): domain records, `core.parse_models_config` collecting
every problem at once, `adapters.check_api_keys` naming every unset
variable, `ProviderKeys` that never prints a key. `LangGraphAgent`: a
compiled `StateGraph` per turn with no checkpointer, streamed through
`content_blocks` (text and reasoning, vendor-neutral), `AnswerCompleted`
built from what was streamed, tool calls refused (no tools yet), the
endpoint/key/proxy/headers pinned so no `ANTHROPIC_*` variable can move a
request or change its credential, LangSmith tracing forced off process-wide
and the legacy variables removed. `app.py` wires `ENGINES` (the port's
`kinds` decide which provider kinds a build reaches); the local
development mode reads the model tables of the file and refuses sign-in
tables. The contract suite runs over the real engine with a scripted chat
model; a live test exists outside CI.

Review: 2 rounds.
- High: 1
  - `ChatAnthropic` was built without a base URL, so `ANTHROPIC_BASE_URL`
    in the environment redirected every call — and the key — to any host;
    fixed: endpoint, key, proxy and headers pinned, tested against ~25
    variables with sockets blocked.
- Medium: 4 (4/0) — the legacy `LANGCHAIN_TRACING` variable made every
  turn fail once v2 tracing was forced off; the discard test no longer
  held; a URL with embedded credentials was accepted in the configuration;
  the framework-confinement contracts left the adapter package's own
  modules uncovered (now structural, with probe tests over a copy).
- Low: 22 (22/0)

Checks: `scripts/check-all.sh` without a database (2473 passed, 195
skipped) and with one required (2662 passed, 6 skipped); the changed
modules 10× under `python -X dev -W error`; no network call to a provider
in any test.
Not done / to watch: **OpenAI / OpenAI-compatible (OpenRouter) is not in
this build**: `langchain-openai` needs `tiktoken` (metadata carries the
MIT text but no identifier) and `regex` (`Apache-2.0 AND CNRI-Python`, on
no list) — a policy decision, recorded under "Known exclusions" in
`DEPENDENCIES.md`; the configuration vocabulary keeps all three kinds and
a deployment naming one this build cannot reach is refused at start-up.
`system_prompt_file` not implemented. About 4,400 lines with tests and the
lock, far over the aim: the engine is small, the parser and its tests are
most of it. Three direct dependencies in one step (recorded as one change
in `DEPENDENCIES.md`).
Important design decisions made / open questions:
- The model tables live in the same file as sign-in, under
  `model_providers` (sign-in already owns `providers`).
- `MAX_RETRIES = 0` on the client: retrying is sending the message again.
- The contract suite declares per engine what a script can ask for
  (`answers_per_turn`, `can_answer_without_streaming`); every promise is
  still checked on every turn by `core.check_engine_events`.
- The adapter removes `LANGCHAIN_TRACING`, `LANGCHAIN_HANDLER` and
  `ANTHROPIC_CUSTOM_HEADERS` from the environment at construction.

### Step 16 — second-engine   (feature/poc-16-second-engine)

Summary: the Pydantic AI engine and the swap test. `pydantic-ai-slim
[anthropic]` (and `anthropic`, imported directly, as a direct dependency);
`logfire-api`/`opentelemetry-api` arrive transitively and are imported by
nothing. `PydanticAIAgent` mirrors the LangGraph engine: a framework agent
per turn with `instructions` (never in `message_history`), the stored
history as `message_history` with no separate prompt, node-level streaming
mapped to text and reasoning deltas, `AnswerCompleted` from the joined
deltas, one model call per turn (the loop breaks after the first model
node, so the framework cannot retry an empty answer), tool calls refused,
the vendor client built by the adapter with endpoint/key/headers pinned and
no retries, `instrument = False` per agent, the Logfire banner off. Both
adapters now also pin the vendor loggers that write request bodies, so no
`ANTHROPIC_LOG` or root-DEBUG can put a conversation in a log. The swap:
LangGraph → Pydantic AI → LangGraph over one store with real engines over
scripted models — each answer's `provenance.engine` is the engine that
produced it, each engine receives exactly the stored history (no
reasoning), and the stored document is identical but for
`provenance.engine`; plus the swap by configuration across a restart on
PostgreSQL. A third import contract confines the Anthropic SDK to the two
adapters; the architecture probe tests run over both frameworks.

Review: 2 rounds (one full, one focused).
- High: 1
  - the log-quieting guard of the previous round decided on the inherited
    level, so it pinned nothing in the ordinary deployment and a root
    logger turned up later leaked every conversation — fixed: the
    logger's own level, and the emitting child logger pinned too.
- Medium: 3 (3/0) — `ANTHROPIC_LOG=debug` leaked request bodies through
  the SDK's import-time logging (both engines); the documented discard
  test missed the shared swap fixtures; a level named on the child logger
  bypassed the parent pin.
- Low: 8 (8/0)

Checks: `scripts/check-all.sh` without a database (2527 passed, 196
skipped) and with one required (2717 passed, 6 skipped); the changed
modules 10× under `python -X dev -W error`; no provider reached in any
test.
Not done / to watch: both engines build a vendor client per turn and
nothing closes it but the wrapper's finaliser (a shared, closed client
lifetime is a change to both adapters and the root). The "nothing phones
home" guarantee for `pydantic_graph`'s spans rests on `logfire` being
absent from the lock (asserted by a test). A logger an operator names at
DEBUG in their own configuration after start-up is not fought. About
2,900 lines with tests; the engine and its tests are within ten lines of
the LangGraph pair. `DEPENDENCIES.md` has a duplicated paragraph from an
earlier step (to fix in passing).
Important design decisions made / open questions:
- `instructions`, not `system_prompt`: the prompt is taken from the
  definition at every run and never enters the stored history.
- One model call is the whole of a turn; an empty answer is an empty
  answer, never a retry.
- Both engines offer Anthropic alone in this build, so the swap holds
  for every model either has.

### Step 17 — frontend-skeleton   (feature/poc-17-frontend-skeleton)

Summary: `frontend/` — Vite 8, React 19, TypeScript strict, Vitest,
ESLint, Prettier; exact pins, `.npmrc` with `ignore-scripts` and
`save-exact`, `npm ci` only, every pin at least ten days old at adoption.
The seam of ADR 0001 as ESLint rules: `@assistant-ui/*` and
`assistant-cloud` and any path with a segment `assistant-ui` are refused
everywhere but under `src/chat/assistant-ui/` (and the seam file may reach
its own implementation only), for static imports, re-exports, `import()`,
template specifiers, `new URL(x, import.meta.url)`, `import.meta.glob/
resolve`; specifiers are written plainly (no `..` inside, no `//`, no
backslash, no `node_modules/`); `require` and Node globals are refused in
app code. Two licence gates: `frontend/scripts/check-licences.mjs` holds
every locked package to the policy read from `DEPENDENCIES.md` (SPDX
expressions, forbidden spellings shared with the Python gate through
`scripts/licence-fixtures.json`, unreadable claims refused, exceptions by
name, version and scope in a "JavaScript build tooling" table, restricted
licences development-only, exact pins), and `rollup-plugin-license` holds
the bundle to the allowed list and writes `bundled-packages.txt` (compared
in CI) and `THIRD_PARTY_LICENSES.txt`. A provisional size budget. CSS
Modules, other stylesheet languages and `<style>` in `index.html` are
refused by name. A typed API client over the generated OpenAPI types.
`scripts/check-frontend.sh`, the CI job, Dependabot npm with the cooldown,
the contributor rules in `docs/contributing/js-dependencies.md`, the
tooling shape carried over from neorc and recorded in `ip-clearance.md`.

Review: 10 rounds — the recipe's limit, reached; the user then decided to
drop the CSS scanner (below).
- High: 17 found over the ten rounds. Fixed: the client sent no content
  type on bodyless writes (every delete/cancel/sign-out would have been
  415); seven pins younger than the cooldown; `UNLICENSED`/`LicenseRef-*`
  and non-SPDX GPL spellings exceptable by a table row; a `..` in the
  middle of a path, and `new URL` false positives, in the seam rule.
  Dropped rather than fixed: the stylesheet scanner (a package reached
  through CSS), whose highs in rounds 4–10 never converged with Vite's own
  resolver (root-absolute and dot-less specifiers, `composes … from`,
  escaped quotes, `exports` conditions, extension-less imports, symlinks).
- Medium: about 30 (all fixed or removed with the scanner).
- Low: about 55 (all fixed or removed with the scanner).

Checks: `scripts/check-all.sh` without a database (2528 backend, 145
frontend tests) and with one required (2718); the bundle 68.6 kB gzipped
(react, react-dom, scheduler); `check-frontend.sh` end to end, including a
faked GPL licence failing the build and a stale bundled list failing CI.
Not done / to watch: **known limit** — the bundle record sees rollup's
module graph only; a package reached only through a stylesheet
(`@import "pkg"`, `url(pkg/x)`, a package stylesheet's own imports) is not
in `bundled-packages.txt`; it is still held to the allowed list by the
installed-tree gate, so nothing outside the policy can be installed, but a
development-scoped exception pulled into the bundle by CSS is not caught —
a CSS change naming a package is reviewed by hand (unticked in the
checklist). This step was two steps: the skeleton and the supply-chain
gates; ~4,100 lines excluding the lock. `gitleaks` still unticked.
Important design decisions made / open questions:
- The policy lists live in `DEPENDENCIES.md` alone; both gates read them.
- Dev-only npm exceptions are rows with version and scope in
  `DEPENDENCIES.md`; a restricted licence may be excepted only
  development-scoped; a forbidden or unreadable claim never.
- No CSS Modules, no stylesheet language but `.css`, no inline `<style>`.
- The provisional bundle budget is 800 KB gzipped until the chat exists.

### Step 18 — shell   (feature/poc-18-shell)

Summary: the shell on Tailwind 4 over neorc's design tokens (`tokens.css`,
light and dark from one `--dark-*` set, `prefers-color-scheme` and a
`data-theme` override; `@theme inline` maps utilities to the tokens);
`lucide-react` as the one icon package. `src/shell/`: the panel (collapse
button and brand, "New chat", an empty history, the profile block pinned
at the bottom with sign-out), the icon rail remembered per browser, the
phone drawer (`inert` when closed, `role="dialog"` + focus trap + scroll
lock when open, breakpoint read in `rem` so CSS and JS agree at any default
font size), the theme toggle (applied before first paint), the local-mode
banner, an empty chat with the agent picker over `GET /api/agents`.
`src/session/`: a `useSyncExternalStore` session store over
`GET /auth/session`, sign-out, a 401 anywhere flips to signed-out, the
sign-in page (one link per provider with a validated `return_to` mirroring
`core.safe_return_to`, fixed sentences per error code). The `ErrorBoundary`
outermost. A by-hand `CSS_PACKAGES` list in `vite.config.ts` puts a
CSS-imported package (Tailwind) into `bundled-packages.txt` and its notice
into `THIRD_PARTY_LICENSES.txt`. A fixture server for screenshots.

Review: 3 rounds (two full, one focused).
- High: 3
  - `#/sign-in?error=__proto__` blanked the page (an object lookup with
    inherited keys, carried over from neorc; the sign-in page was outside
    the boundary) — fixed: a `Map`, the boundary outermost;
  - the drawer's focus trap deadlocked on a CSS-hidden button in a real
    browser (jsdom loads no CSS, so the test was green) — fixed: stops are
    filtered to what is drawn, verified in Chromium;
  - the JS breakpoint (`768px`) disagreed with Tailwind's `48rem` at a
    non-default font size, leaving the off-screen panel tabbable — fixed:
    `48rem` in both.
- Medium: 4 (4/0) — Tailwind's MIT notice did not ship; the closed drawer
  was tabbable; Escape dropped focus; a sign-out whose re-read failed
  looked still signed in.
- Low: 18 (18/0)

Checks: `scripts/check-all.sh` without a database (2528 backend, 217
frontend tests) and with one required (2718); the bundle 76 kB gzipped;
thirteen screenshots (light/dark, rail, phone drawer, sign-in, error,
local banner, no agents) and the trap/breakpoint verified in Chromium.
Not done / to watch: the drawer and the theme toggle were "also out" in
the scope and are in (cheap, and specified) — the scope is corrected. No
`aria-modal` focus trap covers elements scrolled out of view or
`opacity: 0`. A new CSS `@import` of a package must be added to
`CSS_PACKAGES` by hand (the step 17 known limit). About 2,400 lines with
tests, over the aim. Three pins in one step (`tailwindcss`,
`@tailwindcss/vite` 4.3.3 of 2026-07-16; `lucide-react` 1.45.0 of
2026-09-11) recorded as one change: the styling stack.
Important design decisions made / open questions:
- The theme toggle lives in the panel's foot: the spec says no top bar.
- In the rail the labels are hidden; sign-out stays as an icon.
- In local mode the profile block says "Sign-in is off" instead of a
  sign-out button.
- `@import "tailwindcss" source(none)` with explicit `@source` lines, so the
  scan never reads the backend tree.

### Step 19 — history   (feature/poc-19-history)

Summary: hash routing by hand (`#/`, `#/c/<uuid>`; an unknown hash reads
as the new chat without rewriting the address, so `#/sign-in?error=`
survives; ids validated before any URL), the history in the panel over
`GET /api/conversations` (thirty a page, "Load more" by cursor, folded by
id, every answer numbered so a superseded ask is dropped, a refresh after
every write and a refresh beating a later page), per-row rename (inline,
Enter/Escape, code-point bound of 120, the backend's own sentence when
over) and delete (inline confirm, a 409 as a fixed sentence), accessible
names carrying the title, focus handed back to the list when a row is
gone; opening a conversation (`useConversation`: the current branch walked
from `leaf_id` up the `parent_id`s, siblings excluded, cycles ended;
`run_id`/`resume`/`ended_badly`), a read-only `ConversationView` as the
stand-in for the chat (title first, messages with time and provenance,
"Answering…" with Cancel, one sentence per bad ending, "not here" for a
404), the shell routing between the empty chat and a conversation, the
seam types `AgentId`/`ChatProps`, fixture-server scenes and screenshots.

Review: 1 round.
- High: 0
- Medium: 0
- Low: 8 (8/0)

Checks: `scripts/check-all.sh` without a database (2528 backend, 279
frontend tests) and with one required (2718); the bundle 79.5 kB gzipped,
no new dependency; nine screenshots (list, rename, confirm, conversation
light/dark, answering, ended badly, phone).
Not done / to watch: `PUT .../leaf` (branch switching) is the chat's;
no streaming here; the row menu is a disclosure, not `role="menu"`; a
refresh after a write goes back to page one. About 2,700 lines with tests
and the fixture server.
Important design decisions made / open questions:
- Reading a hash never writes one.
- `aria-disabled`/`readOnly` instead of `disabled` on a focused control
  while a request is in flight, so focus and Escape keep working.
- The conversation's heading follows the panel's list; a reread refreshes
  the list too, so the two never disagree.

### Step 20 — vendor-assistant-ui   (feature/poc-20-vendor-assistant-ui)

Summary: the assistant-ui styled components copied into
`frontend/src/chat/assistant-ui/vendor/` from the two shadcn-style
registries (eleven assistant-ui items, five shadcn/ui items; the
attachment items skipped; the CLI never run), with the upstream commits at
fetch time, both MIT texts beside the code, a README listing every file,
the six local modifications (`@/` → relative paths, our own `lib/utils.ts`
in place of the squatted `cn` package, the attachment composer removed
from `thread`, one field read as optional against the pinned release,
Prettier, two ESLint rules relaxed for `vendor/**` only), the re-sync
procedure, and a vetting-judgement table for the nine packages the copy
imports (all under the ten-day cooldown; `assistant-cloud` and `zustand`
arrive transitively and nothing configures them). `tw-animate-css` and
`tw-shimmer` are CSS packages, named in `CSS_PACKAGES`. Records in
`ip-clearance.md`, `third-party.md`, `REUSE.toml` (three copyright
groups), `LICENSES/MIT.txt`. Nothing imports the copy yet; Tailwind's
scan excludes it (`@source not`) until step 21 wires it, so the step
costs 345 bytes gzipped. A test keeps the README's file list, the licence
texts and the copy's imports honest.

Review: 1 round (about provenance): the copy is byte-exact against the
registries, every difference one of the listed modifications; commits
unchanged; licence texts exact.
- High: 1
  - three pins predate npm provenance and four are single-maintainer while
    the contributor rule stated both as requirements (and the same change
    refused `cn` by that rule) — fixed as a records change: the rule now
    says what is true (provenance required for releases after April 2023;
    a single maintainer accepted with years of settled history), and the
    judgement per package is written down in the vendor README.
- Medium: 4 (4/0) — "absent from the bundle" was false for CSS (40 kB of
  utilities compiled for unrendered markup; now excluded); `lib/utils.ts`
  was attributed to upstream though written here; `AGENTS.md` lacked the
  vendor do-nots; publish dates not recorded.
- Low: 5 (5/0)

Checks: `scripts/check-all.sh` without a database (2528 backend, 289
frontend tests) and with one required (2718); `reuse lint` 294/294; the
licence gate over 483 installed packages with no new exception.
Not done / to watch: tools and reasoning components kept though the POC
has neither (cutting them would fork `thread`); `@assistant-ui/react-ag-ui`
0.0.60 is still pinned to `@ag-ui/client ^0.0.59` while the protocol is
at 1.0.0 — step 21 decides between the bridge and a small client of our
own. The registries carry no version; the recorded commit is each
default branch's head at fetch time. The implementer once ran Prettier
over `docs/` by mistake and restored it — `docs/` is not
Prettier-formatted.
Important design decisions made / open questions:
- The vetting rules were amended rather than silently bypassed.
- The `utils` registry item now re-exports a three-week-old package on a
  squatted name; refused, the helper is ours.

### Step 21 — chat   (feature/poc-21-chat)

Summary: the chat behind the seam, with no new dependency. Our own AG-UI
client under `src/chat/assistant-ui/agui/` — an SSE reader over `fetch`
(any newline convention, multi-line data, comments, a 4 MiB cap, linear
scanning), typed decoding of exactly the vocabulary the backend emits
(unknown events ignored), and a client that starts a turn or a
conversation, reads the run and conversation ids from the headers (UUIDs
only), tracks the position from numbered events alone, re-attaches with
`Last-Event-ID` on a dropped stream with bounded backoff (5xx and network
retried, 4xx not, the budget reset when the position advances, a replayed
block skipped unless terminal). A pure reducer (`state.ts`) over the whole
tree, the in-flight answer, thinking per stretch, the three re-attach
no-ops, fixed sentences per `RUN_ERROR` code, `RUN_FINISHED cancelled` not
an error, a refused turn taking back only what it added, a lost stream
reading the store once and re-watching, a `notice` for a second send that
keeps the text. The assistant-ui external-store runtime over it
(`messageRepository` with `headId` for the branch picker,
`unstable_onBranchChange` → `PUT .../leaf`, `setMessages` re-syncing our
state, edit → `parent_id`, regenerate → `{regenerate}`, sending under an
unanswered question → its parent, cancel → `POST .../cancel`, every read
attaching to a run in flight, `isRunning` only when a run exists, the
composer's draft put back or cleared exactly where the library moves it).
`Chat.tsx` renders the vendored Thread; `src/chat/index.ts` exports `Chat`
and three types. The read-only `ConversationView` is deleted. Tailwind's
palette names mapped onto our tokens (`muted`/`accent` collisions renamed
in the shell); `@source not` removed; the bundle budget set to 400 KB
gzipped (278.6 KB measured). Verified against the real backend in local
mode from a real browser: sending, thinking collapsed, streaming,
re-attaching after a tab went away, a `RUN_ERROR`, a retry.

Review: 4 rounds (three full, one short).
- High: 5
  - a stream lost after the retries fell through to a re-read that set
    the run again with nothing watching (stuck "answering");
  - a first turn resolving after the person had moved on still routed the
    shell to it while the state held another conversation;
  - the post-turn re-read adopted another tab's run with nothing watching;
  - a refused turn removed every unsent message and orphaned a running
    answer, crashing the interface;
  - a send during the sending window lost the text and its notice was
    wiped within milliseconds.
- Medium: 9 (9/0) — re-attach requests not retried; an early stop putting
  a sent question back in the composer; stop inert before the run exists;
  the whole tree relinked per delta (now a `WeakMap` cache, 46 → 2 ms
  over 400 messages); `tries` reset by a replay; a lost stream leaving a
  message spinning; a failed cancel showing the browser's words; a
  whitespace draft defeating the stop guard; and one more.
- Low: 20 (20/0)

Checks: `scripts/check-all.sh` without a database (2528 backend, 381
frontend tests) and with one required (2718); the bundle 278.6 KB gzipped
of 400 KB; `bundled-packages.txt` +129 (the Thread's tree, all allowed;
`assistant-cloud` absent from the bundle); nine live-backend screenshots
and fourteen fixture-scene ones.
Not done / to watch: no tool-call or `STATE_*`/`STEP_*` events; no
delete/feedback/speech/attachment adapters; `unstable_onBranchChange` is
an unstable API inside the seam; the `useChat` → `runtime.thread.composer`
bridge (read on cancel, write on refusal/stop) is the fragile edge — it
acts only on text it put there or found empty; the reducer's third no-op
is reachable only from a re-attach at the end. About 5,400 lines with
tests; the runtime is 2,400 non-test lines with comments.
Important design decisions made / open questions:
- `@assistant-ui/react-ag-ui`/`@ag-ui/client` not used: the bridge pins a
  pre-1.0 client; the spec foresaw our own.
- One turn at a time: a second send is refused with a notice and its
  text put back, rather than a Stop that could stop nothing.
- Every read attaches to a run it finds in flight (a run begun elsewhere
  is watched here too).
- A refused turn takes back only the message it added; a message with a
  child is never removed.

### Step 22 — wheel   (feature/poc-22-wheel)

Summary: the built frontend served by the backend at `/ui/` (`api/ui.py`:
Starlette static files behind `UiHeaders` — the spec's CSP with `base-uri`
and `form-action` added, `X-Frame-Options`, `no-store` for the page,
immutable for Vite's hashed assets only, the policy on 404/405 too; dot
segments refused; `/` and `/ui` redirect to `/ui/` honouring `root_path`;
HEAD on both doors; a 503 "not built" page in a checkout without a
build). The `robinauts` command (`argparse`): `start` (uvicorn built as a
`Server`, `proxy_headers`, `--forwarded-allow-ips`, a bounded graceful
shutdown, an access log that cuts every query string, root-only logging so
the pinned vendor loggers stay), `--dev-no-sign-in` refusing a
non-loopback host before binding, `--uds` with the socket bound by the
command itself at mode 0600 (`--uds-mode`), removed on shutdown by a
signal handler that unlinks only the file it made and exits `128+signum`
even when the signal was inherited-ignored; `db init` (idempotent, refuses
another version); `version`; driver failures translated in `datastore`
into one `DatabaseUnreachableError` line with secrets redacted. The wheel
(hatchling, `hatch_build.py`: a metadata hook staging `LICENSE`, `NOTICE`
and the frontend's `THIRD_PARTY_LICENSES.txt` into `license-files`, a build
hook refusing a wheel without the built frontend, force-including
`frontend/dist` as `robinauts/ui/` file by file without dotfiles; an sdist
that is honest about not being the deliverable). `scripts/build-wheel.sh`,
`scripts/check-wheel.sh` (contents, metadata, an install into an empty
venv), a `wheel` CI job uploading the artifact, `check-all.sh` builds it.

Review: 3 rounds, no high in any.
- High: 0
- Medium: 9 (9/0) — sdist builds leaving staged licence copies and a
  misleading refusal; `HEAD /` 405; driver errors as tracebacks on
  `db init`; the socket world-writable under uvicorn's chmod; a 405 from
  the mount without `Allow`; a lost bind race unlinking the winner's
  socket; an sdist unpacked inside the repository misread as a checkout;
  a `SIG_IGN` parent making the server survive its own termination.
- Low: 19 (19/0)

Checks: `scripts/check-all.sh` without a database (2672 backend, 381
frontend tests; the wheel built, installed and answering) and with one
required (2869); the installed wheel run live in local mode over TCP and
over a unix socket with the headers, refusals, signals and socket modes
observed; a screenshot of the interface served from the wheel.
Not done / to watch: `/ui` redirects rather than serving the page (the
bundle's `base: "./"`); `favicon.ico` is a 404; the sdist is not the
deliverable and a plain `uv build` fails on purpose; `hatch_build.py`
infers the target from hatchling's imported builder modules (pinned by
tests both ways); SBOM, provenance and `RELEASING.md` are later. About
2,300 lines excluding tests; the socket handling is 140 of them.
Important design decisions made / open questions:
- `ROBINAUTS_DATABASE_URL` only from the environment; never a flag.
- The CLI exits 2 for usage and configuration, 1 for a failure; a
  `ConfigError` prints its problems one per line, never a traceback.
- Over `--uds`, `forwarded_allow_ips` defaults to `*` (nothing but what
  the mode admits can connect).

### Step 23 — deployment   (feature/poc-23-deployment)

Summary: `docs/deployment.md`, the guide a platform team follows on a
clean machine: the wheel and its hashed `requirements.txt` from CI (the
locked set installed with `pip install --require-hashes`, then the wheel
with `--no-deps` — a plain install had drifted to four versions no gate
judged), a system user and venv, the role and database, a complete worked
`robinauts.toml` (tested against both parsers), registering the redirect
URIs at Google and Okta with the claims the platform reads, the
environment file and a systemd unit (`RestartPreventExitStatus=2`, the
`--uds` recipe with the group and `--forwarded-allow-ips '*'`), nginx and
Caddy snippets (no buffering, the ACME challenge, 1 MiB bodies, HSTS),
first start, verification, operating, the local mode, and a table of what
can go wrong with the real messages. `docs/working-notes/deployment-
rehearsal.md`: the rehearsal on this machine (`scripts/rehearse-
deployment.sh` + `rehearse_deployment.py`: the wheel installed the guide's
way, `db init`, two stand-in OIDC providers behind a self-signed TLS
front, two people in with the `__Host-` cookie, a third refused, private
conversations, a stream re-attached, the engine swapped across a restart,
every secret absent from logs, responses and rows; the GPL gates red for
both ecosystems) with the status of the seven "Done when" items and the
exact list of what remains for a real deployment.

Review: 2 rounds.
- High: 1
  - the guide said cookie security depended on `X-Forwarded-Proto`; it
    depends on `public_url` and the origin check — nothing in the platform
    reads a forwarded header (a safer design than the guide described) —
    fixed in the prose and three table rows.
- Medium: 11 (11/0) — dependencies resolved unlocked at install (now the
  exported, hashed requirements beside the wheel, checked in CI); a
  restart loop on a configuration refusal; the socket recipe; a vacuous
  hash test; the wrong half of the database url grepped; an nginx
  directive too new for Ubuntu 24.04; the ACME challenge redirected;
  socket forwarded-IPs advice; and others.
- Low: 16 (16/0)

Checks: `scripts/check-all.sh` without a database (2679 backend, 381
frontend; the wheel and its requirements built, installed the locked way
and answering) and with one required (2876); the rehearsal 32/32.
Not done / to watch: the real deployment is the user's (below). The
systemd unit and the proxy snippets are prose that has not been run. The
guide is 613 lines. `docs/specs/legacy/runs.md` now says shutdown cancels and
draining is planned.
What remains for the real deployment ("Done when"):
1. a machine, a host name, a certificate, nginx or Caddy from the guide,
   the systemd unit;
2. Google and Okta client registrations with the two redirect URIs
   (Okta: the groups claim and scope, the people assigned);
3. a real Anthropic key — then item 3's engine half and item 4 can be
   finished on the real instance;
4. item 3's vendor half needs a second provider kind admitted through
   `DEPENDENCIES.md` (this build reaches Anthropic alone);
5. what breaks on the real machine written into the rehearsal note.

### Step 24 — demo   (feature/poc-24-demo)

Summary (requested after the plan): a `demo/` folder beside `backend/` and
`frontend/` with one entry point that starts everything and one that
stops it. `demo/start.sh` refuses root, checks `uv` and Node (via nvm),
reads `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` from the environment or a
mode-0600 `demo/.env` (OpenRouter preferred; the key is unset again at
once and handed only to the server's own invocation, never printed),
starts a throwaway PostgreSQL 16 with `pgserver` 0.1.4 run as a tool under
`demo/.state/pgdata`, writes `demo/.state/robinauts.toml` from a template
through `demo/config.py` (literal substitution, validated by a `tomllib`
round trip: one provider, one model, two agents — one per engine), builds
the frontend if missing, installs the platform non-editable into
`demo/.state/venv` on CPython 3.12 (`uv sync --locked --no-dev`, so the
packaged UI is inside the package), `db init`, `robinauts start
--dev-no-sign-in` in the background with a bounded health wait, prints
the URL and opens it when a display exists. `demo/stop.sh` stops the
server and the cluster; `--reset` removes the state. To reach OpenRouter
without the excluded OpenAI client, the backend gained the
`anthropic-compatible` provider kind: a `base_url` (required, validated
by `is_endpoint_url`) at an endpoint speaking Anthropic's Messages API —
OpenRouter's `https://openrouter.ai/api` does — through the same client,
key and headers pinned as before; `endpoint_of` is the one place both
engines choose the endpoint. A routing proof under `tests/live/`
(`ROBINAUTS_LIVE_ROUTING=1`, no key needed) shows both engines' requests
reach `openrouter.ai/api/v1/messages`.

Review: 1 round.
- High: 0
- Medium: 4 (4/0) — the key was exported to every subprocess (pgserver,
  npm, uv); the routing test reached a vendor on ordinary runs; it failed
  rather than skipped on a non-401; the demo venv was on Python 3.14.
- Low: 12 (12/0)

Checks: `scripts/check-all.sh` without a database (2698 backend, 381
frontend) and with one required (2895); the demo run from nothing with a
bogus key: the bundle served, both agents listed, a turn on each failing
with OpenRouter's `User not found` 401 in the log (the request reached
it), the key in no log line and no file; `start` twice, `stop` twice,
`--reset`, and the same with a bogus Anthropic key against
`api.anthropic.com`.
Not done / to watch: no real key exists here, so no live answer was seen
— the first real run is the user's. `pgserver` publishes no licence
metadata (recorded under "Known exclusions"; it is a demo tool, not a
dependency). The demo cluster uses trust auth on loopback (contained by
the 0700 data directory; a demo, not a way to run anything). No automated
test of the shell scripts.
Important design decisions made / open questions:
- OpenRouter is reached through the Anthropic-compatible kind, not the
  excluded OpenAI client; `openai`/`openai-compatible` stay out.
- No ordinary test run reaches a provider; the routing proof is opt-in.
