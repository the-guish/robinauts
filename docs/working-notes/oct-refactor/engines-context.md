# Context for the engine blocks

Read this first, then the block's plan. It is written for a session that has not seen
the refactor. The engines are blocks 3 and 4 of `master-plan.md`; both are built the same
way, from the same base, and may run in parallel.

## The shape of the backend now

`backend/src/robinauts/` has three packages: `agent_engines/`, `controller/` and `web/`,
the new layers. `docs/architecture/rules.md`
says who may import whom, and `backend/tests/unit/test_architecture.py` enforces it with
import-linter, so a wrong import fails the unit suite. The two rules that matter here:
an engine imports its own package, `agent_engines.contract`, its framework, the vendors'
SDKs, an MCP client and a database driver, and nothing else of robinauts; and the
third-party libraries are confined by name in `backend/pyproject.toml`.

## The engine contract

`backend/src/robinauts/agent_engines/contract/ports.py` is the port: `AgentEngine` with
`kinds`, `setup`, `create`, `exists`, `stream`, `fork`, `forget`; `EngineSettings` (the
`ModelsConfig` of providers, models and tool servers, a `ProviderKeyLookup` and a
`ToolSecretLookup`, keys asked for by id and never handed over in the open);
`StorageConfig` (kind and options; only `IN_MEMORY` is used in these blocks); the factory
type; and `installed()`, which names the engines this build has. `domain.py` is what
crosses the port as plain data: the configuration records, `AgentDefinition` (a system
prompt and tool server ids, passed at every turn, never stored), the events of a turn
(`TextDelta`, `ReasoningDelta`, `ToolCall`, `ToolResult`, `Done` with the answer and the
new checkpoint id) and the errors (`SessionExistsError`, `SessionNotFoundError`,
`CheckpointNotFoundError`, `UnknownModelError`, `ResumeMismatchError`,
`MissingSecretError`). The meaning of all of it is `docs/specs/agent-engines.md`: read it
whole. The contract is not to be changed by an engine; a field added to a dataclass is
allowed, a new class or a changed signature stops the work and asks the owner.

`agent_engines/echo_engine/engine.py` is a complete engine in ninety lines: no model, one
tool, the contract's memory kept as ids in a dict. It is the model for how an engine
looks and how its tests look (`tests/unit/test_echo_engine.py`). `installed()` in the
contract lists the shipped engines by package and init function; the two framework
engines are listed already, with their classes in place and every operation raising
`NotImplementedError`.

## How an engine is used

The controller builds one engine per name its agents use (`controller/application/
engines.py`), with `EngineSettings` mapped from its configuration and the storage asked.
A turn is `controller/application/turns.py`: it calls `create` once per conversation,
then `stream(session_id, AgentDefinition, prompt, model=<model id>, checkpoint_id=<the
last answer's or None>, timeout_seconds=120.0)`, stores each event numbered, and stores
the answer with `Done.checkpoint_id`. The next turn passes that id back. `forget` is
called when a conversation is deleted. The session id is the conversation id. The
`model` argument is the id in `settings.models.models`, not the vendor's name.

`robinauts start` with `ROBINAUTS_CONFIG` pointing at a TOML (`examples/echo.toml` is the
shape: `model_providers.<id>`, `models.<id>`, `tool_servers.<id>`, `agents.<id>`, engine
names `langchain`, `pydantic-ai`, `echo`) serves the UI; a turn can be watched in the
browser. `docs/working-notes/oct-refactor/config-plan.md` is how that was built.

## Verified facts about the frameworks, at the locked versions

langgraph 1.2.12, langchain 1.4.3, langgraph-checkpoint 4.2.0, langchain-anthropic 1.2,
langchain-openai 1.6.2, langchain-mcp-adapters 0.1, pydantic-ai-slim 2.47.0 with the
`anthropic`, `mcp` and `openai` extras. All are in `backend/uv.lock`; no package is
added, removed or bumped in these blocks, and every dependency passes the licence policy
of `DEPENDENCIES.md` (`langgraph-checkpoint-postgres` does not: psycopg is LGPL).

- LangGraph: with a checkpointer every step writes a checkpoint (three per plain turn,
  five with one tool round); checkpoint ids are time-ordered UUIDs; invoking with an
  earlier `checkpoint_id` in the config continues from it and leaves the later branch
  in place; `update_state` seeds or rewrites a thread; there is no built-in copy of a
  thread. `InMemorySaver` ships with langgraph-checkpoint. `create_agent` takes the
  model, the tools, the system prompt, the middleware and the checkpointer;
  `SummarizationMiddleware` writes its summary into the messages channel.
- Pydantic AI: no persistence of its own. `Agent.run`/`run_stream`/`iter` take
  `message_history`; `all_messages()` is the history after the run; `conversation_id` is
  only a correlation id. The engine keeps its own snapshot of the history per
  checkpoint. `FunctionModel` and `TestModel` script a model; `MCPServerStreamableHTTP`
  is the MCP client; history processors bound the context.

## Rules for the work

- One branch per step, `feature/<engine>-engine-N`, from the previous step's branch; the
  base is `feature/langchain-engine-0` for both blocks. One commit per branch, pushed. No
  pull requests, no reviews between steps. Opus subagents do the steps; the session
  verifies each result before the next.
- Happy path only. Minimal code: what the step names and the next step needs. No
  defensive checks beyond the refusals the contract names. No docstrings unless a line
  cannot be read without one; comments only where the code does not say it. Keep the
  volume small; the echo engine is the measure.
- Nothing leaves the process in the unit suite: no key, no network. A live test runs only
  when its own variable holds a key, and is skipped otherwise, as `tests/live/` does.
- Before each commit, from `backend/`: `uv run --locked ruff check --config
  pyproject.toml . ../scripts ../demo ../examples`, `uv run --locked black --check
  --config pyproject.toml . ../scripts ../demo ../examples`, `uv run --locked pytest
  tests/unit -q`, and `uvx reuse lint` from the root, all green. Every new file starts
  with the two licence header lines. Line length 100.
- Commit messages end, after a blank line, with the attribution lines the session's
  system reminder gives. No Signed-off-by.

## The shared suite both engines pass

`backend/tests/contracts/engine.py`, written by whichever block reaches it first and
reconciled at merge: a suite over the new port that a test file subclasses with a
`new_engine()` and a scripted model. Two halves. The memory half runs over every engine,
echo included: create twice refuses, a turn on an unknown session refuses, a checkpoint
of another session refuses, the next turn continues from a handed-out id, forget is safe to repeat. Fork is
stage two and not in the suite. The turn half runs over the two
framework engines with a script: a streamed answer, a tool round the framework runs
with a plain function tool, a failure raised from the model, a cancellation let
through, a timeout ending the turn with `TimeoutError`.
