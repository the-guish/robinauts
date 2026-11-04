// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The interface is dependency-free: a guard that it stays so.
 *
 * The shell, the history, the sign-in page and the two pages around them,
 * the chat window and the icons draw their icons from `../icons` and their
 * styles from plain stylesheets over `../tokens.css`. Those packages are no
 * longer installed, so the build would refuse an import of `lucide-react`
 * today; but it would accept one again the day the package came back into
 * the tree for any other reason, and a stylesheet's `@import "tailwindcss"`
 * or Tailwind at-rule is not something it reads at all. These tests refuse
 * them whatever is installed.
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
 * The chat window's and the icons' sources: code and stylesheets, and not
 * the prose beside them, where a package is named in backticks as a name.
 */
const chatSources = ["chat", "icons"]
  .flatMap((name) => filesUnder(join(src, name)))
  .filter((path) => /\.(?:tsx?|css)$/.test(path));

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

describe("the chat window and the icons", () => {
  test("have sources to look at", () => {
    expect(chatSources.length).toBeGreaterThan(10);
    // The icon data names the package it was copied from, in a comment.
    expect(chatSources.map((path) => relative(src, path))).toContain(
      join("icons", "lucide.ts"),
    );
  });

  for (const path of chatSources) {
    const name = relative(src, path);
    test(`${name} does not import lucide-react`, () => {
      expect(lucideIn(readFileSync(path, "utf8"))).toEqual([]);
    });
  }

  const stylesheets = chatSources.filter((path) => path.endsWith(".css"));

  test("have stylesheets of their own", () => {
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
