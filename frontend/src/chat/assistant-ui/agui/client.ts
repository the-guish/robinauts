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
 * - **is patient about it**: a load balancer, a deploy or a laptop changing
 *   network drops a stream, and a long run outlives many of them. It backs
 *   off from half a second to thirty, with jitter, tries again at once when
 *   the browser comes back `online` or the tab becomes visible, or when the
 *   person asks (`reconnectNow`), and reconnects a stream that has sent no
 *   byte for `IDLE_MS` -- the backend sends a keep-alive every fifteen
 *   seconds, so such a stream is a dead one nobody closed;
 * - gives up only after many tries in a row with nothing delivered, because
 *   a stream nobody can open again for minutes is a thing to say rather than
 *   a thing to keep doing.
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
 * How many tries in a row, with nothing delivered between them, before
 * giving up.
 *
 * Many, with the backoff below: about nine minutes of a link that does not
 * work. A deployment restarting, a load balancer cutting a connection, a
 * laptop asleep -- all of those come back sooner. Past that, something is
 * wrong that trying again will not fix, and the conversation is still on the
 * server: reopening it is how the answer is seen.
 */
export const RETRIES = 20;

/** The wait before the first try, doubled after each, up to the second. */
export const FIRST_BACKOFF_MS = 500;
export const MAX_BACKOFF_MS = 30_000;

/**
 * The waits `backoff` works from: the two above, unless a test that drives a
 * whole chat through a dropped stream says otherwise.
 */
export const RECONNECT = { firstMs: FIRST_BACKOFF_MS, maxMs: MAX_BACKOFF_MS };

/**
 * How long a stream may send nothing at all before it is taken for dead.
 *
 * Three of the backend's keep-alives (every fifteen seconds): a connection a
 * proxy dropped without closing says nothing, and would otherwise be waited
 * on for ever.
 */
export const IDLE_MS = 45_000;

/**
 * How long to wait before try number `tries`, counted from zero: doubling,
 * capped, and a quarter either way at random, so that the browsers a
 * restart dropped together do not all come back in the same instant.
 */
export function backoff(
  tries: number,
  random: () => number = Math.random,
): number {
  const base = Math.min(RECONNECT.maxMs, RECONNECT.firstMs * 2 ** tries);
  return Math.round(base * (0.75 + random() * 0.5));
}

/** The waits under way, each of which `reconnectNow` cuts short. */
const nudges = new Set<() => void>();

/**
 * Try every dropped stream again now, rather than at the end of its wait:
 * what the Reconnect button does.
 */
export function reconnectNow(): void {
  for (const nudge of [...nudges]) nudge();
}

/**
 * A wait between two tries, cut short by `reconnectNow`, by the browser
 * coming back online, or by the tab becoming visible: each of those is a
 * reason to think the next try will work.
 */
function pause(ms: number): Promise<void> {
  return new Promise<void>((done) => {
    const visible = () => {
      if (globalThis.document?.visibilityState !== "hidden") finish();
    };
    const finish = () => {
      clearTimeout(timer);
      nudges.delete(finish);
      globalThis.removeEventListener?.("online", finish);
      globalThis.document?.removeEventListener("visibilitychange", visible);
      done();
    };
    const timer = setTimeout(finish, ms);
    nudges.add(finish);
    globalThis.addEventListener?.("online", finish);
    globalThis.document?.addEventListener("visibilitychange", visible);
  });
}

/** What a stream that could not be picked up again is reported as. */
export const LOST =
  "the connection to this answer was lost and could not be picked up again";

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
  /**
   * Told `true` when the stream dropped and is being picked up again, and
   * `false` once it is back, or over: what the Reconnect button is shown by.
   */
  onReconnecting?: (reconnecting: boolean) => void;
  /** `IDLE_MS`, unless a test says otherwise. */
  idleMs?: number;
}

/** What a connection that went quiet for too long is aborted with. */
const IDLE = new Error("the stream sent nothing for too long");

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
    wait = pause,
    onReconnecting = () => undefined,
    idleMs = IDLE_MS,
  } = watching;
  let body: ReadableStream<Uint8Array> | null = first;
  let position = from;
  let tries = 0;
  let reconnecting = false;
  const reconnected = (now: boolean) => {
    if (now !== reconnecting) onReconnecting(now);
    reconnecting = now;
  };
  try {
    for (;;) {
      let carried = false;
      // **One connection's own abort**, so that a stream that went quiet can be
      // dropped without the watch: the watcher's signal still ends both.
      const connection = new AbortController();
      const stop = () => connection.abort(signal?.reason);
      signal?.addEventListener("abort", stop, { once: true });
      let idle = setTimeout(() => connection.abort(IDLE), idleMs);
      const heard = () => {
        clearTimeout(idle);
        idle = setTimeout(() => connection.abort(IDLE), idleMs);
      };
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
        reconnected(false);
        for await (const block of blocks(body, connection.signal, heard)) {
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
        if (signal?.aborted === true) throw failure;
        // A stream that went quiet was dropped here, and is picked up below.
        // **A refusal is an answer, not a connection that went.** A run that is
        // not this person's (404), a position it has not reached (422), a
        // session that ended (401): asking again asks the same question and
        // gets the same answer, and a 401 has already put the interface back to
        // signed-out. Only a request that did not arrive, or a deployment
        // answering 5xx, is worth another try.
        if (refusalOf(failure)) throw failure;
      } finally {
        clearTimeout(idle);
        signal?.removeEventListener("abort", stop);
      }
      body = null;
      if (carried) tries = 0;
      // Here three ways -- the body ended, reading it threw, or the request for
      // it did -- and none of them is the run saying it is over. So the
      // connection went and the run did not: it is picked up where it was left
      // off.
      if (tries >= RETRIES) {
        throw new ApiError(0, "stream_lost", LOST);
      }
      reconnected(true);
      await wait(backoff(tries));
      tries += 1;
    }
  } finally {
    reconnected(false);
  }
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
