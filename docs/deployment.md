# Deploying Robinauts

What a platform team does on a clean machine to get a running instance:
one Python wheel (with the locked set of dependencies that was built
beside it), one PostgreSQL, one TLS-terminating reverse proxy, and one
configuration file. Nothing else
([specs/operations.md](specs/operations.md)).

Read it once through before starting. The order below is the order that
works: the database before the schema, the configuration before the first
start, the reverse proxy before signing anyone in — the session cookie is
`__Host-`-prefixed, so **https is mandatory**.

## Prerequisites

- **Linux or macOS.** Windows is not a target: `backend/uv.lock` is
  resolved for those two platforms only, and a package that Windows alone
  would need is neither locked nor checked
  ([DEPENDENCIES.md](../DEPENDENCIES.md)).
- **Python 3.12 or later.** The wheel is `py3-none-any` and its lock
  resolves for 3.12; the deployment machine needs only the interpreter
  and `venv`. No Node — the interface is built into the wheel —
  and no compiler where every dependency has a wheel of its own, which on
  an ordinary Linux they do.
- **PostgreSQL 16**, reachable from the machine. It is always required. The platform
  sets no session time zone and
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
  holds the `.whl`, `requirements.txt` and a `neorc-wheels/` directory, and
  you want all three.
- **From a checkout**, on a build machine with the Node version
  `frontend/.nvmrc` names: `scripts/build-wheel.sh <output-directory>`
  prints the path of the wheel it built and writes `requirements.txt` and
  `neorc-wheels/` beside it. `scripts/check-wheel.sh` builds it, looks inside it, installs
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
        --require-hashes -r /tmp/requirements.txt \
        --find-links /tmp/neorc-wheels
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

<!-- neorc-from-source -->
`--find-links` names the wheels of neorc, which this build made. neorc is not
on PyPI yet.

## 3. The database

One database and one role. From a shell that may administer PostgreSQL:

    sudo -u postgres createuser --pwprompt robinauts
    sudo -u postgres createdb --owner robinauts robinauts

The url is given to the process in `ROBINAUTS_DATABASE_URL` and **never on
a command line**: a url holds a password and a command line is a shell
history. So the schema is created in step 6, once that variable has a file
to live in.

The server never creates or changes the schema itself. It refuses to start
against a database that is not the one the build was written against, and
names the command that fixes it.

Every query has 10 seconds to finish. A query that takes longer, such as
one waiting on a lock held elsewhere, fails with `TimeoutError`. The log
then shows a line such as `could not end task …` or
`turn … ended on an error`, followed by the traceback. A turn whose
query timed out fails with "a call timed out before the turn's deadline"
when its last write still lands. Otherwise it ends as interrupted once
its lease passes. To find the cause, look in `pg_stat_activity` for long
transactions and for queries waiting on a lock.

## 4. The configuration files

Two TOML files describe the deployment. **oneberth's** holds the sign-in, and
`ONEBERTH_CONFIG` names it (oneberth's `docs/specs/identity.md`, "Configuration";
[specs/sign-in.md](specs/sign-in.md)). **Robinauts'** holds the models, the agents, the tool
servers and the workers, and `ROBINAUTS_CONFIG` names it. Neither holds a **secret**: a secret
is always the *name* of an environment variable. Unknown keys are errors and every problem is
reported at once.

Write oneberth's as `/etc/robinauts/oneberth.toml`, owned by root, readable by the service
user:

```toml
# How long a session lasts. There is no renewal; the allow list is checked
# again when a credential is used, at most once a minute.
session_hours = 12

# The origin this backend is served at, exactly as a browser writes it:
# scheme, host, optional port, and no path. It drives the redirect URI, the
# cookie prefix and the origin check on every write, so it must be the
# public name -- not the address the proxy reaches the backend on.
[apps.robinauts]
public_url = "https://robinauts.example.com"

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
# everyone, subject, email, email_domain, hosted_domain or group. It gives
# robinauts/user and neorc/user.
[[allow]]
provider = "google"
hosted_domain = "example.com"

[[allow]]
provider = "okta"
group = "robinauts-users"

# One more role, with the same matchers.
[[grant]]
provider = "okta"
group = "robinauts-admins"
roles = ["robinauts/admin", "oneberth/admin"]
```

