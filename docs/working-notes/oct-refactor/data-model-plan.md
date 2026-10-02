# Plan: the data model and its documents

Block 5 of `master-plan.md`. Read `docs/architecture/data-model.md` first: it is the
model this block puts into the code, decided in `data-model-context.md`'s session.
`docs/architecture/the-path-of-one-message.md` follows one turn through it, and
`controller/adapters/postgres/schema.sql`, already written, is its PostgreSQL rendering
for block 6.

Base branch: `design/data-model`. Branches `feature/data-model-N`.

Goal: the controller, the memory store, web and the frontend work on the model of
`data-model.md`. Turns have ids, and the run id on the wire is the turn's. Messages and
events cross the store port as versioned documents, which the application encodes. An
answer keeps what was streamed, in order. A store's contract suite proves the memory
store, and block 6's PostgreSQL store passes it unchanged. Happy path only.

## Decisions

- **The documents are as `data-model.md` writes them.** The whole record, `"v": 1`,
  a `kind` on every part and event, fixed-width UTC times, lowercase uuids, the four
  part kinds and the nine event kinds, the versioning rules. The encoder and decoder
  live in `controller/application/documents.py`. A document that does not decode, a
  key nobody wrote, an unknown kind, or a version above 1 is an `InvalidValueError`
  that names what it found. The table of upgrades is empty: there is one version. The
  encoder cleans every string it writes, keys and values alike, dropping a NUL and
  replacing an unpaired surrogate with U+FFFD, and
  refuses NaN and infinity (`allow_nan=False`): PostgreSQL holds none of them, and a
  tool may send any. The controller cleans a title, a name, an email and a turn's
  error the same way before they reach a column.
- **The store port passes documents for messages and events**, typed as
  `Mapping[str, Any]` in `controller/ports/store.py`, with the keys beside them.
  Users, sessions and turns cross it as the contract's records. The store never
  imports the encoder.
- **Addressing from the owner down.** Every store operation on a session takes the
  owner's id and the session's. Every operation on a turn or its events takes the
  owner's, the session's and the turn's. A session that is not that owner's is not
  found.
- **The contract changes these records, and only these**:
  - a new `Turn`: `id`, `session_id`, `follows`, `model`, `state`, `started_at`,
    `ended_at`, `error`, `lease_until`;
  - `Session` gains `engine`, the engine its agent ran on when it was created;
  - a new `TurnLostError`, which the store raises to a runner that has lost its turn;
  - `TurnStarted` and `ActiveTurn` gain `turn_id`;
  - `Message` gains `turn_id`, `None` on a question, and `engine`;
  - `MessageCompleted` carries `message_id`, not the message;
  - `TurnEnded` carries the state alone; the error is on the `Turn`;
  - the controller port's `watch_turn` and `cancel_turn` take the turn's id after the
    session's.

  This plan is the owner's approval for these. Any other new class or field in a
  contract stops the work and asks.
- **The store port, as `data-model.md` asks:**
  - `add_user_if_absent(user) -> User` replaces `add_user`;
  - `get_session(owner, session)`, which leaves out a hidden session;
    `add_session`, `update_session`;
  - `sessions_of(owner, limit, before)`, a page of the owner's visible sessions,
    `before` being `(updated_at, id)` of the last one seen;
  - `hide_session(owner, session, at)`, refused while a turn is running
    (`TurnActiveError`), and `purge_session(owner, session)`;
  - `messages_of(owner, session) -> list[document]`;
  - `start_turn(owner, turn, question)`: stores the question's document, when there
    is one, with the turn, or neither, raising `TurnActiveError`. A regeneration has
    no new question;
  - `append_event(owner, session, turn, position, document, written_at, expires_at)`,
    accepting the same document again at a position it has and refusing another
    document there, or any append on a turn that is not running or whose
    `lease_until` has passed `written_at`, with `TurnLostError`;
  - `events_after(owner, session, turn, position) -> list[(position, document)]`;
  - `finish_turn(owner, session, turn, state, ended_at, error, answer, events,
    updated_at)`: the answer's document when the turn finished, the last events, the
    turn's state and the session's `updated_at`, in one operation, only if the turn
    is running and its `lease_until` has not passed `ended_at`, else `TurnLostError`;
  - `end_expired_turn(owner, session, now) -> Turn | None`: the session's running turn
    ended as `interrupted` if its `lease_until` has passed `now`, by one conditional
    write that also removes a marker store's "running" marker, with no event; nothing
    otherwise;
  - `active_turn(owner, session) -> Turn | None` and `get_turn(owner, session, turn)`;
  - `wait_for_events(owner, session, turn, after, timeout) -> bool`.

  `add_message`, `end_turn` and `delete_session` go.
