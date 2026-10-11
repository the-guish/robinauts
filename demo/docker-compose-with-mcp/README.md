# Robinauts with MCP tools in Docker

> **Unofficial example.** This folder is a throwaway demo, shared as is. It is not
> intended to be merged in this state, and nothing else in the repository depends on it.

This demo runs Robinauts on your machine with agents that use real tools. The tools are
MCP servers in Docker:

| Tool | What it does | Needs |
|---|---|---|
| fetch | Reads a web page as text. | Nothing |
| browser | Drives a headless Chromium (Playwright MCP). | Nothing |
| files | Reads and writes `agents-fs/`, in this folder. | Nothing |
| search | Searches the web through Brave. | `BRAVE_API_KEY` |
| composio | Reaches Composio's apps, such as Google Maps. | `COMPOSIO_API_KEY` |

There are three agents. Researcher (Claude Sonnet 5) answers questions with links. Pi
Sidekick (Claude Opus 5.5) does code, errands and research reports in `agents-fs/`. Echo
calls no model, and serves to test the interface.

[how-it-works.md](how-it-works.md) explains the services, the configuration and the
security choices.

## What you need

- Linux, with Docker and Docker Compose.
- [uv](https://docs.astral.sh/uv/) and Python 3.12.
- Node.js and npm, at the version in `frontend/.nvmrc`. nvm is used when it is installed.
- An [OpenRouter](https://openrouter.ai/keys) key. Brave and Composio keys are optional.

This folder must stay at `demo/docker-compose-with-mcp` in a Robinauts checkout. The demo
builds the backend and the interface from that checkout.

## Run it

```sh
cd demo/docker-compose-with-mcp
cp .env.example .env
chmod 600 .env
# Fill in .env, then:
./start.sh
```

Open <http://127.0.0.1:8000/>. The first start takes a few minutes: it builds the Docker
images, the interface and the backend.

The keys can also come from the environment. Exported variables win over `.env`.

| Variable | Default | Effect |
|---|---|---|
| `ROBINAUTS_DEMO_PORT` | `8000` | The port of the chat. |
| `ROBINAUTS_DEMO_HOST` | `127.0.0.1` | `0.0.0.0` serves the chat to your local network. |

The chat has no sign-in. With `ROBINAUTS_DEMO_HOST=0.0.0.0`, anyone on your network can use
it, and spend your keys through it.

## Stop it

`Ctrl-C` stops the chat server. `./stop.sh` stops the server, PostgreSQL and the tools.
Conversations stay in a Docker volume, and files stay in `agents-fs/`.

`./stop.sh --reset` also deletes the volume, and with it every conversation.

## What it leaves behind

| Path | Contents |
|---|---|
| `.state/` | The Python environment, the generated passwords, the server's pid. |
| `agents-fs/` | The files the agents wrote. |
| Docker | Project `robinauts-mcp-demo`: the images and the `pgdata` volume. |

git ignores `.env`, `.state/` and `agents-fs/`.
