# How the demo works

> **Unofficial example.** See [README.md](README.md).

## The pieces

| File | Role |
|---|---|
| `start.sh` | Starts Docker, builds the interface and the backend, and runs the chat server. |
| `stop.sh` | Stops the chat server and the Docker services. |
| `compose.yaml` | PostgreSQL 16 and the four tool servers. |
| `robinauts.toml` | The Robinauts configuration: the provider, the models, the agents and the tools. |
| `fetch/`, `search/`, `files/` | The Dockerfiles of three tool servers. The browser uses Microsoft's image. |
| `.env.example` | The keys the demo reads. |

## What start.sh does

1. It reads the keys from the environment, then from `.env`.
2. It makes a database password and a key for the files tool, once, in `.state/`.
3. It starts PostgreSQL and the tools with Docker Compose. The search server starts only
   with `BRAVE_API_KEY`.
4. It builds the interface with `npm ci`, and installs the backend from its lock into
   `.state/venv`. Both happen at every start, so the demo runs the checkout as it is.
5. It picks the configuration. A tool without its key is taken off every agent, in a copy
   at `.state/robinauts.toml`.
6. It creates the schema, then runs the server in the local mode, with no sign-in.

The local mode accepts only a loopback host. For `ROBINAUTS_DEMO_HOST`, `start.sh` patches
that check out of the running process. The repository's code is unchanged.

## Ports

Every service listens on `127.0.0.1` only.

| Port | Service |
|---|---|
| 8000 | The chat (`ROBINAUTS_DEMO_PORT`) |
| 54391 | PostgreSQL |
| 8932 | fetch |
| 8933 | browser |
| 8934 | search |
| 8935 | files |

## Security

Keys stay in the environment. `robinauts.toml` names each key's variable, and the server
reads the value when a turn needs it. `.env` must have mode 600, or `start.sh` refuses it.

Generated secrets live in `.state/`, which has mode 700. They are the database password and
the files tool's key.

The tools run on Docker networks apart from the database. Playwright MCP always offers
`browser_run_code_unsafe`, which runs arbitrary code in its container. From there, a tool
reaches the internet and the LAN, but not the database or the host's loopback.

The files tool sees `agents-fs/` alone. It runs as your user, read-only apart from that
folder, and every request must carry its key.

The browser image is `mcr.microsoft.com/playwright/mcp:latest`, which is not pinned.
`files/` uses supergateway `4.2.0-rc.1`, a release candidate: 4.1.0 has no API key.

## Changing it

Agents, models and tools are in `robinauts.toml`. The models go through OpenRouter's
Anthropic-compatible API, so a model is any OpenRouter model name. A new tool server needs a
service in `compose.yaml` and a `[tool_servers]` table.
