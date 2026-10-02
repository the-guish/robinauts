# The LangChain engine: what was verified

Branches `feature/langchain-engine-1` to `-6`, one commit each, on
`feature/langchain-engine-0`.

- A chat model is built for each of the four kinds with the endpoint pinned, the key from
  the settings, retries at zero and the model's timeout and token limit, with nothing
  sent: `test_langchain_engine.py`, the two `*_is_built_from_the_settings` tests.
- The memory half of the shared suite (`tests/contracts/engine.py`) passes over the echo
  engine and over this one on `InMemorySaver`; the turn half passes over a scripted chat
  model and a plain function tool: streamed answer, tool round, model failure,
  cancellation, timeout.
- One turn over the real OpenAI client and the real framework against the scripted Chat
  Completions transport: the request carries the configured endpoint, key, vendor name,
  `max_completion_tokens` and messages; a streamed answer and a tool round come back as
  the contract's events: `test_langchain_engine_over_chat_completions.py`.
- The MCP connection for bearer and basic servers carries the right `Authorization`
  header and the timeout; a `none` server asks for no secret. No real MCP server was
  reached: that is a live test still to write.
- Live, through OpenRouter as an `openai-compatible` provider on `anthropic/claude-haiku-4.5`
  (the key this machine has): `tests/live/test_langchain_live.py::test_one_real_turn_through_openrouter`
  passed. The Anthropic test of the same file skipped: no Anthropic key here.
- In the browser: the shell started from a local TOML on the same OpenRouter model (the
  shape of `examples/langchain.toml`, with the provider swapped) on port 8017, since 8000
  was taken. "Reply with the single word: robinaut" answered "robinaut"; a second turn in
  the same conversation, "What single word did you reply with before?", answered
  "robinaut" from the checkpoint. Two requests to `openrouter.ai/api/v1/chat/completions`
  in the log, one per turn.

## Departures from the plan

- The timeout is one deadline around every `await` on the framework's stream, not
  `asyncio.timeout` around the whole run: a timer around a `yield` would fire inside the
  caller's handling of an event.
- `tools_for` does not prefix tool names with the server id; two servers
  offering a tool of the same name would collide. Left for the stage-two plan.
- The live test has an OpenRouter route beside the Anthropic one, so a machine with only an
  OpenRouter key can run it.

## Known

- `tests/unit/test_conversation_routes.py::test_a_body_nested_deeper_than_it_can_be_read_is_refused`
  fails on this machine before and after this block (a recursion-depth test).
