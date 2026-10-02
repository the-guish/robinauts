# Block 7: auth, what was verified

Branches `feature/auth-1` to `feature/auth-7`, each from the one before, one commit per
step of `auth-plan.md`. On the code of step 6, which step 7 did not change, the unit suite
with the layer rules passed 3,075 and skipped 30 (`uv run --locked pytest tests/unit`), and
the block's `io` and database tests passed 34, over the stand-in provider and a PostgreSQL
16.14 cluster (`ROBINAUTS_TEST_DATABASE_URL`), each database test in a schema of its own.

## The steps

1. **The sign-in configuration and the mode.** `web/sign_in.py` parses sign-in's half of
   the file into `SignInConfig`, `ProviderConfig`, `AllowEntry` and `Matcher`, every
   problem in one `ConfigError`. The composition gained `read_tables` and `configure`, so
   `robinauts start` reads the file once and refuses a key of neither half.
   `--dev-no-sign-in` is the one start with no provider: it refuses a sign-in table and a
   host off loopback, and its user is `("!local", "developer")`. Beyond the plan: `admin`
   is a sign-in key, refused as roles; a provider id is a lower-case name; `everyone` must
   be `true`; `demo/start.sh` passes the flag.
2. **The credential port and its memory store.** `UserSession`, `PendingLogin` and
   `ApiToken` in `contract/domain.py`, and `Credentials` beside `Controller` in
   `contract/ports.py`: nine operations over hashes, every id and time the caller's. The
   memory adapter is the package `adapters/memory/`, its store in `store.py` and
   `MemoryCredentials` in `credentials.py`, over the store's users. The suite is
   `tests/contracts/credentials.py`. Beyond the plan: `api_tokens_of` breaks ties by id;
   the expired pending sign-in is asked for again at an earlier `now`, to see it deleted.
3. **PostgreSQL.** The three tables in `schema.sql`, every constraint named, each hash
   checked as 64 hex characters, an index on each `expires_at` and `user_id`;
   `SCHEMA_VERSION` stays 1 and `SCHEMA_SHA256` is re-pinned. `PostgresCredentials` reads
   the store's pool at each call, one statement per operation. `compose` returns the
   controller and the credentials on its storage. `data-model.md` gained the records.
   Beyond the plan: the credentials share the store's row helpers, and
   `test_controller_on_postgres` resolves a session on the pool the controller opened.
4. **The exchange.** `web/oidc.py`: `Exchange` on an `httpx` client of its own, bounded,
   following no redirect, TLS verified; discovery kept on success only, its issuer the
   configured one; the authorization URL with `state`, `nonce` and S256; the code
   exchanged with `client_secret_basic`; the ID token read unsigned and its claims checked.
   `SignInErrorCode` holds the spec's eight codes. Beyond the plan: an unconfigured
   provider is `unknown_provider`; a non-JSON 200 from the token endpoint is tested; the
   unit tests are `test_web_oidc_claims.py`, since two test files of one name collide.
5. **The flow, the cookies and the routes.** `SignIn` begins and completes a sign-in over
   the credentials, the exchange and `ensure_user`, resolves a cookie and signs out.
   `web/cookies.py` drops `__Host-` and `Secure` on a loopback `http` `public_url`.
   `current_user` answers every `/api/` route and replaces `app.state.user`; a write with
   the session cookie needs `Origin`; a refused sign-in lands on
   `/ui/#/sign-in?error=<code>`. Deviations: `create_app` also takes `secret_for`;
   `random_secret` moved to `sign_in.py`, which `oidc.py` imports; the login and callback
   routes exist only with sign-in configured; a callback with no code is
   `provider_refused`.
6. **API tokens.** `current_user` reads a bearer before the cookie, and a bearer that names
   nobody is 401. `POST /auth/tokens` answers 201 with the secret, once; `GET` lists
   without it; `DELETE` is 204, or 404 for a token not the person's. The `Origin` check
   stays on the cookie. `openapi.json` was regenerated, and `sign-in.md`'s "Not there yet"
   became "API tokens". Beyond the plan: a token naming the local development mode's user
   is refused, as a session naming it is.
7. **Proof.** Below. No code changed: this note, the token routes in `web.md`, block 7
   marked done in `master-plan.md`, and a correction entry in `docs/legal/ip-clearance.md`
   for the API tokens, since that log's entries are never edited.

## The proof

