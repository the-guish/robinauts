// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The interface: oneberth's shell with two applications, Robinauts and neorc
 * (oneberth's `shell.md`). The shell holds the session, the sign-in page,
 * the rail, the theme toggle, the account button and the account pages;
 * `./apps.tsx` registers the two applications.
 *
 * **One page stands outside the shell**: `#/link`, the page a pairing code
 * opens in a browser (`./LinkPage.tsx`). It is shown whatever the session
 * says, before it has even answered: the browser a phone's camera app opens
 * is signed in to nothing, and the page needs nothing from a session.
 */
import { Shell } from "@oneberth/ui";
import { useSyncExternalStore } from "react";

import { APPS } from "./apps";
import { ErrorBoundary } from "./ErrorBoundary";
import { isLinkHash, LinkPage } from "./LinkPage";

function subscribe(changed: () => void): () => void {
  window.addEventListener("hashchange", changed);
  return () => {
    window.removeEventListener("hashchange", changed);
  };
}

/**
 * Everything is inside the boundary. A render that throws unmounts the whole
 * tree and leaves a blank page; the boundary's fallback is the one thing that
 * always works, a message and a reload. An application that throws is caught
 * by the shell first, and the rest of the shell stays.
 */
export function App() {
  const link = useSyncExternalStore(
    subscribe,
    () => isLinkHash(location.hash),
    () => false,
  );
  return (
    <ErrorBoundary>
      {link ? <LinkPage /> : <Shell apps={APPS} title="Robinauts" />}
    </ErrorBoundary>
  );
}
