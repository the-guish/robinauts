# The wire between the UI and the backend

The wire is one of the seams: the backend is not shaped by the UI library
([ADR 0001](../adr/0001-chat-ui-assistant-ui-with-tailwind.md)), and
another UI — or no UI — can drive it. It is the one API that every
delivery channel uses ([channels.md](channels.md)).

## A chat turn

- Streamed as [AG-UI](https://docs.ag-ui.com) events over **server-sent
  events**, on the same origin. The UI starts a turn with a POST; the
  response is the event stream of the run it created.
- **The stream is a view of the run, not the run** ([runs.md](runs.md)).
  Closing it changes nothing. The UI re-attaches to an active run by its
  id, giving the last event it saw, and receives what it missed and then
  the rest.
- Loading a conversation returns its messages and, when a run is active,
  that run's id and the position to attach after (`resume_point`,
  [runs.md](runs.md)) — so a UI that has just loaded every complete message
  attaches without being shown any of them twice. With a run active, the
  messages end at `resume.follows`, which is what the run's next message
  hangs under: a UI appends what streams in to the end of the list.
- A POST to a conversation that has an active run is refused. A run is
  cancelled by an explicit request.
- The request names the conversation and **either** a new user message with
  the message it hangs under — nothing for the first, the parent of the
  message being replaced for an edit — **or** the assistant message whose
  turn is to be produced again, and then there is no new user message: a
  regeneration answers the question that turn already had
  ([conversations.md](conversations.md)). **The server loads the history
  from its own store**; it does not accept a history from the browser. This
  makes our wire a profile of AG-UI, not its stock run input, and it is
  documented with the API.
- **The `api` layer emits the events.** The application yields the
  platform's own turn events; `api` maps them to AG-UI. One mapping,
  shared by every runtime. The events have a written form of their own —
  versioned like a message, and the same one the events table keeps
  ([runs.md](runs.md)) — so what is stored and what is sent cannot drift
  apart.
- **The framework's AG-UI bridge is not used** — Pydantic AI's `ag-ui`
  extra stays out. Every turn goes through the runtime port, the controller
  and the platform's persistence, and the wire is the platform's whatever
  the runtime.
- **Tool calls are AG-UI's tool events.** A call the model makes is part of
  the assistant message that made it ([conversations.md](conversations.md)),
  and is sent as `TOOL_CALL_START` (the call's id, the tool's full name, the
  assistant message it belongs to), `TOOL_CALL_ARGS` (its arguments as JSON text, in pieces when the runtime streams them and whole when it does not) and `TOOL_CALL_END`; each result of the tool message
  that answers the batch is sent as `TOOL_CALL_RESULT` as it lands, naming
  the call it answers. The call's id is the one stored on the part — the
  vendor's, carried by the engine as data ([agents.md](agents.md)) — so
  that a result, a re-attach and the conversation loaded afterwards all
  name one call one way. A result that is an error says so as
  `metadata: {"isError": true}` on its `TOOL_CALL_RESULT` — AG-UI 1.0 has no
  field for it there, and this flag, a boolean of ours, is the one thing
  this wire ever puts in `metadata`; a plain result carries none. The
  re-attach rules below hold for these as for text: a `*_START` for a call
  the client already holds open and a `*_END` for one it does not are
  no-ops, and no delta and no result is repeated or lost. **Arguments and
  results are rendered as data** by the client — text, never
  Markdown-with-HTML, never a URL turned into a link without the CSP in mind
  — because both are attacker-influenced text.

## The endpoints

The profile above, as it is served. These three are **outside the OpenAPI
document** ([backend.md](backend.md)) — a streaming endpoint described in it
would have a generated client believe it could read the response as JSON — so
they are documented here, which is what "documented with the API" means for
them. All three need a session; the two writes are held to the same origin
checks as every other write.

| method and path | body | answers |
|---|---|---|
| `POST /api/turns` | `{"agent_id": str, "model_id": str\|null, "text": str}` | the stream of the run answering the first question of a **new** conversation |
| `POST /api/conversations/{id}/turns` | `{"text": str, "parent_id": uuid\|null}` **or** `{"regenerate": uuid}` | the stream of the run that turn began |
| `GET /api/runs/{run_id}/events?after=<position>` | — | the stream of that run from `after`; `Last-Event-ID` says the same thing, and is what is read when `after` is absent |

- A body that is neither shape of a turn, or both at once, is refused (422);
  so is a field the body does not know. `parent_id` is the message the new one
  hangs under — nothing for a conversation's first question, the parent of the
  message being replaced for an edit.
- `model_id` is the model a new conversation runs on, one of those
  `GET /api/models` lists; left out or `null`, it is the agent's default
  ([agents.md](agents.md)). An agent the deployment has not got is 404, and
  is looked for first; a model it does not offer is 422, `UnknownModelError`.
  A turn in a conversation that exists names no model — it runs on the
  conversation's, which `PUT /api/conversations/{id}/model` changes — so a
  `model_id` there is a field that body does not know (422). A conversation
  whose model the deployment no longer offers refuses the turn — a question,
  an edit or a regeneration — with 409, `ModelNotOfferedError`, decided after
  the conversation was found to be the caller's: somebody else's is the 404
  of one that is not there. Both bodies are a fixed sentence and never the
  model's id.
- A conversation with a run going refuses a second turn (409). A run that is
  not there and one in somebody else's conversation answer the same 404, and
  **before the stream begins**: a refusal is a status.