- **The runner numbers its events**, from 1, and is their only writer, one at a time.
  It mints the answer's id afresh, so that no two runners' claims are one document.
  The first append is its claim on the turn, made before the handlers that end a
  turn, so a runner that has not won position 1 never finishes it: a second runner
  dispatched for the same turn is refused there and returns without running the
  engine. Its deadline for the engine is the lease less ten seconds, never more than
  the model's timeout, and with less than ten seconds left it does not claim. On
  `TurnLostError` from any append or from `finish_turn`, the runner closes the
  engine's stream (`aclosing`) and returns, writing nothing more, not from its error
  handlers either: the turn is another runner's, a reader ended it, or its lease has
  passed. It builds the answer's parts in the order they were streamed:
  text that arrives in a row is one text part until a tool call comes between; a tool
  call and its result are parts in their place. Reasoning is still streamed as events
  and kept in no part.
- **Events expire** at their write time plus a retention of 24 hours, a constant of
  the application for now.
- **A lease is written and read, not renewed.** `lease_until` is the turn's start plus
  the turn timeout and a minute, and every write of the runner's is refused past it.
  The controller calls `end_expired_turn` in `open_session`, before `start_turn`, in
  `watch_turn` when a wait returns nothing new, and first thing in `cancel_turn` and
  `delete_session`, so a turn a dead process left running blocks its session for the
  lease and no longer. A watcher that finds the turn ended with no `turn_ended` event
  yields one from the record, under the last position stored. `close` waits for the
  turns its dispatcher runs, up to a bound below the service's stop timeout, then
  cancels them naming the close as the reason, and the runner ends a turn cancelled
  that way as `interrupted`; a finish is shielded from cancellation once `Done` has
  arrived, so a restart loses no finished answer. A crash leaves its turns to the
  lease. Renewing a lease for long turns, cancelling through the store, and the sweep
  for turns nobody reads are stage two's hardening.
- **Cancelling stays in the process that runs the turn.** `cancel_turn` ends an
  expired turn first, then asks the dispatcher, which cancels the task and waits for
  it; a runner that never claimed wrote nothing, so the controller then ends the turn
  `cancelled` if it is still running. A turn this process does not run is refused
  with `TurnActiveError`, 409, until stage two's cancel through the store.
- **The turn dispatcher is a port.** `TurnDispatcher.dispatch(owner, session, turn)`,
  `cancel(owner, session, turn) -> bool`, true when this process ran the turn and has
  cancelled and awaited it, and `close(timeout)`, which waits for its turns and then
  cancels the rest naming the close, in `controller/ports`. The in-process adapter
  keeps the asyncio tasks, and is handed the controller's `run_turn` by the
  composition, so that no adapter imports the application. The controller gains
  `run_turn(owner, session, turn)`, which loads everything a turn needs by those ids:
  what a worker in another process would call.
