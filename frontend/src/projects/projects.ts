// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The projects of whoever is signed in: named groups of their
 * conversations (`docs/specs/privacy.md`).
 *
 * Every user has one called "Default", which the backend makes the first
 * time the list is asked for, and a conversation started without naming
 * another goes in it. The panel draws the history a project at a time, each
 * one collapsible (`../history/HistoryList.tsx`); which are collapsed is a
 * preference of this browser, like the rail.
 */
import { useCallback, useState } from "react";

import { request } from "../api/client";
import type { components } from "../api/schema";
import { type Offered, useOffered } from "../shell/offered";
import { remember, remembered } from "../shell/storage";

export type Project = components["schemas"]["ProjectSummary"];

/** The name of the project every user has, and conversations go in by default. */
export const DEFAULT_PROJECT = "Default";

/** How long a name may be, in code points, as the backend counts. */
export const MAX_PROJECT_NAME_CHARS = 60;

export const PROJECT_KEY = "project";
export const FOLDED_KEY = "projects.folded";

export type Projects = Offered<Project>;

const askProjects = (signal: AbortSignal) =>
  request("get", "/api/projects", { signal });

/** The projects, asked for again each time `round` changes. */
export function useProjects(round: number): Projects {
  return useOffered(askProjects, round);
}

/** Make a project of that name, or find the one the user already has. */
export async function createProject(name: string): Promise<Project> {
  return request("post", "/api/projects", { body: { name } });
}

/** The project that is "Default", or the first there is. */
export function defaultOf(items: readonly Project[]): Project | null {
  return items.find((p) => p.name === DEFAULT_PROJECT) ?? items[0] ?? null;
}

/**
 * Which project a first message would start the conversation in, and how to
 * change it. The remembered one while it is still there, else "Default";
 * `null` until the list has come, which the backend reads as "Default" too.
 */
export function useChosenProject(
  projects: Projects,
): [string | null, (id: string) => void] {
  const [kept, setKept] = useState<string | null>(() =>
    remembered(PROJECT_KEY),
  );
  let chosen: string | null = null;
  if (projects.status === "ready") {
    chosen = projects.items.some((p) => p.id === kept)
      ? kept
      : (defaultOf(projects.items)?.id ?? null);
  }
  const choose = useCallback((id: string) => {
    setKept(id);
    remember(PROJECT_KEY, id);
  }, []);
  return [chosen, choose];
}

function readFolded(): ReadonlySet<string> {
  const kept = remembered(FOLDED_KEY);
  if (kept === null) return new Set();
  try {
    const parsed: unknown = JSON.parse(kept);
    return new Set(
      Array.isArray(parsed)
        ? parsed.filter((id): id is string => typeof id === "string")
        : [],
    );
  } catch {
    // Something else wrote it; nothing is collapsed.
    return new Set();
  }
}

/** Which projects are folded away in the panel, and how to fold or open one. */
export interface Folded {
  has: (id: string) => boolean;
  toggle: (id: string) => void;
}

export function useFolded(): Folded {
  const [folded, setFolded] = useState(readFolded);
  const has = useCallback((id: string) => folded.has(id), [folded]);
  const toggle = useCallback((id: string) => {
    setFolded((was) => {
      const next = new Set(was);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      remember(FOLDED_KEY, JSON.stringify([...next]));
      return next;
    });
  }, []);
  return { has, toggle };
}
