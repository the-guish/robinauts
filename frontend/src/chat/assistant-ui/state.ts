// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * What the chat holds, and the one function that changes it.
 *
 * A reducer, and nothing else: no `fetch`, no timers, no React. What arrives
 * -- a conversation read from the API, an AG-UI event, a message somebody
 * typed -- is an action, and what comes out is the next state. That is what
 * makes the interesting half of the chat a thing a test can drive one event
 * at a time (`./runtime.test.tsx`), and it is where the rules of
 * `docs/specs/wire.md` about a stream that begins in the middle are written
 * down once.
 *
 * **The store is the truth and this is a view of it.** What is held here is
 * the conversation as it was last read, plus the message the run in flight is
 * producing -- which is in no conversation until it is complete
 * (`docs/specs/runs.md`). When a turn ends, the conversation is read again
 * and what the server says replaces all of it: real ids, the provenance of
 * the answer. Nothing here is ever the only copy of anything.
 *
 * **One thread, in order.** The server sends the one path a conversation
 * shows (`docs/specs/conversations.md`), and this holds it as a list. An
 * edit or a regeneration cuts the list after the edit point and goes on from
 * there: what was after it is off the screen for good, kept only in the
 * store, and the next read says the same.
 *
 * **A tool call is part of the answer that made it, and its result is drawn
 * on the call.** The store holds the results of one batch as a tool message
 * under the answer (`docs/specs/conversations.md`); here that message is
 * folded into the answer -- it is no bubble of its own -- and the answer
 * remembers its id, because it is what the next message hangs under
 * (`resultsId`). On the wire the call arrives as AG-UI's tool events and the
 * result as `TOOL_CALL_RESULT` (`docs/specs/wire.md`); everything in either
 * is text the model or a tool wrote, held here as data for the renderer to
 * draw as text.
 */
import type { Message } from "../../conversation/conversation";
import type { AguiEvent } from "./agui/events";

/**
 * What a message of ours that the server has never seen is called.
 *
 * A send puts the question on the screen before the turn has been accepted,
 * and until the conversation is read again that message has an id no route
 * would answer for. The prefix is how everything that would put an id into a
 * request -- hanging a message under one -- tells the two apart. No id the
 * backend issues holds a colon (they are uuids).
 */
export const UNSENT = "unsent:";

/** Whether that is a message the server has never been told about. */
export function isUnsent(id: string | null): boolean {
  return id !== null && id.startsWith(UNSENT);
}

/** A piece of what a message says: what it said, or what it thought. */
export interface ChatTextPart {
  kind: "text" | "reasoning";
  /**
   * The id of a stretch of thinking, which is how the event that ends it
   * finds it. A stretch has one because a turn may think more than once and
   * each one is a message of its own on the wire (`api/agui.py`).
   */
  id?: string;
  text: string;
}

/**
 * A call the answer made, and what came back.
 *
 * Everything in it is text the model or a tool wrote -- the name the model
 * chose, the arguments it streamed, the tool's answer -- and it is rendered
 * as data, never as markup (`docs/specs/wire.md`).
 */
export interface ChatToolCall {
  kind: "tool-call";
  /** The call's id: the vendor's, carried as data, and what its result names. */
  id: string;
  /** The tool's full name, `<server>__<tool>`. */
  name: string;
  /** The arguments as the model wrote them, JSON text, growing as they stream. */
  argsText: string;
  /** The arguments as data: read from the store, or parsed once the call is whole. */
  args?: Record<string, unknown>;
  /** What the tool answered, once it has. */
  result?: string;
  /** Whether the tool said it failed. */
  isError?: boolean;
}

/** One part of a message, of the three kinds the chat draws. */
export type ChatPart = ChatTextPart | ChatToolCall;

/** How a message stands. */
export type ChatMessageState = "stored" | "running" | "cancelled" | "failed";

