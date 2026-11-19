// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Robinauts' two applications of oneberth's shell: its own, and neorc as it
 * registers neorc (oneberth's `shell.md`, "An application in the shell").
 * `APPS` is the rail of Robinauts' own interface, in order.
 *
 * This module is the entry of the package `@robinauts/ui`
 * (`../package/package.json`). A host with applications of its own gives the
 * two to the shell beside its own.
 *
 * Each loads the first time its icon is chosen, so neither application's
 * code is in the entry. neorc is given the address of its API, which this
 * backend answers (`/neorc/`), and Robinauts' source of forms to start a run
 * (`./flows/forms.ts`), which loads when neorc first asks for a form.
 */
import { neorcApp } from "@neorc/ui";
import type { ShellApp } from "@oneberth/ui";

import type { FormRequest } from "./flows/forms";
import { MessageSquareIcon } from "./icons";

/** Robinauts, as an application of the shell. */
export function robinautsApp(): ShellApp {
  return {
    name: "robinauts",
    title: "Robinauts",
    icon: <MessageSquareIcon size={20} />,
    load: () => import("./RobinautsApp"),
  };
}

/** neorc, as an application of the shell, with Robinauts' forms to start a run. */
export function neorcWithForms(): ShellApp {
  return neorcApp({
    api: "/neorc/",
    forms: async (flow: FormRequest) => {
      const { formFor } = await import("./flows/forms");
      return formFor(flow);
    },
  });
}

export const APPS: readonly ShellApp[] = [robinautsApp(), neorcWithForms()];
