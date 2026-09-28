# The demo

One command brings the whole platform up on this machine, and one takes it
down again:

    demo/start.sh          # everything: a database, the interface, the server
    demo/stop.sh           # everything down again; the conversations stay
    demo/stop.sh --reset   # and delete demo/.state, which is the data

`start.sh` prints a `http://127.0.0.1:8000/` to open, and opens it for you when
this machine has `xdg-open` and a display to open it on — over ssh, and in a
container, the printed URL is the whole of it.

**It is the local development mode, and it is not a deployment**
([../docs/deployment.md](../docs/deployment.md)). There is no sign-in at all:
everything runs as one fixed local user, the server binds the loopback
interface and refuses any other address, and the interface shows a banner
saying so. Nothing here is reachable from another machine, nothing is behind a
password, and none of it is a way to put Robinauts in front of anybody. For
that, the guide is the deployment guide.

## What it needs

Linux or macOS, and:

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which fetches
  the backend's locked dependencies, **CPython 3.12** and the demo's
  PostgreSQL. 3.12 is what the demo runs everything on: it is the version CI
  judges every gate on and `backend/uv.lock` is resolved for, so the demo runs
  the platform on the interpreter that was checked rather than on whichever one
  this machine has;
- Node.js at the version in [../frontend/.nvmrc](../frontend/.nvmrc), because
  the interface is built from source. With
  [nvm](https://github.com/nvm-sh/nvm) installed, `start.sh` asks it for that
  version itself;
- **one model provider key**, below.

It needs no PostgreSQL, no Docker and no `sudo`. It refuses to run as root.

## The key

The demo reads exactly two variables, and uses whichever it finds:

| variable | what it reaches | where to get one |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter, as an `anthropic-compatible` provider | <https://openrouter.ai/keys> |
| `ANTHROPIC_API_KEY` | Anthropic itself, as an `anthropic` provider | <https://console.anthropic.com/> |

With both set it uses OpenRouter, and `ROBINAUTS_DEMO_PROVIDER=anthropic`
chooses the other. With neither it says so and stops.

Either from the environment:

    export OPENROUTER_API_KEY=...
    demo/start.sh

or from `demo/.env`, which is never committed and is read **only** when it is
mode 0600, since it holds a key:

    printf 'OPENROUTER_API_KEY=%s\n' "$THE_KEY" > demo/.env
    chmod 600 demo/.env

**The key is never printed, never written into a file, and reaches nothing but
the server.** `demo/.env` is read rather than sourced — a `.env` that could run
commands would be the demo executing whatever was pasted into it — and the
configuration the demo generates holds the *name* of the variable and nothing
else, which is the rule the platform keeps everywhere
([../docs/specs/agents.md](../docs/specs/agents.md)). `start.sh` also takes both
variables **out of its own environment** as soon as it has read them, and puts
the one that is used back on the server's invocation alone: PostgreSQL, npm and
everything npm runs, and uv are started without it.

**OpenRouter is reached with the Anthropic client.** OpenRouter serves
Anthropic's Messages API at `https://openrouter.ai/api/v1/messages` and takes
the key in the same `x-api-key` header, so this build reaches it as an
`anthropic-compatible` provider whose `base_url` is `https://openrouter.ai/api`
— the prefix the client appends `/v1/messages` to. No OpenAI client is
involved, which matters because that one does not pass the dependency policy
([../DEPENDENCIES.md](../DEPENDENCIES.md), "Known exclusions").

## What you get

Two agents, one per engine:

- **Assistant (LangGraph)**
- **Assistant (Pydantic AI)**

An agent is chosen when a conversation is started and stays with it, so
**seeing the swap means starting a chat with each**: ask both the same thing,
and then look at the two conversations in the panel. The same configuration,
the same model, the same stored format; two frameworks underneath. That is the
seam the project exists to prove
([../docs/specs/agents.md](../docs/specs/agents.md)).

And three models, in the picker beside the agent's. Which three depends on
the key:

| through | the first, each agent's default | the second | the third |
|---|---|---|---|
| OpenRouter | Claude Sonnet 5 (`anthropic/claude-sonnet-5`) | GPT-5.5 (`openai/gpt-5.5`) | Gemini 3.8 Flash (`google/gemini-3.8-flash`) |
| Anthropic | Claude Sonnet 5 (`claude-sonnet-5`) | Claude Opus 5.5 (`claude-opus-5-5`) | Claude Haiku 4.5 (`claude-haiku-4-5`) |

OpenRouter routes the one Messages API this build speaks to every vendor's
models, so through it the three are three vendors; Anthropic serves Claude
alone. The OpenRouter names were on its list of models when they were chosen,
and **a turn on GPT or Gemini through that API has not been tried here**:
OpenRouter documents non-Anthropic models behind it with "compatibility
limitations". If one of them fails every turn, the log says why, and the
other models still answer.

Unlike the agent, **the model can be changed at any point** in a
conversation: the next answer comes from the new one, and every answer records
the model that wrote it. Either engine runs any of the three.

What a conversation and an answer record is the model's **id** in the
configuration, which names the default model: `claude-sonnet-5` either way,
and `gpt-5-5` and `gemini-3-8-flash` through OpenRouter, `claude-opus-5-5` and
`claude-haiku-4-5` through Anthropic. So after a restart on the other key, with
the default models, a conversation on a model that key does not offer is
refused, saying the model is no longer offered, and the picker shows its id
until another is picked; nothing is answered by a different model under the
old one's id. **That holds only while no model is overridden**: a model named
by one of the variables below keeps the default's id (see there), and then the
id no longer names the model that answered.

## What you can change

Every one of these is read by `start.sh` when it starts, and none of them is
needed:

| variable | default | what it does |
|---|---|---|
| `ROBINAUTS_DEMO_PROVIDER` | `openrouter` when its key is set | `openrouter` or `anthropic` |
| `ROBINAUTS_DEMO_MODEL` | `anthropic/claude-sonnet-5`, or `claude-sonnet-5` for Anthropic | the vendor's name for the first model, the agents' default |
| `ROBINAUTS_DEMO_MODEL_2` | `openai/gpt-5.5`, or `claude-opus-5-5` for Anthropic | the vendor's name for the second |
| `ROBINAUTS_DEMO_MODEL_3` | `google/gemini-3.8-flash`, or `claude-haiku-4-5` for Anthropic | the vendor's name for the third |
| `ROBINAUTS_DEMO_PORT` | `8000` | where the server listens |
| `ROBINAUTS_DEMO_PG_PORT` | `54390` | where the demo's PostgreSQL listens |
| `ROBINAUTS_DEMO_OPEN` | `1` | `0` does not open a browser |

The model names are the vendor's own: OpenRouter's are `<vendor>/<model>`
(`anthropic/claude-sonnet-5`), Anthropic's are plain (`claude-sonnet-5`). A
model named this way is shown in the picker by that name, since the demo has no
title to give it. Changing one takes a restart — `demo/stop.sh` then
`demo/start.sh` — because the configuration is read once, at start-up.

**A model named this way keeps the id of the one it replaces.**
`ROBINAUTS_DEMO_MODEL_2=openai/gpt-5.6-sol` is still `gpt-5-5` in the
configuration, so what the conversations and answers record is that id, not
the vendor's name: answers written before and after the change carry the same
one, and the id no longer says which model wrote them.

## Tools

Off until you turn them on. The configuration the demo writes holds, commented
out, two MCP tool servers and a `tools` line under each agent
([robinauts.toml.in](robinauts.toml.in)); an agent whose `tools` names a server
can call what that server offers, and the chat shows each call, its arguments
and what came back ([../docs/specs/agents.md](../docs/specs/agents.md),
"Tools").

**Microsoft Learn's server needs no credential.** Take the `#` off the
`[mcp_servers.learn]` table and off one agent's `tools = ["learn"]` line in
`robinauts.toml.in`, restart (`demo/stop.sh`, then `demo/start.sh`), and ask
that agent something about, say, Azure: the model calls
`learn__microsoft_docs_search`, the answer arrives with the call shown above
it, and the conversation records both. **The demo then reaches
`learn.microsoft.com` from this machine** for as long as the line is on, with
no credential and nothing about you in the request beyond what any request
carries.

**GitHub's server needs a token**, read from the variable
`[mcp_servers.github]` names and never from the file. The demo does not carry
that variable for you: `start.sh` reads the model provider's key alone and
strips it from everything but the server, so a token exported in the shell
that runs `start.sh` would reach every process it starts — PostgreSQL, npm and
what npm runs, uv. Put the server behind a shell of its own, or wait for the
deployment shape ([../docs/deployment.md](../docs/deployment.md)), before
handing the demo a token that can write.

## Where the state lives

All of it in `demo/.state/`, and none of it is committed
([.gitignore](.gitignore)):

| | |
|---|---|
| `pgdata/` | the PostgreSQL data directory: the conversations are in here |
| `venv/` | the platform installed from `backend/uv.lock` (see below) |
| `robinauts.toml` | the configuration, written from [robinauts.toml.in](robinauts.toml.in) by [config.py](config.py) |
| `server.log` | everything the server said |
| `server.pid` | what `stop.sh` signals |

`demo/stop.sh --reset` deletes the lot, and the next `start.sh` builds it
again from nothing.

The interface is built only when `frontend/dist/index.html` is not there, so
after changing the interface, build it again yourself (`npm run build` in
`frontend/`) or delete `frontend/dist` and start the demo again. Backend
changes need no such thing: the platform is reinstalled on every start.

**The demo installs into an environment of its own**, non-editably, rather
than using `backend/.venv`. The interface is served from *inside* the installed
package, as `robinauts/ui/`, which is where the wheel's build hook puts
`frontend/dist`; an editable install has no such directory and `/ui/` would
answer "the interface is not built" however many times it had been. Doing it
this way also leaves a developer's `backend/.venv` exactly as it was. It is
installed from `backend/uv.lock` on CPython 3.12, with `--no-dev`: the runtime
set a deployment gets and no test or lint tool. The project itself is
reinstalled on every start, so the demo always shows the code that is in the
tree.

## The PostgreSQL it brings

A throwaway cluster under `demo/.state/pgdata`, on `127.0.0.1:54390`, started
and stopped by [pg.py](pg.py). The server binaries come from **`pgserver`
0.1.4**, a PyPI package that ships PostgreSQL's own binaries (**PostgreSQL
16.2**) in a wheel. Its wheel carries the **Apache-2.0** licence text as its
`LICENSE` file, and PostgreSQL itself is under the PostgreSQL licence; both are
on the allowed list in [../DEPENDENCIES.md](../DEPENDENCIES.md).