The frontend built with Node 24 (`npm run build`), and `robinauts start` served that
build. The stand-in ran from a script under the scratchpad, on the port the operating
system gave it, `http://127.0.0.1:33755`, with the claims of the next ID token read from
a file it watched. The configuration named it as the one provider, `standin`, titled
"Stand-in", with `public_url = "http://127.0.0.1:8711"`, the client secret through
`ROBINAUTS_STANDIN_SECRET`, `[[allow]]` with `email_domain = "example.com"`, and the echo
agent of `examples/echo.toml`. Storage was the empty database `robinauts_proof`.

- `robinauts db init`, twice, said "the database is at schema version 1 (schema.sql
  90bddf827ac5)", and `user_sessions`, `pending_logins` and `api_tokens` were there with
  the plan's columns.
- In headless Chromium, `/ui/` showed the sign-in page with one link, "Sign in with
  Stand-in". Clicking it went to `/auth/login/standin`, `302` to the stand-in's
  `/authorize` with `state`, `nonce` and an S256 challenge, `303` to
  `/auth/callback/standin`, `302` to `/` and on to `/ui/`, signed in. The browser held
  `robinauts_session` alone, `HttpOnly`, `SameSite=Lax`, not `Secure`, twelve hours;
  `user_sessions` held one row, the SHA-256 of the cookie. `GET /auth/session` from the
  page named Ada Lovelace, `ada@example.com`, provider `standin`.
- A turn over echo: "hello" in the textarea, "The tool said: hello" in the thread.
- A reload kept the cookie and the user, and showed the conversation.
- The server stopped cleanly and started again. A reload kept the same cookie and user,
  resolved against `user_sessions` on PostgreSQL, and showed the conversation and its
  thread. The new process fetched discovery again at its first sign-in.
- From `curl`, with the browser's session cookie and `Origin: http://127.0.0.1:8711`,
  `POST /auth/tokens` answered 201 with the secret; without `Origin` it was 403. With
  `Authorization: Bearer` and no cookie, `GET /api/conversations` listed the browser's
  conversation, `GET /auth/tokens` listed the token without secret or hash, and
  `POST /api/turns` with no `Origin` was 200 and streamed echo's answer to `RUN_FINISHED`,
  in a second conversation of the same user. `api_tokens` held the token's SHA-256, for
  ninety days.
- "Sign out" in the profile block showed the sign-in page again. The cookie was gone,
  `user_sessions` was empty, and `/api/conversations` was 401 from the page and with the
  old cookie from `curl`.
- With the claims changed to `mallory@elsewhere.org`, the same link came back to
  `/ui/#/sign-in?error=not_allowed`, and the page said "The provider knows who you are, but
  this deployment does not let that account in." No cookie was set and no user was made;
  the log said "no allow entry of 'standin' lets in 'mallory@elsewhere.org'".
- `robinauts start` without the flag over `examples/echo.toml`, which names no provider,
  was refused with "no identity provider is configured". `--dev-no-sign-in` with the
  proof's configuration was refused: "the file holds allow, providers, public_url". Both
  exited 1 and bound nothing. `--dev-no-sign-in` over `examples/echo.toml`, in memory,
  started with its warning, and `/auth/session` answered `sign_in: false`,
  `local_development: true` and the user of `!local`.

The browser's only console errors were a 404 for `/favicon.ico`, which Chromium asks the
origin root for, and the two 401s the script asked for.

Not verified: a real Google or Okta tenant, which this environment cannot reach. The
stand-in serves the three endpoints over loopback `http` with an unsigned token. Google's
`hd` and Okta's groups claim are read by the unit tests of the claims
(`test_web_oidc_claims.py`), and a provider that misbehaves is met by the `io` tests
against the stand-in (`test_web_oidc.py`). The `https` cookies, `__Host-` and `Secure`, never reached
a browser, which sets neither without TLS. `test_web_tokens.py` runs on an `https`
`public_url` and sends the `__Host-robinauts_session` cookie the app reads, but no test of
the new layer asserts the `Set-Cookie` of an `https` deployment. A one-line check of
`Cookies("https://robinauts.example.com")` printed `__Host-robinauts_session=x; HttpOnly;
Max-Age=43200; Path=/; SameSite=lax; Secure`.

## Decided on the way

- The local development mode's user signs nobody in where sign-in is on, by a session or
  by a token, since the mode mints tokens for that user too.
- Sign-out ends the session it is asked with and nothing else. The token minted through
  the browser's session still answered 200 after the sign-out: a token ends at
  `DELETE /auth/tokens/{token_id}` or after ninety days, as the spec says.
- The local development mode was checked in memory, so `robinauts_proof` holds the proof's
  records alone: one user, two conversations, one token, no session.
