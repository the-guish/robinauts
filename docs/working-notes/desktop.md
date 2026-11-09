# A desktop code editor out of Robinauts + neorc + the Claude Code worker

A design note, written against `main` (6bc13ec), PR 30 (`feature/neorc-embedded`),
`demo/`, `backend/src/robinauts/cli/` and `extra-workers/claude-code-worker/`.

## 1. What the three pieces already give us

| piece | what it is today | what the desktop reuses |
|---|---|---|
| `robinauts start --dev-no-sign-in` | one uvicorn process: API + built UI, one `!local` user, loopback only, `Host`/`Origin` guard, workers in-process without PostgreSQL and as child processes with it | the whole server, unchanged in shape |
| PR 30 (`cli/orchestration.py`) | neorc's manager mounted at `/neorc`, scheduler in-process, one neorc worker as a child process with a scrubbed environment, Flows section in the left panel | orchestration of local workflows across conversations |
| `extra-workers/claude-code-worker` | a `robinauts.cli.worker_process` with the `claude-code` engine plugged in; Docker by default, but documented to run as a plain process (`python -m robinauts_claude_worker`); personal mode on `CLAUDE_CODE_OAUTH_TOKEN` | the code-editing agent |
| `demo/start.sh` | brings PostgreSQL from `pgserver`, writes the TOML from a template, scrubs keys from every child but the server, pid file, opens a browser | the launcher logic, re-done in Python |

The lifecycle machinery is the asset: `Lifecycle` already spawns and stops
processes, hands secrets on stdin, waits for `ready`, watches for death,
and `PR_SET_PDEATHSIG`s children on Linux. The desktop adds one layer on
top of it and changes as little as possible underneath.

## 2. The shape

A new top-level project, `desktop/`, a sibling of `extra-workers/` and `mcp/`,
publishing one package, `robinauts-desktop`:

```
uv tool install "robinauts-desktop[claude]"     # or: uvx robinauts-desktop
robinauts-desktop                                # opens the window
```

