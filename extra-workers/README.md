# Extra workers

A worker that runs beside the server rather than inside it: in a container, on
another machine, with an engine the backend does not ship. Each is a folder
here, a project of its own.

What they have in common, and what a new one keeps:

- **It is the backend's worker, started like every other.** Its
  `[workers.<name>]` table names the command that starts it, and that is all
  that sets it apart ([docs/specs/operations.md](../docs/specs/operations.md),
  "Workers"). The server runs the command in place of its own worker module,
  with the same environment plus `ROBINAUTS_WORKER_NAME`,
  `ROBINAUTS_WORKER_BROKER_URL` and `ROBINAUTS_WORKER_PARENT`. The broker URL is
  the server's worker listener (`--worker-host`, `--worker-port`). The server
  writes the worker's token on its standard input and closes it: a oneberth
  service token of `robinauts/worker`, bound to the worker's name, that ends
  when the server stops. The server waits for the line `ready` on the worker's
  standard output, sends it SIGTERM at the stop, and reads its requests as its signs of life for `/health`. So an extra worker is a command,
  `docker run -i` of its image, that carries those streams, that signal and the
  variables its `-e` flags name, and a mount of the configuration file. The
  database's URL is not passed on: the worker reaches the core over HTTP alone.
- **On the desktop, oneberth's launcher starts it.** Its table names its
  `image`, and `robinauts desktop` describes it to the launcher as a container
  ([docs/deployment.md](../docs/deployment.md), "The desktop with Docker"). The
  launcher writes the token on its standard input, names the worker listener in
  `ONEBERTH_WORKER_URL` and the worker in `ONEBERTH_WORKER_NAME`, waits for
  `ready`, and stops it with SIGTERM. The worker module reads those variables
  before a server's. The container sees its own folder of the data directory,
  which holds a copy of the configuration and the engines' state. It gets the
  keys of its agents and the variables its table's `environment` names.
- **Every extra worker of this version is a trusted worker.** On the desktop
  its container joins the launcher's `app` network, which reaches the model
  providers and the tool servers, and a service on the host as
  `host.docker.internal`. The launcher's `sandbox` network, for untrusted
  workers behind an egress proxy, holds no worker yet.
- **What more than one worker needs is in [worker-common/](worker-common/README.md)**,
  a library each worker lists as a dependency and its image copies, never in
  a copy per worker: the shared start, the GitHub token, cloning a repository,
  and the repositories of the flows' settings, one clone each and a worktree
  of each per session.
- **It runs `robinauts.cli.worker_process`**, the backend's own worker module,
  with any engine of its own installed beside the `robinauts` package and
  registered under the `robinauts.engines` entry-point group. Leases,
  heartbeats, cancels, stalls, failed answers and resumes are the worker's and
  are not written again. The wire it speaks is `web/contract/broker/wire.py`, which has no
  version handshake yet: an extra worker runs the same build of `robinauts` as
  the server it serves.
- **Nothing in `backend/` names it.** The backend's import-linter cannot see
  this folder, so `scripts/check-extra-workers.sh` checks that rule itself.
- **No credential is Robinauts' to make.** A worker reads the names of
  variables, checks which are set, and hands the engine what the mode calls
  for. It never creates, requests, stores, prints or shares a key or a token;
  the person or the operator does, with the vendor's own tools, and the
  worker's documentation says who makes which credential and where it goes.
  A GitHub token, the one credential every worker may be handed, comes from
  a file the command names with `--env-file`, and is handed to git as a
  header scoped to the GitHub server ([worker-common/](worker-common/README.md)).
- **It is checked like the rest of the repository**: SPDX headers on every
  file, its lock under the licence gate (`scripts/check-licences.sh`), ruff and
  black under the backend's rules, its own tests, and the image built in CI.

The workers:

| folder | what it runs | state |
| --- | --- | --- |
| [base-worker/](base-worker/README.md) | the backend's worker module, through the shared start, and git: any engine the backend ships, for the agents of its worker | in memory, as long as the container; on the desktop, in its folder |
| [claude-code-worker/](claude-code-worker/README.md) | the same, with the engine `claude-code` plugged in: each turn a query of the Claude Agent SDK, which drives the Claude Code binary it bundles, in a directory of the conversation's own | Claude Code's transcripts and the conversations' workspaces, in two volumes; on the desktop, in its folder |

Beside them, [worker-common/](worker-common/README.md) is not a worker but the
library they share, with no image of its own; the checks run over it as over
the workers.

`base-worker/` is what a new extra worker starts from: copy it, add the package
that brings the engine, keep the rest. `claude-code-worker/` is one made that
way; its [CREDENTIALS.md](claude-code-worker/CREDENTIALS.md) is the rule above
on credentials, spelled out for Claude Code's two.
