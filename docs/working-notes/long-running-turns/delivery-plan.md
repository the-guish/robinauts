# Delivery plan: long-running turns, in stages

- Status: plan for a development environment, not a release. Written 2026-10-04,
  companion to [long-running-turns.md](long-running-turns.md) (the study, the
  decisions D1–D7) and the tech designs (the target state).
- Effort is in developer-days, rough, for ordering and not for commitments.

## Scope rules

- **A workable development deployment, not a release.** The schema is still
  edited in place, and a change of schema recreates the database. Migrations wait
  for the first release (D5).
- **Two or more identical replicas from Stage 1.** Kubernetes, the same image in
  every pod, behind a load balancer (D1). Nothing in Stages 1–2 assumes a single
  process.
- **No resume in Stages 1–2.** A turn that crashes, or is caught by a deploy, ends
  as interrupted. Its conversation stays usable, and Retry starts the task over at
  full cost (D7). A tool call in flight is never repeated automatically (D6).
- **No per-user caps and no queue in Stages 1–2.** One fleet-wide number. Past it,
  a new turn is refused with "busy, try again".
- **Expected load:** 2–3 users, at most 30 conversations of any kind.
- **Slow tools (external jobs) and resume** form a track of their own. It is
  planned separately, first in Stage 3.
- **Nothing in Stages 1–2 is throwaway.** Every item is a piece of the target
  state, and Stage 3 adds to it without replacing it.

---

## Stage 1 — what the app is missing today, for both modes

Exit criteria:

- The app runs as **two or more replicas** behind a load balancer.
- Streams, Stop and delete work whichever pod a request lands on.
- A pod crash or a deploy leaves **every conversation usable within about two
  minutes**. The interrupted answer is shown with what it had done, and can be
  retried.

| # | Item | What it involves | Days |
|---|---|---|---|
| 1.1 | Fail fast on configuration | Refuse to start in sign-in mode without `ROBINAUTS_DATABASE_URL`, instead of silently running in memory. An agent unknown to one replica answers 404, not 500 (`StopIteration` in `default_model`) | 0.5 |
| 1.2 | Separate timeouts, and survive vendor hiccups | `timeout_seconds` stays the timeout of each vendor call. A new `max_turn_seconds` sets the turn's deadline (`turns.deadline_at`), apart from the lease. Vendor SDK retries on (about 2, with backoff). Pydantic AI `request_limit` and `tool_error_behavior`, and LangGraph `recursion_limit`, come from configuration | 1–2 |
| 1.3 | Heartbeat lease and fencing | Columns `worker_id`, `attempt`, `heartbeat_at`, `deadline_at`. Each pod renews its turns' leases in one batched `UPDATE` every about 30 s, for a 90 s lease, on a connection of its own. The lease condition on every append and finish also checks `attempt` | 2–3 |
| 1.4 | Decent recovery from a crash | Expired turns end as `interrupted` within about 2 min, ended by the sweep (1.9) or the next reader. The pod that ends one **materialises its partial answer from the turn's events** as a failed answer, so the thread shows what it did. Retry then works as today: it starts over, and the prompt lists the calls the failed attempt made. Fix the frontend's marking of interrupted answers as `failed` without a stored answer behind them | 1–2 |
| 1.5 | Stop and delete from any pod | `cancel_turn` writes `cancel_requested_at` and sends `NOTIFY robinauts_cancel`. The owning pod cancels the task at once, or at its next heartbeat. Delete requests a cancel, waits for the turn to end, then hides and purges. The UI stops showing "the stop did not reach the server" for a 409 | 2–3 |
| 1.6 | Streams that survive a load balancer | `: keep-alive` comments every 15 s. The frontend reconnects with capped backoff while the run lives, has an idle watchdog, retries on `online` and `visibilitychange`, and offers a manual "Reconnect". Text deltas are coalesced over about 100–250 ms, with one `NOTIFY` per batch. `open_session` counts with `max(position)` instead of loading every event | 3–4 |
| 1.7 | Shutdown and health | The drain starts on `SIGTERM`, after a `preStop` `sleep 5`. `/ready` answers 503, the pod takes no new turns, and its streams close early with a reconnect hint. Running turns get a bounded window to finish, then end as interrupted (1.4). The final wait gets a timeout. `/ready` checks the database and the listener. Grace period about 45–60 s | 1–2 |
| 1.8 | Connection budget | Pool size and acquire timeout configurable (today 2–10, fixed, with no timeout). The listener and the heartbeat each keep their own connection. Document `max_connections` against the number of replicas | 0.5 |
| 1.9 | Housekeeping | `sweep()` in every pod on an interval, under `pg_try_advisory_xact_lock`. It deletes expired `turn_events`, `user_sessions` and `api_tokens`, ends turns whose lease expired (1.4), and finishes the purge of hidden sessions whose purge died | 1–2 |
| 1.10 | Run it as N replicas | Logs carry turn, conversation and pod ids. A multi-replica test: two app processes on one database, with a stream on one, Stop through the other, and a kill mid-turn. Deployment notes: `db init` as a Job, `preStop` (`sleep 5`), the grace period, the connection math, `Origin` passed through by the load balancer | 2–3 |
| 1.11 | Context management in both engines | Clear old tool results at about 60% of the window and summarise at about 85%, through LangChain middleware and a Pydantic AI history processor. Amend ADR 0005 on the "never cut the question" invariant. The last step of Stage 1; Stage 2 needs it | 2–4 |

