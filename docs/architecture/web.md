# Web

The operations the web shell offers the frontend.

## Sign-in

- `sign_in`: start the sign-in with a provider, and finish it when the provider answers.
- `sign_out`
- `current_session`: who is signed in, if anyone.

## Catalogue

- `list_agents`
- `list_models`

## Conversations

- `list_conversations`: most recently updated first, a page at a time.
- `open_conversation`: one moment of a conversation, its messages and its active turn if any.
- `rename_conversation`
- `delete_conversation`
- `fork_conversation`: a new conversation from a message of another, independent from then on.

## Turns

- `start_conversation`: the first message, with the agent and the model; mints the conversation.
- `send_message`: a message under a chosen parent, with the model. An edit is this under an earlier parent.
- `regenerate_answer`: the answer to a question again, under the same question.
- `cancel_turn`: stop the conversation's active turn.
- `watch_turn`: the events of the conversation's active turn, from a position, as they happen.