**It is not a dependency of the platform**, and cannot be: its metadata states
no licence *identifier*, only the text, so the licence gate cannot classify it
and fails closed — which is why it already has a row under "Known exclusions".
It is a **tool**, fetched by `uv run --with pgserver==0.1.4` into a throwaway
environment of its own, exactly as `reuse` and `pip-audit` are: nothing under
`backend/` imports it, and it is not in `backend/uv.lock`.

To look inside it while the demo is running, ask `pg.py` where it is:

    uv run --no-project --python 3.12 --with pgserver==0.1.4 python demo/pg.py url

The cluster listens on the loopback interface only and uses `trust`
authentication, so there is no password for a script to keep. That is a demo on
one person's machine; a deployment does none of it
([../docs/deployment.md](../docs/deployment.md)).

## When something goes wrong

Every failure is one line, and the server's own log is
`demo/.state/server.log`.

| what you see | why | what to do |
|---|---|---|
| `The demo needs one model provider key` | neither variable is set | set one of the two, or write `demo/.env` |
| `demo/.env is mode 644 and may hold a key` | anybody on this machine could read it | `chmod 600 demo/.env` |
| every answer fails, and the log says `authentication_error` / `API key is invalid` / `User not found` | the key is wrong, expired, or belongs to the other vendor | check the key; `User not found` is OpenRouter's way of saying it has never issued that one |
| every answer on one model fails, and the log's message names the model | that vendor has retired it, or spells it differently | pick another in the picker; set `ROBINAUTS_DEMO_MODEL`, `_2` or `_3` and start again; OpenRouter lists its ids at <https://openrouter.ai/models> |
| `node is not on the PATH` | the interface is built from source | install the Node.js in `frontend/.nvmrc`, or `nvm use` in `frontend/` |
| `uv is not on the PATH` | everything Python is fetched with it | install uv |
| `Already running on http://127.0.0.1:8000/` | it is already up | `demo/stop.sh` first, or just open the page |
| `the server did not come up`, with the log below it | the log says which: most often a configuration refusal | read the lines it printed; `demo/stop.sh --reset` starts from nothing |
| something else is on port 8000 or 54390 | another server, or another copy of this | `ROBINAUTS_DEMO_PORT` / `ROBINAUTS_DEMO_PG_PORT` |