/** One message, as the chat holds one. */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  parts: ChatPart[];
  state: ChatMessageState;
  /** The sentence to show on a message whose run failed. */
  detail?: string;
  /**
   * The tool message that answered this answer's calls, if one has.
   *
   * Not a message of the thread on the screen -- its results are drawn on
   * the calls that made them -- but one of the conversation's, and the one a
   * message after it hangs under (`docs/specs/conversations.md`). So it is
   * what a new question or an edit names as its parent (`under`,
   * `storedParent`), and where a cut after this answer is made (`upTo`).
   */
  resultsId?: string;
}

/** The whole of it. */
export interface ChatState {
  /** Which conversation this is of; `null` on an empty chat. */
  conversationId: string | null;
  /** The one call that opens a conversation has not answered yet. */
  loading: boolean;
  /** Why it could not be opened, and whether that was a 404. */
  failure: { detail: string; missing: boolean } | null;
  /** The thread, oldest first, and the message being produced at its end. */
  messages: ChatMessage[];
  /** The run in flight. */
  runId: string | null;
  /**
   * A turn has been asked for and its run is not known yet.
   *
   * `runId` alone would leave the moment between the send and the response's
   * headers looking idle, which is the moment a person is most likely to
   * press send again.
   */
  sending: boolean;
  /** The assistant message the stream is writing, if one is open. */
  writing: string | null;
  /** The stretch of thinking that is open, if one is. */
  thinking: string | null;
  /** What to say about the last run when it did not end well. */
  ended: string | null;
  /**
   * Something said to the person, which only the person clears.
   *
   * Apart from `ended`, which is about a run and goes when the next one
   * starts. A notice is about **what they just did** -- a message that was
   * not sent, a stop that did not reach the server -- and a run starting
   * milliseconds later must not wipe it, or the only trace of a message that
   * went nowhere disappears before it can be read. What clears it is the
   * next turn they successfully ask for, or leaving the conversation.
   */
  notice: string | null;
  /**
   * The thread as it was before the turn on its way changed it.
   *
   * A turn the server refuses is one that never happened, so what it did to
   * the screen is undone: an edit that cut the thread and put a question at
   * its end, a regeneration that cut an answer off, a retry that replaced
   * the question nobody answered. The snapshot is what was there, and it is
   * put back as it was rather than reconstructed.
   */
  before: ChatMessage[] | null;
}

export const EMPTY: ChatState = {
  conversationId: null,
  loading: false,
  failure: null,
  messages: [],
  runId: null,
  sending: false,
  writing: null,
  thinking: null,
  ended: null,
  notice: null,
  before: null,
};

/**
 * How a run that ended badly is said, one fixed sentence per state.
 *
 * A `Map` rather than an object, as the sign-in errors are and for the same
 * reason: the states are the API's closed set today, and a build that met one
 * it did not know must say that something went wrong rather than render
 * whatever an object inherited under that name.
 */
export const ENDED_BADLY = new Map<string, string>([
  ["cancelled", "This answer was stopped before it was finished."],
  [
    "failed",
    "This answer did not finish: something went wrong while it was being produced.",
  ],
  [
    "interrupted",
    "This answer was interrupted when the server stopped. Sending the message again is how it is retried.",
  ],
]);

/**
 * The same, for the `code` of an AG-UI `RUN_ERROR` (`docs/specs/wire.md`).
 *
 * The event carries a sentence of the backend's as well, and this is used
 * instead of it: what the backend sends is written for a reader of a stock
 * AG-UI client, and the two states it shares with `ENDED_BADLY` should read
 * the same here whether they arrived in a stream or in a reload.
 */
export const RUN_ERRORS = new Map<string, string>([
  ["failed", ENDED_BADLY.get("failed") ?? ""],
  ["interrupted", ENDED_BADLY.get("interrupted") ?? ""],
  [
    "quiet",
    "This answer stopped saying anything and was given up on. Sending the message again is how it is retried.",
  ],
  [
    "gone",
    "This answer is no longer there. Open the conversation again to see what is.",
  ],
  ["internal", "Something went wrong while this answer was being produced."],
]);

