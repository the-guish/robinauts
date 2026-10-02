// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Our state, given to assistant-ui to render.
 *
 * `useExternalStoreRuntime` is the runtime for a host that owns its own
 * messages: the library renders and calls back, and every message and every
 * byte of a stream is ours (`./state.ts`). None of assistant-ui's own
 * persistence is used -- no `assistant-cloud`, no thread list -- because the
 * conversation lives in our database and nowhere else (ADR 0001).
 *
 * **What the adapter really offers, having read it** (`@assistant-ui/react`
 * 0.15.19, `ExternalStoreAdapter`):
 *
 * - `messageRepository` is how the thread is handed over: an
 *   `ExportedMessageRepository` is **every message with its parent, and a
 *   head**, and in that mode the runtime keeps exactly what it is given and
 *   drops what it is no longer given. Ours is a chain -- each message's
 *   parent is the one before it -- so a message an edit cut off the thread
 *   is gone from the runtime on the next render. The other way in, plain
 *   `messages`, relinks a flat list and keeps what it has already seen, so a
 *   message cut off would linger beside its replacement as a branch. Nothing
 *   here is a branch (ADR 0003, `docs/specs/conversations.md`).
 * - `setMessages` is **not** offered. It is what turns on branch switching
 *   and deleting, neither of which exists; and it is what makes the library's
 *   own `cancelRun` take a trailing question out of the thread and put its
 *   text back into the box, which is wrong for a host whose backend stored
 *   that question when the turn began (`@assistant-ui/core`,
 *   `external-store-thread-runtime-core`, `cancelRun`). Without it a stop
 *   leaves the thread as it is.
 * - `onNew`, `onEdit`, `onReload` and `onCancel` are the four the vendored
 *   Thread's composer and action bar reach: send, edit, regenerate, stop.
 *   Each is one of the wire's turns (`docs/specs/wire.md`). An edit cuts the
 *   thread after the edited message's parent and goes on from there; a
 *   regeneration cuts the answer off and produces it again.
 * - `isRunning` is ours to say, and while it is true with no assistant
 *   message at the end of the thread the runtime draws a placeholder of its
 *   own -- which is the "working" dot between a question and its first word.
 */
