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

The demo reads exactly three variables, and uses whichever it finds:

| variable | what it reaches | where to get one |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter, as an `anthropic-compatible` provider | <https://openrouter.ai/keys> |
| `ANTHROPIC_API_KEY` | Anthropic itself, as an `anthropic` provider | <https://console.anthropic.com/> |
| `OPENAI_API_KEY` | OpenAI itself, as an `openai` provider, over Chat Completions | <https://platform.openai.com/api-keys> |

With more than one set it uses OpenRouter first, then Anthropic, then OpenAI:
OpenRouter because it reaches every vendor's models, and the other two in
the order the demo grew in. `ROBINAUTS_DEMO_PROVIDER=anthropic` or
`=openai` chooses one outright. With none it says so and stops.

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
([../docs/deployment.md](../docs/deployment.md)). `start.sh` also takes all
three variables **out of its own environment** as the first thing it does,
before it runs anything, and puts the one that is used back on the server's
invocation alone: nvm and the node it may run, uv, PostgreSQL, and npm and
everything npm runs are all started without it. A key in `demo/.env` is never
in the environment at all until that one invocation. That
holds for `OPENAI_API_KEY` too, although the OpenAI client would read it by
itself if it were let: the configuration names it, the platform reads it at
start-up, and the engines hand it to the client — no client is ever left to
find a key in the environment.

**OpenRouter is reached with the Anthropic client.** OpenRouter serves
Anthropic's Messages API at `https://openrouter.ai/api/v1/messages` and takes
the key in the same `x-api-key` header, so the demo reaches it as an
`anthropic-compatible` provider whose `base_url` is `https://openrouter.ai/api`
— the prefix the client appends `/v1/messages` to. It serves OpenAI's Chat
Completions too, and a configuration of your own may reach it as an
`openai-compatible` provider at `https://openrouter.ai/api/v1` instead; the
demo does not, so the OpenRouter models below are the ones it has always had.
**OpenAI is reached with the OpenAI client**, at `https://api.openai.com/v1`,
which both engines pin ([../docs/deployment.md](../docs/deployment.md)).

## What you get

Two agents, one per engine:

- **Assistant (LangGraph)**
- **Assistant (Pydantic AI)**

An agent is chosen when a conversation is started and stays with it, so
**seeing the swap means starting a chat with each**: ask both the same thing,
and then look at the two conversations in the panel. The same configuration,
the same model, the same stored format; two frameworks underneath. That is the
seam the project exists to prove
([../docs/specs/agent-engines.md](../docs/specs/agent-engines.md)).

And three models, in the picker beside the agent's. Which three depends on
the key:

| through | the first, each agent's default | the second | the third |
|---|---|---|---|
| OpenRouter | Claude Sonnet 5 (`anthropic/claude-sonnet-5`) | GPT-5.5 (`openai/gpt-5.5`) | Gemini 3.8 Flash (`google/gemini-3.8-flash`) |
| Anthropic | Claude Sonnet 5 (`claude-sonnet-5`) | Claude Opus 5.5 (`claude-opus-5-5`) | Claude Haiku 4.5 (`claude-haiku-4-5`) |
| OpenAI | GPT-5.5 (`gpt-5.5`) | GPT-5.4 Mini (`gpt-5.4-mini`) | GPT-5.4 Nano (`gpt-5.4-nano`) |

Through OpenRouter the three are three vendors' models: the demo reaches
OpenRouter over Anthropic's Messages API, and OpenRouter passes a request on
to whichever vendor serves the model named. Anthropic serves Claude alone and
OpenAI serves GPT alone, so through either of those the three are one
vendor's. All the names were on OpenRouter's and the vendors' own lists of
models on 2026-09-28.

**Through OpenRouter, a turn on GPT or Gemini has not been tried here.**
OpenRouter documents non-Anthropic models reached over the Messages API with
"compatibility limitations". If one of them fails every turn, the log says
why, and the other models still answer.

**Through OpenAI, the three are GPT-5.5 and the smaller two of the GPT-5.4
family**, which do not reason unless asked — and deliberately not GPT-6 or
GPT-5.6. The demo's agents are given GitHub's tools whenever a GitHub token is
set ("Tools", below), every turn then offers those tools to the model, and on
the newer models a request that offers tools may be refused over Chat
Completions, the protocol the engines speak to OpenAI:

- OpenAI documents function calling on GPT-6 Sol and GPT-6 Luna only with
  reasoning turned off, and sends GPT-6 Astra's tools to its Responses API;