Dependencies: `robinauts` (the wheel with the UI in it), `neorc[...]` (through
`robinauts`, once PR 30's lock points at release wheels),
`robinauts-claude-code-worker` as the `[claude]` extra, `platformdirs`,
`keyring`, and a window adapter (section 4). No Node, no Docker, no `sudo`.

Process tree at run time:

```
robinauts-desktop                      the shell: owns the window, the launch secret,
│                                      the data directory and the lifetime of everything below
├── robinauts start --desktop ...      the server: API + UI, controller, neorc manager + scheduler
│   ├── python -m robinauts_claude_worker     always a process of its own (it has a command)
│   │   └── claude                            Claude Code's binary, one per running turn
│   ├── neorc worker start                    the flow handlers (PR 30, unchanged)
│   └── python -m robinauts.cli.worker_process   the built-in engines, only on PostgreSQL storage
└── <window>                           the OS window showing http://127.0.0.1:<port>/ui/
```

Why the server is a child and not a thread of the shell:

- uvicorn and `Server.startup/shutdown` in `cli/main.py` are written for a
  process that owns the main thread and its signals. A GUI toolkit wants the
  same main thread. Two owners of one thread is where desktop apps go wrong.
- A server crash must not take the window down, and a window crash must not
  leave agents running with the keys in memory. Two processes, each watching
  the other, is the only way to promise both.
- The shell then holds no provider key at all (section 5), which is the same
  property `demo/start.sh` works hard for.

Threads inside the shell: the main thread runs the window; one thread reads
the server's stdout for the `ready <url>` line and logs; one watches the
server process. Nothing else. Inside the server nothing changes: one asyncio
loop, engines async, the Claude worker is a subprocess per turn already.

## 3. Storage: the one hard problem

Without `ROBINAUTS_DATABASE_URL` the server runs on `InProcessStore`, which is
dicts: closing the window loses every conversation. An editor cannot do that.

Two routes, and I recommend doing them in order:

**v1, PostgreSQL from `pgserver`**, as the demo does, under the user's data
directory (`~/.local/share/robinauts-desktop/pgdata`), loopback, random
password in a `0600` file, started and stopped by the shell. Zero change to
any adapter, and `demo/pg.py` is most of the code. Costs: 40 MB of binaries,
a second daemon to clean up after a crash, one to two seconds at start, and
a licence-gate problem: `pgserver` states no licence *identifier*, so it
cannot be in a locked dependency set today (`demo/README.md`). The desktop
would need either a signed row in `DEPENDENCIES.md` or to fetch it as a tool
at first run with `uv run --with`, which is acceptable only because
`uv tool install` guarantees `uv` is present.

**v2, the `LOCAL` storage kind on SQLite.** The contract already names it
(`StorageKind.LOCAL`, `options: {"path": ...}`), `rules.md` already allows
`sqlite3` and `aiosqlite` under `controller.adapters` and the two engines,
and `compose()` raises `NotImplementedError` for it today. What it needs:

- a `controller/adapters/local/` store and credentials (model: the Postgres
  ones);
- the LangGraph engine on `langgraph-checkpoint-sqlite` (MIT), the Pydantic
  AI engine's own session tables on `aiosqlite`;
- neorc has `MemoryStore` and PostgreSQL only: either a SQLite store in neorc,
  or accept that flow run history does not survive a restart on the desktop
  for a while (flows are re-uploaded at every start anyway).

With `LOCAL`, the built-in workers run in the server's process like the
in-process case does, and the process tree above loses its last branch. That
is the desktop the architecture was clearly drawn for; v1 is the bridge.

## 4. The window: an OS window, no URL bar

The licence policy decides this more than taste does. `DEPENDENCIES.md`
forbids LGPL in any version, dynamic linking included, transitively. That
rules out, as pip dependencies:

- `pywebview` on Linux (its GTK backend is `PyGObject`, LGPL; its Qt backend
  is PyQt, GPL, or PySide, LGPL);
- PySide6 / Qt WebEngine anywhere;
- Electron and Tauri are not pip-installable at all.

What is clean, and already installed on every developer machine:

**Chromium-family browsers in `--app` mode.** `chrome --app=<url>
--user-data-dir=<ours>` opens a plain OS window: the page's title, the page's
icon, no tabs, no address bar, no bookmarks. Chrome, Chromium, Edge and Brave
all honour it. With a user-data-dir of our own it is a separate browser
process that exits when its window closes, so "window closed" is a process
exit the shell waits on. Nothing is shipped, so nothing goes through the
licence gate; it works on Linux and macOS today and on Windows the day the
lock does.

Put it behind a small port in the shell, `Window.open(url, title) -> process`,
with `chromium-app` as the one adapter in v1, and leave room for two
native ones later: `WKWebView` through `pyobjc` (MIT) on macOS, and
`WebView2` through `pythonnet` (MIT) on Windows. Firefox has no app mode
(`--kiosk` is full-screen only), so a machine with Firefox alone gets the
one honest fallback, a message naming what to install.

What the frontend learns from this: a `desktop: true` flag in
`GET /auth/session`, beside PR 30's `flows_url`, so the shell hides "open in
browser" affordances, and routes native things (choose a folder, reveal a
file) through `/desktop/*` routes on the server rather than through a
JavaScript bridge. One frontend, one wire, in the browser and in the window.

## 5. Secrets and tokens

Today: the configuration names an environment variable, the shell exports
it, and `secret_for` is `environ.get`. There is no shell on a desktop.

**Model provider keys go in the OS keychain**, through `keyring` (MIT):
macOS Keychain, Linux Secret Service, Windows Credential Locker. The change
is one function: `configure()` in `cli/composition.py` returns the
`SecretLookup`; the desktop start returns one that reads the environment
first and the keychain second, under service `robinauts-desktop` and the
variable's *name* as the key. So `api_key_env = "ANTHROPIC_API_KEY"` keeps
meaning what it means, nothing in the file changes, and the key is read by
the **server**, lazily, when an engine asks. The shell never sees it. The
window never sees it: a first-run page posts the key once to
`POST /desktop/secrets` (cookie-gated, Origin-checked, local mode only) and
the server writes it to the keychain and forgets it. Linux without a running
Secret Service is a start-up refusal that says so, not a plaintext fallback.

