# Deploying Robinauts

What a platform team does on a clean machine to get a running instance:
one Python wheel (with the locked set of dependencies that was built
beside it), one PostgreSQL, one TLS-terminating reverse proxy, and one
configuration file. Nothing else
([specs/operations.md](specs/operations.md)).

Read it once through before starting. The order below is the order that
works: the database before the schema, the configuration before the first
start, the reverse proxy before signing anyone in — the session cookie is
`__Host-`-prefixed, so **https is not optional**.

## Prerequisites

- **Linux or macOS.** Windows is not a target: `backend/uv.lock` is
  resolved for those two platforms only, and a package that Windows alone
  would need is neither locked nor checked
  ([DEPENDENCIES.md](../DEPENDENCIES.md)).
- **Python 3.12 or later.** The wheel is `py3-none-any` and its lock
  resolves for 3.12; the deployment machine needs the interpreter and
  `venv`, nothing more. No Node — the interface is built into the wheel —
  and no compiler where every dependency has a wheel of its own, which on
  an ordinary Linux they do.
- **PostgreSQL 16**, reachable from the machine. It is always required and
  there is no mode without it. The platform sets no session time zone and
  stores every time as `timestamptz`, so the server's time zone is its own business.
- **A reverse proxy that terminates TLS**, on this machine or in front of
  it. Which one is the platform team's choice; nginx and Caddy are shown
  below.
- Outbound https, **while installing**, to PyPI or to the company's mirror:
  a wheel is not a bundle and its dependencies are downloaded then. **While
  running**, to the identity providers and to the model provider, and
  nothing else ([specs/operations.md](specs/operations.md)).

## 1. Get the wheel

The wheel is built in CI and is what a deployment installs
([specs/frontend.md](specs/frontend.md)): it carries the backend, the
schema and the built interface, and nothing else is built on the machine.

- **Normally**: download the `robinauts-wheel` artifact of a green CI run
  (`.github/workflows/ci.yml`, job "the wheel, built and installed"). It
  holds **two** files — the `.whl` and `requirements.txt` — and you want
  both.
- **From a checkout**, on a build machine with the Node version
  `frontend/.nvmrc` names: `scripts/build-wheel.sh <output-directory>`
  prints the path of the wheel it built and writes `requirements.txt`
  beside it. `scripts/check-wheel.sh` builds it, looks inside it, installs
  it into an empty virtual environment **the way section 2 does** — the
  locked set under its hashes, then the wheel with `--no-deps` — and runs
  the command.

**Never a laptop build for a release.** The bundle is built where the lock
and the licence gates decide what goes into it, which is CI.

**Why the second file.** The wheel's own dependencies are version
*ranges*, which `pip` resolves against PyPI on the day you install. That
set can differ from `backend/uv.lock` — which is the set the licence gate
and `pip-audit` judged, and the set the tests ran against. `requirements.txt`
is that locked set, pinned exactly and with hashes, exported by the same
build. Section 2 installs from it.

## 2. A system user and a virtual environment

A dedicated account that owns nothing else, and an environment of its own.
The locked set goes in **first**, and the wheel then goes in with
`--no-deps`, so that `pip` resolves nothing of its own:

    sudo useradd --system --create-home --home-dir /var/lib/robinauts \
        --shell /usr/sbin/nologin robinauts
    sudo -u robinauts python3 -m venv /var/lib/robinauts/venv
    sudo -u robinauts /var/lib/robinauts/venv/bin/pip install \
        --require-hashes -r /tmp/requirements.txt
    sudo -u robinauts /var/lib/robinauts/venv/bin/pip install \
        --no-deps /tmp/robinauts-0.1.0-py3-none-any.whl
    sudo -u robinauts /var/lib/robinauts/venv/bin/robinauts version

The last line prints the build, the schema version it wants and the first
characters of the `schema.sql` it carries — which is what an upgrade is
decided by.

`pip install robinauts-*.whl` on its own also works, and is fine for a
trial: it just installs whatever `pip` resolves today rather than the set
the gates judged. `--require-hashes` is redundant — a requirements file
that carries hashes already makes `pip` insist on them — and is written out
so that a file somebody edited cannot quietly lose them.

## 3. The database

One database and one role. From a shell that may administer PostgreSQL:

    sudo -u postgres createuser --pwprompt robinauts
    sudo -u postgres createdb --owner robinauts robinauts

The url is given to the process in `ROBINAUTS_DATABASE_URL` and **never on
a command line**: a url holds a password and a command line is a shell
history. So the schema is created in step 6, once that variable has a file
to live in, and not here.

The server never creates or changes the schema itself. It refuses to start
against a database that is not the one the build was written against, and
names the command that fixes it.

## 4. The configuration file

