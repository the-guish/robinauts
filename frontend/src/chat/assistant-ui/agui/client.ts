// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The AG-UI client: ours, and about two hundred lines of it.
 *
 * **Why not `@assistant-ui/react-ag-ui`.** The protocol is at 1.0
 * (`@ag-ui/core`, `@ag-ui/client`), and assistant-ui's bridge to it is not:
 * 0.0.60, published 2026-09-18, is still pinned to `@ag-ui/client ^0.0.59`,
 * so taking it would install a second, pre-1.0 client beside the 1.0 one and
 * tie the chat to a package that has not caught up in a month. The wire spec
 * foresaw exactly this and named the alternative: "a small AG-UI client of
 * our own takes its place behind the same seam; the wire does not change"
 * (`docs/specs/wire.md`, "Details likely to change"). Checked again
 * 2026-09-22. So this step adds **no dependency at all**: `fetch`, a
 * `ReadableStream` and `./sse.ts`.
 *
 * What it does, and nothing else:
 *
 * - opens the three streaming routes of `docs/specs/wire.md` and reads the
 *   run and the conversation out of the response headers, so that a client
 *   which got the headers and then lost the connection can still re-attach;
 * - turns each block into one of `./events.ts`'s events;
 * - **carries on where it left off** when a connection drops before the run
 *   ended: the last `id:` it saw is the position, `Last-Event-ID` is how it
 *   is said, and the backend replays no event it has had in full;
 * - **never gives up while the run lives**: a connection that drops is made
 *   again, backing off from half a second to thirty with jitter, and at once
 *   when the browser comes back online or into view, or the person asks
 *   (`Wake`). Only the server saying no -- a 4xx -- ends the watch;
 * - **notices a connection that has gone quiet**: the backend says something
 *   at least every fifteen seconds, a keep-alive comment when nothing else,
 *   so one that has said nothing for `IDLE_MS` is a connection a proxy has
 *   dropped without closing, and it is made again.
 *
 * **A refusal is a status and comes before the stream** (`docs/specs/wire.md`),
 * so everything that can be refused -- a conversation already answering, a
 * body that is neither shape of a turn, a run that is not there -- arrives as
 * an `ApiError` from the call that opened the stream, through the API
 * client's own mapping.
 */
import { ApiError, refused } from "../../../api/client";
import { isUuid } from "../../../router";
import { blocks } from "./sse";
import { decode, isTerminal, type AguiEvent } from "./events";

/** A position, as this build writes one: decimal digits and nothing else. */
const DIGITS = /^\d+$/;

/** What a response says about the run it is the stream of. */
const RUN_ID_HEADER = "x-robinauts-run-id";
const CONVERSATION_ID_HEADER = "x-robinauts-conversation-id";

/**
 * How many times a dropped stream is opened again, by default, before giving
 * up: never, while the run lives. A run answering for hours behind a flaky
 * link, or a deployment restarting, is not one to stop watching; the
 * conversation is on the server, and the server saying no is what ends it.
 */
export const RETRIES = Number.POSITIVE_INFINITY;

/** The first wait before trying again, and the longest. */
export const FIRST_BACKOFF_MS = 500;
export const MAX_BACKOFF_MS = 30_000;

/**
 * How long a connection may say nothing before it is taken for dropped.
 *
 * Three of the backend's keep-alives (`docs/specs/wire.md`): one lost is
 * not a dropped connection, three are.
 */
export const IDLE_MS = 45_000;

/**
 * How long to wait before try number `tries`, counted from zero: doubling
 * from `FIRST_BACKOFF_MS` up to `MAX_BACKOFF_MS`, give or take a fifth, so
 * that every browser of a deployment that restarted does not come back in
 * the same instant.
 */
export function backoff(tries: number, random: () => number = Math.random) {
  const base = Math.min(MAX_BACKOFF_MS, FIRST_BACKOFF_MS * 2 ** tries);
  return Math.round(base * (0.8 + 0.4 * random()));
}

/**
 * What cuts a wait before the next try short.
 *
 * The person asking (a Reconnect button), and the browser coming back online
 * or into view: each is a good moment to try at once rather than at the end
 * of a backoff that may be thirty seconds long.
 */
export class Wake {
  private readonly waiters = new Set<() => void>();

  /** End every wait now. */
  now(): void {
    const waiting = [...this.waiters];
    this.waiters.clear();
    for (const done of waiting) done();
  }

  /**
   * Have `now` call `done`, until the function handed back is called: a wait
   * that ended some other way lets go of it, so that nothing gathers here
   * over a long run's many reconnections.
   */
  listen(done: () => void): () => void {
    this.waiters.add(done);
    return () => {
      this.waiters.delete(done);
    };
  }

  /** How many waits are listening: for the tests. */
  get listening(): number {
    return this.waiters.size;
  }
}

