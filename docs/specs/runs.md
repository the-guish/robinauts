# Runs

A **run** is the work an agent does to answer one user message. It is a
record in the database, it executes in the background, and it does not
depend on the request that started it.

## Behaviour

- Sending a message creates a run and starts it. The request that sent the
  message then only *watches* the run.
- **If the request drops — the tab closes, the network fails — the agent
  keeps working.** The answer is in the conversation when the person comes
  back.
- A conversation has **at most one active run**. While one is active, a new
  message in that conversation is refused; the person can cancel the run.
- Everything a run produces is **persisted as it is produced**, not at the
  end: each message — an answer with the tool calls it made, and the one
  tool message of a batch once its last result is in — is appended when it
  is complete. A message still being produced is in the run's events and
  not yet in the conversation. At any moment the conversation in the
  database is consistent and complete up to that moment.
- **An event has one written form**, the platform's own, versioned like a
  message: what the events table keeps and what the wire carries are the
  same document, and the two documents of the platform are that and a
  message ([conversations.md](conversations.md)) — with the same reserved
  `extras` on it and on the event inside it. What the platform
  publishes is storable text, so a delta that would end on half a character
  holds it back until the other half arrives.
- **What was published is what was stored.** The deltas of a message,
  joined, are the text of the message that completed it. A person who
  watched an answer arrive has the answer that is in the conversation.
- **Every event of a run has a position**: a number starting at 1, counting
  up by one, with no gaps, for the life of the run. The application assigns
  it; an engine yields events and knows nothing of positions, so one run is
  numbered one way whichever engine produced it.
