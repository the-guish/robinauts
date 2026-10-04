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
 * - **is patient about it**: while the run lives it never gives up, backing
 *   off from half a second to thirty, trying again at once when the network
 *   comes back or the tab is looked at again, and saying so to its caller,
 *   who can ask for a try now;
 * - **notices a connection that went quiet**: the backend says a comment at
 *   least every fifteen seconds (`docs/specs/wire.md`), so one that has said
 *   nothing for `IDLE_MS` is a connection something in between dropped
 *   without closing, and it is opened again.
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
 * How long before the first try at a dropped stream, and the longest wait
 * between two: each wait doubles the one before, up to that.
 *
 * **There is no last try.** A run that is still going is still going on the
 * server, and a deployment restarting, a proxy closing a connection, a laptop
 * asleep or changing network are all things that end; only a refusal (a run
 * that is not there, a session that ended) ends the watch.
 */
export const FIRST_WAIT_MS = 500;
export const LONGEST_WAIT_MS = 30_000;

/**
 * How long a connection may say nothing at all before it is taken for one
 * that went: three of the backend's fifteen-second keep-alives.
 */
export const IDLE_MS = 45_000;

/**
 * How long to wait before try number `tries`, counted from zero: doubling,
 * capped, and spread by a fifth either way, so that every tab a restart cut
 * off does not come back in the same instant.
 */
export function backoff(
  tries: number,
  random: () => number = Math.random,
): number {
  const base = Math.min(LONGEST_WAIT_MS, FIRST_WAIT_MS * 2 ** tries);
  return Math.round(base * (0.8 + 0.4 * random()));
}

/** What a watch says while it is waiting to open a dropped stream again. */
export interface Reconnecting {
  /** How many tries since the stream last delivered: 1 for the first. */
  tries: number;
  /** How long until this one. */
  inMs: number;
  /** Try now rather than then. */
  now: () => void;
}

/** What a connection that went quiet is ended with. */
const IDLE = "the connection said nothing for too long";
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
   * Told when a dropped stream is about to be opened again, and `null` once
   * a stream delivers again: what the interface says meanwhile.
   */
  onReconnecting?: (reconnecting: Reconnecting | null) => void;
  /** `IDLE_MS`, unless a test says otherwise. */
  idleMs?: number;
  /** Where the spread of the waits comes from. */
  random?: () => number;
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
    onReconnecting,
    idleMs = IDLE_MS,
    random = Math.random,
  } = watching;
  let body: ReadableStream<Uint8Array> | null = first;
  let position = from;
  let tries = 0;
  for (;;) {
    let carried = false;
    // **One connection, one controller**: the watcher's own signal ends it,
    // and so does a connection that has said nothing for `idleMs`, which
    // ends this connection and not the watch.
    const connection = new AbortController();
    const unlink = linked(signal, connection);
    let idle: ReturnType<typeof setTimeout> | undefined;
    const heard = () => {
      clearTimeout(idle);
      idle = setTimeout(() => {
        connection.abort(new ApiError(0, "stream_idle", IDLE));
      }, idleMs);
    };
    try {
      heard();
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
            // waits below are about connections that do not: they count
            // tries since the last event the platform numbered, not since
            // the watch began.
            if (!carried && tries > 0) onReconnecting?.(null);
            carried = true;
          } else if (!over) {
            // **A numbered event that does not move the position forward is
            // one this watcher has had in full.** An id means "everything
            // derived from the run's events up to here has been sent"
            // (`docs/specs/wire.md`), and this build never asks from before
            // what it has seen, so the backend cannot send one: what can is
            // something in front of the deployment replaying a block.
            // Reading it would say a delta twice, and counting it as
            // delivery would reset the waits below -- so a proxy replaying
            // one event and cutting would be reconnected to at once for ever.
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
        if (over) {
          if (tries > 0 && !carried) onReconnecting?.(null);
          return;
        }
      }
    } catch (failure) {
      // An abort is this watcher going away, not a stream that failed.
      if (signal?.aborted === true) throw failure;
      // **A refusal is an answer, not a connection that went.** A run that is
      // not this person's (404), a position it has not reached (422), a
      // session that ended (401): asking again asks the same question and
      // gets the same answer, and a 401 has already put the interface back to
      // signed-out. Only a request that did not arrive, a deployment
      // answering 5xx, or a connection that went quiet is worth another try.
      if (refusalOf(failure)) throw failure;
    } finally {
      clearTimeout(idle);
      unlink();
    }
    body = null;
    if (carried) tries = 0;
    // Here three ways -- the body ended, reading it threw, or the request for
    // it did -- and none of them is the run saying it is over. So the
    // connection went and the run did not: it is picked up where it was left
    // off, after a wait that grows while nothing gets through.
    const ms = backoff(tries, random);
    tries += 1;
    await pause(ms, wait, signal, (now) => {
      onReconnecting?.({ tries, inMs: ms, now });
    });
    // A watcher that went away while this waited is told it was its own doing.
    if (signal?.aborted === true) throw signal.reason;
  }
}

/** That controller aborted when that signal is; undone by what it returns. */
function linked(
  signal: AbortSignal | undefined,
  connection: AbortController,
): () => void {
  if (signal === undefined) return () => undefined;
  if (signal.aborted) {
    connection.abort(signal.reason);
    return () => undefined;
  }
  const abort = () => {
    connection.abort(signal.reason);
  };
  signal.addEventListener("abort", abort, { once: true });
  return () => {
    signal.removeEventListener("abort", abort);
  };
}

/**
 * Wait `ms`, or less: until the network is back, the tab is looked at again,
 * the watcher goes away, or somebody asks for a try now through `told`.
 */
function pause(
  ms: number,
  wait: (ms: number) => Promise<void>,
  signal: AbortSignal | undefined,
  told: (now: () => void) => void,
): Promise<void> {
  return new Promise<void>((resolve) => {
    let done = false;
    const visible = () => {
      if (document.visibilityState === "visible") now();
    };
    const now = () => {
      if (done) return;
      done = true;
      if (typeof window !== "undefined") {
        window.removeEventListener("online", now);
        document.removeEventListener("visibilitychange", visible);
      }
      signal?.removeEventListener("abort", now);
      resolve();
    };
    if (typeof window !== "undefined") {
      window.addEventListener("online", now);
      document.addEventListener("visibilitychange", visible);
    }
    signal?.addEventListener("abort", now, { once: true });
    told(now);
    void wait(ms).then(now, now);
  });
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
