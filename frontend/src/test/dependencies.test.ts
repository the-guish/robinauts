// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The runtime dependencies are `react` and `react-dom`, and nothing else
 * (ADR 0006).
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

/** What ships: the two packages, and react-dom's own `scheduler`. */
const RUNTIME = ["react", "react-dom"];
const BUNDLED = [...RUNTIME, "scheduler"];

test("package.json declares react and react-dom, and nothing else", () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8")) as {
    dependencies?: Record<string, string>;
  };
  expect(Object.keys(manifest.dependencies ?? {}).sort()).toEqual(RUNTIME);
});

test("the bundle's record names those two and react-dom's scheduler", () => {
  const named = readFileSync("bundled-packages.txt", "utf8")
    .split("\n")
    .filter((line) => line !== "" && !line.startsWith("#"))
    .map((line) => line.split(" ")[0]);
  expect(named.sort()).toEqual(BUNDLED);
});
