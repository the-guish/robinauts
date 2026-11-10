# What the extra workers share

A library, not a worker: the code more than one worker under `extra-workers/`
needs. It has no image of its own. A worker lists it as a dependency and its
image copies this folder; `scripts/check-extra-workers.sh` lints and tests it
like the workers, and the licence gate reads it, in each worker's lock, as this
repository's own.

| module | what it is |
| --- | --- |
| `robinauts_worker_common.__main__` | the shared start, `python -m robinauts_worker_common`: checks what every extra worker checks, then runs `robinauts.cli.worker_process`. The base worker's command. A worker with a start of its own calls `problems_of` beside its own checks, as the Claude Code worker does |
| `robinauts_worker_common.github` | the GitHub token a worker may be handed: the variable, its checks, the header git sends it in |
| `robinauts_worker_common.clone` | cloning a GitHub repository inside the container, with that token; `run_git`, every other git command the same way |
| `robinauts_worker_common.worktrees` | the repositories of the flows' settings: one shared clone of each, and a worktree of each per session, on `rb/<session id>` |

## Using it from a worker

In the worker's `pyproject.toml`:

```toml
dependencies = ["robinauts", "robinauts-worker-common"]

[tool.uv.sources]
robinauts = { path = "../../backend", editable = true }
robinauts-worker-common = { path = "../worker-common", editable = true }
```

and in its `Dockerfile`, before `uv sync`:

```dockerfile
COPY extra-workers/worker-common/pyproject.toml extra-workers/worker-common/
COPY extra-workers/worker-common/src extra-workers/worker-common/src
```

It may import from `robinauts` what an extra worker may: the engine contract,
and `robinauts.cli.worker_process` to hand over to. Nothing in `backend/` names
it.

## The GitHub token

**Robinauts makes no GitHub credential.** You make one on GitHub and put it
where the worker's command reads it from. The worker reads the variable's
name, checks what it holds, and hands it to git, and to nothing else.

| | |
| --- | --- |
| Variable | `GITHUB_TOKEN` |
| What it is | A [fine-grained personal access token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token), or a GitHub App's installation token |
| Who makes it | The operator, or the person running their own instance, on GitHub: Settings, Developer settings, Personal access tokens, Fine-grained tokens |
| What it needs | The repositories the worker clones, alone, with **Contents: Read and write**, since agents push their branches; add **Pull requests: Read and write** if they open pull requests. To clone alone, **Contents: Read-only** is enough |
| Where it goes | A file of the worker's own, `github.env`, named by `--env-file` in the worker's command |
| Optional | Without it, public repositories alone are cloned |

The file, on the machine the server runs on, readable by the server's user
alone (`chmod 600`):

```sh
GITHUB_TOKEN=github_pat_...
```

`docker run --env-file` reads `NAME=value` lines as they are: **no quotes**
around the value, or they become part of it. The worker refuses to start with
a token that holds a quote, a space or a line break, or with the variable set
and empty, and says so on the server's standard error. It never prints the
value.

In the worker's `[workers.<name>]` command, beside the other flags:

```toml
"--env-file", "/etc/robinauts/github.env",
```

The `.gitignore` and the images' `.dockerignore` keep `extra-workers/**/*.env`
out of commits and out of images. A token kept elsewhere stays out of both too:
it is the command that reads it, at the start.

Where the token goes, and where it does not:

- **To git, as one header, for the GitHub server alone.** It reaches git as
  configuration in git's environment, scoped to `https://github.com/`. It is
  never on a command line, where any process could read it, and never in the
  clone's `.git/config`, where it would outlive the clone.
- **To the agents that run commands, by inheritance.** A subprocess that runs
  an agent's own commands inherits the worker's environment, `GITHUB_TOKEN`
  included: Claude Code's `Bash` and `git` can use it. A clone's
  configuration holds a credential helper that reads the variable when the
  agent's git asks, so the agent pushes with it; the configuration holds the
  variable's name, never its value. That is allowed; see "Enhancements".
- **Not to logs.** A failed clone says what git said, with the token taken out.

When it expires or is revoked, the clone fails with GitHub's refusal. Make a
new one on GitHub and replace the value in the file. The next start of the
worker reads it.

## Cloning a repository

From an engine or a tool in the worker:

```python
from robinauts_worker_common.clone import clone

path = await clone("owner/name", workspace / "name", branch="main", depth=1)
```

From a shell in the container, to try a token:

```bash
docker run --rm --env-file /etc/robinauts/github.env robinauts-base-worker \
    python -m robinauts_worker_common.clone owner/name /tmp/name --depth 1
```

- The repository is `owner/name`, or its https URL on the GitHub server. Any
  other host, scheme or a URL with credentials in it is refused before git
  runs. A GitHub Enterprise Server is named with `server=` (`--server`), as
  `https://<host>`, and the token is then sent to it alone.
