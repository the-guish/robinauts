# The Pydantic AI engine: what was verified

Branches `feature/pydantic-ai-engine-1` to `-6`, one commit each, on
`feature/pydantic-ai-engine-0` (which carries the LangChain engine and the shared suite).

- A model is built for each of the four kinds on a client of ours with the endpoint
  pinned, the key from the settings, retries at zero, and the timeout and token limit in
  the model settings, with nothing sent: `test_pydanticai_engine.py`, the per-kind tests.
- Both halves of the shared suite (`tests/contracts/engine.py`, unchanged from the
  LangChain block) pass over this engine: the memory half over `TestModel`, the turn half
  over `FunctionModel` and a plain function tool: streamed answer, tool round, model
  failure, cancellation, timeout. The same suite passing over two frameworks is what the
  master plan asked of block 4.
- One turn over the real OpenAI client and the real framework against the scripted Chat
  Completions transport: the request carries the configured endpoint, key, vendor name,
  `max_completion_tokens` and messages; a streamed answer and a tool round come back as
  the contract's events: `test_pydanticai_engine_over_chat_completions.py`.
- The MCP toolset for bearer and basic servers carries the right `Authorization` header
  and the timeouts; a `none` server asks for no secret; an agent gets one toolset per
  server it names. No real MCP server was reached: a live test still to write.
- Live, through OpenRouter as an `openai-compatible` provider on `anthropic/claude-haiku-4.5`:
  `tests/live/test_pydanticai_live.py::test_one_real_turn_through_openrouter` passed. The
  Anthropic test of the same file skipped: no Anthropic key here.
- In the browser: the shell started from a local TOML on the same OpenRouter model with
  `engine = "pydantic-ai"` (the shape of `examples/pydantic-ai.toml`, provider swapped) on
  port 8018. "Reply with the single word: robinaut" answered "robinaut"; a second turn in
  the same conversation, "What single word did you reply with before?", answered
  "robinaut" from the checkpoint's history. Two requests to
  `openrouter.ai/api/v1/chat/completions` in the log, one per turn.

## Departures from the plan

- `pydantic_ai.mcp` at 2.47.0 has no `MCPServerStreamableHTTP`; the MCP client is
  `MCPToolset` (what legacy used), built with the URL, `headers`, `init_timeout` and
  `read_timeout`. A URL ending in `/sse` would make fastmcp pick an SSE transport; not
  guarded.
- The timeout is one deadline around every await on the framework, as in the LangChain
  engine, not `asyncio.timeout` around the whole run.
- `chat_model` returns `(Model, ModelSettings)`: the timeout and `max_tokens` travel with
  the run in this framework, not on the model object.
- `Agent(...)` at 2.47.0 has no `instrument` keyword; tracing is kept off with
  `Agent.instrument_all(False)` and the banner with `pydantic_ai.BANNER_ENABLED = False`.
- The live test has an OpenRouter route beside the Anthropic one, so a machine with only an
  OpenRouter key can run it.

## Noticed, not changed

- On the `-compatible` OpenAI kind the real client sends tool definitions with
  `strict: true` and `additionalProperties: false`, and `tool_choice: "auto"`. Some
  gateways may refuse strict tools; OpenRouter did not for a turn without tools.

## Known

- `tests/unit/test_conversation_routes.py::test_a_body_nested_deeper_than_it_can_be_read_is_refused`
  fails on this machine before and after this block (a legacy recursion-depth test).
