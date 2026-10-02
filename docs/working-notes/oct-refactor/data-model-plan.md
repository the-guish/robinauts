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
  that names what it found. The table of upgrades is empty: there is one version.
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
  - `TurnStarted` and `ActiveTurn` gain `turn_id`;
  - `Message` gains `turn_id`, `None` on a question;
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
  - `hide_session(owner, session, at)` and `purge_session(owner, session)`;
  - `messages_of(owner, session) -> list[document]`;
  - `start_turn(owner, turn, question)`: stores the question's document, when there
    is one, with the turn, or neither, raising `TurnActiveError`. A regeneration has
    no new question;
  - `append_event(owner, session, turn, position, document, expires_at)`, refusing a
    position it has;
  - `events_after(owner, session, turn, position) -> list[(position, document)]`;
  - `finish_turn(owner, session, turn, state, ended_at, error, answer, events,
    updated_at)`: the answer's document when the turn finished, the last events, the
    turn's state and the session's `updated_at`, in one operation, only if the turn
    is running;
  - `active_turn(owner, session) -> Turn | None` and `get_turn(owner, session, turn)`;
  - `wait_for_events(owner, session, turn, after, timeout) -> bool`.

  `add_message`, `end_turn` and `delete_session` go.
- **The runner numbers its events**, from 1, and is their only writer. It builds the
  answer's parts in the order they were streamed: text that arrives in a row is one
  text part until a tool call comes between; a tool call and its result are parts in
  their place. Reasoning is still streamed as events and kept in no part.
- **Events expire** at their write time plus a retention of 24 hours, a constant of
  the application for now.
- **A lease is written, not renewed.** `lease_until` is the turn's start plus the turn
  timeout and a minute. Nothing renews it and nothing reads it in this block. Ending a
  turn whose lease has passed, renewing a lease for long turns, and cancelling through
  the store are stage two's hardening, with the sweep that reads them. Cancelling stays
  in the process that runs the turn.
- **The turn dispatcher is a port.** `TurnDispatcher.dispatch(owner, session, turn)`
  and `cancel(owner, session, turn)` in `controller/ports`. The in-process adapter keeps
  the asyncio tasks, and is handed the controller's `run_turn` by the composition, so
  that no adapter imports the application. The controller gains
  `run_turn(owner, session, turn)`, which loads everything a turn needs by those ids:
  what a worker in another process would call.
- **Deleting hides, forgets, then purges, in one call.** `delete_session` hides the
  session, calls the engine's `forget`, and purges. The scheduled purge, and trash
  before it, are stage two's housekeeping.
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
- Leases read and renewed, cancel through the store, the sweep: stage two.
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
   now. `Turn` is added and not used yet. Test: the unit suite as it is, adjusted
   where it builds these records.
2. **The documents.** `controller/application/documents.py`: `message_to_document`,
   `message_from_document`, `event_to_document`, `event_from_document`. Test: every
   part kind and every event kind round-trips; times and ids are written in their one
   spelling; an unknown key, an unknown kind, a missing `v` and `"v": 2` are refused
   by name.
3. **The store port, the memory store and the contract suite.** The port above. The
   memory store holds documents and turns by the same keys a partitioned store would.
   `tests/contracts/store.py`: a suite a test file subclasses with a `new_store()`,
   over every operation of the port, including a second running turn refused, a
   position offered twice refused, a hidden session not found, another owner's session
   not found, a page and the next page, a finished turn's answer and events read back,
   and `wait_for_events` returning on an append and at its timeout. The controller and
   the runner call the new port, encoding and decoding with step 2's functions. Test:
   the suite over the memory store; the controller's unit tests as they are.
4. **The runner and the dispatcher.** The runner numbers its events, builds the
   answer's parts in stream order, writes `turn_id` on the answer, the lease and the
   events' expiry, and finishes the turn in one `finish_turn`. `TurnDispatcher` and its
   in-process adapter; `RobinautsController.run_turn`; the composition wires them.
   Test: a turn whose model writes text, calls a tool and writes again stores one
   answer with the four parts in that order; two turns of one session number their
   events from 1 each; a cancelled turn stores no answer and ends `cancelled`.
5. **Paging and deleting.** `list_sessions` with its cursor; `delete_session` hides,
   forgets and purges. Test: three sessions listed two at a time come back in order,
   none twice; a cursor that does not decode is refused; a deleted session is not
   found, is not listed, and its engine was asked to forget it.
6. **Web.** The run id is the turn's id; the new events route and the old one gone; the
   two headers; `provenance.run_id` from `turn_id`; the cancel route passes its run id.
   `wire.md`'s "Planned with turn ids" becomes the wire's text, and
   `docs/architecture/controller.md` says that `watch_turn` and `cancel_turn` name the
   turn. Test: `test_web_turns.py` and `test_agui.py`, adjusted, and a re-attach through
   the new route.
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
