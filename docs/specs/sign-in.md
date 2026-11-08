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
  `(provider, subject)`. Later sign-ins refresh its name, verified email,
  hosted domain and groups: what the allow list is checked against again
  when a credential of theirs is used.
- A user owns their conversations and projects
  ([privacy.md](privacy.md)).

## Roles

**Deferred.** Roles are not in this release. Everyone who may sign in is a
user, and a configuration with an `admin` table is refused at start-up. When
roles come, they work as follows.

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
- Cookie: `__Host-` prefix, `HttpOnly`, `SameSite=Lax`, `Secure`. On an
  `http` loopback `public_url`, the prefix and `Secure` are left out.
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
  `SignIn.resolve`, so a database kept from a run of the mode hands nobody
  the account.
- It is never the default. It is asked for explicitly when starting the
  server, and it cannot be combined with a sign-in configuration: asking
  for both is a start-up refusal. **No environment variable switches it
  on** — only the argument the starting command passes — so nothing a
  process inherits can turn sign-in off in a deployment.
- It reads the **configuration file** (`ROBINAUTS_CONFIG`), as every start
  does, and **only its model tables are read**: the chat is developed in
  this mode ([frontend.md](frontend.md)) and a chat needs an agent. A file that also holds sign-in tables — `public_url`,
  `session_hours`, `device_days`, `providers`, `allow`, `admin` — is what
  "cannot be combined" refuses,
  because the mode exists where there is nothing to sign in to. A file with
  no agents gives an empty agent list.
- It serves the loopback interface only, and refuses to start on any other
  address. Two rules hold that, and they are deliberately of different
  strictness. **What may be bound**: a literal loopback address, or exactly
  `localhost` — the mode carries the bind host and refuses anything else
  when it is built, before a socket exists. A name such as
  `dev.localhost` is resolved by whatever this machine resolves names with,
  and a bind address is not a thing to leave to a resolver. **What may be
  requested**: every request carries one `Host` header, and it names
  `localhost` or an IP address, with any port. This defeats DNS rebinding.
  A page rebinds a name it controls to `127.0.0.1`, and the browser sends
  that name as `Host`. An IP address cannot be rebound. So a phone on the
  LAN can reach the frontend's dev server at `192.168.1.10:5173`, which
  proxies to this server.
- The server logs a warning at start-up, and the interface shows a
  permanent banner saying that sign-in is off. `GET /auth/session` answers
  `sign_in: false` with `local_development: true`, no providers, and the
  local user.
- There is no session cookie here, and every request is the local user.
  A write that carries `Origin` must name the `Host` it was sent to, over
  `http` or `https`. A write with no `Origin` passes: browsers send one on
  every write, and scripts such as `curl` send none.
- Tests still exercise the real sign-in flow, against a stand-in identity
  provider; this mode is not a substitute for that.

## API tokens

- A signed-in person mints a token for themselves, with a name, at
  `POST /auth/tokens`, from the browser: minting takes the session cookie,
  never a bearer, and a session younger than ten minutes; an older one is
  answered 403 `FreshSignInRequired`, and the person signs in again. The
  answer shows its secret, and it is the only time the secret is shown: the
  database holds its SHA-256, as it does a session's. A new secret starts
  with `rbt_`; one with no prefix is a token minted before the prefix, and
  works until it expires.
- A token lives ninety days and is not renewed. `GET /auth/tokens` lists
  the person's tokens, oldest first, without their secrets, to a bearer
  too; `DELETE /auth/tokens/{token_id}` revokes one of them, from the
  browser's session alone. A bearer that mints or revokes is 403.
- It is sent as `Authorization: Bearer <secret>` and reaches every route
  under `/api/`, `GET /auth/tokens`, `GET /auth/devices` and
  `GET /auth/session`, as the person who minted it. It is what
  channels other than the browser sign in with.
- A bearer is not a cookie: a write that carries one, and no session
  cookie, is not subject to the `Origin` check.
- In the local development mode a token is minted for the local user. Like
  a session naming that user, it signs nobody in where sign-in is on.
- Not there yet: tokens minted by an operator for somebody else, and scopes.
  A token reaches all that its owner can.

Linking a phone, its device sessions, and the allow-list check on every
use are in [device-pairing.md](../architecture/device-pairing.md).

## Details likely to change

Configuration — one TOML file, named by `ROBINAUTS_CONFIG`, holding the
sign-in tables below and the model tables of [deployment.md](../deployment.md);
secrets are given as the *name* of an environment variable; unknown keys
are errors; all problems are reported at once:

```toml
public_url = "https://robinauts.example.com"
session_hours = 12
device_days = 30

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

The routes of linked devices are in
[device-pairing.md](../architecture/device-pairing.md), "Routes".

- A pending sign-in is stored under the SHA-256 of `state`, single use, for
  10 minutes; their number is capped.
- A failure redirects to the sign-in page with a fixed error code; the
  provider's own words go to the log only.
- The UI shows the sign-in page in place of every page until there is a
  principal, and returns the person to where they were.
- Sign-ins, refusals and sign-outs are written to the audit log.

## Known limits

- Group membership and the other claims are what the last sign-in said.
  The allow list is checked against them again on every use, at most once
  a minute, so a person the operator takes out of the allow list loses
  every credential within a minute; but a person removed at the identity
  provider keeps access until their next sign-in is refused, the session
  ends, or an operator clears it. A shorter `session_hours` is the
  available control.
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