const ENDED_SOMEHOW = "This answer did not finish.";

/**
 * What a second turn asked for while one is on its way is told.
 *
 * **One run at a time** (`docs/specs/runs.md`). It says that the message was
 * not sent, because the box has already cleared it: assistant-ui empties the
 * composer when it hands a message over, and a turn that went nowhere with a
 * box that went empty would be a message somebody thinks they sent.
 */
export const ONE_AT_A_TIME =
  "This conversation is already answering, and it takes one turn at a time. That message was not sent.";

/**
 * The same, for asking for an answer again.
 *
 * Without the last sentence: a regeneration carries no message
 * (`docs/specs/conversations.md`), so there is nothing that was not sent and
 * nothing to put back into the box.
 */
export const ONE_AT_A_TIME_ANSWER =
  "This conversation is already answering, and it takes one turn at a time.";

/**
 * What a stop that did not reach the server is told.
 *
 * **The run is not affected by it**, and neither is the stream watching it:
 * the request failed, so nothing was cancelled, and the answer carries on
 * arriving. Saying that the connection to the answer was lost would be
 * exactly backwards.
 */
export const STOP_DID_NOT_ARRIVE =
  "The stop did not reach the server, so the answer is still arriving.";

/** That code's sentence, and one for a code this build does not know. */
export function saidFor(code: string): string {
  return RUN_ERRORS.get(code) ?? ENDED_SOMEHOW;
}

/**
 * What a new question goes after: the end of the thread, or just before it.
 *
 * Usually the end itself. **Unless the end is a question nobody answered** --
 * which is what a run that failed, was cancelled or was interrupted leaves
 * behind, since the answer it was producing is in no conversation
 * (`docs/specs/runs.md`) -- and then it is the message before that question.
 * Two reasons, and they are the same reason: the format refuses a message of
 * role `user` under another (`InvalidMessageTreeError`,
 * `docs/specs/conversations.md`), and **asking again is how such a turn is
 * retried**. The question that went unanswered comes off the screen, and
 * the new one stands where it stood.
 */
export function under(state: ChatState): string | null {
  const tail = state.messages.at(-1);
  if (tail === undefined) return null;
  const parent = tail.role === "user" ? state.messages.at(-2) : tail;
  return parent === undefined ? null : storedEnd(parent);
}

/**
 * What a new message under `parentId` hangs under in the store.
 *
 * The runtime names the message before it on the screen; where that is an
 * answer whose calls a tool message answered, the store's parent is that
 * tool message (`ChatMessage.resultsId`), and an edit or a question sent
 * under the answer itself would leave the results off the path the model
 * sees.
 */
export function storedParent(
  state: ChatState,
  parentId: string | null,
): string | null {
  if (parentId === null) return null;
  const parent = find(state, parentId);
  return parent === null ? parentId : storedEnd(parent);
}

/** The last message of the store's at that one: its tool message, or itself. */
function storedEnd(message: ChatMessage): string {
  return message.resultsId ?? message.id;
}

/** Everything that can change the chat. */
export type ChatAction =
  /** An empty chat: nothing opened, nothing being read. */
  | { kind: "cleared" }
  /** That conversation is being read. */
  | { kind: "opening"; conversationId: string }
  /** It was read: this is what the server says it is. */
  | {
      kind: "opened";
      conversationId: string;
      messages: readonly Message[];
      runId: string | null;
      /** How the last run ended, when it ended badly. */
      endedBadly: string | null;
    }
  /** It could not be read. */
  | { kind: "unopened"; detail: string; missing: boolean }
  /**
   * Somebody sent a message: it is on the screen before the server has it.
   *
   * `after` is the message it goes after, and everything after **that** is
   * cut: nothing for a first message or an edit of one, the end of the
   * thread for a plain send, the message before the edited one for an edit.
   */
  | { kind: "asked"; id: string; after: string | null; text: string }
  /** An answer is to be produced again: it and everything after it are cut. */
  | { kind: "again"; after: string | null }
  /** The stream is open: this is the run it is of. */
  | { kind: "started"; runId: string; conversationId: string }
  /** One event of that run. */
  | { kind: "event"; event: AguiEvent }
  /** The stream could not be picked up again: nothing is watching the run. */
  | { kind: "lost"; detail: string }
  /** Something to say, and nothing else about the conversation changes. */
  | { kind: "told"; detail: string }
  /**
   * The turn was refused before it began, so nothing about the run changed.
   *
   * Told apart from `lost` because a conversation that is already answering
   * refuses a second turn (409) **while the first is still going**: forgetting
   * the run it is watching over a turn it never started would leave the answer
   * arriving into a thread that thinks nothing is happening.
   */
  | { kind: "refused"; detail: string };

