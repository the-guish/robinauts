# The base worker

The backend's own worker, `robinauts.cli.worker_process`, in a container and
nothing else: an image the server starts with `docker run -i`, which runs the
turns of the agents on its worker with any engine the backend ships, LangChain,
Pydantic AI or the echo engine. It is the base of every other extra worker: copy
the folder, add the package that brings your engine, keep the rest.

It reaches the core over HTTP alone and opens no database. The engines keep
their state in the container's memory.

## How the server starts it

The server starts a worker whose `[workers.<name>]` table names a command with
that command, in place of its own worker module, and treats it like every other
worker ([docs/specs/operations.md](../../docs/specs/operations.md), "Workers").
It runs the command with its own environment plus `ROBINAUTS_WORKER_NAME`,
`ROBINAUTS_WORKER_BROKER_URL` and `ROBINAUTS_WORKER_PARENT`; writes the
worker's secret on its standard input and closes it; waits for the line
`ready` on its standard output; counts the worker's requests as its signs of
life for `/health`; and sends it SIGTERM at the stop.

```toml
[agents.assistant]
title = "Assistant"
system_prompt = "You are a robinaut."
model = "gpt"                # a model of a provider whose key the worker is handed
engine = "pydantic-ai"       # or "langchain": both are in the image
worker = "base"

[workers.base]
command = [
    "docker", "run", "-i", "--rm", "--network", "host",
    "-e", "ROBINAUTS_CONFIG", "-e", "ROBINAUTS_WORKER_NAME", "-e", "ROBINAUTS_WORKER_BROKER_URL",
    "-e", "ROBINAUTS_OPENAI_KEY",
    "-v", "/etc/robinauts/robinauts.toml:/etc/robinauts/robinauts.toml:ro",
    "robinauts-base-worker",
]
```

What each part of the command is for:

- `-i` connects the container's standard input and output to the server's
  hand-off: the secret goes in, `ready` comes out. `--rm` leaves nothing behind
  when the server stops it.
- `--network host` lets the container reach the server at the loopback address
  `ROBINAUTS_WORKER_BROKER_URL` names, on Linux. Docker Desktop has no host
  network: set the variable yourself, `-e ROBINAUTS_WORKER_BROKER_URL=http://host.docker.internal:8000`,
  and make the server listen on an address the containers reach.
- `-e ROBINAUTS_CONFIG` with the `-v` mount hands the worker the server's
  configuration file, at the same path: the worker reads the same file the
  server does, and builds the engines of its own agents from it.
- `-e ROBINAUTS_WORKER_NAME -e ROBINAUTS_WORKER_BROKER_URL` pass on the two
  variables the server set: which worker this is, and where the core is.
- `-e ROBINAUTS_OPENAI_KEY` passes on the key of each provider the worker's
  agents use, by the name the provider's `api_key_env` gives: the worker reads
  it from its own environment, as the server would. A `--env-file` does the
  same for several.
- Not passed, and not to be: `ROBINAUTS_DATABASE_URL`. The worker has no
  database and needs none; a worker handed the URL would open the engines'
  tables in it, which is the server's ground.
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

The engines' state is in the worker's memory, as it is for every worker that is
handed no database: a conversation's checkpoints live as long as the container.
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
3. Add what the engine needs to `dependencies`, and to the image.
4. Keep the `CMD`, or put a thin entry of your own in front of it that checks
   what your engine needs in the environment and then calls
   `robinauts.cli.worker_process.main()`.
5. Say in the README who makes which credential and where it goes. Robinauts
   makes none.

`scripts/check-extra-workers.sh` runs the folder's lint, tests and image build.

## The test

`tests/test_conversation.py` starts a server with no database, a worker from the
image, and a model server of its own, and holds a two-turn conversation on each
engine. It needs Docker on Linux and is skipped elsewhere; it builds the image
unless `ROBINAUTS_BASE_WORKER_IMAGE` names one already built.
