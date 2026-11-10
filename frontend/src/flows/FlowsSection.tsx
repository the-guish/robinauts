// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The panel's "Flows" section, below the history: the orchestrator's
 * navigation, which its framed page leaves out (`./FlowsPage.tsx`), so the
 * interface has one panel and one menu button.
 *
 * It holds what the orchestrator's own sidebar holds: a way to a new run, the
 * runs, its flows and its latest runs, each status a dot, kept up to date as
 * the orchestrator records events (`./orchestrator.ts`). In the rail, "New
 * run" and "Runs" stay as their icons and the lists go, as the history does.
 * Each link is a route of the shell's, so opened from the drawer it closes the
 * drawer, as a conversation does.
 */
import { PlayIcon, WorkflowIcon } from "../icons";
import { FLOWS, formatRoute, NEW_RUN, type FlowsView } from "../router";
import { useMenu } from "./orchestrator";
import "./flows.css";

export function FlowsSection({
  url,
  view,
  onOpened,
}: Readonly<{
  url: string;
  /** The orchestrator's page that is up, if one is. */
  view: FlowsView | null;
  onOpened: () => void;
}>) {
  const menu = useMenu(url);
  const at = (page: FlowsView["at"], name?: string) =>
    view !== null &&
    view.at === page &&
    (name === undefined ||
      (view.at === "run" && view.run === name) ||
      (view.at === "flow" && view.flow === name))
      ? ("page" as const)
      : undefined;
  return (
    <nav aria-label="Flows" className="flows-section">
      <h2 className="panel-heading panel-wide">Flows</h2>
      <a
        href={formatRoute(NEW_RUN)}
        title="New run"
        aria-label="New run"
        aria-current={at("new") ?? at("start")}
        onClick={onOpened}
        className="flows-link"
      >
        <PlayIcon className="panel-icon" />
        <span className="panel-wide">New run</span>
      </a>
      <a
        href={formatRoute(FLOWS)}
        title="Runs"
        aria-label="Runs"
        aria-current={at("runs")}
        onClick={onOpened}
        className="flows-link"
      >
        <WorkflowIcon className="panel-icon" />
        <span className="panel-wide">Runs</span>
      </a>
      {menu !== null && menu.flows.length > 0 && (
        <ul aria-label="Workflows" className="flows-list panel-wide">
          {menu.flows.map((flow) => (
            <li key={flow}>
              <a
                href={formatRoute({
                  kind: "flows",
                  view: { at: "flow", flow },
                })}
                title={flow}
                aria-current={at("flow", flow)}
                onClick={onOpened}
                className="flows-item"
              >
                <span aria-hidden="true" className="flows-dot" />
                <span className="flows-name">{flow}</span>
              </a>
            </li>
          ))}
        </ul>
      )}
      {menu !== null && menu.runs.length > 0 && (
        <>
          <h3 className="flows-heading panel-wide">Recent runs</h3>
          <ul aria-label="Recent runs" className="flows-list panel-wide">
            {menu.runs.map((run) => (
              <li key={run.id}>
                <a
                  href={formatRoute({
                    kind: "flows",
                    view: { at: "run", run: run.id },
                  })}
                  title={`${run.flow} ${run.id.slice(0, 8)}, ${run.status}`}
                  aria-current={at("run", run.id)}
                  onClick={onOpened}
                  className="flows-item"
                >
                  <span
                    aria-hidden="true"
                    data-status={run.status}
                    className="flows-dot"
                  />
                  <span className="flows-name">{run.flow}</span>
                  <span className="flows-id">{run.id.slice(0, 8)}</span>
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
    </nav>
  );
}
