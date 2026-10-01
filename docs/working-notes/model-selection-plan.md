# Plan: the user picks the model

Written 2026-09-27 on `feature/model-selection`, after the agent line under
a conversation's title and the always-shown agent picker (merged in #16). Revised
the same day against main at #18 (one visible thread, the schema version
fixed at 1), with the questions it ended on settled. Not a spec: the
decisions, what they cost across the codebase, and the order to build them
in. The spec sentences to change are listed at the end.

## What is wanted

- An agent stays what it is: a name, a system prompt, an engine and a
  **default** model, pinned to a conversation when it starts
  ([specs/legacy/agents-engines-models.md](../specs/legacy/agents-engines-models.md)).
- The user can pick the **model** from the ones the operator configured
  (`[models.*]`), at the start of a conversation and again at any point
  during it. The choice sits next to the agent picker on the empty chat and
  next to the agent line on an open conversation.
- Every answer keeps saying which model produced it, which it already does
  (`Provenance.model`).

## Decisions

1. **The model is a property of the conversation, like the agent, but
   changeable.** Not a field on each turn request. A choice that lived only
   in the browser would be gone on a reload and invisible to another
   channel; one that is stored is what a regenerate, an edit and a resumed
   run all read. Each run copies it (`runs.model` already exists), so a run
   in flight keeps the model it started with and provenance stays per
   answer.

2. **`conversations.model` is `NOT NULL`: the agent's default is copied in
   when the conversation starts**, unless the user picked another. What a
   conversation runs on is then read off the conversation alone, and a
   reader never has to know what the agent's default was at some earlier
   time. The cost is a guarantee: changing an agent's model in the
   configuration no longer reaches its existing conversations, only new
   ones. [specs/legacy/agents-engines-models.md](../specs/legacy/agents-engines-models.md) says it does and changes
   (listed at the end). The picker has no "Default" option: it is the list
   of models, with the agent's default selected on a new chat.

3. **A chosen model that the deployment no longer offers refuses the turn**
   with an error of its own, `ModelNotOfferedError` (409), and not as "not
   found" the way an agent removed from the configuration is
   (`UnknownAgentError`): a model's id is no secret, and a client told "not
   found" about the conversation on its screen has to guess (step 7). No
   silent fall-back to the default: the whole point of the feature is that
   the user knows who is answering. The picker shows the stale id so it can
   be changed. With decision 2 this is
   also what an operator who removes a model from the configuration sees in
   the conversations that were using it, including those that took it as
   their agent's default.

4. **Any configured model may run under any agent's engine.** That is true
   today: a model has no engine, and the provider kinds both engines reach
   are the same two (`anthropic`, `anthropic-compatible`). If that ever
   differs, `GET /api/models` grows an `engines` field and the picker
   filters; nothing else changes. Not built now.

5. **GPT and Gemini reach the platform through OpenRouter**, as the demo
   already does for Claude: the `openai` and `openai-compatible` kinds are
   still excluded by the dependency policy
   ([specs/legacy/agents-engines-models.md](../specs/legacy/agents-engines-models.md), "Known findings"). Model
   selection needs no new provider client. The demo configuration should
   declare two or three models so the picker has something to switch to.

