// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * "Flows", at `#/flows` and `#/flows/runs/<id>`: the orchestrator's own
 * page, framed where the chat would be (`docs/architecture/orchestration.md`).
 *
 * The server says where that page is, in the session's `flows_url`, and
 * only when it serves one: this file knows nothing of what draws it. The
 * page is the server's, on its origin, so the frame needs no sandbox
 * attribute to reach its own API, and the server's headers let this page
 * alone frame it.
 *
 * Inside the frame the orchestrator routes by a hash of its own, so a run
 * is opened by writing `#/runs/<id>` after the page's address. Once open,
 * what the frame does is its own business: moving about inside it changes
 * nothing out here, and a run link in the address bar opens that run again.
 */
import { isUuid } from "../router";
import "./flows.css";

/** The frame's address: the page, and the run to open on it if any. */
export function flowsPage(url: string, run: string | null): string {
  return run !== null && isUuid(run) ? `${url}#/runs/${run}` : url;
}

export function FlowsPage({
  url,
  run,
}: Readonly<{ url: string; run: string | null }>) {
  return (
    <div className="flows">
      <iframe
        // A new address is a new page: React would otherwise move the
        // frame's `src`, which only some browsers treat as a navigation
        // when nothing but the hash differs.
        key={run ?? ""}
        title="Flows"
        src={flowsPage(url, run)}
        className="flows-frame"
      />
    </div>
  );
}
