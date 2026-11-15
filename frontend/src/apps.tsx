// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The two applications of the shell, in the order of the rail: Robinauts,
 * then neorc (oneberth's `shell.md`, "An application in the shell").
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

const robinauts: ShellApp = {
  name: "robinauts",
  title: "Robinauts",
  icon: <MessageSquareIcon size={20} />,
  load: () => import("./RobinautsApp"),
};

const neorc = neorcApp({
  api: "/neorc/",
  forms: async (flow: FormRequest) => {
    const { formFor } = await import("./flows/forms");
    return formFor(flow);
  },
});

export const APPS: readonly ShellApp[] = [robinauts, neorc];
