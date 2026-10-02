# Sign-in, users and roles

People sign in through the company's identity provider. There are no local
passwords. The design is neorc's
(`neorc/docs/working-notes/sso-plan.md`), re-implemented in this project's
[layout](../layout.md): the code is written fresh, from neorc's design and
with neorc's code open, and that is recorded in
`docs/legal/ip-clearance.md` when it lands.

## Providers

- Google and Okta, over OpenID Connect. One generic client serves both:
  Google is recognised by its issuer; Okta is any other issuer, with a
  `groups_claim`. Other providers may work and are not claimed to.
- The client is written on `httpx`. There is no OIDC, JWT or crypto
  dependency.
- Authorization code flow with `state`, `nonce` and PKCE (S256).
- Discovery happens at the first sign-in, not at start-up; a success is
  cached, a failure is not. The issuer it names must equal the configured
  one. The authorization and token endpoints must be `https`, loopback
  excepted.
- **The ID token signature is not verified.** The token comes from the
  token endpoint over TLS, which OIDC Core 3.1.3.7 allows for the code
  flow. Claims are checked: `iss`, `aud`, `azp`, `exp` and `iat` (60 s
  skew), `nonce`, `sub`. If a token ever has to be accepted from anywhere
  else, this no longer holds: that needs a JWT library, JWKS, and a
  decision of its own.
- Google's `email_verified` is trusted only when `hd` is present or the
  address is `gmail.com`.
- Sign-in buttons are text ("Sign in with Google"). No provider logos.

## Who may sign in

An allow list. Each entry names a provider and one matcher:

| matcher | meaning |
|---|---|
| `everyone` | anyone the provider authenticates — not for Google |
| `subject` | one account, by the provider's subject id |
| `email` | a verified email, case-insensitive |
| `email_domain` | verified emails of a domain — not for Google |
| `hosted_domain` | Google's `hd` claim — Google only |
| `group` | a value of the provider's groups claim |

At a company's own provider `everyone` is everyone in its tenant; at
Google it would be every Google account in the world, so it is refused
there, as `email_domain` is. A Workspace uses `hosted_domain`, and one
person `email`.

With providers configured and no allow entry, start-up fails.

## Users

- The first successful sign-in creates a user, keyed by
  `(provider, subject)`. Later sign-ins refresh its name and verified
  email.
- A user owns their conversations and projects
  ([privacy.md](privacy.md)).

## Roles

- Two roles: **user** and **admin**.
- Admins are named in the configuration, with the same matchers as the
  allow list.
- The role is decided at sign-in and held by the session. The UI is told
  the role so it can show admin screens; the server enforces it regardless.
- What an admin can and cannot see is in [privacy.md](privacy.md).
- Roles map to permissions in one fixed, exhaustive table in `core`. Every
  route declares the permission it needs, and a test asserts that every
  route declares one. Ownership and project membership are checked in the
  application, not in routes.

## Sessions

- A session is a row in the database. The cookie holds a random 256-bit
  secret; the database holds its SHA-256.
- Cookie: `__Host-` prefix, `HttpOnly`, `SameSite=Lax`, `Secure`.
- Fixed lifetime, `session_hours`, 12 by default. No renewal. The
  provider's tokens are discarded after sign-in.
- Sign-out deletes the row and clears the cookie.

## Request protection

- No CSRF token. Every write must be `application/json` and must not be
  cross-site (`Sec-Fetch-Site`). A cookie-authenticated write must also
  carry `Origin` equal to `public_url`.
- The deployment is at the origin root; `public_url` is mandatory and
  drives the redirect URI, the cookie prefix and the origin check.

## Local development mode

- A mode for developing on one's own machine, which **requires no
  sign-in**: no identity provider, no sign-in configuration.
- Everything runs as one fixed local user, who owns the conversations
  created in this mode. The rest of the platform behaves as usual, so
  ownership checks and everything built on users is exercised.
- That user is a **real row**, got or created under the reserved key
  `("!local", "developer")`. The provider id is deliberately not spelt like
  a provider id — an id is a name, and `!local` is not one — so no
  configuration can name it, no identity can carry it, and nothing but this
  mode can reach that row. A session that names it is refused by
  `resolve_session`, so a database kept from a run of the mode hands nobody
  the account.
- It is never the default. It is asked for explicitly when starting the
  server, and it cannot be combined with a sign-in configuration: asking
  for both is a start-up refusal. **No environment variable switches it
  on** — only the argument the starting command passes — so nothing a
  process inherits can turn sign-in off in a deployment.
- It may still be given the **configuration file** (`ROBINAUTS_CONFIG`),
  and then **only its model tables are read**: the chat is developed in
  this mode ([frontend.md](frontend.md)) and a chat needs an agent. A file that also holds sign-in tables — `public_url`,
  `session_hours`, `providers`, `allow`, `admin` — is what "cannot be
  combined" refuses,
  because the mode exists where there is nothing to sign in to. With no
  file, the mode starts with no agents and the agent list is empty.
