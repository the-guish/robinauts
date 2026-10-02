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
  checkpoint today. Needs a turn marked interrupted, which block 5's lease does, and
  the controller to pass `resume` when the same question is asked again.
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

## Data model and PostgreSQL

Findings of the review of `design/data-model` (blocks 5 and 6), numbered as the review
ranked them. Findings 1 to 7 were answered in `data-model.md`, the path of one message,
`data-model-plan.md` and `postgres-plan.md`; these are the rest.

8. **The path doc and the lease.** `the-path-of-one-message.md` says it describes the
   controller after block 5, but its lease tick, its `request_cancel` through the store
   and its runner-dies step are stage two: mark them so, or move them here. Add
   `cancel_requested_at` to the `Turn` record when cancel goes through the store. Until
   then, with several processes, a cancel that lands on a process not running the turn
   returns 204 and cancels nothing; when it does go through the store, the store
   checks the owner before it writes the flag.
9. **Expiry enforced.** The sweep deletes `turn_events` past `expires_at`, one statement
   on `turn_events_expires_at_idx`, and every read treats an event past `expires_at`
   as absent, whatever the store's deletion lag: DynamoDB deletes "typically within a
   few days" and returns the item from a `Query` until then, Firestore within about a
   day, Cosmos hides it at once. Correct `aws-serverless.md`'s "TTL deletes expired
   events, user sessions and pending logins without code": a sign-in read checks
   expiry itself. Give all of a turn's events one `expires_at`, the turn's start plus
   the retention, so that a late replay never starts in the middle of a turn. Say in
   `wire.md` that reasoning is kept as events until they expire, not "never stored".
10. **The document's bytes and its version.** `data-model.md` says every store keeps the
    same bytes; `jsonb` reorders keys, drops whitespace and re-renders numbers, so say
    "the same document, equal after decoding", and align `aws-serverless.md`'s "JSON
    text, not a map" with the port's `Mapping`. Decide what a reader does with a newer
    document during a rolling deploy, a minor version it reads past and a major one it
    refuses, since "a new key moves the version" makes every new message unreadable to
    the old process, and refusing one message breaks the thread walk.
    `messages_role_is_a_role` is a CHECK, so a new role is a schema change, not an
    additive one: say so, or drop the CHECK. The schema hash does not cover the
    document format.
11. **An index on `turns (session_id, follows)`.** Without it the cascade from `sessions`
    checks `turns_follows_fkey` once per message by a range scan of the session's
    turns: measured at 55 ms for 2,000 messages, 718 ms for 8,000, and 87 ms with the
    index. It also serves the turns of a question. One line in `schema.sql`, which its
    own rule for the parent key asks for.
12. **The path doc's SQL.** Its `INSERT INTO turn_events` omits `expires_at`, which is
    `NOT NULL`; its `start_turn` checks no `owner_id`, though `data-model.md` says
    PostgreSQL checks the rest of the address; a deleted session raises
    `TurnActiveError` (409) there and is not found (404) in `data-model.md`, the schema
    and the wire; `NOTIFY '<turn> end'` names no channel; `start_turn(question, turn)`
    is not the plan's signature. Fix the doc, and name `SessionNotFoundError` for a
    hidden session in the plan's `start_turn`.
13. **Decisions that reverse a spec or an ADR, unrecorded.** `core.md` says no framework
    checkpointer and no framework tables in the deployment's schema, and ADR 0005 keeps
    memory in `runs.engine_state`; block 6 builds engine-owned checkpoint tables, and
    `data-model.md` cites ADR 0005 as its authority. ADR 0005 says the transcript shows
    a cancelled turn's tool calls; the model stores no message for one, so a call with
    side effects survives only in events that expire. ADR 0005's checkpoint rule, the
    nearest finished answer on the path above the question, is dropped: a turn under a
    question (after a failed turn, or on an edited first question) passes
    `checkpoint_id=None`, which LangChain reads as the thread's latest and Pydantic AI
    as an empty history. Write each decision down, in the doc or a superseding ADR,
    and state the checkpoint rule.