One TOML file describes the deployment: the sign-in half and the model
half ([specs/sign-in.md](specs/sign-in.md)). `ROBINAUTS_CONFIG` names it. It holds
**no secret**: a secret is always the *name* of an environment variable.
Unknown keys are errors and every problem is reported at once.

Write it as `/etc/robinauts/robinauts.toml`, owned by root, readable by
the service user:

```toml
# The origin this deployment is served at, exactly as a browser writes it:
# scheme, host, optional port, and no path. It drives the redirect URI, the
# cookie prefix and the origin check on every write, so it must be the
# public name -- not the address the proxy reaches the backend on.
public_url = "https://robinauts.example.com"

# How long a session lasts. There is no renewal, and the allow list is
# evaluated at sign-in: this is the control over somebody removed at the
# identity provider.
session_hours = 12

[providers.google]
title = "Google"
issuer = "https://accounts.google.com"
client_id = "1234567890-abcdefghijklmnop.apps.googleusercontent.com"
client_secret_env = "ROBINAUTS_GOOGLE_SECRET"

[providers.okta]
title = "Okta"
issuer = "https://example.okta.com/oauth2/default"
client_id = "0oa1b2c3d4e5f6g7h8i9"
client_secret_env = "ROBINAUTS_OKTA_SECRET"
# The groups scope has to be asked for, and the claim it arrives in has to
# be named, or a [[allow]] group entry would match nobody.
scopes = ["openid", "email", "profile", "groups"]
groups_claim = "groups"

# Who may sign in. Each entry names a provider and exactly one matcher:
# everyone, subject, email, email_domain, hosted_domain or group. With
# providers configured and no entry at all, start-up fails -- nobody could
# sign in, and that is a mistake rather than a policy.
[[allow]]
provider = "google"
hosted_domain = "example.com"

[[allow]]
provider = "okta"
group = "robinauts-users"

[[allow]]
provider = "okta"
email = "contractor@partner.example"

[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ROBINAUTS_ANTHROPIC_KEY"

# Or reach OpenRouter, which serves Anthropic's Messages API too. The kind
# names the protocol rather than the vendor, so the endpoint is written down;
# base_url is the PREFIX the client appends /v1/messages to, which is why it
# stops at /api. A model reached this way carries OpenRouter's own name for
# it, such as name = "anthropic/claude-sonnet-5".
# [model_providers.openrouter]
# kind = "anthropic-compatible"
# base_url = "https://openrouter.ai/api"
# api_key_env = "ROBINAUTS_OPENROUTER_KEY"

# Or OpenAI itself, whose endpoint the engines pin, so there is no base_url:
# [model_providers.openai]
# kind = "openai"
# api_key_env = "ROBINAUTS_OPENAI_KEY"
#
# Or any endpoint that speaks OpenAI's Chat Completions -- a gateway, vLLM,
# OpenRouter again. base_url is again a PREFIX, but OpenAI's client appends
# /chat/completions alone, so the API's version is part of it: requests to
# this one go to https://gateway.example.com/v1/chat/completions, and
# OpenRouter this way is base_url = "https://openrouter.ai/api/v1".
# [model_providers.gateway]
# kind = "openai-compatible"
# base_url = "https://gateway.example.com/v1"
# api_key_env = "ROBINAUTS_GATEWAY_KEY"

[models.sonnet]
provider = "anthropic"
name = "claude-sonnet-5"
title = "Claude Sonnet 5"
timeout_seconds = 120     # one call to the vendor, never the whole turn
max_retries = 2           # the vendor SDK's retries of one call, with backoff
max_output_tokens = 8192

# How turns run. Every key is optional; these are the defaults.
# [work]
# max_turn_seconds = 1200        # a turn's deadline, from its start
# max_model_calls = 100          # calls to the model in one turn
# tool_error_behavior = "failed" # a tool's error goes back to the model
# lease_seconds = 90             # a turn of a process that went away, at most
# heartbeat_seconds = 30         # renews them all, at most half the lease
# drain_seconds = 30             # what a stop gives the turns running here
#
# What one process takes of PostgreSQL. Optional; these are the defaults.
# [database]
# pool_max = 10                  # the pool's connections at most
# acquire_timeout_seconds = 5    # a request waits this long, then answers 503

[agents.assistant]
title = "Assistant"
model = "sonnet"
engine = "langgraph"
system_prompt = "Play fair."
```

Notes on what is and is not there:

- **Matchers.** `hosted_domain` is Google's `hd` claim and is Google only;
  `email_domain` is *not* for Google, because Google verifies personal
  accounts registered with any address. `email` and `subject` name one
  person. `everyone = true` admits anyone the provider authenticates, and
  is refused for Google too, where that is every Google account in the
  world; a Google client set to Internal with `hosted_domain` is how a
  Workspace lets all of its people in.
- **`[[admin]]` is refused.** Roles are deferred in this release: everyone
  who may sign in is a user, and a file with an `admin` table does not
  start ([specs/sign-in.md](specs/sign-in.md)).
