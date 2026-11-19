// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The extra applications of the rail, shown below Robinauts and neorc, in
 * this order (`docs/specs/frontend.md`, "Extra applications").
 *
 * Robinauts ships none. A build that adds applications puts its own folder in
 * the place of this one: its `index.ts` exports the list, and each
 * application's files stand beside it. Robinauts' image takes that folder as
 * the build context `extra` (`Dockerfile`).
 */
import type { ShellApp } from "@oneberth/ui";

export const EXTRA_APPS: readonly ShellApp[] = [];