- **Deleting hides, forgets, then purges, in one call.** `delete_session` cancels the
  turn its dispatcher runs, if any, and waits for it to end; hides the session, which
  the store refuses while a turn is running (`TurnActiveError`, 409: a turn run by
  another process is the owner's to cancel first); calls `forget` on the engine the
  session records; and purges. So no runner and no engine writes under a purge. The
  scheduled purge, trash before it, and a delete that cancels through the store are
  stage two's housekeeping.
- **A session records its engine.** `Session.engine` is set by `start_session` from
  the agent's configuration and never changes. A turn runs on it, and the purge
  forgets on it, not on the engine the configuration names today: the operator may
  have moved or removed the agent. The controller builds the engines the agents name
  at `open`, as today, and any other engine a session names on demand, from the
  installed factories, with `UnknownEngineError` when the build lacks it. What a
  moved agent's sessions do on the new engine is stage two. An answer's document
  records the engine beside `agent` and `model`, and web fills `ProvenanceView.engine`
  from it.
- **Paging.** `list_sessions` returns a cursor, which is `(updated_at, id)` of the last
  session in the page, encoded by the controller as an opaque string. A cursor that
  does not decode is an `InvalidValueError`.
- **The wire changes as `wire.md`'s "Planned with turn ids" says**, and the section
  becomes the wire's text. The run id is the turn's id. The events URL is
  `GET /api/conversations/{id}/runs/{run_id}/events`, and `GET /api/runs/{run_id}/events`
  goes. The two headers carry the turn's id and the conversation's. An answer's
  `provenance.run_id` is its `turn_id`. The OpenAPI snapshot changes only if a schema
  does; the streaming endpoints are outside it.

## Not in this block

- PostgreSQL: block 6, `postgres-plan.md`.
- Serving `ended_badly`, and every refusal of the wire's "not yet served": stage two.
- Leases renewed, cancel through the store, the sweep for turns nobody reads: stage
  two.
- Keeping reasoning in the answer: a part kind in the format already, written by
  nobody until it is decided (`data-model.md`, "Parts").
- Fork, titles and bounds: stage two.
- The extension points for stores, dispatchers and engines' storage from outside this
  repository: stage three.

## Rules

- One branch per step, `feature/data-model-N`, from the previous step's branch. One commit
  per branch, pushed. No pull requests, no reviews between steps. Every step leaves
  the suite green, so a step that changes a signature changes its callers with it.
- Minimal code for the main flows. No edge cases, no defensive checks beyond the
  refusals this plan names. Comments only where the code does not say it; no
  docstrings unless a line cannot be read without one.
- The layer rules of `docs/architecture/rules.md` hold; `tests/unit/test_architecture.py`
  runs them.
- Before each commit, from `backend/`: `uv run --locked ruff check --config
  pyproject.toml . ../scripts ../demo ../examples`, `uv run --locked black --check
  --config pyproject.toml . ../scripts ../demo ../examples`, `uv run --locked pytest
  tests/unit -q`, and `uvx reuse lint` from the root, all green. A step that touches
  the frontend also runs, from `frontend/`, `npm run typecheck`, `npm run lint` and
  `npm test`. Every new file starts with the two licence header lines. Line length 100.
- Commit messages end, after a blank line, with the attribution lines the session's
  system reminder gives.

## Steps

1. **The contract.** The record changes above, and their callers follow with no change
   of behaviour: the runner sends `MessageCompleted(message_id)` and
   `TurnEnded(state)`, `agui.py` reads them, `Message.turn_id` is `None` everywhere for
   now, `Session.engine` and `Message.engine` are filled from the configuration. `Turn`
   and `TurnLostError` are added and not used yet. Test: the unit suite as it is,
   adjusted where it builds these records.
2. **The documents.** `controller/application/documents.py`: `message_to_document`,
   `message_from_document`, `event_to_document`, `event_from_document`. Test: every
   part kind and every event kind round-trips; times and ids are written in their one
   spelling; a NUL is dropped and a lone surrogate replaced in a question's text, a
   piece, a tool's arguments, their keys included, and a result; a NaN in arguments
   is refused; an unknown key, an unknown kind, a missing `v` and `"v": 2` are
   refused by name.
3. **The store port, the memory store and the contract suite.** The port above. The
   memory store holds documents and turns by the same keys a partitioned store would.
   `tests/contracts/store.py`: a suite a test file subclasses with a `new_store()`,
   over every operation of the port, including a second running turn refused, the
   same document accepted again at its position and another document there refused,
   an append and a finish on an ended turn refused, an append and a finish past the
   lease refused, a hide refused while a turn runs, `end_expired_turn` ending a turn
   whose lease has passed and leaving one whose lease has not, a start and a hide
   succeeding after it, a hidden session not found, another owner's session not
   found, a page and the next page, a finished turn's answer and events read back, and
   `wait_for_events` returning on an append and at its timeout. The controller and the
   runner call the new port, encoding and decoding with step 2's functions. Test: the
   suite over the memory store; the controller's unit tests as they are.
4. **The runner and the dispatcher.** The runner numbers its events, claims before
   its handlers, builds the answer's parts in stream order, writes `turn_id` on the
   answer, the lease and the events' expiry, takes its deadline from the lease,
   cleans the error it records, finishes the turn in one shielded `finish_turn`, and
   stops on `TurnLostError`. `TurnDispatcher` and its in-process adapter;
   `RobinautsController.run_turn`, on the session's engine; the composition wires
   them. `end_expired_turn` is called from `open_session`, `start_turn`, `watch_turn`,
   `cancel_turn` and `delete_session`, and `watch_turn` yields `TurnEnded` from the
   record when the events hold none; `cancel_turn` ends a never-claimed turn and
   refuses another process's; `close` waits, then interrupts. Test: a turn whose model
   writes text, calls a tool and writes again stores one answer with the four parts
   in that order; two turns of one session number their events from 1 each; a
   cancelled turn stores no answer and ends `cancelled`; a runner whose first append
   is refused runs no engine and never finishes the turn; a runner refused mid-stream
   writes nothing more, and the turn keeps the state the reader gave it; a turn whose
   lease has passed is `interrupted` by opening the session, its watcher receives
   `TurnEnded(interrupted)`, and a new turn then starts; a cancel before the runner
   claimed ends the turn `cancelled`; `close` during a turn ends it `interrupted`,
   and a close during a finish keeps the answer.
5. **Paging and deleting.** `list_sessions` with its cursor; `delete_session` ends an
   expired turn, cancels its own turn and waits, hides, forgets on the session's
   engine, and purges. Test: three sessions listed two at a time come back in order,
   none twice; a cursor that does not decode is refused; a deleted session is not
   found, is not listed, and its engine was asked to forget it; a session deleted
   during its turn has the turn end `cancelled` before its engine is asked to forget;
   a session whose agent has moved to an engine no agent names any more is still
   deleted, its own engine built on demand and asked to forget.
6. **Web.** The run id is the turn's id; the new events route and the old one gone; the
   two headers; `provenance.run_id` from `turn_id` and `provenance.engine` from the
   answer; the cancel route passes its run id; a delete refused by a running turn,
   and a cancel of a turn another process runs, are 409; `robinauts start` gives
   uvicorn a graceful-shutdown timeout below a service's stop timeout, so `close`
   runs. `wire.md`'s "Planned with turn ids" becomes the wire's text, with the two
   409s, and `docs/architecture/controller.md` says that `watch_turn` and
   `cancel_turn` name the turn, that `open` leaves ended turns to the lease, and that
   `delete_session` refuses a running turn. Test: `test_web_turns.py` and
   `test_agui.py`, adjusted, and a re-attach through the new route.
7. **The frontend.** The client opens
   `/api/conversations/{conversationId}/runs/{runId}/events`, with the conversation's
   id it already has in `Attached`, and re-attaches after a reload with the opened
   conversation's `run_id`. Test: `client.test.ts` and the tests that build the URL,
   adjusted.
8. **Proof.** The unit suite and the layer rules green. In the browser, over
   `examples/echo.toml` and an example on a framework engine where a key is in the
   session: a turn streams and finishes; a reload during a turn re-attaches to it by
   the turn's id; a reload after a turn whose model wrote text before a tool call shows
   that text; two turns of one session each re-attach from position 1.
   `data-model.md`'s "What this asks of the code" is emptied of what is done. A short
   `data-model-progress.md` records what was verified.
