// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The chat's runtime: our state, the four turns, and what the boxes say.
 *
 * Every message and every byte of a stream is ours (`./state.ts`), and so is
 * everything that is done about them: a send, an edit, a regeneration, a stop
 * and a resume are each one of the wire's turns (`docs/specs/wire.md`), made
 * here and nowhere else. What the components under `./thread/` draw is
 * `ChatMessage` and `ChatPart` as the reducer holds them, with no layer of
 * conversion between; what they do is call the functions below, which they
 * reach through `ChatContext`.
 *
 * - `send` is a new message: under the end of the thread, or in place of a
 *   question nobody answered (`askedIn`).
 * - `edit` is a new message under the parent of the question it replaces;
 *   the thread on the screen is cut after that parent and goes on from it.
 * - `regenerate` cuts the answer's turn off and produces it again; on an
 *   answer that failed, the same button is Retry (`docs/specs/ui.md`).
 * - `cancel` asks the server to stop the run; the stream says when it has.
 * - `resume` goes on with a failed answer's turn from the rounds saved.
 *
 * **The boxes are ours too.** What is typed into the main box, and into the
 * box of a question being edited, is held here rather than in the components
 * that draw them, because a turn the server refuses puts what was written
 * back -- into the box it came from (`turnsOf`, `wanted`) -- and only the
 * thing that made the turn knows it was refused.
 *
 * **Running is a run this chat knows the id of**, and nothing else. Between a
 * send and its response's headers there is nothing to stop, so the box goes
 * on offering to send, and a second send is refused here and said
 * (`ONE_AT_A_TIME`) rather than stopping anything.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";

import {
  ApiError,
  detailOf,
  isRefusal,
  MODEL_NOT_OFFERED,
  request,
  UNKNOWN_MODEL,
} from "../api/client";
import { cancelRun, type Opened } from "../conversation/conversation";
import type { ChatProps } from "./index";
import {
  attach,
  startNewConversation,
  startTurn,
  type Attached,
} from "./agui/client";
import {
  askedIn,
  EMPTY,
  ENDED_BADLY,
  isUnsent,
  lastRead,
  ONE_AT_A_TIME,
  ONE_AT_A_TIME_ANSWER,
  reduce,
  sameRead,
  STOP_DID_NOT_ARRIVE,
  storedParent,
  turnStart,
  under,
  UNSENT,
  type ChatAction,
  type ChatMessage,
  type ChatState,
  type Read,
} from "./state";

/** What is said when there is nobody to talk to. */
export const NO_AGENT =
  "This deployment has no agent configured, so there is nobody to send this to.";

/** What a second turn in a conversation that is already answering is told. */
export const STILL_ANSWERING =
  "This conversation is still answering. Stop that answer before sending another.";

/**
 * What a turn refused in a conversation whose model has gone is told
 * (`MODEL_NOT_OFFERED`).
 *
 * Still true once another model has been picked: it is about the turn that
 * was refused, and it stays until the next one, as every refusal does.
 */
export const MODEL_GONE =
  "This conversation's model is no longer offered here, so the turn was refused. Pick another model above and try again.";

/**
 * What a first message refused for its model is told (`UNKNOWN_MODEL`).
 *
 * The model is one the page picked from a list that has gone stale, or one
 * this browser remembered before the list came. The shell forgets it
 * (`onModelRefused`), but a list gone stale may offer it again under another
 * name -- the agent's default, say -- so what is promised is a pick or a
 * reload, which are always true.
 */
export const MODEL_GONE_NEW_CHAT =
  "This chat was not started: the model it was for is no longer offered here. Pick another model above, or reload the page.";

/**
 * What a first message refused as not there is told.
 *
 * On a new chat there is no conversation to be missing, and the backend
 * looks for the agent before anything else (`docs/specs/wire.md`): a 404 is
 * the agent, gone from a list this page fetched before the operator changed
 * it.
 */
export const AGENT_GONE =
  "This chat was not started: the agent it was for is no longer offered here. Pick another agent above, or reload the page.";

/**
 * What is said when the answer could not be followed to its end.
 *
 * One sentence for every way a **watch** ends badly -- a connection nobody
 * could pick up again, a run that is no longer there, a stream that sent
 * something we do not read -- because what a person can do about them is the
 * same thing, and none of the details is about them. What the *turn* itself
 * was refused with is said as the backend said it (`said`).
 */
export const LOST_TOUCH =
  "The connection to this answer was lost. Opening the conversation again shows what was stored.";

