// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The rules the build itself carries, tested on their own.
 *
 * They are what is left of a longer attempt: a scanner that read stylesheets
 * and followed what they reached was written and then dropped, because it
 * never agreed with Vite's own resolver (see ../../DEPENDENCIES.md, which
 * states the limit that leaves). What remains needs no resolver -- each rule
 * judges a file's name, or a file the build has already written.
 */
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { afterEach, describe, expect, test } from "vitest";

import {
  appendCssNotices,
  bundledPackagesFrom,
  cssPackagesIn,
  refuseInlineStyles,
  refuseStylesheets,
  sizeBudget,
} from "../../vite.config";

/** This package's own directory, for the one test that reads the real tree. */
const here = resolve(import.meta.dirname, "..", "..");

const scratches: string[] = [];
const scratch = () => {
  const where = mkdtempSync(join(tmpdir(), "robinauts-build-"));
  scratches.push(where);
  return where;
};
afterEach(() => {
  for (const where of scratches.splice(0)) {
    rmSync(where, { recursive: true, force: true });
  }
});

describe("what the build refuses outright", () => {
  /** The plugin's transform hook, called as rollup would call it. */
  const transform = (id: string) => {
    const plugin = refuseStylesheets() as {
      transform: (code: string, id: string) => unknown;
    };
    return () => plugin.transform("", id);
  };

  test("a CSS Module, in any language and from anywhere", () => {
    for (const id of [
      "/x/src/a.module.css",
      "/x/src/a.module.scss",
      "/x/node_modules/some-ui/dist/a.module.css?used",
    ]) {
      expect(transform(id), id).toThrow(/a dependency's included/);
    }
  });

  test("a stylesheet in a language this project does not write", () => {
    for (const id of [
      "/x/src/a.scss",
      "/x/src/a.pcss",
      "/x/src/a.sss?inline",
    ]) {
      expect(transform(id), id).toThrow(/language this project does not use/);
    }
  });

  test("a <style> block, which Vite hands over as a page", () => {
    // Its id's path is index.html, so the language rule would name it wrongly
    // if the inline rule did not come first.
    expect(
      transform("/x/index.html?html-proxy&inline-css&index=0.css"),
    ).toThrow(/<style> block/);
  });

  test("and nothing else", () => {
    for (const id of ["/x/src/a.css", "/x/src/main.tsx", "\0virtual:thing"]) {
      expect(transform(id), id).not.toThrow();
    }
  });

  test("a <style> in index.html, read from the file", () => {
    const root = scratch();
    writeFileSync(
      join(root, "clean.html"),
      '<head><!-- <style>@import "pkg";</style> --></head>',
    );
    writeFileSync(
      join(root, "dirty.html"),
      '<head><style type="text/css">.a { color: red }</style></head>',
    );
    expect(() => refuseInlineStyles(join(root, "missing.html"))).not.toThrow();
    expect(() => refuseInlineStyles(join(root, "clean.html"))).not.toThrow();
    expect(() => refuseInlineStyles(join(root, "dirty.html"))).toThrow(
      /<style> block/,
    );
  });
});

describe("the bundle's size and its record", () => {
  test("the budget reads what a browser would download, and no more", () => {
    const dist = scratch();
    writeFileSync(join(dist, "app.js"), "x".repeat(4096));
    // Big enough to break any budget, and not part of one: the wheel carries
    // it and no page fetches it.
    writeFileSync(join(dist, "THIRD_PARTY_LICENSES.txt"), "y".repeat(200000));

    const run = (budget: number) => {
      const plugin = sizeBudget(dist, budget) as { closeBundle: () => void };
      return () => plugin.closeBundle();
    };
    // Between the two: over the budget if the licence file were counted, and
    // under it if only what a browser downloads is. It is the second.
    expect(run(2000)).not.toThrow();
    expect(run(8)).toThrow(/over the budget of 8/);
  });

  test("a build that wrote nothing is not a build over its budget", () => {
    // The real error is the one that stopped the build; a budget complaining
    // about a missing directory would bury it.
    const plugin = sizeBudget(join(scratch(), "never-written"), 1) as {
      closeBundle: () => void;
    };
    expect(() => plugin.closeBundle()).not.toThrow();
  });

  test("the record is written, or compared and refused", () => {
    const file = join(scratch(), "bundled-packages.txt");
    const lines = ["b 2.0.0 ISC", "a 1.0.0 MIT", "a 1.0.0 MIT"];

    const written = bundledPackagesFrom(lines, { check: false, file });
    // Sorted, de-duplicated, and with no trailing newline: what is compared
    // has to be what the build would have put on disk.
    expect(written.split("\n").slice(2)).toEqual([
      "a 1.0.0 MIT",
      "b 2.0.0 ISC",
    ]);
    expect(written.endsWith("\n")).toBe(false);

    expect(() => bundledPackagesFrom(lines, { check: true, file })).toThrow(
      /differs from what the build wrote/,
    );
    writeFileSync(file, written);
    expect(bundledPackagesFrom(lines, { check: true, file })).toBe(written);
    expect(() =>
      bundledPackagesFrom([...lines, "c 3.0.0 MIT"], { check: true, file }),
    ).toThrow(/differs from what the build wrote/);
  });
});

describe("the packages a stylesheet reaches", () => {
  /** A node_modules with one package in it, as npm would have left it. */
  const installed = (
    manifest: Record<string, unknown>,
    licence: string | null,
    file = "LICENSE",
  ) => {
    const modules = scratch();
    const where = join(modules, String(manifest.name));
    mkdirSync(where, { recursive: true });
    writeFileSync(join(where, "package.json"), JSON.stringify(manifest));
    if (licence !== null) writeFileSync(join(where, file), licence);
    return modules;
  };

  test("its metadata and its notice are read from what npm installed", () => {
    const modules = installed(
      { name: "some-css", version: "2.1.0", license: "MIT" },
      "MIT License\n\nCopyright (c) Somebody\n",
      "LICENCE.md",
    );
    expect(cssPackagesIn(modules, ["some-css"])).toEqual([
      {
        name: "some-css",
        version: "2.1.0",
        license: "MIT",
        // Spelt either way, with any suffix, and trimmed.
        text: "MIT License\n\nCopyright (c) Somebody",
      },
    ]);
  });

  test("it is held to the allowed list, like anything else that ships", () => {
    const modules = installed(
      { name: "some-css", version: "1.0.0", license: "GPL-3.0-only" },
      "…",
    );
    expect(() => cssPackagesIn(modules, ["some-css"])).toThrow(
      /not on the allowed list/,
    );
  });

  test("metadata that cannot be read fails, and is never skipped", () => {
    expect(() =>
      cssPackagesIn(installed({ name: "some-css", version: "1.0.0" }, "…"), [
        "some-css",
      ]),
    ).toThrow(/no version or no licence/);
    expect(() => cssPackagesIn(scratch(), ["some-css"])).toThrow(
      /cannot be read/,
    );
  });

  test("a package with no licence file has no notice to ship", () => {
    const modules = installed(
      { name: "some-css", version: "1.0.0", license: "MIT" },
      null,
    );
    // The refusal names every spelling that was looked for.
    expect(() => cssPackagesIn(modules, ["some-css"])).toThrow(
      /ships no readable LICENSE, LICENCE, COPYING or NOTICE file/,
    );
  });

  test("the licence file is found however it is spelt", () => {
    for (const file of ["LICENSE", "licence.md", "COPYING", "NOTICE.txt"]) {
      const modules = installed(
        { name: "some-css", version: "1.0.0", license: "MIT" },
        `the text of ${file}`,
        file,
      );
      expect(cssPackagesIn(modules, ["some-css"])[0]?.text, file).toBe(
        `the text of ${file}`,
      );
    }
  });

  test("a LICENSES/ directory is not the licence, and the plainest name wins", () => {
    const modules = installed(
      { name: "some-css", version: "1.0.0", license: "MIT" },
      "the licence itself",
    );
    const where = join(modules, "some-css");
    // What some packages keep their per-file texts in, and what reading as
    // a file would throw on.
    mkdirSync(join(where, "LICENSES"));
    writeFileSync(join(where, "LICENSES", "MIT.txt"), "not this one");
    writeFileSync(join(where, "LICENSE-MIT"), "nor this one");
    writeFileSync(join(where, "NOTICE"), "nor this");

    expect(cssPackagesIn(modules, ["some-css"])[0]?.text).toBe(
      "the licence itself",
    );
  });

  test("a directory is the only candidate: that is no notice at all", () => {
    const modules = scratch();
    const where = join(modules, "some-css");
    mkdirSync(where, { recursive: true });
    writeFileSync(
      join(where, "package.json"),
      JSON.stringify({ name: "some-css", version: "1.0.0", license: "MIT" }),
    );
    mkdirSync(join(where, "LICENSES"));
    expect(() => cssPackagesIn(modules, ["some-css"])).toThrow(
      /a directory of that name is not one/,
    );
  });

  test("it joins the record, sorted in with the module graph's", () => {
    const file = join(scratch(), "bundled-packages.txt");
    const record = bundledPackagesFrom(
      ["react 19.3.0 MIT", "some-css 2.1.0 MIT"],
      { check: false, file },
    );
    expect(record.split("\n").slice(2)).toEqual([
      "react 19.3.0 MIT",
      "some-css 2.1.0 MIT",
    ]);
  });

  test("its notice joins the file the module graph wrote, in its shape", () => {
    const file = join(scratch(), "THIRD_PARTY_LICENSES.txt");
    const packages = [
      { name: "some-css", version: "2.1.0", license: "MIT", text: "MIT text" },
      { name: "other", version: "1.0.0", license: "ISC", text: "ISC text" },
    ];
    // Nothing written yet: a build that shipped no notices at all must not
    // pass quietly.
    expect(() => appendCssNotices(file, packages)).toThrow(/was not written/);

    // What rollup-plugin-license leaves: entries parted by a rule, the last
    // one ending in its licence text and a newline.
    writeFileSync(file, "Name: react\nLicense Text:\n===\n\nreact text\n");
    appendCssNotices(file, packages);

    expect(readFileSync(file, "utf8")).toBe(
      "Name: react\nLicense Text:\n===\n\nreact text" +
        "\n\n---\n\n" +
        "Name: some-css\nVersion: 2.1.0\nLicense: MIT\n" +
        "Reached through a stylesheet, not through the module graph: " +
        "see DEPENDENCIES.md.\nLicense Text:\n===\n\nMIT text" +
        "\n\n---\n\n" +
        "Name: other\nVersion: 1.0.0\nLicense: ISC\n" +
        "Reached through a stylesheet, not through the module graph: " +
        "see DEPENDENCIES.md.\nLicense Text:\n===\n\nISC text\n",
    );
    // One rule per boundary, and none left dangling at the end.
    const written = readFileSync(file, "utf8");
    expect(written.split("\n\n---\n\n")).toHaveLength(3);
    expect(written.trimEnd().endsWith("---")).toBe(false);

    // Nothing to add is not a reason to touch the file.
    appendCssNotices(file, []);
    expect(readFileSync(file, "utf8")).toBe(written);
  });

  test("what is actually named is read from the real tree", () => {
    // The by-hand list and the installed tree have to agree, and this is
    // where a name that was written down without the package being there
    // stops the build.
    // Since ADR 0006 no stylesheet names a package, so the list is empty and
    // the real tree yields nothing. That is itself the thing to prove: an
    // entry here would be a package shipping through a stylesheet, and the
    // first question to ask of it is which stylesheet.
    expect(cssPackagesIn(resolve(here, "node_modules"))).toEqual([]);
  });
});

describe("where the plugins are registered", () => {
  test("the page build and the worker build both carry the gates", async () => {
    // A worker is a second rollup build: its modules are gated only if the
    // licence plugin is in its own list, and nothing else would say so.
    const config = (await import("../../vite.config")).default as {
      plugins: { name?: string }[];
      worker: { plugins: () => { name?: string }[] };
      build: { rollupOptions: { plugins: { name?: string }[] } };
    };
    const named = (plugins: { name?: string }[]) =>
      plugins.map((one) => one?.name);

    expect(named(config.plugins.flat())).toContain(
      "robinauts-stylesheet-rules",
    );
    // Without it the CSS-reached packages' notices never reach dist/.
    expect(named(config.plugins.flat())).toContain(
      "robinauts-css-package-notices",
    );
    expect(named(config.build.rollupOptions.plugins)).toContain(
      "rollup-plugin-license",
    );
    const worker = named(config.worker.plugins());
    expect(worker).toContain("robinauts-stylesheet-rules");
    expect(worker).toContain("rollup-plugin-license");
  });
});