- OpenAI Support reported GPT-5.6 Sol refusing function tools with its
  reasoning on (OpenAI's developer forum, post 1386454, 2026-09-07);
- GPT-5.6 Luna and Terra reason by default, and have not been tried.

The engines do not turn reasoning off. So one of those models named in
`ROBINAUTS_DEMO_MODEL` (or `_2`, `_3`) is only safe with no GitHub token set,
when the agents have no tools; with a token, every turn on it may fail (see
"When something goes wrong").

Unlike the agent, **the model can be changed at any point** in a
conversation: the next answer comes from the new one, and every answer records
the model that wrote it. Either engine runs any of the three.

What a conversation and an answer record is the model's **id** in the
configuration, which names the default model whichever key reaches it:
`claude-sonnet-5` through OpenRouter and through Anthropic, `gpt-5-5` through
OpenRouter and through OpenAI, and `gemini-3-8-flash`, `claude-opus-5-5`,
`claude-haiku-4-5`, `gpt-5-4-mini` and `gpt-5-4-nano` through the one key each
that has them. So after a restart on another key, with the default models, a
conversation on a model that key also offers carries on with it, and one on a
model that key does not offer is refused, saying the model is no longer
offered, and the picker shows its id until another is picked; nothing is
answered by a different model under the old one's id. (With the OpenAI key
the agents' default is GPT-5.5, so a conversation started on Claude Sonnet 5
is one of those refused until another model is picked.) **That holds only while no model is overridden**: a model named
by one of the variables below keeps the default's id (see there), and then the
id no longer names the model that answered.

## What you can change

Every one of these is read by `start.sh` when it starts, and none of them is
needed:

| variable | default | what it does |
|---|---|---|
| `ROBINAUTS_DEMO_PROVIDER` | the first key set of `openrouter`, `anthropic`, `openai` | `openrouter`, `anthropic` or `openai` |
| `ROBINAUTS_DEMO_MODEL` | `anthropic/claude-sonnet-5`, or `claude-sonnet-5` for Anthropic, or `gpt-5.5` for OpenAI | the vendor's name for the first model, the agents' default |
| `ROBINAUTS_DEMO_MODEL_2` | `openai/gpt-5.5`, or `claude-opus-5-5` for Anthropic, or `gpt-5.4-mini` for OpenAI | the vendor's name for the second |
| `ROBINAUTS_DEMO_MODEL_3` | `google/gemini-3.8-flash`, or `claude-haiku-4-5` for Anthropic, or `gpt-5.4-nano` for OpenAI | the vendor's name for the third |
| `ROBINAUTS_DEMO_PORT` | `8000` | where the server listens |
| `ROBINAUTS_DEMO_PG_PORT` | `54390` | where the demo's PostgreSQL listens |
| `ROBINAUTS_DEMO_OPEN` | `1` | `0` does not open a browser |

The model names are the vendor's own: OpenRouter's are `<vendor>/<model>`
(`anthropic/claude-sonnet-5`), Anthropic's and OpenAI's are plain
(`claude-sonnet-5`, `gpt-5.5`). A
model named this way is shown in the picker by that name, since the demo has no
title to give it. Changing one takes a restart — `demo/stop.sh` then
`demo/start.sh` — because the configuration is read once, at start-up.

**A model named this way keeps the id of the one it replaces.**
`ROBINAUTS_DEMO_MODEL_2=openai/gpt-5.6-sol` is still `gpt-5-5` in the
configuration, so what the conversations and answers record is that id, not
the vendor's name: answers written before and after the change carry the same
one, and the id no longer says which model wrote them.

## Tools

The configuration the demo writes can hold three MCP tool servers and a `tools`
line under each agent ([robinauts.toml.in](robinauts.toml.in)); an agent whose
`tools` names a server can call what that server offers, and the chat shows each call behind a
tool-call toggle above the answer -- the tool's name, its arguments and what
came back, as text.

**GitHub's server is on whenever a GitHub token is exported** in the shell
that runs `start.sh`, as `ROBINAUTS_GITHUB_TOKEN`. Both agents then get `tools = ["github"]`, and a
question about a repository has the model call, say,
`get_latest_release`; the answer arrives with the call's toggle above
it. `start.sh` says `Tools: GitHub's MCP server, for both agents.` when it is
on.

The token is handled like the model key: taken **out of the script's
environment** as the first thing it does, so nvm, node, uv, PostgreSQL and
npm never see it, and put back on the server's invocation alone, as
`ROBINAUTS_GITHUB_TOKEN`, the variable `[tool_servers.github]` names. The
configuration holds that name and nothing else. **The agents act as the
token's owner** on `api.githubcopilot.com`, with whatever the token may do,
so a token with no more than read access to public repositories is the one to
give a demo.