6. **`AgentSummary` gains `model`** (the default's id). Its docstring
   deliberately left the model out because "which model an agent runs is
   the operator's choice"; the picker now needs to name the default, so that
   reason no longer holds. The docstring changes with it.

7. **A model gets an optional `title`** in `[models.*]`, defaulting to its
   id, so the operator can write "GPT 5.5" rather than `gpt-5-5-openrouter`.
   Agents already have one. One optional key in the parser, nothing else.

## The seams, by layer

Everything the model touches today is one line in `Turns._new_run`
(`application/turns.py`): `model=definition.model`. That is where the
override goes. Everything else is carrying the choice to and from that
line.

- **Domain** (`domain/agents.py`, `domain/conversation.py`,
  `domain/errors.py`): `ModelConfig.title`; `ModelsConfig.model_by_id`;
  `Conversation.model: str` checked with `checked_config_id`;
  `UnknownModelError`.
- **Config** (`core/models_config.py`): read `title`. Allowed keys list.
- **Storage** (`datastore/schema.sql`, `datastore/conversations.py`, the
  fake store, the contract suites): `conversations.model text NOT NULL`, in
  the column lists and row mapping; `set_model(conversation_id, model, *,
  now)` on the port beside `rename_conversation`; creation takes the model.
  Schema edited in place, no migration: `SCHEMA_VERSION` stays at 1 and
  `SCHEMA_SHA256` is re-pinned, which is what makes `check_schema` and
  `robinauts db init` refuse a database made from the older file (1d646a4).
  `_check_run_change` keeps agent, engine and model immutable on a run,
  which is exactly right.
- **Application** (`application/turns.py`): `Turns` gets the models
  mapping beside the agents; `begin(...)` takes `model_id: str | None` for
  a new chat and stores it, or the agent's default when it is `None`, on
  the conversation; a `set_model` use case that validates against the
  mapping and writes; `_new_run` takes `conversation.model` and refuses one
  the deployment no longer offers;
  `_produce` hands the engine **`run.model`**, which it ignores today and
  which is what a resumed turn should run on anyway.
- **The agent port** (`ports/agents.py`, both engines, the fake engine, the
  contract suite): `run_turn(agent, history, *, model)`. The engines
  replace `model_for(agent)` with `model_by_id(model)`. Passing a copy of
  the definition with `model` swapped would leave the engines untouched but
  hide the turn's model inside "the agent", which a reviewer would rightly
  question; the explicit argument is two lines per engine.
- **API** (`api/schemas.py`, `api/agent_routes.py`,
  `api/conversation_routes.py`, `api/stream_routes.py`, `api/errors.py`):
  `GET /api/models` → `{items: [{id, title}]}`; `PUT
  /api/conversations/{id}/model` with `{"model_id": str}`, answering the
  `ConversationSummary` like the rename (`PATCH /api/conversations/{id}`)
  does; `NewChatRequest.model_id` optional, the agent's default when
  absent; `ConversationSummary.model`; `AgentSummary.model`;
  `UnknownModelError` → 404 like the agent's on the turn routes and 422 on
  the PUT (step 4; step 7 gives both refusals names and statuses of their
  own). Regenerate `scripts/update-openapi.sh`; the snapshot test pins
  it. The wire routes are outside the snapshot and documented in
  [specs/wire.md](../specs/wire.md).
- **Frontend** (`shell/AgentPicker.tsx` or a sibling `ModelPicker.tsx`,
  `shell/Shell.tsx`, `chat/index.ts`, `chat/assistant-ui/agui/client.ts`,
  `chat/assistant-ui/runtime.tsx`, `api/schema.d.ts` regenerated):
  `useModels()` fetched once by the shell like `useAgents()`; a `<select>`
  of the models by title; on the empty chat it sits beside the agent picker
  and its choice is remembered per browser like the agent's (key `model`,
  validated against the list), falling back to the chosen agent's default
  when nothing valid is remembered; on a conversation it sits on the
  "with Assistant (LangGraph)" line, shows the conversation's model, and a
  change is a `PUT` followed by the history's refresh. A change while a run
  is going is allowed: the run keeps the model it started with and the
  next turn uses the new one. `ChatProps.modelId`
  and `startNewConversation(agentId, modelId, text)` carry it into the first
  message; the continue, edit and regenerate calls change nothing, because
  the server reads the conversation.

## Order of work

One stacked branch per step, reviewed and committed one at a time, as the
POC steps were ([three-agent recipe](poc-progress.md)). Each step leaves
every check green.

1. **Specs and domain.** The spec sentences below; `ModelConfig.title` and
   `model_by_id`; `Conversation.model`; `UnknownModelError`; the parser.
   Tests: `test_models_config.py`, `test_conversation_domain.py`.
   **And the storage that carries `Conversation.model`**, since the field
   is required and every step leaves every check green, Postgres included:
   the `conversations.model` column, `SCHEMA_SHA256` re-pinned, the store's
   column lists and row mapping, and a contract test that a conversation's
   model round-trips through both stores. A new chat takes its agent's
   default (`test_turn_start.py`).
