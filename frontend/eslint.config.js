// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

// The seam of ADR 0006 (and of ADR 0001 before it), as lint rules.
//
// The chat is behind `src/chat/index.ts`, an interface this project owns. What
// the rest of the application may import is that file; what is behind it --
// `src/chat/Chat.tsx`, the runtime, the thread, the Markdown renderer, the
// state and the wire client -- is the chat's own, and the rest of the
// application is everything else: the shell, the routes, the API client, the
// tests. The implementation used to be a library behind this same seam
// (ADR 0001); the seam outlived the library, which is what it was for.
//
// Three things had to be true for a pattern to be worth anything here, and
// each of them cost a review to learn:
//
//  1. **The implementation is matched by segment, not by prefix.** A path is
//     not its spelling: `../history/../chat/state` and `..//chat/state` reach
//     the same place as `../chat/state`. What is refused is a specifier with
//     a path segment that *is* `chat`, followed by anything but the seam's
//     own `index`, wherever in the path it sits.
//  2. **Specifiers are written plainly.** `//`, `/./`, a `..` that is not
//     part of the leading prefix, a trailing `/` -- every one of them is a
//     second spelling of a path, and a rule that reads spellings can only
//     ever chase them. They are refused outright, so there is one spelling of
//     each path for the rules above to read.
//  3. **A specifier that cannot be read cannot be cleared.** A variable, a
//     concatenation, an interpolated template, `import.meta.glob`,
//     `import.meta.resolve`: all refused, everywhere, because nothing could
//     clear them.
//
// Each restriction is one regular expression, used by `no-restricted-imports`
// for `import`, `export ... from` and `import type`, and by
// `no-restricted-syntax` for `import("...")`, `` import(`...`) `` and
// `new URL("...", import.meta.url)`, none of which the first rule sees.
// Sharing the string is what stops them from drifting apart.
//
// This is a blocking check (docs/specs/open-source.md, "Checks"). Every rule
// fires in `src/test/seam-rule.test.ts`.

/**
 * `new URL(..., import.meta.url)`, and only that.
 *
 * That is the form a bundler reads as a module reference and builds a second
 * bundle out of. `new URL("/api/runs", location.origin)` and
 * `new URL("https://example.test")` are ordinary browser code that happens to
 * share a constructor, and nothing here has any business refusing them.
 */
const MODULE_URL =
  'NewExpression[callee.name="URL"]:has(> MemberExpression[object.type="MetaProperty"])';

/**
 * A path segment that is exactly `chat`, followed by anything but the seam's
 * own `index`, wherever in the path it sits: `../chat/state`,
 * `../chat/thread/Thread`, `../chat/Chat.js`, `/src/chat/runtime`. `../chat`
 * and `../chat/index` are the seam itself and pass.
 */
const CHAT_INTERNALS_SEGMENT = String.raw`(?:^|/)chat/(?!index(?:\.[cm]?[jt]sx?)?$)[^/]`;

/** The implementation behind the seam, by any path that reaches it. */
const CHAT_IMPLEMENTATION = {
  source: CHAT_INTERNALS_SEGMENT,
  message:
    "what is under src/chat/ is behind the seam (ADR 0006). Import " +
    "src/chat/index.ts, and add to it what you are missing.",
};

/**
 * One spelling per path.
 *
 * `./a//b`, `./a/./b`, `./a/../b` and `./a/` are four more ways to write
 * `./b` or `./a/b`, and a rule that reads a specifier reads the spelling it
 * was given. Rather than teach every rule to normalise, the unusual spellings
 * are refused: `..` and `.` segments may appear only as the leading prefix,
 * which is how a relative import is written.
 */
const PLAIN_SEGMENTS = String.raw`(?:\.\.?/)*(?:(?!\.\.?(?:/|$))[^/]+)(?:/(?:(?!\.\.?(?:/|$))[^/]+))*`;
const UNNORMALISED_SPECIFIER = {
  source: `^(?!(?:${PLAIN_SEGMENTS})$)`,
  message:
    "write the path plainly: no empty, `.` or `..` segments inside it, and " +
    "no trailing slash. One spelling per path is what lets the rules of " +
    "ADR 0001 read one.",
};

/** Paths are written with forward slashes, whatever the machine writes them with. */
const BACKSLASH_SPECIFIER = {
  source: String.raw`\\`,
  message:
    "a module specifier uses forward slashes, on every machine. A backslash " +
    "is a separator to some resolvers and a character to others, and to the " +
    "rules of ADR 0001 it is a path they cannot read.",
};

/** A package is imported by its name, never by a path into node_modules. */
const THROUGH_NODE_MODULES = {
  source: "(?:^|/)node_modules(?:/|$)",
  message:
    "import a package by its name. A path through node_modules/ is a way " +
    "into a package that no rule here -- and no stylesheet scan -- reads as " +
    "that package.",
};

/** What every file in this package is held to, seam or no seam. */
const HYGIENE = [
  UNNORMALISED_SPECIFIER,
  BACKSLASH_SPECIFIER,
  THROUGH_NODE_MODULES,
];

/**
 * Node's globals are not in a browser.
 *
 * `src/**` is compiled into the bundle and runs in a page; `process`,
 * `Buffer` and the rest are not there, and a reference to one is a crash a
 * type checker will not catch, since the checks that drive tools do have
 * Node's types (`tsconfig.tools.json`). The tool-driving tests under
 * `src/test/` are the exception, because they run in Node.
 */
