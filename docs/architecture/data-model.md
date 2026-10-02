# Data model

What the controller stores, and how a message and a turn's event are written as
data. The model is the same in every store. `controller/adapters/postgres/schema.sql`
is its PostgreSQL rendering, and the in-memory store holds the same model. Another
store, outside this repository, holds it by the rules of
`docs/working-notes/oct-refactor/aws-serverless.md`.

Engines keep their memory apart, in storage of their own, and nothing here references
it (`docs/specs/agent-engines.md`). The controller keeps only the checkpoint id an
engine hands back, on the answer.

## Records

| record | key | holds | document |
|---|---|---|---|
| user | `id` | `provider` and `subject` (unique together), `name`, `email`, `created_at` | no |
| session | `id` | `owner_id`, `agent`, `title`, `created_at`, `updated_at`, `deleted_at` | no |
| message | `id` | `session_id`, `parent_id`, `role`, `created_at` | **yes** |
| turn | `id` | `session_id`, `follows`, `model`, `state`, `started_at`, `ended_at`, `error`, `lease_until`, `cancel_requested_at` | no |
| turn event | `(turn_id, position)` | `expires_at` | **yes** |

Also in the PostgreSQL schema: `schema_version`, one row with the version and the
hash of the file. Sign-in adds `user_sessions` and the pending logins.

A record whose fields are all ordered, filtered or decided by is columns. A record
with content of many shapes is a document: the store keeps it whole and never reads
inside it.

## Relationships

```
user 1 ── N session 1 ── N message ── parent_id ──► message (same session)
                    │
                    └─ N turn ── follows ──► message (the question, same session)
                           │
                           └─ N turn event
```

- **A user owns sessions.** Deleting a user deletes their sessions, after their
  sessions have been purged, which calls each engine's `forget`.
- **A session's messages are a tree.** `parent_id` is a message of the same session,
  or nothing for the first question. An edit is a new message under an earlier parent.
  The visible thread is the path from the root to the newest message.
- **A turn answers one question.** `follows` is a user message of the same session. A
  question has any number of turns: its first, each regeneration, and those that
  failed.
- **A turn produces at most one answer.** A finished turn stores one assistant
  message, whose `parent_id` is the question and whose document names the turn.
  A failed, cancelled or interrupted turn stores none. Every answer comes from exactly
  one turn.
- **A turn has its events**, numbered from 1.

## Rules every store keeps

- **At most one running turn per session**, held atomically by `start_turn`.
  PostgreSQL holds it with a partial unique index on `turns (session_id) WHERE state
  = 'running'`. A store without one holds it with a record keyed by the session,
  written with the turn on condition that it does not exist. There is no pointer to
  the running turn on the session: what is running is looked up.
- **A question and its turn are stored in one operation**, so a question refused a
  turn is not left behind.
- **A turn finishes in one operation:** its answer, its last events, its state, and
  the session's `updated_at`.
- **The runner numbers its turn's events** and is their only writer. The store
  refuses a position it already has.
- **Deleting hides, then purges.** `deleted_at` makes a session not found and closes
  it to turns. The purge calls the engine's `forget`, then deletes the session with
  its messages, turns and events.
- **Events expire.** `expires_at` is set when an event is written, as that moment
  plus a retention of hours. Ending a turn touches none of its events. The answer is
  in `messages` and the outcome is in `turns`, so after a turn ends nothing reads its
  events but a late watcher. Until they expire, they are the only copy of a turn's
  reasoning and of what a failed turn streamed.
- **A turn holds a lease.** The runner pushes `lease_until` forward and reads back
  `cancel_requested_at`. A running turn whose lease has passed is ended as
  `interrupted` by the next reader to find it.
- **No clocks and no ids in a store.** The controller mints every id and sets every
  time.

## Documents

### Who writes them

The controller encodes and decodes, in `controller/application`. **The store port
passes documents**, with the keys and columns beside them. A store never imports the
encoder and never reads inside a document. Every store, and every store outside this
repository, keeps the same bytes, and versions are upgraded in one place.

### What a document holds

**The whole record.** The fields that are also columns are written in the document as
well, so a document stands alone: it is what any store keeps, and what a JSON export
writes. The store writes the columns and the document from the same record in the
same write. The decoder reads the document alone.

### Spelling

- **The version** is `"v"`, an integer, first in every document.
- **A kind** is `"kind"`, in snake case, on every part and every event.
- **An id** is a uuid, lowercase, with hyphens.
- **A time** is UTC, fixed-width, with microseconds and `Z`:
  `2026-10-02T12:00:00.000000Z`. Fixed width makes text order time order, which some
  stores sort by.
- **A document is JSON.** A store that has no JSON type keeps it as UTF-8 text, never
  as its own map type.

### A message

