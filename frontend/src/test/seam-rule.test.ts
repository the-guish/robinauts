// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The lint rule that keeps the chat behind its seam, tested for firing.
 *
 * `npm run lint` proves only that no file in the tree breaks the rule, which
 * is also what it would print if the rule matched nothing at all. This runs
 * ESLint over files that do not exist, with the real configuration, and asks
 * which of them it refuses. A change that quietly stopped the seam from
 * matching -- a renamed directory, a pattern syntax that changed under us --
 * fails here (ADR 0006, docs/specs/open-source.md "Checks").
 */
import { readFileSync } from "node:fs";

import { ESLint } from "eslint";
import { expect, test } from "vitest";

/** The two rules that together make up the seam. */
const SEAM = ["no-restricted-imports", "no-restricted-syntax"];

/**
 * Whether the seam refuses this file, were it to exist.
 *
 * The paths are relative to the working directory, which is the root of this
 * package: the configuration ESLint finds is the one CI runs, not a copy.
 */
async function complains(path: string, code: string): Promise<string[]> {
  const eslint = new ESLint();
  const results = await eslint.lintText(code, { filePath: path });
  const messages = results.flatMap((result) => result.messages);
  expect(
    messages.filter((message) => message.fatal),
    `${path} did not parse`,
  ).toEqual([]);
  return messages.map((message) => message.ruleId ?? "");
}

/** What a refusal has to say for itself, beside being a refusal. */
interface Expected {
  /** Which of the two rules is meant to fire. */
  rule?: string;
  /** A fragment of the message, so a test proves its own pattern fired. */
  says?: string;
}

/**
 * Whether the seam refuses this file, and -- when it is told what to expect
 * -- that it is refused for the reason the test is about.
 *
 * Without the second argument a test proves only that something refused the
 * file, and several of these rules overlap: a spelling refused by the plain
 * -path rule would look like a seam rule doing its job. With it, each test
 * names the pattern it is there to prove.
 */
async function refuses(
  path: string,
  code: string,
  expected: Expected = {},
): Promise<boolean> {
  const eslint = new ESLint();
  const results = await eslint.lintText(code, { filePath: path });
  const messages = results.flatMap((result) => result.messages);
  expect(
    messages.filter((message) => message.fatal),
    `${path} did not parse`,
  ).toEqual([]);
  const seam = messages.filter((message) =>
    SEAM.includes(message.ruleId ?? ""),
  );
  if (expected.rule !== undefined || expected.says !== undefined) {
    const matching = seam.filter(
      (message) =>
        (expected.rule === undefined || message.ruleId === expected.rule) &&
        (expected.says === undefined ||
          message.message.includes(expected.says)),
    );
    expect(
      matching.length,
      `${code}\nwas refused by: ${seam
        .map((message) => `${message.ruleId ?? ""}: ${message.message}`)
        .join("\n")}`,
    ).toBeGreaterThan(0);
  }
  return seam.length > 0;
}

const SHELL = "src/shell/panel.tsx";
const HISTORY = "src/history/list.tsx";
const SEAM_FILE = "src/chat/index.ts";
const INSIDE = "src/chat/thread/thread.tsx";
const DEEPER = "src/chat/thread/parts/tool.tsx";

/** The refusal this file is about, as the message says it. */
const BEHIND = { rule: "no-restricted-imports", says: "behind the seam" };

test("the shell may not reach behind the seam, extension or none", async () => {
  for (const specifier of [
    "../chat/Chat",
    "../chat/Chat.js",
    "../chat/runtime",
    "../chat/state",
    "../chat/agui/client",
    "../chat/markdown/Markdown",
    "../chat/thread/thread",
    "/src/chat/runtime",
    "../chat/index/anything",
  ]) {
    expect(
      await refuses(SHELL, `import "${specifier}";`, BEHIND),
      specifier,
    ).toBe(true);
  }
  expect(await refuses(SHELL, 'import type { T } from "../chat/state";')).toBe(
    true,
  );
});

test("re-exporting is importing", async () => {
  expect(await refuses(SHELL, 'export { Chat } from "../chat/Chat";')).toBe(
    true,
  );
  expect(await refuses(SHELL, 'export * from "../chat/state";')).toBe(true);
});

test("import() is importing", async () => {
  expect(await refuses(SHELL, 'void import("../chat/Chat");')).toBe(true);
  expect(await refuses(SHELL, 'void import("../chat/thread/thread");')).toBe(
    true,
  );
});

test("a template literal is a specifier too", async () => {
  expect(await refuses(SHELL, "void import(`../chat/Chat`);")).toBe(true);
  expect(await refuses(SHELL, "void import(`../chat`);")).toBe(false);
});

test("case is not a way round it", async () => {
  // no-restricted-imports ignores case; the esquery selectors carry the same
  // `i` flag, so import() cannot be spelt past the rule either.
  expect(await refuses(SHELL, 'import "../CHAT/state";')).toBe(true);
  expect(await refuses(SHELL, 'void import("../Chat/State");')).toBe(true);
  expect(await refuses(SHELL, "void import(`../CHAT/state`);")).toBe(true);
});

