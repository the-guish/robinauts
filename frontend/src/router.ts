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
 * No router library. Six routes, one event, and a store the size of this
 * file; a library would be a dependency to vet, to keep pinned and to carry
 * in the bundle for something React already provides through
 * `useSyncExternalStore` (`docs/contributing/js-dependencies.md`).
 *
 *     #/            the empty chat, ready for a first message
 *     #/c/<id>      that conversation
 *     #/devices     "Linked devices": the phones and the API tokens
 *     #/link        what a phone's camera app opens a pairing code at
 *     #/flows       the orchestrator's runs, when the server has one
 *     #/flows/new   its flows, to pick one to start
 *     #/flows/new/<name>  the form that starts a run of that flow
 *     #/flows/runs/<id>   that run
 *     #/flows/flow/<name> that flow
 *
 * The flows routes are what the panel's "Flows" section links to. The two
 * that start a run are this interface's own pages, over `/api/flows`
 * (`./flows/NewRunPage.tsx`); the others frame the orchestrator's page, which
 * the session names (`flows_url`). Inside the frame the orchestrator keeps a
 * hash of its own, which `./flows/orchestrator.ts` translates both ways.
 *
 * `#/link?v=1&c=…` is the URL inside a pairing QR
 * (`docs/architecture/device-pairing.md`). Its query is cut off like any
 * other and is never read: the code is for the app, not for this page.
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
  | { readonly kind: "conversation"; readonly id: ConversationId }
  | { readonly kind: "devices" }
  | { readonly kind: "link" }
  | { readonly kind: "flows"; readonly view: FlowsView };

/** Which of the flows pages the flows routes show. */
export type FlowsView =
  | { readonly at: "runs" }
  | { readonly at: "new" }
  | { readonly at: "start"; readonly flow: string }
  | { readonly at: "run"; readonly run: string }
  | { readonly at: "flow"; readonly flow: string };

/** Whether a view is one of this interface's own pages, not the frame's. */
export function isOwnFlowsPage(view: FlowsView): boolean {
  return view.at === "new" || view.at === "start";
}

/** The empty chat: the route of a page that has been told nothing else. */
export const NEW_CHAT: Route = { kind: "new" };

/** "Linked devices". */
export const DEVICES: Route = { kind: "devices" };

/** The page a pairing code opens in a browser. */
export const LINK: Route = { kind: "link" };

/** The orchestrator's runs. */
export const FLOWS: Route = { kind: "flows", view: { at: "runs" } };

/** The flows, where one is picked to start a run of. */
export const NEW_RUN: Route = { kind: "flows", view: { at: "new" } };

/**
 * The shape of a flow's name, as the orchestrator allows one: an
 * identifier. Judged here for the same reason as an id: a name goes into a
 * URL, and a `/` or a `#` in it would forge another.
 */
const FLOW_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,199}$/;

export function isFlowName(value: string): boolean {
  return FLOW_NAME.test(value);
}

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
  if (path === "/devices") return DEVICES;
  if (path === "/link") return LINK;
  if (path === "/flows") return FLOWS;
  if (path === "/flows/new") return NEW_RUN;
  const parts = path.split("/");
  const name = parts[3];
  if (
    parts.length === 4 &&
    parts[0] === "" &&
    parts[1] === "flows" &&
    name !== undefined
  ) {
    if (parts[2] === "runs" && isUuid(name)) {
      return { kind: "flows", view: { at: "run", run: name } };
    }
    if (parts[2] === "flow" && isFlowName(name)) {
      return { kind: "flows", view: { at: "flow", flow: name } };
    }
    if (parts[2] === "new" && isFlowName(name)) {
      return { kind: "flows", view: { at: "start", flow: name } };
    }
  }
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
  switch (route.kind) {
    case "devices":
      return "#/devices";
    case "link":
      return "#/link";
    case "flows":
      return formatFlowsView(route.view);
    case "conversation":
      return isConversationId(route.id) ? `#/c/${route.id}` : "#/";
    default:
      return "#/";
  }
}

function formatFlowsView(view: FlowsView): string {
  switch (view.at) {
    case "new":
      return "#/flows/new";
    case "start":
      return isFlowName(view.flow) ? `#/flows/new/${view.flow}` : "#/flows/new";
    case "run":
      return isUuid(view.run) ? `#/flows/runs/${view.run}` : "#/flows";
    case "flow":
      return isFlowName(view.flow) ? `#/flows/flow/${view.flow}` : "#/flows";
    default:
      return "#/flows";
  }
}

/**
 * Be there now, in place of where the page was: the address bar says so and
 * every `useRoute` hears it, but Back skips it. For a move that has already
 * happened and taken its own step in the history, as a page in the
 * orchestrator's frame does (`./flows/FlowsPage.tsx`).
 */
export function replaceRoute(route: Route): void {
  const hash = formatRoute(route);
  if (location.hash === hash) return;
  history.replaceState(history.state, "", hash);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
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