**Microsoft Learn's server needs no credential.** Take the `#` off the
`[tool_servers.learn]` table in `robinauts.toml.in`, add `"learn"` to an
agent's `tools` (or give it the line, `tools = ["learn"]`, when GitHub is
off), restart (`demo/stop.sh`, then `demo/start.sh`), and ask that agent
something about, say, Azure: the model calls `microsoft_docs_search`.
**The demo then reaches `learn.microsoft.com` from this machine** for as long
as the line is on. What the request carries is no credential and nothing that
names you, but it does carry what the model wrote for the tool -- the search
query it composed from your question -- and the deployment's address, as any
request does.

**Composio's hosted MCP servers take their key in `x-api-key`.** The
`[tool_servers.composio]` table in `robinauts.toml.in` is written for that,
with `auth = "header"` and `header = "x-api-key"`. In Composio's dashboard,
create an MCP server from the toolkits you want and copy its id into the URL
in place of `SERVER_ID`, and take the project's API key from the project's
settings; export the key as `COMPOSIO_API_KEY` in the shell that runs
`start.sh`, which, unlike GitHub's token, stays in the script's environment.
Put a Composio user id in place of `USER_ID`: the tools act through the
accounts that user has connected in Composio. **That user is you**, the
operator, for everybody who chats: a deployment is one identity to a tool
server, so the `user_id` in the URL is not the person in the chat. Then take
the `#` off the table, name `"composio"` in an agent's `tools` and restart, as
for Microsoft Learn. The URL ends in `/mcp`, the server's streamable HTTP
endpoint; the bare server URL answers with a redirect to it.

## Where the state lives

All of it in `demo/.state/`, and none of it is committed
([.gitignore](.gitignore)):

| | |
|---|---|
| `pgdata/` | the PostgreSQL data directory: the conversations are in here |
| `pg-password` | the database's password, made at random on the first start |
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

The cluster listens on the loopback interface only, and every connection needs
the password in `demo/.state/pg-password`, which the first start makes at random
(the URL `pg.py url` prints includes it). That is a demo on one person's machine;
a deployment does none of it
([../docs/deployment.md](../docs/deployment.md)).

## When something goes wrong

Every failure is one line, and the server's own log is
`demo/.state/server.log`.

| what you see | why | what to do |
|---|---|---|
| `The demo needs one model provider key` | none of the three variables is set | set one of them, or write `demo/.env` |
| every GitHub tool call fails with `401` | the token is wrong, expired or revoked | a new token, or unset `ROBINAUTS_GITHUB_TOKEN` to go on without |
| `demo/.env is mode 644 and may hold a key` | anybody on this machine could read it | `chmod 600 demo/.env` |
| every answer fails, and the log says `authentication_error` / `API key is invalid` / `User not found` / `invalid_api_key` | the key is wrong, expired, or belongs to another vendor | check the key; `User not found` is OpenRouter's way of saying it has never issued that one, and `invalid_api_key` OpenAI's |
| every answer on one model fails, and the log's message names the model (`model_not_found`, from OpenAI) | that vendor has retired it, or spells it differently, or the key's project may not use it | pick another in the picker; set `ROBINAUTS_DEMO_MODEL`, `_2` or `_3` and start again; OpenRouter lists its ids at <https://openrouter.ai/models>, OpenAI at <https://platform.openai.com/docs/models> |
| with the OpenAI key and a GitHub token, every turn on one model fails, and the log names function tools and `reasoning_effort` | the model does not take tools over Chat Completions with its reasoning on: GPT-6 Sol and Luna by OpenAI's documentation, GPT-5.6 Sol by OpenAI Support's report, and possibly the other GPT-5.6 models, which reason by default | pick one of the demo's own three, or unset `ROBINAUTS_GITHUB_TOKEN` so that the agents have no tools |
| `ROBINAUTS_DEMO_PROVIDER=openai, but OPENAI_API_KEY is not set.` (or another provider's name) | the provider chosen has no key | set that key, or choose another |
| `node is not on the PATH` | the interface is built from source | install the Node.js in `frontend/.nvmrc`, or `nvm use` in `frontend/` |
| `uv is not on the PATH` | everything Python is fetched with it | install uv |
| `Already running on http://127.0.0.1:8000/` | it is already up | `demo/stop.sh` first, or just open the page |
| `the server did not come up`, with the log below it | the log says which: most often a configuration refusal | read the lines it printed; `demo/stop.sh --reset` starts from nothing |
| something else is on port 8000 or 54390 | another server, or another copy of this | `ROBINAUTS_DEMO_PORT` / `ROBINAUTS_DEMO_PG_PORT` |
