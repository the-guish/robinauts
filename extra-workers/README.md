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
  `ROBINAUTS_WORKER_BROKER_URL` and `ROBINAUTS_WORKER_PARENT`; writes the
  worker's secret on its standard input and closes it; waits for the line
  `ready` on its standard output; sends it SIGTERM at the stop; and reads its
  requests as its signs of life for `/health`. So an extra worker is a command,
  `docker run -i` of its image, that carries those streams, that signal and the
  variables its `-e` flags name, and a mount of the configuration file. The
  database's URL is not passed on: the worker reaches the core over HTTP alone.
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
| [base-worker/](base-worker/README.md) | the backend's worker module, through the shared start, and git: any engine the backend ships, for the agents of its worker | in memory, as long as the container |
| [claude-code-worker/](claude-code-worker/README.md) | the same, with the engine `claude-code` plugged in: each turn a query of the Claude Agent SDK, which drives the Claude Code binary it bundles, in a directory of the conversation's own | Claude Code's transcripts and the conversations' workspaces, in two volumes |

Beside them, [worker-common/](worker-common/README.md) is not a worker but the
library they share, with no image of its own; the checks run over it as over
the workers.

`base-worker/` is what a new extra worker starts from: copy it, add the package
that brings the engine, keep the rest. `claude-code-worker/` is one made that
way; its [CREDENTIALS.md](claude-code-worker/CREDENTIALS.md) is the rule above
on credentials, spelled out for Claude Code's two.
