# Orchestration flows

The neorc flows that Robinauts ships, and their handlers. `robinauts start`
runs them on the neorc composed into its backend when `ROBINAUTS_FLOWS_DIR`
names a flows folder
([docs/architecture/orchestration.md](../docs/architecture/orchestration.md)).

- `flows/`: a flows folder: the flow files, uploaded to neorc's manager at every
  start. It has no `flows.toml`, so its handlers are the ones beside it.
- `robinauts_tasks.py`: the handlers. They reach the Robinauts API at
  `ROBINAUTS_API_URL`, `…/robinauts`, with the token of `robinauts/handler` in
  `ROBINAUTS_API_TOKEN`, both of which the server hands to neorc's worker.
  Each request names the handler's run in `X-Robinauts-Run`, from the step's
  `run: neorc.flow_run_id`, and the server acts for the person who started it
  ([docs/architecture/orchestration.md](../docs/architecture/orchestration.md),
  "Whose a run is"). They use the standard library alone.

## Trying it

neorc is a dependency of Robinauts, installed by `uv sync` with the rest. The
agents' configuration (`ROBINAUTS_CONFIG`) stays as it is; name this folder of
flows beside it. neorc keeps its flows and runs where the server keeps its own,
in the process or in the PostgreSQL `ROBINAUTS_DATABASE_URL` names, in the
schema `neorc`:

```bash
ROBINAUTS_FLOWS_DIR=orchestration/flows robinauts start --local
```

Open the printed address, which signs you in, and start `guess_number` from neorc's
application in the interface. Or start one from a shell, with a personal token from the
account page:

```bash
curl -X POST http://127.0.0.1:8000/robinauts/api/flows/guess_number/runs -H 'content-type: application/json' -H "authorization: Bearer $TOKEN" -d '{"inputs": {}}'
```

It plays the guessing game between two conversations, which show up in the
history, and needs a model whose id or title holds `haiku`.

## Code and review

`code_and_review` has an agent do a task on a repository and open a pull
request, and a second conversation review it. Its form offers the repositories
of the flows folder's `flows.toml`, which the shipped folder has none of: use a
folder of your own, with the two files of the flow and the repositories it may
work on.

```bash
mkdir -p ~/robinauts/flows
cp orchestration/flows/code_and_review.* ~/robinauts/flows/
cat > ~/robinauts/flows/flows.toml <<'TOML'
[repositories.robinauts]
url = "https://github.com/<owner>/<name>"
branches = ["main"]
TOML
ROBINAUTS_FLOWS_DIR=~/robinauts/flows robinauts start --local
```

The agent works in a worktree of the repository, which the extra worker makes
for each conversation from those same `[repositories]`, on a branch of the
conversation's own, and pushes it with the worker's GitHub token: an agent of
the Claude Code worker
([extra-workers/claude-code-worker](../extra-workers/claude-code-worker/README.md)),
whose command passes `ROBINAUTS_FLOWS_DIR` on and mounts the folder, and is
handed the token by `--env-file`
([extra-workers/worker-common](../extra-workers/worker-common/README.md),
"Repositories and worktrees"). The token needs, on those repositories alone,
**Contents: Read and write** to push and **Pull requests: Read and write** to
open the pull request.

The handlers send a `code_task/1` task payload beside the prompt, so the
worker starts the worktree from the branch chosen in the form ("A code task"
in that README); that branch must be one of the repository's `branches`.

Then **New run**, **Code and review**: the task in words, the repository and
branch, the agent and the model. The run's output is the review's verdict and
summary, with the pull request's URL and both conversations, which are in the
history.
