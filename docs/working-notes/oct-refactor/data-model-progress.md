# Block 5: the data model in the code, what was verified

Branch `design/data-model`, one commit per step of `data-model-plan.md`. Every step left
the unit suite and the layer rules green (`uv run --locked pytest tests/unit`: 2,997
passed, `lint-imports`: 0 broken), ruff, black and `reuse lint` clean; step 7 left the
frontend's typecheck, lint, format check and its 433 tests green.

## The steps

1. **The contract.** `Turn`, `TurnLostError`, `Session.engine`, `Message.engine` and
   `Message.turn_id`, `TurnStarted` and `ActiveTurn` with the turn's id,
   `MessageCompleted(message_id)`, `TurnEnded(state)`. The controller port's `watch_turn`
   and `cancel_turn` took the turn's id in step 3, with `None` naming the latest or the
   running turn until web carried it in step 6.
2. **The documents.** `controller/application/documents.py`, with the cleaning of every
   string, keys included, and the refusals by name.
3. **The store port, the memory store and the contract suite.** `tests/contracts/store.py`,
   which the memory store passes. `latest_turn(owner, session)` was added to the port: a
   watcher that names no turn, and the wire's "how the last turn ended", read it.
4. **The runner and the dispatcher.** The deadline from the lease, the claim before the
   handlers, the shielded finish, the close that waits and then interrupts, the readers
   that end an expired turn, the watcher that supplies `turn_ended` from the record.
   Deleting landed here too, since it cancels through the dispatcher.
5. **Paging.** The cursor.
6. **Web.** The run id is the turn's id; the new events route; the two headers; the two
   409s; uvicorn's graceful-shutdown timeout. `wire.md` and `controller.md` say so.
7. **The frontend.** The events URL names the conversation and the run.

## The proof

In headless Chromium, over `examples/echo.toml`, through the built interface:

- a turn streams and finishes, and a second turn after it; each was answered with a run
  id of its own, neither the conversation's, and `provenance.engine` is `echo`;
- a reload after the two turns shows both exchanges, read from the store;
- each run re-attaches from position 1 through
  `GET /api/conversations/{id}/runs/{run_id}/events?after=0`: `id: 1` first, nine events,
  `RUN_FINISHED` last;
- a run id the conversation does not have answers 404.

Not verified in a browser: a reload *during* a turn, and a turn whose model wrote text
before a tool call, which need an engine slower than echo and a model key, neither of
which this environment had. The unit suite covers both paths
(`test_a_turn_whose_lease_has_passed_is_interrupted_and_a_new_turn_starts`,
`test_an_answer_keeps_its_parts_in_stream_order`,
`test_a_turn_is_re_attached_to_by_the_conversation_and_the_run`).

## Decided on the way

- The `turn_ended` a watcher supplies for a turn ended by a reader carries the last
  position stored, 0 when the runner never claimed.
- An engine that streamed no text at all still hands its answer over: `Done.text` becomes
  the one text part then, and is ignored otherwise.
- `run_turn` loads by ids but is still handed the question it answers from the store's
  messages, decoded; a worker in another process would call it the same way.
