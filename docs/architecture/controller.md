# Controller

The operations the controller offers its shells: the web and a terminal.

Sign-in is not here. A shell decides who is asking and names the user; every
operation below is asked for a user, and answers only what that user owns.

A session is one conversation of a user with an agent. To the controller it is the record,
its messages and its turns, owned by one user. To an engine it is the memory it keeps under
the session's id, and nothing more. To web it is what the wire calls a conversation; who is
signed in is web's own concern, the user session.

## Lifecycle

- `open`: build the stores on the given storage, build the engines the configuration names, and
  run their setup. A turn a process that went away left running is ended by its lease, by the
  next reader to find it.
- `close`: wait for the turns this process runs, bounded, interrupt the rest, and release the
  storage.

## Worker

The worker claims queued turns from the store and dispatches them in its process. It is
not an operation of the controller. The shell starts it after `open` and stops it before
`close` (`web/lifecycle.py`).

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
- `delete_session`: the records, and the engine's memory with them, after stopping a running turn.
- `fork_session`: a new session from a message of another, independent from then on.

## Turns

- `start_session`: the first message, with the agent and the model; mints the session.
- `send_message`: a message under a chosen parent, with the model. An edit is this under an
  earlier parent.
- `regenerate_answer`: the answer to a question again, under the same question.
- `cancel_turn`: stop the turn named, whichever process runs it.
- `watch_turn`: the events of the turn named, from a position, as they happen, ending with how
  it ended.

## Housekeeping

Run by the process that holds the controller, on a schedule of its own, never by a shell.

- `sweep`: delete what has expired, and forget its memory.
