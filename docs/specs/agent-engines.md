# Agent engines

## What they are

An agent engine runs an AI agent on an agent framework and remembers the
conversations it has. Robinauts talks to engines through one contract and
does not know which framework is behind it.

There are two engines today, one on LangChain (with LangGraph underneath)
and one on Pydantic AI. From the outside they do the same job in the same
way. Adding a third means writing one more engine that keeps the contract.

An engine is standalone. The Robinauts backend uses it, but so can a
scheduled job, a command line script, or anything else that follows the
contract. 

## Words

- **Session**: the engine's memory of one conversation, under an id the
  caller chose.
- **Turn**: one question and everything the agent does to answer it: model
  calls, tool calls, and the final answer.
- **Checkpoint**: the memory as it was at the end of a finished turn. The
  engine names each one with an id. The caller keeps the id and never looks
  inside.
- **Agent definition**: the system prompt and the tool servers an agent may
  use, as the operator configured them.

## Responsibilities

- **Run a turn.** Given a session, the agent definition, the question and
  the model, run the whole agent loop: ask the model, call the tools it
  asks for, give the results back, and go on until the model answers.
  Tools are reached over MCP through the framework's own client.
- **Stream what happens.** As the turn runs, report pieces of the answer,
  pieces of the model's reasoning when there is any, each tool call once its
  arguments are known, each tool result as it comes back, and finally the
  answer together with the new checkpoint id.
- **Keep the memory.** Store each session in the framework's own format, in
  storage the engine owns. Create sessions, continue them, fork them, delete
  them. Keep every checkpoint it has handed out, so a turn can continue from
  any of them and a fork can be taken at any of them.
- **Manage the context.** Keep the history within the model's window by the
  framework's own means, summarising or trimming older exchanges, and place
  the vendor's prompt cache so that long conversations stay affordable. The
  caller never trims anything.
- **Reach the model providers.** Build the vendor's client for the model the
  caller names, with the key the engine asks for, and say which provider
  kinds it can reach so that a configuration naming another is refused
  early.
- **Set up its own storage.** Make tables, folders or files ready with its
  own driver, in a way that is safe to repeat.
- **Let go cleanly.** When a turn ends, fails or is cancelled, release what
  it opened: the model's stream, the HTTP connections, the tool sessions.
  Never swallow a cancellation.
- **Keep secrets secret.** Never write a key, a secret or conversation
  content to a log, and never send anything to a tracing or telemetry
  service.

## Out of scope

- The transcript people read, exports, retention, who owns a conversation,
  sign-in and access control. All of that belongs to the caller.
- Deciding when a turn runs, running it in the background, 
  retrying it, or scheduling it. The engine runs a turn when asked and does
  nothing on its own.
- Storing anything but the memory. No conversation records, no run records,
  no user data.
- Creating a session by itself. A session exists only when the caller
  creates it. A turn on a session the engine does not have is refused.
- Knowing where a conversation stands. The caller tells the engine which
  checkpoint to continue from at every turn.
- Choosing the model or the agent. The caller passes both at every turn.
- Reading or changing the caller's database, tables or schema.
- Counting usage or cost. Planned, not part of the contract yet.

## What an engine may depend on

- Its own framework and the libraries that come with it: LangChain and
  LangGraph for one, Pydantic AI for the other.
- The model vendors' own SDKs.
- An MCP client, normally the framework's.
- The shared contract: the engine interface, the events, the agent
  definition, the settings, the storage description and the errors. Nothing
  else from Robinauts.
- Optionally, a PostgreSQL driver when it supports PostgreSQL storage, and
  a local database or file library when it supports local storage. The
  engine opens, uses and closes these itself.

An engine must not depend on the web backend, its database layer, its
application services, its API, or its configuration reading. If an engine
needs something from Robinauts that is not in the contract, the contract is
what changes.

Every dependency must pass the project's licence policy, like everything
else in Robinauts.

## The contract

### Building an engine

Every engine is built by one function with the same inputs and the same
output, so that a caller adds an engine by a name and a function. The
function receives:

- **Settings**: the configured models, providers and tool servers; a way to
  ask for a provider's key; a way to ask for a tool server's secret. Keys and
  secrets are asked for, never handed over in the open.
- **Storage**: which kind, and what that kind needs. PostgreSQL with a
  connection pool; local with a folder; or in-memory with nothing, for
  tests.

It returns an engine. The caller then runs the engine's setup once before
anything else.

### Operations

- **kinds**: which provider kinds this engine can reach.
- **setup**: make the storage ready. Safe to repeat.
- **create** a session by id. Creating one that already exists is an error.
- **exists**: whether the engine has that session.
- **stream** a turn: the session id, the agent definition, the question, the
  model, the checkpoint id to continue from (none for the first turn), the
  seconds the turn has left, after which the engine ends it with an error
  (one call to the vendor has the model's own, shorter, timeout),
  and whether to resume. Yields the events as they happen and ends with
  the answer and a new checkpoint id.
- **fork** a session into a new one, at a checkpoint of the source. The new
  session has the memory as it was at that checkpoint and nothing after it.
  The source's earlier checkpoints stay valid in the fork. From then on the
  two are independent.
- **forget** a session: delete everything the engine keeps for it. Safe to
  repeat. This is the only way memory is deleted.

### Events of a turn

- A piece of the answer's text.
- A piece of the model's reasoning. Optional; not every model or vendor
  exposes it.
- A tool call: the call's id, the tool's name, its arguments, once the
  arguments are complete.
- A tool result: the call's id, the tool's name, the output as the model
  will see it, and whether the tool reported an error.
- Done: the final answer's text and the id of the new checkpoint. Always
  last, always once, and only after every tool call it reported has its
  result.

Events carry no ids of the caller's, no timestamps and no provenance. The
caller adds those.

### How a turn ends

- **Normally**: with Done. The memory already holds the turn when Done is
  sent, so a turn that finished is remembered even if the caller then fails
  to store the answer.
- **By an error**: the engine raises, sends nothing more, and the next turn
  does not remember what this one said.
- **By cancellation**: the caller cancels the turn, the engine lets the
  cancellation through and releases what it held, and the next turn does not
  remember what this one said.
- **Resumed**: when the caller runs an interrupted turn again with the same
  session, question and checkpoint and asks to resume, the engine continues
  from whatever partial work it kept, without repeating tool calls already
  made. If it kept nothing, the turn simply runs again from the checkpoint.
  Asking to resume with a different question is an error.

### Refusals

- A session created twice: an error, and the memory is left as it was.
- A turn or a fork on a session the engine does not have: an error.
- A checkpoint id the engine does not hold for that session, including one
  of another session or of a turn that did not finish: an error, and nothing
  is written.
- Resuming with a different question: an error, and nothing runs.
- Forgetting what is not there: nothing happens.

### What the caller promises

- Create a session before its first turn, and forget it when the
  conversation is deleted.
- Pass the agent definition and the model at every turn. The engine stores
  neither.
- Store the checkpoint id that Done carries on the answer, and pass it back
  for the next turn and for a fork. Hand an engine only its own checkpoint
  ids.
- Run one turn at a time per session.
- Cancel a turn by cancelling it and closing the stream, within a bound of
  the caller's choosing.
- Keep a session with the engine that created it. Another engine cannot read
  its memory; moving an agent to another engine means its conversations
  start again from nothing.