2. **Storage: `set_model`.** On the port beside `rename_conversation`, in
   both stores, with the contract suites. Tests:
   `contracts/conversation_store.py`, `test_fake_conversation_store.py`,
   `test_postgres_conversation_store.py`.
3. **Application and port.** `Turns` with models; `begin(model_id=)`;
   `set_model`; `_new_run` resolving; `_produce` handing `run.model`;
   the port's new argument through both engines, the fake engine and the
   contract suite. Tests: `test_turn_start.py`, `test_turn_lifecycle.py`,
   `test_langgraph_engine.py`, `test_pydantic_ai_engine.py`,
   `test_engine_swap.py`, `contracts/agents.py`, `test_app_composition.py`.
4. **API.** The two new routes, the three schema fields, the error
   mapping, the OpenAPI snapshot, `wire.md`. Tests: `test_stream_routes.py`,
   `test_conversation_routes.py`, `test_api_access.py`,
   `test_openapi_snapshot.py`.
5. **Frontend.** The hook, the picker in both places, the plumbing into the
   first message, the `PUT` on change. Tests: `AgentPicker.test.tsx` (or the
   sibling), `Shell.test.tsx`, `client.test.ts`, `runtime.test.tsx`; the
   fixtures in `src/test/conversations.ts` gain `model`.
6. **Demo.** `demo/robinauts.toml.in` declares a second and third model
   through the same provider (OpenRouter names such as `openai/gpt-5.5`,
   `google/gemini-2.5-pro`) so a switch can be seen working.
7. **A model not offered gets its own error.** Step 1 refused a model no
   longer offered "as not found", and the frontend had to guess what a 404
   was about (the review of #21). `UnknownModelError` leaves `NotFoundError`
   and answers 422 for a model a request names (a new chat, the `PUT`);
   `ModelNotOfferedError`, its subclass, answers 409 for a turn in a
   conversation whose model is gone, decided after ownership so a stranger
   still gets the 404. Both bodies name the class, with a fixed sentence.
   The frontend branches on the name: the guessed sentences, the three-state
   `modelGone` and the `unsaid` action go; a refused edit goes back into its
   own edit box; a new chat refused for its model forgets it. With it, the
   review's other findings: the empty chat's pickers no longer remount on a
   pick, and the fixture server answers as the backend does. Tests:
   `test_turn_start.py`, `test_stream_routes.py`,
   `test_conversation_routes.py`, `test_api_errors.py`, the OpenAPI
   snapshot, `runtime.test.tsx`, `Shell.test.tsx`. The per-answer model
   caption once planned here is not needed, the user decided.

## Spec sentences to change

- [specs/legacy/agents-engines-models.md](../specs/legacy/agents-engines-models.md), "Agents": the agent's model is its
  **default**; a conversation may name another of the operator's models and
  may change it at any point; the change takes effect at the next turn;
  a model the deployment no longer offers refuses the turn. The sentence
  that a change to an agent's model reaches its existing conversations at
  their next turn goes: the default is copied into a conversation when it
  starts (decision 2), so the change reaches new conversations only. "Model
  providers": `title` on a model. The `AgentSummary` reversal (decision 6)
  is in code, not spec.
- [specs/legacy/conversations.md](../specs/legacy/conversations.md): a conversation is
  bound to one agent and has a model, the agent's default at the moment it
  started unless its author picked another, changeable at any point; "What an answer records" already says the model.
- [specs/wire.md](../specs/wire.md): `model_id` on `POST /api/turns`.
- The route list in `api/conversation_routes.py`'s docstring, beside
  `PATCH /api/conversations/{id}`; `backend.md` lists no routes.
- [specs/frontend.md](../specs/frontend.md): the model picker beside the
  agent picker on the empty chat and on the agent line of an open
  conversation.

## Settled

- **The conversation's model is copied at creation**, not NULL for "the
  agent's default" (decision 2, reversed from the first draft).
- **The empty chat remembers the model per browser**, like the agent, and
  falls back to the chosen agent's default.
- **The picker on an open conversation is not refused while a run is
  going**: the run keeps its model and the change applies to the next turn.
