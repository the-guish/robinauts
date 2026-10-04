# Controller

The operations the controller offers its shells: the web and a terminal.

Sign-in is not here. A shell decides who is asking and names the user; every
operation below is asked for a user, and answers only what that user owns.

A session is one conversation of a user with an agent. To the controller it is the record,
its messages and its turns, owned by one user. To an engine it is the memory it keeps under
the session's id, and nothing more. To web it is what the wire calls a conversation; who is
signed in is web's own concern, the user session.

## Lifecycle

- `open`: build the stores on the given storage, build the engines the configuration names, run
  their setup, and start the heartbeat that renews the leases of the turns this pod runs. A turn
  a process that went away left running is ended by its lease, by the next reader to find it.
- `close`: wait for the turns this process runs, bounded, with the heartbeat still renewing
  their leases, interrupt the rest, stop the heartbeat, and release the storage.

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
- `delete_session`: the records, and the engine's memory with them; a running turn is stopped
  first, whichever process runs it, and the delete is refused only when it has not ended within
  a few seconds.
- `fork_session`: a new session from a message of another, independent from then on.

## Turns

- `start_session`: the first message, with the agent and the model; mints the session.
- `send_message`: a message under a chosen parent, with the model. An edit is this under an
  earlier parent.
- `regenerate_answer`: the answer to a question again, under the same question.
- `cancel_turn`: stop the turn named, whichever process runs it: through the store, which that
  process hears at once or reads back at its next heartbeat. Says whether the turn has ended
  yet.
- `watch_turn`: the events of the turn named, from a position, as they happen, ending with how
  it ended.

## Housekeeping

Run by the process that holds the controller, on a schedule of its own, never by a shell.

- `sweep`: delete what has expired (turn events, sign-in records), end the turns whose lease has
  passed in every session, keeping what each had answered, and finish the purges of hidden
  sessions that never finished, forgetting their memory. Every process runs it, every five
  minutes, and the turns left behind every heartbeat; the store lets one process at a time do
  each task (`pg_try_advisory_xact_lock`), in batches of a thousand, and each is safe to
  repeat.
