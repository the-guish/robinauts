# Stage two: hardening

The running plan for stage two of `master-plan.md`. Stage one's last step completes it
with every learning still held in legacy before legacy goes; until then, each block of
stage one adds what it found. Items are grouped by what they touch: the features that
cross components first, then one section per component.

## Cross-component features

- **Fork.** `fork_conversation` in the controller: a new conversation with the records
  up to the message asked, and the engine's `fork` at that answer's checkpoint. In the
  engines: Pydantic AI copies its snapshot rows up to the checkpoint, so the source's ids
  stay valid in the target as the spec says; LangChain seeds the target thread with
  `aupdate_state` from `aget_state` at the checkpoint, and the source's ids are not valid
  in the target unless the saver's rows are copied, which the PostgreSQL saver can do and
  `InMemorySaver` cannot without reaching inside it. Decide which the spec keeps.
- **Context management.** Each engine keeps the history within the model's window by its
  own means: `SummarizationMiddleware` on LangChain, a history processor on Pydantic AI,
  sized from `ModelConfig.context_window`, with legacy's numbers (`SUMMARIZE_AT`,
  `KEEP_MESSAGES`, `TRIM_AT`, `CHARS_PER_TOKEN`, `DEFAULT_CONTEXT_WINDOW`). The vendor's
  prompt cache placed on the system prompt. Any middleware or processor writes updates of
  its own, which the engines' event translation must tolerate (see LangChain below).
- **Resume.** The spec's `resume=True`: an interrupted turn run again from the engine's
  partial work without repeating tool calls. Both engines run the turn again from the
  checkpoint today. Needs the controller to mark a turn interrupted at start-up (the
  sweep) and to pass `resume` when the same question is asked again.
- **Housekeeping.** Trash expiry and retention in the controller, on the process's own
  schedule, each calling the engine's `forget`, which is the only way memory is deleted.
- **Titles.** A conversation's title from its first exchange: legacy derived it in `core`;
  the spec says ask the model, which needs a sessionless call the engine contract does
  not have. Decide, then add the operation or keep the derivation.
- **Tool names across servers.** Both engines list tools from several MCP servers; a tool
  of the same name on two servers collides. Legacy prefixed `<server id>_<tool>` in both
  frameworks. Restore in both engines, and decide whether the prefix is shown in the UI.
- **A tool result the server marks as an error** becomes a message the model reads, not a
  failure of the turn, in both engines (`handle_tool_errors` / `tool_error_behavior`).
- **Usage and cost**, attachments, and tool approval: out of the engine contract today;
  planned, not specified.

## langchain_engine

Findings of the review of `feature/langchain-engine-6`, none blocking.

- `events_of` reads `update["messages"]` for every node update. With `create_agent` and no
  middleware both nodes always carry messages; any middleware, including the context
  block above, writes updates without them and the line raises inside a turn. Read it
  with a `get`.
- Tool names are not prefixed by server id (cross-component above).
- `Done` takes the thread's latest state after a run started from an earlier checkpoint.
  Correct today because LangGraph's latest is the newest write; it is the one place the
  engine relies on "the latest" rather than on an id of its own. Comment it, and test two
  turns continued from the same earlier checkpoint.
- `force_tracing_off` pops two LangChain variables from the process environment at
  construction. Legacy did the same; keep the one comment that says why.
- Sessions are a set beside the saver; `create` and `exists` never ask the saver. Fine in
  memory; the PostgreSQL saver should be the one source of which threads exist.
- A live test against a real MCP server is still to write; the unit tests prove only
  the connection's header and timeout.
- Keep the timeout as written: one deadline around each `await` on the stream, with
  `aclosing` releasing it. The plan's `asyncio.timeout` around the whole run would fire
  inside the caller's handling of a yielded event.
- The shared suite `tests/contracts/engine.py` is the one description both engines are
  held to; extend it rather than each engine's own tests.

## pydantic_ai_engine

To be filled by the review of its block. Known from the plan:

- Copy the LangChain engine's timeout shape and subclass the shared suite as written.
- The snapshot dict is the engine's own memory; `fork` by copying rows is cheap here
  (cross-component above).
- A live test against a real MCP server, as for LangChain.

## controller

From the echo and config blocks, outside the happy path:

- The refusals the contract names: ownership (a conversation that is not the user's is
  not found), not found, unknown agent or model, an invalid title or cursor, empty text.
- One active turn per conversation (`TurnActiveError`), the turn timeout, a bounded
  cancel, and the sweep at start that ends turns a dead process left active as
  interrupted.
- A cancel that lands before the runner starts leaves the turn active with no
  `TurnEnded`.
- A rename during a turn is overwritten when the turn ends: the runner writes back the
  conversation as it was when the turn started.
- An edit of a conversation's first question has no parent; `send_message` requires one.
- Paging: `list_conversations` answers a plain slice with no cursor.
- The visible thread after edits: the path to the newest message; the tree rules legacy
  kept in `core` (`ConversationTree`) say what may follow what.
- `parse_config`: a table or entry that is not a TOML table raises a plain error, not
  `ConfigError`; missing fields are reported in the dataclass's own words; URLs, env
  names, bounds and text are not validated.
- Event positions restart at 1 per turn and the last turn's events stay until the next
  turn starts; `watch_turn` relies on it. Decide whether positions become per
  conversation with PostgreSQL.
- One `asyncio.Condition` for the whole in-memory store wakes every watcher on every
  append.
- The user's id is a fresh uuid on each start with in-memory storage.

## web

From the echo and config blocks, and the wire's "not yet served" list:

- Request protection: CSRF and origin checks on writes, the one-mebibyte body bound, the
  security headers and the Content-Security-Policy on the served UI, hashed asset caching.
- The error mapping to statuses and fixed bodies, with the test that every error class
  of the controller's contract has a status; log redaction of keys and secrets.
- The wire: a body that is neither shape of a turn or both is refused (422); a second
  turn while one runs (409); `Last-Event-ID` and `after` disagreeing (422); the keep-alive
  comment on a quiet stream; the terminal event on a re-attach at or past the end of a
  turn that ended; a position a running turn has not reached (422); `ended_badly` on the
  opened conversation after a failed or cancelled turn; reasoning brackets rebuilt on a
  re-attach.
- The set-model route stores nothing; decide whether it stays.
- `AgentSummary.engine` and `ProvenanceView.engine` are always empty strings.
- The frontend's `folded()` accepts a tool result inside the answer and in a separate
  tool message; `docs/specs/legacy/conversations.md` still describes the latter.
- `robinauts db init` and the other legacy subcommands are gone from the console script
  until the PostgreSQL block adds `db init` back.

## Packaging and operations

- The wheel hook, the demo (`demo/robinauts.toml.in` uses `mcp_servers` and
  `engine = "langgraph"`), the deployment rehearsal and the CI scripts, all pointing at
  legacy paths or legacy subcommands.
- `scripts/check-frontend.sh` refuses Node 22; `.nvmrc` asks for 24. One frontend timing
  test in `sse.test.ts` fails on slow machines.
