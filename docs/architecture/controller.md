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
the configuration names, and runs their setup, when it starts. It claims queued tasks and
runs them, up to `max_running_tasks_per_worker` at once. A conversation's first turn says
so, and the worker creates the engine's session before it runs. It renews the tasks' leases
every `heartbeat_seconds` and stops a task that any process asks to stop. Its stop waits for
those tasks, bounded, and interrupts the rest. The worker is not an operation of the
controller. The shell starts it after `open` and stops it before `close`
(`web/lifecycle.py`). A server finishes its start-up once its worker has started, and stops
its worker before it stops listening.

The worker reaches the core through one port, `TurnBroker` (`contract/ports.py`), and never
through the store. These are its operations, the store's of the same names:

- `claim_tasks`, `wait_for_queued`: the queued tasks, made the worker's.
- `renew_leases`, `listen_for_cancels`: the leases of the tasks it runs, and the tasks asked
  to stop.
- `get_task`, `find_turn`: what a task names.
- `get_session`, `messages_of`, `sessions_of`: what a turn reads, and its built-in tools.
- `append_event`, `finish_turn`: what a turn writes.

A worker in the server's process has it over the controller's store
(`application/turn_broker.py`). A worker in a process of its own, which a server on
PostgreSQL spawns, has it over HTTP: the same operations as the API's `/internal/worker/`
routes (`docs/architecture/web.md`), with the secret its server handed it at its start. The
engines keep their memory apart, in the worker's `EngineMemory`: on PostgreSQL a pool of the
worker's own, for the engines' tables, never the core's.

## Users

- `ensure_user`: the user for an identity, created on first sight. Web passes the provider and
  the subject; a terminal passes the operating system's user.

## Catalogue

- `list_agents`
- `list_models`

## Sessions

- `list_sessions`: most recently updated first, a page at a time.
- `open_session`: one moment of a session, its messages and its active turn if any.
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
