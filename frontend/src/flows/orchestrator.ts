// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The orchestrator, as the interface sees it: its page, framed, and the
 * little of its API the panel's "Flows" section reads
 * (`docs/architecture/orchestration.md`).
 *
 * The session names the page (`flows_url`, `/neorc/ui/`); the API is one
 * level above it, as the orchestrator's own page finds it. The page is
 * opened with `?embedded`, which leaves out the orchestrator's own sidebar:
 * the panel is the one navigation, and links to the same pages by the
 * shell's `#/flows…` routes, which this file translates to the frame's own
 * hashes and back.
 *
 * The API is not this server's own and not in `backend/openapi.json`, so it
 * is read with a plain `fetch`, and every answer is checked for the fields
 * used before it is believed: what does not fit is left out.
 */
import { useEffect, useState } from "react";

import { type FlowsView, isFlowName, isUuid } from "../router";

/** The frame's address: the page, embedded, at that view. */
export function frameSrc(url: string, view: FlowsView): string {
  return `${url}?embedded${framedHash(view)}`;
}

/** The hash of the orchestrator's own page for a view. */
export function framedHash(view: FlowsView): string {
  switch (view.at) {
    case "new":
      return "#/flows";
    case "start":
      // Not framed: the interface's own form. Its flow's page, should a
      // frame ever be asked for it.
      return isFlowName(view.flow) ? `#/flows/${view.flow}` : "#/flows";
    case "run":
      return isUuid(view.run) ? `#/runs/${view.run}` : "#/runs";
    case "flow":
      return isFlowName(view.flow) ? `#/flows/${view.flow}` : "#/flows";
    default:
      return "#/runs";
  }
}

/**
 * The view the frame's own hash is at; `null` for a page the shell has no
 * route for, which the shell then leaves alone. A query in the hash -- a
 * run's `?view=graph`, a flow's `?version=` -- is the frame's to keep.
 */
export function viewOfFramed(hash: string): FlowsView | null {
  const path = (hash.startsWith("#") ? hash.slice(1) : hash).split("?")[0];
  if (path === "" || path === "/" || path === "/runs") return { at: "runs" };
  if (path === "/flows") return { at: "new" };
  const parts = (path ?? "").split("/");
  const name = parts[2];
  if (parts.length === 3 && parts[0] === "" && name !== undefined) {
    if (parts[1] === "runs" && isUuid(name)) return { at: "run", run: name };
    if (parts[1] === "flows" && isFlowName(name)) {
      return { at: "flow", flow: name };
    }
  }
  return null;
}

export function sameView(a: FlowsView, b: FlowsView): boolean {
  return framedHash(a) === framedHash(b);
}

/** Where the API is: one level above the page. */
export function apiOf(url: string): string {
  return url.replace(/[^/]*\/?$/, "").replace(/([^/])$/, "$1/");
}

/** A run, as the panel lists it. */
export interface RecentRun {
  readonly id: string;
  readonly flow: string;
  readonly status: string;
}

/** What the panel's section shows: the flows, and the latest runs. */
export interface Menu {
  readonly flows: readonly string[];
  readonly runs: readonly RecentRun[];
}

/** How many of the latest runs the panel lists, as the orchestrator does. */
export const RECENT_RUNS = 6;

/** How long one wait for the orchestrator's news lasts, in seconds. */
export const WAIT_SECONDS = 25;

/** The pause after a wait that came back with nothing, or failed. */
export const PAUSE_MS = 1000;

const STATUSES = new Set(["active", "succeeded", "failed", "cancelled"]);

function objects(value: unknown, key: string): Record<string, unknown>[] {
  if (typeof value !== "object" || value === null) return [];
  const items = (value as Record<string, unknown>)[key];
  if (!Array.isArray(items)) return [];
  return items.filter(
    (item): item is Record<string, unknown> =>
      typeof item === "object" && item !== null,
  );
}

/** The flows' names, of the orchestrator's `GET flows`. */
export function flowsOf(answer: unknown): string[] {
  return objects(answer, "flows")
    .map((flow) => flow.name)
    .filter(
      (name): name is string => typeof name === "string" && isFlowName(name),
    );
}

/** The runs, of the orchestrator's `GET runs`. */
export function runsOf(answer: unknown): RecentRun[] {
  return objects(answer, "runs").flatMap((run) => {
    const { id, flow, status } = run;
    return typeof id === "string" &&
      isUuid(id) &&
      typeof flow === "string" &&
      isFlowName(flow) &&
      typeof status === "string" &&
      STATUSES.has(status)
      ? [{ id, flow, status }]
      : [];
  });
}

/** The last sequence among the events of `GET events`; `after` if none. */
export function lastSequence(answer: unknown, after: number): number {
  return objects(answer, "events").reduce(
    (last, event) =>
      typeof event.sequence === "number" && event.sequence > last
        ? event.sequence
        : last,
    after,
  );
}

async function read(url: string, signal: AbortSignal): Promise<unknown> {
  const answer = await fetch(url, {
    signal,
    headers: { accept: "application/json" },
  });
  if (!answer.ok) throw new Error(`${answer.status} from ${url}`);
  return answer.json();
}

const pause = (ms: number, signal: AbortSignal) =>
  new Promise<void>((done) => {
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      done();
    });
  });

/**
 * The flows and the latest runs, kept up to date: read once, and again
 * whenever the orchestrator records an event, which its `events` route
 * long-polls for. `null` for nothing to show, which `url` being `null` --
 * no orchestrator -- always is.
 */
export function useMenu(url: string | null): Menu | null {
  const [menu, setMenu] = useState<Menu | null>(null);
  useEffect(() => {
    if (url === null) return;
    const api = apiOf(url);
    const stop = new AbortController();
    const { signal } = stop;
    const refresh = async () => {
      const [flows, runs] = await Promise.all([
        read(`${api}flows`, signal),
        read(`${api}runs?root_only=true&limit=${RECENT_RUNS}`, signal),
      ]);
      if (!signal.aborted) {
        setMenu({ flows: flowsOf(flows), runs: runsOf(runs) });
      }
    };
    const follow = async () => {
      let after: number | null = null;
      while (!signal.aborted) {
        try {
          if (after === null) {
            const latest = await read(`${api}events/latest`, signal);
            const sequence = (latest as { sequence?: unknown }).sequence;
            after = typeof sequence === "number" ? sequence : 0;
            await refresh();
          }
          const news = await read(
            `${api}events?after=${after}&timeout=${WAIT_SECONDS}`,
            signal,
          );
          const last = lastSequence(news, after);
          if (last > after) {
            after = last;
            await refresh();
          } else {
            await pause(PAUSE_MS, signal);
          }
        } catch {
          // The orchestrator restarting, or the page going: start again from
          // the latest, after a pause, until the panel is gone.
          after = null;
          await pause(PAUSE_MS * 5, signal);
        }
      }
    };
    void follow();
    return () => {
      stop.abort();
    };
  }, [url]);
  return url === null ? null : menu;
}
