# Orchestration flows

The neorc flows that Robinauts ships, and their handlers. `robinauts start
--dev-no-sign-in` runs them on the neorc it starts beside itself when the
configuration has an `[orchestration]` table
([docs/architecture/orchestration.md](../docs/architecture/orchestration.md)).

- `flows/`: the flow files, uploaded to neorc's manager at every start.
- `robinauts_tasks.py`: the handlers. They reach the Robinauts API at
  `ROBINAUTS_API_URL`, which the server hands to neorc's worker, with no
  credential: the local development mode has none, and every request there is
  the developer's. Given an API token in `ROBINAUTS_API_TOKEN`, as a neorc
  started against a server with sign-in on would be, they send it as a
  bearer. They use the standard library alone.

## Trying it

neorc is not a dependency of Robinauts: get it on its own, with a PostgreSQL
for its manager.

```bash
git clone https://github.com/open-shipyard/neorc ../neorc
```

```bash
(cd ../neorc && uv sync --all-packages)
```

Then add to the configuration, beside the model tables:

```toml
[orchestration]
command = ["uv", "run", "--project", "../neorc", "neorc"]
ui = false
```

Start the server with neorc's database:

```bash
NEORC_DATABASE_URL=postgresql://localhost/neorc robinauts start --dev-no-sign-in
```

Then start a run, which opens a conversation in the UI:

```bash
curl -X POST http://127.0.0.1:8420/flows/guess_number/runs -H 'content-type: application/json' -d '{"inputs": {}}'
```
