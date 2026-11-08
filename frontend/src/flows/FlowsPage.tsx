// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * "Flows", at `#/flows` and `#/flows/runs/<id>`: neorc's UI, framed in the
 * page of the shell (`docs/architecture/orchestration.md`).
 *
 * **neorc's UI stays on its own origin.** Its manager serves it at
 * `flows_url`, on a port of its own, which is another origin than this
 * page's: nothing neorc's code does can read this server's cookies, call its
 * API as the person signed in, or reach into this page. That is the point of
 * framing it rather than serving it from here. neorc lets itself be framed by
 * this server's origin alone (its manager's `--frame-ancestors`), and this
 * page's policy frames neorc's origin and no other (`frame-src`).
 *
 * The frame opens neorc in its embedded mode, `?embed=1`, which leaves out
 * its own left navigation: the panel is this one. `#/flows` is neorc's list
 * of runs, `#/runs`, and `#/flows/runs/<id>` that run, `#/runs/<id>`. What
 * the reader does inside the frame stays there; this page's hash does not
 * follow it, since the two origins share nothing.
 *
 * The sandbox gives the frame what neorc needs and nothing more: its scripts,
 * its own origin -- to reach its API and keep its settings -- and its forms,
 * to start a run. No popups, no top navigation, no downloads.
 */
import "./flows.css";

/** What the frame's sandbox allows. */
export const FLOWS_SANDBOX = "allow-scripts allow-same-origin allow-forms";

/**
 * The frame's address: neorc's UI at `flowsUrl`, embedded, at the run given
 * or its list of runs. `null` when `flowsUrl` is not an http(s) URL, which
 * the server never sends; a frame of anything else is not drawn.
 */
export function flowsFrameUrl(
  flowsUrl: string,
  run: string | null,
): string | null {
  let url: URL;
  try {
    url = new URL(flowsUrl);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  url.search = "?embed=1";
  // The run has been judged a UUID by the router: it needs no escaping.
  url.hash = run === null ? "#/runs" : `#/runs/${run}`;
  return url.href;
}

export function FlowsPage({
  flowsUrl,
  run,
}: Readonly<{
  /** Where neorc's UI is, as `GET /auth/session` said; `null` without one. */
  flowsUrl: string | null;
  run: string | null;
}>) {
  const src = flowsUrl === null ? null : flowsFrameUrl(flowsUrl, run);
  return (
    <div className="flows">
      <h1 className="flows-title">Flows</h1>
      {src === null ? (
        <p className="flows-none">
          No orchestrator runs beside this server: start it with an
          [orchestration] table to see its flows here.
        </p>
      ) : (
        <iframe
          title="Flows in neorc"
          src={src}
          sandbox={FLOWS_SANDBOX}
          referrerPolicy="no-referrer"
          className="flows-frame"
        />
      )}
    </div>
  );
}
