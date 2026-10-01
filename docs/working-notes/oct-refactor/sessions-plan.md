# Plan: the controller speaks of sessions

Runs after the Pydantic AI block and before the data model. Rules as in
`config-plan.md`: one branch per step, `feature/sessions-N`, one commit each, pushed.

Goal: "session" is the one word for a conversation from the controller down. The
engine contract already says session; the controller and the way web calls it follow.
The sign-in session becomes the user session, so the word is free.

## What changes

- The controller's contract: `Conversation` -> `Session`, `ConversationPage` ->
  `SessionPage`, `OpenedConversation` -> `OpenedSession`,
  `ConversationNotFoundError` -> `SessionNotFoundError`, every `conversation_id` field
  and argument -> `session_id`, the operations `list_conversations`,
  `open_conversation`, `rename_conversation`, `delete_conversation`,
  `fork_conversation`, `start_conversation` -> `list_sessions`, `open_session`,
  `rename_session`, `delete_session`, `fork_session`, `start_session`.
- The controller's application, ports, adapters and composition follow, and the tests.
- Web calls the controller with the new names. The sign-in names become user session:
  `SessionResponse` -> `UserSessionResponse`, the route function `current_session` ->
  `current_user_session`, and the same in `docs/architecture/web.md`.
- `docs/architecture/controller.md` and `web.md` use the new operation names, and
  `controller.md` says once what a session is to each layer.

## What does not change

- AG-UI's words on the wire, thread and run, and web's mapping of both to the session's
  id.
- The HTTP paths and the JSON field names the frontend reads (`/api/conversations`,
  `conversation_id`, `/auth/session`), the OpenAPI snapshot and the frontend. The wire
  keeps saying conversation until the frontend is revisited; `wire.md` gains one sentence
  saying the wire's conversation is the controller's session.
- The engine contract, which already speaks of sessions, and legacy.

## Steps

1. The contract and `docs/architecture/controller.md`: the renames above, nothing else.
2. The controller's application, ports, adapters, composition and their tests follow the
   contract.
3. Web calls the controller with the new names; the sign-in names become user session;
   `docs/architecture/web.md` and the sentence in `wire.md`. The OpenAPI snapshot is
   regenerated only if a schema name changed; the frontend is untouched.
4. Proof: the unit suite and the layer rules green, and one turn in the browser over
   `examples/echo.toml`.