```json
{
  "v": 1,
  "id": "6f1c…",
  "session_id": "0b7e…",
  "parent_id": "a3d2…",
  "role": "assistant",
  "created_at": "2026-10-02T12:00:03.141592Z",
  "agent": "assistant",
  "model": "sonnet",
  "checkpoint_id": "1f0a…",
  "turn_id": "c9e4…",
  "parts": [
    {"kind": "text", "text": "Let me check the weather."},
    {"kind": "tool_call", "call_id": "toolu_01…", "name": "weather",
     "arguments": {"city": "Lisbon"}},
    {"kind": "tool_result", "call_id": "toolu_01…", "text": "18°C, clear",
     "is_error": false},
    {"kind": "text", "text": "It is 18°C and clear in Lisbon."}
  ]
}
```

| field | on a question | on an answer |
|---|---|---|
| `parent_id` | the message it follows, or `null` for the first | the question |
| `role` | `user` | `assistant` |
| `agent`, `model` | what it was asked with | what answered |
| `checkpoint_id` | `null` | the engine's checkpoint after this answer |
| `turn_id` | `null` | the turn that produced it, which web shows as the run id |

Every field is present on every message; one that does not apply is `null`. `tool` is
a reserved role: no message has it yet.

### Parts

| kind | fields |
|---|---|
| `text` | `text` |
| `reasoning` | `text` |
| `tool_call` | `call_id`, `name`, `arguments` |
| `tool_result` | `call_id`, `text`, `is_error` |

- **In the order they were streamed.** Text that arrives in a row is one `text` part,
  until a tool call or a stretch of thinking comes between. Each stretch of thinking
  is one `reasoning` part. So what a person watched arrive is what is stored, and a
  reload shows the text written before and between tool calls.
- **`arguments` is a JSON object.** The model fills the tool's input schema, which is
  an object, and the frameworks hand it over parsed. The model's exact text is not
  kept, and nothing needs it.
- **A tool result is text**: the output as the model saw it, which is all the engine
  contract passes. It is not parsed, even when it looks like JSON. `is_error` is what
  the tool reported.
- **`reasoning` is in the format, and not written yet.** The runner streams reasoning
  as events and keeps none of it in the answer (`docs/specs/wire.md`). Keeping it is a
  change to the runner, to web, which shows it collapsed, and to
  `docs/specs/privacy.md`, since it would then live as long as the conversation. It
  needs no new version.

### A turn's event

```json
{"v": 1, "kind": "text_piece", "turn_id": "c9e4…", "position": 7,
 "message_id": "6f1c…", "text": "It is 18°C"}
```

Every event carries `v`, `kind`, `turn_id` and `position`, and then:

| kind | fields |
|---|---|
| `message_started` | `message_id`, `parent_id`, `role` |
| `text_piece` | `message_id`, `text` |
| `reasoning_piece` | `message_id`, `text` |
| `call_started` | `message_id`, `call_id`, `name` |
| `arguments_piece` | `message_id`, `call_id`, `text` |
| `call_completed` | `message_id`, `call_id` |
| `result_landed` | `message_id`, `call_id`, `text`, `is_error` |
| `message_completed` | `message_id` |
| `turn_ended` | `state` |

- **`message_completed` carries the id, not the message.** The answer is written in
  the same operation, and a copy in the events would put every answer in the store
  twice.
- **`turn_ended` carries the state, not the error.** The error is for the operator,
  is on the turn's record, and is never sent to a browser.
- **`TurnStarted` has no document.** The controller returns it to web and never
  stores it.

### Versions

One number for both documents, each with its own table of upgrades, since an upgrade
written for one would make nonsense of the other.

- **Additive changes keep the version:** a new kind of part or of event, and a new
  role. A build that does not know a kind refuses **the document holding it**, by
  name, and reads every other. A message is refused whole, since a message shown
  without part of itself is one misread.
- **Anything else moves it:** a new key, or a change to the meaning or shape of what
  is already written. Nothing is read past, so a build that dropped a key it did not
  know would write the record back without it.
- **A reader** reads every version up to its own, lifting older documents one version
  at a time, and refuses a version above its own.
- **A writer** always writes the current version. Nothing is rewritten in place.
- **Everything is checked on the way in.** A key nobody wrote, a time without its
  zone or an id spelt another way is an error, never ignored and never guessed at.

There is no reserved key for vendors' data. The signed thinking blocks a vendor needs
back are in the engine's memory (ADR 0005), so nothing in a message needs one.

### Size

A document is unbounded until stage two's bounds. A store that cannot keep a large
document in a record, such as DynamoDB at 400 KB, keeps it elsewhere and a pointer to
it. That is the store's own business, invisible at the port.

## What this asks of the code

The contract and the port today differ from this model in these places. Block 5 brings
them in line.

- The store port passes documents for messages and events, with their keys.
- Turns have ids. `TurnStarted` and the active turn carry the turn's id, positions
  are given by the runner, and `start_turn` stores the question and the turn
  together.
- `MessageCompleted` carries the message's id. `TurnEnded`'s error moves to the
  turn's record.
- An answer records its turn, and web takes the run id from it.
- The runner builds an answer's parts in stream order.
- The memory store follows, and a contract suite that any store can import proves it.
