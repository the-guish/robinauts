// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The runtime dependencies are `react` and `react-dom` (ADR 0006), and the
 * two node packages of the platform: `@oneberth/ui`, the shell, and
 * `@neorc/ui`, neorc's interface, which brings a query library and two graph
 * libraries with their trees (oneberth's `shell.md`). The two packages are
 * packed from the pinned neorc commit (`scripts/ui-packages-from-source.sh`).
 *
 * The build already compares what reached the bundle with
 * `bundled-packages.txt`, so a package that arrives through a dependency's
 * dependency is a red build. This reads the declaration itself, so that a
 * package added to `package.json` is refused before anything imports it, and
 * the record, so that the two are checked against the same short list. A
 * change to either list is a decision, and this is where it is written down.
 */
import { readFileSync } from "node:fs";

import { expect, test } from "vitest";

/** What is declared: React, and the platform's two packages. */
const RUNTIME = ["@neorc/ui", "@oneberth/ui", "react", "react-dom"];
/**
 * What ships: those, react-dom's own `scheduler`, and what `@neorc/ui`
 * brings, as neorc's own `bundled-packages.txt` lists it.
 */
const BUNDLED = [
  ...RUNTIME,
  "scheduler",
  "@dagrejs/dagre",
  "@tanstack/query-core",
  "@tanstack/react-query",
  "@xyflow/react",
  "@xyflow/system",
  "classcat",
  "d3-color",
  "d3-dispatch",
  "d3-drag",
  "d3-ease",
  "d3-interpolate",
  "d3-selection",
  "d3-timer",
  "d3-transition",
  "d3-zoom",
  "use-sync-external-store",
  "zustand",
].sort();

test("package.json declares React and the platform's two packages", () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8")) as {
    dependencies?: Record<string, string>;
  };
  expect(Object.keys(manifest.dependencies ?? {}).sort()).toEqual(RUNTIME);
});

test("the bundle's record names those, and what they bring", () => {
  const named = readFileSync("bundled-packages.txt", "utf8")
    .split("\n")
    .filter((line) => line !== "" && !line.startsWith("#"))
    .map((line) => line.split(" ")[0]);
  expect(named.sort()).toEqual(BUNDLED);
});
