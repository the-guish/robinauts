// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * `#/link`: what a browser shows when a phone's camera app opens a pairing
 * code (`docs/architecture/device-pairing.md`, step 3).
 *
 * The code is a URL of the deployment, `{public_url}/#/link?v=1&c=<code>`,
 * which oneberth's pairing writes, and it is for the Robinauts app, which
 * reads it itself. A plain camera app hands it to a browser instead, and this
 * page says so. It stands outside oneberth's shell (`./App.tsx`): `link` is
 * no application's name, and the browser on the phone is signed in to
 * nothing. It explains and does nothing: it reads no parameter, so the code
 * is never used here.
 */
import "./page.css";

export function LinkPage() {
  return (
    <main className="page">
      <h1 className="page-title">Open this in the Robinauts app</h1>
      <p className="page-text">
        This code is for the Robinauts app on a phone. Open the app and scan it
        there.
      </p>
    </main>
  );
}

/** Whether a hash is the link page's: `#/link`, with or without its query. */
export function isLinkHash(hash: string): boolean {
  const path = (hash.startsWith("#") ? hash.slice(1) : hash).split("?")[0];
  return path === "/link";
}