test("a directory that merely contains the word is not the seam", async () => {
  // The segment has to be exactly `chat`: a neighbour called something else
  // is nobody's business here.
  expect(await refuses(SHELL, 'import "./chat-log";')).toBe(false);
  expect(await refuses(SHELL, 'import "../chatter/rows";')).toBe(false);
  expect(await refuses(SHELL, 'import "../chats/state";')).toBe(false);
});

test("a specifier that cannot be read cannot be cleared", async () => {
  // Nothing here judges what a variable will hold, so none of these is let
  // through on the grounds that it did not match.
  expect(
    await refuses(SHELL, "declare const w: string;\nvoid import(w);"),
  ).toBe(true);
  expect(await refuses(SHELL, 'void import("../chat" + "/state");')).toBe(true);
  expect(
    await refuses(
      SHELL,
      "declare const b: string;\nvoid import(`${b}/state`);",
    ),
  ).toBe(true);
  expect(
    await refuses(SHELL, 'void import.meta.glob("../chat/**/*.ts");'),
  ).toBe(true);
  expect(await refuses(SHELL, 'void import.meta.globEager("./*.ts");')).toBe(
    true,
  );
  // Inside the seam as well: it is this project's code, and nothing in it
  // has a use for a specifier nobody can read.
  expect(
    await refuses(INSIDE, "declare const w: string;\nvoid import(w);"),
  ).toBe(true);
});

test("require is not part of this package", async () => {
  expect(await refuses(SHELL, 'const x = require("../chat/state");')).toBe(
    true,
  );
  expect(await refuses(SHELL, 'const x = require("react");')).toBe(true);
  // Inside the seam too: the seam is off there, this is not.
  expect(await refuses(INSIDE, 'const x = require("react");')).toBe(true);
});

test("a path is not its spelling", async () => {
  // Every one of these reaches src/chat/state.ts. The rule reads a path
  // segment, so none of them is a way round it -- and the plain-specifier
  // rule refuses the odd spellings besides.
  // The plainly-written ones must be refused *by the seam rule*; the rest
  // are refused for being a second spelling of a path, which is a rule of its
  // own and is proved below.
  for (const specifier of [
    "../chat/state",
    "/src/chat/state",
    "../chat/state.js",
    "../chat/state.mjs",
    "../CHAT/state",
  ]) {
    expect(
      await refuses(SHELL, `import "${specifier}";`, BEHIND),
      specifier,
    ).toBe(true);
  }
  for (const specifier of [
    "../history/../chat/state",
    "..//chat/state",
    "./../chat/state",
    "../chat/./state",
    "../chat/state/",
  ]) {
    expect(await refuses(SHELL, `import "${specifier}";`), specifier).toBe(
      true,
    );
  }
});

test("a specifier is written plainly, or not at all", async () => {
  // These reach nothing behind the seam; they are refused for being a second
  // spelling of a path, which is what makes the rules above readable.
  const plainly = {
    rule: "no-restricted-imports",
    says: "write the path plainly",
  };
  for (const specifier of [
    ".//index",
    "./a/./b",
    "./a/../b",
    "./a/",
    "react/",
    "../chat//index",
  ]) {
    expect(
      await refuses(SHELL, `import "${specifier}";`, plainly),
      specifier,
    ).toBe(true);
  }
  expect(await refuses(SHELL, 'import "../chat/index";')).toBe(false);
  expect(await refuses(SHELL, 'import "../../api/client";')).toBe(false);
});

test("a package is imported by its name", async () => {
  const byPath = {
    rule: "no-restricted-imports",
    says: "import a package by its name",
  };
  expect(
    await refuses(SHELL, 'import "../../node_modules/react";', byPath),
  ).toBe(true);
  expect(
    await refuses(SHELL, 'import "/node_modules/react/index.js";', byPath),
  ).toBe(true);
  expect(
    await refuses(INSIDE, 'import "../../../node_modules/react";', byPath),
  ).toBe(true);
  expect(await refuses(SHELL, 'import "react";')).toBe(false);
});

test("the seam applies from anywhere outside, however deep", async () => {
  // src/history/ is not src/shell/, and a test harness is not either; the
  // rule is about where the import lands, not where it is written.
  expect(await refuses(HISTORY, 'import "../chat/state";', BEHIND)).toBe(true);
  expect(
    await refuses("src/test/harness.ts", 'import "../chat/runtime";', BEHIND),
  ).toBe(true);
  expect(
    await refuses("scripts/tool.mjs", 'import "../src/chat/state";', BEHIND),
  ).toBe(true);
});

