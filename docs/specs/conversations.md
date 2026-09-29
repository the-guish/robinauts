# Conversations

## Shape

- A conversation is **stored as a tree of messages**: every message has a
  parent. It is **shown as one thread**: the path from a root to its newest
  message ("The visible thread", below).
- Editing a user message, or regenerating an answer, writes the new message
  beside the old one, under the same parent. Nothing is overwritten. From
  the user's point of view the old message and everything after it are
  discarded: they leave the visible thread and never return to it. In
  storage they stay where they were, with their parents, so that the
  lineage of edits and regenerations is there for analytics.
- Editing the first message gives the new message the parent the old one
  had, which is nothing: a conversation then has more than one root, and
  the visible thread begins at the newest.
- **A turn is a chain.** A root is a user message. A user message's parent
  is an assistant message, or the tool message a turn ended on — a turn
  stopped or failed after its results were in leaves that tool message as
  the leaf, and the next question hangs under it so that the results stay
  on the path the model sees — or nothing. An assistant message's parent is a
  user message, another assistant message, or a tool message: one turn may
  produce several messages, and with tools it produces a call and a result
  among them ([runs.md](runs.md)). A tool message's parent is the
  assistant message that made the calls, and **it answers exactly the calls
  of its parent, once each**: the results of one call batch are one tool
  message, holding one result per call, so that the visible path holds
  every result and no two tool messages ever stand side by side as one
  replacing the other. A tool message holds nothing but results; a user
  message holds no tool part; an assistant message holds no result. A
  stored conversation that breaks any of this is refused where it is read,
  as every other fault of the tree is. Tool messages are ordinary messages
  everywhere else: on the visible path, shown to whoever may read the
  conversation, in both exports, deleted with it. So a turn is one user
  message and everything the run produced under it.
- Regenerating replaces the **turn**: the new answer hangs under the user
  message that began it, beside the answer that was produced before, not
  under whatever the old answer happened to follow.
- A conversation belongs to one user, optionally inside a project, and is
  bound to an agent ([privacy.md](privacy.md), [agents.md](agents.md)).
- **It has a model**: its agent's default at the moment it started, unless
  its author picked another of the configured models, and changeable by its
  author at any point. A change applies from the next turn; a run keeps the
  model it started with ([agents.md](agents.md)). The conversation always
  names one — never "whatever the agent says" — so a later change to the
  agent's default does not reach it.
- Asking for a conversation that is not there and asking for one that
  belongs to somebody else are answered identically, in status, in words
  and in every header: which of the two it was is a difference only an
  attacker has a use for. Which it really was is in the log. The
  application raises the **same error** for both — "no such conversation" —
  so that the two can never drift apart in a route written later; what it
  really was is the error's own detail, which reaches the log alone.
- A conversation has at most one active run. The answer keeps being
  produced when its author is not watching ([runs.md](runs.md)).

## The format

The format is the platform's own
([ADR 0002](../adr/0002-conversation-persistence.md)): not that of an
agent framework, not that of a model vendor. The runtime projects each message it
completes into it; what the model is sent is the framework's own
transcript, stored beside the record ("The native transcript", below;
[ADR 0005](../adr/0005-one-agent-runtime.md)).

**What a message can contain**

| content | notes |
|---|---|
| text | |
| image | passed to the model where the model accepts images |
| file | an attachment, stored in the database |
| reasoning | the thinking some models emit, kept apart from the answer |
| tool call, tool result | a call is a part of the assistant message that made it — the call's id, the tool's full name, its arguments as data; the results of one call batch are one `tool` message under that assistant message, one result per call, each naming the call it answers, its text and whether it is an error. A call may stand without a result while its run waits, or after its run was stopped ([runs.md](runs.md)) |

**The version**

- Everything written in the format — a row of the database, a line of an
  export — carries the version of the format it was written in, and
  writing always writes the current version.
