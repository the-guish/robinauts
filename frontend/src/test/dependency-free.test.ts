// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The shell is dependency-free: a guard that it stays so.
 *
 * The shell, the history, the sign-in page and the two pages around them
 * draw their icons from `../icons` and their styles from plain stylesheets
 * over `../tokens.css`. Nothing in the type checker or the build would notice
 * one of them going back to `lucide-react`, or the global stylesheet going
 * back to `@import "tailwindcss"`, while those packages are still installed;
 * these tests do.
 *
 * Each rule is tested on what it must refuse as well as on the tree, because
 * a rule that refuses nothing passes over every tree.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, test } from "vitest";

const src = resolve(import.meta.dirname, "..");

/** The shell's own sources: these directories, and these files. */
const SHELL_DIRECTORIES = ["shell", "history", "session"];
const SHELL_FILES = ["App.tsx", "ErrorBoundary.tsx", "main.tsx"];

function filesUnder(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const shellSources = [
  ...SHELL_DIRECTORIES.flatMap((name) => filesUnder(join(src, name))),
  ...SHELL_FILES.map((name) => join(src, name)),
];

/**
 * Where a module names `lucide-react`, or a path inside it: an `import ...
 * from`, a bare `import "..."`, an `export ... from` or an `import()`.
 */
function lucideIn(text: string): string[] {
  return [...text.matchAll(/["'`](lucide-react(?:\/[^"'`]*)?)["'`]/g)].map(
    ([, name = ""]) => name,
  );
}

/**
 * What a stylesheet `@import`s by a bare specifier: anything not relative and
 * not absolute is a package, resolved from `node_modules`. `url(...)` and a
 * quoted string are both forms of `@import`.
 */
function packagesImportedBy(css: string): string[] {
  const imports = css.matchAll(
    /@import\s+(?:url\(\s*)?["']?([^"')\s;]+)["']?\s*\)?/g,
  );
  return [...imports]
    .map(([, specifier = ""]) => specifier)
    .filter((specifier) => !/^(?:\.{1,2}\/|\/)/.test(specifier));
}

/**
 * The at-rules only Tailwind reads. In a plain stylesheet they are either
 * dropped by the browser or a sign that Tailwind is still expected to run.
 */
function tailwindRulesIn(css: string): string[] {
  return [
    ...css.matchAll(
      /@(theme|source|custom-variant|apply|plugin|utility|variant|config|tailwind)\b/g,
    ),
  ].map(([rule]) => rule);
}

describe("the rules refuse what they are for", () => {
  test("lucide-react, however it is imported", () => {
    expect(lucideIn('import { X } from "lucide-react";')).toEqual([
      "lucide-react",
    ]);
    expect(lucideIn("import 'lucide-react/dist/esm/icons/x';")).toEqual([
      "lucide-react/dist/esm/icons/x",
    ]);
    expect(lucideIn('export { X } from "lucide-react";')).toHaveLength(1);
    expect(lucideIn('await import("lucide-react")')).toHaveLength(1);
    expect(lucideIn('import { XIcon } from "../icons";')).toEqual([]);
  });

  test("a package in a stylesheet's @import, and only a package", () => {
    expect(packagesImportedBy('@import "tailwindcss" source(none);')).toEqual([
      "tailwindcss",
    ]);
    expect(packagesImportedBy("@import 'tw-animate-css';")).toEqual([
      "tw-animate-css",
    ]);
    expect(packagesImportedBy('@import url("pkg/x.css");')).toEqual([
      "pkg/x.css",
    ]);
    expect(packagesImportedBy("@import url(pkg/x.css);")).toEqual([
      "pkg/x.css",
    ]);
    expect(packagesImportedBy('@import "@scope/pkg";')).toEqual(["@scope/pkg"]);
    expect(
      packagesImportedBy(
        '@import "./tokens.css";\n@import "../a.css";\n@import "/b.css";',
      ),
    ).toEqual([]);
  });

  test("Tailwind's own at-rules", () => {
    expect(
      tailwindRulesIn("@theme inline {}\n@source './x';\n.a { @apply p-1; }"),
    ).toEqual(["@theme", "@source", "@apply"]);
    expect(tailwindRulesIn("@media (width >= 48rem) {}")).toEqual([]);
  });
});

describe("the shell", () => {
  test("has sources to look at", () => {
    // A guard over an empty list would pass for ever.
    expect(shellSources.length).toBeGreaterThan(10);
  });

  for (const path of shellSources) {
    const name = relative(src, path);
    test(`${name} does not import lucide-react`, () => {
      expect(lucideIn(readFileSync(path, "utf8"))).toEqual([]);
    });
  }

  const stylesheets = [
    join(src, "styles.css"),
    ...shellSources.filter((path) => path.endsWith(".css")),
  ];

  test("has stylesheets of its own", () => {
    expect(stylesheets.length).toBeGreaterThan(1);
  });

  for (const path of stylesheets) {
    const name = relative(src, path);
    test(`${name} names no package and no Tailwind at-rule`, () => {
      const css = readFileSync(path, "utf8");
      expect(packagesImportedBy(css)).toEqual([]);
      expect(tailwindRulesIn(css)).toEqual([]);
    });
  }
});