export function reduce(state: ChatState, action: ChatAction): ChatState {
  switch (action.kind) {
    case "cleared":
      return state === EMPTY || state.conversationId === null
        ? EMPTY
        : { ...EMPTY };
    case "opening":
      // A different conversation is a different page, not this one with other
      // messages: what is on the screen goes, so that nothing of the one
      // before it is ever shown under the new one's name -- a notice about
      // something done here included. Reading the same one again -- which is
      // what follows a turn -- keeps what is there until the answer arrives.
      return state.conversationId === action.conversationId
        ? { ...state, loading: true }
        : { ...EMPTY, conversationId: action.conversationId, loading: true };
    case "opened": {
      // **A message the store still says the same thing about is the message
      // this state already holds.** Identity is what the chat converts for
      // assistant-ui by (`./runtime.tsx`, `converted`), so rebuilding every
      // object on the read that ends a turn would convert a whole
      // conversation again -- and move every `createdAt` -- over one answer.
      const before = new Map(state.messages.map((each) => [each.id, each]));
      const messages = folded(action.messages).map((fresh) => {
        const already = before.get(fresh.id);
        return already !== undefined && unchanged(already, fresh)
          ? already
          : fresh;
      });
      return {
        ...state,
        conversationId: action.conversationId,
        loading: false,
        failure: null,
        messages,
        runId: action.runId,
        sending: false,
        writing: null,
        thinking: null,
        ended: action.endedBadly,
        before: null,
      };
    }
    case "unopened":
      // Nothing is being watched either: a conversation that could not be
      // read is one whose run, if it had one, this client is not following.
      // Leaving `runId` set would be a thread that says it is answering
      // behind a page that says it could not be opened.
      return {
        ...state,
        loading: false,
        runId: null,
        sending: false,
        writing: null,
        thinking: null,
        failure: { detail: action.detail, missing: action.missing },
      };
    case "asked": {
      const asked: ChatMessage = {
        id: action.id,
        role: "user",
        parts: [{ kind: "text", text: action.text }],
        state: "stored",
      };
      return {
        ...state,
        messages: [...upTo(state, action.after), asked],
        before: state.messages,
        sending: true,
        ended: null,
        // The person has asked for something that went out, so whatever they
        // were last told about a turn that did not is behind them.
        notice: null,
      };
    }
    case "again":
      // Nothing is added: a regeneration answers the question the turn
      // already had (`docs/specs/conversations.md`). The old answer comes off
      // the screen, and the new one arrives where it stood.
      return {
        ...state,
        messages: upTo(state, action.after),
        before: state.messages,
        sending: true,
        ended: null,
        notice: null,
      };
    case "started":
      return {
        ...state,
        conversationId: action.conversationId,
        runId: action.runId,
        sending: false,
        before: null,
        ended: null,
      };
    case "told":
      return { ...state, notice: action.detail };
    case "event":
      return applied(state, action.event);
    case "lost":
      // Nothing is watching the run, so nothing will say that the message it
      // was writing is over: it is closed here, or it spins for ever.
      // `cancelled` and not `failed`, because nothing is known to have gone
      // wrong with the answer -- only with the watching of it -- and the
      // sentence beside it says which (`LOST_TOUCH`).
      return { ...ending(state, "cancelled"), ended: action.detail };
    case "refused":
      // The thread goes back to what it was: the server refused the turn
      // that would have changed the conversation, so what that turn did to
      // the screen -- a question added, a cut made -- is undone as one.
      return {
        ...state,
        messages: state.before ?? state.messages,
        before: null,
        sending: false,
        ended: action.detail,
      };
  }
}

