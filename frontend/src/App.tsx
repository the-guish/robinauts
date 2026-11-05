// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The session first: until `GET /auth/session` has answered there is nothing
 * to show, and with nobody signed in the sign-in page stands in place of
 * every page (`docs/specs/sign-in.md`, `docs/specs/frontend.md`).
 *
 * **The session is read before the route is**, with one exception. Which
 * page the hash names is the shell's business (`src/router.ts`), and it only
 * ever has one while somebody is signed in; until then the hash is the
 * sign-in page's, which reads its error code and its return target out of
 * it for itself.
 *
 * The exception is `#/link`, the page a pairing code opens in a browser
 * (`./session/LinkPage.tsx`). It is shown whatever the session says, before
 * it has even answered: the browser a phone's camera app opens is signed in
 * to nothing, and the page needs nothing from a session to say what it says.
 */
import type { ReactNode } from "react";

import { ErrorBoundary } from "./ErrorBoundary";
import { useRoute } from "./router";
import { LinkPage } from "./session/LinkPage";
import { loadSession, useSession } from "./session/session";
import "./session/session.css";
import { SignInPage } from "./session/SignInPage";
import { Shell } from "./shell/Shell";

/**
 * Everything is inside the boundary, the sign-in page included.
 *
 * A render that throws unmounts the whole tree and leaves a blank page. That
 * is worst on the sign-in page, which is the only page somebody who is not
 * signed in can reach: a blank one there is a deployment nobody can get into
 * and no way to tell why. So the boundary is outermost, not around the shell
 * alone, and its fallback is the one thing that always works -- a message
 * and a reload.
 */
export function App() {
  return (
    <ErrorBoundary>
      <Pages />
    </ErrorBoundary>
  );
}

function Pages() {
  const { status, session, error } = useSession();
  const route = useRoute();
  if (route.kind === "link") {
    return <LinkPage />;
  }
  if (status === "loading") {
    return <Centred>Loading…</Centred>;
  }
  if (session === null) {
    // The very first ask failed, so nothing is known: not whether sign-in is
    // configured, not who is in. There is nothing to show but a way to ask
    // again.
    return (
      <Centred>
        <p role="alert" className="page-failed">
          The session could not be loaded
          {error === null ? "" : `: ${error.detail}`}
        </p>
        <button
          type="button"
          onClick={() => void loadSession()}
          className="page-button"
        >
          Try again
        </button>
      </Centred>
    );
  }
  if (status === "signed-out") {
    return <SignInPage session={session} />;
  }
  return <Shell session={session} />;
}

function Centred({ children }: Readonly<{ children: ReactNode }>) {
  return <main className="page page-centred">{children}</main>;
}
