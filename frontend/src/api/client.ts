// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The typed client for our own API.
 *
 * The types come from `schema.d.ts`, which `npm run generate` writes from
 * `backend/openapi.json` -- the snapshot the backend commits and its tests
 * keep honest. The generated file is never committed: a route that changed
 * without the snapshot being refreshed is a failing type check here, not a
 * surprise in a browser.
 *
 * This is the whole of it. The AG-UI stream of a run is not `fetch`-and-JSON
 * and does not belong here; it arrives with the chat, behind `src/chat/`
 * (ADR 0001).
 */
import type { components, paths } from "./schema";

/** Every refusal the API makes, in one shape. */
export type ErrorResponse = components["schemas"]["ErrorResponse"];

/**
 * A refusal, as something to catch.
 *
 * `error` is the fixed code a caller may branch on; `detail` is a sentence
 * for a person. A body that is not the documented shape -- a proxy's HTML
 * error page, say -- still becomes one of these, with `error` naming the
 * status, because a caller has to be able to treat every failure the same.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly error: string;
  readonly detail: string;

  constructor(status: number, error: string, detail: string) {
    super(`${status} ${error}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.error = error;
    this.detail = detail;
  }
}

/**
 * A model the request named that the deployment does not offer: a new
 * chat's, or the one a conversation is moved to (422, `docs/specs/wire.md`).
 */
export const UNKNOWN_MODEL = "UnknownModelError";

/**
 * A turn in a conversation whose model the deployment no longer offers
 * (409): the conversation stands in the way until it is moved to another.
 */
export const MODEL_NOT_OFFERED = "ModelNotOfferedError";

/** Whether that failure is the backend refusing with the error of that name. */
export function isRefusal(failure: unknown, error: string): boolean {
  return failure instanceof ApiError && failure.error === error;
}

/**
 * The sentence to put in front of a person when a call did not work.
 *
 * Every failure a caller can meet is an `ApiError` (see `request`), and its
 * `detail` is the backend's own sentence, written for a reader and already
 * cleared of anything that came from outside (`api/errors.py`). The other
 * branch is for what never came from a call at all -- a bug in a handler
 * above this -- and is there so that nothing is ever shown an empty message.
 */
export function detailOf(failure: unknown): string {
  return failure instanceof ApiError ? failure.detail : String(failure);
}

/**
 * What is told when a call is answered 401.
 *
 * A session ends between two requests, and the page finds out from whichever
 * call happens to be next. The session store subscribes here and puts the
 * interface back to signed-out (`src/session/session.ts`); nothing else does.
 *
 * A callback rather than an event on `window`: the interface's own state is
 * not something any script sharing the page should be able to raise, and a
 * subscription that is returned as a function is one a test can take back.
 *
 * `/auth/session` itself is never a 401 (`docs/specs/sign-in.md`), so this
 * cannot fire for the very call that would answer it.
 */
const unauthorized = new Set<() => void>();

export function onUnauthorized(listener: () => void): () => void {
  unauthorized.add(listener);
  return () => {
    unauthorized.delete(listener);
  };
}

type Method = "get" | "put" | "post" | "delete" | "patch";

/** The paths that declare this method; anything else is a type error. */
type PathsWith<M extends Method> = {
  [P in keyof paths]: [paths[P][M]] extends [undefined] ? never : P;
}[keyof paths];

/** The operation behind one path and method. */
type Operation<P extends keyof paths, M extends Method> = Extract<
  paths[P][M],
  { responses: unknown }
>;

type Json<T> = T extends { content: { "application/json": infer B } }
  ? B
  : never;

/** What a call gives back: the 200 body, or nothing for a 204. */
type Result<O> = O extends { responses: { 200: infer R } }
  ? Json<R>
  : O extends { responses: { 204: unknown } }
    ? void
    : never;

type PathPart<O> = O extends { parameters: { path: infer T } }
  ? { path: T }
  : { path?: never };
type QueryPart<O> = O extends { parameters: { query?: infer T } }
  ? { query?: T }
  : { query?: never };
type BodyPart<O> = [
  Json<O extends { requestBody: infer B } ? B : never>,
] extends [never]
  ? { body?: never }
  : { body: Json<O extends { requestBody: infer B } ? B : never> };

type Options<O> = PathPart<O> &
  QueryPart<O> &
  BodyPart<O> & { signal?: AbortSignal };

/**
 * Same origin, always: the backend serves these files, so a relative URL is
 * the API. It is what makes the rest work -- the browser sends the
 * `__Host-` session cookie, `Sec-Fetch-Site: same-origin` and an `Origin`
 * the backend recognises, which is what stands in place of a CSRF token
 * (`docs/specs/sign-in.md`). An absolute URL to another host would take all
 * three away, so there is nothing here to configure one with.
 */
const BASE_URL = "";

function url(
  path: string,
  params: Record<string, string>,
  query: unknown,
): string {
  const filled = path.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = params[name];
    if (value === undefined) {
      throw new TypeError(`${path} needs a path parameter ${name}`);
    }
    return encodeURIComponent(value);
  });
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(query ?? {})) {
    // A parameter left out and one explicitly null are the same request.
    if (value !== undefined && value !== null) {
      search.set(name, String(value));
    }
  }
  const tail = search.toString();
  const suffix = tail === "" ? "" : `?${tail}`;
  return `${BASE_URL}${filled}${suffix}`;
}

