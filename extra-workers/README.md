# Extra workers

A worker that runs beside the server rather than inside it: in a container, on
another machine, with an engine the backend does not ship. Each is a folder
here, a project of its own.

What they have in common, and what a new one keeps:

- **It is the backend's worker.** It is composed from the `robinauts` package
  (`compose_worker`, `HttpTurnBroker`), with its engines plugged in. Leases,
  heartbeats, cancels, stalls, failed answers and resumes are the worker's and
  are not written again. The wire it speaks is `web/contract/broker/wire.py`,
  which has no version handshake yet: an extra worker runs the same build of
  `robinauts` as the server it serves.
- **The server knows it as an external worker**
  ([docs/specs/operations.md](../docs/specs/operations.md), "Workers"): a
  `[workers.<name>]` table of kind `external`, with the environment variable
  that holds the secret it presents. The server never starts it, never waits
  for it, and counts it out of `/health`.
- **Nothing in `backend/` names it.** The backend's import-linter cannot see
  this folder, so `scripts/check-extra-workers.sh` checks that rule itself.
- **No credential is Robinauts' to make.** A worker reads the names of
  variables, checks which are set, and hands the engine what the mode calls
  for. It never creates, requests, stores, prints or shares a key or a token;
  the person or the operator does, with the vendor's own tools, and the
  worker's documentation says who makes which credential and where it goes.
- **It is checked like the rest of the repository**: SPDX headers on every
  file, its lock under the licence gate (`scripts/check-licences.sh`), ruff and
  black under the backend's rules, its own tests, and the image built in CI.

The first extra worker, one that runs its turns on Claude Code through the Claude
Agent SDK in a Docker container, follows in a pull request of its own.