/**
 * The thread up to and including `after`; nothing when that is `null`.
 *
 * `after` may be a tool message's id, which is on the screen only as the
 * answer that holds it (`resultsId`): the cut is after that answer.
 */
function upTo(state: ChatState, after: string | null): ChatMessage[] {
  if (after === null) return [];
  const at = state.messages.findIndex(
    (message) => message.id === after || message.resultsId === after,
  );
  return at === -1 ? state.messages : state.messages.slice(0, at + 1);
}

/**
 * One AG-UI event.
 *
 * **The three no-ops of `docs/specs/wire.md` are here**, and they are what
 * lets a client re-attach without seeing anything twice: a `*_START` for a
 * message it already holds open, a `*_END` for one it does not hold, and the
 * terminal event of a run it has already seen end. Everything else arrives
 * once.
 *
 * It is also deliberately forgiving in the other direction -- content for a
 * message no start was seen for opens one -- because the alternative is an
 * answer that arrives and is not shown.
 */
function applied(state: ChatState, event: AguiEvent): ChatState {
  switch (event.type) {
    case "RUN_STARTED":
      // The run is already known from the response's headers, which arrive
      // before any event does. Re-attaching never replays this one.
      return state.runId === null ? { ...state, runId: event.runId } : state;

    case "TEXT_MESSAGE_START": {
      // **Already held: nothing to do.** Held *open* is the no-op a re-attach
      // in the middle of a message relies on; held complete is one this
      // client already has from the store, and either way an id stands for
      // one message and never for a second copy of it.
      if (find(state, event.messageId) !== null) return state;
      const started: ChatMessage = {
        id: event.messageId,
        role: event.role === "user" ? "user" : "assistant",
        parts: [],
        state: "running",
      };
      return {
        ...state,
        messages: [...state.messages, started],
        writing: started.id,
        thinking: null,
      };
    }

    case "TEXT_MESSAGE_CONTENT": {
      const open = find(state, event.messageId);
      // A message whose opening this client never saw: open it. Being
      // forgiving here is what keeps an answer that arrives from being
      // dropped over a bracket that went missing.
      if (open === null) {
        return applied(applied(state, opening(event.messageId)), event);
      }
      // One that is **already complete**: nothing to add to. It is the store's
      // now, and a delta arriving for it is a stream repeating something it
      // should not (`docs/specs/wire.md`: no delta is repeated) -- appending
      // would say the answer twice.
      if (open.state !== "running") return state;
      return appended(state, event.messageId, {
        kind: "text",
        text: event.delta,
      });
    }

    case "TEXT_MESSAGE_END": {
      const ending = find(state, event.messageId);
      // Not held: the no-op for an end a re-attach derived for a message this
      // client never had open.
      if (ending === null || ending.state !== "running") return state;
      return {
        ...state,
        messages: state.messages.map((message) =>
          message.id === event.messageId
            ? { ...message, state: "stored" }
            : message,
        ),
        writing: null,
        thinking: null,
      };
    }

    case "REASONING_MESSAGE_START": {
      if (state.thinking === event.messageId) return state;
      const writing = state.writing;
      // Thinking belongs to the answer that is open. The backend announces
      // the answer before anything it thinks (`application/turns.py`), so
      // there is nothing to do with a stretch that belongs to no message.
      if (writing === null) return state;
      return {
        ...state,
        thinking: event.messageId,
        messages: state.messages.map((message) =>
          message.id === writing
            ? {
                ...message,
                parts: [
                  ...message.parts,
                  { kind: "reasoning", id: event.messageId, text: "" },
                ],
              }
            : message,
        ),
      };
    }

    case "REASONING_MESSAGE_CONTENT": {
      const writing = state.writing;
      if (writing === null) return state;
      if (state.thinking !== event.messageId) {
        // A stretch whose opening this client did not see: open it, then
        // append. A re-attach derives the same id from the same position, so
        // this is that stretch and not a second one.
        return applied(
          applied(state, {
            type: "REASONING_MESSAGE_START",
            messageId: event.messageId,
          }),
          event,
        );
      }
      return appended(state, writing, {
        kind: "reasoning",
        id: event.messageId,
        text: event.delta,
      });
    }

    case "REASONING_MESSAGE_END":
      return state.thinking === event.messageId
        ? { ...state, thinking: null }
        : state;

    case "TOOL_CALL_START": {
      // The answer the call is part of: the one the event names, else the
      // one being written. Opened if this client never saw its start, as
      // content is; **already held: nothing to do**, as a message start
      // already held is -- which is the no-op a re-attach on a call relies
      // on (`docs/specs/wire.md`).
      const parent = event.parentMessageId ?? state.writing;
      if (parent === null) return state;
      const holder = find(state, parent);
      if (holder === null) {
        return applied(applied(state, opening(parent)), event);
      }
      if (
        holder.state !== "running" ||
        callOf(holder, event.toolCallId) !== null
      ) {
        return state;
      }
      return withMessage(state, parent, (message) => ({
        ...message,
        parts: [
          ...message.parts,
          {
            kind: "tool-call",
            id: event.toolCallId,
            name: event.toolCallName,
            argsText: "",
          },
        ],
      }));
    }

    case "TOOL_CALL_ARGS": {
      // For a call this client holds, in an answer still being written. A
      // delta for a call never announced is ignored -- there is no name to
      // open one under, and a re-attach always begins at or after the
      // call's start -- and one for an answer that is complete is a stream
      // repeating itself.
      const holder = holding(state, event.toolCallId);
      if (holder === null || holder.state !== "running") return state;
      return withCall(state, holder.id, event.toolCallId, (call) => ({
        ...call,
        argsText: call.argsText + event.delta,
      }));
    }

    case "TOOL_CALL_END": {
      // The arguments are whole: what they parse to is what the tool is
      // called with, and what the renderer is handed as data. Nothing else
      // changes, so an end for a call already ended changes nothing.
      const holder = holding(state, event.toolCallId);
      if (holder === null) return state;
      return withCall(state, holder.id, event.toolCallId, (call) => {
        if (call.args !== undefined) return call;
        const args = parsedArguments(call.argsText);
        return args === undefined ? call : { ...call, args };
      });
    }

    case "TOOL_CALL_RESULT": {
      // Drawn on the call that made it, wherever that is: the answer is
      // complete by the time a result lands, so it is not the message being
      // written. One result per call (`docs/specs/wire.md`), so a result for
      // a call that has one is a repeat. The tool message it is part of is
      // what the next message hangs under (`resultsId`).
      const holder = holding(state, event.toolCallId);
      if (holder === null) return state;
      if (callOf(holder, event.toolCallId)?.result !== undefined) return state;
      return withMessage(state, holder.id, (message) => ({
        ...message,
        resultsId: event.messageId,
        parts: message.parts.map((part) =>
          part.kind === "tool-call" && part.id === event.toolCallId
            ? { ...part, result: event.content, isError: event.isError }
            : part,
        ),
      }));
    }

    case "RUN_FINISHED": {
      // **The terminal event of a run this client has already seen end.**
      // Reachable only from a re-attach at or past the last position: a
      // stream of its own run sets `runId` before its first event (the
      // response's headers, `started`), so the first ending always lands on
      // a run this state knows about. A message still open is closed all the
      // same -- an ending is never something to ignore while something is
      // waiting to be told it is over.
      if (state.runId === null && !anyRunning(state)) return state;
      return {
        ...ending(state, event.cancelled ? "cancelled" : "stored"),
        ended: event.cancelled ? (ENDED_BADLY.get("cancelled") ?? null) : null,
      };
    }

    case "RUN_ERROR": {
      if (state.runId === null && !anyRunning(state)) return state;
      const said = saidFor(event.code);
      const after = ending(state, "failed", said);
      // A run that failed before it announced anything has no message to put
      // the sentence on, so the thread says it instead.
      return state.writing === null ? { ...after, ended: said } : after;
    }
  }
}