/**
 * The sentence for a refusal, in the chat's own words where it has any.
 *
 * A model that is not offered is told as what to do about it. A conversation
 * already answering is the one refusal a person causes by doing something
 * reasonable, and the backend's own detail for it names a run and a
 * conversation by id, for an operator's log (`api/errors.py`); it shares its
 * 409 with the model's, so the name is what is read. Everything else is the
 * backend's sentence, which is already written for a reader.
 */
function said(failure: unknown): string {
  if (isRefusal(failure, MODEL_NOT_OFFERED)) return MODEL_GONE;
  if (isRefusal(failure, UNKNOWN_MODEL)) return MODEL_GONE_NEW_CHAT;
  if (failure instanceof ApiError && failure.status === 409) {
    return STILL_ANSWERING;
  }
  return detailOf(failure);
}

/** Whether a turn was refused for its model, which is to be picked again. */
function forItsModel(failure: unknown): boolean {
  return (
    isRefusal(failure, MODEL_NOT_OFFERED) || isRefusal(failure, UNKNOWN_MODEL)
  );
}

/** A box of the chat's: what it says, and how to change that. */
interface ChatBox {
  text: string;
  setText: (text: string) => void;
}

/** The question being edited, and what its box says. */
export interface Editing {
  id: string;
  text: string;
}

/**
 * What the chat offers the components that draw it.
 *
 * `state` is the reducer's, read as it is. The rest is either worked out from
 * it once, here, so that no component works it out a second way (`running`,
 * `sendDisabled`), or is something a control does.
 */
export interface Chatting {
  state: ChatState;
  /** A run this chat knows the id of is in flight: what stop is offered for. */
  running: boolean;
  /**
   * There is nobody to send a first message to, or the conversation is still
   * being read; the box still takes text.
   */
  sendDisabled: boolean;
  /** The main box. */
  composer: ChatBox;
  /** Send what the main box says, and empty it; nothing when it may not. */
  submit: () => void;
  /** The question being edited, while its box is open. */
  editing: Editing | null;
  /** Open that question's box, with what it says in it. */
  beginEdit: (messageId: string) => void;
  /** Change what the open edit box says. */
  setEditText: (text: string) => void;
  /** Close the edit box without sending. */
  cancelEdit: () => void;
  /** Send what the open edit box says, and close it. */
  submitEdit: () => void;
  /** A new message, as typed. */
  send: (text: string) => Promise<void>;
  /** That question replaced by this text. */
  edit: (messageId: string, text: string) => Promise<void>;
  /** That answer produced again, or retried when it failed. */
  regenerate: (answerId: string) => Promise<void>;
  /** A failed answer gone on with from the rounds its engine saved. */
  resume: (answerId: string) => Promise<void>;
  /** Stop the run in flight. */
  cancel: () => Promise<void>;
}

/** The chat, for the components under `./thread/`. */
export const ChatContext = createContext<Chatting | null>(null);

/** The chat this component is drawn in; drawn outside one is a bug. */
export function useChatContext(): Chatting {
  const chat = useContext(ChatContext);
  if (chat === null) throw new Error("This is drawn outside a <Chat>.");
  return chat;
}

/**
 * The message before that one on the screen, which is where an edit of it or
 * a regeneration of it cuts the thread; `null` for the first message, and for
 * one that is not there.
 */
export function parentOf(
  messages: readonly ChatMessage[],
  messageId: string,
): string | null {
  const at = messages.findIndex((message) => message.id === messageId);
  return at > 0 ? (messages[at - 1]?.id ?? null) : null;
}

/** That edit box, if the question it is open on is on the screen. */
function openOn(
  editing: Editing | null,
  messages: readonly ChatMessage[],
): Editing | null {
  return editing !== null &&
    messages.some((message) => message.id === editing.id)
    ? editing
    : null;
}

/** What a message says in words, as Copy copies it: a blank line between. */
export function textOf(message: ChatMessage): string {
  return message.parts
    .flatMap((part) => (part.kind === "text" ? [part.text] : []))
    .join("\n\n");
}

