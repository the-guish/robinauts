// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Where the interface is, written in the hash of the address bar.
 *
 * Hash routing, by hand (`docs/specs/frontend.md`). The built files are
 * served under `/ui/` with `base: "./"`, and the hash is the one part of a
 * URL no server ever sees: there is no fallback route to configure, no path
 * prefix to build for, and nothing here depends on where the application is
 * mounted.
 *
 * No router library. Two routes, one event, and a store the size of this
 * file; a library would be a dependency to vet, to keep pinned and to carry
 * in the bundle for something React already provides through
 * `useSyncExternalStore` (`docs/contributing/js-dependencies.md`).
 *
 *     #/            the empty chat, ready for a first message
 *     #/c/<id>      that conversation
 *
 * **Anything else is the empty chat**, `#/sign-in?error=…` included -- the
 * hash the backend sends a failed sign-in back to, which
 * `src/session/SignInPage.tsx` reads for itself. Reading a hash never writes
 * one: an unknown hash is answered with the empty chat, and the address bar
 * is left saying whatever it said, so nothing here can destroy a hash
 * another part of the interface is still reading.
 */
import { useSyncExternalStore } from "react";

import type { ConversationId } from "./chat";

/** Where the interface is. */
export type Route =
  | { readonly kind: "new" }
  | { readonly kind: "conversation"; readonly id: ConversationId };

/** The empty chat: the route of a page that has been told nothing else. */
export const NEW_CHAT: Route = { kind: "new" };

/**
 * The shape of every id the backend gives a conversation.
 *
 * Checked before an id is put in a URL, and before one taken out of a URL is
 * put in a request. A `/` or a `#` in an id would otherwise forge a route,
 * and anything at all would become a path in an API call; this is the one
 * place either is judged, so neither can be decided twice and differently.
 */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Every id the backend issues is one of these.
 *
 * Read by the chat as well: the run and the conversation a stream is of come
 * back in response **headers**, which are as much "from outside" as a hash is
 * (`src/chat/agui/client.ts`), and both go straight into a
 * request path. One shape, judged in one place.
 */
export function isUuid(value: string): boolean {
  return UUID.test(value);
}

export function isConversationId(value: string): boolean {
  return isUuid(value);
}

/**
 * The route a hash names.
 *
 * `location.hash` includes the `#`; a hash that is empty, absent or none of
 * ours is the empty chat. The query of a hash is cut off first, because
 * `#/sign-in?error=…` is a hash with one, and nothing is percent-decoded:
 * an id this would have written needs no escaping, so a hash that carries an
 * escape is not one of ours.
 */
export function parseRoute(hash: string): Route {
  const path =
    (hash.startsWith("#") ? hash.slice(1) : hash).split("?")[0] ?? "";
  const parts = path.split("/");
  const id = parts[2];
  if (
    parts.length === 3 &&
    parts[0] === "" &&
    parts[1] === "c" &&
    id !== undefined &&
    isConversationId(id)
  ) {
    return { kind: "conversation", id };
  }
  return NEW_CHAT;
}

/**
 * The hash a route is written as: what a link's `href` holds.
 *
 * An id that is not one of ours is written as the empty chat rather than
 * pasted into a URL. It cannot come from `parseRoute`, which refused it, nor
 * from the API, which gives UUIDs; what it would be is a bug, and a link
 * that goes home is a smaller one than a hash somebody else composed.
 */
export function formatRoute(route: Route): string {
  return route.kind === "conversation" && isConversationId(route.id)
    ? `#/c/${route.id}`
    : "#/";
}

/** Go there. The hash is the whole of the navigation. */
export function navigate(route: Route): void {
  location.hash = formatRoute(route);
}

/**
 * The route last read, kept so that reading it twice gives one object.
 *
 * `useSyncExternalStore` compares snapshots by identity and re-renders --
 * for ever -- when a snapshot is a fresh object every time it is asked for.
 * So the hash is what is remembered, and the route is parsed again only when
 * the hash has actually changed.
 */
let readHash: string | null = null;
let readRoute: Route = NEW_CHAT;

function snapshot(): Route {
  const hash = location.hash;
  if (hash !== readHash) {
    readHash = hash;
    readRoute = parseRoute(hash);
  }
  return readRoute;
}

function subscribe(changed: () => void): () => void {
  window.addEventListener("hashchange", changed);
  return () => {
    window.removeEventListener("hashchange", changed);
  };
}

/** Where the interface is, as a value React re-renders on. */
export function useRoute(): Route {
  return useSyncExternalStore(subscribe, snapshot, () => NEW_CHAT);
}