- **Engines**: `langgraph` and `pydantic-ai`, both wired. **Provider
  kinds**: all four, under both engines. `anthropic` and `openai` are the
  vendors themselves, at the endpoint the engines pin, and take no
  `base_url`; `anthropic-compatible` is any endpoint that speaks Anthropic's
  Messages API and `openai-compatible` any that speaks OpenAI's Chat
  Completions, at a `base_url` you give. **What `base_url` means differs by
  protocol**: it is the prefix the client appends its own path to, which is
  `/v1/messages` for Anthropic's (so OpenRouter is
  `https://openrouter.ai/api`) and `/chat/completions` alone for OpenAI's
  (so OpenRouter is `https://openrouter.ai/api/v1`, and a vLLM server on
  this machine `http://127.0.0.1:8000/v1`). The OpenAI kinds speak Chat
  Completions, never the Responses API, and the key is sent as
  `Authorization: Bearer`. `max_output_tokens` left out means 8192 over
  Anthropic's protocol, which requires a ceiling, and no ceiling at all over
  OpenAI's; one that is set is sent to `openai` as `max_completion_tokens`
  and to `openai-compatible` as `max_tokens`, the field OpenRouter and older
  servers know.
- **What the engines take out of the environment.** The vendors' clients
  would otherwise read settings from variables nobody wrote in this file, so
  the engines pass everything as arguments and, when they are built at
  start-up, remove the variables an argument cannot override:
  `ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_LOG`, `OPENAI_CUSTOM_HEADERS`,
  `OPENAI_LOG`, `OPENAI_ORG_ID`, `OPENAI_ORGANIZATION`, `OPENAI_PROJECT_ID`
  and `OPENAI_ADMIN_KEY`. `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`,
  `OPENAI_API_BASE`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and the
  vendor-specific proxy variables are never consulted; a proxy is
  `HTTPS_PROXY`. An `OPENAI_API_KEY` in the unit's environment is used only
  if a provider's `api_key_env` names it.
- `token_endpoint_auth` is `client_secret_basic` by default, or
  `client_secret_post`.
- **Titles.** An agent's or a model's `title` is the name a person picks it
  by. It is optional, and left out it is the table's id.
- **An agent's `model` is its default.** It is copied into a conversation
  when the conversation starts, so changing it reaches new conversations
  only. Removing a model that conversations are using refuses their next
  turn, saying the conversation's model is no longer offered, rather than
  answering with another model; the log names the model.
- A file with no `[agents]` table is a deployment with no agents: it
  starts, the picker is empty, and the log says so.
- **Connections.** Each process holds a pool of at most
  `[database] pool_max` connections (10), shared by requests, turn events
  and the engines' checkpoints, and two of its own beside it: the listener
  (`LISTEN robinauts_turns, robinauts_cancel`) and the work connection, which
  renews the leases of its turns so that a busy pool never delays one. So
  `N` processes need `N × (pool_max + 2)` connections, and two more for
  `robinauts db init` and an operator: keep that below the server's
  `max_connections` (100 by default, which is 8 processes at the default
  `pool_max`). An operation that finds no connection of the pool free within
  `acquire_timeout_seconds` answers 503 rather than queueing for ever, and a
  turn's runner writes its batch again.

## 5. Register the redirect URI

The redirect URI is `<public_url>/auth/callback/<provider id>`, where the
provider id is the key of the `[providers.*]` table. For the file above:

    https://robinauts.example.com/auth/callback/google
    https://robinauts.example.com/auth/callback/okta

Register it **exactly**, character for character. The platform sends it in
the authorization request and again in the token request, and both
providers compare it as a string.

**Google** (console.cloud.google.com → APIs & Services → Credentials):

1. Create an OAuth client of type *Web application*.
2. Under *Authorised redirect URIs*, add the `.../google` URI above.
3. Copy the client id into `client_id`; put the client secret in the
   environment variable `client_secret_env` names.
4. No scopes to configure: the platform asks for `openid email profile`
   and reads `sub`, `email`, `email_verified`, `name` and — for a
   Workspace — `hd`. `hosted_domain` entries need a Workspace account;
   Google's `email_verified` is trusted only when `hd` is present or the
   address is at `gmail.com` or `googlemail.com`.

**Okta** (Admin → Applications → Create App Integration):

1. *OIDC — OpenID Connect*, application type *Web Application*.
2. *Sign-in redirect URI*: the `.../okta` URI above. Grant type
   *Authorization Code*; PKCE is used and needs nothing enabled.
3. Copy the client id and secret as above. `issuer` is the authorization
   server's issuer exactly as Okta prints it — usually
   `https://<org>.okta.com/oauth2/default`, and the discovery document's
   own `issuer` must equal it.