export function useChat(props: ChatProps): Chatting {
  const [state, dispatch] = useReducer(reduce, EMPTY);

  // Made once, and what it holds is its own: `dispatch` never changes, and
  // everything a turn reads while it runs -- the props, the state, the
  // stream being watched -- is written into it by the effect below. A turn
  // is begun from a click and finishes minutes later, so it has to read
  // whatever is current then rather than what was current when it was made.
  // They also call one another: a turn ends in a reading, and a reading of a
  // conversation with a run going attaches to it.
  //
  // A layout effect, so that it is written before anything just drawn can be
  // clicked: a click that landed before a passive effect had run would begin
  // its turn on the state before -- a regeneration of an answer the screen
  // shows as failed, say, where the screen is offering a retry.
  const [turns] = useState(() => turnsOf(dispatch, props));
  useLayoutEffect(() => {
    turns.now(props, state);
  });

  // Which conversation is on the screen. Reading one is one call
  // (`docs/specs/conversations.md`), and leaving one abandons it.
  const conversationId = props.conversationId;
  useEffect(() => {
    // A conversation this chat created itself is already on the screen, and
    // reading it again would put "Loading…" over an answer that is arriving.
    if (conversationId !== null && turns.began(conversationId)) return;
    turns.stop();
    if (conversationId === null) {
      dispatch({ kind: "cleared" });
      return;
    }
    const dropped = new AbortController();
    dispatch({ kind: "opening", conversationId });
    void turns.read(conversationId, dropped.signal);
    return () => {
      dropped.abort();
    };
  }, [conversationId, turns]);

  // **Something changed elsewhere** (`ChatProps.changes`): the open
  // conversation is read again, and `refresh` decides whether that redraws
  // anything. The count it was mounted with is not a change.
  const changes = props.changes ?? 0;
  const seenChanges = useRef(changes);
  useEffect(() => {
    if (changes === seenChanges.current) return;
    seenChanges.current = changes;
    void turns.refresh();
  }, [changes, turns]);

  // Going away stops watching: the stream is a view of the run, and closing
  // it changes nothing about the run (`docs/specs/runs.md`).
  useEffect(() => {
    return () => {
      turns.stop();
    };
  }, [turns]);

  // **The two boxes.** Each is state, for the components to draw, and a ref
  // beside it, for the functions below to read: a send reads what the box
  // says at the moment it is pressed, and those functions are made once, so
  // that a keystroke does not hand every message on the screen a new one.
  const [text, setTextState] = useState("");
  const textRef = useRef(text);
  const setText = useCallback((next: string) => {
    textRef.current = next;
    setTextState(next);
  }, []);
  const [editingState, setEditingState] = useState<Editing | null>(null);
  const editingRef = useRef(editingState);
  const setEditing = useCallback((next: Editing | null) => {
    editingRef.current = next;
    setEditingState(next);
  }, []);

  // **Running is a run, and not a turn that has been asked for.** Between the
  // send and the response's headers there is nothing to stop, and a stop
  // offered there would be a stop of nothing; while `sending`, a second send
  // is refused instead (`turnsOf`, `busy`).
  const running = state.runId !== null;
  // There is nothing to send a first message to when the deployment has no
  // agent, and nothing yet to send one under while a conversation is being
  // read for the first time: where it would go is the end of a thread that
  // has not arrived (`askedIn`). The box still takes what is typed into it
  // either way, and keeps it, rather than emptying itself for a send that
  // does not happen. The read that follows a turn is a read too, but the
  // thread is already here then, so a send under it goes where it should.
  const sendDisabled =
    (state.conversationId === null && props.agentId === null) ||
    (state.conversationId !== null &&
      state.loading &&
      state.messages.length === 0);
  // What a control reads when it is used. Written as the render is put on the
  // screen, before anything on it can be clicked: a passive effect may still
  // be pending when a click lands, and Edit would then open on nothing.
  const held = useRef({ running, sendDisabled, messages: state.messages });
  useLayoutEffect(() => {
    held.current = { running, sendDisabled, messages: state.messages };
  });

  const submit = useCallback(() => {
    const written = textRef.current;
    const { running: answering, sendDisabled: nobody } = held.current;
    if (written.trim() === "" || answering || nobody) return;
    // The box empties itself as it hands the message over, which is what a
    // send looks like; a turn refused for being a second one puts it back.
    setText("");
    void turns.onNew(written);
  }, [setText, turns]);

  const beginEdit = useCallback(
    (messageId: string) => {
      const message = held.current.messages.find(
        (each) => each.id === messageId,
      );
      if (message === undefined) return;
      setEditing({ id: messageId, text: textOf(message) });
    },
    [setEditing],
  );
  const setEditText = useCallback(
    (next: string) => {
      const open = editingRef.current;
      if (open !== null) setEditing({ ...open, text: next });
    },
    [setEditing],
  );
  const cancelEdit = useCallback(() => {
    setEditing(null);
  }, [setEditing]);
  const submitEdit = useCallback(() => {
    const open = editingRef.current;
    if (open === null || open.text.trim() === "") return;
    // Closed as it is sent, as the main box is emptied: an edit refused for
    // a reason the person can act on opens again with its text (`follow`).
    setEditing(null);
    void turns.onEdit(open.id, open.text);
  }, [setEditing, turns]);

  // **A box put back.** What a refused turn wants a box to say is held in
  // `turnsOf` (`wanted`) and written here, after the render that follows the
  // refusal -- which is the render that put the message an edit was of back
  // on the screen. It never overwrites anything the person has typed since.
  useEffect(() => {
    const wanted = turns.saying();
    if (wanted === null) return;
    const editing = wanted.editing;
    // Only while that message is still in the thread: a read landing in
    // between may have taken it off, and the text then goes to the main box,
    // which is still somewhere.
    const there = state.messages.some((message) => message.id === editing);
    if (editing !== null && there) {
      // An edit goes back into its own box, open on the message it was
      // editing, so that sending it again is still an edit and not a new
      // message at the end of the thread. If a box has been opened since,
      // the text goes into it only while it is that message's and empty, and
      // otherwise on to the main box below: it is handed over, not dropped.
      const open = openOn(editingRef.current, state.messages);
      if (open === null || (open.id === editing && open.text === "")) {
        setEditing({ id: editing, text: wanted.text });
        return;
      }
    }
    if (textRef.current === "") setText(wanted.text);
  });

  // A question that has left the thread has no box to be open on: an edit,
  // a regeneration or another conversation took it off the screen. Only a
  // refused turn brings a message back, and then what was being written in
  // its box comes back with it rather than being lost.
  const editing = openOn(editingState, state.messages);

  return useMemo(
    () => ({
      state,
      running,
      sendDisabled,
      composer: { text, setText },
      submit,
      editing,
      beginEdit,
      setEditText,
      cancelEdit,
      submitEdit,
      send: turns.onNew,
      edit: turns.onEdit,
      regenerate: turns.onReload,
      resume: turns.onResume,
      cancel: turns.onCancel,
    }),
    [
      state,
      running,
      sendDisabled,
      text,
      setText,
      submit,
      editing,
      beginEdit,
      setEditText,
      cancelEdit,
      submitEdit,
      turns,
    ],
  );
}

