// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Robinauts' root component, as oneberth's shell loads it: the default export
 * of this module, given the shell's `AppProps` (oneberth's `shell.md`, "An
 * application in the shell").
 *
 * The shell gives the route, `navigate`, `href` and whether Robinauts is
 * shown; the router context hands them to every part of the application
 * (`./router.ts`), and `visible` stops the change feed while neorc is shown.
 */
import type { AppProps } from "@oneberth/ui";
import { useMemo } from "react";

import { formatRoute, parseRoute, RouterContext, type Router } from "./router";
import { Workspace } from "./shell/Workspace";

export default function RobinautsApp({
  route,
  navigate,
  href,
  visible,
}: AppProps) {
  const router = useMemo<Router>(
    () => ({
      route: parseRoute(route),
      navigate: (to) => {
        navigate(formatRoute(to));
      },
      href: (to) => href(formatRoute(to)),
    }),
    [route, navigate, href],
  );
  return (
    <RouterContext.Provider value={router}>
      <Workspace visible={visible} />
    </RouterContext.Provider>
  );
}