/** What a stream that could not be picked up again is reported as. */
export const LOST =
  "the connection to this answer was lost and could not be picked up again";

/** What a connection that went quiet is aborted with, and then made again. */
export const QUIET = "the connection to this answer went quiet";

/** What a response that is not one of our streams is reported as. */
export const NOT_OURS = "the answer was not one of our event streams";

/** One event of a run, with the position to carry on after it, if it has one. */
export interface Numbered {
  /**
   * The platform's own numbering, or `null`.
   *
   * The backend puts an id on the **last** wire event derived from each of
   * the run's events, so an event without one is never something to
   * re-attach after (`docs/specs/wire.md`).
   */
  position: number | null;
  event: AguiEvent;
}

/** A run being watched: what it is, and what it says. */
export interface Attached {
  runId: string;
  conversationId: string;
  /** The run's events, until it ends. Iterated once. */
  events: AsyncIterable<Numbered>;
}

/** What a caller may pass every call here. */
export interface Watching {
  signal?: AbortSignal;
  /**
   * How a wait between two tries is done. Injected so that a test of the
   * retrying does not wait the seconds a person would.
   */
  wait?: (ms: number) => Promise<void>;
  /** How many tries before giving up; `RETRIES`, never, by default. */
  retries?: number;
  /** How long a connection may say nothing; `IDLE_MS` by default. */
  idleMs?: number;
  /** What cuts a wait short besides the browser coming back. */
  wake?: Wake;
  /**
   * Told the wait before each try once a connection has dropped, and `null`
   * once one delivers again: what a "reconnecting" line is drawn from.
   */
  onReconnecting?: (inMs: number | null) => void;
}

const sleep = (ms: number) =>
  new Promise<void>((done) => {
    setTimeout(done, ms);
  });

/**
 * Begin a conversation with an agent, and watch the run answering it.
 *
 * `modelId` is the model the conversation runs on; `null` is the agent's
 * default, which the backend copies in (`docs/specs/wire.md`).
 */
export async function startNewConversation(
  agentId: string,
  modelId: string | null,
  text: string,
  watching: Watching = {},
): Promise<Attached> {
  return attachTo(
    await post(
      "/api/turns",
      { agent_id: agentId, model_id: modelId, text },
      watching,
    ),
    0,
    watching,
  );
}

/**
 * What a turn in a conversation that exists asks for: a reply, an edit, an
 * answer again, or a retry of one that failed, with the model it runs on --
 * the picker's, sent with every turn; left out, the backend takes the
 * conversation's last one.
 */
export type Turn = (
  | { text: string; parentId: string }
  | { text: string; edit: string }
  | { regenerate: string }
  | { retry: string }
) & { modelId?: string | null };

/**
 * A turn in a conversation that exists, and the run it began.
 *
 * The four shapes are the wire's: `text` with the message it answers, `text`
 * with the question it is a new version of (the backend works out where that
 * hangs), the answer to produce again, or the failed answer to try again
 * from; the last two carry no text.
 */
export async function startTurn(
  conversationId: string,
  turn: Turn,
  watching: Watching = {},
): Promise<Attached> {
  const body = {
    ...wireTurn(turn),
    ...(turn.modelId == null ? {} : { model_id: turn.modelId }),
  };
  return attachTo(
    await post(
      `/api/conversations/${encodeURIComponent(conversationId)}/turns`,
      body,
      watching,
    ),
    0,
    watching,
  );
}

function wireTurn(turn: Turn): Record<string, string> {
  if ("regenerate" in turn) return { regenerate: turn.regenerate };
  if ("retry" in turn) return { retry: turn.retry };
  if ("edit" in turn) return { text: turn.text, edit: turn.edit };
  return { text: turn.text, parent_id: turn.parentId };
}

/**
 * Watch a run that is already going, from the position last seen.
 *
 * `after` is `resume.after` when a conversation has just been opened: the
 * watcher has every complete message already, so attaching there replays
 * exactly the message still being produced and nothing it has seen
 * (`docs/specs/runs.md`).
 */
export async function attach(
  conversationId: string,
  runId: string,
  after: number,
  watching: Watching = {},
): Promise<Attached> {
  return attachTo(
    await open(conversationId, runId, after, watching),
    after,
    watching,
  );
}

/** The stream of a run, from the response that carries it. */
function attachTo(
  response: Response,
  from: number,
  watching: Watching,
): Attached {
  const runId = response.headers.get(RUN_ID_HEADER);
  const conversationId = response.headers.get(CONVERSATION_ID_HEADER);
  const body = response.body;
  // Every stream of ours carries both headers and a body, and **both ids are
  // uuids**: they go straight into a request path and into a route, so a
  // header that is not one is not read as one (`src/router.ts`). Anything
  // else answering here is not one of our streams.
  if (
    runId === null ||
    conversationId === null ||
    body === null ||
    !isUuid(runId) ||
    !isUuid(conversationId)
  ) {
    throw new ApiError(response.status, "unreadable_stream", NOT_OURS);
  }
  return {
    runId,
    conversationId,
    events: following(body, conversationId, runId, from, watching),
  };
}