/**
 * Everything the chat does to the API, made once.
 *
 * A closure rather than a set of hooks: what these read is plainly the four
 * values below, which the component keeps current, and nothing a render
 * happened to leave behind.
 */
function turnsOf(dispatch: (action: ChatAction) => void, first: ChatProps) {
  let props = first;
  let state = EMPTY;
  /** The stream being watched; aborted when another starts or this goes away. */
  let watching: AbortController | null = null;
  /** The run that stream is of, while it is still being read. */
  let watchingRun: string | null = null;
  /** What the last read of the conversation found (`sameRead`). */
  let shown: Read | null = null;
  /** The re-read a change asked for, while it is in the air. */
  let refreshing: AbortController | null = null;
  /**
   * The turns whose request is still in the air.
   *
   * A stream is adopted only once its request has been answered (`follow`),
   * so between the two there is a watcher-to-be that `watching` does not name
   * -- and leaving a conversation while one is in the air must stop it too,
   * or a stream would begin for a page nobody is on.
   */
  const starting = new Set<AbortController>();
  /** A conversation this chat created itself, which the shell then routes to. */
  let ours: string | null = null;
  /**
   * What a box must be made to say, once, after the next render.
   *
   * The box empties itself when it hands a message over, so a turn refused
   * here for being a second one leaves nothing behind at all. The text goes
   * back, and a notice says why (`ONE_AT_A_TIME`). `editing` is the message
   * an edit was of, whose own box it goes back into; `null` is the main one.
   */
  type Wanted = { text: string; editing: string | null };
  let wanted: Wanted | null = null;

  /** What a box must be made to say, once. */
  function saying(): Wanted | null {
    const held = wanted;
    wanted = null;
    return held;
  }

  /** What a turn begun from now on reads. Written after every render. */
  function now(current: ChatProps, held: ChatState): void {
    props = current;
    state = held;
  }

  /**
   * Whether a turn is already on its way, and so no second one may be.
   *
   * **One run at a time** (`docs/specs/runs.md`), and the backend says so
   * with a 409. Refusing here as well is not belt and braces: it is what
   * keeps at most one question on the screen that the server has not been
   * told about, so a turn that *is* refused can be taken back off it without
   * having to work out which of several it was.
   *
   * The thread does not offer either -- the box shows stop while a run is
   * going, and the action bars are not drawn -- so this is reachable only by
   * calling these directly, or by being very quick between a send and its
   * answer's headers.
   */
  function busy(): boolean {
    return state.runId !== null || state.sending;
  }

  /**
   * Say that the turn was not sent, because the box has already cleared it.
   *
   * The box empties itself when it hands a message over, so a turn dropped
   * in silence is a message somebody believes they sent (`ONE_AT_A_TIME`).
   * `told` changes nothing else: the turn that *is* on its way keeps its
   * question, its run and its stream.
   */
  function told(text: string, editing: string | null = null): void {
    dispatch({ kind: "told", detail: ONE_AT_A_TIME });
    // The box cleared itself when it handed this over, so without this the
    // message is gone and the notice is all there is. An edit goes back into
    // its own box, so that sending it again still replaces that message.
    if (text !== "") wanted = { text, editing };
  }

  /**
   * Whether reading that conversation would put "Loading…" over it.
   *
   * True only for one this chat **created and is still showing**. Both
   * halves matter: `ours` alone would skip the read for a conversation this
   * chat created and then left, and the state alone cannot tell "already
   * shown" from "about to be read".
   */
  function began(conversationId: string): boolean {
    return conversationId === ours && state.conversationId === conversationId;
  }

  /** Stop watching, and forget the conversation this chat began. */
  function stop(): void {
    ours = null;
    shown = null;
    refreshing?.abort();
    refreshing = null;
    watching?.abort();
    watching = null;
    watchingRun = null;
    for (const beginning of starting) beginning.abort();
    starting.clear();
  }

  /**
   * Read that conversation and show what the server says it is.
   *
   * **A read that finds a run in flight watches it**, whichever read it is.
   * Opening a conversation is the obvious one (`docs/specs/runs.md`), and the
   * read that ends a turn is the one that matters: another tab may have begun
   * a run in the meantime, and taking its id without watching it would leave
   * this thread answering for ever with nothing reading the answer.
   */
  async function read(
    conversationId: string,
    signal: AbortSignal,
    afterLoss = false,
  ): Promise<void> {
    let opened;
    try {
      opened = await request("get", "/api/conversations/{conversation_id}", {
        path: { conversation_id: conversationId },
        signal,
      });
    } catch (failure) {
      if (signal.aborted) return;
      dispatch({
        kind: "unopened",
        detail: said(failure),
        missing: failure instanceof ApiError && failure.status === 404,
      });
      return;
    }
    if (signal.aborted) return;
    show(conversationId, opened, afterLoss);
  }

  /** Show what a read of the conversation found, and watch a run it found. */
  function show(
    conversationId: string,
    opened: Opened,
    afterLoss = false,
  ): void {
    shown = lastRead(conversationId, opened);
    props.onConversationOpened?.(opened.conversation);
    dispatch({
      kind: "opened",
      conversationId,
      messages: opened.messages,
      runId: opened.run_id,
      endedBadly:
        opened.ended_badly === null
          ? null
          : (ENDED_BADLY.get(opened.ended_badly.state) ?? null),
      endedState: opened.ended_badly === null ? null : opened.ended_badly.state,
    });
    // Every complete message has just been loaded, so attaching at
    // `resume.after` replays exactly the one still being produced and none
    // that is already on the screen.
    const runId = opened.run_id;
    const resume = opened.resume;
    if (runId !== null && resume !== null) {
      void follow(
        (watched) =>
          attach(conversationId, runId, resume.after, { signal: watched }),
        {
          watch: true,
          afterLoss,
        },
      );
    }
  }

  /**
   * Whether nothing here is under way that a re-read could disturb: no turn
   * being sent or watched, and no read of the conversation in the air.
   */
  function quiet(): boolean {
    return (
      state.runId === null &&
      !state.sending &&
      !state.loading &&
      starting.size === 0 &&
      watchingRun === null
    );
  }

  /**
   * Read the open conversation again because something changed elsewhere.
   *
   * **It disturbs nothing.** While a turn of this chat is being sent or
   * watched, its own stream and the read that ends it are the truth, so
   * nothing is asked. An answer that finds the last message and the running
   * turn as they were redraws nothing: the boxes, the edit box and the
   * scroll position stay as they are either way, and a redraw would only
   * redo what is on the screen. A conversation deleted elsewhere is said as
   * not here; any other failure of a re-read is left unsaid, and the next
   * change asks again.
   */
  async function refresh(): Promise<void> {
    const conversationId = state.conversationId;
    if (conversationId === null || state.failure !== null || !quiet()) return;
    refreshing?.abort();
    const control = new AbortController();
    refreshing = control;
    let opened: Opened;
    try {
      opened = await request("get", "/api/conversations/{conversation_id}", {
        path: { conversation_id: conversationId },
        signal: control.signal,
      });
    } catch (failure) {
      if (control.signal.aborted || refreshing !== control) return;
      refreshing = null;
      if (failure instanceof ApiError && failure.status === 404 && quiet()) {
        dispatch({ kind: "unopened", detail: said(failure), missing: true });
      }
      return;
    }
    if (control.signal.aborted || refreshing !== control) return;
    refreshing = null;
    if (state.conversationId !== conversationId || !quiet()) return;
    if (sameRead(shown, lastRead(conversationId, opened))) return;
    show(conversationId, opened);
  }

  /**
   * Open a stream and read it to its end, then read the conversation again.
   *
   * The promise is resolved once the **stream is open**, not once the run has
   * finished: what a caller waits for is whether the turn was accepted, and a
   * refusal is a status that comes before the stream (`docs/specs/wire.md`).
   * The answer then arrives event by event.
   *
   * `watch` tells the two callers apart, and what it decides is what a
   * refusal means. A **turn** somebody asked for that is refused leaves the
   * run alone -- a conversation that is already answering refuses a second
   * turn (409) while the first is still going. A **watch** that is refused is
   * nobody watching the run at all, which is a different thing to say and
   * must not leave the thread waiting for an answer nothing is reading.
   */
  function follow(
    start: (signal: AbortSignal) => Promise<Attached>,
    {
      watch = false,
      afterLoss = false,
      text = "",
      editing = null,
      newChat = false,
      modelRefused,
    }: {
      watch?: boolean;
      afterLoss?: boolean;
      /** What was written, to put back if the turn is refused for its model. */
      text?: string;
      /** The message an edit was of, whose box that goes back into. */
      editing?: string | null;
      /** A first message, where a 404 can only be about the agent. */
      newChat?: boolean;
      /** What else a refusal for the model calls for. */
      modelRefused?: () => void;
    } = {},
  ): Promise<void> {
    const control = new AbortController();
    starting.add(control);
    return start(control.signal).then(
      (attached) => {
        starting.delete(control);
        // This page went away while the request was in the air.
        if (control.signal.aborted) return;
        // **A watcher on the same run is left alone.** Two reads can each
        // find the run a change started elsewhere; the second watch would
        // only replay what the first is already drawing.
        if (watch && watchingRun === attached.runId) {
          control.abort();
          return;
        }
        // **The one being watched is let go of here and not before.** A turn
        // a conversation that is already answering refuses (409) must leave
        // the first run's stream alone; stopping it to make room for a turn
        // that never began would lose the answer that is arriving.
        watching?.abort();
        watching = control;
        watchingRun = attached.runId;
        dispatch({
          kind: "started",
          runId: attached.runId,
          conversationId: attached.conversationId,
        });
        void consume(attached, control, afterLoss);
      },
      (failure: unknown) => {
        starting.delete(control);
        if (control.signal.aborted) return;
        if (watch) {
          dispatch({ kind: "lost", detail: LOST_TOUCH });
          return;
        }
        const agentGone =
          newChat && failure instanceof ApiError && failure.status === 404;
        dispatch({
          kind: "refused",
          detail: agentGone ? AGENT_GONE : said(failure),
        });
        const forModel = forItsModel(failure);
        // An edit the conversation refused for answering already (409): the
        // person is told to stop that answer and send again, and an edit
        // sent again from the main box would be a new message instead.
        const editWhileAnswering =
          editing !== null &&
          failure instanceof ApiError &&
          failure.status === 409;
        if (!forModel && !agentGone && !editWhileAnswering) return;
        // **Refused for its model or its agent, the message goes back in its
        // box**: what the person is told to do is pick another and send
        // again, and the box emptied itself when it handed the message over
        // -- or, for an edit, the edit box closed. Only then: any other
        // refusal is left as it always was.
        if (text !== "") wanted = { text, editing };
        if (forModel) modelRefused?.();
      },
    );
  }

  async function consume(
    attached: Attached,
    control: AbortController,
    afterLoss: boolean,
  ): Promise<void> {
    let lost: string | null = null;
    // A result names its call alone; the call named the tool.
    const tools = new Map<string, string>();
    try {
      for await (const { event } of attached.events) {
        if (control.signal.aborted) return;
        dispatch({ kind: "event", event });
        if (event.type === "TOOL_CALL_START") {
          tools.set(event.toolCallId, event.toolCallName);
        } else if (event.type === "TOOL_CALL_RESULT") {
          const name = tools.get(event.toolCallId);
          if (name !== undefined) props.onToolResult?.(name);
        }
      }
    } catch {
      if (control.signal.aborted) return;
      lost = LOST_TOUCH;
    }
    if (control.signal.aborted) return;
    if (watching === control) watchingRun = null;

    if (lost !== null) {
      // **The connection went and the run did not have to.** A client that
      // missed deltas reloads the conversation, which is what the store is
      // for (`docs/specs/wire.md`), and a run that is still in flight there
      // is one to watch again -- with the client's budget of tries reset,
      // since a link that has come back is not the link that went.
      //
      // Once. A second loss is one to tell about rather than to keep
      // retrying, and the reading below leaves `running` false, so the box
      // and the answer's "try again" are usable.
      if (!afterLoss) {
        await read(attached.conversationId, control.signal, true);
        return;
      }
      dispatch({ kind: "lost", detail: lost });
      return;
    }

    // A turn writes to the conversation, and a write is what the panel's list
    // is asked for again after (`src/history/history.ts`). **Before** the
    // read below: that read may find another run in flight and watch it,
    // which stops this watcher -- and the list still has to be asked for.
    props.onTurnEnded?.();
    // The turn is over and **the store is the truth**: the ids are the
    // server's, the answer carries its provenance, and the thread is the one
    // the server shows.
    await read(attached.conversationId, control.signal);
  }

  /** A new message: what the main box sends. */
  async function onNew(written: string): Promise<void> {
    const text = written.trim();
    if (text === "") return;
    if (busy()) return told(text);
    // Not the end of the thread where that is a question nobody answered:
    // the new message replaces it (`under`, `askedIn`).
    const parentId = under(state);
    const conversationId = state.conversationId;
    if (conversationId !== null) {
      const asked = askedIn(state, text);
      // A conversation that exists has its first question stored, so there is
      // always something to answer or to replace.
      if (asked === null) return;
      dispatch({ kind: "asked", id: unsent(), after: parentId, text });
      await follow(
        (signal) =>
          startTurn(
            conversationId,
            { ...asked, modelId: props.modelId },
            { signal },
          ),
        { text },
      );
      return;
    }
    dispatch({ kind: "asked", id: unsent(), after: parentId, text });
    const { agentId, modelId } = props;
    const projectId = props.projectId ?? null;
    if (agentId === null) {
      dispatch({ kind: "lost", detail: NO_AGENT });
      return;
    }
    await follow(
      async (signal) => {
        // The model goes with the first message and never again: every later
        // turn runs on the conversation's, which the server reads.
        const attached = await startNewConversation(
          agentId,
          modelId,
          text,
          { signal },
          projectId,
        );
        // **Only if this page is still the empty chat.** Somebody who opened
        // another conversation while the request was in the air is not to be
        // taken to this one instead, and claiming it as ours would stop the
        // conversation they *did* open from being read at all. The
        // conversation exists either way; the panel's next refresh lists it.
        if (signal.aborted) return attached;
        // The interface has one to be on now: a route to go to and a row for
        // the panel. What to do about it is the application's
        // (`src/chat/index.ts`).
        ours = attached.conversationId;
        props.onConversationStarted(attached.conversationId);
        return attached;
      },
      {
        text,
        newChat: true,
        // The shell holds the choice, and forgets it (`onModelRefused`).
        modelRefused: () => {
          if (modelId !== null) props.onModelRefused?.(modelId);
        },
      },
    );
  }

  /** That question replaced: what an edit box sends. */
  async function onEdit(edited: string, written: string): Promise<void> {
    const text = written.trim();
    const conversationId = state.conversationId;
    if (text === "" || conversationId === null) return;
    // **An edit is a new message under the parent of the one it replaces**
    // (`docs/specs/conversations.md`): the store keeps the old one, and the
    // thread on the screen is cut after that parent and goes on from the new
    // one. The backend is told which question is edited and works out the
    // parent itself; the message before the edited one on the screen, or
    // nothing for the first, is only where the screen is cut.
    const parentId = parentOf(state.messages, edited);
    // A question the server has never been told about: one this chat put on
    // the screen a moment ago, or under one, and the conversation has not
    // been read since. The backend would answer 404 for it. The thread hides
    // the edit button while a run is going, so this is reachable only by
    // calling this directly.
    if (isUnsent(edited) || isUnsent(parentId)) return;
    if (busy()) return told(text, edited);
    // The store's parent, which is the tool message under that answer when
    // its calls were answered (`state.ts`): the screen does not hold one.
    const storedId = storedParent(state, parentId);
    dispatch({ kind: "asked", id: unsent(), after: storedId, text });
    // Refused for its model, or because the conversation is answering, the
    // edited text comes back in the edit box of the message it was of
    // (`follow`).
    await follow(
      (signal) =>
        startTurn(
          conversationId,
          { text, edit: edited, modelId: props.modelId },
          { signal },
        ),
      { text, editing: edited },
    );
  }

  /** That answer produced again: what Refresh does. */
  async function onReload(regenerate: string): Promise<void> {
    const conversationId = state.conversationId;
    if (conversationId === null) return;
    // Nothing was typed for a regeneration, so there is nothing to put back
    // and nothing that was not sent.
    if (busy()) {
      dispatch({ kind: "told", detail: ONE_AT_A_TIME_ANSWER });
      return;
    }
    // **The same button is Retry on an answer that failed** (`docs/specs/ui.md`):
    // the thread offers one Refresh, and what it means is decided here. A
    // regeneration drops the answer from what the model sees; a retry keeps
    // the failure for it to see.
    const retry = state.messages.some(
      (message) => message.id === regenerate && message.state === "failed",
    );
    // Neither carries a new message, and both take the answer off the
    // screen: a regeneration answers the question that turn already had, and
    // **replaces the turn** (`docs/specs/conversations.md`). The cut is after
    // its question -- which is not always the message before the regenerated
    // one: a turn with tools in it has an answer that called before the
    // answer after the results (`turnStart`).
    dispatch({
      kind: "again",
      after:
        turnStart(state, regenerate) ?? parentOf(state.messages, regenerate),
    });
    const modelId = props.modelId;
    await follow((signal) =>
      startTurn(
        conversationId,
        retry ? { retry: regenerate, modelId } : { regenerate, modelId },
        { signal },
      ),
    );
  }

  /**
   * **Resume** a failed answer (`docs/specs/ui.md`): its turn goes on from the
   * rounds the engine saved, and the model is told nothing. Like a retry, it
   * takes the failed answer off the screen.
   */
  async function onResume(answerId: string): Promise<void> {
    const conversationId = state.conversationId;
    if (conversationId === null) return;
    if (busy()) {
      dispatch({ kind: "told", detail: ONE_AT_A_TIME_ANSWER });
      return;
    }
    dispatch({ kind: "again", after: turnStart(state, answerId) });
    const modelId = props.modelId;
    await follow((signal) =>
      startTurn(conversationId, { resume: answerId, modelId }, { signal }),
    );
  }

  /** Stop the run in flight: what the stop button does. */
  async function onCancel(): Promise<void> {
    const { conversationId, runId } = state;
    // Nothing to stop. The box offers stopping only while a run is going,
    // which is a run this chat knows the id of, so this is a guard and not a
    // state: there is no request that would say "stop whatever is going".
    if (conversationId === null || runId === null) return;
    try {
      await cancelRun(conversationId, runId);
    } catch {
      // **Nothing about the run changed**, and the stream watching it is
      // still watching: the request failed, so nothing was cancelled and the
      // answer carries on arriving. Forgetting the run here would leave it
      // writing into a thread that thinks nothing is happening -- and would
      // leave an answer hanging under a question a later refusal could take
      // off the screen.
      dispatch({ kind: "told", detail: STOP_DID_NOT_ARRIVE });
      return;
    }
    // Nothing else: a cancellation is not a failure, and the stream this is
    // already watching ends with `RUN_FINISHED` and AG-UI's `cancelled`
    // outcome (`docs/specs/wire.md`).
  }

  return {
    now,
    began,
    stop,
    saying,
    read,
    refresh,
    onNew,
    onEdit,
    onReload,
    onResume,
    onCancel,
  };
}

/** An id for a message the server has not been told about yet. */
function unsent(): string {
  return `${UNSENT}${crypto.randomUUID()}`;
}