14. **Tree and turn invariants, enforced and owned.** A self-parent and a two-node
    cycle pass the composite key, which is checked at the end of the statement: add
    `CHECK (parent_id <> id)` and make the thread walk cycle-safe. "A turn produces at
    most one answer" is held by nothing: `turns.answer_id UNIQUE`, with a key to
    `messages (session_id, parent_id, id)`, holds it at no extra index. "`follows` is a
    user message" is held by nothing, and the schema's comment that a key cannot say
    so is wrong: a constant `follows_role` column with a composite key does. Decide
    whether the store or the controller owns these rules (the schema says the store,
    `aws-serverless.md` the controller), list them under the rules every store keeps,
    and test them in the contract suite. Decide the visible thread while a
    regeneration runs ("the newest message" against the wire's "messages end at
    `follows`") and after a failed turn, and let `regenerate` work on a question whose
    only turn failed. Editing the first question makes a second root: the tree is a
    forest, and `data-model.md` should say so.
15. **Opening a session is one snapshot.** Messages and the active turn are read
    separately, so an answer can show twice, or be missing with no run id; the frontend
    assumes a snapshot. Read both in one transaction, or the active turn first and
    the messages up to it.
16. **Cascades, and what keeps the schema's claims.** No port operation deletes a user,
    so that deletion depends on the cascade alone, against the schema's header, and
    the purge relies on the cascade for messages, turns and events. On DynamoDB the
    sparse listing index hides trashed sessions, and the sweeps for hidden sessions
    and expired leases need a `Scan` or an index. `schema.sql` says, in the present
    tense, that the server refuses a wrong hash, and nothing keeps it: port legacy's
    schema tests (one row, the hash, a half-applied file, four `db init` at once) and
    the advisory lock `create_schema` needs against concurrent runs.
17. **Where deferred work lives.** `master-plan.md` and this plan listed "the sweep at
    start", which with two processes on one database would interrupt live turns; the
    lease replaces it (`aws-serverless.md`), and both now say so. `controller.md`'s
    `open` and `close`, and `web.md`'s `cancel_turn` and `watch_turn`, describe the old
    lifecycle until block 5's step 6 rewrites them. Block 5's steps 1, 3 and 4 cannot
    each leave the suite green as written: no turn id exists until step 3, web has
    none until step 6, and step 3's port forces step 4's runner; the plan says so, or
    regroups them.
18. **One name for each operation, and one address.** `end_turn` in `schema.sql`'s
    comments and the three serverless notes, against the plan's `finish_turn`;
    `request_cancel` in the path doc, in no port; `delete_session(session, at)`
    against `hide_session(owner, session, at)`; `start_turn(question, turn)` against
    `start_turn(owner, turn, question)`. Turn operations take two ids in the path doc
    and the AWS and GCP notes, three in `data-model.md` and the plan, and the turn id
    alone in the AWS sketch's `events_after` and `end_turn`, which cannot find `EVT#`
    items under `PK = SESSION#` without an index. `azure-serverless.md` says PostgreSQL
    may ignore the owner, and `data-model.md` says it checks it. The AWS note keys
    messages and turns by `(session_id, id)` and omits `turns.model`.
19. **The wire with turn ids.** `agui.py` sets `thread_id = run_id`; once the run id is
    the turn's, say in the planned section that `thread_id` is the conversation's. Say
    what "two headers carrying two values" means on the wire. `watched()` waits for
    the first event before it sends the headers, and CloudFront gives up after 30 s on
    a cold or lost worker: send the headers first, or bound the wait. "A session that
    is not its caller's owner's is not found" meets privacy.md's share links and
    projects; say which wins.
20. **`position` as `bigint`**, as legacy had it, and `after` bounded at the edge: with
    an `int4` column asyncpg raises on `?after=3000000000` or a large `Last-Event-ID`,
    a 500 where an empty replay or a 422 is due.

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
  cancel, the lease renewed for long turns, and the sweep for turns a dead process
  left running that nobody reads (block 5 ends the ones a reader finds, by the lease).
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
  turn starts; `watch_turn` relies on it. Decided in block 5: positions are per turn,
  keyed by the turn's id.
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