4. To use `group` entries, add a **groups claim** to the ID token (Sign On
   → OpenID Connect ID Token → Groups claim), ask for the `groups` scope
   in `scopes`, and name the claim in `groups_claim`.

Assignment at the provider is the first gate and the allow list is the
second: a person Okta has not assigned to the application never reaches
the allow list at all.

## 6. The environment

Four kinds of variable, and three of them are secrets. Put them in
`/etc/robinauts/environment`, owned by root, mode `0600`:

    ROBINAUTS_CONFIG=/etc/robinauts/robinauts.toml
    ROBINAUTS_DATABASE_URL=postgresql://robinauts:...@127.0.0.1:5432/robinauts
    ROBINAUTS_GOOGLE_SECRET=...
    ROBINAUTS_OKTA_SECRET=...
    ROBINAUTS_ANTHROPIC_KEY=...

`ROBINAUTS_LOG_FORMAT` is `text`, one line per entry (the default), or
`json`, one object per line, for a log collector.

`ROBINAUTS_WORKER_ID` names the process as the holder of the turns it runs,
which every write of a turn's runner names; it is not a secret. Left out it is
`<host name>:<pid>`, which is unique on one machine; where processes come and
go under the same host name, set it to something unique per process, as a
Kubernetes Deployment does with the pod's name.

`ROBINAUTS_AUTH_CONFIG` is the old name of `ROBINAUTS_CONFIG`. It is still
read, with a warning at start-up; if both are set the new one wins and the
log says so. Rename it.

Now create the schema, with that file and not with a flag:

    sudo systemd-run --pipe --wait --uid=robinauts \
        --property=EnvironmentFile=/etc/robinauts/environment \
        /var/lib/robinauts/venv/bin/robinauts db init

It prints `the database is at schema version N (schema.sql …)` whether it
created the schema or found it already there. It works on an **empty**
database only: there are no migrations before the first release, and a
database made from another `schema.sql` is refused and made again rather
than upgraded. The command records
the file's hash in the database, and the server checks it at every start.

The local development mode has **no** variable: it is asked for on the
command line and nowhere else, so nothing a process inherits can turn
sign-in off in a deployment.

`/etc/systemd/system/robinauts.service`:

```ini
[Unit]
Description=Robinauts
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=robinauts
Group=robinauts
EnvironmentFile=/etc/robinauts/environment
ExecStart=/var/lib/robinauts/venv/bin/robinauts start \
    --host 127.0.0.1 --port 8000 --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=5
# Exit 2 is "the configuration or the command line is wrong", and restarting
# will not mend it: without this, a misspelt key is a service that restarts
# for ever and a log nobody can read.
RestartPreventExitStatus=2
KillSignal=SIGTERM
# A stop drains: streams are told to reconnect and open requests get 5 s,
# then the runs in flight get [work] drain_seconds (30 s) to finish, the rest
# end as interrupted within 10 s, and the connections close within 5 s more
# -- some 50 s at most. Anything under a minute here would SIGKILL the tail.
TimeoutStopSec=90
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes

[Install]
WantedBy=multi-user.target
```

    sudo systemctl daemon-reload
    sudo systemctl enable --now robinauts

**`--forwarded-allow-ips` is the address of the proxy**, and nothing else.
`proxy_headers` is on, so uvicorn reads `X-Forwarded-For` and
`X-Forwarded-Proto` — but only from a connection that came from one of
these addresses. In **this build** what that changes is the client address
in the access log: nothing in the platform reads the scheme or the client
of a request, and what makes a deployment https is `public_url`
(section 4). So a wrong value here costs you an access log full of
`127.0.0.1`, and a `*` on a port a network can reach lets anybody write
whatever address they like into it. The default is `127.0.0.1`; a proxy on
another host is named here.

**Or a unix socket**, for a proxy on the same machine:

    RuntimeDirectory=robinauts
    RuntimeDirectoryMode=0750
    ExecStart=/var/lib/robinauts/venv/bin/robinauts start \
        --uds /run/robinauts/robinauts.sock

`RuntimeDirectory=` is not optional here: `ProtectSystem=strict` leaves
`/run` read-only, and systemd makes and cleans up that one directory for
the service. The command binds the socket itself at mode `0600` before it
listens, and removes it when it stops. `--uds` cannot be given with
`--host` or `--port`.

Mode `0600` is this user alone, so a proxy that runs as somebody else —
nginx as `www-data`, Caddy as `caddy` — cannot open it. The recipe for
that, all three parts together:

    # on the robinauts unit
    RuntimeDirectory=robinauts
    RuntimeDirectoryMode=0750
    ExecStart=… robinauts start --uds /run/robinauts/robinauts.sock \
        --uds-mode 0660 --forwarded-allow-ips '*'

    # once, so the proxy may enter the directory
    sudo usermod -aG robinauts www-data      # or, on the proxy's unit:
                                             # SupplementaryGroups=robinauts