- It serves the loopback interface only, and refuses to start on any other
  address. Two rules hold that, and they are deliberately of different
  strictness. **What may be bound**: a literal loopback address, or exactly
  `localhost` — the mode carries the bind host and refuses anything else
  when it is built, before a socket exists. A name such as
  `dev.localhost` is resolved by whatever this machine resolves names with,
  and a bind address is not a thing to leave to a resolver. **What may be
  requested**: every request must be addressed to this machine — one `Host`
  header naming any loopback host (any spelling, any port, `*.localhost`
  included: a name that arrived here arrived over loopback), and an address
  the server answered on that is loopback. A unix socket counts as one; a
  scope that does not say where it answered is refused. The request rule is
  what defeats DNS rebinding, where a name somebody else controls is pointed
  at `127.0.0.1` and every same-origin rule holds for the page that did it.
- The server logs a warning at start-up, and the interface shows a
  permanent banner saying that sign-in is off. `GET /auth/session` answers
  `sign_in: false` with `local_development: true`, no providers, and the
  local user.
- The checks on writes (JSON, same origin) stay on. There is no session
  cookie here, so **every** write is judged as a credentialed one: `Origin`
  equal to the loopback origin the request was addressed to, or
  `Sec-Fetch-Site: same-origin` with no `Origin`. Every request in this
  mode is authenticated, so there is no unauthenticated write to relax the
  rule for.
- Tests still exercise the real sign-in flow, against a stand-in identity
  provider; this mode is not a substitute for that.

## API tokens

- A signed-in person mints a token for themselves, with a name, at
  `POST /auth/tokens`. The answer shows its secret, and it is the only
  time the secret is shown: the database holds its SHA-256, as it does a
  session's.
- A token lives ninety days and is not renewed. `GET /auth/tokens` lists
  the person's tokens, oldest first, without their secrets;
  `DELETE /auth/tokens/{token_id}` revokes one of them.
- It is sent as `Authorization: Bearer <secret>` and reaches every route
  under `/api/`, and these three, as the person who minted it. It is what
  channels other than the browser sign in with.
- A bearer is not a cookie: a write that carries one, and no session
  cookie, is not subject to the `Origin` check.
- In the local development mode a token is minted for the local user. Like
  a session naming that user, it signs nobody in where sign-in is on.
- Not there yet: tokens minted by an operator for somebody else, and scopes.
  A token reaches all that its owner can.

## Details likely to change

Configuration — one TOML file, named by `ROBINAUTS_CONFIG`, holding the
sign-in tables below and the model tables of [deployment.md](../deployment.md);
secrets are given as the *name* of an environment variable; unknown keys
are errors; all problems are reported at once:

```toml
public_url = "https://robinauts.example.com"
session_hours = 12

[providers.google]
title = "Google"
issuer = "https://accounts.google.com"
client_id = "..."
client_secret_env = "ROBINAUTS_GOOGLE_SECRET"

[providers.okta]
title = "Okta"
issuer = "https://example.okta.com/oauth2/default"
client_id = "..."
client_secret_env = "ROBINAUTS_OKTA_SECRET"
scopes = ["openid", "email", "profile", "groups"]
groups_claim = "groups"

[[allow]]
provider = "google"
hosted_domain = "example.com"

[[allow]]
provider = "okta"
group = "robinauts-users"

[[admin]]
provider = "okta"
group = "robinauts-admins"
```

Routes:

| route | purpose |
|---|---|
| `GET /auth/session` | who is signed in, their role, the providers; never a 401 |
| `GET /auth/login/{provider}` | start a sign-in |
| `GET /auth/callback/{provider}` | finish it; the redirect URI to register |
| `POST /auth/logout` | sign out |
| `POST /auth/tokens` | mint an API token; the one answer that shows its secret |
| `GET /auth/tokens` | the API tokens of the person asking, without their secrets |
| `DELETE /auth/tokens/{token_id}` | revoke one of them |

- A pending sign-in is stored under the SHA-256 of `state`, single use, for
  10 minutes; their number is capped.
- A failure redirects to the sign-in page with a fixed error code; the
  provider's own words go to the log only.
- The UI shows the sign-in page in place of every page until there is a
  principal, and returns the person to where they were.
- Sign-ins, refusals and sign-outs are written to the audit log.

## Known limits

- The allow list, the admin list and group membership are evaluated at
  sign-in only. A person removed at the identity provider keeps access
  until the session ends, or an operator clears sessions. A shorter
  `session_hours` is the available control.
- No sign-out at the identity provider.
- No rate limit on sign-in beyond the cap on pending sign-ins. The cap
  bounds the table; it does not protect sign-in. Beginning a sign-in needs
  no credentials, so one client that keeps the table full denies sign-in
  to everyone for as long as it goes on. The protection is a rate limit in
  front of the platform — the operator's reverse proxy today, the
  per-client limits of [operations.md](operations.md) later.
- Beginning a sign-in is a plain navigation (a `GET`), so any page can make
  a visitor's browser begin one: it adds a pending sign-in and replaces the
  login cookie of a sign-in that visitor had in flight, which then fails
  and has to be started again. It cannot sign the visitor in to anything:
  the cookie is `SameSite=Lax` and `__Host-`, and the callback checks it.
