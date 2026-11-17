# Sign-in, users and roles

Sign-in is oneberth's. oneberth's `docs/specs/identity.md` is the spec: the identity
providers, the allow list, the users, the sessions, the personal tokens, the device sessions,
pairing a phone, the service tokens and the local mode. This document says what Robinauts adds
to it.

## What Robinauts holds

- Its roles and permissions, as data, in `backend/src/robinauts/web/roles.py`. Every route of
  its component declares one permission ([web.md](../architecture/web.md), "Roles and
  permissions").
- The checks on ownership: whose conversation, project or turn this is. The controller makes
  them, for the person a request is for.
- Its own row of each person, under oneberth's id for them, in its `users` table: their
  projects, conversations and change version hang from it. oneberth holds the user itself.

## Roles

| role | given to | resource |
|---|---|---|
| `robinauts/user` | every person an allow entry admits | none |
| `robinauts/admin` | the people a `[[grant]]` entry names | none |
| `robinauts/worker` | a service token | one worker name |
| `robinauts/handler` | a service token | none |

- `robinauts/user` is Robinauts' default role. An allow entry gives it, beside `neorc/user`.
- `robinauts/admin` holds what `robinauts/user` holds. No route is an admin's alone yet.
- `robinauts/worker` holds the workers' routes and nothing else. The server mints one for each
  worker it starts, bound to the worker's name.
- `robinauts/handler` is what a flow's handler holds. It acts for the person who started its
  run ([orchestration.md](../architecture/orchestration.md), "Whose a run is").

## Two modes

- **Through identity providers.** `robinauts start` reads oneberth's configuration from the
  file `ONEBERTH_CONFIG` names: the providers, the allow list, the grants, and the backend's
  `public_url` under `[apps.robinauts]`. Robinauts' own file, `ROBINAUTS_CONFIG`, holds no
  sign-in key, and refuses one with where it went.
- **Locally.** `robinauts start --local` is oneberth's local mode: one person, the one at the
  machine, signed in with a ticket. It needs no oneberth file and refuses one. A browser
  reaches the front listener on a loopback address. Behind a forward, such as a VM's, the
  front listener binds `--host` and `--public-host` names that loopback address. Development
  uses this mode too.

```toml
# ONEBERTH_CONFIG
[apps.robinauts]
public_url = "https://robinauts.example.com"

[providers.okta]
title = "Okta"
issuer = "https://example.okta.com/oauth2/default"
client_id = "..."
client_secret_env = "ONEBERTH_OKTA_SECRET"
groups_claim = "groups"

[[allow]]
provider = "okta"
group = "platform-users"

[[grant]]
provider = "okta"
group = "platform-admins"
roles = ["robinauts/admin", "oneberth/admin"]
```

Each provider registers the redirect URI `{public_url}/auth/callback/{provider}`.

## Admin scripts

`robinauts store`, `robinauts tokens`, `robinauts sessions` and `robinauts devices` are
oneberth's admin scripts, with oneberth's, Robinauts' and neorc's roles. They open oneberth's
store on the PostgreSQL `ROBINAUTS_DATABASE_URL` names, or `--database-url`.

```sh
robinauts store init
robinauts tokens create worker-gpu --role robinauts/worker --resource gpu
robinauts devices end --user ada@example.com
```

A service token is created and revoked with `robinauts tokens`, by the launcher, or by an admin
from a browser session. An admin's bearer is answered 403 there. The backend makes the tokens
of its own workers and handlers in its own memory.