import {
  fromThreadMessageLike,
  useExternalStoreRuntime,
  type AppendMessage,
  type AssistantRuntime,
  type ExportedMessageRepository,
  type ExternalStoreAdapter,
  type MessageStatus,
  type ThreadMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { useEffect, useMemo, useReducer, useState } from "react";

import {
  ApiError,
  detailOf,
  isRefusal,
  MODEL_NOT_OFFERED,
  request,
  UNKNOWN_MODEL,
} from "../../api/client";
import { cancelRun } from "../../conversation/conversation";
import type { ChatProps } from "../index";
import {
  attach,
  startNewConversation,
  startTurn,
  type Attached,
} from "./agui/client";
import {
  EMPTY,
  ENDED_BADLY,
  isUnsent,
  ONE_AT_A_TIME,
  ONE_AT_A_TIME_ANSWER,
  reduce,
  STOP_DID_NOT_ARRIVE,
  storedParent,
  turnStart,
  under,
  UNSENT,
  type ChatAction,
  type ChatMessage,
  type ChatState,
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

/** What the component below gets back. */
export interface Chatting {
  state: ChatState;
  runtime: AssistantRuntime;
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
  const [turns] = useState(() => turnsOf(dispatch, props));
  useEffect(() => {
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

  // Going away stops watching: the stream is a view of the run, and closing
  // it changes nothing about the run (`docs/specs/runs.md`).
  useEffect(() => {
    return () => {
      turns.stop();
    };
  }, [turns]);

  // The runtime re-imports what it is given when the object is not the one it
  // was given last, so this is what decides how often it does.
  const repository = useMemo(
    () => asRepository(state.messages),
    [state.messages],
  );

  const adapter: ExternalStoreAdapter = useMemo(
    () => ({
      messageRepository: repository,
      // **A run, and not a turn that has been asked for.** Between the send
      // and the response's headers there is nothing to stop: the library's
      // own `cancelRun` would take the question back out of the thread and
      // put its text into the box, and the server would answer it anyway.
      // While `sending`, `onNew` refuses instead (see `turnsOf`).
      isRunning: state.runId !== null,
      isLoading: state.loading,
      // There is nothing to send a first message to when the deployment has
      // no agent; the box still takes what is typed into it.
      isSendDisabled: state.conversationId === null && props.agentId === null,
      onNew: turns.onNew,
      onEdit: turns.onEdit,
      onReload: turns.onReload,
      onCancel: turns.onCancel,
    }),
    [
      repository,
      state.runId,
      state.loading,
      state.conversationId,
      props.agentId,
      turns,
    ],
  );

  const runtime = useExternalStoreRuntime(adapter);

  // **The box.** The one thing the library leaves it saying the wrong thing
  // about a message that never became a turn; what it is and why is in
  // `turnsOf`, beside `wanted`. This runs after every render, which is after
  // the library's own work in the same event, and it never overwrites
  // anything the person has typed since.
  useEffect(() => {
    const wanted = turns.saying();
    if (wanted === null) return;
    const editing = wanted.editing;
    // Only while that message is still in the thread: a read landing in
    // between may have taken it off, and asking the runtime for a message it
    // does not hold throws -- which would put the error boundary in place of
    // the chat. The text then goes to the main box, which is still somewhere.
    const held = runtime.thread
      .getState()
      .messages.some((message) => message.id === editing);
    if (editing !== null && held) {
      // An edit goes back into its own box, open on the message it was
      // editing -- which the refusal has just put back on the screen -- so
      // that sending it again is still an edit and not a new message at
      // the end of the thread. If that box has been opened again since, the
      // text goes into it only while it is empty, and otherwise on to the
      // main box below: `saying` has handed it over, and it is not dropped.
      const edit = runtime.thread.getMessageById(editing).composer;
      if (!edit.getState().isEditing) {
        edit.beginEdit();
        edit.setText(wanted.text);
        return;
      }
      if (edit.getState().text === "") {
        edit.setText(wanted.text);
        return;
      }
    }
    const box = runtime.thread.composer;
    if (box.getState().text === "") box.setText(wanted.text);
  });

  return { state, runtime };
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
   * What the box must be made to say, once, after the next render.
   *
   * The box empties itself when it hands a message over, so a turn refused
   * here for being a second one leaves nothing behind at all. The text goes
   * back, and a notice says why (`ONE_AT_A_TIME`). `editing` is the message
   * an edit was of, whose own box it goes back into; `null` is the main one.
   */
  type Wanted = { text: string; editing: string | null };
  let wanted: Wanted | null = null;

  /** What the box must be made to say, once. */
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
   * The Thread does not offer either -- the box shows stop while a run is
   * going, and the action bar hides itself -- so this is reachable only by
   * driving the runtime, or by being very quick between a send and its
   * answer's headers.
   */
  function busy(): boolean {
    return state.runId !== null || state.sending;
  }

  /**
   * Say that the turn was not sent, because the box has already cleared it.
   *
   * assistant-ui empties the composer when it hands a message over, so a turn
   * dropped in silence is a message somebody believes they sent
   * (`ONE_AT_A_TIME`). `told` changes nothing else: the turn that *is* on its
   * way keeps its question, its run and its stream.
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
    watching?.abort();
    watching = null;
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
        // **The one being watched is let go of here and not before.** A turn
        // a conversation that is already answering refuses (409) must leave
        // the first run's stream alone; stopping it to make room for a turn
        // that never began would lose the answer that is arriving.
        watching?.abort();
        watching = control;
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
    try {
      for await (const { event } of attached.events) {
        if (control.signal.aborted) return;
        dispatch({ kind: "event", event });
      }
    } catch {
      if (control.signal.aborted) return;
      lost = LOST_TOUCH;
    }
    if (control.signal.aborted) return;

    if (lost !== null) {
      // **The connection went and the run did not have to.** A client that
      // missed deltas reloads the conversation, which is what the store is
      // for (`docs/specs/wire.md`), and a run that is still in flight there
      // is one to watch again -- with the client's budget of tries reset,
      // since a link that has come back is not the link that went.
      //
      // Once. A second loss is one to tell about rather than to keep
      // retrying, and the reading below leaves `isRunning` false, so the box
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

  async function onNew(message: AppendMessage): Promise<void> {
    const text = wrote(message);
    if (text === "") return;
    if (busy()) return told(text);
    // Not the end of the thread where that is a question nobody answered:
    // sending again is how a turn that went wrong is retried (`under`).
    const parentId = under(state);
    dispatch({ kind: "asked", id: unsent(), after: parentId, text });
    const conversationId = state.conversationId;
    if (conversationId !== null) {
      await follow(
        (signal) =>
          startTurn(
            conversationId,
            { text, parentId, modelId: props.modelId },
            { signal },
          ),
        { text },
      );
      return;
    }
    const { agentId, modelId } = props;
    if (agentId === null) {
      dispatch({ kind: "lost", detail: NO_AGENT });
      return;
    }
    await follow(
      async (signal) => {
        // The model goes with the first message and never again: every later
        // turn runs on the conversation's, which the server reads.
        const attached = await startNewConversation(agentId, modelId, text, {
          signal,
        });
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

  async function onEdit(message: AppendMessage): Promise<void> {
    const text = wrote(message);
    const conversationId = state.conversationId;
    if (text === "" || conversationId === null) return;
    // **An edit is a new message under the parent of the one it replaces**
    // (`docs/specs/conversations.md`): the store keeps the old one, and the
    // thread on the screen is cut after that parent and goes on from the new
    // one. The runtime names the parent, which in our chain is the message
    // before the edited one, or nothing for the first.
    // A parent the server has never been told about: the question being
    // edited is itself one this chat put on the screen a moment ago and the
    // conversation has not been read since. The backend would answer 404 for
    // it. The Thread hides the edit button while a run is going, so this is
    // reachable only by driving the runtime directly.
    if (isUnsent(message.parentId)) return;
    if (busy()) return told(text, message.sourceId);
    // The store's parent, which is the tool message under that answer when
    // its calls were answered (`state.ts`): the runtime does not hold one.
    const parentId = storedParent(state, message.parentId);
    dispatch({ kind: "asked", id: unsent(), after: parentId, text });
    // Refused for its model, or because the conversation is answering, the
    // edited text comes back in the edit box of the message it was of
    // (`follow`).
    await follow(
      (signal) =>
        startTurn(
          conversationId,
          { text, parentId, modelId: props.modelId },
          { signal },
        ),
      { text, editing: message.sourceId },
    );
  }

  async function onReload(
    parentId: string | null,
    config: { sourceId: string | null },
  ): Promise<void> {
    const conversationId = state.conversationId;
    const regenerate = config.sourceId;
    if (conversationId === null || regenerate === null) return;
    // Nothing was typed for a regeneration, so there is nothing to put back
    // and nothing that was not sent.
    if (busy()) {
      dispatch({ kind: "told", detail: ONE_AT_A_TIME_ANSWER });
      return;
    }
    // A regeneration carries no new message: it answers the question that
    // turn already had, and **replaces the turn**
    // (`docs/specs/conversations.md`). Everything the turn produced comes
    // off the screen, so the cut is after its question -- which is not
    // always `parentId`, the message before the regenerated one: a turn
    // with tools in it has an answer that called before the answer after
    // the results (`turnStart`).
    dispatch({
      kind: "again",
      after: turnStart(state, regenerate) ?? parentId,
    });
    await follow((signal) =>
      startTurn(
        conversationId,
        { regenerate, modelId: props.modelId },
        { signal },
      ),
    );
  }

  async function onCancel(): Promise<void> {
    const { conversationId, runId } = state;
    // Nothing to stop. The Thread offers stopping only while `isRunning`,
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
    onNew,
    onEdit,
    onReload,
    onCancel,
  };
}

/** An id for a message the server has not been told about yet. */
function unsent(): string {
  return `${UNSENT}${crypto.randomUUID()}`;
}

/** What was typed, as one string. */
function wrote(message: AppendMessage): string {
  return message.content
    .map((part) => (part.type === "text" ? part.text : ""))
    .join("")
    .trim();
}

/**
 * Every message of ours, converted once.
 *
 * **A delta rebuilds the repository**, and a conversation is as long as it
 * is: without this, a hundred deltas into a turn of a hundred-message
 * conversation is ten thousand conversions of messages that have not changed
 * since they were read. The reducer builds a new object only for a message it
 * touches, so object identity is exactly "this message has changed", and a
 * `WeakMap` keyed on it is a cache nothing has to invalidate -- an entry goes
 * when the message it is of does.
 *
 * Giving assistant-ui converted messages rather than `ThreadMessageLike`s is
 * also what makes `createdAt` stop moving: `fromThreadMessageLike` stamps
 * `new Date()` on anything without one, so the same message converted twice
 * was two different messages.
 */
const converted = new WeakMap<ChatMessage, ThreadMessage>();

/**
 * The thread as the runtime takes it: a chain, each message under the one
 * before it, with the last as the head.
 *
 * A chain and not a flat list, because of how the two ways in differ
 * (the header above): given a repository, the runtime holds exactly these
 * messages and no others, so a message an edit cut off is gone from it on
 * the next render rather than kept beside its replacement.
 */
export function asRepository(
  messages: readonly ChatMessage[],
): ExportedMessageRepository {
  const items = messages.map((message, at) => ({
    message: stored(message),
    parentId: at === 0 ? null : (messages[at - 1]?.id ?? null),
  }));
  const headId = items[items.length - 1]?.message.id ?? null;
  return {
    messages: items,
    // Left out rather than given as `null`, which would mean "no thread at
    // all"; left out, the runtime reads the last message as the head.
    ...(headId === null ? {} : { headId }),
  };
}

/** That message as assistant-ui holds one, converted at most once. */
function stored(message: ChatMessage): ThreadMessage {
  const held = converted.get(message);
  if (held !== undefined) return held;
  const made = fromThreadMessageLike(
    asThreadMessage(message),
    message.id,
    // The fallback status, for a message that carries none. Ours all do --
    // an assistant message is given one below and a user message may not have
    // one at all -- so this is never what is used.
    { type: "complete", reason: "unknown" },
  );
  converted.set(message, made);
  return made;
}

/** One part of a message as assistant-ui takes one. */
type PartLike = Exclude<ThreadMessageLike["content"], string>[number];

/** A call as assistant-ui takes one, built up field by field. */
type ToolCallLike = {
  type: "tool-call";
  toolCallId: string;
  toolName: string;
  argsText: string;
  args?: NonNullable<Extract<PartLike, { type: "tool-call" }>["args"]>;
  result?: string;
  isError?: boolean;
};

/**
 * One message of ours, as assistant-ui takes one.
 *
 * A call is the library's `tool-call` part: the id and the name as the
 * platform stored them, the arguments as text and -- once whole -- as data,
 * the result as the string the tool answered, and whether it failed. The
 * vendored Thread draws it with its `ToolFallback`, which puts every one of
 * those in a text node (`docs/specs/wire.md`: rendered as data).
 */
export function asThreadMessage(message: ChatMessage): ThreadMessageLike {
  const content = message.parts.map((part): PartLike => {
    if (part.kind === "reasoning")
      return { type: "reasoning", text: part.text };
    if (part.kind === "tool-call") {
      const call: ToolCallLike = {
        type: "tool-call",
        toolCallId: part.id,
        toolName: part.name,
        argsText: part.argsText,
      };
      // What the store or the model wrote, which is JSON by construction.
      if (part.args !== undefined)
        call.args = part.args as NonNullable<ToolCallLike["args"]>;
      if (part.result !== undefined) call.result = part.result;
      if (part.isError !== undefined) call.isError = part.isError;
      return call;
    }
    return { type: "text", text: part.text };
  });
  if (message.role === "user") {
    return {
      id: message.id,
      role: "user",
      content: content.filter((part) => part.type === "text"),
    };
  }
  return {
    id: message.id,
    role: "assistant",
    content,
    status: asStatus(message),
  };
}

/** How a message of ours stands, in assistant-ui's words. */
function asStatus(message: ChatMessage): MessageStatus {
  switch (message.state) {
    case "running":
      return { type: "running" };
    case "cancelled":
      return { type: "incomplete", reason: "cancelled" };
    case "failed":
      // What the Thread shows under the message, and the one place a sentence
      // about a run that went wrong is drawn by the library rather than by us.
      return {
        type: "incomplete",
        reason: "error",
        error: message.detail ?? "",
      };
    case "stored":
      return { type: "complete", reason: "stop" };
  }
}
