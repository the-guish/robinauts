// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Who is signed in, and signing out.
 *
 * `GET /auth/session` is asked once, when the application starts. It is never
 * a 401 -- "nobody is signed in" is an answer, not a refusal
 * (`docs/specs/sign-in.md`) -- so this is the one call that always tells the
 * interface where it stands, and everything else follows from what it said.
 *
 * A module-level store with `subscribe`/`getSnapshot`, read through
 * `useSyncExternalStore`. There is no data-fetching library here and no state
 * library: one value, asked for once, changed by two events -- signing out,
 * and any call answering 401.
 *
 * The design is neorc's, written again for this project; recorded in
 * `docs/legal/ip-clearance.md`.
 */
import { useEffect, useSyncExternalStore } from "react";

import { ApiError, onUnauthorized, request } from "../api/client";
import type { components } from "../api/schema";

/** What `GET /auth/session` answers. */
export type Session = components["schemas"]["UserSessionResponse"];
/** One provider to offer a button for. */
export type Provider = components["schemas"]["ProviderSummary"];
/** Who is signed in, as the profile block shows them. */
export type User = components["schemas"]["UserSummary"];

/**
 * Where the interface stands.
 *
 * `session` is what the last successful answer said, so the sign-in page
 * knows which providers to offer even after a session has ended. It is
 * `null` only when the very first ask failed, and then `error` says why and
 * there is nothing to show but a way to ask again.
 */
export interface SessionState {
  status: "loading" | "signed-in" | "signed-out";
  session: Session | null;
  error: ApiError | null;
}

const LOADING: SessionState = {
  status: "loading",
  session: null,
  error: null,
};

let state: SessionState = LOADING;
const listeners = new Set<() => void>();

function publish(next: SessionState): void {
  state = next;
  for (const listener of [...listeners]) listener();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function snapshot(): SessionState {
  return state;
}

/** Somebody is in: the local development mode's fixed user counts. */
function signedIn(session: Session): boolean {
  return session.user !== undefined && session.user !== null;
}

/**
 * Ask again who is signed in.
 *
 * A failure keeps the session that was there -- a dropped request is not a
 * sign-out, and throwing the shell away over one would lose whatever the
 * person was doing. Only a first ask that fails leaves nothing.
 */
export async function loadSession(): Promise<void> {
  // With a session in hand the shell stays up while this runs; with none --
  // a first ask that failed, and the "Try again" beneath it -- there is
  // nothing to keep on screen, and the refusal has just been cleared, so
  // going back to loading is what says something is happening.
  publish({
    status: state.session === null ? "loading" : state.status,
    session: state.session,
    error: null,
  });
  try {
    const session = await request("get", "/auth/session");
    publish({
      status: signedIn(session) ? "signed-in" : "signed-out",
      session,
      error: null,
    });
  } catch (failure) {
    publish({
      status: state.session === null ? "signed-out" : state.status,
      session: state.session,
      error:
        failure instanceof ApiError
          ? failure
          : new ApiError(0, "unknown_error", String(failure)),
    });
  }
}

let started = false;

/** The one ask a loaded page makes. Called again, it does nothing. */
export function startSession(): Promise<void> {
  if (started) return Promise.resolve();
  started = true;
  return loadSession();
}

/**
 * Back to the state a freshly loaded page is in.
 *
 * The store is module state, which one test would otherwise hand to the
 * next. Nothing in the application calls this.
 */
export function resetSession(): void {
  started = false;
  publish(LOADING);
}

/**
 * Nobody is signed in, said without asking anybody.
 *
 * The last answer is kept rather than thrown away -- `sign_in` and the
 * providers are still the deployment's, and they are what the sign-in page
 * needs -- with the user removed, which is what "signed out" is.
 */
function forgetUser(): void {
  if (state.session === null || !signedIn(state.session)) return;
  publish({
    status: "signed-out",
    session: { ...state.session, user: null },
    error: null,
  });
}

/**
 * End the session, and ask again.
 *
 * A 401 is a session that had already gone: signed out either way. Any other
 * refusal is thrown to the caller, which shows it beside the button, because
 * the session is still open.
 *
 * **Signed out before the ask, not after it.** Once the route has answered,
 * the session is gone on the server whatever happens next, so that is
 * published straight away; the ask that follows is only for what else may
 * have changed. Done the other way round, a re-ask that fails would leave
 * `loadSession` keeping the session it had -- the shell still up, the
 * person still apparently signed in, and their session actually ended.
 */
export async function signOut(): Promise<void> {
  try {
    await request("post", "/auth/logout");
  } catch (failure) {
    if (!(failure instanceof ApiError && failure.status === 401)) throw failure;
  }
  forgetUser();
  await loadSession();
}

/**
 * Any call answered 401: the session is gone, whatever this page still shows.
 *
 * Nothing is fetched: the interface has to stop trusting the session at
 * once, and the reload that a fetch would wait for might never arrive.
 */
onUnauthorized(forgetUser);

/** The state, and the one ask that starts it. */
export function useSession(): SessionState {
  const current = useSyncExternalStore(subscribe, snapshot);
  useEffect(() => {
    void startSession();
  }, []);
  return current;
}