- The destination is a new directory, or an empty one.
- git runs with an environment of this library's own making: the path, the
  home directory, the proxy and certificate variables, and nothing else of the
  worker's. It reads no system or global configuration, asks no one for a
  password (a private repository without a token fails at once), and speaks
  https alone, submodules included.
- A clone stops after `timeout` seconds (600). A cancelled clone stops git and
  the cancellation goes on.
- `GitHubError` is a refusal before git ran, and `CloneError` a clone that
  failed. Neither ever holds the token.

## Repositories and worktrees

Agents that run at once each work in a copy of their own of every repository,
so that none sees another's changes. The repositories are the one list every
worker reads, `[repositories]` of the flows' settings: `flows.toml` in the
folder `ROBINAUTS_FLOWS_DIR` names, the file the server's flows read too
([docs/architecture/orchestration.md](../../docs/architecture/orchestration.md)).

```toml
[repositories.robinauts]
url = "https://github.com/the-guish/robinauts"
branches = ["main"]
```

The worker's command passes the variable on and mounts the folder, read-only,
at the same path:

```toml
"-e", "ROBINAUTS_FLOWS_DIR",
"-v", "/etc/robinauts/flows:/etc/robinauts/flows:ro",
```

Without the variable, or without a `flows.toml`, there are no repositories,
and a session's directory starts empty. A repository that is not on the GitHub
server, or whose id cannot name a folder, stops the worker's start, with every
other problem of the shared start.

On disk, with the Claude Code worker's volume:

```
/workspace/
    repos/
        <repository id>/            one clone of each, on its default branch
    <session id>/                   a session's directory, the agent's working directory
        <repository id>/            a worktree of that clone, on the branch rb/<session id>
```

- **A clone** is made the first time a session needs it, and fetched before
  each new worktree of it. It is the remote's default branch, `origin/HEAD`,
  whatever it is called. Nothing works in it: it holds the worktrees' history.
- **A session's worktrees** are made before its first turn starts the agent,
  one per repository, from the default branch as just fetched, on a branch of
  the session's own, `rb/<session id>`: git checks a branch out in one
  worktree at most. They stay between the session's turns, as the agent left
  them. One lost is made again on its branch, its commits kept.
- **The agent pushes** its branch with a plain `git push`, which creates
  `rb/<session id>` on GitHub (`push.autoSetupRemote`), with the token in its
  environment, through the clone's credential helper. Its commits are by
  `Robinauts <robinauts@localhost>`, unless the worker's environment names
  someone with git's own `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
  `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL`, in the same env file as the
  token.
- **A deleted session** takes its worktrees and its branch with it, unpushed
  work included. What it pushed stays on GitHub.
- **At once**: a lock per repository keeps one fetch or worktree change on a
  clone at a time; sessions on different repositories go on together. How many
  turns run at once is the worker's limit, `[work] max_running_tasks_per_worker`
  in the agents' configuration: a worker claims a turn only when it runs fewer
  than that, so a turn it holds always has its slot before the agent starts.

```python
from robinauts_worker_common.worktrees import Repositories

repositories = Repositories.from_environ(workspaces / "repos")   # None without any
worktrees = await repositories.add_worktrees(session_id, workspaces / session_id)
...
await repositories.remove_worktrees(session_id, workspaces / session_id)
```

`WorktreeError`, a `GitError` like `CloneError`, is a clone, a fetch or a
worktree that failed; it never holds the token.

## Enhancements

- **Clone ahead of the first turn.** A repository is cloned when a session
  first needs it, so the first turn after a start waits for the clone, under
  its deadline. The worker could warm the clones up in the background once it
  has said `ready`; not before, since the server waits a minute for it.
- **Keep the token from the agents.** Today an agent whose commands run in a
  subprocess that inherits the worker's environment, such as Claude Code's
  `Bash`, can read `GITHUB_TOKEN`, and so can use it with `git` and `gh`. That
  is accepted for now. The risk is that the model steers the agent, and a
  prompt in a repository, an issue or a web page could get it to send the
  token elsewhere. The hardening: the engine lays `GITHUB_TOKEN=""` over the
  environment it hands that subprocess (for the Claude Code worker,
  `ClaudeAgentOptions.env`, built in `credentials.subprocess_environment`), so
  that the worker's own clones keep the token and the agent does not. If the
  agent must push or open pull requests, a second, narrower token for it alone
  (one repository, the permissions it needs, a short expiry) keeps the two
  apart.

## The tests

`tests/test_github.py` checks the pure parts: the token, the server, the
repository names, git's command line and environment. `tests/test_clone.py`
and `tests/test_worktrees.py` run the real git against a local stand-in for
GitHub, `tests/github_stand_in.py`: git's own `http-backend` behind HTTPS, with
a certificate made for the test, which asks for the token when it is told to
and takes pushes. They clone, fetch, make worktrees for sessions at once, push
an agent's branch with the token from its environment, and remove a session's
worktrees. They are skipped where git or openssl is missing.