Write Robinauts' as `/etc/robinauts/robinauts.toml`, beside it. A sign-in key in it is
refused, with where it went:

```toml
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
timeout_seconds = 120
max_output_tokens = 8192

[agents.assistant]
title = "Assistant"
# The models its turns may run on. The first is its default.
models = ["sonnet"]
engine = "langgraph"
system_prompt = "Play fair."
# Optional: how long one of its turns may run. Left out, max_turn_seconds.
turn_timeout_seconds = 3600
# Optional: the worker its turns run on, a process of its own. Left out, "default".
# worker = "special"

# Optional: a worker started with a command of its own in place of this build's
# worker module, say a container; its agents name it with `worker`.
# [workers.claude-container]
# command = ["docker", "run", "-i", "--rm", "--network", "host",
#            "-e", "ROBINAUTS_CONFIG", "-e", "ROBINAUTS_WORKER_NAME",
#            "-e", "ROBINAUTS_WORKER_BROKER_URL",
#            "-v", "/etc/robinauts/robinauts.toml:/etc/robinauts/robinauts.toml:ro",
#            "robinauts/claude-container"]

# Optional, here with its defaults.
[work]
max_turn_seconds = 1200
lease_seconds = 90
heartbeat_seconds = 30
sweep_seconds = 300
stalled_after_seconds = 900
max_running_tasks_per_worker = 100
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
  Completions, and the key is sent as
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
- **An agent's first model is its default.** An agent's `models` are the
  models its turns may run on. The default is copied into a conversation
  when the conversation starts, so changing it reaches new conversations
  only. Removing a model that conversations are using refuses their next
  turn, saying the conversation's model is no longer offered, rather than
  answering with another model; the log names the model.
- **`[work]`.** A turn may run for `max_turn_seconds`. An agent's
  `turn_timeout_seconds` replaces it for that agent's turns. A model's
  `timeout_seconds` bounds each call to that model. A server renews the lease
  of each turn it runs every `heartbeat_seconds`. A turn whose server died is
  ended once its lease of `lease_seconds` has passed. The next request about
  its conversation ends it, or a sweep every `sweep_seconds` does.
  `heartbeat_seconds` must be at most half of `lease_seconds`. A turn whose
  agent sends nothing for `stalled_after_seconds` is stopped as failed: no
  text, no tool call and no tool result. A single model call or tool call
  longer than that needs a larger value. Each of a server's workers runs
  at most `max_running_tasks_per_worker` turns at once, and the others wait
  queued. A turn queued for 48 hours is ended. A turn's time limit counts
  from when a server takes it.
- **Workers.** An agent's `worker` names the process its turns run in; the
  agents without one share the `default` worker, which always runs. The
  server spawns one process per worker named, and hands each the turns of its
  own agents alone. `max_running_tasks_per_worker` counts per worker
  ([specs/operations.md](specs/operations.md), "Workers"). A `[workers.<name>]`
  table names the command that starts that worker in place of this build's
  worker module, and that is all that sets it apart: the workers under
  [extra-workers/](../extra-workers/README.md) start that way, as containers.
- A file with no `[agents]` table is a deployment with no agents: it
  starts, the picker is empty, and the log says so.

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
    ONEBERTH_CONFIG=/etc/robinauts/oneberth.toml
    ROBINAUTS_DATABASE_URL=postgresql://robinauts:...@127.0.0.1:5432/robinauts
    ROBINAUTS_GOOGLE_SECRET=...
    ROBINAUTS_OKTA_SECRET=...
    ROBINAUTS_ANTHROPIC_KEY=...

Now create Robinauts' schema and oneberth's store, with that file:

    sudo systemd-run --pipe --wait --uid=robinauts \
        --property=EnvironmentFile=/etc/robinauts/environment \
        /var/lib/robinauts/venv/bin/robinauts db init
    sudo systemd-run --pipe --wait --uid=robinauts \
        --property=EnvironmentFile=/etc/robinauts/environment \
        /var/lib/robinauts/venv/bin/robinauts store init

`store init` makes oneberth's tables in the schema `oneberth`. neorc's tables are made in the
schema `neorc` at the server's first start.

It prints `the database is at schema version N (schema.sql …)` whether it
created the schema or found it already there. It works on an **empty**
database only: there are no migrations before the first release, and a
database made from another `schema.sql` is refused and made again rather
than upgraded. The command records
the file's hash in the database, and the server checks it at every start.

The local mode has **no** variable: it is asked for on the command line, `--local`, and
nowhere else, so nothing a process inherits can turn sign-in through providers off in a
deployment.

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
# A stop lets open connections finish for 10 s, then ends the runs in flight
# and gives back what the process holds, bounded at about 35 s more -- some
# 45 s in all. Anything under a minute here would SIGKILL the tail of that.
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

`RuntimeDirectory=` is required here: `ProtectSystem=strict` leaves
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
- **No buffering on the event stream**, and a read timeout longer than a
  whole turn. A stream sends a heartbeat comment every 15 s and a turn may
  run for 600 s by default. Every stream also carries
  `X-Accel-Buffering: no`, which nginx obeys — the setting below is there
  for the proxies that do not.
- The paths are `/ui/` (the interface), `/robinauts/` (Robinauts' API and its streams),
  `/neorc/` (neorc's), `/auth/` (sign-in), `/health` and each component's
  `/<name>/openapi.json`. All
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
        # read timeout past the longest turn.
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

A good start says five lines at `info` and no more: "Started server
process", "Waiting for application startup", "the start-up sweep ended
N run(s) left going by a process that went away", "Application startup
complete", "Uvicorn running on http://127.0.0.1:8000". The log goes to
**stdout**, one line each, with every query string cut off — one of this
platform's paths carries an authorization code in one.

`no [agents] table …` is an `info` line saying the picker will be empty.
`oneberth's local mode: …` is a warning, and means `--local` was asked for — which is never
right in a deployment.

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
   It reads no database. It answers 503 when any of this server's workers has
   not been heard from for five minutes: recycle that server.
