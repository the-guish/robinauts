# Plan: the UI works end to end over the echo engine

Goal: start `python -m robinauts.web`, open the UI, send a message, see the echo tool's
call, its result and the answer. No sign-in. Happy path only.

## Rules

- One branch per step, `feature/echo-e2e-stepN`, from the previous step's branch. One
  commit per branch, pushed. No pull requests, no reviews between steps.
- Minimal code for the happy path. No edge cases, no defensive checks on input, no
  docstrings unless a line cannot be understood without one. All of that comes later.
- The contracts between layers stay as they are: web imports `controller.contract`
  only; the controller imports `agent_engines.contract` only; engines import their
  contract only. Adding a field to a contract dataclass is fine. A new class in a
  contract, or a new import direction, stops the work and asks the owner.
- Before the commit: `uv run ruff check --config pyproject.toml . ../scripts ../demo ../examples`,
  `uv run black --check --config pyproject.toml . ../scripts ../demo ../examples`,
  `uv run pytest tests/unit -q --deselect tests/unit/test_openapi_snapshot.py`, and
  `uvx reuse lint` from the root, all from `backend/`, all green. Every new file
  starts with the two licence header lines.
- Tests: the unit tests a step needs to prove its happy path, over the echo engine and
  the in-memory store. Nothing more.

## Steps

1. A store port inside the controller and its in-memory implementation over dicts:
   users, conversations, messages, turn events by position, the active turn.
2. `ensure_user`; web calls it once at start-up with the local identity.
3. `list_agents` and `list_models` from the configuration.
4. The local start's configuration: one agent on `echo`, one provider and one model.
5. `start_conversation`: conversation, question, engine session, turn begun.
6. The turn runner: a task that streams the engine, stores numbered turn events and the
   answer with its checkpoint id, and ends the turn.
7. `watch_turn`: stored events past a position, waiting for new ones until `TurnEnded`.
8. `open_conversation`: the visible thread and the active turn.
9. `list_conversations`, `rename_conversation`, `delete_conversation` with `forget`.
10. `send_message`, `regenerate_answer`, `cancel_turn`.
11. Web's turn and watch routes answer what the frontend reads, as `docs/specs/wire.md`
    describes.
12. `backend/openapi.json` regenerated from web; the frontend's types regenerated and the
    frontend adapted where the shapes changed.
13. Started locally over echo: a message sent in the browser shows the call, the result
    and the answer, and survives a reload mid-turn. Checked with Playwright.
14. One end-to-end test over the web app, the echo engine and the in-memory store.