**Stage 1: about 16–26 days (3–5 weeks).**

## Stage 2 — a task that can run for 3 hours, without resume

Exit criteria:

- A person starts a conversation as **live** or as a **background task**.
- A background task's turn runs for up to **3 hours** on any pod. A live session's
  turn is cut off sooner.
- The person sees in the conversation list when a task is running and when it has
  finished.
- The fleet stops taking new turns at one configured number.
- A crash or a deploy costs a rerun, never a stuck conversation.

| # | Item | What it involves | Days |
|---|---|---|---|
| 2.1 | Live and background, per conversation | `sessions.mode` (`live` / `background`), chosen when the conversation starts: an option in the UI, and `mode` on `POST /api/turns`. Each mode has its own `max_turn_seconds` (for example live 20 min, background 3 h) and its own call caps (model calls, tool calls), which serve as the cost guard. The framework's own limit middleware and usage limits are reliable here, since nothing resumes | 2–3 |
| 2.2 | One fleet-wide cap | `start_turn` counts running turns under a transaction advisory lock. At the configured number it refuses with 429 and "busy, try again", which the UI shows. No queue | 1–2 |
| 2.3 | Running and finished badges | `active` and a "finished since you looked" flag on each conversation summary. A badge in the history list, and the tab title while a tab is open | 1–2 |
| 2.4 | Storage at 3-hour scale | Prune a finished turn's intermediate LangGraph checkpoints and writes, keeping the final one. Cap the Retry note to the most recent calls, so a long failed task does not produce a huge prompt | 1–2 |
| 2.5 | A 3-hour soak test | A stand-in provider and slow stand-in tools run a background task for 3 hours across two replicas: keep-alives, reconnects, context management and the deadline. A pod is killed mid-run, and the conversation shows interrupted, with Retry working | 1–2 |

**Stage 2: about 6–11 days (1.5–2 weeks). Stages 1–2: about 22–37 days.**

Known costs, accepted for now:

- A deploy or a crash during a background task means a full rerun. Deploy when no
  background task runs: one query on `turns` tells.
- Retry repeats every tool call of the task. The agent sees what the failed
  attempt did, and may skip what is already done.

---

## Stage 3 — ordered backlog

| Order | Item | Notes | Days |
|---|---|---|---|
| 1 | **Resume — LangGraph** (track: slow tools and resume) | `durability="sync"`, turn-tagged checkpoints, `StepCommitted` from the saver's `aput`, "outcome unknown" for in-flight calls (D6). Release on drain through `RunControl.request_drain()`, so deploys stop costing reruns. `crashes` counted apart from `attempt`, with jitter on re-claims. A "resumed" marker in the transcript | 8–12 |
| 2 | **Slow tools: external jobs** (same track) | `call_me_back` and `job_status` built-ins on an in-process platform MCP server. `tool_jobs` and `job_waits`, a poller in every pod, and a transactional fire. Callback turns carry a delimited platform message. Cancel on soft delete, and audit of the platform's own actions | 10–15 |
| 3 | Resume — Pydantic AI | Per-node snapshots including the pending request, native cancel capture, a per-call ledger, and `supports_resume` in the contract suite | 6–8 |
| 4 | Queue and admission | A `queued` state with a claim loop under one claimer at a time. Live capped at about 10 per user and served first. Background unlimited, with a guaranteed share (D2, D3). Queue-behind for messages sent during a turn (D4). The "…waiting…" screen with its reason. This replaces Stage 2's single cap and 409 | 10–14 |
| 5 | Controller-held budgets | Time, calls and cost, kept across resumes, where middleware counters reset. A wrap-up call without tools, then "Continue" | 2–3 |
| 6 | Visibility | `progress` and a partial-answer snapshot (a reload attaches at the snapshot, not at 0), in-app notifications, `/metrics` on a separate port or behind a token, a `robinauts turns list\|cancel` CLI, and an audit log | 6–10 |
| 7 | Prompt cache TTL per agent | 1 h for long agents. It makes resumes and callbacks cheap | 1 |
| 8 | Load test and admission watermarks | Memory (cgroup) and event-loop lag as claim brakes, sized from measurements | 3–5 |
| 9 | Role split | `robinauts start --role web\|worker\|all`, with queue-depth autoscaling for workers | 2–4 |
| 10 | MCP tasks for external jobs | Once tool servers support SEP-1686 | 3–5 |
| 11 | Suspended-turn option for jobs | For agents that must not end their turn without the results | 5–8 |
| 12 | Admin view of running work | Metadata only, with the roles feature | — |
| Gate | **Migrations** | Before the first release (D5). Numbered expand/contract SQL, under the existing advisory lock | 3–5 |
