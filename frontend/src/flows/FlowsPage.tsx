// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * "Flows": neorc's own UI, in a frame, where the chat would be
 * (`docs/architecture/orchestration.md`, "The Flows page").
 *
 * Nothing of neorc's is imported: the runtime of this package is React alone
 * (ADR 0006). The backend serves the manager it started beside itself under
 * `/neorc/`, its UI at `/neorc/ui/` with the API right above it, where that
 * UI looks for it, so the frame needs nothing but an address. neorc routes by
 * the hash, so a run of this page's (`#/flows/runs/<id>`) is that run of
 * neorc's (`#/runs/<id>`). A change of the hash alone does not reload a
 * frame, so moving between the two keeps neorc's page and its event poll.
 *
 * **The frame is on this server's origin**, and so is every script neorc's
 * bundle carries. What holds it in: the backend narrows the frame's
 * `connect-src` to `/neorc/` and lets this server alone frame it; the
 * `sandbox` below keeps it from navigating this page, opening windows,
 * downloading or showing dialogs. `allow-same-origin` is the price of neorc
 * reaching its API at all, and with it a script in the frame can reach this
 * page; that is why the backend offers the page in the local development
 * mode alone.
 */
import { useRoute, type Route } from "../router";
import "./flows.css";

/** Where the backend serves neorc's UI. */
export const NEORC_UI = "/neorc/ui/";

/** What the frame may do; see above for what it may not. */
export const FRAME_SANDBOX = "allow-scripts allow-same-origin allow-forms";

/** The address of neorc's page for a route of this one's. */
export function neorcAddress(route: Route): string {
  if (route.kind === "flows" && route.run !== null) {
    return `${NEORC_UI}#/runs/${route.run}`;
  }
  return `${NEORC_UI}#/runs`;
}

export function FlowsPage({ available }: Readonly<{ available: boolean }>) {
  const route = useRoute();
  return (
    <div className="flows">
      <h1 className="flows-title">Flows</h1>
      {available ? (
        <iframe
          title="neorc"
          src={neorcAddress(route)}
          sandbox={FRAME_SANDBOX}
          referrerPolicy="same-origin"
          className="flows-frame"
        />
      ) : (
        <p className="flows-off">
          No orchestrator is running. Flows are there when the server starts in
          the local development mode with an <code>[orchestration]</code> table
          whose <code>ui</code> is on.
        </p>
      )}
    </div>
  );
}