/**
 * The events of that run, picking the stream up again when it drops.
 *
 * The loop is: read until the stream ends; if it ended with a terminal event
 * the run is over and so is this; otherwise the connection went and the run
 * did not, so wait a moment and open
 * `GET /api/conversations/{id}/runs/{run_id}/events` after the last position
 * seen. `Last-Event-ID` is how that position is said, which is
 * what a browser's own `EventSource` would send and what the backend reads
 * whether or not `after` is there as well.
 */
async function* following(
  first: ReadableStream<Uint8Array>,
  conversationId: string,
  runId: string,
  from: number,
  watching: Watching,
): AsyncGenerator<Numbered> {
  const {
    signal,
    wait = sleep,
    retries = RETRIES,
    idleMs = IDLE_MS,
    wake,
    onReconnecting,
  } = watching;
  let body: ReadableStream<Uint8Array> | null = first;
  let position = from;
  let tries = 0;
  let reconnecting = false;
  for (;;) {
    // A watcher that went away during the wait before this try makes none.
    if (gone(signal)) throw signal?.reason;
    let carried = false;
    // **One connection, with its own signal**: the watcher going away aborts
    // it, and so does the connection saying nothing for `idleMs`.
    const connection = new AbortController();
    const away = () => {
      connection.abort(signal?.reason);
    };
    signal?.addEventListener("abort", away, { once: true });
    let quiet: ReturnType<typeof setTimeout> | undefined;
    const heard = () => {
      clearTimeout(quiet);
      quiet = setTimeout(() => {
        connection.abort(new ApiError(0, "stream_quiet", QUIET));
      }, idleMs);
    };
    heard();
    try {
      // **Opening is inside the try**, so that a re-attach whose *request*
      // does not arrive is retried like a stream that connected and then
      // ended. Outside it, the one failure that a dropped network is most
      // likely to produce -- the next `GET` never getting through -- would
      // have been the one failure that ended the watch.
      body ??= streamOf(
        await open(conversationId, runId, position, {
          signal: connection.signal,
        }),
      );
      for await (const block of blocks(
        listened(body, heard),
        connection.signal,
      )) {
        const event = decode(block.data);
        if (event === null) continue;
        const over = isTerminal(event);
        let numbered = false;
        // An id nothing here could have sent is **not a position**, and not
        // a reason to drop the event either: this build writes whole numbers
        // and only whole numbers, so anything else came from something in
        // front of the deployment. The event is read; the id is not.
        //
        // Read by the shape first, because `Number` is generous where this
        // must not be: it makes `""` zero, `0x10` sixteen and ` 4 ` four,
        // and every one of those would be a position nothing here issued.
        const seen = DIGITS.test(block.id ?? "") ? Number(block.id) : null;
        if (seen !== null && Number.isSafeInteger(seen)) {
          if (seen > position) {
            position = seen;
            numbered = true;
            // A stream that is delivering is a connection that works, so the
            // budget below is about connections that do not: it counts tries
            // since the last event the platform numbered, not since the
            // watch began. A run answering for an hour over a flaky link is
            // not one to give up on at its fifth reconnection.
            carried = true;
            if (reconnecting) {
              reconnecting = false;
              onReconnecting?.(null);
            }
          } else if (!over) {
            // **A numbered event that does not move the position forward is
            // one this watcher has had in full.** An id means "everything
            // derived from the run's events up to here has been sent"
            // (`docs/specs/wire.md`), and this build never asks from before
            // what it has seen, so the backend cannot send one: what can is
            // something in front of the deployment replaying a block.
            // Reading it would say a delta twice, and counting it as
            // delivery would reset the budget below -- so a proxy replaying
            // one event and cutting would be reconnected to for ever.
            //
            // **Never an ending, whatever its id says.** Every stream ends
            // with one, and a watcher that dropped the one it was sent would
            // wait for an answer that has already been given.
            continue;
          }
        }
        yield { position: numbered ? position : null, event };
        // The run is over: every stream ends with one of these, and one that
        // merely closed is a connection to make again.
        if (over) return;
      }
    } catch (failure) {
      // An abort is this watcher going away, not a stream that failed.
      if (gone(signal)) throw failure;
      // **A refusal is an answer, not a connection that went.** A run that is
      // not this person's (404), a position it has not reached (422), a
      // session that ended (401): asking again asks the same question and
      // gets the same answer, and a 401 has already put the interface back to
      // signed-out. Only a request that did not arrive, or a deployment
      // answering 5xx, is worth another try.
      if (refusalOf(failure)) throw failure;
    } finally {
      clearTimeout(quiet);
      signal?.removeEventListener("abort", away);
    }
    if (gone(signal)) throw signal?.reason;
    body = null;
    if (carried) tries = 0;
    // Here three ways -- the body ended, reading it threw, or the request for
    // it did -- and none of them is the run saying it is over. So the
    // connection went and the run did not: it is picked up where it was left
    // off.
    if (tries >= retries) {
      throw new ApiError(0, "stream_lost", LOST);
    }
    const delay = backoff(tries);
    reconnecting = true;
    // Listening before telling: what is told may be what cuts the wait short.
    const waiting = waitOrWake(wait(delay), wake, signal);
    onReconnecting?.(delay);
    await waiting;
    tries += 1;
  }
}

