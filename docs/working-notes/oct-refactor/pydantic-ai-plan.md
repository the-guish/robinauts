# Plan: the Pydantic AI engine over in-memory storage

Read `engines-context.md` first; its rules apply. Base branch: `feature/langchain-engine-0`.
Branches `feature/pydantic-ai-engine-N`. Everything lives under
`backend/src/robinauts/agent_engines/pydantic_ai_engine/`, in as few files as read well.

Goal: an agent configured with `engine = "pydantic-ai"` on an Anthropic or OpenAI model
answers in the browser, with tools over MCP, with the memory kept as the engine's own
snapshots, and passes the shared suite.

## Decisions

- The engine keeps the memory itself: per session, a dict of checkpoint id to the
  history (`all_messages()` after the run) as the framework's own message objects. A
  checkpoint id is minted per finished turn. PostgreSQL storage is block 6.
- Provider kinds: `anthropic` and `anthropic-compatible` through `AnthropicModel`,
  `openai` and `openai-compatible` through `OpenAIChatModel`, each with a provider built
  on a client of ours with the endpoint pinned, the key, `base_url` for the compatible
  kinds and retries at zero; `kinds()` answers all four.
- Tools come from `MCPServerStreamableHTTP` toolsets built per turn for the agent's
  servers; the unit suite uses plain function tools; MCP against a real server is a live
  test.
- `resume` runs the turn again from the checkpoint; nothing is kept between attempts.
- Context management is stage two: no history processor in this block.

## Steps

1. Clients. `chat_model(model_id, settings)` builds the model for the provider kind as
   above, with the timeout and `max_output_tokens` from `ModelConfig` in the model
   settings; tracing off. `kinds()` answers the four. Test: a model is built for each
   kind without any request leaving.
2. Sessions. `create`, `exists`, `forget`, `fork` over the snapshot dict: fork copies the
   source's checkpoints up to the one asked, so the source's ids stay valid in the
   target. `stream` refuses an unknown session and a checkpoint the session does not
   hold. Test: the whole memory half of the shared suite.
3. The turn. `Agent(model, instructions=system_prompt, toolsets=...)` run with
   `message_history` from the checkpoint, iterated so that text parts become
   `TextDelta`, thinking parts `ReasoningDelta`, a complete tool call `ToolCall`, a
   tool return `ToolResult`, and the end `Done` with the final text and a new checkpoint
   id under which `all_messages()` is stored. `asyncio.timeout(timeout_seconds)` around
   it. Test: the turn half of the shared suite over `FunctionModel` and a plain function
   tool.
4. Tools over MCP. A `MCPServerStreamableHTTP` per server id in `AgentDefinition.tools`,
   with the URL, the auth header from `settings.tool_secrets.secret_for` and the
   timeout. Test: the toolset built for bearer and basic servers carries the right
   header; no server is reached.
5. Over the wire. One turn over the OpenAI kind through `tests/chat_completions.py`'s
   scripted transport, so the real client and the real framework are exercised with
   nothing leaving the process.
6. Live, opt-in. One real turn on Anthropic when `ROBINAUTS_LIVE_ANTHROPIC_KEY` is set,
   skipped otherwise; `examples/pydantic-ai.toml` with one Anthropic provider, one model
   and one agent on `pydantic-ai`; if the key is in the session, start the shell from it
   and see a turn in the browser, as `config-plan.md`'s step 5 did. A short
   `pydantic-ai-progress.md` records what was verified.
