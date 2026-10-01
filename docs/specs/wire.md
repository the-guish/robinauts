# The wire between the UI and the backend

The wire is one of the seams: the backend is not shaped by the UI library
([ADR 0001](../adr/0001-chat-ui-assistant-ui-with-tailwind.md)), and
another UI — or no UI — can drive it. It is the one API that every
delivery channel uses ([channels.md](legacy/channels.md)). It is served by
the web shell over the controller ([architecture/web.md](../architecture/web.md),
[architecture/controller.md](../architecture/controller.md)). What the wire
calls a conversation is the controller's session: the wire keeps the word
until the frontend is revisited.

## A chat turn

- Streamed as [AG-UI](https://docs.ag-ui.com) events over **server-sent
  events**, on the same origin. The UI starts a turn with a POST; the
  response is the event stream of the turn it began.
- **The stream is a view of the turn, not the turn.** Closing it changes
  nothing. A conversation has at most one turn going, so **the run id on the
  wire is the conversation's id**: the UI re-attaches to the turn of a
  conversation by that id, giving the last event it saw, and receives what
  it missed and then the rest.
- Loading a conversation returns its messages and, when a turn is going, the
  run id and where to attach (`resume`: the position to attach after, and
  `follows`, the message the turn's answer hangs under). The messages end at
  `follows`: a UI appends what streams in to the end of the list.
- A turn is cancelled by an explicit request.
- The request names the conversation and **either** a new user message with
  the message it hangs under — nothing for the first, the parent of the
  message being replaced for an edit — **or** the assistant message whose
  turn is to be produced again, and then there is no new user message: a
  regeneration answers the question that answer had. **The server loads the
  history from its own store**; it does not accept a history from the
  browser. This makes our wire a profile of AG-UI, not its stock run input,
  and it is documented with the API.
- **The model travels with the turn.** Every turn may name the model it
  runs on, one of those `GET /api/models` lists. Left out, a new conversation
  runs on its agent's default, and a conversation that exists on the model of
  its last message. The model is recorded on the messages the turn produces,
  so a conversation's model is the one its last answer ran on, and the next
  turn may run on another.
- **The web shell emits the events.** The controller yields the platform's
  own turn events, numbered; web maps them to AG-UI. One mapping, whatever
  the engine.
- **The frameworks' AG-UI bridges are not used** — neither
  `ag-ui-langgraph` nor Pydantic AI's `ag-ui` extra. Every turn goes
  through the engine port, the controller and the controller's persistence,
  and the wire is the same whatever the engine.
- **Tool calls are AG-UI's tool events.** A call the model makes, and the
  result that answers it, are parts of the assistant message that made the
  call: the message holds its calls, their results and its text, in order.
  A call is sent as `TOOL_CALL_START` (the call's id, the tool's name, the
  assistant message it belongs to), `TOOL_CALL_ARGS` (its arguments as JSON
  text, in one piece, since the engine hands them over whole once it is
  about to run the call; a client built for arguments that stream reads one
  piece as it reads several) and `TOOL_CALL_END`; its result is sent as
  `TOOL_CALL_RESULT` as it lands, naming the call it answers. The call's id
  is the one stored on the part — the vendor's, carried by the engine as
  data — so that a result, a re-attach and the conversation loaded
  afterwards all name one call one way. A result that is an error says so
  as `metadata: {"isError": true}` on its `TOOL_CALL_RESULT` — AG-UI 1.0
  has no field for it there, and this flag, a boolean of ours, is the one
  thing this wire ever puts in `metadata`; a plain result carries none.
  **Arguments and results are rendered as data** by the client — text,
  never Markdown-with-HTML, never a URL turned into a link without the CSP
  in mind — because both are attacker-influenced text.

## The endpoints

The profile above, as it is served. The three streaming ones are **outside
the OpenAPI document** — a streaming endpoint described in it would have a
generated client believe it could read the response as JSON — so they are
documented here, which is what "documented with the API" means for them.

| method and path | body | answers |
|---|---|---|
| `POST /api/turns` | `{"agent_id": str, "model_id": str\|null, "text": str}` | the stream of the turn answering the first question of a **new** conversation |
| `POST /api/conversations/{id}/turns` | `{"text": str, "parent_id": uuid\|null, "model_id": str\|null}` **or** `{"regenerate": uuid, "model_id": str\|null}` | the stream of the turn it began |
| `GET /api/runs/{run_id}/events?after=<position>` | — | the stream of that turn from `after`; `Last-Event-ID` says the same thing, and is read when `after` is absent |
| `POST /api/conversations/{id}/runs/{run_id}/cancel` | — | 204; the turn ends as cancelled |

- `parent_id` is the message the new one hangs under — nothing for a
  conversation's first question, the parent of the message being replaced
  for an edit. `regenerate` is the assistant message to produce again; the
  turn answers that answer's question.
- Every stream carries `Content-Type: text/event-stream`,
  `Cache-Control: no-store`, `X-Accel-Buffering: no`, and
  `X-Robinauts-Run-Id` and `X-Robinauts-Conversation-Id` — the same value,
  the conversation's id — so that a client which received the headers and
  nothing else can re-attach.
- **An `id: <position>` is the platform's own numbering of the turn's
  events**, and it is what makes `Last-Event-ID` re-attaching native. The
  numbering starts again at 1 with each turn. One wire event can be
  **derived** from another — the brackets around thinking are — and the
  platform numbered none of those: the id goes on the **last** wire event
  derived from each of the turn's events, and an event without one is never
  something to re-attach after. `RUN_STARTED` opens every stream and carries
  no id. So an id means "everything derived from the turn's events up to
  this position has been sent", and a client that re-attaches at the last id
  it saw is replayed no event it has had in full, loses none it has not, and
  has the brackets of the one it is in the middle of derived again.
- The events are AG-UI's: `RUN_STARTED`, `TEXT_MESSAGE_START` /
  `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END`, the `REASONING_MESSAGE_*`
  events for thinking — under an id derived from the answer's, and never
  stored — the `TOOL_CALL_*` events above, and `RUN_FINISHED` or
  `RUN_ERROR`. A completed message is a bare `TEXT_MESSAGE_END`: the client
  built it from the deltas, and one that did not receive every delta reloads
  the conversation, which is what the store is for.
- **Thinking is bracketed.** Each stretch of thinking inside an answer is a
  reasoning message of its own, under an id derived from the answer's and
  from the position it opened at, so a turn that thinks twice does not
  reopen a message it has ended.
- **What re-attaching may repeat is the bracket around the cut and the
  ending**, and only those: a `*_START` for a message the client already
  holds open, a `*_END` for one it does not, and the terminal event of a
  turn it has already seen end. **A client reads all three as no-ops.**
  Everything else is sent once: no delta is repeated and none is lost.
- **A cancellation is not a failure.** A turn somebody stopped is
  `RUN_FINISHED` with AG-UI's `cancelled` outcome — "stopped before it
  completed, by whoever was running it, and did not fail" — and not a
  `RUN_ERROR`, which a stock client shows as something having gone wrong.
- `RUN_ERROR` carries a `code`: the turn's state where it ended in an error
  (`failed`, or `interrupted` — the deployment stopped with the turn in it,
  which is not AG-UI's *interrupt* outcome). Its message is **a fixed
  sentence** — never the turn's stored error, which is written for an
  operator.

**Not yet served.** The wire is the happy path today
([working-notes/echo-e2e-plan.md](../working-notes/echo-e2e-plan.md)); these
hold as the rule and are not enforced yet:

- A body that is neither shape of a turn, or both at once, is refused (422);
  so is a field the body does not know. An agent the deployment has not got
  is 404; a model it does not offer is 422, `UnknownModelError`.
- A conversation with a turn going refuses a second turn (409). A turn that
  is not there and one in somebody else's conversation answer the same 404,
  and **before the stream begins**: a refusal is a status.
- `Last-Event-ID` and `after` saying two different things is refused (422).
- A comment line (`: keep-alive`) goes out while nothing is arriving, so
  that nothing in front of the deployment closes a quiet stream.
- **Every stream ends with an event saying the turn is over.** Re-attaching
  at or past the last position of a turn that has ended is answered with
  **how that turn really ended**, read from its record. A position a turn
  that is still going has not reached is refused (422).
- A reload after a turn that failed or was cancelled says so
  (`ended_badly` on the opened conversation).
- A body is bounded at one mebibyte, refused with 413 on the declared length
  before a byte of it is read.

## Without streaming

- A client that cannot stream starts a turn and obtains the result once the
  turn has finished. Planned ([channels.md](legacy/channels.md)).

## Everything else

- Conversations, projects, sharing, session, audit: a plain JSON API,
  described by OpenAPI. The OpenAPI document is committed as a snapshot,
  and the frontend's typed client is generated from it. The agents and the
  models a turn can run on are `GET /api/agents` and `GET /api/models`.
- A conversation has no model of its own: the model travels with each turn,
  above. `PUT /api/conversations/{id}/model` answers the choice back so that
  a client can show it and send it with the next turn; it stores nothing.
- `POST /api/conversations/{id}/fork` makes a new conversation from a message
  of another ([architecture/controller.md](../architecture/controller.md)).
  Planned: it answers 501 until the controller forks.

## Details likely to change

State of the packages, checked again on 2026-09-22 (publication dates from
`npm view <pkg> time --json`; the frontend pins the newest version published
at least ten days before that day, `contributing/js-dependencies.md`):

| package | version | licence | note |
|---|---|---|---|
| `ag-ui-protocol` (PyPI) | 1.0.0 | MIT | event types and encoder; depends on pydantic only. Imported only in `web` |
| `@ag-ui/core`, `@ag-ui/client` (npm) | 1.0.0, published 2026-09-17 | MIT | |
| `@assistant-ui/react-ag-ui` (npm) | 0.0.60, published 2026-09-18 | MIT | **not used.** Still pinned to `@ag-ui/client ^0.0.59`, as is 0.0.59 (2026-09-11), which is what the cooldown allows |
| `@assistant-ui/react` (npm) | 0.15.19, published 2026-09-11 — **pinned**, step 20 | MIT | 0.15.21 (2026-09-18) is newer than the cooldown allows |

- The protocol is at 1.0. The immature piece is assistant-ui's bridge, and
  it sits inside `src/chat/assistant-ui/`, the place that is cheapest to
  replace. If it proves too immature, a small AG-UI client of our own
  takes its place behind the same seam; the wire does not change.
- A month on, the bridge has not caught up: it would install a second,
  pre-1.0 `@ag-ui/client` beside the 1.0 one. **Step 21 decided: the client
  is ours** (`frontend/src/chat/assistant-ui/agui/`, 2026-09-22), and the
  bridge is not taken. It is about two hundred lines over `fetch` and a
  `ReadableStream` -- the format, the event vocabulary this backend emits,
  and re-attaching with `Last-Event-ID` -- and it added **no dependency at
  all**, where the bridge would have added two, one of them a pre-1.0 copy
  of a protocol that is at 1.0. Nothing about the wire turned on the answer,
  which is what the seam was for; if the bridge catches up, taking it is a
  change inside `src/chat/assistant-ui/` and nowhere else.
- What the client does **not** do, because nothing needs it yet: AG-UI's
  `STATE_*` and `STEP_*` events, and the `RunAgentInput` a stock AG-UI
  server is handed -- the profile is ours and the history comes from the
  store. It decodes the four tool events, and an older client that does not
  ignores them, since unknown event types were always ignored.
- The copy of the styled components is a release behind the registry for the
  same reason — the registry serves files written against the newest
  library, and the cooldown pins the one before it. What that cost is one
  optional field, written down in
  `frontend/src/chat/assistant-ui/vendor/README.md`.
- Same-origin SSE passes the Content-Security-Policy
  (`connect-src 'self'`).