The directory stays `0750` and group-owned by `robinauts`, so only those
two accounts get in; the socket is `0660`, so only those two may open it.
**Keep the `*`.** A connection over a unix socket has no address at all —
uvicorn reports no client — so any other value simply believes nobody and
the forwarded headers are dropped. Who may connect here is the directory's
mode and group, not a list of addresses; the `*` is written out because
`--uds-mode` was, and the two belong together.

## 7. The reverse proxy

It terminates TLS, adds HSTS, and passes everything to the backend at the
root of the origin. Three things matter:

- **The name the browser is on must be `public_url`**, exactly. That
  string — not a header — is what decides the `Secure` and `__Host-`
  cookies and what the `Origin` of every write is compared with. A proxy
  that serves the deployment under a second name, or on a second port,
  gives that name a platform that will not keep a session. The
  `X-Forwarded-*` headers below are good practice and reach the access log;
  they are not what makes the deployment https.
- **No buffering on the event stream**, and a read timeout well above
  15 s. A stream sends a `: keep-alive` comment after every 15 s of
  silence, and the interface re-attaches to a stream that drops, so the
  timeout bounds a quiet moment, not the turn, which may run for
  `[work] max_turn_seconds` (1200 s by default). Every stream also carries
  `X-Accel-Buffering: no`, which nginx obeys — the setting below is there
  for the proxies that do not.
- The paths are `/ui/` (the interface), `/api/` (the API and the streams),
  `/auth/` (the sign-in navigations), `/health` and `/openapi.json`. All
  of them are under `/`, so one location is enough.

nginx:

```nginx
# Port 80 exists to answer the certificate authority's challenge and to
# send everybody else to 443; it serves nothing of the platform. The
# challenge location comes first, because a redirect in front of it is a
# renewal that fails every ninety days.
server {
    listen 80;
    server_name robinauts.example.com;
    location /.well-known/acme-challenge/ { root /var/www/letsencrypt; }
    location / { return 308 https://$host$request_uri; }
}

server {
    # `listen ... http2` rather than the `http2 on;` directive: the directive
    # is nginx 1.25.1 and later, and Ubuntu 24.04 LTS ships 1.24.
    listen 443 ssl http2;
    server_name robinauts.example.com;

    ssl_certificate     /etc/ssl/robinauts/fullchain.pem;
    ssl_certificate_key /etc/ssl/robinauts/privkey.pem;

    # HSTS belongs here: the platform never sends it, because only what
    # terminates TLS knows whether every host under this name is ready for
    # it.
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # The platform refuses a body over 1 MiB itself; this is the same rule
    # one hop earlier, so the bytes are never carried.
    client_max_body_size 1m;

    location / {
        # A unix socket instead: http://unix:/run/robinauts/robinauts.sock:
        # (the trailing colon is nginx's, and www-data must be in the
        # robinauts group -- see the --uds recipe above).
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;

        # Server-sent events: nothing held back, nothing rewritten, and a
        # read timeout past the 15 s keep-alive (the interface re-attaches to
        # a stream that is cut, so it need not outlast the turn).
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }
}
```

Caddy:

```caddyfile
robinauts.example.com {
    header Strict-Transport-Security "max-age=31536000; includeSubDomains"
    request_body {
        max_size 1MiB
    }

    reverse_proxy 127.0.0.1:8000 {
        # or: reverse_proxy unix//run/robinauts/robinauts.sock
        flush_interval -1          # never buffer: SSE goes straight through
        transport http {
            read_timeout 900s
        }
    }
}
```