const NODE_GLOBALS = [
  "process",
  "Buffer",
  "__dirname",
  "__filename",
  "global",
  "require",
];

/**
 * `require` is not part of this package. It is ESM throughout
 * (`package.json`, `"type": "module"`), the bundle is ESM, and a `require`
 * would be both a module the seam rules do not see and a call that does not
 * work in a browser.
 */
const NO_REQUIRE = [
  {
    selector: 'CallExpression[callee.name="require"]',
    message:
      "this package is ESM throughout; use import. A require() is also a " +
      "module no seam rule can see (ADR 0006).",
  },
];

/**
 * Specifiers no rule can read, and so no rule can clear.
 *
 * `import(name)`, `import("a" + b)` and `` import(`${base}/react`) `` are
 * decided when they run, which is after every check has passed. The same
 * holds for `new URL(x, import.meta.url)`, which is how a bundler is told to
 * build a second bundle out of a file no import rule sees -- and only for
 * that form, so that an ordinary `new URL(someHref)` is left alone.
 * `import.meta.glob` is refused on the simpler rule: not "unless its pattern
 * reaches behind the seam", which would be one more pattern to keep in step,
 * but never -- nothing here has a use for it.
 */
const UNJUDGEABLE = [
  {
    selector:
      'ImportExpression[source.type!="Literal"][source.type!="TemplateLiteral"]',
    message:
      "import() with a specifier that is not a plain string cannot be checked " +
      "against the seam of ADR 0006, so it is not allowed. Write the " +
      "specifier out.",
  },
  {
    selector: "ImportExpression > TemplateLiteral[expressions.length>0]",
    message:
      "import() with an interpolated specifier cannot be checked against the " +
      "seam of ADR 0006, so it is not allowed. Write the specifier out.",
  },
  {
    selector: `${MODULE_URL}:not(:has(> Literal)):not(:has(> TemplateLiteral))`,
    message:
      "new URL(..., import.meta.url) names a module the bundler will build. " +
      "One that is not a plain string cannot be checked against the seam of " +
      "ADR 0006, so it is not allowed.",
  },
  {
    selector: `${MODULE_URL} > TemplateLiteral[expressions.length>0]`,
    message:
      "new URL(..., import.meta.url) with an interpolated specifier cannot be " +
      "checked against the seam of ADR 0006, so it is not allowed.",
  },
  {
    selector:
      'MemberExpression[object.type="MetaProperty"][property.name=/^(glob|resolve)/]',
    message:
      "import.meta.glob takes a pattern and import.meta.resolve takes whatever " +
      "it is handed; neither could be checked against the seam of ADR 0006, " +
      "and nothing here has a use for them.",
  },
];

/** Every way a specifier is written, as selectors, for one restriction. */
const spellings = ({ source, message }) => {
  // esquery delimits the regular expression with slashes, so the ones inside
  // it are escaped here rather than being written twice. The `i` flag matches
  // no-restricted-imports, which ignores case.
  const escaped = source.replaceAll("/", String.raw`\/`);
  const pattern = `/${escaped}/i`;
  return [
    { selector: `ImportExpression[source.value=${pattern}]`, message },
    {
      selector: `ImportExpression > TemplateLiteral > TemplateElement[value.cooked=${pattern}]`,
      message,
    },
    { selector: `${MODULE_URL} > Literal[value=${pattern}]`, message },
    {
      selector: `${MODULE_URL} > TemplateLiteral > TemplateElement[value.cooked=${pattern}]`,
      message,
    },
  ];
};

/**
 * The rules for one kind of file.
 *
 * @param restricted what this file may not name, beside the hygiene rules
 * @param unjudgeable whether specifiers it cannot read are refused
 */
const confine = (restricted, { unjudgeable = true } = {}) => {
  const patterns = [...HYGIENE, ...restricted];
  return {
    "no-restricted-imports": [
      "error",
      {
        patterns: patterns.map(({ source, message }) => ({
          regex: source,
          message,
        })),
      },
    ],
    "no-restricted-syntax": [
      "error",
      ...NO_REQUIRE,
      ...(unjudgeable ? UNJUDGEABLE : []),
      ...patterns.flatMap(spellings),
    ],
  };
};

export default tseslint.config(
  { ignores: ["dist", "src/api/schema.d.ts"] },
  {
    files: ["**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}"],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
    ],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    rules: confine([CHAT_IMPLEMENTATION]),
  },
  {
    // The build configuration and the checks run in Node, not in a browser.
    files: ["vite.config.ts", "eslint.config.js", "scripts/**"],
    languageOptions: { globals: globals.node },
  },
  {
    // The application is browser code, whatever the type checker is told.
    files: ["src/**"],
    ignores: ["src/test/**"],
    rules: {
      "no-restricted-globals": [
        "error",
        ...NODE_GLOBALS.map((name) => ({
          name,
          message:
            `${name} is Node's, and src/ is compiled into a page. The checks ` +
            "that drive tools live in src/test/ and scripts/.",
        })),
      ],
    },
  },
  {
    // The inside of the seam. Its files import one another freely -- the
    // seam does not apply to them -- but the hygiene rules still do, and so
    // does the refusal of a specifier nobody can read: this is this project's
    // code, and it has no use for one.
    files: ["src/chat/**"],
    rules: confine([]),
  },
);
