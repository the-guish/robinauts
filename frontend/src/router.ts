// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Where Robinauts' application is: its route, which oneberth's shell gives it.
 *
 * The shell owns the hash (oneberth's `shell.md`, "Routes"). Its first
 * segment names the application, `#/robinauts/…`, and the application gets
 * the rest as its route and changes it through the shell's `navigate`. The
 * shell keeps the route of a hidden application as it was, so nothing here
 * reads `location.hash`: a page that did would read neorc's hash while neorc
 * is shown and lose the conversation on the screen.
 *
 *     ""            the empty chat, ready for a first message
 *     /c/<id>       that conversation
 *     /search       the search page
 *
 * **Any other route is the empty chat.** Reading a route never writes one:
 * an unknown route is answered with the empty chat, and the address bar is
 * left saying whatever it said.
 */
import { createContext, useContext } from "react";

import type { ConversationId } from "./chat";

/** Where the application is. */
export type Route =
  | { readonly kind: "new" }
  | { readonly kind: "search" }
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
 * The route the shell's route names.
 *
 * The shell gives the part of the hash after `#/robinauts`: empty, or
 * starting with `/` or `?`. A query is cut off first, and nothing is
 * percent-decoded: an id this would have written needs no escaping, so a
 * route that carries an escape is not one of ours.
 */
export function parseRoute(route: string): Route {
  const path = route.split("?")[0] ?? "";
  const parts = path.split("/");
  if (path === "/search") return { kind: "search" };
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
 * The shell's route a route is written as.
 *
 * An id that is not one of ours is written as the empty chat rather than
 * pasted into a URL. It cannot come from `parseRoute`, which refused it, nor
 * from the API, which gives UUIDs; what it would be is a bug, and a link
 * that goes home is a smaller one than a hash somebody else composed.
 */
export function formatRoute(route: Route): string {
  if (route.kind === "search") return "/search";
  return route.kind === "conversation" && isConversationId(route.id)
    ? `/c/${route.id}`
    : "";
}

/** What the application knows of where it is, and how it moves. */
export interface Router {
  readonly route: Route;
  /** Go there, through the shell. */
  navigate(route: Route): void;
  /** The `href` of a link there. */
  href(route: Route): string;
}

/**
 * Outside the application, as in a test of one component: the empty chat,
 * and moves that go nowhere.
 */
export const RouterContext = createContext<Router>({
  route: NEW_CHAT,
  navigate: () => undefined,
  href: (route) => `#/robinauts${formatRoute(route)}`,
});

export function useRouter(): Router {
  return useContext(RouterContext);
}

/** Where the application is, as a value React re-renders on. */
export function useRoute(): Route {
  return useContext(RouterContext).route;
}