/** Whether an answer is still being written. */
function anyRunning(state: ChatState): boolean {
  return state.messages.some((message) => message.state === "running");
}

/** A `TEXT_MESSAGE_START` for a message whose start never arrived. */
function opening(messageId: string): AguiEvent {
  return { type: "TEXT_MESSAGE_START", messageId, role: "assistant" };
}

/** That message, or `null`. */
function find(state: ChatState, id: string): ChatMessage | null {
  return state.messages.find((message) => message.id === id) ?? null;
}

/** That call of that message, or `null`. */
function callOf(message: ChatMessage, callId: string): ChatToolCall | null {
  return (
    message.parts.find(
      (part): part is ChatToolCall =>
        part.kind === "tool-call" && part.id === callId,
    ) ?? null
  );
}

/**
 * The message that holds that call, or `null`.
 *
 * From the end, since a call belongs to the latest answer in every case
 * this build has; the ids are the vendor's and unique within a run.
 */
function holding(state: ChatState, callId: string): ChatMessage | null {
  for (let at = state.messages.length - 1; at >= 0; at -= 1) {
    const message = state.messages[at];
    if (message !== undefined && callOf(message, callId) !== null) {
      return message;
    }
  }
  return null;
}

/**
 * That message changed, and every other as it was.
 *
 * **The same state when nothing changed**: a change that hands the message
 * back as it was is a no-op, and a no-op is the same object, which is what
 * a re-attach's repeated event must leave behind (`docs/specs/wire.md`).
 */
