# Plan: auth

Block 7 of `master-plan.md`. Read `docs/specs/sign-in.md` first: it is the design this
block implements, from neorc's (`docs/legal/ip-clearance.md`), and the legacy code that
implemented it once is open beside it (`legacy/application/sign_in.py`,
`legacy/adapters/identity_provider.py`, `legacy/core/claims.py`). `docs/architecture/
controller.md` says sign-in is not the controller's; `web.md` lists `sign_in`,
`sign_out` and `current_user_session` as web's.

Base branch: `feature/auth`, which carries blocks 5 and 6. Branches `feature/auth-N`.

Goal: `robinauts start` with a configuration naming an identity provider serves the
sign-in page. A person on the allow list signs in through Google or Okta over OpenID
Connect and gets a session cookie. Every `/api/` route answers for the signed-in user,
whom `ensure_user` makes from the real identity, and 401 without one. Sign-out ends the
session. An API token reaches the same routes without a browser. `--dev-no-sign-in` is
the one way to run with no provider, and it is never the default. On PostgreSQL a
session outlives a restart. Happy path only.

## What block 6 hands over

- `users`, keyed by `(provider, subject)`, and `ensure_user` on the controller, which
  web calls today with one local identity at start and will call with the real one.
- The store port, its two stores and their contract suite in `tests/contracts/`: the
  pattern the credential store follows.
- `schema.sql` pinned by `SCHEMA_SHA256`, `robinauts db init`, and the pool the
  PostgreSQL store opens, which the credential store shares.
- The composition reading the TOML file and handing `secret_for`, which is how a
  provider's client secret is read.
- The frontend's sign-in page, session store and 401 handling, written already against
  `GET /auth/session`, `GET /auth/login/{provider}?return_to=` and `POST /auth/logout`
  (`frontend/src/session/`), and the local-mode banner. The frontend changes only if a
  shape does.
- The stand-in OpenID Connect provider, `tests/standin/provider.py`: three endpoints on
  a loopback port, an unsigned ID token with the claims the test wrote, scripted
  misbehaviour. It imports nothing of legacy's. The fakes in `tests/fakes/` do, and are
  not reused.
- The layer rules: `httpx` under web, `asyncpg` under `controller.adapters`, and web
  importing `controller.contract` and `controller.composition` alone.

## Decisions

- **Where sign-in lives.** The flow, the OpenID Connect exchange, the cookies and the
  routes are web's: `web/sign_in.py` and `web/oidc.py`. The records are kept through
  one new port in the controller's contract, `Credentials`, beside `Controller` in
  `contract/ports.py`: user sessions, pending sign-ins and API tokens. Its records
  `UserSession`, `PendingLogin` and `ApiToken` go in `contract/domain.py`.
  `MemoryCredentials` (`controller/adapters/memory.py`) and `PostgresCredentials`
  (`controller/adapters/postgres/credentials.py`, over the store's pool) implement it;
  the composition builds it on the same storage as the controller and hands it to web
  beside the controller. The controller learns nothing: sign-in is not there, as
  `controller.md` says, and it is asked `ensure_user` with the identity the provider
  vouched for. **This is the one new class in a contract this block adds**, and the
  owner approves it by approving this plan; no other.
- **Every credential operation is one statement**, as legacy's were, because the port
  promises what a read followed by a write does not keep: a pending sign-in is taken by
  `DELETE … RETURNING`, once; a session is one `INSERT`; a secret is resolved by one
  `SELECT … JOIN users`, judged against the `now` the caller gives, so the store keeps
  no clock. Secrets never reach a store: the cookie holds a random 256-bit value and
  the row its SHA-256; the same for a token and for `state`.
- **The configuration is the same file.** `public_url`, `session_hours`, `providers`
  and `allow` are web's tables, parsed by `web/sign_in.py` into `SignInConfig`
  (`ProviderConfig`, `AllowEntry`, `Matcher`); the controller's four stay the
  controller's. The composition gains `read_tables(path)`, so both halves are parsed
  from one reading, and `robinauts start` refuses an unknown top-level key. Problems
  are collected and reported at once, as `parse_config` does. Refused as the spec
  says: providers with no `[[allow]]`; `everyone` or `email_domain` on Google; an
  `admin` table, since roles are not this block's; a `public_url` that is `http` off
  loopback. A client secret is the name of an environment variable, read through
  `secret_for` at the moment of the exchange and held nowhere.
- **The exchange** is `web/oidc.py` on `httpx`: discovery at the first sign-in, cached
  on success and not on failure, whose issuer must equal the configured one and whose
  endpoints must be `https`, loopback excepted; the authorization URL with `state`,
  `nonce` and PKCE S256; the code exchange with `client_secret_basic`, following no
  redirect, with timeouts, TLS verified. The ID token is decoded without verifying its
  signature, which the spec allows for a token read out of the token endpoint's own
  answer, and its claims are checked: `iss`, `aud`, `azp`, `exp`, `iat` within 60 s,
  `nonce`, and a `sub` that is there. Google's `email_verified` counts only with `hd`
  or a `gmail.com` address. No JWT or crypto dependency: `hashlib`, `secrets` and
  `base64` of the standard library.
- **Sessions and cookies.** A user session is `(id, user_id, secret_hash, created_at,
  expires_at)`, `expires_at` being `now + session_hours`, never renewed. The cookie is
  `__Host-robinauts_session`: `HttpOnly`, `SameSite=Lax`, `Secure`, `Path=/`; on a
  loopback `http` `public_url` the prefix and `Secure` are dropped, as legacy did
  (`legacy/api/cookies.py`), which is what the proof and the stand-in run on. A pending
  sign-in is `(state_hash, provider, nonce, verifier, return_to, created_at,
  expires_at)`, ten minutes, taken once; the login cookie `robinauts_login` carries
  `state`, and the callback requires the query's `state` to equal it. `begin` deletes
  the expired pending sign-ins before inserting its own, so the table holds ten minutes
  of sign-ins with no sweep; the cap is stage two's. `return_to` is kept only as a path
  of this origin, else `/`.
- **Who is asking** is one FastAPI dependency, `current_user`, on every route under
  `/api/`: the session cookie, or `Authorization: Bearer`, resolved to a `User`; none is
  401 with the error body the wire has. It replaces `app.state.user`, which goes.
  `GET /auth/session` is never a 401: it answers `sign_in`, the providers and the user,
  the shape the frontend already reads. A write that carries the session cookie must
  carry `Origin` equal to `public_url`, else 403: the one request-protection rule the
  cookie makes necessary now. `Sec-Fetch-Site`, the JSON type, duplicate headers and
  the body bound stay stage two's (`stage-two-plan.md`, "web").
- **A sign-in that does not complete** redirects to `/ui/#/sign-in?error=<code>`, where
  the sign-in page reads it, with one of the spec's fixed codes (`expired`, `state_mismatch`,
  `not_allowed`, `unknown_provider`, `busy`, `provider_unavailable`,
  `provider_refused`, `invalid_id_token`); the provider's own words go to the log alone.
  One log line per sign-in, refusal and sign-out, with the user's id or the code; the
  audit table of `privacy.md` is not this block's.