**The Claude Code credential stays the person's own.** `CREDENTIALS.md` is
already written for exactly this case: one person, their own machine, their
own subscription, `ROBINAUTS_CLAUDE_AUTH=personal`,
`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, run by them. The
desktop never runs `claude setup-token` or `claude /login` for anyone. The
first-run page asks them to paste what `claude setup-token` printed; it goes
to the keychain like a key and into the worker's environment at its start,
as the README's "Without Docker" paragraph already describes. An option
worth a line in `CREDENTIALS.md` later: pointing `CLAUDE_CONFIG_DIR` at the
person's own `~/.claude` would reuse their existing Claude Code login with
no token at all, which the documents quoted there allow for the unmodified
binary; the transcripts would then live beside their own, which the engine's
checkpointing would need to tolerate.

**The children's environments.** `Lifecycle._spawn` passes `**os.environ`
to every worker, which is right for the built-in engines, which need the
keys. PR 30's `worker_environment()` already scrubs the neorc worker. On the
desktop the shell should scrub its *own* environment before it starts the
server, the way `demo/start.sh` unsets the three variables at its first
line: a GUI launched from a desktop session inherits whatever the session
has, and none of it should reach the Claude binary or a flow handler.

**A launch secret for the window.** The local development mode has no
authentication: anything on this machine that can open
`http://127.0.0.1:<port>` can drive an agent that edits code with the
person's key. In a browser the `Host` check stops DNS rebinding; a local
process is not a browser. On the desktop:

1. the shell makes a random nonce and hands it to the server on stdin,
   exactly as `Lifecycle` hands a worker its secret;
2. the server, started with `--desktop`, refuses every request under `/ui/`,
   `/api/`, `/neorc/` and `/auth/` without a session cookie, but one route:
   `GET /auth/desktop?nonce=...`, single use, which mints a session for the
   `!local` identity (the machinery of `Via.COOKIE` and `/auth/session`
   exists) and redirects to `/ui/`;
3. the window's first URL is that route; the cookie lives in the browser
   profile under our data directory and nowhere else.

The port is random too: `--port 0`, and the server prints `ready <url>` on
its stdout the way a worker prints `ready`, which the shell reads. The neorc
worker's handlers reach the API with no credential in PR 30 because the
mode has none; with the gate they need one, and the API-token path is
there: the server mints a per-launch `rbt_` token for the `!local` user and
hands it to the neorc worker in `ROBINAUTS_API_TOKEN`, the variable
`orchestration/robinauts_tasks.py` already reads. Device pairing and API
tokens from the window keep working, since the desktop cookie is a browser
session and counts as `browsing`.

## 6. Docker, gone; processes, kept

Docker exists in the Claude worker for two reasons: isolation of a shared
deployment, and reproducibility of the Claude binary. On a desktop both are
answered differently: the person *wants* the agent in their own repository,
and the Claude Agent SDK wheel bundles the binary. So the desktop installs
`robinauts-claude-code-worker` into the same environment and the worker's
table in the generated configuration is:

```toml
[workers.claude]
command = ["<venv>/bin/python", "-m", "robinauts_claude_worker"]
```

Two things in that package must change for this to be a release and not a
checkout:

- its `pyproject.toml` pins `robinauts` by editable path. On PyPI it pins
  `robinauts == <same version>`, exactly, because the broker wire has no
  version handshake (`extra-workers/README.md` says so);
- `ROBINAUTS_CLAUDE_WORKSPACES` with one directory per conversation is the
  wrong shape for an editor. The agent must work **in the folder the person
  opened**. Smallest change: a `ROBINAUTS_CLAUDE_WORKSPACE_MODE=shared`
  that uses one directory for every conversation of this server, set by the
  shell to the opened folder. Right change: a working directory on the
  conversation, carried in the agent definition the engine receives, so a
  window with two projects open is two conversations in two folders. That
  is a contract change and belongs in `agent-engines.md`.

Shutdown order is the one that exists: window closes, the shell sends the
server SIGTERM, the server stops its workers (15 s, then kill) and neorc,
closes storage, exits; the shell then stops PostgreSQL in v1. The shell
waits `GRACEFUL_SHUTDOWN_SECONDS` plus the worker stop before it kills.
Linux children already die with their parent; macOS does not offer
`PR_SET_PDEATHSIG`, and a leaked server holding a port and keys in memory
on a laptop is worse than on a host, so `worker_process` and the server
get a parent-pid watchdog there (poll `os.getppid()` once a second). A lock
file per data directory makes a second launch focus the first window rather
than start a second server, which `demo/common.sh`'s `server_pid()` logic
already does for a demo.

