# Data model

What the controller stores, and how a message and a turn's event are written as
data. The model is the same in every store. `controller/adapters/postgres/schema.sql`
is its PostgreSQL rendering, and the in-memory store holds the same model. Another
store, outside this repository, holds it by the rules of
`docs/working-notes/oct-refactor/aws-serverless.md`.

Engines keep their memory apart, in storage of their own, and nothing here references
it (`docs/specs/agent-engines.md`). The controller keeps only the checkpoint id an
engine hands back, on the answer, and the engine's name on the session and on the
answer. A turn runs on the engine the session records, which holds its memory, and the
purge forgets on it, whatever the agent's configuration names today; the controller
builds that engine on demand when the configuration no longer names it. Moving an
agent to another engine, and what its sessions then do, is stage two.

## Records

| record | key | holds | document |
|---|---|---|---|
| user | `id` | `provider` and `subject` (unique together), `name`, `email`, `created_at` | no |
| session | `id` | `owner_id`, `agent`, `engine`, `title`, `created_at`, `updated_at`, `deleted_at` | no |
| message | `id` | `session_id`, `parent_id`, `role`, `created_at` | **yes** |
| turn | `id` | `session_id`, `follows`, `model`, `state`, `started_at`, `ended_at`, `error`, `lease_until`, `deadline_at`, `cancel_requested_at`, `retries` | no |
| turn event | `(turn_id, position)` | `expires_at` | **yes** |
| user session | `id` | `user_id`, `secret_hash` (unique), `created_at`, `expires_at` | no |
| pending login | `state_hash` | `provider`, `nonce`, `verifier`, `return_to`, `created_at`, `expires_at` | no |
| API token | `id` | `user_id`, `name`, `secret_hash` (unique), `created_at`, `expires_at` | no |

Also in the PostgreSQL schema: `schema_version`, one row with the version and the
hash of the file. The last three records are sign-in's, kept through the
`Credentials` port and not the store, in `user_sessions`, `pending_logins` and
`api_tokens`. Each holds the SHA-256 of a secret, never the secret, and is found by
it (`docs/specs/sign-in.md`).

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
  question has any number of turns: its first, each regeneration and retry, and
  those that failed. A retry's `retries` names the failed answer it tries again,
  which the model is told about (`docs/specs/ui.md`).
- **A turn produces at most one answer.** A finished turn stores one assistant
  message, whose `parent_id` is the question and whose document names the turn.
  A failed turn stores what it streamed before it failed, as an answer marked
  `failed`, with no checkpoint; a reply hangs under it, and the next turn continues
  from the nearest answer above it that has a checkpoint. A cancelled or interrupted
  turn stores none. Every answer comes from exactly one turn.
- **A turn has its events**, numbered from 1.

## Rules every store keeps

- **At most one running turn per session**, held atomically by `start_turn`.
  PostgreSQL holds it with a partial unique index on `turns (session_id) WHERE state
  = 'running'`. A store without one holds it with a record keyed by the session,
  written with the turn on condition that it does not exist, and removed in the same
  write as whatever ends the turn, `finish_turn` or `end_expired_turn`. There is no
  pointer to the running turn on the session: what is running is looked up.
- **A question and its turn are stored in one operation**, so a question refused a
  turn is not left behind.
- **A turn finishes in one operation:** its answer, its last events, its state, and
  the session's `updated_at`.
- **The runner numbers its turn's events** and is their only writer, one at a time, so
  positions commit in order. The store accepts the same document again at a position
  it has, since a write retried after a lost acknowledgement is not a second runner,
  and refuses another document there, or any append or finish on a turn that is no
  longer running or whose lease has passed at the time of the write, with
  `TurnLostError`. A runner refused has lost its turn, to a second runner, to a reader
  that ended it, or to its own lease: it cancels its engine's stream and writes
  nothing more. **The first append is the claim:** a second runner dispatched for the
  same turn is refused at position 1, before it has run the engine, because each
  runner mints the answer's id afresh and so no two claims are the same document.
