# Stage two: hardening

The running plan for stage two of `master-plan.md`. Stage one's last step completes it
with every learning still held in legacy before legacy goes; until then, each block of
stage one adds what it found. Items are grouped by what they touch: the security gaps
first, then the features that cross components, then one section per component. Each item
has a code, `<section>-<number>`, to cite it by; the data-model items keep the numbers the
review ranked them with.

## Security

Found by the survey of legacy before block 9 (`legacy-learnings.md`), by reading what
legacy guarded and then running the new layers at `refactor/cleanup` (`52a4ebf`). Each is a
live gap today, not a feature deferred. "Verified" means the behaviour was observed, not
read. `L` is `backend/src/robinauts/legacy/`.

### security-01: The sign-in exchange keeps its credentials reachable from its exceptions

In `web/oidc.py`, `complete` (lines 103 to 131) holds `secret`, `code` and `verifier` as
locals, and `_send` (165 to 172) raises `SignInError ... from error`. The `httpx` cause
carries the `Request`: its form body holds `code=...` and `code_verifier=...`, and its
`Authorization` header base64-decodes to `<client id>:<client secret>`. Verified by
walking the chain and by formatting the traceback with `capture_locals=True`, which is
what error reporters such as Sentry do: both printed all three. Nothing logs these with
`exc_info` today, so the first error reporter or `logger.exception` added to the sign-in
path would write the deployment's client secret and a live authorization code to the log.

Legacy made that impossible by construction (`L adapters/identity_provider.py:42-52`,
`:327-580`): the frames that touch a secret return a value and never raise; `_exchanged`
returns the provider's answer or a `_Refused` and clears and deletes the secret, the form
and the headers in a `finally`; `exchange_code` deletes the code and the verifier before it
raises, from a frame that holds none of them and while no exception is being handled, so
`__cause__` and `__context__` stay empty; the token request keeps no `httpx` cause
(`keep_cause=False`) where discovery, which carries no credential, keeps its own; a
`CancelledError` or a closed-client `RuntimeError` is re-raised as itself with every
traceback in its chain cleared. The docstring's reason: "a traceback captured with locals,
which error reporters do, prints every frame's variables, and the frames of a code exchange
hold the client secret, the authorization code and the PKCE verifier; an `httpx` exception
holds the `Request`, whose body holds all three, and stays reachable through `__context__`
even when it is raised `from None`."

Do the same in `web/oidc.py`, and port the traceback walker
(`tests/integration/test_http_identity_provider.py:848-1153`): it walks every exception
reachable through `__cause__` and `__context__` and every frame's `f_locals`, reads an
`httpx.Request` as an attacker would (headers, the form body parsed, the Basic header
decoded), formats with `capture_locals`, and runs over the stand-in's eight misbehaviours
(500, HTML, OAuth error, timeout, deep JSON, gzip bomb, oversized chunked, redirect). Use a
secret with a colon, a space, `&`, `=`, `+`, `%`, `/` and non-ASCII, as legacy's
`AWKWARD_SECRET` did, so RFC 6749 form-encoding is exercised too. Document the rule in
`docs/architecture/web.md`: what may be reachable from an exception.

### security-02: An environment variable replaces the configured API key in both engines

Both new engines build their vendor clients with the endpoint and the key as arguments
(`agent_engines/langchain_engine/clients.py`, `pydantic_ai_engine/clients.py`), which keeps
`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL` and `*_API_KEY` from redirecting a turn when a
`base_url` is configured. Nothing more. The Anthropic and OpenAI SDKs merge
`ANTHROPIC_CUSTOM_HEADERS` and `OPENAI_CUSTOM_HEADERS` into every request, and a line
`x-api-key: <somebody else's key>` replaces the configured key outright. Verified: with that
variable set, both new engines' clients sent `sk-somebody-elses-key`. `OPENAI_ORG_ID`,
`OPENAI_ORGANIZATION`, `OPENAI_PROJECT_ID` and `OPENAI_ADMIN_KEY` reach every OpenAI client
too. For a protocol kind (`anthropic-compatible`, `openai-compatible`) with no `base_url`,
the new engines pass `None`, and the SDKs then read `ANTHROPIC_BASE_URL` or
`OPENAI_BASE_URL`, so a variable in the process environment decides where the operator's
key is sent. `parse_config` accepts `base_url` on any kind and does not require it on a
protocol kind. The legacy negative control that proves the header leak
(`tests/unit/test_engines_over_chat_completions.py:315-330`) still passes on the installed
SDKs.