## 7. The editor itself

Robinauts has no code editor; the frontend has React and nothing else by
policy, and a gzipped bundle gate in `vite.config.ts`. Three steps, each
useful on its own:

1. **Watch the agent edit.** The Claude worker already streams every `Edit`,
   `Write` and `Bash` as tool calls into the transcript. A side-panel tab
   (the side panel exists, ADR 0007) that shows a file tree of the opened
   folder and the diff of each edit as it lands is most of what people want
   from "agent writes code, I watch". Needs `/api/workspace/...` routes,
   scoped to the opened folder, local mode only: list, read, and a
   change feed, for which PR 30's `/api/changes` is the pattern.
2. **Approve before it runs.** `dontAsk` was chosen because nobody could
   answer from the chat. On the desktop somebody can. The SDK's
   `can_use_tool` callback becomes a new turn event, "a tool asks", that the
   UI answers through a route; the engine contract and `wire.md` gain the
   pair. This is the planned "tools that suspend a run" item in `core.md`,
   and the desktop is its first real customer.
3. **Edit.** CodeMirror 6 (MIT) rather than Monaco: it needs no web workers
   and no `blob:` URLs, so it fits `default-src 'none'`, and it is a tenth
   of the size. It is still the first JavaScript dependency the frontend
   takes beyond React, so it goes through `bundled-packages.txt`, the
   licence gate and the size gate like anything else. Writes go through the
   same workspace routes.

An alternative worth recording and rejecting for v1: an editor as an MCP
App view in the side panel. It fits ADR 0007 beautifully, but the view's
`connect-src 'none'` means every file read and write is a JSON-RPC tool call
relayed by the backend, which is the wrong grain for an editor.

## 8. What changes where

| where | change |
|---|---|
| `backend/cli/main.py` | `--desktop`: implies `--dev-no-sign-in` and `--host 127.0.0.1`; `--port 0`; `ready <url>` on stdout; nonce on stdin; keychain-backed `SecretLookup` |
| `backend/web/app.py` | the cookie gate for `--desktop`; `GET /auth/desktop`; `desktop` in the session; `/desktop/secrets`; `/api/workspace/*` |
| `backend/cli/orchestration.py` (PR 30) | hand the neorc worker a per-launch API token when the gate is on |
| `backend/controller/adapters/local/` (v2) | the `LOCAL` store on SQLite; `compose()` stops raising for it |
| `agent_engines/contract` | a working directory in the agent definition; the "tool asks" event and its answer |
| `extra-workers/claude-code-worker` | PyPI release with an exact `robinauts` pin; shared or per-conversation workspace; `can_use_tool` wired to the new event |
| `frontend/` | `desktop` flag; workspace tab in the side panel; approval prompt; later CodeMirror |
| `desktop/` (new) | the shell: data dirs, keychain, config template (a Python `config.py` like the demo's), `Window` port with the `chromium-app` adapter, PostgreSQL in v1, lock, lifecycle, lock file, launcher files (`.desktop`, `.app`) |
| `docs/specs/sign-in.md`, `operations.md` | name a third mode, **desktop**: the local development mode plus a launch cookie, keychain secrets and a random port; still loopback, still one user, still not a deployment |

## 9. Open questions

- **Which first, pgserver or SQLite?** pgserver ships in weeks; SQLite is
  the right end state and touches neorc too.
- **Linux without a Chromium-family browser.** A WebKitGTK adapter without
  `PyGObject` is possible through `ctypes` against the system library, which
  the lock never sees, but it is a few hundred lines of C-shaped Python to
  maintain. Not for v1.
- **The policy on a desktop mode with no sign-in.** The spec says the local
  mode "is not a way to deploy", and a desktop app is not a deployment, but
  it is a product, so the spec should say which guarantees the desktop mode
  gives: one person, one machine, loopback, a launch cookie, secrets in the
  keychain, nothing phoning home. That last one holds: the only outbound
  traffic stays the model provider and the configured tool servers.
- **Reusing the person's own Claude Code login** instead of a pasted token:
  cleaner for them, needs a line in `CREDENTIALS.md` and a transcripts
  location the engine agrees with.
- **Windows.** Still blocked on the lock (`pywin32`'s LGPL piece). The window
  adapter and keychain backend are both ready for it when that moves.