- **Opening a conversation with a run in flight is told where to attach.**
  Whoever serves the conversation serves its messages and, when a run is
  active, that run's id and its `resume_point`: the position of the run's
  last completed message, or of the event that started it if it has
  completed none, together with the message a new announcement after that
  point will hang under — the two things anyone checking that stream needs,
  handed over together so that neither is guessed. The messages served
  end at that message ([conversations.md](conversations.md), "The visible
  thread"), so what arrives is shown where it belongs, even for a
  regeneration that has not yet replaced the answer it is producing again.
  The watcher has every complete message already, so
  attaching there replays exactly the message still being produced, from its
  announcement. Nothing twice, nothing missed.
- The stream of a run is **re-attachable**, and this is what that promises.
  A watcher gives the position it last saw. What it receives is every event
  of that run after that position, in order, numbered on from it by one
  with no gaps, and nothing else. Giving 0, or nothing, is asking for the
  run from its beginning, the event that started it included; giving any
  later position is asking for what followed it, and the event that started
  the run is never in that. A slice may begin in the middle of a message —
  its remaining deltas, and its completion — and it is the middle of
  **one** message, because a run produces one at a time. So a watcher that
  first loads the conversation's finished messages and then applies the
  slice has the run entire, with nothing shown twice. Opening a
  conversation that has an active run attaches to it.
- A run can be **cancelled** by the conversation's author. Cancelling is the
  application cancelling the engine's task; the engine lets the cancellation
  through and releases what it holds ([agents.md](agents.md)). What was
  produced before it stays: a message that was complete is in the
  conversation, and the one in flight is not.
- **Cancelling reaches a run two ways, and the database is the one that
  counts.** The process executing a run keeps the task, keyed by run id, and
  cancelling that task is what releases the provider connection promptly. When
  no task is known — after a restart, or from another process — the run is
  ended `cancelled` in the store instead, with its `RunEnded` at the next
  position. That is not a weaker cancellation: **a run that has ended takes no
  more writes**, so a task still executing it is refused at its next event and
  stops there. The registry is the fast path; the store is the rule.
- An engine reports a failure by **raising**. The run ends `failed`, with a
  description of what was raised recorded on it — made storable and cut to
  fit — and the answer that was in flight is left uncompleted.
- **A turn that ends without an answer is a failed run**, never a finished
  one: the run records that the engine produced no answer. A `finished` run
  has at least one message in the conversation. So is a turn that **announced
  an answer and never completed it** while neither raising nor being
  cancelled: a finished run leaves nothing half-written, and a turn that
  stopped without saying so is a turn that went wrong.
- **An answer that does not match what was published fails the run.** If any
  text was published for an answer and the answer completes with different
  text, the promise a watcher was given is broken, and what would be stored is
  a stream that cannot be read back against the conversation. The run is
  failed and the answer is not stored, rather than either being kept.

## States

| state | meaning |
|---|---|
| `running` | executing in a backend process |
| `waiting` | suspended: a tool call has no result yet; nothing is held in memory |
| `finished` | completed; its messages are in the conversation |
| `failed` | ended with an error, which is recorded on the run |
| `cancelled` | stopped by the author |
| `interrupted` | its process went away while it was running |

`running` and `waiting` are the active states.

Which state may follow which:

- `running` becomes `waiting`, `finished`, `failed`, `cancelled` or
  `interrupted`.
- `waiting` holds no process, so nothing interrupts it: a tool result
  resumes it to `running`, its author cancels it, or it fails.
- The four ended states are ended. A run never leaves one, and never
  re-enters the state it is already in; a retry is a new run.
- A run records when it ended, in every ended state and in no other. Only
  a run that ended badly records an error, and a `failed` run always does.
- The times a run records are the clock's, clamped so that none of them is
  earlier than the one it follows. A wall clock steps backwards now and
  then, and a run in flight must not become unrecordable because of it. A
  run that was executing records when it began, at the latest when it ends.
- **Ending a run never fails.** What went wrong is whatever a provider or a
  traceback said, at whatever length and in whatever characters: it is made
  storable and cut to fit before it is recorded, and a failure with nothing
  to say records that it had nothing to say. A run is never left `running`
  because its error would not fit.

A run records the conversation, the message it answers, the agent, the
engine and the model it used, its state, its times, and its error if any. The
model is the conversation's at the moment the run is begun, and it is what
the engine is handed, so a run keeps it however the conversation's changes
([agents.md](agents.md)).

## Tools

Tools are in [agents.md](agents.md) ("Tools"); runs were designed for
them.

- A tool call is part of the assistant message that made it, and its result
  is a message of the conversation, persisted like any other: **the results
  of one call batch are one tool message** under that assistant message,
  one result per call ([conversations.md](conversations.md)).
- **Short tools** run inside the run: the model calls the tools, the
  platform runs them in parallel, publishes each result as it lands, writes
  the tool message when the last one is in, and continues the turn from the
  stored history — in one execution, as many rounds as the turn needs up
  to `max_tool_rounds`. **The run stays `running` through the loop**; the
  engine's "waiting on these calls" is how one round ends, and the `waiting`
  state below is a run that holds no process. Every call has its own
  timeout; the turn's timeout holds over the whole turn.
- **Long tools** need nothing more: the run outlives the request, the UI
  shows it as running and re-attaches at will.
- **Tools that outlast a process** — an external job, a person's approval —
  suspend the run. The conversation holds a tool call without a result, the
  run is `waiting`, and no process holds anything. When the result arrives
  it is appended as the tool message, and execution resumes from the
  history. **Deferred in this iteration**: the loop is written so that "the
  result arrives later" is the same code path as "the result arrives now",
  and a batch with one result missing is what a suspended run looks like,
  but nothing suspends a run yet and no route appends a result
  ([working-notes/mcp-plan.md](../working-notes/mcp-plan.md), step 7).
- Resuming is therefore the ordinary stateless turn
  ([ADR 0002](../adr/0002-conversation-persistence.md)): **the conversation
  record is the checkpoint.** It works the same with either engine and
  needs no framework persistence.
- The agent port's result is either "finished" or "waiting on these tool
  calls". Without tools it is always "finished".
- **A tool's error is a result, not a failure.** A server answering that
  the call failed, or a call that ran out of its time, becomes a result
  marked as an error, and the model is told. Only a server that cannot be
  reached at all — or that will not list its tools when the run begins —
  fails the run, naming the server.
- **A run stopped in the middle of a batch leaves the calls without a
  result message**, which the format allows: a cancellation, an
  interruption or a failure between the assistant message and the tool
  message ends the run with the calls stored and no result. Nothing is
  re-executed on its own: a retry is a new run from the question, which
  puts the unanswered calls off the visible path
  ([conversations.md](conversations.md)). When such an answer is on the
  visible path of a later turn — a question asked after the stop — the
  model is told, for each call, that no result of it was recorded and
  whether it ran is not known; that sentence is the adapter's, the same
  under both engines, and is never stored
  ([ADR 0004](../adr/0004-context-management-in-the-adapter.md)). The MCP annotations a server
  sends with a tool (`readOnlyHint`, `destructiveHint`, `idempotentHint`)
  are carried on the definition and are what a policy would read — to
  re-execute a safe call after an interruption, or to ask a person before
  a destructive one — and no policy reads them yet
  ([agents.md](agents.md), "Tools").

## Where the work happens

- In the backend process, as asyncio tasks on the same event loop that
  serves requests. There is no separate worker, queue or scheduler
  (goal 6).
- Runs are I/O-bound: they wait on model providers and on tool servers.
  CPU-bound work of our own goes to a thread, never on the loop.
- The database holds everything that matters — the run, its state, its
  messages, its events. The process only executes.
- Periodic housekeeping — retention, emptying the trash, expiring sessions,
  detecting orphaned runs — runs the same way, and a database lock ensures
  that one process does it.

## Restarts and several processes

- On shutdown the backend stops accepting new messages and cancels every run
  it is carrying, which ends them `interrupted` under one bound for the whole
  stop ("Shutdown cancels; it does not drain", below). Letting them **drain**
  first, so that an answer nearly finished is finished rather than retried, is
  planned.
- Asking the store which runs are in a state is asked for the **active**
  states, by the sweep alone, and is bounded: a read with no bound over a
  table that only grows is a read waiting to take all of it.
- A run records the process that owns it, with a heartbeat. A `running` run
  whose owner is gone is marked `interrupted`. **Whoever marks it appends the
  event that ends it** — `interrupted`, at the next position, which the store
  knows — so that every ended run's stored stream is complete and a watcher
  of one is told it is over rather than waiting. A `waiting` run holds no
  process and is never interrupted; the state machine already says so, and the
  sweep asks it rather than repeating it.
- **A run ended before anything of its stream was stored is given its
  beginning.** A run cancelled in the instant after it was created, or left
  behind by a process that died before it wrote anything, would otherwise have
  a stream beginning with its end, which nothing can read. Whoever ends a run
  with no events writes the `RunStarted` first and then the event that ends
  it.
- An `interrupted` or `failed` run can be retried by the author. The retry
  is a new run from the conversation as it stands, so nothing already
  produced is lost or repeated.
- With several backend processes a run lives in exactly one. A watcher
  connected to another process receives the run's events through the
  database.
- Every model call, every tool call and every run has a timeout; so has
  listing a server's tools when a run begins.

## In the layout

- `application` owns the run lifecycle: start, persist as it goes, publish
  events, suspend, finish, cancel, retry. **The engine's events are not the
  run's events.** An engine says an answer has begun, more of its text, more
  of its thinking, and here are the parts it ended with: it has no ids, no
  clock and no rows, so it can neither name a message nor say that one is
  stored ([agents.md](agents.md)). The application is what gives an answer
  its id, its parent and its provenance, writes it down, and only then
  publishes it as a message. What it publishes are the platform's own turn
  events, in this order:
  - the run started, once, before anything else;
  - for each message: it is announced — with its role and the message it
    hangs under, so that a watcher can place it before any of it exists —
    then the pieces of it as they arrive, then the message completed,
    carrying the message as it was stored. For an **assistant** message
    the pieces are its text, its reasoning, and its tool calls: each call
    announced with its id and name, then its arguments as they stream. For
    a **tool** message — announced with the `tool` role under the assistant
    message that made the calls — the pieces are the results, each
    published as it lands, in the order they land;
  - the run ended, once, last, with the state it ended in. Nothing follows
    it.

  Reasoning is published as it arrives and kept **in the run's events alone**:
  no message holds any of it, so it is in no conversation and in no export,
  and it is in the events because a watcher that re-attaches in the middle of
  an answer must be able to rebuild what it is watching
  ([conversations.md](conversations.md)).

  A run that ended `finished` completed at least one message and left none
  half-written. One that failed, was cancelled or was interrupted may leave a
  message announced and never completed: that is what a cancellation in the
  middle of an answer, or in the middle of a batch of tool calls, looks
  like. **What was published is what was stored** holds for a tool message
  too: the results published for it are the results of the message that
  completed it, and the arguments streamed for a call parse to the
  arguments stored on it.
- **Two ports and a watcher**, where this document first said one port. A
  `RunExecutor` carries the work of a run in the background and cancels it,
  and knows nothing else: it is handed a run's id and something to run, and
  which run may be executed, what a cancellation means and what is written
  when work stops are all the application's. Subscribing is **not** its
  business, because a run's events are read from the store and never from
  whoever is executing them — a watcher in another process has no task to
  subscribe to, and a watcher in this one must still be told only what was
  stored. So a second port, `RunSignals`, says one thing — "this run now
  reaches this position" — carrying no data at all, and the application's
  watcher reads the store, yields what is past the position it was asked
  for, waits on a signal under a bound, reads again when the bound passes,
  and ends once it has yielded the event that ended the run — or gives up and
  says so, if the run stores nothing at all for long enough (known limits). A
  signal that is lost is a stream that arrives a wait later; a store that is not read is a
  stream that is wrong. `api` only watches and maps events to the wire
  ([wire.md](wire.md)).
- **Signals remember what they said, for a bounded while.** A watcher does not
  ask to be woken; it asks whether there is anything past the position it
  holds, and for everything already announced — the end included — that
  question has an answer and must be answered at once. A run's record
  therefore outlives the run, bounded in number and in age, and a watcher of
  one that has been forgotten falls back to the bounded poll.
- **A watcher looks at the run again after every wait, and every turn of its
  loop either sends something or waits.** A signal that answers at once must
  not become a loop that answers at once for ever: a wake-up with nothing
  behind it is not believed a second time in a row, and the run record is
  re-read after each wait — which is what ends a watcher of a run that has
  ended without storing the end it was waiting for, and of one whose rows are
  gone. **A run that is no longer there is over**: the conversation was
  deleted, the stream ends, and nothing is reported, because nothing about the
  request was wrong.
- **A run is claimed before its work exists, and work is cancelled only once
  it has begun.** Between handing work over and its first step there is an
  instant in which nothing is executing the run, and a sweep looking in it
  would interrupt a run that is about to be answered; and work cancelled
  before that first step would write no ending at all. So the process says
  the run is its own before it hands the work over, and a cancellation that
  cannot reach work that has begun is written into the store instead — which
  is where a cancellation counts anyway.
- **A turn whose work cannot be scheduled is ended, not left.** The one way
  that happens is a request landing while the process is shutting down: the
  run has been written by then, and a run nothing will ever execute would
  block its conversation until the next start-up swept it. It is ended
  `interrupted` at once, and the refusal is answered to whoever asked.
- Runs and their events are stored by the **same port as conversations and
  messages** (`ConversationStore`), because they are the same database and
  several operations over them are one transaction: beginning a turn, ending
  a run, completing a message, deleting a conversation. Two ports would be
  two calls, and a process can stop between two calls.
- **The store is where "at most one active run" is held**, not the
  application: two requests that both looked, both found none and both
  inserted would give one conversation two answers writing into it at once,
  so creating a run and refusing a second are one indivisible step, and the
  second caller is told the conversation is already answering. The store
  holds four more rules of its own, for the same reason — they are about the
  rows that are there and not about the request:
  - **a run that has ended is done with.** It is never written again, and it
    takes no more events and completes no more messages. That is what stops a
    process coming back from a timeout and re-opening a run its author
    cancelled, and what stops a writer that read the last position before the
    cancel putting an event past the `RunEnded` that is already there, leaving
    a stream nothing can read back. Refused in the same step as the write.
  - **an event is stored at the next position and at no other.** The
    application is the single writer of a run's events and reads the last
    position from the store before offering the one after it; a position
    already stored, or one that skips, is refused, and of several callers
    offering one position at once exactly one succeeds. A refusal means
    somebody else ended the run first, so the answer is to stop writing into
    it rather than to renumber and try again.
  - **nothing is announced that is not stored, and nothing stored goes
    unannounced.** A message and the event that completes it are written
    together; so are a run's ended record and the event that says it ended.
    Both or neither. And they must **say the same thing**: the end announces
    the state the record ended in, the completion announces the message that
    was stored, that message is an answer recording that run, and the run is
    in that message's conversation. The store reads its own columns and the
    records to see it; that a stored **document** says what its record says,
    and that a run's stream reads correctly as a whole, are the application's,
    and are what `core.check_event_order` holds it to.
  - **a run begins active and answers a question**: it is created in one of
    the active states, the message it names is a user message of its
    conversation, and its agent is its conversation's agent — a conversation
    is bound to one ([agents.md](agents.md)). A run stored already ended, with
    no events under it, could never be made whole.
- **Deleting a conversation deletes its runs and their events in the same
  transaction**, and is refused while a run is active — which is decided
  inside that transaction, so a run cannot begin in the window between the
  check and the delete and nothing can be left orphaned.

## Known limits of this version

- **A run whose end cannot be written stays `running`.** Ending a run is the
  one write with nobody to report a failure to, so it is shielded from
  cancellation, it reads its position back rather than trusting what it
  counted, and it is tried again a few times when the store **cannot be
  reached or does not answer** — each attempt under a bound of its own, so
  that a store which never replies cannot hold the task for ever where
  nothing could even cancel it. If it still cannot be written, the failure is
  logged and the run is left as it is: the **start-up sweep of the next
  restart** is what ends it, and until then its conversation refuses a new
  message. The periodic sweep that would shorten that window is part of the
  several-processes work above.
- **A refused position is a question, never a repetition.** The application is
  a run's single writer, so a position that is refused, or a write whose
  answer never came back, is answered by reading what is actually stored where
  the event was offered: it is already there and nothing more is needed, the
  run has ended and this writer stops, or something that is not this writer's
  is there — which is a fault, and the run is failed saying so. An event is
  never simply offered again at the next free position: two writers that both
  did that would store a run's beginning twice and leave a stream nobody can
  read back.
- **A watcher gives up on a run that stores nothing.** A run whose end could
  not be written, or whose process was killed, would otherwise be followed
  until the client gave up: after a silence as long as a whole turn may take,
  watching it is given up on — with a line in the log, and by **raising**,
  which the wire turns into something the person can see rather than a page
  that waits for ever. It is **said** and not left to be noticed: a stream
  that simply ends has sent everything there is — the run's `RunEnded`, or
  everything stored after the position it was asked from of a run that is over
  or is no longer there — so "we stopped watching" and "there is nothing more"
  are told apart by what was raised rather than by what is missing.
- **Shutdown cancels; it does not drain.** The backend says that it is
  stopping, cancels the work of every run it is carrying, and waits for them
  to write their ends under **one bound for the whole stop** — not one bound
  each. They write at the same time, so what the stop needs is as long as one
  ending may take, which is a number the run lifecycle owns and the
  composition root adds up; a bound multiplied by however many runs a process
  happened to be carrying is not a bound at all. They end `interrupted` —
  nobody cancelled them, their authors may retry them, and an interface must
  not tell somebody they stopped what they did not — and each is announced, so
  a watcher is told the run is over rather than left waiting. A run whose work
  does not stop inside the bound is abandoned, and the start-up sweep of the
  next restart is what ends it. **Work cancelled in the instant before it ever
  began is reported rather than lost**: it ran no line, so nothing of it wrote
  an ending, and the run is ended `interrupted` while the process still holds
  its store — inside that same one bound, and told what it is, because an
  ending is written where no cancellation can reach it and a stop that simply
  waited would be as long as the slowest store times the number of runs. A
  write still going when the bound passes is **left to land** rather than
  killed, with a line in the log saying so. Letting runs finish what they were
  doing is the draining described above, and it is outside this version
  ([poc-scope.md](../working-notes/poc-scope.md)).
- **A run's events are kept until its conversation is deleted.** They exist to
  be re-attached to, and removing them once a run has been over for a while is
  the housekeeping described above; until it exists, what a run published —
  including the reasoning it published — is kept as long as the conversation
  is.

## Details likely to change

- The executor is an adapter over `asyncio.create_task`, with a registry of
  live tasks (so that none is lost to garbage collection, every failure is
  retrieved and logged, and shutdown can stop them — this version cancels
  rather than drains, as the known limits say), started and stopped by the
  ASGI lifespan. FastAPI's `BackgroundTasks` is not used: it is tied to a
  request.
- Events are kept per run in an events table and announced with
  PostgreSQL `LISTEN/NOTIFY`; a watcher re-attaches with the id of the last
  event it saw. Events of a finished run are removed after a while; the
  messages are the lasting record.
- Housekeeping takes a PostgreSQL advisory lock.
- A later `robinauts worker` process role — the same wheel, claiming runs
  from the database — would be another adapter of `RunExecutor`. It is not
  planned.
- With LangGraph, a graph that keeps state of its own beyond the messages
  cannot be resumed from the conversation alone. That case belongs to the
  open discussion in ADR 0002.