- **Sign-out** deletes the row and clears the cookie; 204 either way.
- **The local development mode** is today's behaviour made explicit: `robinauts start
  --dev-no-sign-in` is the only way to start with no provider. It refuses a file that
  holds any sign-in table and a `--host` that is not loopback; without the flag, a file
  that names no provider is refused at start. Everyone is one real user under the
  reserved key `("!local", "developer")`, got through `ensure_user` once at start as
  today, so no identity can carry it. `/auth/session` answers `sign_in: false` with
  `local_development: true`, and the server logs a warning. The per-request `Host`
  check against DNS rebinding is stage two's, with the rest of request protection.
- **API tokens.** The spec has them "planned" and no design; this block does the least
  that lets a channel other than the browser in. A signed-in person mints one at
  `POST /auth/tokens` with a name and is shown the secret once; `GET /auth/tokens`
  lists theirs; `DELETE /auth/tokens/{id}` revokes. The row is `(id, user_id, name,
  secret_hash, created_at, expires_at)`, the life ninety days. `Authorization: Bearer
  <secret>` resolves to the user on every route and is not subject to the `Origin`
  check, a bearer being no cookie. `docs/specs/sign-in.md`'s "Not there yet" becomes
  the section that says this when it lands. Who may mint a token is the decision most
  likely to be overruled; it changes one route and nothing below it.
- **The schema** gains `user_sessions`, `pending_logins` and `api_tokens`, with named
  constraints and an index on each `expires_at` for stage two's sweep; `SCHEMA_VERSION`
  stays 1 and `SCHEMA_SHA256` is re-pinned. There are no migrations in stage one: a
  database made by block 6 is dropped and made again.
- **Roles are not here.** The spec's two roles, the admin list and the permission table
  wait; every route under `/api/` needs a signed-in user and nothing finer.

## Not in this block

- Roles, the admin list and the permission a route declares.
- Request protection beyond the `Origin` check on cookie-authenticated writes, and the
  local mode's per-request `Host` check: stage two, "web".
- The cap on pending sign-ins, and the sweeps of expired sessions, pending sign-ins and
  tokens: stage two, "housekeeping".
- The audit log table; sign-out at the provider; a rate limit on beginning a sign-in.
- Tokens minted by an operator for somebody else, scopes on a token.

## Rules

- One branch per step, `feature/auth-N`, from the previous step's branch. One commit per
  branch, pushed. No pull requests, no reviews between steps.
- Minimal code for the main flows. No edge cases, no defensive checks beyond the
  refusals the spec and the port name. Comments only where the code does not say it;
  no docstrings unless a line cannot be read without one.
- The layer rules of `docs/architecture/rules.md` hold. `httpx` is imported under web
  alone; `asyncpg` under `controller.adapters`. The contract grows what the first
  decision names and nothing else; another class there, or a new import direction,
  stops the work and asks the owner. Nothing imports legacy.
- Tests that need a database are marked `database` and take
  `ROBINAUTS_TEST_DATABASE_URL`, each in a schema of its own, as block 6's do. Tests
  that need a provider start the stand-in on a port the operating system picks and are
  marked `io`.
- Before each commit, from `backend/`: `uv run --locked ruff check --config
  pyproject.toml . ../scripts ../demo ../examples`, `uv run --locked black --check
  --config pyproject.toml . ../scripts ../demo ../examples`, `uv run --locked pytest
  tests/unit -q`, the step's database tests with `ROBINAUTS_TEST_DATABASE_URL` set,
  and `uvx reuse lint` from the root, all green. Every new file starts with the two
  licence header lines. Line length 100.
- Commit messages end, after a blank line, with the attribution lines the session's
  system reminder gives.

## Steps

1. **The sign-in configuration and the mode.** `SignInConfig` and its records in
   `web/sign_in.py`, with `parse_sign_in(raw)` and its refusals; `read_tables` in the
   composition; `robinauts start` parsing both halves, refusing an unknown top-level
   key; `--dev-no-sign-in` with its two refusals, and the local user under the reserved
   key; a file with no provider refused without the flag. Test: the spec's TOML parses
   to the expected records; one with three mistakes names all three; the flag with a
   sign-in table refuses; no flag and no provider refuses; `--host 0.0.0.0` with the
   flag refuses.
2. **The credential port and its memory store.** The records and the `Credentials`
   port; `MemoryCredentials`; the suite in `tests/contracts/credentials.py`. Test: the
   suite over the memory store: a session opened is resolved to its user until the
   `now` given passes its expiry and not after; of two callbacks taking one pending
   sign-in, one gets it; a token resolves until revoked; sign-out of a session nobody
   has is false.
3. **PostgreSQL.** The three tables in `schema.sql`, the hash re-pinned,
   `PostgresCredentials`, and the composition building the credentials on the store's
   pool. `data-model.md`'s table gains the records. Test: the suite over PostgreSQL;
   the schema pin test; `db init` on an empty schema makes the three tables.
4. **The exchange.** `web/oidc.py`: discovery, the authorization URL, the code
   exchange, the claims. Test: against the stand-in, the authorization URL carries
   `state`, `nonce` and the S256 challenge, and the exchange returns the claims the
   stand-in wrote; a token with another `nonce`, an `exp` in the past or another `aud`
   is refused as `invalid_id_token`; discovery naming another issuer is refused; a
   token endpoint that answers 500 is `provider_unavailable`.
5. **The flow, the cookies and the routes.** `begin`, `complete`, `resolve` and
   `sign_out` in `web/sign_in.py` over the credentials, the exchange and the
   controller's `ensure_user`; the four `/auth/` routes; `current_user` on every
   `/api/` route and the 401; the `Origin` check; the redirect with the code; the log
   lines. `the-path-of-one-message.md`'s step 2 says what happens now. Test: over the
   memory credentials and the stand-in, `GET /auth/login/okta` sets the login cookie
   and redirects to the stand-in; the callback with the right `state` signs in, sets
   the session cookie and redirects to `return_to`; `/auth/session` names the user and
   the providers; `/api/conversations` is 401 without the cookie and 200 with it; a
   write without `Origin` is 403; after `/auth/logout` the cookie is gone and the next
   request is 401; a person not on the allow list lands with `not_allowed`; a callback
   whose `state` is not the cookie's lands with `state_mismatch`; the mode answers
   `/auth/session` as the spec says and every route as the local user.
6. **API tokens.** The three routes, the bearer in `current_user`, and the spec's
   section. Test: a token minted is shown once, lists, authenticates a `GET` and a
   `POST` with no cookie and no `Origin`, is refused after its revocation and past its
   expiry.
7. **Proof.** The frontend built; a PostgreSQL with `db init`; the stand-in provider on
   a loopback port and a configuration naming it as the issuer with `public_url` on
   loopback `http`. In headless Chromium: the sign-in page with one button; sign in;
   a turn over echo; a reload keeps the session; the server restarted keeps it; sign
   out shows the sign-in page; a second person not on the allow list is sent back
   with `not_allowed`. From `curl`, a token minted through the browser's session lists
   the conversations. A short `auth-progress.md` records what was verified.
