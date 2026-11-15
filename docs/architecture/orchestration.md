# Orchestration

One of Robinauts foundationals premises is to be well architected to support multiple deployment modes with the same code base, and to be modular enough to be partially adopted as component on other systems. It aims to be deployable as a multi-user platform for a company, but also be fully reused as a desktop application.

[Neorc](https://github.com/open-shipyard/neorc) aims to be lightweight orchestration layer with minimal dependencies that also can be deployed to cluster or be embedded in smaller applications.


They integrate together to server multiple scenarios.

On dev mode or desktop application, Robinauts starts a local instance of Neorc that users can take for orchestrating their own local workflows.


On a cluster Neorc would be started on its own and connected to a running instance of Robinauts, creating sessions with a system user provided by Robinauts.


## neorc composed into the backend

Robinauts' backend is a oneberth composition of two components: Robinauts' own and neorc's
manager (oneberth's `docs/specs/neorc-in-robinauts.md`, "Composed";
[orchestrator/adapters/neorc/composed.py](../../backend/src/robinauts/orchestrator/adapters/neorc/composed.py)):

1. neorc's **manager** is the component named `neorc`, from neorc's `new_manager_webapp`,
   served under `/neorc/`. oneberth checks every request to it for its credential and its
   neorc permission, as it checks Robinauts' routes. neorc's worker routes are on the worker
   listener.
2. its **storage** follows this server's: in this process without a database; the file
   `neorc.db` in `ROBINAUTS_DATA_DIR`, with its notifications in memory; on the PostgreSQL of
   `ROBINAUTS_DATABASE_URL`, in a schema of its own, `neorc`, made at the start if it is not
   there. Robinauts' component opens it before neorc's component starts, and closes it after.
3. its **scheduler** runs in this process, a part of neorc's component.

A flows folder adds the rest (below, [Two configurations](#two-configurations-agents-and-flows)).
Once the backend serves:

4. the `default` queue is declared as a worker queue on the manager, and the flows in the flows
   folder are **uploaded**, with a service token of `neorc/ci`, through the backend's own
   front listener in this process;
5. one **worker**, on the `default` queue, with the handlers in `code_location`, runs in a
   process of its own: `robinauts neorc-worker`, which becomes neorc's own command against the
   worker listener once it holds its tokens.

Robinauts' component does 4 and 5 in its serving, oneberth's moment between the listeners and
`ready` ([web.md](web.md), "The start and the stop"). The server says `ready` once these have
started. On the desktop, the backend does 4 alone, and oneberth's launcher starts the worker
once the backend is ready. If the flows are refused or the worker cannot be started, the server stops and exits 1. At a stop, the worker stops first, with SIGTERM and
killed after fifteen seconds, while the listeners still serve; then the scheduler, then the
storage.

### Two configurations: agents and flows

The agents and the flows are configured apart, each in a place of its own:

| | named by | holds |
|---|---|---|
| **agents** | `ROBINAUTS_CONFIG`, a TOML file | model providers, models, tool servers, agents, workers: everything Robinauts has without neorc |
| **flows** | `ROBINAUTS_FLOWS_DIR`, a folder | the flow files (`*.yaml`, `*.yml`), uploaded to neorc as they are; their forms, `<flow>.form.toml`; and `flows.toml`, their settings and repositories |

Without `ROBINAUTS_FLOWS_DIR`, neorc's manager serves no flow of Robinauts', and no worker of
neorc's runs. The agents' file knows nothing of the flows: an `[orchestration]` table in it, where the flows'
settings were before they had a folder, is refused with where they went.

`flows.toml` is optional; these are its keys and their defaults (and its
`[repositories]`, under [Flows as forms](#flows-as-forms)):

```toml
code_location = "handlers"   # the handlers; default: this checkout's orchestration/
```

A relative `code_location` starts at the flows folder. A folder of a person's
own flows, anywhere, may call the handlers Robinauts ships, which are the
default:

```bash
ROBINAUTS_FLOWS_DIR=~/robinauts/flows robinauts start --local
```

### Flows as forms

The server lists every flow of the flows folder with its inputs, and the values each may
take, and starts a run once the inputs fit
([orchestrator/application/flows.py](../../backend/src/robinauts/orchestrator/application/flows.py)).
The inputs go to neorc as they came: neorc knows nothing of the forms, and its
flow files are plain neorc flows.

| route | answer |
|---|---|
| `GET /robinauts/api/flows` | every flow, with its inputs: a label, the type the flow declares, and the values a list of values offers |
| `POST /robinauts/api/flows/<name>/runs` | `{"inputs": {...}}`; 201 with the run's id, 404 for a flow the folder does not hold, 422 for inputs that do not fit |

The start is sent to neorc's API with the person's own credential, so the run is theirs at
neorc too (oneberth's `docs/specs/neorc-in-robinauts.md`, "Requests of the backend's own").

A flow's form is `<flow name>.form.toml`, beside it in the flows folder. It is
optional: without one, every input is a plain field.

```toml
title = "Code and review"

[inputs.task]
label = "Task"

[inputs.repo_branch]
label = "Repository"
lov = "repo_branch"

[inputs.model]
lov = "model"

[inputs.agent]
lov = "agent"
```

The form's inputs come first, in its order, then those it leaves out. An input
it names must be one its flow declares, and an input with a list of values
must be a `string`: a form that does not fit its flow is refused at the start.
There are three lists of values:

| `lov` | the values | the value sent |
|---|---|---|
| `repo_branch` | each branch of each repository of `flows.toml` | `<id>@<branch>`, the repository by its id there |
| `model` | the models of the agents' configuration | the model's id |
| `agent` | its agents, each labelled with the worker it runs on | the agent's id |

The repositories are the flows' configuration, in `flows.toml`:

```toml
[repositories.robinauts]
url = "https://github.com/the-guish/robinauts"
branches = ["main"]
```

A model the chosen agent's engine cannot reach is not refused yet: that needs
the engines' provider kinds, which the orchestrator does not see.

### Why the worker is a process of its own

The manager and the scheduler are neorc's own code deciding about flows; the
worker runs the handlers, which are whatever `code_location` holds. Three
things put it outside the server's process:

- neorc puts the code location first on `sys.path`, so a handler module could
  shadow a package the server imports later;
- the handlers read `ROBINAUTS_API_URL` from their environment, and the
  server's holds the model providers' keys and the database's URL, which a
  process of its own is not handed (below);
- a handler that blocks, or brings its interpreter down, takes a worker with
  it, not the server.

It costs one process and a long-poll over the loopback interface.

### Whose a run is

A run is the person's who started it, and so are the conversations its handlers open for it.
That holds in both modes, for a run started on neorc's form and for one started through
`POST /robinauts/api/flows/<name>/runs`.

- neorc records the starter on the run, as `started_by`, from the credential that started it.
  Robinauts starts a run with the person's own credential. A sub-flow's run has its tree's
  starter.
- The handlers hold a service token of `robinauts/handler`, and each request names its run in
  `X-Robinauts-Run`, from the step's `run: neorc.flow_run_id`. The server reads the run's
  starter from neorc's manager, and acts for that person. The record is neorc's, so it holds
  after a restart.
- A run neorc does not hold, one a service started, or a request without a run is refused
  with 403.

### The interface

neorc's interface is composed into Robinauts' at build time, from neorc's node package, and
reaches neorc's API at `/neorc/` (oneberth's `docs/specs/shell.md`). neorc is the second
application of oneberth's shell, beside Robinauts. The backend serves no frame of neorc's:
neorc's own built interface is not served, and the session has no `flows_url`.

A run is started from neorc's form, at `/neorc/`. For a flow with a form file the form is
Robinauts': the interface turns the flow's form, as `GET /robinauts/api/flows` gives it with
its lists filled, into A2UI v0.9 in neorc's subset (`frontend/src/flows/forms.ts`). Each list
of values is a choice of its values, and each other input a field of its type. neorc draws its
own form for a flow with no form file, and for one with an empty list or a datetime input. A
form sends the flow's inputs to neorc, which checks their types. Robinauts' own checks of a
form, such as the agent and model pair, run only on `POST /robinauts/api/flows/<name>/runs`.

### Where neorc may be imported

In `orchestrator/adapters/neorc/` alone (an import-linter contract in
`backend/pyproject.toml`, and [rules.md](rules.md)). The orchestrator is a
sibling of the controller: it may see the controller's contract, and the
controller sees nothing of it. `cli/orchestration.py` builds it, and
`cli/main.py` alone imports that module; `cli/main.py` otherwise sees the
orchestrator's contract alone, an `Orchestrator` whose component it composes, which it opens,
starts and stops, and the `Flows` that `web/` serves. `web/` sees that contract and nothing else.
Taking neorc out is taking out that package and `cli/orchestration.py`.

`cli/orchestration.py` is apart from `cli/composition.py` because a worker in
a process of its own imports the composition, and the extra workers' images
leave neorc out. The extra workers also read `[repositories]` of `flows.toml`
with the orchestrator's own parser, `orchestrator/core/config.py`. A contract
keeps all of it so: nothing `cli/worker_process.py`, `cli/composition.py`,
`orchestrator/contract/` or `orchestrator/core/config.py` imports may reach
neorc, even through another module.

### Installing neorc

Robinauts depends on `neorc[http,postgres]`, `neorc-core` and `oneberth`. Neither
publishes wheels yet. neorc's `manager` extra is left out: it brings neorc's built interface,
which nothing serves here. Robinauts builds them from commits of the-guish/neorc and
the-guish/oneberth, named in `[tool.uv.sources]` in `backend/pyproject.toml`. The oneberth
commit is the one the neorc commit pins, so the lock holds one oneberth.

uv builds these packages from those commits. Every other package stays under `--no-build`.
`scripts/neorc-from-source.sh` builds the wheels of a release, and refuses a commit outside
neorc's `main`. The extra workers' images leave neorc out. `scripts/ui-packages-from-source.sh`
packs `@neorc/ui` and `@oneberth/ui` for the interface from the same neorc commit.

When neorc and oneberth publish wheels, take this step out:

1. `git grep neorc-from-source` lists every part of it. Remove each one.
2. Point the dependencies at the published wheels.
3. Run `uv lock` in `backend/` and in each extra worker.

neorc's PostgreSQL adapter is on asyncpg: psycopg is LGPL-3.0, which
[DEPENDENCIES.md](../../DEPENDENCIES.md) forbids.

### What the worker is handed

neorc's worker is started by its supervisor: the server, or the launcher on the desktop. The
supervisor writes a token of `neorc/worker`, bound to the queue `default`, on its standard
input. It names the worker listener in `ONEBERTH_WORKER_URL` and the front listener in
`ONEBERTH_FRONT_URL`. The worker then asks Robinauts for the handlers' token with its own, at
`POST /robinauts/internal/queues/default/handler-token`. Robinauts gives that token to a holder
of `neorc/tasks:claim` on the queue `default`, the handlers' queue, and to nobody else. Both
tokens live in the backend's memory alone, and end with it.

neorc's worker is a process of the host in both shapes of the desktop. In the Docker shape the
backend runs in a container, and the worker reaches both listeners where they are published on
the loopback address. It runs the handlers from the host, and the backend's container sees the
flows folder and the handlers' code at the same paths, read-only.

| variable | holds |
|---|---|
| `NEORC_API_TOKEN` | the token of `neorc/worker`: what the worker sends to the manager |
| `ROBINAUTS_API_URL` | Robinauts' API on the front listener: `http://<host>:<port>/robinauts` |
| `ROBINAUTS_API_TOKEN` | a token of `robinauts/handler`: what the handlers send to Robinauts ([Whose a run is](#whose-a-run-is)) |

neorc's worker is not handed what the agents' configuration keeps from
everything but the server: the model providers' keys, the tool servers'
secrets, `ROBINAUTS_DATABASE_URL`, `ROBINAUTS_CONFIG` and `ONEBERTH_CONFIG`.
It ends with its supervisor on Linux, killed or not.

### The flows Robinauts ships

[orchestration/](../../orchestration/README.md) holds them, with their
handlers: plain functions that use the standard library alone and import
neither neorc nor Robinauts.

- `guess_number`: a guessing game between two conversations with the first
  agent the server offers.
- `code_and_review`: a task done on a repository and reviewed, from a form
  (`code_and_review.form.toml`): the chosen agent, on the chosen model, does
  the task in its session's worktree of the chosen repository, from the chosen
  branch, pushes the session's branch and opens a pull request against the
  chosen branch; a second conversation reads the pull request in its own
  worktree and reviews it against the task. Each answer ends with a `RESULT`
  line of JSON the handler reads: the pull request's URL and branch, then the
  verdict, `approve` or `request_changes`, which is the run's output. The
  worktrees, their branches and the push are the extra worker's, from the same
  `[repositories]` the form offers
  ([extra-workers/worker-common](../../extra-workers/worker-common/README.md),
  "Repositories and worktrees"); the handlers hold no credential.
