// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
//
// The gates on what may end up in the bundle. The rules they enforce are in
// ../docs/contributing/js-dependencies.md; the policy behind them is
// ../DEPENDENCIES.md.
import {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { gzipSync } from "node:zlib";

import react from "@vitejs/plugin-react";
import license from "rollup-plugin-license";
import type { Dependency } from "rollup-plugin-license";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

// The licence policy, read from DEPENDENCIES.md by the gate that also applies
// it to the installed tree. Importing it is what keeps the bundle's allowed
// list and the tree's from being two lists (scripts/check-licences.mjs).
import { isAllowed } from "./scripts/check-licences.mjs";

/**
 * The stylesheet languages, and the ones that are CSS Modules.
 *
 * Vite 8.3.0's own `CSS_LANGS_RE` and `cssModuleRE`, copied, so that what is
 * refused below is exactly what Vite would have compiled.
 */
const CSS_LANGS = /\.(css|less|sass|scss|styl|stylus|pcss|postcss|sss)(?:$|\?)/;
const CSS_MODULE =
  /\.module\.(css|less|sass|scss|styl|stylus|pcss|postcss|sss)(?:$|\?)/;

/**
 * Everything shipped, gzipped, must fit in this.
 *
 * **Set, not provisional.** It was 800 KB while the chat did not exist and
 * there was nothing to measure (docs/specs/frontend.md left the real number
 * open; neorc's 500 KB was never going to fit a chat UI with Markdown in
 * it). The bundle with the chat in it measures **278.6 KB**, by the rule
 * `gzippedSize` below applies and no other -- every file of `dist/` gzipped,
 * the licence text excepted -- on 2026-09-23. So this is that, with room for
 * the features the specs already name (tool calls, attachments, a second
 * engine's UI) and none for a dependency that is an order of magnitude too
 * big.
 *
 * Raising it is a reviewed change, and the reason goes in the pull request
 * (docs/contributing/js-dependencies.md: the budget is not loosened to make
 * a library fit).
 */
const SIZE_BUDGET_BYTES = 400 * 1024;

const here = import.meta.dirname;

/**
 * What ends up in the bundle, one line per package, committed and compared
 * with the build: a new line here is what a reviewer looks at. The build
 * writes it; it is never edited by hand.
 *
 * **What it sees, and what it does not.** rollup-plugin-license reads
 * rollup's module graph, so it lists every package a *module* was imported
 * from. A package reached only through a stylesheet -- `@import "pkg"`,
 * `url(pkg/x.png)`, or a package stylesheet's own imports -- is in no module
 * graph, and the other half of this list is `CSS_PACKAGES` below, which is
 * written by hand.
 */
const BUNDLED_PACKAGES = resolve(here, "bundled-packages.txt");

/**
 * Packages whose CSS ships although no module was ever imported from them.
 * **This list is maintained by hand**, and it is the by-hand rule of
 * ../DEPENDENCIES.md ("What the bundle's record does not see") made into a
 * file the build reads. A stylesheet that gains an `@import "pkg"`, a
 * `url(pkg/…)` or a reference to a package stylesheet that imports another
 * package is a change that adds a name here, and that name is what a
 * reviewer looks at -- exactly as a new line in `bundled-packages.txt` is.
 * Nothing discovers these: a scanner that agrees with Vite's own resolver
 * about what a specifier means was written for step 17 and dropped after ten
 * rounds of review. What the build can still do, once a name is written
 * down, is everything that follows from it: read the package's own metadata,
 * hold its licence to the allowed list like any bundled package, put it in
 * the record, and ship its notice with the code it belongs to.
 *
 * **Empty since ADR 0006.** Every stylesheet is this project's own, written
 * over `src/tokens.css`, and none names a package: Tailwind and the two
 * plugins the vendored chat components were written against went with them.
 * The mechanism stays, because the day a stylesheet does name a package,
 * this is where the name goes, and `src/test/build-rules.test.ts` proves it
 * still works on a tree that has one.
 */
const CSS_PACKAGES: readonly string[] = [];

/** Where the packages above are read from, which is what npm installed. */
const NODE_MODULES = resolve(here, "node_modules");

/**
 * What a package calls the file its licence is in.
 *
 * `LICENSE`, `LICENCE`, `LICENSE.md`, `LICENSE-MIT`, `COPYING`, `NOTICE`:
 * there is no rule, only convention, and a package whose file this misses
 * fails the build rather than shipping unattributed.
 */
const LICENCE_FILE = /^(licen[cs]e|copying|notice)/i;

/** The bare name, with no extension and nothing after it. */
const BARE = /^(licen[cs]e|copying|notice)$/i;

/**
 * The one to quote, out of everything in a package that could be it.
 *
 * Two things decide it. **It has to be a file**: `LICENSES/` is a
 * *directory* some packages keep their per-file texts in (this repository
 * has one), and reading it would throw rather than fail with something a
 * person can act on. And **the plainest name wins**: `LICENSE` over
 * `LICENSE.md` over `LICENSE-MIT`, and a licence over a `NOTICE`, which is
 * an addition to one rather than the thing itself. Ties go alphabetically,
 * so the answer does not depend on the order a directory happens to list.
 */
/** Character-code order, the order `bundled-packages.txt` is compared in; not the locale's. */
function byCodeUnit(a: string, b: string): number {
  if (a < b) return -1;
  return a > b ? 1 : 0;
}

function rank(file: string): number {
  const licence = /^licen[cs]e/i.test(file);
  if (BARE.test(file)) return licence ? 0 : 1;
  return licence ? 2 : 3;
}

function licenceFile(where: string): string | undefined {
  return readdirSync(where)
    .filter((file) => {
      if (!LICENCE_FILE.test(file)) return false;
      try {
        return statSync(join(where, file)).isFile();
      } catch {
        // A broken symbolic link, or something unreadable: not a notice.
        return false;
      }
    })
    .sort((a, b) => rank(a) - rank(b) || byCodeUnit(a, b))[0];
}

/** What a package that reaches the bundle through CSS contributes. */
export interface CssPackage {
  name: string;
  version: string;
  license: string;
  /** The text of its LICENSE file, as the notice carries it. */
  text: string;
}

/**
 * One of them, read from what npm installed.
 *
 * Everything here fails the build rather than being skipped. A name in
 * `CSS_PACKAGES` is a statement that this package's code ships; a licence
 * that cannot be read, or that is not on the allowed list, is then the same
 * problem as one in the bundle's own gate, and **the bundle is the allowed
 * list or nothing** (../DEPENDENCIES.md): no exception row reaches here.
 */
export function cssPackage(name: string, modules: string): CssPackage {
  const where = join(modules, name);
  let manifest: { name?: unknown; version?: unknown; license?: unknown };
  try {
    manifest = JSON.parse(
      readFileSync(join(where, "package.json"), "utf8"),
    ) as {
      name?: unknown;
    };
  } catch {
    throw new Error(
      `${name} is named in CSS_PACKAGES and ${where}/package.json cannot be ` +
        "read. A package whose CSS ships has to be installed to be judged.",
    );
  }
  const version = manifest.version;
  const license = manifest.license;
  if (typeof version !== "string" || typeof license !== "string") {
    throw new TypeError(
      `${name} states no version or no licence in its package.json. ` +
        "../DEPENDENCIES.md: an exception can never cover metadata nobody " +
        "can read, and this is code that ships.",
    );
  }
  if (!isAllowed(license)) {
    throw new Error(
      `${name} ${version} is ${license}, which is not on the allowed list of ` +
        "../DEPENDENCIES.md. Its CSS is in the bundle, and what ships is the " +
        "allowed list or nothing.",
    );
  }
  const named = licenceFile(where);
  if (named === undefined) {
    throw new Error(
      `${name} ${version} ships no readable LICENSE, LICENCE, COPYING or ` +
        `NOTICE file in ${where} (any extension; a directory of that name ` +
        "is not one), so there is no notice to carry. The wheel distributes " +
        "its CSS and has to distribute the licence with it.",
    );
  }
  return {
    name,
    version,
    license,
    text: readFileSync(join(where, named), "utf8").trimEnd(),
  };
}

export function cssPackagesIn(
  modules: string,
  names: readonly string[] = CSS_PACKAGES,
): CssPackage[] {
  return names.map((name) => cssPackage(name, modules));
}

/**
 * The rule rollup-plugin-license puts between two entries, copied.
 *
 * What is appended has to be indistinguishable from what it wrote, so that
 * the file has one shape throughout and nothing reading it has to know
 * which half an entry came from.
 */
const BETWEEN_NOTICES = "\n\n---\n\n";

/**
 * One entry per package, in the shape rollup-plugin-license writes them:
 * a block of fields, `License Text:`, and the text.
 *
 * No rule before the first or after the last -- `appendCssNotices` puts one
 * between what is already there and these, and the file ends with a licence
 * as the plugin's own does.
 */
export function cssNotices(packages: readonly CssPackage[]): string {
  return packages
    .map(
      (one) =>
        `Name: ${one.name}\nVersion: ${one.version}\nLicense: ${one.license}\n` +
        "Reached through a stylesheet, not through the module graph: " +
        `see DEPENDENCIES.md.\nLicense Text:\n===\n\n${one.text}`,
    )
    .join(BETWEEN_NOTICES);
}

/**
 * Whether the build compares the committed list instead of rewriting it.
 * CI sets CI; scripts/check-frontend.sh sets CHECK_BUNDLED. A plain
 * `npm run build` rewrites the file, which is how a new bundled package
 * gets into a diff.
 */
const CHECK_BUNDLED =
  process.env.CHECK_BUNDLED === "1" || process.env.CI === "true";

/**
 * What the browser would download, gzipped.
 *
 * `THIRD_PARTY_LICENSES.txt` is not part of it: it is written into `dist/`
 * for the wheel to carry (docs/oss-checklist.md), no page ever fetches it,
 * and counting it would mean the budget tightened every time a dependency
 * was added -- twice over.
 */
function gzippedSize(dir: string): number {
  let total = 0;
  for (const name of readdirSync(dir)) {
    if (name === "THIRD_PARTY_LICENSES.txt") continue;
    const path = join(dir, name);
    total += statSync(path).isDirectory()
      ? gzippedSize(path)
      : gzipSync(readFileSync(path)).length;
  }
  return total;
}

/**
 * What a stylesheet may be, which is very little.
 *
 * Three refusals, and no resolver: each one is a rule about a file's name,
 * which is the only thing about a stylesheet this build can judge without
 * following a specifier the way Vite would. That is deliberate --
 * ../DEPENDENCIES.md says what a stylesheet can still smuggle past the
 * bundle's record, and what a reviewer therefore has to read by hand.
 *
 * **No CSS Modules.** `composes: x from "pkg/y.css"` and `@value x from
 * "..."` are module references that postcss-modules resolves on its own, so
 * rollup never sees a module for them at all. Vite has no switch to turn
 * them off, so the build refuses the file.
 *
 * **Plain CSS only.** `.scss`, `.less`, `.pcss` and the rest are languages
 * this project does not write, and a preprocessor is one more thing between
 * what is written and what ships.
 *
 * **No `<style>` in `index.html`.** It would need `unsafe-inline` in the
 * Content-Security-Policy to run at all (docs/specs/frontend.md).
 */
export function refuseStylesheets(): Plugin {
  return {
    name: "robinauts-stylesheet-rules",
    apply: "build",
    transform(_code, id) {
      // `?inline`, `?raw`, `?used` and the rest are Vite's; the file is what
      // is in front of the first question mark.
      const path = (id.split("?")[0] ?? "").replace(/^\0/, "");
      if (CSS_MODULE.test(id)) {
        throw new Error(
          `${path} is a CSS Module. Their cross-file references ` +
            "(`composes ... from`, `@value ... from`) are resolved by " +
            "postcss-modules, so rollup never sees a module for them and the " +
            "bundle's record cannot name what they reach. This build refuses " +
            "CSS Modules wherever they come from, a dependency's included: " +
            "pick a dependency that ships plain CSS.",
        );
      }
      // Vite hands a `<style>` block over as `index.html?html-proxy&inline-css
      // &index=0.css`, whose *path* is the page. It is refused for being
      // inline, not for being HTML, so it is named before the rule below.
      if (/[?&]html-proxy\b/.test(id) && /[?&]inline-css\b/.test(id)) {
        throw new Error(inlineStyleRefusal(path));
      }
      if (!CSS_LANGS.test(id)) return null;
      if (!path.endsWith(".css")) {
        throw new Error(
          `${path} is a stylesheet in a language this project does not use. ` +
            "It writes plain CSS. Write .css.",
        );
      }
      return null;
    },
  };
}

/**
 * `index.html` carries no stylesheet, and may not.
 *
 * A `<style>` block would need `unsafe-inline` in the Content-Security-Policy
 * to run at all (docs/specs/frontend.md), and reading one properly would mean
 * an HTML parser this project has no other use for. The project's CSS lives
 * in `src/`, so the answer is that there is none to read.
 */
function inlineStyleRefusal(page: string): string {
  return (
    `${page} has a <style> block. The Content-Security-Policy this is served ` +
    "under allows no inline stylesheet, and the licence gate reads files, not " +
    "markup. Put the CSS in src/ and import it."
  );
}

export function refuseInlineStyles(page: string): void {
  if (!existsSync(page)) return;
  // HTML comments first: a `<style` inside one is not a stylesheet.
  const html = readFileSync(page, "utf8").replace(/<!--[\s\S]*?-->/g, "");
  if (/<style\b/i.test(html)) throw new Error(inlineStyleRefusal(page));
}

export function sizeBudget(
  built = resolve(here, "dist"),
  budget = SIZE_BUDGET_BYTES,
): Plugin {
  return {
    name: "robinauts-size-budget",
    apply: "build",
    closeBundle() {
      // A build that failed wrote nothing, and a budget complaining about a
      // directory that is not there would bury the error that matters.
      if (!existsSync(built)) return;
      const size = gzippedSize(built);
      if (size > budget) {
        throw new Error(
          `the built assets are ${size} bytes gzipped, over the budget of ` +
            `${budget}: see docs/contributing/js-dependencies.md`,
        );
      }
    },
  };
}

/**
 * The list as it should be, and either written or compared.
 *
 * Split from the gathering above so that a test can drive both sides of it
 * without a build: what the file says is the whole of the record, and the
 * compare is the whole of the check.
 */
export function bundledPackagesFrom(
  lines: Iterable<string>,
  { check = CHECK_BUNDLED, file = BUNDLED_PACKAGES } = {},
): string {
  // No trailing newline: rollup-plugin-license trims what a template returns
  // before writing it, and what is compared below has to be, byte for byte,
  // what the build would have put on disk.
  const written = [
    "# Packages in the built bundle, written by the build.",
    "# A change here is reviewed: see docs/contributing/js-dependencies.md.",
    ...[...new Set(lines)].sort(byCodeUnit),
  ].join("\n");
  if (!check) return written;
  let committed: string;
  try {
    committed = readFileSync(file, "utf8");
  } catch {
    committed = "";
  }
  if (committed !== written) {
    throw new Error(
      "bundled-packages.txt differs from what the build wrote. Run " +
        "`npm run build` without CHECK_BUNDLED, commit the file, and have " +
        "the new package reviewed: docs/contributing/js-dependencies.md.",
    );
  }
  // Returning what is already there makes the write that follows a no-op.
  return written;
}

function bundledPackages(dependencies: Dependency[]): string {
  refuseInlineStyles(resolve(here, "index.html"));
  // The module graph's packages and the ones a stylesheet reaches, in one
  // record: what ships is one list, whichever way a package got there.
  return bundledPackagesFrom([
    ...dependencies.map((d) => `${d.name} ${d.version} ${d.license}`),
    ...cssPackagesIn(NODE_MODULES).map(
      (one) => `${one.name} ${one.version} ${one.license}`,
    ),
  ]);
}

/**
 * The notices of the CSS-reached packages, after the file has been written.
 *
 * rollup-plugin-license writes `THIRD_PARTY_LICENSES.txt` from the module
 * graph and knows nothing of these, so they are appended once it has. A
 * missing file is a failure rather than a no-op: it would mean the licence
 * output did not run at all, and a build that quietly shipped no notices is
 * the thing this is here to prevent.
 */
export function appendCssNotices(
  file: string,
  packages: readonly CssPackage[],
): void {
  if (packages.length === 0) return;
  if (!existsSync(file)) {
    throw new Error(
      `${file} was not written, so the notices of the packages reached ` +
        "through a stylesheet have nowhere to go. The licence output of " +
        "rollup-plugin-license has to run before this.",
    );
  }
  // Read and rewritten rather than appended to: the rule between two
  // entries is exact, and the only way to put one there is to know where
  // what is already in the file ends.
  const written = readFileSync(file, "utf8").trimEnd();
  writeFileSync(file, `${written}${BETWEEN_NOTICES}${cssNotices(packages)}\n`);
}

function carryCssNotices(): Plugin {
  return {
    name: "robinauts-css-package-notices",
    apply: "build",
    closeBundle() {
      appendCssNotices(
        resolve(here, "dist/THIRD_PARTY_LICENSES.txt"),
        cssPackagesIn(NODE_MODULES),
      );
    },
  };
}

// The dev server against a local `robinauts start`. One target, one set of
// settings: a run's events arrive as server-sent events, and an intermediary
// that buffers or times out turns a live answer into a batch of text at the
// end of it.
const BACKEND = "http://127.0.0.1:8000";
const proxy = {
  target: BACKEND,
  changeOrigin: false,
  // Nothing may sit on the stream: no compression to fill a buffer with, and
  // no deadline for a run that takes minutes.
  headers: { "accept-encoding": "identity" },
  timeout: 0,
  proxyTimeout: 0,
};

export default defineConfig({
  // Relative asset paths, so the built files work under any prefix, /ui/
  // included, without being rebuilt for it. It is also why the backend answers
  // `/ui` with a redirect to `/ui/` rather than with the page: a document at
  // `/ui` resolves `./assets/…` against `/`.
  //
  // **Nothing here names the files under `assets/`**, so Vite's own default
  // applies and every one of them carries an eight-character digest of its
  // contents (`[name]-[hash][extname]`). The backend caches on exactly that --
  // a year of `immutable` for a name that holds a digest, revalidation for one
  // that does not (`robinauts.api.ui.HASHED`) -- and `scripts/check-wheel.sh`
  // fails the build if an asset in the wheel is named any other way. So a
  // `rollupOptions.output.assetFileNames` written here is a change to that
  // rule as well.
  base: "./",
  // The stylesheet rules judge every file that reaches the build.
  plugins: [react(), refuseStylesheets(), carryCssNotices(), sizeBudget()],
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
  server: {
    proxy: {
      "/api": proxy,
      "/auth": proxy,
      "/health": proxy,
      "/openapi.json": proxy,
    },
  },
  // A worker is a second rollup build with a bundle of its own, so the licence
  // gate has to be registered for it separately or it ships ungated. It emits
  // no record: see the note on BUNDLED_PACKAGES.
  worker: {
    // The stylesheet rules too: a worker is a separate rollup build, and
    // would otherwise be the one place a CSS Module could still be written.
    plugins: () => [
      refuseStylesheets(),
      license({
        thirdParty: {
          allow: {
            test: (dependency) => isAllowed(dependency.license ?? ""),
            failOnUnlicensed: true,
            failOnViolation: true,
          },
          // Gated, not recorded: an output here would fight the one below
          // over the same files.
          output: [],
        },
      }),
    ],
  },
  build: {
    rollupOptions: {
      plugins: [
        license({
          thirdParty: {
            allow: {
              // The allowed list, and SPDX's AND/OR with it, from the gate.
              // A bundled package is never excepted: this is the list or
              // nothing (../DEPENDENCIES.md).
              test: (dependency) => isAllowed(dependency.license ?? ""),
              failOnUnlicensed: true,
              failOnViolation: true,
            },
            output: [
              // The wheel carries this file and lists it in license-files
              // (docs/oss-checklist.md); step 22 puts it there.
              { file: resolve(here, "dist/THIRD_PARTY_LICENSES.txt") },
              { file: BUNDLED_PACKAGES, template: bundledPackages },
            ],
          },
        }),
      ],
    },
  },
});