function withMessage(
  state: ChatState,
  messageId: string,
  change: (message: ChatMessage) => ChatMessage,
): ChatState {
  let touched = false;
  const messages = state.messages.map((message) => {
    if (message.id !== messageId) return message;
    const changed = change(message);
    touched = touched || changed !== message;
    return changed;
  });
  return touched ? { ...state, messages } : state;
}

/** That call of that message changed, and everything else as it was. */
function withCall(
  state: ChatState,
  messageId: string,
  callId: string,
  change: (call: ChatToolCall) => ChatToolCall,
): ChatState {
  return withMessage(state, messageId, (message) => {
    let touched = false;
    const parts = message.parts.map((part) => {
      if (part.kind !== "tool-call" || part.id !== callId) return part;
      const changed = change(part);
      touched = touched || changed !== part;
      return changed;
    });
    return touched ? { ...message, parts } : message;
  });
}

/**
 * The arguments a call streamed, as data; `undefined` when they are not an
 * object -- which the backend never stores, and which the renderer is then
 * left to read from the text.
 */
function parsedArguments(
  argsText: string,
): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(argsText);
    return typeof parsed === "object" &&
      parsed !== null &&
      !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : undefined;
  } catch {
    return undefined;
  }
}

/**
 * `piece` added to that message: onto its last part, or as a new one.
 *
 * **Onto the last part and no other.** An answer that thinks, says something
 * and thinks again holds three parts in the order it produced them, and a
 * delta belongs to whichever is open. Adding text to an earlier text part
 * would put a sentence in front of the thinking that came before it.
 */
function appended(
  state: ChatState,
  messageId: string,
  piece: ChatTextPart,
): ChatState {
  return withMessage(state, messageId, (message) => {
    const last = message.parts[message.parts.length - 1];
    if (
      last !== undefined &&
      last.kind !== "tool-call" &&
      last.kind === piece.kind &&
      last.id === piece.id
    ) {
      return {
        ...message,
        parts: [
          ...message.parts.slice(0, -1),
          { ...last, text: last.text + piece.text },
        ],
      };
    }
    return { ...message, parts: [...message.parts, piece] };
  });
}