/**
 * That wait, cut short by `wake`, or by the browser coming back online or
 * into view: the moments a connection is most likely to work again.
 */
async function waitOrWake(
  waiting: Promise<void>,
  wake: Wake | undefined,
  signal: AbortSignal | undefined,
): Promise<void> {
  let back: () => void = () => undefined;
  const browser = new Promise<void>((done) => {
    back = done;
  });
  const shown = () => {
    if (document.visibilityState === "visible") back();
  };
  const hasWindow = typeof window !== "undefined";
  if (hasWindow) {
    window.addEventListener("online", back);
    document.addEventListener("visibilitychange", shown);
  }
  // The watcher going away ends the wait too, and the loop then stops.
  signal?.addEventListener("abort", back, { once: true });
  const unlisten = wake?.listen(back);
  try {
    await Promise.race([waiting, browser]);
  } finally {
    unlisten?.();
    signal?.removeEventListener("abort", back);
    if (hasWindow) {
      window.removeEventListener("online", back);
      document.removeEventListener("visibilitychange", shown);
    }
  }
}

/** That body, telling `heard` of every chunk, keep-alives included. */
function listened(
  body: ReadableStream<Uint8Array>,
  heard: () => void,
): ReadableStream<Uint8Array> {
  return body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        heard();
        controller.enqueue(chunk);
      },
    }),
  );
}

/** Whether the watcher has gone away, read afresh each time it is asked. */
function gone(signal: AbortSignal | undefined): boolean {
  return signal?.aborted === true;
}

/** Whether that failure is the backend refusing rather than a link that went. */
function refusalOf(failure: unknown): boolean {
  return (
    failure instanceof ApiError && failure.status >= 400 && failure.status < 500
  );
}

/** The body of a stream of ours, which always has one. */
function streamOf(response: Response): ReadableStream<Uint8Array> {
  const body = response.body;
  if (body === null) {
    throw new ApiError(response.status, "unreadable_stream", NOT_OURS);
  }
  return body;
}

/** `POST` a turn, and the stream it answers with. */
async function post(
  path: string,
  body: unknown,
  { signal }: Watching,
): Promise<Response> {
  return answered(
    fetch(path, {
      method: "POST",
      headers: {
        // The backend refuses a write that is not JSON, body or none
        // (`src/api/client.ts`), and `text/event-stream` is what comes back.
        "content-type": "application/json",
        accept: "text/event-stream",
      },
      credentials: "same-origin",
      body: JSON.stringify(body),
      ...(signal === undefined ? {} : { signal }),
    }),
    signal,
  );
}

/** `GET` the events of a run of a conversation, from a position. */
async function open(
  conversationId: string,
  runId: string,
  after: number,
  { signal }: Watching,
): Promise<Response> {
  const headers: Record<string, string> = { accept: "text/event-stream" };
  // Said once. The backend reads both and refuses two that disagree, so
  // there is nothing to gain from sending the same number twice.
  if (after > 0) headers["last-event-id"] = String(after);
  return answered(
    fetch(
      `/api/conversations/${encodeURIComponent(conversationId)}/runs/${encodeURIComponent(runId)}/events`,
      {
        method: "GET",
        headers,
        credentials: "same-origin",
        ...(signal === undefined ? {} : { signal }),
      },
    ),
    signal,
  );
}

/** That call, with everything it can go wrong with as an `ApiError`. */
async function answered(
  sending: Promise<Response>,
  signal: AbortSignal | undefined,
): Promise<Response> {
  let response: Response;
  try {
    response = await sending;
  } catch (failure) {
    // The caller's own abort is handed back as it is, as `request` does.
    if (signal?.aborted === true) throw failure;
    throw new ApiError(
      0,
      "network_error",
      failure instanceof Error ? failure.message : "the request did not arrive",
    );
  }
  if (!response.ok) throw await refused(response);
  return response;
}