Legacy's rule, in the spec (`docs/specs/legacy/agents-engines-models.md:195-240`) and
promised to operators (`docs/deployment.md:233-243`, `:582-586`), had three layers
(`L adapters/agents/langgraph/engine.py:242-306`, `:351-367`, `:510-530`; the same in the
Pydantic AI engine): the endpoint, the key and the proxy are passed as arguments, because
an argument beats the environment; the key is passed again as a default header (`x-api-key`
or `Authorization: Bearer`), since the SDKs merge the variable's headers under the caller's
and the caller's win; and the variables no argument can refuse are removed from
`os.environ` once, at engine construction and never per turn ("a process-wide edit made
while turns are running would be one turn changing another's environment"):
`ANTHROPIC_CUSTOM_HEADERS`, `OPENAI_CUSTOM_HEADERS`, `OPENAI_ORG_ID`, `OPENAI_ORGANIZATION`,
`OPENAI_PROJECT_ID`, `OPENAI_ADMIN_KEY`, `ANTHROPIC_LOG`, `OPENAI_LOG`. A vendor kind takes
no `base_url` and a protocol kind requires one, "since a second answer to where is a way to
send the operator's key elsewhere".

Restore all three layers in both engines, make `parse_config` refuse `base_url` on a vendor
kind and require it on a protocol kind, and put the rule and the list of variables in
`docs/specs/agent-engines.md` under "Reach the model providers". Add a case to the shared
suite `tests/contracts/engine.py`: with every variable above set to a poison value, the
request the engine builds carries the configured key, the configured endpoint and no
header nobody configured. Keep legacy's negative control beside it: the same variables do
reach a client built without the engine, so the test proves something.

### security-03: The vendor SDKs' debug logging writes whole conversations to stderr

The Anthropic SDK reads `ANTHROPIC_LOG` at import, before any argument exists, and at
`debug` writes every request's options, `json_data` included, which is the system prompt
and every message of the conversation. An operator who turns the root logger up to DEBUG,
"which is a thing an operator does", gets the same from `anthropic._base_client` and
`openai._base_client`. The new engines pin no logger (`grep getLogger` under
`agent_engines/` finds nothing), and `docs/specs/agent-engines.md` promises "never write a
key, a secret or conversation content to a log". Legacy's two negative controls
(`tests/unit/test_langgraph_engine.py:1291-1300` and `:1380-1391`, which show the
conversation leaking without the pin) still pass, so the leak is real on the installed
SDKs.

Legacy (`L adapters/agents/langgraph/engine.py:308-349`, `:533-580`; Pydantic AI
`:298-338`, `:507-548`) set six loggers to WARNING at engine construction: `anthropic`,
`anthropic._base_client`, `openai`, `openai._base_client`, `httpx2`, `httpcore2`. The
decision was on each logger's own level, not its effective one: a logger at `NOTSET` or
below WARNING was pinned, one an operator had set stricter was left alone. The emitting
child was named as well as the parent, because a `dictConfig` entry on the child walks past
a pin on the parent. `httpx2` and `httpcore2` are the SDKs' forks, not the `httpx` that
sign-in uses. `tests/conftest.py:44-78` restored the levels around every test.

Restore the pins in both engines, name the loggers and the rule in
`docs/specs/agent-engines.md` under "Keep secrets secret", port the two negative controls,
and keep the subprocess test that sets `ANTHROPIC_LOG=debug` before import
(`tests/unit/test_langgraph_engine.py:1225-1275`), since what is read at import cannot be
tested in the same process.

### security-04: The local development mode answers a rebound `Host`

A page on the internet can point a host name of its own at `127.0.0.1`. The developer's
browser then connects to the local server and sends `Host: evil.example`, and every
same-origin rule holds, because it really is that page's own origin. In the new layer,
`same_origin` (`web/app.py:382-389`) returns at once when `sign_in is None`, and
`current_user` answers the local user to anyone. Verified: with `Host: evil.example` and
`Origin: http://evil.example`, `POST /api/turns` answered 200 and started a turn, and
`GET /api/conversations` answered 200 with the list. A rebinding page can read every local
conversation and spend the developer's model keys. `web/cli.py` refuses a non-loopback
bind, which is the other half of the rule and does nothing against rebinding.
`docs/specs/sign-in.md:117-140` states the request rule in the present tense.

Legacy (`L api/protection.py:51-71`, `:422-524`; `tests/unit/test_local_mode.py:364-560`)
ran one check on every request in the mode, reads included: exactly one `Host` header,
naming a loopback host in any spelling, any port, `*.localhost` included; the address the
server answered on (`scope["server"]`) loopback, or a unix socket; a scope that does not
say where it was answered refused rather than believed. Every write was then judged as
credentialed, against the origin of the `Host` it named: `Origin` equal to
`http://<host>`, port included, or no `Origin` and `Sec-Fetch-Site: same-origin`. "The
`Host` header is what says otherwise, and it is why this check covers reads: a rebound name
is read from, not only written to."

Restore the check as middleware in front of the app, with the tests of `test_local_mode.py`
(two `Host` headers, a unix socket, a scope with no server, a server answering off
loopback), and mark the rule in `sign-in.md` as not yet served until it is.

### security-05: `/ui/` serves dotfiles

`web/app.py` mounts a bare `StaticFiles` on the built interface. Verified: `GET /ui/.env`
answered 200 with the file's contents. A `.env` beside the bundle is a deployment's database
URL, and a `.git` directory beside it is the whole source. Neither the page nor an asset
carries a `Content-Security-Policy` or a `Cache-Control`, a missing file answers Starlette's
`{"detail": "Not Found"}`, and the not-built page answers 200. `docs/specs/frontend.md:84-97`
still states the CSP, the 405 with `Allow` and the dotfile rule as served.

Legacy (`L api/ui.py:98-112`, `:138-239`, `:255-463`): `NothingHidden` answered 404 for
any path with a segment that starts with a dot, the same answer as a file that is not there;
`UiHeaders` wrapped the mount, set the CSP, `X-Frame-Options` and `Cache-Control` with
`setdefault`, and caught the `HTTPException` that `StaticFiles` raises to build the 404 and
405 inside the mount, so a refusal carries the policy too; an asset under `assets/` whose
name ends in exactly eight hash characters was cached for a year and immutable ("exactly
eight, not at least: the digest's own alphabet includes the hyphen"), any other asset
`no-cache`, everything else `no-store`; an installation without the interface answered a
503 page with no script.

Restore the mount's wrapper with the tests of `tests/unit/test_ui_routes.py`, and put the
rules in `docs/architecture/web.md` under "The interface". The digest rule lives today only
in `frontend/vite.config.ts:486-493` and `scripts/check-wheel.sh:100-103`; write it down
in `frontend.md`.

### security-06: Authorization codes and `state` reach the access log

The new command runs uvicorn with its default access log, which writes the full request
line with its query. One of this platform's paths carries credentials in one:
`GET /auth/callback/<provider>?code=...&state=...` is an authorization code and the `state`
that goes with it, written to a file on every sign-in. A code is single use and short lived,
but a log is read by more people than a database and kept for longer.
`docs/deployment.md` section 8 promises that query strings do not reach the log.

Legacy's `NoQueryStrings` (`L cli.py:233`, installed by `configure_logging` at `:259` on
`uvicorn.access`, replacing rather than adding filters) cut every access-log line at the
`?`. The path, the method and the status stayed. Every path rather than the sign-in paths,
because "a rule that named the sign-in paths would be a list to keep in step with the
routes, and the query string of every other request is of no use in a log". Tests:
`tests/unit/test_cli.py:717`, `:728`, `:736`.

Restore the filter in `web/cli.py`, where logging is configured, with the test that a
callback's line in the access log holds neither the code nor the state.

### security-07: Validation errors echo the request back

A request that fails validation answers FastAPI's default body. Verified: a `PATCH` with
`{"title": {"sk-secret": 1}}` answered
`{"detail": [{"type": "string_type", "loc": [...], "msg": ..., "input": {"sk-secret": 1}}]}`.
That repeats whatever was sent, keys included, so a secret pasted into the wrong field by
a client or a script comes back in the response, is shown by the frontend (which reads
`detail` and cannot read this shape, `frontend/src/api/client.ts:65-71`, `:206`) and lands
in whatever log or proxy keeps responses. Pydantic's `msg` often quotes the value too.

Legacy (`L api/errors.py:156-224`, `:417-478`; `L api/schemas.py:564-589`) turned a
`RequestValidationError` into one `InvalidValueError("<location>: <rule>")`: the rule a
sentence of ours keyed on pydantic's error `type`, with one fallback sentence for an
unknown type and pydantic's `msg` and `input` never used; unknown keys counted, not named,
"because a secret pasted into the wrong tool arrives as a key as readily as as a value"; a
location keeping only its string pieces, so a character offset and a list index drop out,
clipped to 40 characters, the whole detail to 300 characters and five fields; the one-form
rule for a turn checked in the route rather than by a discriminated union, so no tag the
sender wrote gets into a location.

Restore the handler with the tests of `tests/unit/test_api_errors.py:365-422` and
`tests/unit/test_conversation_routes.py:745-927`, and add the guard test legacy's docstring
names and never had: no request model puts a sender's word in a location.

### security-08: 5xx bodies carry the exception's text, and refusals carry no security headers

`web/app.py:435-438` answers every `ControllerError` with `str(exc)`, 500s included. The
mapping (`:71-79`) covers 7 of the 12 contract classes; `ConfigError`,
`MissingSecretError`, `UnreachableProviderError`, `UnknownEngineError` and `TurnLostError`
fall to 500 with their message, and a `ConfigError` from the pool reads "the database could
not be opened: <the driver's sentence>" (`controller/adapters/postgres/pool.py:66`), which
can name a host, a database or a role. There is no handler for bare `Exception`, so an
unhandled error is answered by Starlette's outermost middleware with none of our headers.
Verified: `/health` carries no `X-Content-Type-Options: nosniff` and no `Referrer-Policy`;
a `POST` to `/api/conversations/{id}` answered 405 with `Allow: GET`, which hides `DELETE`
and `PATCH`. The frontend shows `detail` to the person, so whatever is in it reaches a
screen.

Legacy (`L api/errors.py:4-53`, `:243-398`, `:481-578`; `L api/protection.py:859-877`):
one status per error class found through the MRO, with a test that every class has an
entry of its own and no stale entry survives; every 5xx body `{"error": "InternalError",
"detail": "the request could not be served"}`, "a 500 is a mistake of ours, and its message
is written for an operator: it may hold a query, a row, the name of a variable"; the cause
chain and the innermost frames to the log, on one line, escaped and bounded; everything
that is not there, or not yours, answered with one body byte for byte, "the difference
between no such id and not yours is the whole of what an attacker wants from an id"; a
handler for bare `Exception` answering the same generic JSON and handed the security
headers, because Starlette builds that response beyond our middleware; Starlette's own 404
and 405 put in the same shape, the 405's `Allow` rebuilt from every route whose pattern
matches the path; `nosniff` and `Referrer-Policy: same-origin` on every answer through
`setdefault`, the latter "so that a path of ours, which may name a conversation, is not
sent to whatever a person clicks through to".

Restore the handlers and the headers, map every contract class, and port the canary test
(`tests/unit/test_api_errors.py:140-237`): routes that raise with `SECRET_IN_A_BUG` in the
message, the canary absent from the body, present in the log, the log one line, and
"Traceback" and the class name absent from the body.

### security-09: No body bound, and the body is read before anyone is identified

FastAPI buffers and parses a request's body before any dependency runs, `current_user`
included, so anyone can make the server read and parse any amount with no session. There is
no bound at all. `web-01` names "the one-mebibyte body bound" and `docs/specs/wire.md:153`
says it is refused "on the declared length before a byte of it is read"; neither says what
happens to a body sent with no length, or that the bound must stand in front of
authentication.

Legacy's bound (`L api/protection.py:166-204`, `:384-419`, `:538-710`) was pure ASGI
middleware on writes: a declared `Content-Length` over 1 MiB refused before a byte is read,
two `Content-Length` headers refused, leading zeros stripped and the digit count checked
before `int()` ("CPython refuses to convert a decimal of a few thousand digits and raises a
`ValueError` that would be a 500 over a header somebody chose the length of"); a body with
no length counted in a wrapped `receive`, the chunk that would cross the bound never handed
over, the app seeing `http.disconnect` and every later `receive` answering a disconnect at
once without waiting ("a request that hangs until something else times it out"); a wrapped
`send` dropping whatever the app made of the cut body and the middleware sending the 413;
`GET` not wrapped, because a streaming response listens on `receive` to hear a disconnect.
Tests: `tests/unit/test_api_protection.py:290-575`.

Restore it as the first middleware, before the write checks, with those tests. It belongs
with `web-01`, which this item details.

### security-10: A route's permission is declared by hand, and nothing checks the next one

Every `/api/` route in `web/app.py` writes `user: User = asking` or `dependencies=[asking]`
by hand. A route added without it answers anyone, and no test would notice.
`docs/specs/sign-in.md:68-70` requires that "every route declares the permission it needs,
and a test asserts that every route declares one"; `backend/pyproject.toml:11-21` pins
FastAPI below 0.142 for the sake of legacy's walk, whose stated reason goes when legacy
does.

Legacy (`L api/access.py:4-55`, `:79-107`, `:220-343`, `:389-426`; `L api/web.py:133`,
`:193`; `tests/unit/test_api_access.py:131-500`): a route declares `public()` or
`signed_in()`, a dependency whose callable carries the permission's name. `undeclared()`
walks every list a router keeps routes in, into included routers and into mounts, and
reports anything that is not a declared `APIRoute`: an undeclared route, a plain Starlette
route, a websocket, a mounted app whose routes cannot be read, a `frontend()` group, and a
declaration passed to `include_router` rather than written on the route. A short
hand-written list (`/openapi.json`, `/ui`) excuses non-API things by name and never excuses
an `APIRoute`. A backstop, `unknown_route_lists`, reports any other router attribute
holding routes, so a framework that grows a new place for routes stops the deployment: "a
check that only understands the routes it was written for is a check that stops working
the day somebody adds another kind". `create_api` ran the check when it built the app, and
the lifespan ran it again at start, "so the door cannot be left open by a branch nobody ran
the test on".

Restore the declaration and the walk, at build and at start. For the port: FastAPI 0.141
keeps the frontend group in `router._frontend_routes` as one object, not a list, as well as
in `_low_priority_routes`. Put the rule in `docs/architecture/web.md` under "Who may ask";
the declaration is where a permission argument goes when roles arrive.

### security-11: Sign-in secrets in reprs, and stored rows never scanned for them

`SignedIn.secret` (`web/sign_in.py:246`) is in the dataclass's default repr, so a log line,
a traceback or a crash report that prints the record prints the session secret, and
"whoever reads it can finish someone else's sign-in". Legacy hid every secret-bearing field
with `field(repr=False)` (`L application/sign_in.py:120-136`: the state, the authorization
URL and the session secret) and tested it two ways (`tests/unit/test_signin_flow.py:1007`,
`:1048`): every stored row, dumped through `store.everything()`, scanned for the raw secret,
and every repr scanned too. The deployment rehearsal did the same end to end, grepping every
secret it was given across the server log, every response and every database row
(`scripts/rehearse_deployment.py:467-499`).

Hide the fields, port the two tests to `web/sign_in.py` and the credential suite, and give
`tests/integration/test_web_sign_in.py` an end-to-end search of every table for the cookie
secret, the `state` and the client secret after a whole sign-in.

### security-12: A failed sign-in leaves the login cookie, caches, and logs the provider id raw

`web/app.py:462-464` answers a refused sign-in with a 302 and no `Cache-Control: no-store`,
and leaves the login cookie in place; an exception inside `flow.complete` becomes a 500
with the cookie still set; `/auth/session` carries no `no-store`; `web/app.py:517` builds a
log detail from the `provider` path parameter without checking its shape, so a crafted path
reaches the log ("unchecked it would reach a message, and a message reaches a log");
`web/cookies.py:34` rounds `Max-Age` down with `int()`, and `Max-Age=0` is how a cookie is
deleted.

Legacy (`L api/auth_routes.py:65-79`, `:114-206`, `:241-320`; `L api/cookies.py:11-23`,
`:57-72`; `tests/unit/test_api_auth_routes.py:205-598`): every `/auth` answer carried
`no-store`; navigations answered 303; every way a sign-in failed cleared the login cookie,
an unexpected exception included ("a state cookie left behind would be offered to the next
callback that arrives"), the handler catching `Exception` so `CancelledError` still passed;
the provider path parameter was checked against the id pattern before anything used it,
and a misshapen one went to the sign-in page as `unknown_provider`; a finished sign-in
landed on `public_url` plus `/ui/` plus the hash of `return_to` and nothing else from it;
cookies were `Lax`, since `Strict` "would drop the `state` cookie on exactly that request,
so no sign-in would ever complete"; `Max-Age` never rounded down to 0.

Restore all of it, with those tests, and give `docs/specs/sign-in.md` one line on why the
cookies are `Lax`.

### security-13: A repeated JSON key is accepted

`{"title": "Safe", "title": "Evil"}` is parsed by the framework as the last value. "It is a
write that asked two things and was answered on one of them, and a reviewer, a log or a
proxy reading the same bytes may well pick the other." Nothing in the new web layer refuses
it, at any depth. The trap legacy avoided is still set: `controller/contract/domain.py:22`
makes `InvalidValueError` a `ValueError` too, so a `JSONDecodeError` caught as a `ValueError`
would swallow the refusal.

Legacy's `read_once` (`L api/protection.py:732-856`; `tests/unit/test_conversation_routes.py:
929-1043`): a dependency parsed the cached body again with an `object_pairs_hook` that
raises on a repeated key; `RecursionError` became a 422, since there is a narrow band of
nesting where the framework's parse succeeds and the deeper one runs out of stack;
`JSONDecodeError` was caught by its own class; a test walked the routes and required every
route with a body to declare the check, and asserted that list is not empty.

Restore it on every route that takes a body, with the walk that proves the declaration.

### security-14: No cap on pending sign-ins, and `busy` is never raised

Anyone may begin a sign-in without signing in: `GET /auth/login/<provider>` stores a
pending login. The new `begin` deletes the expired pending logins before it inserts, so
the table holds the last ten minutes of whatever arrives, with no bound on how much that
is; `busy` is in `web/sign_in.py`'s codes and nothing raises it. The spec's known limit
says the cap "bounds the table; it does not protect sign-in", and the rate limit belongs in
front of the platform. Both halves need the cap to exist. `auth-plan.md` deferred it to
"stage two, housekeeping" and `cross-04` never received it.

Legacy (`L datastore/credentials.py:19`, `:105-124`, `:266`, `:355`; `L application/
sign_in.py:350`; `L domain/identity.py:32`, 10,000): count the unexpired sign-ins and
insert one in a single transaction under `pg_advisory_xact_lock((space << 32) |
'pending_logins'::regclass::oid)`, because `SELECT count(*)` sees committed rows only, so
"ten transactions inserting at once each count nine and all ten get in" at any isolation
level; the lock rather than `SERIALIZABLE`, since "a forgotten reset would silently change
the isolation of everything else that connection later does"; the insert `INSERT ... SELECT
... WHERE (count) < $limit ON CONFLICT DO NOTHING RETURNING`; the hash's shape checked in
Python before the statement, because "a constraint only runs when a row is really
inserted" and at the cap nothing is; each lock kind in a high half of its own, the low half
the OID of the thing locked, so two deployments or two test schemas do not queue behind
each other. The new schema lock uses `1382508616` (`controller/adapters/postgres/schema.py`),
so the cap's space must differ. The contract suite's cap tests
(`tests/contracts/credential_store.py:352-397`) need a warm pool to bite
(`tests/postgres.py:143`).

Add the cap to the credential port and both implementations, raise `busy` from `begin`,
and add the sweeps of `user_sessions`, `pending_logins` and `api_tokens` to `cross-04`.

### security-15: Identity-provider answers are unbounded, and a rate limit reads as a refusal

`web/oidc.py:266-271` reads `response.content`, which `httpx` inflates in full with no cap,
and sends no `Accept-Encoding`; a provider, or whoever answers for one on a compromised
network path, can make the sign-in path allocate without bound ("half a megabyte of gzip is
sixty-seven megabytes of memory before anybody looks at the number"). `_token_answer`
(`:247-263`) makes every 4xx `provider_refused` and repeats the provider's `error` and
`error_description` into the log unbounded, so a provider's 429 tells the person their
sign-in was refused and "sends them to fix something that is not broken".

Legacy (`L adapters/identity_provider.py:20-33`, `:97-143`, `:523-609`, `:625-688`): every
request sent `Accept-Encoding: identity` and an answer carrying a content encoding was
refused unread; a declared `Content-Length` over 256 KiB was refused before the body was
read, and the raw stream was counted off the wire and abandoned past the bound; 408, 425
and 429 were `provider_unavailable`, not `provider_refused`; the provider's words were cut
to 120 characters before they reached a message; a fixed, honest `User-Agent` was sent.

Restore the bound, the encoding rule and the retryable statuses in `web/oidc.py`, and run
the stand-in's gzip bomb, oversized and overstated-length misbehaviours against them.

## Cross-component features

### cross-01: Fork

`fork_conversation` in the controller: a new conversation with the records
up to the message asked, and the engine's `fork` at that answer's checkpoint. In the
engines: Pydantic AI copies its snapshot rows up to the checkpoint, so the source's ids
stay valid in the target as the spec says; LangChain seeds the target thread with
`aupdate_state` from `aget_state` at the checkpoint, and the source's ids are not valid
in the target unless the saver's rows are copied, which the PostgreSQL saver can do and
`InMemorySaver` cannot without reaching inside it. Decide which the spec keeps.

### cross-02: Context management

Each engine keeps the history within the model's window by its
own means: `SummarizationMiddleware` on LangChain, a history processor on Pydantic AI,
sized from `ModelConfig.context_window`, with legacy's numbers (`SUMMARIZE_AT`,
`KEEP_MESSAGES`, `TRIM_AT`, `CHARS_PER_TOKEN`, `DEFAULT_CONTEXT_WINDOW`). The vendor's
prompt cache placed on the system prompt. Any middleware or processor writes updates of
its own, which the engines' event translation must tolerate (see LangChain below).

### cross-03: Resume

The spec's `resume=True`: an interrupted turn run again from the engine's
partial work without repeating tool calls. Both engines run the turn again from the
checkpoint today. Needs a turn marked interrupted, which block 5's lease does, and
the controller to pass `resume` when the same question is asked again.

### cross-04: Housekeeping

Trash expiry and retention in the controller, on the process's own
schedule, each calling the engine's `forget`, which is the only way memory is deleted.

### cross-05: Titles

A conversation's title from its first exchange: legacy derived it in `core`;
the spec says ask the model, which needs a sessionless call the engine contract does
not have. Decide, then add the operation or keep the derivation.

### cross-06: Tool names across servers

Both engines list tools from several MCP servers; a tool
of the same name on two servers collides. Legacy prefixed `<server id>_<tool>` in both
frameworks. Restore in both engines, and decide whether the prefix is shown in the UI.

### cross-07: A tool result the server marks as an error

**A tool result the server marks as an error** becomes a message the model reads, not a
failure of the turn, in both engines (`handle_tool_errors` / `tool_error_behavior`).

### cross-08: Usage and cost

**Usage and cost**, attachments, and tool approval: out of the engine contract today;
planned, not specified.

## Data model and PostgreSQL

Findings of the review of `design/data-model` (blocks 5 and 6), numbered as the review
ranked them. Findings 1 to 7 were answered in `data-model.md`, the path of one message,
`data-model-plan.md` and `postgres-plan.md`; these are the rest.

### data-08: The path doc and the lease

`the-path-of-one-message.md` says it describes the
controller after block 5, but its lease tick and its `request_cancel` through the
store are stage two: mark them so, or move them here. Add
`cancel_requested_at` to the `Turn` record when cancel goes through the store. Until
then, with several processes, a cancel that lands on a process not running the turn
returns 204 and cancels nothing; when it does go through the store, the store
checks the owner before it writes the flag.

### data-09: Expiry enforced

The sweep deletes `turn_events` past `expires_at`, one statement
on `turn_events_expires_at_idx`, and every read treats an event past `expires_at`
as absent, whatever the store's deletion lag: DynamoDB deletes "typically within a
few days" and returns the item from a `Query` until then, Firestore within about a
day, Cosmos hides it at once. Correct `aws-serverless.md`'s "TTL deletes expired
events, user sessions and pending logins without code": a sign-in read checks
expiry itself. Give all of a turn's events one `expires_at`, the turn's start plus
the retention, so that a late replay never starts in the middle of a turn. Say in
`wire.md` that reasoning is kept as events until they expire, not "never stored".

### data-10: The document's bytes and its version

`data-model.md` says every store keeps the
same bytes; `jsonb` reorders keys, drops whitespace and re-renders numbers, so say
"the same document, equal after decoding", and align `aws-serverless.md`'s "JSON
text, not a map" with the port's `Mapping`. Decide what a reader does with a newer
document during a rolling deploy, a minor version it reads past and a major one it
refuses, since "a new key moves the version" makes every new message unreadable to
the old process, and refusing one message breaks the thread walk.
`messages_role_is_a_role` is a CHECK, so a new role is a schema change, not an
additive one: say so, or drop the CHECK. The schema hash does not cover the
document format.

### data-11: An index on `turns (session_id, follows)`

Without it the cascade from `sessions`
checks `turns_follows_fkey` once per message by a range scan of the session's
turns: measured at 55 ms for 2,000 messages, 718 ms for 8,000, and 87 ms with the
index. It also serves the turns of a question. One line in `schema.sql`, which its
own rule for the parent key asks for.

### data-12: The path doc's SQL

Its `INSERT INTO turn_events` omits `expires_at`, which is
`NOT NULL`; its `start_turn` checks no `owner_id`, though `data-model.md` says
PostgreSQL checks the rest of the address; a deleted session raises
`TurnActiveError` (409) there and is not found (404) in `data-model.md`, the schema
and the wire; `NOTIFY '<turn> end'` names no channel; `start_turn(question, turn)`
is not the plan's signature. Fix the doc, and name `SessionNotFoundError` for a
hidden session in the plan's `start_turn`.

### data-13: Decisions that reverse a spec or an ADR, unrecorded

`core.md` says no framework
checkpointer and no framework tables in the deployment's schema, and ADR 0005 keeps
memory in `runs.engine_state`; block 6 builds engine-owned checkpoint tables, and
`data-model.md` cites ADR 0005 as its authority. ADR 0005 says the transcript shows
a cancelled turn's tool calls; the model stores no message for one, so a call with
side effects survives only in events that expire. ADR 0005's checkpoint rule, the
nearest finished answer on the path above the question, is dropped: a turn under a
question (after a failed turn, or on an edited first question) passes
`checkpoint_id=None`, which LangChain reads as the thread's latest and Pydantic AI
as an empty history. Write each decision down, in the doc or a superseding ADR,
and state the checkpoint rule.

### data-14: Tree and turn invariants, enforced and owned

A self-parent and a two-node
cycle pass the composite key, which is checked at the end of the statement: add
`CHECK (parent_id <> id)` and make the thread walk cycle-safe. "A turn produces at
most one answer" is held by nothing: `turns.answer_id UNIQUE`, with a key to
`messages (session_id, parent_id, id)`, holds it at no extra index. "`follows` is a
user message" is held by nothing, and the schema's comment that a key cannot say
so is wrong: a constant `follows_role` column with a composite key does. Decide
whether the store or the controller owns these rules (the schema says the store,
`aws-serverless.md` the controller), list them under the rules every store keeps,
and test them in the contract suite. Decide the visible thread while a
regeneration runs ("the newest message" against the wire's "messages end at
`follows`") and after a failed turn, and let `regenerate` work on a question whose
only turn failed. Editing the first question makes a second root: the tree is a
forest, and `data-model.md` should say so.

### data-15: Opening a session is one snapshot

Messages and the active turn are read
separately, so an answer can show twice, or be missing with no run id; the frontend
assumes a snapshot. Read both in one transaction, or the active turn first and
the messages up to it.

### data-16: Cascades, and what keeps the schema's claims

No port operation deletes a user,
so that deletion depends on the cascade alone, against the schema's header, and
the purge relies on the cascade for messages, turns and events. On DynamoDB the
sparse listing index hides trashed sessions, and the sweeps for hidden sessions
and expired leases need a `Scan` or an index. `schema.sql` says, in the present
tense, that the server refuses a wrong hash, and nothing keeps it: port legacy's
schema tests (one row, the hash, a half-applied file, four `db init` at once);
the advisory lock itself is block 6's step 1.

### data-17: Where deferred work lives

`master-plan.md` and this plan listed "the sweep at
start", which with two processes on one database would interrupt live turns; the
lease replaces it (`aws-serverless.md`), and both now say so. `controller.md`'s
`open` and `close`, and `web.md`'s `cancel_turn` and `watch_turn`, describe the old
lifecycle; block 5's step 6 rewrites `controller.md`, and `web.md` waits for this
stage. Block 5's steps 1, 3 and 4 cannot
each leave the suite green as written: no turn id exists until step 3, web has
none until step 6, and step 3's port forces step 4's runner; the plan says so, or
regroups them.

### data-18: One name for each operation, and one address

`end_turn` in `schema.sql`'s
comments and the three serverless notes, against the plan's `finish_turn`;
`request_cancel` in the path doc, in no port; `delete_session(session, at)`
against `hide_session(owner, session, at)`; `start_turn(question, turn)` against
`start_turn(owner, turn, question)`. Turn operations take two ids in the path doc
and the AWS and GCP notes, three in `data-model.md` and the plan, and the turn id
alone in the AWS sketch's `events_after` and `end_turn`, which cannot find `EVT#`
items under `PK = SESSION#` without an index. `azure-serverless.md` says PostgreSQL
may ignore the owner, and `data-model.md` says it checks it. The AWS note keys
messages and turns by `(session_id, id)` and omits `turns.model`.

### data-19: The wire with turn ids

`agui.py` sets `thread_id = run_id`; once the run id is
the turn's, say in the planned section that `thread_id` is the conversation's. Say
what "two headers carrying two values" means on the wire. `watched()` waits for
the first event before it sends the headers, and CloudFront gives up after 30 s on
a cold or lost worker: send the headers first, or bound the wait. "A session that
is not its caller's owner's is not found" meets privacy.md's share links and
projects; say which wins.

### data-20: `position` as `bigint`

**`position` as `bigint`**, as legacy had it, and `after` bounded at the edge: with
an `int4` column asyncpg raises on `?after=3000000000` or a large `Last-Event-ID`,
a 500 where an empty replay or a 422 is due.

The second round of review, of the answers to the first seven, left these:

### data-21: The lease margin on a serverless dispatch

Block 5's margin is a minute past
the turn's timeout, counted from `start_turn`; a dispatch that is late eats it, and
Lambda's minimum event age is 60 s, Cloud Tasks' dispatch deadline bounds the
handler's run and not its delivery, and the GCP note says otherwise. Size the
margin for the dispatch, or have the claim write the lease. DynamoDB cannot
condition a `PutItem` on another item, so the conditioned append, the hide's check
of the `ACTIVE` marker and `end_expired_turn` are `TransactWriteItems` there, at
about four times the write units; the sketches in `aws-serverless.md` and the path
doc say "one conditional write".

### data-22: Text split across pieces, and the engines' own stores

Block 5 cleans each
document alone, so a surrogate pair split across two pieces becomes two U+FFFD in
the events and in the answer; legacy's `publishable` held the high half back for
the next piece (`legacy/domain/values.py`), and `data-model.md` still promises
"what a person watched arrive is what is stored". Pydantic AI's
`ModelMessagesTypeAdapter.dump_json` refuses a lone surrogate before `json` can
help, and LangGraph's serializer writes `?` for one: the engines clean what they
save, with a lone-surrogate case in the engines' contract suite over PostgreSQL.

### data-23: Writes fenced by the claimant, and an idempotent finish

Block 5 fences a
runner's writes by the turn's state and its lease; `turns.answer_id`, written by
the claim and checked by every append and the finish (item 14), fences them by the
claimant too. A `finish_turn` retried after a lost acknowledgement gets
`TurnLostError` although the turn finished: accept a matching repeat (same state,
same answer id), and decide who retries a write asyncpg reports as a lost
connection, since block 6 retries nothing.

### data-24: `NOTIFY` once per interval

A `pg_notify` in every append serialises those
commits across the cluster (measured: 16,161 tps without, 3,118 with, at 32
clients). Watchers re-read the table anyway, so notify at most once per turn per
interval.

### data-25: Cancel through the store

Block 5 refuses a cancel of a turn another process
runs (409), and the frontend's 409 copy says "Stop the answer first", the one
action that then does nothing. `cancel_requested_at`, read by the runner at each
lease renewal, removes both; `Turn` gains the field then.

### data-26: A moved agent

Block 5 runs a session's turns on the engine it records and
builds it on demand; `agent-engines.md` and ADR 0005 say a moved conversation
starts again on the new engine. Decide which, and if the latter: `create` on the
new engine, no checkpoint, the new engine recorded, and `forget` on the old.

### data-27: Drift the two rounds of review left

`aws-serverless.md` still has cancel
through the store unqualified and asks block 5 for "cancel and lease through the
store"; `azure-serverless.md` says `close` waits where block 5 waits and then
interrupts; this section is ordered by review rank, not by what it touches;
`wire.md`'s "in these ways and in no others" omits the two 409s block 5 adds and
AG-UI's `thread_id`; the position of the supplied `turn_ended` is stated only in
the plan; `finish_turn` takes no `expires_at` for its last events; and the nits:
text splitting differs between `data-model.md` and the plan, "an id is a uuid"
would reject `call_id` and `checkpoint_id`, "(hours, not days)" against 24 hours,
`turns_error_only_when_failed` also holds `interrupted`, and "Web builds no
engine" against `create` and `forget` running in web.

A third round, of `design/data-model` at the end of block 6, kept what is on the users'
path or is not undone by a reload, and set the rest aside: an empty session left by a
first message refused for its model, the stop button racing a turn that is finishing,
`limit=0` on the listing, the two stores disagreeing on a repeated position, `follows`
not checked to be a user message, the engines' memory relying on the pool's `json`
codecs, and the schema hash stamped from the pin rather than from the file applied.

### data-28: A session on a removed agent breaks the listing and the open

`web/app.py`'s
`list_sessions` builds `defaults` from the agents the configuration names today and
indexes it by each session's `agent`, a `KeyError`; `default_model` is a `next()`
with no default inside a coroutine, so for an agent that is gone the exhausted
generator surfaces as `RuntimeError`. Both are 500s, on every load of the history
and on every open of that conversation, for as long as the user has one session on
an agent the operator removed; the row never renders, so the user cannot delete it
from the interface. `schema.sql` promises the opposite: the operator may remove the
agent, and the session keeps its row. In the controller, `run_turn` indexes
`self._config.agents[session.agent]` and `self._config.models[turn.model]`
unguarded, where `_engine()` already builds an engine the configuration no longer
names. Decide what such a session shows (its stored agent id and the model of its
last message, or a refusal naming the agent) and apply it to the listing, the open
and the runner, with a test over a configuration that lost an agent.

### data-29: `watch_turn` reads twice per wake

After `wait_for_events` returns, the loop
queries `events_after` to test for emptiness and then `continue`s to the top, where
the same query runs again: two identical SELECTs on `turn_events` per wake of every
watcher, on the one path every open conversation sits on. Bind the list the second
read fetched and feed it to the `for`, or restructure the loop so the read after the
wait is the one iterated.

### data-30: The dispatcher discards a runner's exception

`InProcessDispatcher._settled`
calls `task.exception()` only to silence asyncio's "never retrieved" warning. A
`run_turn` that raises before the claim (the `KeyError` of item 28, a store error)
is logged nowhere; the turn stays `running` until `lease_until`, the timeout plus
the minute of margin, the session answers 409 to every new turn meanwhile, and
watchers wake every `WAIT_SECONDS` until one's `end_expired_turn` ends it as
`interrupted`. Nothing reaches it under normal operation, and it turns every
failure that does into a silent lock-out. Log the exception, and end the turn
there, as `cancel_turn` does with `_end_if_running`.

### data-31: The engines' DDL outside the lock

`init_database` runs `create_schema` under
`pg_advisory_xact_lock`, so two `db init` at once serialise on the schema, and then
runs every installed engine's `setup()` outside any lock: both pass the schema and
then race `CREATE TABLE IF NOT EXISTS` on the engines' tables, where one fails with
`duplicate key value violates unique constraint pg_type_typname_nsp_index` and exits
non-zero on a database that is fine. Hold the same lock around the setups. It bites
only where init runs concurrently, as two replicas' init containers do; the "four
`db init` at once" test item 16 asks for catches it if it covers the engines' tables.

## langchain_engine

Findings of the review of `feature/langchain-engine-6`, none blocking.

### langchain-01: `events_of` reads `update["messages"]` for every node update

`events_of` reads `update["messages"]` for every node update. With `create_agent` and no
middleware both nodes always carry messages; any middleware, including the context
block above, writes updates without them and the line raises inside a turn. Read it
with a `get`.

### langchain-02: Tool names are not prefixed by server id

Tool names are not prefixed by server id (cross-component above).

### langchain-03: `Done` takes the thread's latest state after an earlier checkpoint

`Done` takes the thread's latest state after a run started from an earlier checkpoint.
Correct today because LangGraph's latest is the newest write; it is the one place the
engine relies on "the latest" rather than on an id of its own. Comment it, and test two
turns continued from the same earlier checkpoint.

### langchain-04: `force_tracing_off` pops two LangChain variables from the environment

`force_tracing_off` pops two LangChain variables from the process environment at
construction. Legacy did the same; keep the one comment that says why.

### langchain-05: Sessions are a set beside the saver

Sessions are a set beside the saver; `create` and `exists` never ask the saver. Fine in
memory; the PostgreSQL saver should be the one source of which threads exist.

### langchain-06: A live test against a real MCP server

A live test against a real MCP server is still to write; the unit tests prove only
the connection's header and timeout.

### langchain-07: Keep the timeout as written

Keep the timeout as written: one deadline around each `await` on the stream, with
`aclosing` releasing it. The plan's `asyncio.timeout` around the whole run would fire
inside the caller's handling of a yielded event.

### langchain-08: Extend the shared suite rather than each engine's own tests

The shared suite `tests/contracts/engine.py` is the one description both engines are
held to; extend it rather than each engine's own tests.

## pydantic_ai_engine

To be filled by the review of its block. Known from the plan:

### pydantic-01: Copy the LangChain engine's timeout shape and subclass the shared suite

Copy the LangChain engine's timeout shape and subclass the shared suite as written.

### pydantic-02: The snapshot dict is the engine's own memory; `fork` by copying rows

The snapshot dict is the engine's own memory; `fork` by copying rows is cheap here
(cross-component above).

### pydantic-03: A live test against a real MCP server

A live test against a real MCP server, as for LangChain.

## controller

From the echo and config blocks, outside the happy path:

### controller-01: The refusals the contract names

The refusals the contract names: ownership (a conversation that is not the user's is
not found), not found, unknown agent or model, an invalid title or cursor, empty text.

### controller-02: One active turn, the timeout, a bounded cancel, the lease and the sweep

One active turn per conversation (`TurnActiveError`), the turn timeout, a bounded
cancel, the lease renewed for long turns, and the sweep for turns a dead process
left running that nobody reads (block 5 ends the ones a reader finds, by the lease).

### controller-03: A cancel that lands before the runner starts

A cancel that lands before the runner starts leaves the turn active with no
`TurnEnded`.

### controller-04: A rename during a turn is overwritten when the turn ends

A rename during a turn is overwritten when the turn ends: the runner writes back the
conversation as it was when the turn started.

### controller-05: An edit of a conversation's first question has no parent

An edit of a conversation's first question has no parent; `send_message` requires one.

### controller-06: Paging answers a plain slice with no cursor

Paging: `list_conversations` answers a plain slice with no cursor.

### controller-07: The visible thread after edits

The visible thread after edits: the path to the newest message; the tree rules legacy
kept in `core` (`ConversationTree`) say what may follow what.

### controller-08: `parse_config` validates shape only

`parse_config`: a table or entry that is not a TOML table raises a plain error, not
`ConfigError`; missing fields are reported in the dataclass's own words; URLs, env
names, bounds and text are not validated.

### controller-09: Event positions restart at 1 per turn

Event positions restart at 1 per turn and the last turn's events stay until the next
turn starts; `watch_turn` relies on it. Decided in block 5: positions are per turn,
keyed by the turn's id.

### controller-10: One `asyncio.Condition` wakes every watcher on every append

One `asyncio.Condition` for the whole in-memory store wakes every watcher on every
append.

### controller-11: The user's id is a fresh uuid on each start with in-memory storage

The user's id is a fresh uuid on each start with in-memory storage.

## web

From the echo and config blocks, and the wire's "not yet served" list:

### web-01: Request protection

Request protection: CSRF and origin checks on writes, the one-mebibyte body bound, the
security headers and the Content-Security-Policy on the served UI, hashed asset caching.

### web-02: The error mapping to statuses and fixed bodies, and log redaction

The error mapping to statuses and fixed bodies, with the test that every error class
of the controller's contract has a status; log redaction of keys and secrets.

### web-03: The wire's refusals and the stream's edges

The wire: a body that is neither shape of a turn or both is refused (422); a second
turn while one runs (409); `Last-Event-ID` and `after` disagreeing (422); the keep-alive
comment on a quiet stream; the terminal event on a re-attach at or past the end of a
turn that ended; a position a running turn has not reached (422); `ended_badly` on the
opened conversation after a failed or cancelled turn; reasoning brackets rebuilt on a
re-attach.

### web-04: The set-model route stores nothing

The set-model route stores nothing; decide whether it stays.

### web-05: `AgentSummary.engine` and `ProvenanceView.engine` are empty

`AgentSummary.engine` and `ProvenanceView.engine` are always empty strings.

### web-06: The frontend's `folded()` accepts two shapes of tool result

The frontend's `folded()` accepts a tool result inside the answer and in a separate
tool message; `docs/specs/legacy/conversations.md` still describes the latter.

### web-07: The legacy subcommands and `db init`

`robinauts db init` and the other legacy subcommands are gone from the console script
until the PostgreSQL block adds `db init` back.

## Packaging and operations

### packaging-01: The wheel hook, the demo, the rehearsal and the CI scripts

The wheel hook, the demo (`demo/robinauts.toml.in` uses `mcp_servers` and
`engine = "langgraph"`), the deployment rehearsal and the CI scripts, all pointing at
legacy paths or legacy subcommands.

### packaging-02: `check-frontend.sh` refuses Node 22, and one timing test is flaky

`scripts/check-frontend.sh` refuses Node 22; `.nvmrc` asks for 24. One frontend timing
test in `sse.test.ts` fails on slow machines.