test("a module reference in a URL is a module reference", async () => {
  // new Worker(new URL(...)) is how a bundler is told to build a second
  // bundle out of a file no import rule ever sees. It is read like import():
  // a string, a template, and anything that cannot be read at all.
  const url = (specifier: string) =>
    `void new Worker(new URL(${specifier}, import.meta.url));`;
  expect(await refuses(SHELL, url('"../chat/w.ts"'))).toBe(true);
  expect(await refuses(SHELL, url("`../chat/w.ts`"))).toBe(true);
  expect(await refuses(SHELL, url('"../history/../chat/w.ts"'))).toBe(true);
  expect(await refuses(SHELL, "declare const w: string;\n" + url("w"))).toBe(
    true,
  );
  expect(
    await refuses(SHELL, "declare const b: string;\n" + url("`${b}/w.ts`")),
  ).toBe(true);
  expect(await refuses(SHELL, url('"./w.ts"'))).toBe(false);
  // An ordinary URL is left alone: it names no module.
  expect(
    await refuses(SHELL, "declare const h: string;\nvoid new URL(h);"),
  ).toBe(false);
  // Inside the seam a worker is still named plainly or not at all.
  expect(await refuses(INSIDE, "declare const w: string;\n" + url("w"))).toBe(
    true,
  );
  expect(await refuses(INSIDE, url('"./w.ts"'))).toBe(false);
});

test("an ordinary URL is not a module reference", async () => {
  // Only `new URL(x, import.meta.url)` names a module. These are browser
  // code that happens to share a constructor, and the API client is full of
  // them; a rule that refused them would be a rule people turn off.
  for (const code of [
    'void new URL("/api/runs", location.origin);',
    'void new URL("https://example.test/x");',
    "declare const base: string;\nvoid new URL(`${base}/runs`);",
    "declare const href: string;\nvoid new URL(href);",
    'void new URL("../chat/w.ts", location.origin);',
  ]) {
    expect(await complains(SHELL, code), code).toEqual([]);
    expect(await complains("src/test/harness.ts", code), code).toEqual([]);
  }
});

test("a specifier uses forward slashes", async () => {
  const slashes = { says: "forward slashes" };
  // String.raw so that what ESLint parses is plain to read: the specifier in
  // each of these holds one backslash between each segment.
  expect(
    await refuses(SHELL, String.raw`import "..\\chat\\index";`, slashes),
  ).toBe(true);
  expect(
    await refuses(SHELL, String.raw`void import("..\\chat\\state");`, slashes),
  ).toBe(true);
});

test("import.meta.resolve is refused with import.meta.glob", async () => {
  expect(
    await refuses(SHELL, 'void import.meta.resolve("../chat/index");'),
  ).toBe(true);
});

test("Node's globals are not in a browser", async () => {
  expect(await complains(SHELL, 'void Buffer.from("x");')).toContain(
    "no-restricted-globals",
  );
  expect(await complains(SHELL, "void process.env;")).toContain(
    "no-restricted-globals",
  );
  expect(await complains(SHELL, "void __dirname;")).toContain(
    "no-restricted-globals",
  );
  expect(await complains(INSIDE, "void process.env;")).toContain(
    "no-restricted-globals",
  );
  // The checks that drive tools do run in Node, and say so by living here.
  expect(
    await complains("src/test/harness.ts", "void process.env;"),
  ).not.toContain("no-restricted-globals");
});

test("the seam itself is what the shell imports", async () => {
  expect(await refuses(SHELL, 'import "../chat";')).toBe(false);
  expect(await refuses(SHELL, 'import { Chat } from "../chat/index";')).toBe(
    false,
  );
  expect(
    await refuses(SHELL, 'import type { ChatProps } from "../chat/index";'),
  ).toBe(false);
});

test("inside the seam, the chat's files import one another", async () => {
  // The interface reaches its implementation, and the implementation reaches
  // its own parts, by the plain relative paths; the seam is for everybody
  // else.
  expect(await refuses(SEAM_FILE, 'import "./Chat";')).toBe(false);
  expect(await refuses(SEAM_FILE, 'export { Chat } from "./Chat";')).toBe(
    false,
  );
  for (const specifier of [
    "../runtime",
    "../state",
    "../agui/client",
    "../markdown/Markdown",
    "./composer",
    "../../icons",
  ]) {
    expect(await refuses(INSIDE, `import "${specifier}";`), specifier).toBe(
      false,
    );
  }
  expect(await refuses(DEEPER, 'import "../../state";')).toBe(false);
  // By the plain spelling, and by no other: the hygiene rules hold inside.
  expect(await refuses(INSIDE, 'import "../thread/../state";')).toBe(true);
  expect(await refuses(INSIDE, 'import "..//state";')).toBe(true);
});

test("nothing renames a path behind the seam's back", async () => {
  // An alias is a second name for a directory, and a rule that reads
  // specifiers reads the name it was given. `package.json` may declare
  // `imports`, and a tsconfig may declare `paths`; neither does, and a
  // reviewer seeing one here should ask what it points at.
  const read = (path: string) =>
    JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;

  expect(read("package.json").imports).toBeUndefined();

  // And the build resolves no alias of its own: `resolve.alias` would rename
  // a directory for the bundler and for nobody else.
  const config = readFileSync("vite.config.ts", "utf8");
  expect(config).not.toMatch(/\balias\b/);
  expect(config).not.toMatch(/\bdedupe\b/);

  for (const name of [
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.tools.json",
  ]) {
    const options = (read(name).compilerOptions ?? {}) as Record<
      string,
      unknown
    >;
    expect(options.paths, `${name} declares paths`).toBeUndefined();
    expect(options.baseUrl, `${name} declares baseUrl`).toBeUndefined();
  }
});