Caddy sets `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host`
itself, and obtains and renews the certificate itself — which is why it has
no port-80 block and no certificate paths. **nginx does not**: the
certificate is the platform team's, from `certbot --nginx -d
robinauts.example.com` or from the company's own certificate authority, and
the two `ssl_certificate*` lines name what it produced. The
`acme-challenge` location above is what **webroot** renewal
(`certbot renew --webroot -w /var/www/letsencrypt`) writes into; a
deployment renewing another way can drop it, and one renewing that way and
missing it finds out in ninety days.

Everything else — the Content-Security-Policy, `X-Frame-Options`,
`nosniff`, `Referrer-Policy`, the cache headers — is sent by the platform.
A proxy that adds a second Content-Security-Policy makes the interface the
intersection of the two, which is usually a blank page.

## 8. The first start

    sudo systemctl start robinauts
    journalctl -u robinauts -f

A good start says four lines at `info` and no more: "Started server
process", "Waiting for application startup", "Application startup
complete", "Uvicorn running on http://127.0.0.1:8000". Every five minutes
each process sweeps, and logs a line when the sweep did something: events,
sign-in records and tokens past their expiry deleted, turns a process that
went away left running ended, and conversations whose delete died purged. The log goes to
**stdout**, one line each, with every query string cut off — one of this
platform's paths carries an authorization code in one.

`no [agents] table …` is an `info` line saying the picker will be empty.
`SIGN-IN IS OFF …` is a warning, and means the local development mode was
asked for — which is never right in a deployment.

A start-up **refusal** prints the problems, one per line, with no
traceback, and exits 2. Every problem it can see is in that one list: an
operator with three mistakes fixes three mistakes. The common ones:

- `no configuration: set ROBINAUTS_CONFIG to the TOML file describing this
  deployment`
- `no database: set ROBINAUTS_DATABASE_URL to the PostgreSQL this
  deployment uses`
- `providers.google: the client secret is read from the environment
  variable ROBINAUTS_GOOGLE_SECRET, which is unset or empty`
- `model_providers.anthropic: the API key is read from the environment
  variable ROBINAUTS_ANTHROPIC_KEY, which is unset or empty`
- `unknown key '...'` — a misspelt key, refused rather than ignored,
  because a key that was ignored is a rule the operator believes is in
  force.
- `the database has no Robinauts schema; this build needs schema version
  N. ...` — run `robinauts db init`.

An exit code of 2 means "change something and try again"; 1 means the
command could not do what it was asked.

## 9. Verify

1. `curl -fsS https://robinauts.example.com/health` → `{"status":"ok"}`.
   It reads no database: it answers "this process is up and serving".
   `/ready` → `{"status":"ready"}` also needs the database to answer; it is
   503, naming what is wrong, while the process stops or the database is
   away.
2. Open `https://robinauts.example.com/` in a browser. It redirects to
   `/ui/` and shows the sign-in page with one button per provider.
3. Sign in as somebody the allow list has, through Google. Then as
   somebody else, through Okta. Each lands back where they started and
   sees an empty history.
4. Sign in as somebody the allow list does **not** have. The provider
   authenticates them; the platform sends them back to the sign-in page
   with `?error=not_allowed`, which reads: "The provider knows who you
   are, but this deployment does not let that account in. Ask whoever runs
   it." The provider's own words go to the log and never to the browser.
5. Hold a conversation. Ask the first person for the id in their address
   bar and fetch it as the second: `GET /api/conversations/<id>` answers
   `404`. A conversation is private to its author and there is no other
   way to see one.
6. Close the tab in the middle of an answer and reopen the conversation.
   The answer is complete, or still arriving, and the stream re-attaches.

## 10. Operating

**Logs.** stdout, so `journalctl -u robinauts`. `--log-level debug` on
`robinauts start` for more; it never prints a conversation's content, a
client secret or an API key. Turning a **vendor's** logger up in your own
logging configuration would print request bodies, which is why the
platform pins those loggers and removes `ANTHROPIC_LOG` at start-up.

**Restart.** `systemctl restart robinauts`. Runs in flight get
`[work] drain_seconds` to finish; those still running are then ended and
marked `interrupted`, with what they had written kept as a failed answer,
and their authors press Retry. A configuration change — an agent's engine, an agent's default
model (for new conversations only), the allow list, a new agent — takes
effect at the next restart, because the file is read once at start-up.

**Upgrade.**

1. Stop the service.
2. In the same virtual environment, the same two steps as the first
   install, with the new artifact's own pair of files:
   `pip install --require-hashes -r requirements.txt` then
   `pip install --no-deps --force-reinstall robinauts-<new>.whl`.
3. `robinauts version` — until the first release the schema version stays
   1, so what to compare is the `schema.sql` hash it prints beside it. If
   it differs from what the previous install printed, the database is
   **recreated**, not migrated, and all data in it is disposable. The server
   refuses the old database anyway, saying it was made from an older
   `schema.sql`.
4. `robinauts db init` (a no-op when the schema is unchanged), then start
   the service.

**Backups** are the platform team's: one PostgreSQL holds everything —
conversations, runs, users, sessions. Nothing is kept on the file system
but the configuration and the wheel.

**What is not there yet**, and is the deployment's to provide or to do
without:

- **Rate limiting.** Beginning a sign-in needs no credentials, and the cap
  on pending sign-ins bounds a table rather than protecting anything. The
  proxy in front is the rate limit until per-client limits arrive.
- **Migrations.** As above.
- **Usage reporting**, token budgets, audit export, retention and purge.
- **A container image**, an SBOM and signed releases.

## 11. Several processes, and Kubernetes

Any number of identical processes may serve one deployment, behind a load
balancer, against the one database: every one serves the web and runs
turns, and none holds anything the others need. A turn runs in the process
that took its request; its stream, its Stop and its conversation's delete
work through whichever process a request lands on, and a process that dies
or is stopped leaves every conversation usable.

- **The same wheel, configuration and environment everywhere**, but for
  `ROBINAUTS_WORKER_ID`, which names each process as the holder of its
  turns: on Kubernetes, the pod's name.
