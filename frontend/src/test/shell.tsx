// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Robinauts' application as oneberth's shell holds it, for the tests: its
 * route is the hash after `#/robinauts`, and `navigate` writes the hash, as
 * the shell does. Hidden, the route stays where it was, as the shell keeps
 * it.
 */
import { useMemo, useState, useSyncExternalStore, type ReactNode } from "react";

import { formatRoute, parseRoute, RouterContext, type Router } from "../router";

export const PREFIX = "#/robinauts";

/** The hash of a route of the application. */
export const hashOf = (route: string) => `${PREFIX}${route}`;

function subscribe(changed: () => void): () => void {
  window.addEventListener("hashchange", changed);
  return () => {
    window.removeEventListener("hashchange", changed);
  };
}

function routeOf(hash: string): string | null {
  if (hash === PREFIX) return "";
  if (hash.startsWith(`${PREFIX}/`) || hash.startsWith(`${PREFIX}?`)) {
    return hash.slice(PREFIX.length);
  }
  return null;
}

export function InShell({
  visible = true,
  children,
}: Readonly<{ visible?: boolean; children: ReactNode }>) {
  const hash = useSyncExternalStore(subscribe, () => location.hash);
  const [kept, setKept] = useState("");
  const read = routeOf(hash);
  const shown = read ?? "";
  if (visible && shown !== kept) setKept(shown);
  const route = visible ? shown : kept;
  const router = useMemo<Router>(
    () => ({
      route: parseRoute(route),
      navigate: (to) => {
        location.hash = hashOf(formatRoute(to));
      },
      href: (to) => hashOf(formatRoute(to)),
    }),
    [route],
  );
  return (
    <RouterContext.Provider value={router}>{children}</RouterContext.Provider>
  );
}
