# Controller

The operations the controller offers its shells: the web and a terminal.

Sign-in is not here. A shell decides who is asking and names the user; every
operation below is asked for a user, and answers only what that user owns.

## Lifecycle

- `open`: build the stores on the given storage, build the engines the configuration names, run
  their setup, and end the turns a process that went away left active.
- `close`: stop the active turns, bounded, and release the storage.

## Users

- `ensure_user`: the user for an identity, created on first sight. Web passes the provider and
  the subject; a terminal passes the operating system's user.

## Catalogue

- `list_agents`
- `list_models`

## Conversations

- `list_conversations`: most recently updated first, a page at a time.
- `open_conversation`: one moment of a conversation, its messages and its active turn if any.
- `rename_conversation`
- `delete_conversation`: the records, and the engine's memory with them.
- `fork_conversation`: a new conversation from a message of another, independent from then on.

## Turns

- `start_conversation`: the first message, with the agent and the model; mints the conversation.
- `send_message`: a message under a chosen parent, with the model. An edit is this under an
  earlier parent.
- `regenerate_answer`: the answer to a question again, under the same question.
- `cancel_turn`: stop the conversation's active turn.
- `watch_turn`: the events of the conversation's active turn, from a position, as they happen.

## Housekeeping

Run by the process that holds the controller, on a schedule of its own, never by a shell.

- `sweep`: delete what has expired, and forget its memory.