- Two things carry one: a **message** and a **run event**
  ([runs.md](runs.md)). A message's parts are written inside its message and
  have nothing of their own: one shape is one thing to version, to upgrade
  and to get wrong. The format has one version number, and each of the two
  documents is read forward on its own terms — an upgrade written for one of
  them would make nonsense of the other.
- A document is written **whole**: every key of it is there, `null` where
  there is nothing, and one that is missing is refused rather than read as a
  default. Only `extras` may be left out, since it is the one a build is
  allowed not to know about.
- **Adding does not move the version**, and there are exactly three ways to
  add: a new kind of content, a new role, and anything at all under
  `extras`. That key is reserved on **every document and every part of one**
  the format writes — a message and each of its parts, a run event and the
  event inside it — always the same thing: an object of at most 64 KiB
  written out, holding storable text, which a build that does not use it
  **accepts and reads past**. Vendor-specific extras, when they are kept —
  signed reasoning, provider message ids, cache hints — live there.
- **Any other new key moves the version**, as does any change to the
  meaning or the shape of what is already written. Nothing else is read
  past: a build that quietly dropped a field it did not recognise would
  write the message back without it.
- A build that meets content of a kind it does not carry refuses **the
  message holding it**, by name — "not supported yet" — whatever else that
  piece of content holds, and never reads it as something else: a message
  shown without its image is a message misread. The other messages of the
  conversation, and every other conversation, are unaffected. An operator
  who rolls a deployment back past a kind that has already been written
  will meet this, which is one more reason the version stays where it is
  for anything additive: rolling **forward** is what is meant to be cheap.
- A build **reads every version up to its own**, lifting older records
  through one upgrade per version — one for each of the two documents — and
  refuses a version above its own: it cannot know what that one means, and a
  build that guessed would write back a conversation it had misread.
- A piece of content holds at most 1,000,000 characters and a message at
  most 64 of them, so that no answer a model can produce has to be cut:
  text longer than one part is carried in the next. A title holds at most
  120.
- Stored text carries no NUL and no unpaired surrogate — one cannot be
  stored, the other cannot be encoded. What a provider sends is repaired on
  its way in: a NUL is dropped **first**, since one can arrive between the
  two halves of a character; a character that arrived in two halves is put
  back together; and a surrogate still on its own becomes U+FFFD. What an
  engine yields is not yet subject to this — it splits its answer where it
  likes — but everything the platform **publishes** is, and what is
  published, joined, is exactly what is stored.
- A time is written in UTC, with its offset. A conversation is dated
  between the years 1970 and 9998, which is the window every offset of a
  time can still be written back in; anything outside it is refused where
  it is read.

**Reasoning**

- It is stored, as its own kind of content.
- **Not in this version**, which keeps none of it **as content of a
  message**: an engine may stream it, every watcher sees it arrive, and what
  an engine returns as reasoning with a finished answer is dropped rather
  than refused — an engine is not asked to know what the platform keeps. No message carries the vendor's signed blocks either: they live in the
  native transcript, below.
- **It is in the run's events all the same, and only there as reasoning.**
  What is published while an answer is being produced is what a watcher
  re-attaching in the middle of it is replayed ([runs.md](runs.md)), so the
  reasoning deltas are stored with the other deltas of that run. No message
  holds a reasoning part, so nothing shows one, no Markdown export carries
  one, and no engine is handed one as content; and a run's events are
  removed once nobody can re-attach to them. "Stored nowhere" means "in no
  message as content", and the difference is the life of a run's events.
- If dropping it leaves an answer with nothing in it — a model that only
  thought, or that said nothing at all — what is stored is one empty piece
  of text. A message always has content, and a turn where the agent
  answered with nothing is something a conversation should record rather
  than leave out.
- It is shown collapsed under the answer, to everyone who can read the
  conversation.