- **`robinauts db init` runs once, as a Job before the rollout**, never in
  every pod. Until the first release a change of schema is a stop-all
  rollout (`strategy: Recreate`) and a new database; any other is a rolling
  update with `maxUnavailable: 0`.
- **Probes on the one port.** `readinessProbe` on `/ready`, which is 503
  while the process stops or the database or its own connections are away;
  `livenessProbe` on `/health`, which reads nothing.
- **Stopping.** A `preStop` hook of `sleep 5` lets the pod leave the
  Service's endpoints before `SIGTERM`; then the drain (§10) takes at most
  `[work] drain_seconds` (30) and about 20 s more. Set
  `terminationGracePeriodSeconds: 60`.
- **Connections.** `N × (pool_max + 2) + 2` below the server's
  `max_connections` (§4, "Connections").
- **The load balancer** keeps no affinity (none is needed), passes `Host`
  and `Origin` through untouched (the `Origin` of every write is compared
  with `public_url`), does not buffer `text/event-stream`, and allows a
  minute of silence on a connection: a stream says something every 15 s.
- **A process that dies** leaves its turns running until their lease
  passes (`[work] lease_seconds`, 90 s). The first reader to find one, or
  the next sweep of any process (every five minutes), ends it as
  interrupted, with what it had written kept as a failed answer; Retry
  starts it over.
- **Logs.** `ROBINAUTS_LOG_FORMAT=json` writes one JSON object per line,
  with `pod`, and `session_id` and `turn_id` where a line is about a
  conversation or a turn; the default is one line of text with the same.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: robinauts
spec:
  replicas: 2
  strategy:
    type: RollingUpdate          # Recreate when schema.sql changed
    rollingUpdate: {maxUnavailable: 0, maxSurge: 1}
  selector: {matchLabels: {app: robinauts}}
  template:
    metadata: {labels: {app: robinauts}}
    spec:
      terminationGracePeriodSeconds: 60
      containers:
        - name: robinauts
          image: registry.example.com/robinauts:0.1.0   # the wheel, installed
          args: ["robinauts", "start", "--host", "0.0.0.0", "--port", "8000"]
          ports: [{containerPort: 8000}]
          envFrom: [{secretRef: {name: robinauts-environment}}]
          env:
            - name: ROBINAUTS_WORKER_ID
              valueFrom: {fieldRef: {fieldPath: metadata.name}}
            - name: ROBINAUTS_LOG_FORMAT
              value: json
          readinessProbe: {httpGet: {path: /ready, port: 8000}, periodSeconds: 5}
          livenessProbe: {httpGet: {path: /health, port: 8000}, periodSeconds: 10}
          lifecycle:
            preStop: {exec: {command: ["sleep", "5"]}}
```

## The local development mode

`robinauts start --dev-no-sign-in` runs with no identity provider at all,
as one fixed local user, on the loopback interface only — for developing
on one's own machine. It refuses any bind address that is not loopback, it
logs a warning at start-up, and the interface shows a permanent banner.
It may be given a configuration file, and then only its model tables are
read; a file that also holds sign-in tables is a start-up refusal. Without
`ROBINAUTS_DATABASE_URL` it keeps its records in memory, which no start with
sign-in does: there, a missing database is a start-up refusal.

```toml
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ROBINAUTS_ANTHROPIC_KEY"

[models.sonnet]
provider = "anthropic"
name = "claude-sonnet-5"

