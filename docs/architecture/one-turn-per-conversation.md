# One turn per conversation

How Robinauts stops two workers from working on the same conversation at
once. In the code a conversation is a **session**, and one answer to one
question is a **turn**.

There are three guards. Each one covers a different way the problem could
happen.

## Guard 1: a conversation has at most one active turn

1. A new message can come from the UI, the API or any other client. It
   always becomes a turn the same way.
2. Every entry point in the controller (send, edit, retry, resume, and
   create with a first message) calls
   [`_queue_turn`](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/application/controller.py#L311).
3. `_queue_turn` calls
   [`store.queue_turn`](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/application/controller.py#L353).
   This is the only place that creates a turn.
4. `queue_turn` opens
   [one transaction](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/adapters/postgres/store.py#L327).
   It inserts the question, then a queued task, then the turn in state
   `active`.
5. The `turns` table has a
   [partial unique index](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/adapters/postgres/schema.sql#L340)
   on `session_id`, limited to active turns. A conversation can have many
   finished turns, but only one active turn.
6. If the conversation already has an active turn, the insert fails. The
   store
   [turns that failure](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/adapters/postgres/store.py#L368)
   into `TurnActiveError`.
7. The transaction rolls back. The new question and its task are not stored.
8. The web layer
   [answers `TurnActiveError` with HTTP 409](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/web/app.py#L113).
9. Two messages that arrive at the same moment race on the same index. Only
   one of them commits.

So a second message never reaches a worker.

## Guard 2: one worker claims each task

1. A worker asks for queued tasks of its own queue.
2. The store claims them with
   [one `UPDATE`](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/adapters/postgres/store.py#L100).
   It sets the task to `running` and gives it a lease.
3. The `UPDATE` selects rows with
   [`FOR UPDATE SKIP LOCKED`](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/adapters/postgres/store.py#L108).
   Two workers that ask at the same time never get the same task.

## Guard 3: the first event claims the turn

A task can still be dispatched twice. An event can be delivered twice, or a
task can be re-dispatched after its lease passes.

1. Before it runs the agent, the runner
   [writes `MessageStarted` at position 1](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/worker/turns.py#L187).
2. Each runner creates a new answer id, so two runners never write the same
   event.
3. The table `turn_events` has the
   [primary key `(turn_id, position)`](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/adapters/postgres/schema.sql#L386).
   Only one event can sit at position 1.
4. The second runner gets a unique violation. The store
   [raises `TurnLostError`](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/adapters/postgres/store.py#L407).
5. The runner
   [returns without running the agent](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/worker/turns.py#L188).
6. If the same event is already at that position, it is the runner's own
   write, retried after a lost acknowledgement. The store accepts it.

## Leases: when a worker dies

1. A running task has a lease. The runner
   [renews it](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/adapters/postgres/store.py#L518)
   while the turn runs.
2. If the worker dies, the lease runs out.
3. When a new message arrives, `_queue_turn` first calls
   [`_end_expired`](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/application/controller.py#L408).
   This ends a turn whose lease has passed, so the conversation is free
   again.
4. Every event write
   [checks](https://github.com/open-shipyard/robinauts/blob/main/backend/src/robinauts/controller/adapters/postgres/store.py#L74)
   that the turn is still active and its lease has not passed.
5. If the old worker comes back, its next write is refused with
   `TurnLostError`. It stops, and it cannot work beside the new turn.

## Summary

- Per conversation: the unique index allows one active turn. A second
  message gets 409.
- Per task: `SKIP LOCKED` gives each task to one worker.
- Per turn: the position-1 event lets only one runner start the agent.
- Leases: a dead worker's turn can be replaced, and the worker cannot write
  if it comes back.

## See also

- [data-model.md](data-model.md), "Rules every store keeps"
- [the-path-of-one-message.md](the-path-of-one-message.md), steps 4 to 7
- [../specs/agent-engines.md](../specs/agent-engines.md), "What the caller
  promises"