- **Deleting hides, then purges.** `deleted_at` makes a session not found and closes
  it to turns. The hide is refused while a turn is running (`TurnActiveError`), so no
  runner and no engine writes under a purge: the controller first ends a turn whose
  lease has passed, cancels the turn it runs itself and waits for it to end, and a
  turn run by another process is refused until stage two's cancel through the store.
  The purge calls `forget` on the engine the session records, which may no longer be
  the one its agent's configuration names, then deletes the session with its
  messages, turns and events.
- **Events expire.** `expires_at` is set when an event is written, as that moment
  plus a retention of hours. Ending a turn touches none of its events. The answer is
  in `messages` and the outcome is in `turns`, so after a turn ends nothing reads its
  events but a late watcher. Until they expire, they are the only copy of a turn's
  reasoning, and of what a cancelled or interrupted turn streamed.
- **A turn has a deadline.** `deadline_at` is written with the turn, as its start plus
  `[work] max_turn_seconds`, and bounds the whole run. A model's `timeout_seconds`
  bounds one call to the vendor, which the vendor's SDK retries `max_retries` times,
  and is never the turn's.
- **A turn holds a lease.** `lease_until` is written with the turn, as its deadline
  and a margin, and the runner's own deadline stays short of it. A running
  turn whose lease has passed is ended as `interrupted` by the next reader to find it
  (`open_session`, `start_turn`, `watch_turn`, `cancel_turn`, `delete_session`),
  through `end_expired_turn`: one conditional write that only a running turn takes,
  on the record alone, and on the marker a store without a partial index keeps. No
  event is written, since a `turn_ended` event is the runner's; a watcher that finds
  the turn ended with none supplies it from the record. A runner that outlives its
  lease has lost the turn whether or not a reader has found it: every write names
  its time, and the store refuses one past the lease. Renewing the lease for a long
  turn, and reading back `cancel_requested_at` with each renewal, are stage two.
- **No clocks and no ids in a store.** The controller mints every id and sets every
  time.
- **A session and its records are addressed from the owner down.** Every operation on
  a session names its owner and the session; every operation on a turn or its events
  names the owner, the session and the turn. A store that keeps a session's records in
  one partition (DynamoDB, Cosmos DB, Firestore) finds the partition from those ids,
  with no index on a turn's id alone. PostgreSQL needs only the innermost id, and
  checks the rest. A session that is not its caller's owner's is not found.

## Documents

### Who writes them

The controller encodes and decodes, in `controller/core`. **The store port
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
- **Text holds no NUL and no unpaired surrogate.** The encoder drops the one and
  replaces the other with U+FFFD in every string it writes, keys and values alike,
  question, pieces, tool arguments and results, and writes no NaN or infinity. The controller cleans the same way every text it stores
  as a column: a title, a name, an email, a turn's error. PostgreSQL's `text` and
  `jsonb` hold neither, a provider or a tool may send either, and a store that
  accepts them would hold what another cannot.

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
  "engine": "langchain",
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
| `agent`, `engine`, `model` | what it was asked with | what answered |
| `checkpoint_id` | `null` | the engine's checkpoint after this answer; `null` on a failed one |
| `turn_id` | `null` | the turn that produced it, which web shows as the run id |
| `failed` | absent | `true` on an answer whose turn failed; absent otherwise |

Every field but `failed` is present on every message; one that does not apply is
`null`. `failed` is written only when it is `true`, so the messages stored before it
read the same. `tool` is a reserved role: no message has it yet.

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

- **Additive changes keep the version:** a new kind of part or of event, a new
  role, and a new key written only when it is set and read as unset when absent
  (`failed`). A build that does not know a kind, or such a key, refuses **the
  document holding it**, by name, and reads every other. A message is refused whole,
  since a message shown without part of itself is one misread.
- **Anything else moves it:** a key every document must have, or a change to the
  meaning or shape of what is already written. Nothing is read past, so a build that dropped a key it did not
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

Block 5 brought the contract, the store port, the memory store, the runner, web and the
frontend in line with this model (`docs/working-notes/oct-refactor/data-model-progress.md`
says what was verified). PostgreSQL is block 6 (`postgres-plan.md`).