- Every response carries `Content-Type: text/event-stream`,
  `Cache-Control: no-store`, `X-Accel-Buffering: no`, and
  `X-Robinauts-Run-Id` and `X-Robinauts-Conversation-Id`, so that a client
  which received the headers and nothing else can re-attach.
- **`Last-Event-ID` is always read**, even when `after` is given: the two are
  two ways of saying one thing, and saying two different ones is refused (422)
  rather than settled by preferring one of them. An empty one is a client that
  has seen nothing.
- **An `id: <position>` is the platform's own numbering of the run's events**,
  and it is what makes `Last-Event-ID` re-attaching native. One wire event can
  be **derived** from another — the brackets around thinking are — and the
  platform numbered none of those: the id goes on the **last** wire event
  derived from each of the run's events, and an event without one is never
  something to re-attach after. So an id means "everything derived from the
  run's events up to this position has been sent", and a client that
  re-attaches at the last id it saw is replayed no event it has had in full,
  loses none it has not, and has the brackets of the one it is in the middle
  of derived again.
- A comment line (`: keep-alive`) goes out while nothing is arriving, so that
  nothing in front of the deployment closes a quiet stream.
- The events are AG-UI's: `RUN_STARTED`, `TEXT_MESSAGE_START` /
  `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END`, the `REASONING_MESSAGE_*`
  events for thinking — under an id derived from the answer's, and never
  stored ([conversations.md](conversations.md)) — the `TOOL_CALL_*` events
  above, and `RUN_FINISHED` or `RUN_ERROR`. A completed message is a bare
  `TEXT_MESSAGE_END`: the client built it from the deltas, and one that did
  not receive every delta reloads the conversation, which is what the store
  is for. A tool message, announced or completed, sends nothing of its own:
  its results went out one by one, and the client holds them against their
  calls.
- **Thinking is bracketed, and the brackets survive a re-attach.** Each
  stretch of thinking inside an answer is a reasoning message of its own,
  under an id derived from the answer's and from the position it opened at, so
  a turn that thinks twice does not reopen a message it has ended. A stream
  that carries on from a position is given the run's events up to that
  position first, so it derives the brackets exactly as an unbroken one would:
  re-attaching in the middle of a stretch closes that stretch rather than
  leaving the client's thinking block open for ever.
- **What re-attaching may repeat is the bracket around the cut and the
  ending**, and only those: a `*_START` for a message the client already holds
  open, a `*_END` for one it does not, and — re-attaching at or past the last
  position — the terminal event of a run it has already seen end. **A client
  reads all three as no-ops.** Everything else is sent once: no delta is
  repeated and none is lost.
- **A position a run that is still going has not reached is refused** (422):
  there is nothing after it, and a stream that waited would give up on a run
  that is answering perfectly well ([runs.md](runs.md)). A run that has
  **ended** is not refused past its end — there is nothing to wait for, and
  how it ended is the answer.
- **Every stream ends with an event saying the run is over**, because a
  stream that merely closed is one an `EventSource` would open again.
  Re-attaching at or past the last position of a run that has ended sends
  nothing — there is nothing after `after` ([runs.md](runs.md)) — and is
  answered with **how that run really ended**, read from its record: the same
  `RUN_FINISHED` or `RUN_ERROR` a client would have been sent had it been
  watching. A finished answer is never reported as an error.
- **A cancellation is not a failure.** A run somebody stopped is
  `RUN_FINISHED` with AG-UI's `cancelled` outcome — "stopped before it
  completed, by whoever was running it, and did not fail" — and not a
  `RUN_ERROR`, which a stock client shows as something having gone wrong.
- `RUN_ERROR` carries a `code`: the run's state where it ended in an error
  (`failed`, or `interrupted` — the deployment stopped with the run in it,
  which is not AG-UI's *interrupt* outcome), `quiet` where watching a run that
  stored nothing was given up on ([runs.md](runs.md)), `gone` where the run is
  **no longer there at all** — its conversation deleted under the watcher —
  and `internal` for a fault of ours. Its message is **a fixed sentence** —
  never the run's stored error, which is written for an operator.
- **A body is bounded** at one mebibyte, refused with 413 on the declared
  length before a byte of it is read, and on the bytes themselves as they
  arrive where a body carried no length — before the request reaches anything
  that would parse it or ask who is sending it. That is the bound on a
  *request*, not on a record:
  the conversation format holds a message far longer, and a turn that needs
  more than a mebibyte is a file, which is a channel this version has not got
  ([channels.md](channels.md)).

## Without streaming

- A client that cannot stream starts a run and obtains the result once the
  run has finished. Planned ([channels.md](channels.md)).

## Everything else

- Conversations, projects, sharing, session, audit: a plain JSON API,
  described by OpenAPI. The OpenAPI document is committed as a snapshot,
  and the frontend's typed client is generated from it. The agents and the
  models a conversation can be started with are `GET /api/agents` and
  `GET /api/models`; a conversation's model is changed with
  `PUT /api/conversations/{id}/model`, which refuses a model the deployment
  does not offer with 422 `UnknownModelError` (a fixed detail, checked
  before the conversation) and is allowed while a run is going
  — the run keeps the model it started with.

## Details likely to change

State of the packages, checked again on 2026-09-22 (publication dates from
`npm view <pkg> time --json`; the frontend pins the newest version published
at least ten days before that day, `contributing/js-dependencies.md`):

| package | version | licence | note |
|---|---|---|---|
| `ag-ui-protocol` (PyPI) | 1.0.0 | MIT | event types and encoder; depends on pydantic only. Imported only in `api` |
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
