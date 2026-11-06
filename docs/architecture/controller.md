# Controller

The operations the controller offers its shells: the web and a terminal.

Sign-in is not here. A shell decides who is asking and names the user; every
operation below is asked for a user, and answers only what that user owns.

A session is one conversation of a user with an agent. To the controller it is the record,
its messages and its turns, owned by one user. To an engine it is the memory it keeps under
the session's id, and nothing more. To web it is what the wire calls a conversation; who is
signed in is web's own concern, the user session.

## Lifecycle

- `open`: open the store on the given storage. A turn a process that went away left running is
  ended by its lease, by the next reader to find it.
- `close`: release the storage.

## Worker

The worker handles the tasks its process runs, each running one turn. It builds the engines
the configuration names, and runs their setup, when it starts. It claims queued tasks from
the store and runs them, up to `max_running_tasks_per_worker` at once. A conversation's
first turn says so, and the worker creates the engine's session before it runs. It renews
the tasks' leases every `heartbeat_seconds` and stops a task that any process asks to stop.
Its stop waits for those tasks, bounded, and interrupts the rest. The worker is not an
operation of the controller. The shell starts it after `open` and stops it before `close`
(`web/lifecycle.py`). A server finishes its start-up once its worker has started.

The server's worker never claims the turns of a sandboxed session. A sandbox worker does:
the same worker, composed on the HTTP store, in a process of its own, which reaches the
server over the worker API alone (`sandbox-workers.md`).

## Worker API

What web answers a sandbox worker, behind the `WorkerApi` port, for the sandbox each call
names: `claim` a task, waiting for one; `load_turn`; `append_events`; `renew` the leases of the
tasks it runs, answering those asked to stop; `finish` a turn. Everything a sandbox worker
sends is checked before the store sees it.

## Users

- `ensure_user`: the user for an identity, created on first sight. Web passes the provider and
  the subject; a terminal passes the operating system's user.

## Catalogue

- `list_agents`
- `list_models`

## Sessions

- `list_sessions`: most recently updated first, a page at a time.
- `open_session`: one moment of a session, its messages and its active turn if any, and
  whether its sandbox is gone.
- `rename_session`
- `delete_session`: hide the session, after stopping a running turn. A soft delete: the records
  and the engine's memory stay.
- `fork_session`: a new session from a message of another, independent from then on.

## Turns

- `start_session`: the first message, with the agent and the model; mints the session.
- `send_message`: a message under a chosen parent, with the model. An edit is this under an
  earlier parent.
- `regenerate_answer`: the answer to a question again, under the same question.
- `resume_answer`: the turn of the session's latest answer, when it failed, continued from
  what its engine saved.
- `cancel_turn`: stop the turn named, whichever process runs it. It returns once the stop is
  asked; the turn's watchers see it end.
- `watch_turn`: the events of the turn named, from a position, as they happen, ending with how
  it ended.

## Housekeeping

Run by the process that holds the controller, on a schedule of its own, never by a shell.

- `sweep`: end the turns whose lease has passed.