/**
 * A response that is not a 200, as the one failure a caller handles.
 *
 * Exported because `request` below is not the only thing that calls this API:
 * the chat's own client opens the streaming routes with `fetch` directly --
 * they answer server-sent events rather than JSON and are outside the OpenAPI
 * document (`docs/specs/wire.md`) -- and a refusal there is a refusal like
 * any other. One mapping from a `Response` to an `ApiError`, and one place
 * where a 401 puts the interface back to signed-out.
 */
export async function refused(response: Response): Promise<ApiError> {
  if (response.status === 401) {
    // Told before the refusal is thrown, so that a caller catching it already
    // sees an interface that knows the session is gone. A listener that
    // throws is its own bug and must not swallow the refusal.
    for (const listener of [...unauthorized]) {
      try {
        listener();
      } catch {
        // Nothing here can do anything about it.
      }
    }
  }
  return refusal(response);
}

async function refusal(response: Response): Promise<ApiError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    // A refusal that is not JSON at all: a proxy, or a server that fell over.
  }
  const named =
    typeof body === "object" &&
    body !== null &&
    typeof (body as ErrorResponse).error === "string" &&
    typeof (body as ErrorResponse).detail === "string";
  return named
    ? new ApiError(
        response.status,
        (body as ErrorResponse).error,
        (body as ErrorResponse).detail,
      )
    : new ApiError(
        response.status,
        `http_${response.status}`,
        "the server answered with something that is not one of our errors",
      );
}

/**
 * The methods that change nothing, and so have nothing to prove.
 *
 * `api.protection.SAFE_METHODS`, spelt the same: GET, HEAD, OPTIONS.
 * Everything else is a **write**, and the backend refuses a write that is not
 * `application/json`, body or no body ("It is JSON. ... This holds for every
 * write, session or none"). A bodyless `DELETE` or `POST` that left the
 * header out would be answered 415, so the header goes on every write.
 * Asking for that type is also what makes the browser preflight a
 * cross-origin attempt, which nothing there answers.
 *
 * The list is longer than the `Method` union, which is what the document
 * gives: it is the backend's rule, copied, not a list of the methods this
 * client happens to send today.
 */
const SAFE_METHODS = new Set(["get", "head", "options"]);

/**
 * One call. JSON in, JSON out, an `ApiError` on anything else.
 *
 * `method` and `path` are checked against the document: a path the API does
 * not serve, or a method it does not declare for that path, does not compile,
 * and the type of what comes back is the type the route says it returns.
 *
 * Everything a caller has to handle is an `ApiError`: a refusal, a request
 * that never arrived, and an answer that should have been JSON and was not.
 * One thing is not: a caller that aborted gets back whatever its own
 * `AbortSignal` gave as the reason, because that is not a failure of the API
 * and a caller must be able to tell the two apart.
 */
export async function request<M extends Method, P extends PathsWith<M>>(
  method: M,
  path: P,
  ...rest: Record<string, never> extends Options<Operation<P, M>>
    ? [options?: Options<Operation<P, M>>]
    : [options: Options<Operation<P, M>>]
): Promise<Result<Operation<P, M>>> {
  const options = (rest[0] ?? {}) as {
    path?: Record<string, string>;
    query?: unknown;
    body?: unknown;
    signal?: AbortSignal;
  };
  const headers: Record<string, string> = { accept: "application/json" };
  if (!SAFE_METHODS.has(method)) {
    headers["content-type"] = "application/json";
  }
  let response: Response;
  try {
    response = await fetch(url(path, options.path ?? {}, options.query), {
      method: method.toUpperCase(),
      headers,
      credentials: "same-origin",
      ...(options.body === undefined
        ? {}
        : { body: JSON.stringify(options.body) }),
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    });
  } catch (failure) {
    // The request never arrived, or the answer never did. An abort is the
    // caller's own doing: whatever reason it gave is handed back as it is,
    // rather than being dressed up as a failure of the API.
    if (options.signal?.aborted === true) {
      throw failure;
    }
    throw new ApiError(
      0,
      "network_error",
      failure instanceof Error ? failure.message : "the request did not arrive",
    );
  }
  if (!response.ok) {
    throw await refused(response);
  }
  // A 202 here says "accepted, still under way" and carries nothing to read:
  // a cancel whose run is still ending (`docs/specs/wire.md`).
  if (response.status === 204 || response.status === 202) {
    return undefined as Result<Operation<P, M>>;
  }
  try {
    return (await response.json()) as Result<Operation<P, M>>;
  } catch (failure) {
    // A body arrives after the headers do, so this is the other place an
    // abort lands, and it is the caller's there too.
    if (options.signal?.aborted === true) {
      throw failure;
    }
    // A 200 whose body is not JSON: something between us and the backend
    // answered instead of it, and a caller must not read that as data.
    throw new ApiError(
      response.status,
      "unreadable_response",
      "the answer was not the JSON the document says it is",
    );
  }
}