[agents.assistant]
title = "Assistant"
model = "sonnet"
engine = "pydantic-ai"
```

**It is not a way to deploy.** Nothing in this guide uses it.

## What can go wrong

| what you see | why | what to do |
|---|---|---|
| `pip` says `hashes are required in --require-hashes mode` and names the wheel | the wheel was given on the same command line as the requirements file, and one hashed requirement makes `pip` insist on hashes for everything | two commands: the requirements file, then `pip install --no-deps <wheel>` |
| `pip` says `THESE PACKAGES DO NOT MATCH THE HASHES` | the wheel and the `requirements.txt` beside it came from different builds, or a mirror is serving something else | take both files from the one CI run |
| `no configuration: set ROBINAUTS_CONFIG …` | the variable is unset, or the unit has no `EnvironmentFile` | set it; `ROBINAUTS_AUTH_CONFIG` is the deprecated old name |
| `<path>: not valid TOML: … (at line L, column C)` | a syntax error | the message names the line |
| `unknown key 'sessions_hours'` | a misspelt key | spell it as the example does; unknown keys are never ignored |
| `providers.<id>: the client secret is read from the environment variable X, which is unset or empty` | the variable is unset, empty, or not in the unit's `EnvironmentFile` | set it; an empty variable counts as unset |
| `model_providers.<id>: the API key is read from the environment variable X, which is unset or empty` | as above, for a model provider | set it; every *declared* provider needs its key, used or not |
| `model_providers.<id>.kind: this build cannot reach '…' providers; it was built with …` | a build whose engines do not offer that kind; this one offers all four, so it is a build of something else | use one of the kinds the message lists |
| every turn on an `openai-compatible` provider fails, most often with a `404` | `base_url` is missing the API's version: OpenAI's client appends `/chat/completions` alone | end `base_url` where the endpoint's own documentation puts `/chat/completions` after it, usually at `/v1` |
| every turn on an `openai-compatible` provider fails with a `400` naming `max_tokens` | the endpoint knows only the newer `max_completion_tokens`, and a compatible endpoint is sent the older field every server has known | leave `max_output_tokens` out for that model |
| every turn on one OpenAI model fails, and the vendor's message says the model is not supported on this endpoint, or to use the Responses API | OpenAI serves some models (several `-pro` and agentic ones) only over the Responses API, and this build speaks Chat Completions for both OpenAI kinds | pick a model whose page on OpenAI's site lists `v1/chat/completions` |
| a turn with tools on GPT-6 Sol, GPT-6 Luna or GPT-5.6 Sol is refused by OpenAI (for GPT-5.6 Sol, "Function tools with reasoning_effort are not supported … in /v1/chat/completions"), and the same model answers without tools | OpenAI documents function calling over Chat Completions on GPT-6 Sol and Luna only with reasoning off, OpenAI Support reported the GPT-5.6 Sol refusal (2026-09-07), and the other GPT-5.6 models reason by default and are untried; the engines do not turn reasoning off (not yet seen in a live turn here) | use another model for an agent with tools |
| `agents.<id>.engine: one of langgraph, pydantic-ai, not '…'` | a misspelt engine | `langgraph` or `pydantic-ai`; both are wired in this build |
| `admin: roles are not in this release …` | an `[[admin]]` table | remove it; roles are deferred |
| `allow: no entry, so nobody could sign in` | providers configured, allow list empty | add at least one `[[allow]]` |
| `allow N: group needs providers.<id>.groups_claim …` | a `group` entry with no claim named | add `groups_claim`, and the `groups` scope |
| `no database: set ROBINAUTS_DATABASE_URL …` | the variable is unset | set it; the url is never a command-line flag |
| `the database could not be opened: …` | no server there, no such database, credentials refused | the driver's own sentence says which; the url is never echoed |
| `the database has no Robinauts schema; this build needs schema version N` | `db init` was not run | run `robinauts db init` |
| `the database is at schema version M; this build needs schema version N` | the wheel and the database disagree | there are no migrations before the first release: recreate the database and `db init` |
| `the database was made from an older schema.sql …` | the wheel's `schema.sql` changed since `db init` ran, or the file was applied by hand | drop the database and run `robinauts db init` |
| signing in loops back to the sign-in page | `public_url` is not the origin the browser is on: the redirect URI, the cookie prefix and the origin check are all built from that one string | make `public_url` character for character what the address bar shows, and re-register the redirect URI if it changed |
| the sign-in page says the sign-in "came back to a different browser" (`state_mismatch`) | the login cookie did not come back: the deployment is reachable under two names or two ports, so the cookie was set on one origin and the callback landed on the other | one origin, equal to `public_url`; send the other name to it with a redirect |
| `public_url: '…' is http on a host that is not loopback: use https` | an `http://` origin on a real host | https is mandatory: `public_url` beginning `https://` is what marks the cookies `Secure` and `__Host-`, and neither may be sent over `http` ([specs/sign-in.md](specs/sign-in.md)) |
| `?error=not_allowed` for somebody who should be in | the allow list, or the provider's assignment | the provider's own words are in the server's log |
| `?error=provider_refused` | the provider said no to the client or to the code: usually the wrong client secret in the environment variable, or a registration for another client id | compare `client_id` and the secret with the provider's own page; the provider's words are in the log |
| `?error=provider_unavailable` | the discovery document or the token endpoint could not be reached | outbound https, and the `issuer` spelling |
| `?error=invalid_id_token` | `issuer`, `client_id` or the clock | the discovery document's `issuer` must equal the configured one; check the clock (60 s of skew is allowed) |
| an answer streams nowhere, then arrives all at once at the end | the proxy is buffering | `proxy_buffering off` / `flush_interval -1` |
| the interface keeps saying it is reconnecting | the proxy's read timeout is below the 15 s keep-alive, or it buffers the stream | a read timeout of a minute or more, and no buffering |
| a write is refused with a `403` | the browser's `Origin` header does not equal `public_url`, string for string | the same cause as the sign-in loop: one origin, and `public_url` spelt as the browser spells it |
| the interface shows "not built" | a wheel without the interface, which cannot be built — so this is a source checkout, not a wheel | install the wheel |
