// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The agent's MCP apps, as tabs the side panel offers
 * (`docs/architecture/web.md`, "Apps").
 *
 * The list is the backend's (`GET /api/apps?agent=`): the apps of the tool
 * servers the agent names. It is asked for again when the agent changes,
 * and a list that does not come is no apps: nothing to offer, and no
 * failure of the panel's.
 */
import { useEffect, useState } from "react";

import { request } from "../api/client";
import type { components } from "../api/schema";
import { appTabKey, type TabOffer } from "./tabs";

export type App = components["schemas"]["AppSummary"];

/** The apps of `agent`, none until they come; none for no agent. */
export function useApps(agent: string | null): readonly App[] {
  // Held with the agent they are of, so that another agent's list is never
  // offered while this one's is on its way.
  const [listed, setListed] = useState<{
    agent: string;
    apps: readonly App[];
  } | null>(null);
  useEffect(() => {
    if (agent === null) return;
    const dropped = new AbortController();
    request("get", "/api/apps", {
      query: { agent },
      signal: dropped.signal,
    }).then(
      (answer) => {
        // Read rather than trusted: something between here and the backend
        // may answer instead of it, and a list of apps is no reason for the
        // whole page to fall over.
        const apps: unknown = answer.apps;
        setListed({
          agent,
          apps: Array.isArray(apps) ? apps.filter(isApp) : [],
        });
      },
      () => {
        // An abort, or a list that did not come: no apps to offer.
      },
    );
    return () => {
      dropped.abort();
    };
  }, [agent]);
  return listed !== null && listed.agent === agent ? listed.apps : NO_APPS;
}

const NO_APPS: readonly App[] = [];

function isApp(value: unknown): value is App {
  if (typeof value !== "object" || value === null) return false;
  const { server, tool, title } = value as Record<string, unknown>;
  return (
    typeof server === "string" &&
    typeof tool === "string" &&
    typeof title === "string"
  );
}

/** The apps as tabs: kind `"app"`, keyed by server and tool. */
export function appOffers(apps: readonly App[]): readonly TabOffer[] {
  return apps.map((app) => ({
    key: appTabKey(app.server, app.tool),
    title: app.title,
    kind: "app",
  }));
}

/** The app a tab was opened from, if the agent still has it. */
export function appOf(apps: readonly App[], offer: TabOffer): App | undefined {
  return apps.find((app) => appTabKey(app.server, app.tool) === offer.key);
}

/** The address of an app's view, which the tab frames. */
export function viewUrl(agent: string, app: App): string {
  const server = encodeURIComponent(app.server);
  const tool = encodeURIComponent(app.tool);
  return `/api/apps/${server}/${tool}/view?agent=${encodeURIComponent(agent)}`;
}

/**
 * The tools that answered in the chat, by full name (`<server>__<tool>`),
 * heard by the open app tabs: one per shell, handed to the chat's
 * `onToolResult` and to each `AppTab`.
 */
export interface ToolResults {
  readonly publish: (name: string) => void;
  readonly subscribe: (heard: (name: string) => void) => () => void;
}

export function toolResults(): ToolResults {
  const listeners = new Set<(name: string) => void>();
  return {
    publish: (name) => {
      for (const heard of listeners) heard(name);
    },
    subscribe: (heard) => {
      listeners.add(heard);
      return () => {
        listeners.delete(heard);
      };
    },
  };
}
