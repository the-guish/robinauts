# The base worker

The backend's own worker, `robinauts.cli.worker_process`, in a container: an
image the server starts with `docker run -i`, which runs the
turns of the agents on its worker with any engine the backend ships, LangChain,
Pydantic AI or the echo engine. It starts through the extra workers' shared
start, `python -m robinauts_worker_common`, which checks the GitHub token it
may be handed, and it carries git, to clone repositories with that token
([../worker-common/README.md](../worker-common/README.md)). It is the base of every other extra worker: copy
the folder, add the package that brings your engine, keep the rest.

It reaches the core over HTTP alone and opens no database. Started by a
server, the engines keep their state in the container's memory. Started by the
desktop's launcher, they keep it in the worker's folder of the data directory.

## How the desktop starts it

On the desktop, the worker's table names the image, and `robinauts desktop`
runs it as a container of oneberth's launcher
([docs/deployment.md](../../docs/deployment.md), "The desktop with Docker"):

```toml
[workers.base]
image = "robinauts-base-worker"
environment = ["GITHUB_TOKEN"]   # optional: the token to clone private repositories with
```

The launcher writes the worker's token on its standard input and closes it,
names the worker listener in `ONEBERTH_WORKER_URL` and the worker in
`ONEBERTH_WORKER_NAME`, waits for the line `ready`, and stops it with SIGTERM.
The container joins the `app` network, which reaches the model providers. It
sees its folder of the data directory, `workers/<name>`, and reads the copy of
the configuration there. It gets the keys of its agents' providers and the
variables `environment` names, and nothing else.

## How the server starts it

The server starts a worker whose `[workers.<name>]` table names a command with
that command, in place of its own worker module, and treats it like every other
worker ([docs/specs/operations.md](../../docs/specs/operations.md), "Workers").
It runs the command with its own environment plus `ROBINAUTS_WORKER_NAME`,
`ROBINAUTS_WORKER_BROKER_URL` and `ROBINAUTS_WORKER_PARENT`; writes the
worker's token on its standard input and closes it; waits for the line
`ready` on its standard output; counts the worker's requests as its signs of
life for `/health`; and sends it SIGTERM at the stop.

```toml
[agents.assistant]
title = "Assistant"
system_prompt = "You are a robinaut."
models = ["gpt"]                # a model of a provider whose key the worker is handed
engine = "pydantic-ai"       # or "langchain": both are in the image
worker = "base"

[workers.base]
command = [
    "docker", "run", "-i", "--rm", "--network", "host",
    "-e", "ROBINAUTS_CONFIG", "-e", "ROBINAUTS_WORKER_NAME", "-e", "ROBINAUTS_WORKER_BROKER_URL",
    "-e", "ROBINAUTS_OPENAI_KEY",
    "--env-file", "/etc/robinauts/github.env",
    "-v", "/etc/robinauts/robinauts.toml:/etc/robinauts/robinauts.toml:ro",
    "robinauts-base-worker",
]
```

What each part of the command is for:

- `-i` connects the container's standard input and output to the server's
  hand-off: the token goes in, `ready` comes out. `--rm` leaves nothing behind
  when the server stops it.
- `--network host` lets the container reach the server's worker listener at the
  loopback address `ROBINAUTS_WORKER_BROKER_URL` names, on Linux. Docker Desktop
  has no host network: set the variable yourself,
  `-e ROBINAUTS_WORKER_BROKER_URL=http://host.docker.internal:8001/robinauts`, and make
  the worker listener listen on an address the containers reach (`--worker-host`).
- `-e ROBINAUTS_CONFIG` with the `-v` mount hands the worker the server's
  configuration file, at the same path: the worker reads the same file the
  server does, and builds the engines of its own agents from it.
- `-e ROBINAUTS_WORKER_NAME -e ROBINAUTS_WORKER_BROKER_URL` pass on the two
  variables the server set: which worker this is, and where the core is.
- `-e ROBINAUTS_OPENAI_KEY` passes on the key of each provider the worker's
  agents use, by the name the provider's `api_key_env` gives: the worker reads
  it from its own environment, as the server would. A `--env-file` does the
  same for several.
- `--env-file /etc/robinauts/github.env` hands the worker `GITHUB_TOKEN`, the
  GitHub token **you** make on GitHub, to clone private repositories with.
  It is optional: leave the flag out to clone public repositories alone. Who
  makes it, what it may reach and how the file is written are in
  [../worker-common/README.md](../worker-common/README.md), "The GitHub token".
- Not passed: `ROBINAUTS_DATABASE_URL`. A worker handed the URL would open
  the engines' tables in it, and those tables belong to the server.
- Not passed either: `ROBINAUTS_WORKER_PARENT`, which a worker on the same
  machine uses to end with a server that was killed. Here the server's SIGTERM
  reaches the container through `docker run`; a server killed outright leaves
  the container for `docker stop`.

## Build

From the repository root, so that the backend is installed into the image from
this checkout:

```bash
docker build -f extra-workers/base-worker/Dockerfile -t robinauts-base-worker .
```

The image carries the same build of `robinauts` as the server it serves: the
two parse the same configuration file and speak a wire with no version
handshake yet. Rebuild it when the server is rebuilt.

## State

On the desktop, the engines' state is in the worker's folder of the data
directory, and outlives the container. Started by a server, it is in the
worker's memory, as it is for every worker that is handed no database: a conversation's checkpoints live as long as the container.
After the container is replaced, the next turn of a conversation that started
before it fails, since the engine no longer has that session; the conversation
itself, every message of it, is the server's and is kept. New conversations
run as before.

## Making your own

1. Copy this folder under `extra-workers/<name>/`.
2. Add a package under `src/`, with the engine, and register it in
   `pyproject.toml` under `[project.entry-points."robinauts.engines"]`: the name
   a configuration uses, pointing at a function `init(settings, storage)` that
   returns an `AgentEngine`. The worker module finds it there.
3. Add what the engine needs to `dependencies`, and to the image. Keep
   `robinauts-worker-common` and the lines that copy it: what more than one
   worker needs goes there, not in a copy
   ([../worker-common/README.md](../worker-common/README.md)).
4. Keep the `CMD`, or put a thin entry of your own in front of it that checks
   what your engine needs in the environment, together with
   `robinauts_worker_common.__main__.problems_of`, and then calls
   `robinauts.cli.worker_process.main()`.
5. Say in the README who makes which credential and where it goes. Robinauts
   makes none.

`scripts/check-extra-workers.sh` runs the folder's lint, tests and image build.

## The test

`tests/test_conversation.py` starts a server with no database, a worker from the
image, and a model server of its own, and holds a two-turn conversation on each
engine. The backend's `tests/api/test_desktop_docker.py` starts the image on the
desktop, as a container of the launcher. It needs Docker on Linux and is skipped elsewhere; it builds the image
unless `ROBINAUTS_BASE_WORKER_IMAGE` names one already built.