2. Open `https://robinauts.example.com/` in a browser. It redirects to
   `/ui/` and shows the sign-in page with one button per provider.
3. Sign in as somebody the allow list has, through Google. Then as
   somebody else, through Okta. Each lands back where they started and
   sees an empty history.
4. Sign in as somebody the allow list does **not** have. The provider
   authenticates them; the platform sends them back to the sign-in page
   with `?error=not_allowed`, which reads: "The provider knows who you
   are, but this deployment does not let that account in. Ask whoever runs
   it." The provider's own words go to the log.
5. Hold a conversation. Ask the first person for the id in their address
   bar and fetch it as the second: `GET /robinauts/api/conversations/<id>` answers
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

**Restart.** `systemctl restart robinauts`. Runs still in flight are ended
and marked `interrupted`; their authors retry by sending the message
again. A configuration change — an agent's engine, an agent's default
model (for new conversations only), the allow list, a new agent — takes
effect at the next restart, because the file is read once at start-up.

**Upgrade.**

1. Stop the service.
2. In the same virtual environment, the same two steps as the first
   install, with the new artifact's own pair of files:
   `pip install --require-hashes -r requirements.txt --find-links neorc-wheels` then
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
- **Several backend processes.** One process, one machine.
- **Draining runs on shutdown.** A restart interrupts them.
- **A server's container image**, an SBOM and signed releases. Robinauts' image serves the
  desktop alone for now ([The desktop with Docker](#the-desktop-with-docker)).

## The desktop

`robinauts desktop` runs Robinauts as a desktop application on one machine. oneberth's launcher
starts each part as a process: the backend, one process per worker, neorc's worker when a flows
folder is set, and the window. The window is Chrome or Chromium without an address bar. The
person lands in it signed in, and types nothing.

```sh
ROBINAUTS_ANTHROPIC_KEY=… robinauts desktop
```

- The data directory is `~/.local/share/robinauts` on Linux, or `$XDG_DATA_HOME/robinauts`
  when that variable is set. On macOS it is `~/Library/Application Support/Robinauts`.
  `--data-dir` names another, and so does `ROBINAUTS_DATA_DIR`.
- The configuration is the file `ROBINAUTS_CONFIG` names, else `robinauts.toml` in the data
  directory. The first start makes that file empty. Its tables are those of step 4, and the
  example below is one. The keys are read from the environment of the command.
- `ROBINAUTS_FLOWS_DIR` names a flows folder, as for a server.
- The desktop needs no database server and no oneberth file. It needs Docker only when a worker
  runs in a container ([The desktop with Docker](#the-desktop-with-docker)).
- The launcher looks for Chrome, Chromium, Edge and Brave. `--browser` names another, and each
  `--browser-argument` goes to it. Without a browser, the default browser opens the address,
  and the application then stops on Ctrl-C alone.
- The window opens on port 8420 at the first start, or on a free port when 8420 is taken.
  Every later start keeps that port: the browser keeps the theme and the open application per
  address.
- Closing the window stops the application. Each worker finishes its turns within ten seconds
  and ends the rest as interrupted. The workers stop first, and the backend last.
- A second `robinauts desktop` on the same data directory brings the first one's window
  forward, and starts nothing.

The data directory holds:

| path | what it is | opened by |
|---|---|---|
| `robinauts.toml` | the configuration, when `ROBINAUTS_CONFIG` names none | every part |
| `robinauts.db`, `neorc.db`, `oneberth.db` | the records: Robinauts', neorc's and the sign-in | the backend alone |
| `workers/<worker name>/` | a worker's engines' state, one file per engine | that worker alone |
| `window/` | the window's browser profile | the window |
| `launcher.lock`, `launcher.json` | the launcher's lock, and the port of the window | the launcher |

What runs as a process, and what each one is given:

| process | command | given |
|---|---|---|
| the backend | `robinauts desktop-backend` | the launch secret on its standard input; where each listener is |
| each worker | `python -m robinauts.cli.worker_process`, or its table's `command` | a token of `robinauts/worker` on its standard input, bound to its name; the worker listener's address; the data directory |
| neorc's worker | `robinauts neorc-worker` | a token of `neorc/worker` on its standard input, bound to the queue `default`; both listeners' addresses |
| the window | the browser, with `--app` | the address that signs the person in |

The launcher names everything else in variables that start with `ONEBERTH_`. A worker's
`command` is started with them. A worker in a container is better named by its `image`, below.
The admin scripts, `robinauts tokens` and the rest, open PostgreSQL
alone: a desktop has none of them yet. A script reaches the API with a personal token from the
account page.

### The desktop with Docker

A worker whose `[workers.<name>]` table names an `image` runs in a container of that image. One
such worker puts the whole desktop in the Docker shape: the launcher then runs the backend in a
container too. A person who adds a first Docker worker keeps their data.

```toml
[agents.coder]
title = "Coder"
system_prompt = "You work in this conversation's directory."
models = ["opus"]
engine = "claude-code"
worker = "claude"

[workers.claude]
image = "robinauts-claude-code-worker"
# The variables of the desktop's environment the container gets, beside its agents' keys.
environment = ["ROBINAUTS_CLAUDE_AUTH", "CLAUDE_CODE_OAUTH_TOKEN"]
```

Build Robinauts' image and the worker's from the root of the repository, from the same
checkout as the `robinauts` command that starts them:

```sh
scripts/ui-packages-from-source.sh
docker build -t robinauts .
docker build -f extra-workers/claude-code-worker/Dockerfile -t robinauts-claude-code-worker .
```

- The backend runs in a container of the image `ROBINAUTS_IMAGE` names, `robinauts` by default.
  It sees the data directory at `/data`, and the configuration there or at
  `/etc/robinauts/robinauts.toml`. It gets every key the configuration names.
- The flows folder and the handlers' code are mounted into the backend's container read-only,
  at the paths the host has them. neorc's worker stays a process of the host: it runs the
  handlers from there, and reaches both listeners on the loopback address.
- Each worker's container sees its own folder, `workers/<worker name>/`, at
  `/data/workers/<worker name>/`. That folder holds its engines' state, a copy of the
  configuration that `robinauts desktop` writes at each start, and its home. The container
  runs as the person's own user, so the files it writes are the person's.
- A worker's container gets the keys of its agents' providers and tool servers, and the
  variables its table's `environment` names. It gets nothing else of the desktop's
  environment, and no database.
- Every worker of this version is a trusted worker. Its container joins the `app` network,
  which reaches the model providers and the tool servers. The launcher creates the `sandbox`
  network too, and no worker joins it.
- A service on the host, such as a local model or a local tool server, is
  `host.docker.internal` from a container. On Linux that name is the address of Docker's
  bridge: the service must listen there or on every address. A service on `127.0.0.1` alone
  is out of a container's reach.
- The workers without an `image` stay processes of the host. The front listener and the worker
  listener are published on the host's loopback address.
- The launcher refuses to start while Docker is down, and names the workers that need it.
- Closing the window stops every container and removes them and both networks. A start that
  crashed leaves them, and the next start removes them first.

Not in this version: an untrusted worker, a worker on the `sandbox` network, the egress proxy
that holds the real keys, a worker in a VM, and the narrower worker routes. A trusted worker's
keys are visible to `docker inspect` on that machine.

## The local mode

`robinauts start --local` is oneberth's local mode: no identity provider, one person, the one
at the machine, on the loopback interface only. It refuses any bind address that is not
loopback, and a oneberth file. The person signs in with a ticket: a launcher writes a launch
secret on the server's standard input and asks for the ticket; a start by hand prints
`open http://127.0.0.1:8000/auth/local?ticket=…` after `ready`, and that address signs the
person in once, within a minute.

```sh
ROBINAUTS_CONFIG=robinauts.toml ROBINAUTS_DATA_DIR=~/.robinauts robinauts start --local
```

```toml
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ROBINAUTS_ANTHROPIC_KEY"

[models.sonnet]
provider = "anthropic"
name = "claude-sonnet-5"

[agents.assistant]
title = "Assistant"
models = ["sonnet"]
engine = "pydantic-ai"
```

The mode keeps the records in the server's process, gone at the stop, or in the PostgreSQL
`ROBINAUTS_DATABASE_URL` names. With `ROBINAUTS_DATA_DIR`, it keeps them in files in that
folder, which the server makes: `robinauts.db`, `oneberth.db` and `neorc.db`. Each worker keeps
its engines' state in `workers/<worker name>/` in that folder, one file per engine. A
conversation from before a restart takes new turns ([specs/operations.md](specs/operations.md)).

It is the desktop's mode, and the mode for development. `robinauts desktop` starts the
backend in this mode ([The desktop](#the-desktop)). Nothing else in this guide uses it.

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
| `admin: sign-in is oneberth's …` | a sign-in key in Robinauts' file | move it to oneberth's file; an admin is a `[[grant]]` of `robinauts/admin` there |
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
| a stream dies after a fixed number of seconds | the proxy's read timeout | raise it past the longest turn |
| a write is refused with a `403` | the browser's `Origin` header does not equal `public_url`, string for string | the same cause as the sign-in loop: one origin, and `public_url` spelt as the browser spells it |
| the interface shows "not built" | a wheel without the interface, which cannot be built — so this is a source checkout, not a wheel | install the wheel |