/** The run is over: nothing is being written, and the answer stands as it is. */
function ending(
  state: ChatState,
  how: ChatMessageState,
  detail?: string,
): ChatState {
  return {
    ...state,
    runId: null,
    sending: false,
    writing: null,
    thinking: null,
    // **Every message still open, not only the one being written.** One
    // answer at a time is the rule and the platform keeps it, so there is
    // never more than one; a run that ends leaving two would leave the second
    // spinning for ever, which is worse than closing a message twice.
    messages: state.messages.map((message) =>
      message.state === "running"
        ? {
            ...message,
            state: how,
            ...(detail === undefined ? {} : { detail }),
          }
        : message,
    ),
  };
}

/**
 * Whether the store is still saying exactly what this state already holds.
 *
 * Only what is drawn: who said it, and what it says. Nothing else of a
 * stored message is kept here.
 */
function unchanged(already: ChatMessage, fresh: ChatMessage): boolean {
  return (
    already.role === fresh.role &&
    already.state === fresh.state &&
    already.detail === fresh.detail &&
    already.resultsId === fresh.resultsId &&
    already.parts.length === fresh.parts.length &&
    already.parts.every((part, at) => {
      const other = fresh.parts[at];
      if (other === undefined) return false;
      if (part.kind === "tool-call" || other.kind === "tool-call") {
        return (
          part.kind === "tool-call" &&
          other.kind === "tool-call" &&
          part.id === other.id &&
          part.name === other.name &&
          part.argsText === other.argsText &&
          part.result === other.result &&
          part.isError === other.isError
        );
      }
      return (
        part.kind === other.kind &&
        part.id === other.id &&
        part.text === other.text
      );
    })
  );
}

/**
 * The thread as the chat holds it: every tool message folded into the
 * answer before it.
 *
 * A tool message is the store's (`docs/specs/conversations.md`) and no
 * bubble on the screen: its results are drawn on the calls that made them,
 * and the answer remembers it as what the next message hangs under
 * (`resultsId`). One that follows no answer -- which the format does not
 * allow -- is dropped rather than drawn under nothing.
 */
function folded(messages: readonly Message[]): ChatMessage[] {
  const thread: ChatMessage[] = [];
  for (const message of messages) {
    if (message.role !== "tool") {
      thread.push(held(message));
      continue;
    }
    const answer = thread.at(-1);
    if (answer === undefined) continue;
    thread[thread.length - 1] = answered(answer, message);
  }
  return thread;
}

/** That answer with the results of that tool message on its calls. */
function answered(answer: ChatMessage, results: Message): ChatMessage {
  const found = new Map(
    results.parts.flatMap((part) =>
      part.kind === "tool_result" ? [[part.call_id, part] as const] : [],
    ),
  );
  return {
    ...answer,
    resultsId: results.id,
    parts: answer.parts.map((part) => {
      const result = part.kind === "tool-call" ? found.get(part.id) : undefined;
      return result === undefined
        ? part
        : { ...part, result: result.text, isError: result.is_error };
    }),
  };
}

/** One message of the API's, as the chat holds one; never a tool message. */
function held(message: Message): ChatMessage {
  return {
    id: message.id,
    role: message.role === "user" ? "user" : "assistant",
    // Reasoning is shown and never stored (`docs/specs/conversations.md`), so
    // a message that came out of the store has text and calls in it and
    // nothing else; a result is folded into its call (`answered`).
    parts: message.parts.flatMap((part): ChatPart[] => {
      if (part.kind === "text") return [{ kind: "text", text: part.text }];
      if (part.kind === "tool_call") {
        const args = part.arguments ?? {};
        return [
          {
            kind: "tool-call",
            id: part.call_id,
            name: part.name,
            argsText: JSON.stringify(args),
            args,
          },
        ];
      }
      return [];
    }),
    state: "stored",
  };
}
