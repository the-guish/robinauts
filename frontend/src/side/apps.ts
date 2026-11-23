// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The agent's MCP apps, as tabs the side panel offers, and the agent's
 * calls as the sub-tabs of each (`docs/architecture/mcp-apps.md`).
 *
 * **An app is one view of one tool server**, with the tools that name it.
 * The list is the backend's (`GET /robinauts/api/apps?agent=`). It is asked
 * for again when the agent changes, and a list that does not come is no
 * apps: nothing to offer, and no failure of the panel's.
 *
 * **A sub-tab is one call of the agent**: a tool of the app, and its
 * arguments. The calls are the chat's (`ThreadToolCall`), so the sub-tabs of
 * a conversation are the same wherever it is opened. Everything about them
 * here is pure.
 */
import { useEffect, useState } from "react";

import { request } from "../api/client";
import type { components } from "../api/schema";
import type { ThreadToolCall } from "../chat";
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
    request("get", "/robinauts/api/apps", {
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
  const { server, view, title, tools } = value as Record<string, unknown>;
  return (
    typeof server === "string" &&
    typeof view === "string" &&
    typeof title === "string" &&
    Array.isArray(tools) &&
    tools.every(isAppTool)
  );
}

function isAppTool(value: unknown): boolean {
  if (typeof value !== "object" || value === null) return false;
  const { name, title } = value as Record<string, unknown>;
  return typeof name === "string" && typeof title === "string";
}

/** The apps as tabs: kind `"app"`, keyed by server and view. */
export function appOffers(apps: readonly App[]): readonly TabOffer[] {
  return apps.map((app) => ({
    key: appTabKey(app.server, app.view),
    title: app.title,
    kind: "app",
  }));
}

/** The app a tab was opened from, if the agent still has it. */
export function appOf(apps: readonly App[], offer: TabOffer): App | undefined {
  return apps.find((app) => appTabKey(app.server, app.view) === offer.key);
}

/** The address of the view a tool names, which a sub-tab frames. */
export function viewUrl(agent: string, server: string, tool: string): string {
  const path = `${encodeURIComponent(server)}/${encodeURIComponent(tool)}`;
  return `/robinauts/api/apps/${path}/view?agent=${encodeURIComponent(agent)}`;
}

/** One call a sub-tab shows: where its result is read, and how it stands. */
export interface ShownCall {
  readonly messageId: string;
  readonly callId: string;
  readonly state: ThreadToolCall["state"];
}

/** How a call is named: by its answer and its own id. */
export function callRef(call: {
  readonly messageId: string;
  readonly callId: string;
}): string {
  return `${call.messageId}/${call.callId}`;
}

/** One sub-tab: a tool of the app with its arguments, and its newest call. */
export interface SubTab {
  /** Unique in its tab: the tool and its arguments. */
  readonly key: string;
  /** What the sub-tab says: the tool's title and a number, `Diagram 1`. */
  readonly title: string;
  readonly tool: string;
  readonly arguments: Readonly<Record<string, unknown>>;
  /**
   * The newest call of that tool with those arguments, unless it ended with
   * no result after one that had one: then the newest that had one.
   */
  readonly call: ShownCall;
  /** Where that call stands among the thread's calls: the later, the higher. */
  readonly last: number;
}

/**
 * A value as text that is the same for the same value, whatever order its
 * keys were written in: what tells two calls with the same arguments.
 */
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (typeof value === "object" && value !== null) {
    const fields = Object.entries(value)
      .sort(([a], [b]) => inOrder(a, b))
      .map(([key, held]) => `${JSON.stringify(key)}:${canonical(held)}`);
    return `{${fields.join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/** Two keys in the order of their code units, which no locale changes. */
function inOrder(a: string, b: string): number {
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

/** The key of the sub-tab a call is shown in. */
export function subTabKey(
  tool: string,
  args: Readonly<Record<string, unknown>>,
): string {
  return `${tool}:${canonical(args)}`;
}

/**
 * The app's sub-tabs in a thread, in the order their first calls were made.
 *
 * A call is the app's when the app has a tool of its name. A call whose
 * arguments are not whole yet is left out until they are. A call with the
 * same tool and arguments as an earlier one is that one's sub-tab, which
 * then shows the later call. A later call that ended with no result is the
 * exception: it has nothing to show, so the sub-tab keeps the earlier call
 * and its result. Sub-tabs are numbered per title, so two tools with one
 * title never give two sub-tabs one name.
 */
export function subTabsOf(
  app: App,
  calls: readonly ThreadToolCall[],
): readonly SubTab[] {
  const found = new Map<string, SubTab>();
  const numbered = new Map<string, number>();
  for (const [last, call] of calls.entries()) {
    const tool = app.tools.find((one) => one.name === call.name);
    if (tool === undefined || call.arguments === null) continue;
    const key = subTabKey(call.name, call.arguments);
    const shown: ShownCall = {
      messageId: call.messageId,
      callId: call.callId,
      state: call.state,
    };
    const held = found.get(key);
    if (held !== undefined) {
      const nothingNew =
        shown.state === "unanswered" && held.call.state === "answered";
      if (!nothingNew) found.set(key, { ...held, call: shown, last });
      continue;
    }
    const number = (numbered.get(tool.title) ?? 0) + 1;
    numbered.set(tool.title, number);
    found.set(key, {
      key,
      title: `${tool.title} ${String(number)}`,
      tool: call.name,
      arguments: call.arguments,
      call: shown,
      last,
    });
  }
  return [...found.values()];
}

/**
 * The tab and the sub-tab the agent last asked for, among calls being made
 * now: the newest live call of a tool with a view, by the app of the first
 * server that has a tool of that name. `null` when no live call has a view.
 */
export function lastAsked(
  apps: readonly App[],
  calls: readonly ThreadToolCall[],
): {
  readonly tab: string;
  readonly sub: string;
  readonly callId: string;
} | null {
  for (const call of [...calls].reverse()) {
    if (!call.live || call.arguments === null) continue;
    const app = apps.find((one) =>
      one.tools.some((tool) => tool.name === call.name),
    );
    if (app === undefined) continue;
    return {
      tab: appTabKey(app.server, app.view),
      sub: subTabKey(call.name, call.arguments),
      callId: callRef(call),
    };
  }
  return null;
}

/** The tab and the sub-tab the agent asked for, and a count of its askings. */
export interface AskedView {
  readonly tab: string;
  readonly sub: string;
  readonly at: number;
}

/**
 * The view the agent is asking for now (`lastAsked`), counted so that the
 * panel takes each asking once: it opens that tab and that sub-tab, and the
 * person may then close either.
 */
export function useAsked(
  apps: readonly App[],
  calls: readonly ThreadToolCall[],
): AskedView | null {
  const asking = lastAsked(apps, calls);
  const [counted, setCounted] = useState({ call: "", at: 0 });
  if (asking === null) return null;
  if (asking.callId !== counted.call) {
    setCounted({ call: asking.callId, at: counted.at + 1 });
    return null;
  }
  return { tab: asking.tab, sub: asking.sub, at: counted.at };
}