- It is included in a JSON export and left out of a Markdown export.
- It is never sent to a vendor other than the one that produced it.
- **The vendor's signed blocks live in the native transcript, not in the
  record.** On the models the runtime reaches, thinking is on unless turned
  off, and an answer that makes a tool call carries signed thinking blocks
  the vendor requires back, unchanged, when the results go back. The
  framework keeps them in its own transcript and replays them itself, and
  the platform stores that transcript unread ("The native transcript"). No
  message of the record holds them, so no reader renders them, no export
  writes them, and no other vendor is sent them. `extras` stays reserved on
  every document, and this version writes nothing into it.

**The native transcript**

- Beside the record, each run stores the framework's own transcript of the
  turn it ran — the messages the framework produced, serialised by the
  framework's own serializer — as **one opaque document per run**, tagged
  with the runtime's name and version
  ([ADR 0005](../adr/0005-one-agent-runtime.md)). The platform never reads
  it; it is what the model sees.
- The native history of a turn is the concatenation of the slices of the
  runs on the visible path, oldest first. So the tree, an edit and a
  regeneration stay the platform's: they choose slices, and nothing edits a
  slice in place.
- Everything the framework adds for its own purposes lives there and
  nowhere else: the vendor's signed thinking, provider message ids, the
  summaries and compaction parts its context management produces, cache
  hints. The record is never compacted.
- A run that stored no slice — stopped, failed or interrupted before it
  ended — contributes its messages **rebuilt from the record**, through the
  runtime's lossy reverse projection: text, calls and results cross, and
  what only the transcript held is lost. The same rebuild moves a
  conversation to another runtime, or carries one across a framework
  version that cannot read an old slice.
- A conversation stays on the runtime it started on; **a model may
  change** at any turn, and carrying the transcript to another model of the
  same runtime is the framework's. Losing the vendor's signed parts on the
  way is accepted.
- Slices are deleted with their conversation, in the same transaction as
  its messages and runs.

**What an answer records**

- Every assistant message records the agent, the engine, the model and the
  run that produced it. The interface can show it.
- Every message records the delivery channel it came from
  ([channels.md](channels.md)); a conversation is not tied to one.
- No message records token counts while the question at the end of this
  document is open. The format has no field for them.

**The system prompt**

- It is not a message. It is taken from the agent's current configuration
  at every turn.
- Editing an agent therefore takes effect at the next turn of its existing
  conversations.

**Persistence**

- Messages are persisted as they are produced: each message is appended
  when it is **complete**. A message still being produced lives in its
  run's events ([runs.md](runs.md)) and not in the conversation. At any
  moment the stored conversation is consistent and complete up to that
  moment.
- A run's native slice is stored when the run ends, with the run ("The
  native transcript" above).
- A stored conversation this build cannot read — a version above it, a
  kind of content it does not carry, a tree that is no tree — is a fault
  of the deployment and not of the request that met it. It is answered
  like any other fault of ours, saying nothing, and the whole of it goes
  to the log.

## The visible thread

- Opening a conversation reads **one moment of it**: its messages, the run in
  flight if there is one, and that run's events, together. Two reads would
  disagree, and the gap between them is exactly where an answer is — a
  message completed between them is either shown twice or never shown at all,
  with the next announcement hanging under a message the reader has not got.
- **Users see exactly one thread**: the path from a root to the newest
  leaf, the leaf that sorts last by the message order (`created_at`, then
  id). Nothing is recorded about where a conversation opens; the tree
  decides. The rule holds because an edit or a regeneration always writes
  the newest message, and because a conversation has at most one active
  run, so an older branch can never gain a newer message.
- **While a run is in flight, the thread ends where the run is writing**:
  at the message its next one will hang under — the last message it
  completed, or the question it answers. A regeneration writes nothing
  until its first answer completes, so until then the newest leaf is still
  the answer it replaces, or a later turn, and the thread by the newest
  leaf would show what the run is putting aside with the new answer
  arriving after it. For every other turn the two rules agree.
- **Every message not on that path is discarded** from the user's point of
  view: soft-deleted by the shape of the tree, with no column saying so. It
  stays in storage with its parent for analytics and reaches no other
  reader — not the interface, not a project member, not a share link, not
  the model's history.
