# Connectors: the plan

Working notes, warm-up. The reasoning is in [background.md](background.md).

## Layout

    clients/
      android/                       planned
      connectors/                    the Python project: robinauts-connectors
        pyproject.toml, uv.lock
        src/robinauts_connectors/
          domain.py                  messages, people, threads, answer events
          connector.py               Connector, Reply, Inbox: the contract
          bridge.py                  the Inbox: access, thread links, one turn at a time
          api.py                     the Robinauts API client
          links.py, settings.py, registry.py
          connect.py                 the connect runner: holds every connection
          serve.py                   the serve runner: one webhook route per platform
          cli.py                     robinauts-connectors connect | serve
          telegram/  slack/          one sub-package per platform
        tests/

Code is grouped by platform. Whether a platform is connected or served is a
property it declares (`Connector.modes`) and a command chooses, since Slack and
Telegram can be either.

## Publishing

- One distribution, **`robinauts-connectors`**, separate from the server's
  `robinauts`, released from the same tag with the same version.
- An install takes extras: one per platform, plus `webhook` (FastAPI, uvicorn),
  which every serve-only platform pulls in itself.

      pip install "robinauts-connectors[slack,telegram]"
      robinauts-connectors connect slack telegram          # no public URL, one replica

      pip install "robinauts-connectors[teams,whatsapp]"
      robinauts-connectors serve teams whatsapp --port 8080  # public HTTPS, scales out

- Two images from one Dockerfile, by an `EXTRAS` build argument.
- Connectors are found through the entry-point group `robinauts.connectors`,
  so an outside package can add one.

## Imports

- Import name `robinauts_connectors`, not a namespace under `robinauts`.
- Nothing imports `robinauts`; the server imports nothing of the connectors.
- `aiogram` only under `telegram/`, `slack_bolt` and `slack_sdk` only under
  `slack/`, FastAPI only in `serve.py`, `ag_ui` and `httpx` only in `api.py`.
  Enforced by import-linter contracts in `pyproject.toml`.

## The API

- **Endpoint**: the proof of concept's, ported to `main` (background.md,
  section 4), renamed to this work's vocabulary:
  `POST /api/connectors/agents/{agent_id}/turns`.
- **Request**: `{"conversation": id | null, "person": {"id", "name"}, "text",
  "model_id"?}`. The answer is the turn's AG-UI stream, with
  `X-Robinauts-Conversation-Id`.
- **Auth**: `ROBINAUTS_CONNECTORS_SECRET` as a bearer for now. Then a token
  minted per connector, naming the identities it may assert.
- **People**: users of their own, under a provider no sign-in configuration
  can name. **History**: the server's; the connector sends the newest message
  only. **Busy**: one turn per conversation, 409, so the bridge serialises
  per thread.

## Next

1. Port the endpoint, with its tests.
2. Fill in the core and Telegram on connect, then Slack.
3. A gate script and a CI job; the licence gate and pip-audit over
   `clients/connectors/uv.lock`.
