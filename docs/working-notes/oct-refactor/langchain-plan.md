# Plan: the LangChain engine over in-memory storage

Read `engines-context.md` first; its rules apply. Base branch: `feature/langchain-engine-0`.
Branches `feature/langchain-engine-N`. Everything lives under
`backend/src/robinauts/agent_engines/langchain_engine/`, in as few files as read well.

Goal: an agent configured with `engine = "langchain"` on an Anthropic or OpenAI model
answers in the browser, with tools over MCP, with the memory kept by LangGraph's
`InMemorySaver`, and passes the shared suite.

## Decisions

- The saver is `InMemorySaver` from langgraph-checkpoint. The thread id is the session id.
  The contract's checkpoint id is LangGraph's checkpoint id of the last step of the turn,
  read with `aget_state` after the run. PostgreSQL storage is block 6.
- Provider kinds: `anthropic` and `anthropic-compatible` through `langchain-anthropic`,
  `openai` and `openai-compatible` through `langchain-openai`; `kinds()` answers all four.
- Tools are listed from the agent's MCP servers at every turn through
  `langchain-mcp-adapters`; the unit suite uses plain function tools; MCP against a real
  server is a live test.
- `resume` runs the turn again from the checkpoint; nothing is kept between attempts.

## Steps

1. Clients. `chat_model(model_id, settings)` builds the chat model for the model's
   provider kind with the endpoint pinned, the key from `settings.keys.key_for`, the
   `base_url` for the compatible kinds, retries at zero, the timeout and
   `max_output_tokens` from `ModelConfig`; hosted tracing off. `kinds()` answers the
   four. Test: a model is built for each kind without any request leaving.
2. Sessions. `create`, `exists`, `forget` over a dict of session ids beside the saver;
   `stream` refuses an unknown session and a checkpoint the thread does not hold
   (`aget_state` with the id answers nothing). Test: the memory half of the shared suite,
   fork excepted.
3. The turn. `create_agent` with the chat model, the tools, the system prompt and the
   saver; `astream` with the config's thread id and, when given, the checkpoint id;
   translate: text chunks to `TextDelta`, thinking to `ReasoningDelta`, a complete tool
   call to `ToolCall`, a tool message to `ToolResult`, the end to `Done` with the final
   text and the new checkpoint id. `asyncio.timeout(timeout_seconds)` around it. Test:
   the turn half of the shared suite over a scripted chat model and a plain function
   tool.
4. Tools over MCP. For each server id in `AgentDefinition.tools`, a connection with the
   URL, the auth header from `settings.tool_secrets.secret_for` and the timeout, tools
   listed with `MultiServerMCPClient`. Test: the connection built for bearer and basic
   servers carries the right header; no server is reached.
5. Context. `SummarizationMiddleware` sized from the model's context window, with
   legacy's numbers. Test: a long scripted history is summarised before the model is
   called.
6. Fork. `aget_state` at the checkpoint, `aupdate_state` on the target thread with its
   messages; the target's first checkpoint is new. Test: the fork case of the memory
   half, with the known deviation stated in the test: the source's ids are not yet valid
   in the target.
7. Over the wire. One turn over the OpenAI kind through `tests/chat_completions.py`'s
   scripted transport, so the real client and the real framework are exercised with
   nothing leaving the process.
8. Live, opt-in. One real turn on Anthropic when `ROBINAUTS_LIVE_ANTHROPIC_KEY` is set,
   skipped otherwise; `examples/langchain.toml` with one Anthropic provider, one model and
   one agent on `langchain`; if the key is in the session, start the shell from it and
   see a turn in the browser, as `config-plan.md`'s step 5 did. A short
   `langchain-progress.md` records what was verified.
