# Orchestration flows

The neorc flows that Robinauts ships, and their handlers. `robinauts start
--dev-no-sign-in` runs them on the neorc it embeds when `ROBINAUTS_FLOWS_DIR`
names a flows folder
([docs/architecture/orchestration.md](../docs/architecture/orchestration.md)).

- `flows/`: a flows folder: the flow files, uploaded to neorc's manager at every
  start. It has no `flows.toml`, so its handlers are the ones beside it.
- `robinauts_tasks.py`: the handlers. They reach the Robinauts API at
  `ROBINAUTS_API_URL`, which the server hands to neorc's worker, with no
  credential: the local development mode has none, and every request there is
  the developer's. Given an API token in `ROBINAUTS_API_TOKEN`, as a neorc
  started against a server with sign-in on would be, they send it as a
  bearer. They use the standard library alone.

## Trying it

neorc is a dependency of Robinauts, installed by `uv sync` with the rest. The
agents' configuration (`ROBINAUTS_CONFIG`) stays as it is; name this folder of
flows beside it. neorc keeps its flows and runs where the server keeps its own,
in the process or in the PostgreSQL `ROBINAUTS_DATABASE_URL` names, in the
schema `neorc`:

```bash
ROBINAUTS_FLOWS_DIR=orchestration/flows robinauts start --dev-no-sign-in
```

Use the **Flows** section of the left panel, below the history: **New run**,
then `guess_number`, starts a run from neorc's own form, framed where the chat
would be. Or start one from a shell through the server's port:

```bash
curl -X POST http://127.0.0.1:8000/neorc/flows/guess_number/runs -H 'content-type: application/json' -d '{"inputs": {}}'
```

It plays the guessing game between two conversations, which show up in the
history, and needs a model whose id or title holds `haiku`. The run then
updates live in the frame and in the panel's recent runs.