- There is no branch picker and no moving between branches. Editing or
  regenerating is the only way the visible thread changes shape, and it
  changes it by discarding.
- **Completing a message dates the conversation.** The message and the date
  move together or not at all.
- **Every other reader — a project member, someone with a share link —
  sees the same single thread**, live: it follows the author when they
  continue, edit or regenerate. What was discarded stays private to the
  store.

## Listing

- A person's conversations are listed **most recently updated first**, and
  the order is total: ties are broken by id, so two conversations updated in
  the same millisecond cannot swap places between two pages and be shown
  twice or not at all.
- A page is asked for with a count and, after the first, with the cursor the
  page before it handed back. The cursor is **opaque and is a position inside
  the caller's own listing**: the store writes it and the store reads it, and
  one that does not parse is refused rather than read as a position. One that
  parses and was never issued is simply a position — a listing holds the
  caller's own conversations whatever the cursor says, so there is nothing
  for a guessed one to reach, and cursors are not signed.
- **That is all the cursor promises.** Paging walks a list that is changing,
  not a transaction over a frozen one: a conversation written to while
  somebody is paging moves in the order and moves across the cursor with it,
  so it may be missed in that pass or seen in it twice. An interface that
  cares folds by id.

## Forking

- A project member can fork a conversation from any message of the thread
  they see.
- The fork is a new conversation owned by the person forking, in the same
  project. It holds a copy of the path up to that message, attachments
  included.
- It shows where it was forked from.
- It is independent from then on: later changes to the original do not
  reach it, and deleting the original does not delete it.

## Titles

- After the first exchange the platform asks the conversation's model for
  a short title. This is not a run and not a message.
- If that fails, the title is the beginning of the first user message:
  the first line with anything on it of that message's text, its
  whitespace collapsed to single spaces, at most 120 characters, cut at a
  word boundary with an ellipsis. A message with no text in it gives no
  title, and neither does one that is only spaces.
- A cut never falls inside a character: a letter keeps its accents, and an
  emoji sequence stays whole or is left out.
- The author can rename at any time. A renamed title is never overwritten.

## What a user can do

- Start a conversation with an agent; the application opens on an empty
  chat.
- Edit and regenerate. Either discards what came after the edit point
  from view; there is no going back to it.
- Rename, archive and delete.
- Attach files and images. They are stored in the database; images are
  passed to the model where the model accepts them.
- Search the conversations they can see.
- Export a conversation, as Markdown or JSON.
- Share it, or put it in a project ([privacy.md](privacy.md)).

## Deletion

- Deleting is soft. A deleted conversation sits in a trash for a fixed 30
  days; its author can restore it, or delete it for good.
- **A conversation with an active run is not deleted**: the author is told it
  is still answering and cancels the run first ([runs.md](runs.md)).
  Deleting under a run would leave the run writing messages into a
  conversation that is no longer there, and cancelling it on the author's
  behalf would hide a running answer behind a button that says "delete".
- After 30 days it is removed for good, with its messages, attachments and
  share links.
- Retention and purge are in [privacy.md](privacy.md).

## Details likely to change

- Search is PostgreSQL full-text search, so that no other service is
  needed.
- Attachments are stored as `bytea`. The maximum size is an operator limit
  ([operations.md](operations.md)). An object store could later sit behind
  the same port.
- Fitting a long history into a model's context is the runtime's, through
  the framework's own mechanisms — summarization, server-side compaction,
  cache settings — configured per agent
  ([ADR 0005](../adr/0005-one-agent-runtime.md)): applied to the native
  transcript, never to the record. Rare, large cuts are preferred to sliding
  windows, which break the prompt cache every turn.

## Open

- Whether to store the provider's raw token counts on each assistant
  message before usage reporting exists. They cannot be recovered
  afterwards, and storing them costs almost nothing.
