// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
// @ts-check

/**
 * The npm licence gate: the policy of DEPENDENCIES.md over everything the
 * lockfile pins and npm installed.
 *
 * `vite.config.ts` gates the **bundle** -- the code that ships, whether it
 * arrived through rollup's module graph or through a CSS `@import` -- and
 * records it in `bundled-packages.txt`; it reads the allowed list from here,
 * so there is one copy of it. This gate judges the whole installed tree,
 * because DEPENDENCIES.md asks the same questions of a development dependency
 * as of any other. Neither one stands in for the other: a row's `scope` is
 * about dependency edges, and says nothing about what reached the bundle.
 *
 * What is not plainly on the allowed list needs a row in the "JavaScript
 * build tooling" table of DEPENDENCIES.md naming the same version, the same
 * licence and the right scope. A forbidden licence fails whatever a row says,
 * and so does a claim nobody can read. A row for an installed package that no
 * longer needs one, or for a package the lockfile no longer pins at all,
 * fails too, so the table cannot go stale across a bump.
 *
 * The node standard library and nothing else: a gate that needed vetting
 * would be one more thing to vet.
 *
 *     node scripts/check-licences.mjs
 *
 * Exit 1 is "a dependency fails the policy"; exit 2 is "the gate could not do
 * its work", as in scripts/licence_gate.py.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const FRONTEND = resolve(HERE, "..");
export const ROOT = resolve(FRONTEND, "..");
export const POLICY = resolve(ROOT, "DEPENDENCIES.md");
export const TABLE_HEADING = "JavaScript build tooling";

/**
 * The allowed list of DEPENDENCIES.md, and the restricted one.
 *
 * Read from the document at start-up rather than copied into it: the document
 * is the policy (`scripts/licence_gate.py` reads the same two paragraphs),
 * and a second copy of a list is a second thing to keep in step.
 * `src/test/licence-gate.test.ts` checks that what is read here is what the
 * document says.
 *
 * @param {string} document
 * @param {string} heading
 * @returns {Set<string>}
 */
export function categoryFrom(document, heading) {
  // The first paragraph of the section, which is where the list is, and which
  // is the paragraph `scripts/licence_gate.py::_identifier_list` reads --
  // the two gates have to read the same words. The rest of the section is
  // prose ("Restricted" explains its conditions there), so it cannot simply
  // be swept in. What stops a list from being quietly halved is the other
  // end: `src/test/licence-gate.test.ts` holds both lists against an expected
  // list written out by hand, so a shortened paragraph is a failing test.
  const section = (
    sectionOf(document, heading)
      .split("\n\n")
      .find((part) => part.trim() !== "") ?? ""
  ).trim();
  if (section === "") {
    throw new GateError(`the "${heading}" section of DEPENDENCIES.md is empty`);
  }
  /** @type {Set<string>} */
  const identifiers = new Set();
  for (const raw of section
    .replace(/\s+/g, " ")
    .replace(/\.$/, "")
    .split(",")) {
    const token = raw.trim();
    if (token === "") continue;
    if (!/^[A-Za-z0-9.+-]+$/.test(token)) {
      throw new GateError(
        `not a licence identifier in DEPENDENCIES.md: "${token}"`,
      );
    }
    // `CDDL-1.x` stands for the versions it covers, as in licence_gate.py.
    for (const value of token.toLowerCase().endsWith(".x")
      ? [`${token.slice(0, -2)}.0`, `${token.slice(0, -2)}.1`]
      : [token]) {
      if (isForbidden(value)) {
        throw new GateError(
          `DEPENDENCIES.md lists ${value} as ${heading.toLowerCase()}, and the ` +
            "gate knows it as forbidden; the two cannot both be true",
        );
      }
      identifiers.add(value.toLowerCase());
    }
  }
  if (identifiers.size === 0) {
    throw new GateError("a licence category in DEPENDENCIES.md is empty");
  }
  return identifiers;
}

/**
 * Fragments that mean a licence the policy forbids outright.
 *
 * Read from the start of each **word** of the claim, with that word's case
 * and punctuation taken away. This is blunter than the Python gate's
 * `_FORBIDDEN_IDENTIFIER` on purpose: that gate resolves a spelling through a
 * table of known aliases before it judges it, and npm has no such table --
 * it publishes `GPLv3`, `GPL2` and `LGPLv2.1` as they are, which a
 * word-bounded pattern reads as unknown, and unknown is a thing a row may
 * except.
 *
 * Anchored at a word's start, not merely contained in it, because contained
 * is too blunt: `Cross-Platform License` folded whole holds `sspl`, and a
 * fragment that begins nothing means nothing.
 *
 * `unlicensed` is npm's "no licence granted" and is one letter from
 * `Unlicense`, which is allowed -- and `unlicense` is not listed here, so the
 * allowed one cannot be caught by it.
 */
const FORBIDDEN_WORD =
  /^(a?gpl|lgpl|sspl|busl|elastic|proprietary|noncommercial|unlicensed|licenseref|ccbync|ccbynd)/;

/**
 * Names that are forbidden and are spelt as several words, so that no single
 * word of the claim carries them. These alone are looked for across the whole
 * claim; none is a substring of any permissive licence's name.
 */
const FORBIDDEN_PHRASES = [
  "commonsclause",
  "generalpubliclicense",
  "serversidepublic",
  "businesssource",
];

/** The restricted family, spelt as people write it rather than as SPDX. */
const RESTRICTED_FRAGMENTS = [
  "mpl20",
  "mozillapubliclicense",
  "epl20",
  "eclipsepubliclicense",
  "cddl",
];

/**
 * Claims that name no licence anybody can look up.
 *
 * DEPENDENCIES.md: an exception can never cover "a package that states no
 * licence at all ... or metadata nobody can read". `SEE LICENSE IN <file>`,
 * `NOASSERTION` and npm's old `MIT*` are claims about a file or a guess, not
 * identifiers, so they are refused here rather than falling through to
 * "unknown", which a row could then except.
 */
const UNREADABLE_PHRASE = /see\s+licen[cs]e\s+in\b/i;
const UNREADABLE_WORD = /^(noassertion|unknown)$/i;

/** The form a fragment is looked for in: no case, no punctuation. @param {string} text */
const folded = (text) => text.toLowerCase().replace(/[^a-z0-9]/g, "");

/**
 * Whether a claim is forbidden outright.
 * @param {string} claim
 */
export function isForbidden(claim) {
  const words = claim
    .split(/\s+/)
    .map(folded)
    .filter((word) => word !== "");
  if (words.some((word) => FORBIDDEN_WORD.test(word))) return true;
  const joined = folded(claim);
  return FORBIDDEN_PHRASES.some((phrase) => joined.includes(phrase));
}

/**
 * Whether a claim names anything that cannot be looked up.
 *
 * Read of the whole claim and of each of its tokens, so that one unreadable
 * side of a choice makes the claim unreadable: `MIT OR NOASSERTION` says the
 * package may be under something nobody can name, and picking the readable
 * side of that would be settling it in our own favour.
 *
 * @param {string} claim
 */
export function isUnreadable(claim) {
  if (UNREADABLE_PHRASE.test(claim)) return true;
  return identifiers(claim).some(
    // npm's old `MIT*` means "probably MIT", which is a guess, not a licence.
    (token) => UNREADABLE_WORD.test(token) || token.endsWith("*"),
  );
}

/**
 * `MIT AND (CC0-1.0 OR ISC)` as tokens; parentheses and operators are their own.
 * @param {string} expression
 * @returns {string[]}
 */
export function tokenise(expression) {
  return expression.match(/\(|\)|[^\s()]+/g) ?? [];
}

/**
 * Every identifier an expression names, operators and brackets removed.
 * @param {string} expression
 * @returns {string[]}
 */
export function identifiers(expression) {
  return tokenise(expression).filter(
    (token) => !/^(\(|\)|and|or|with)$/i.test(token),
  );
}

/**
 * The identifiers of an expression that the policy forbids outright.
 * @param {string} expression
 * @returns {string[]}
 */
export function forbiddenIn(expression) {
  const found = identifiers(expression).filter((one) => isForbidden(one));
  if (found.length > 0) return found;
  // Not every licence field is an SPDX expression. `Commons Clause` and
  // `GNU General Public License` are spaces away from being two and four
  // tokens, none of which is forbidden on its own, so the whole claim is
  // read as one thing as well.
  return isForbidden(expression) ? [expression.trim()] : [];
}

/**
 * The identifiers of an expression that the policy calls restricted.
 * @param {string} expression
 * @param {Set<string>} restricted
 * @returns {string[]}
 */
export function restrictedIn(expression, restricted = restrictedLicences()) {
  const named = identifiers(expression).filter((one) =>
    restricted.has(one.toLowerCase().replace(/\+$/, "")),
  );
  if (named.length > 0) return named;
  // `MPL 2.0` and `Mozilla Public License 2.0` are the same licence written
  // as nobody's identifier. Left merely unknown, they could be carried by a
  // `runtime` row, which is the one thing a restricted licence may not be.
  const joined = folded(expression);
  return RESTRICTED_FRAGMENTS.some((fragment) => joined.includes(fragment))
    ? [expression.trim()]
    : [];
}

/**
 * Whether an expression resolves to something the allowed list covers.
 *
 * A choice (`OR`) needs one allowed side; a conjunction (`AND`) needs them
 * all. `WITH` binds an exception to the licence in front of it, and the two
 * together are one thing the allowed list does not name -- so it is not
 * allowed, and needs a row. An expression that does not parse is not allowed
 * either: the gate fails closed.
 *
 * @param {string} expression
 * @param {Set<string>} allowed
 * @returns {boolean}
 */
export function isAllowed(expression, allowed = allowedLicences()) {
  const tokens = tokenise(expression);
  let at = 0;
  const peek = () => tokens[at];
  const take = () => tokens[at++];

  /** @returns {boolean} */
  function unit() {
    if (peek() === "(") {
      take();
      const inner = choice();
      if (take() !== ")") throw new Error("unbalanced");
      return inner;
    }
    const identifier = take();
    if (identifier === undefined || /^(and|or|with|\))$/i.test(identifier)) {
      throw new Error("not a licence identifier");
    }
    if (peek()?.toLowerCase() === "with") {
      take();
      take();
      return false;
    }
    return allowed.has(identifier.toLowerCase().replace(/\+$/, ""));
  }

  /** @returns {boolean} */
  function conjunction() {
    let value = unit();
    while (peek()?.toLowerCase() === "and") {
      take();
      value = unit() && value;
    }
    return value;
  }

  /** @returns {boolean} */
  function choice() {
    let value = conjunction();
    while (peek()?.toLowerCase() === "or") {
      take();
      value = conjunction() || value;
    }
    return value;
  }

  try {
    const value = choice();
    return at === tokens.length && value;
  } catch {
    return false;
  }
}

/** A problem with the gate's own inputs, as opposed to with a dependency. */
export class GateError extends Error {}

/**
 * The text under a heading of a markdown document, up to the next heading.
 * @param {string} document
 * @param {string} heading
 * @returns {string}
 */
function sectionOf(document, heading) {
  const escaped = heading.replace(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`);
  const found = new RegExp(
    String.raw`^#+\s+${escaped}\s*$\n` +
      String.raw`([\s\S]*?)(?=^#+\s|$(?![\s\S]))`,
    "m",
  ).exec(document);
  if (found === null) {
    throw new GateError(`DEPENDENCIES.md has no "${heading}" section`);
  }
  return found[1] ?? "";
}

/**
 * DEPENDENCIES.md, read as text.
 * @param {string} [path]
 * @returns {string}
 */
export function readPolicy(path = POLICY) {
  try {
    return readFileSync(path, "utf8");
  } catch (failure) {
    throw new GateError(
      `DEPENDENCIES.md could not be read (${describe(failure)})`,
    );
  }
}

/** @type {{allowed: Set<string>, restricted: Set<string>} | null} */
let categories = null;

/**
 * The policy's two category lists, read the first time something asks.
 *
 * **Not at module scope.** Reading a file and parsing a document is work that
 * can fail, and work that fails while a module is being imported fails
 * outside anything that could turn it into an exit code -- a renamed heading
 * would print a stack and exit 1, which reads as "a dependency failed". Asked
 * for inside `main`, it is a `GateError` and exit 2 like any other.
 */
function policy() {
  if (categories === null) {
    const document = readPolicy();
    categories = {
      allowed: categoryFrom(document, "Allowed"),
      restricted: categoryFrom(document, "Restricted"),
    };
  }
  return categories;
}

/** Every licence a package here may carry without a row. */
export const allowedLicences = () => policy().allowed;
/** Restricted: development only, unmodified, unbundled, and named by a row. */
export const restrictedLicences = () => policy().restricted;

/** @typedef {{name: string, version: string, licence: string, scope: string}} Row */

/** The scopes a row may state, and what they mean about the lockfile. */
const SCOPES = new Set(["development", "runtime"]);

/**
 * The rows under the fixed heading of DEPENDENCIES.md.
 *
 * Kept as a list rather than a map, because one tree can hold two versions of
 * a package and the licence of the one is no statement about the other.
 *
 * @param {string} document
 * @returns {Row[]}
 */
export function readTable(document) {
  const section = sectionOf(document, TABLE_HEADING);
  /** @type {string[] | null} */
  let header = null;
  /** @type {Row[]} */
  const rows = [];
  for (const line of section.split("\n")) {
    const text = line.trim();
    if (!text.startsWith("|")) continue;
    const cells = text
      .replace(/^\||\|$/g, "")
      .split("|")
      .map((cell) => cell.trim());
    if (cells.every((cell) => /^:?-{2,}:?$/.test(cell))) continue;
    if (header === null) {
      header = cells.map((cell) => cell.toLowerCase());
      for (const wanted of ["package", "version", "licence", "scope"]) {
        if (!header.includes(wanted)) {
          throw new GateError(
            `the ${TABLE_HEADING} table has no "${wanted}" column`,
          );
        }
      }
      continue;
    }
    if (cells.length !== header.length) {
      throw new GateError(
        `a row of the ${TABLE_HEADING} table has ${cells.length} of ` +
          `${header.length} cells`,
      );
    }
    const names = header;
    /** @param {string} name */
    const cell = (name) =>
      (cells[names.indexOf(name)] ?? "").replaceAll("`", "").trim();
    const scope = cell("scope").toLowerCase();
    if (!SCOPES.has(scope)) {
      throw new GateError(
        `a row of the ${TABLE_HEADING} table states the scope "${scope}", which ` +
          `is not one of ${[...SCOPES].join(", ")}`,
      );
    }
    const packages = [
      ...(cells[names.indexOf("package")] ?? "").matchAll(/`([^`]+)`/g),
    ].map((match) => match[1] ?? "");
    if (packages.length === 0) {
      throw new GateError(
        `a row of the ${TABLE_HEADING} table names no package in backticks`,
      );
    }
    for (const name of packages) {
      rows.push({
        name,
        version: cell("version"),
        licence: cell("licence"),
        scope,
      });
    }
  }
  if (header === null) {
    throw new GateError(`the ${TABLE_HEADING} section has no table`);
  }
  return rows;
}

/**
 * Every licence a package's own `package.json` claims, in any of the fields
 * it may claim one in.
 *
 * A package may state its licence more than once -- `license` as a string or
 * as the old `{ "type": ... }` object, and the legacy `licenses` array -- and
 * the claims do not always agree.
 *
 * @param {Record<string, unknown>} own
 * @returns {string[]}
 */
export function statedClaims(own) {
  /** @type {string[]} */
  const claims = [];
  // Each field may hold a string, an object with a `type`, or an array of
  // either -- every shape npm has ever published a licence in.
  for (const field of [own.license, own.licenses]) {
    for (const one of Array.isArray(field) ? field : [field]) {
      if (typeof one === "string") {
        claims.push(one);
        continue;
      }
      const type = /** @type {{type?: unknown} | null} */ (one ?? null)?.type;
      if (typeof type === "string") claims.push(type);
    }
  }
  return [
    ...new Set(
      claims.map((claim) => claim.trim()).filter((claim) => claim !== ""),
    ),
  ];
}

/**
 * Those claims as one expression, joined by `AND`.
 *
 * `AND`, not `OR`, and deliberately: the worst of them decides, as it does in
 * `scripts/licence_gate.py`. A package claiming MIT in one field and the GPL
 * in another is a question for a person, not something to settle in our own
 * favour by reading the fields as a choice. `MIT` beside `ISC` costs nothing;
 * `MIT` beside `GPL-3.0` fails, which is the point.
 *
 * @param {Record<string, unknown>} own
 * @returns {string}
 */
export function statedLicence(own) {
  return statedClaims(own).join(" AND ");
}

/**
 * What the policy is applied to. `where` is the path in the tree, which only
 * the reading needs; `judge` is given the four fields it judges.
 *
 * @typedef {{name: string, version: string, licence: string, development: boolean}} Judged
 * @typedef {Judged & {where: string}} Installed
 */

/**
 * Every package the lockfile pins, as installed here.
 *
 * A platform-specific optional dependency for another operating system is
 * pinned and not installed; there is no licence here to read, and its row is
 * read on the machine that does install it -- which is why the table names
 * every platform. Anything else that is missing, or that will not parse, is
 * the gate's own inputs being wrong, and stops it.
 *
 * @param {string} root
 * @returns {{packages: Installed[], elsewhere: string[], pinned: Set<string>}}
 */
export function installed(root = FRONTEND) {
  const lockfile = resolve(root, "package-lock.json");
  /** @type {{packages?: Record<string, Record<string, unknown>>}} */
  let lock;
  try {
    lock = JSON.parse(readFileSync(lockfile, "utf8"));
  } catch (failure) {
    throw new GateError(
      `package-lock.json could not be read (${describe(failure)}); the gate has ` +
        "nothing to judge",
    );
  }
  /** @type {Installed[]} */
  const packages = [];
  /** @type {string[]} */
  const elsewhere = [];
  /** @type {Set<string>} */
  const pinned = new Set();
  for (const [where, entry] of Object.entries(lock.packages ?? {})) {
    // The root is this project; a link is a directory of ours, not a package
    // the registry served.
    if (where === "" || entry.link === true) continue;
    const name = String(entry.name ?? where.replace(/^.*node_modules\//, ""));
    const version = String(entry.version ?? "");
    pinned.add(`${name}@${version}`);
    /** @type {Record<string, unknown>} */
    let own;
    try {
      own = JSON.parse(
        readFileSync(resolve(root, where, "package.json"), "utf8"),
      );
    } catch (failure) {
      const code = /** @type {{code?: string}} */ (failure)?.code;
      const missing = code === "ENOENT" || code === "ENOTDIR";
      // `optional` and `devOptional` both mean npm may leave it out; a
      // platform's binary is usually the second when the tree is dev-only.
      const mayBeAbsent = entry.optional === true || entry.devOptional === true;
      if (missing && mayBeAbsent) {
        elsewhere.push(`${name} ${version}`);
        continue;
      }
      throw new GateError(
        missing
          ? `${name} ${version} is locked at ${where} and is not installed; ` +
              "run `npm ci`"
          : `${name} ${version} has a package.json the gate cannot read ` +
              `(${describe(failure)})`,
      );
    }
    packages.push({
      name,
      version: version === "" ? String(own.version ?? "") : version,
      licence: statedLicence(own),
      // npm marks a package `dev` only when *every* path to it is a
      // development dependency. Without the mark it is reachable from the
      // runtime dependencies, and is shipped code whatever it looks like.
      development: entry.dev === true,
      where,
    });
  }
  return { packages, elsewhere, pinned };
}

/** What went wrong, as a sentence. @param {unknown} failure */
function describe(failure) {
  return failure instanceof Error ? failure.message : String(failure);
}

/**
 * A version and nothing else: no range, no choice, no wildcard.
 *
 * Semver's own shape, one prerelease and then one build, each introduced by
 * its own character. The pattern it replaces took any number of either, and
 * since `-` also belongs to the characters after one, a long run of `-a`
 * could be split in exponentially many ways before failing
 * (SonarCloud AaDjWhnOz5PIa1mbqWEW, javascript:S5852).
 */
export const EXACT = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

/**
 * A tarball under `vendor/`, packed from a pinned commit: exact as well.
 *
 * `@neorc/ui` and `@oneberth/ui` are published nowhere yet. They are packed
 * from the neorc commit `backend/pyproject.toml` pins
 * (`scripts/ui-packages-from-source.sh`), and the lockfile holds each
 * tarball's hash, so `npm ci` installs that content or nothing.
 */
export const VENDORED = /^file:vendor\/[a-z0-9][a-z0-9.-]*\.tgz$/;

/**
 * Every problem with the exact pins of a `package.json`.
 * @param {Record<string, unknown>} manifest
 * @returns {string[]}
 */
export function pinProblems(manifest) {
  /** @type {string[]} */
  const problems = [];
  /**
   * @param {unknown} value
   * @param {string} where
   */
  const look = (value, where) => {
    if (typeof value === "string") {
      if (!EXACT.test(value) && !VENDORED.test(value)) {
        problems.push(
          `package.json ${where} is "${value}", which is not an exact version; ` +
            ".npmrc sets save-exact and an update is a deliberate diff",
        );
      }
      return;
    }
    // `overrides` nests, and a nested object's keys are package names too.
    if (value !== null && typeof value === "object") {
      for (const [key, inner] of Object.entries(value))
        look(inner, `${where}.${key}`);
    }
  };
  for (const field of [
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
    "overrides",
  ]) {
    const table = /** @type {Record<string, unknown>} */ (
      manifest[field] ?? {}
    );
    for (const [name, wanted] of Object.entries(table)) {
      look(wanted, `${field}.${name}`);
    }
  }
  return problems;
}

/**
 * Every problem the policy has with this tree. Empty means it passes.
 *
 * @param {{packages: Judged[], rows: Row[], pinned?: Set<string>,
 *          allowed?: Set<string>, restricted?: Set<string>}} what
 * @returns {{problems: string[], excepted: number}}
 */
export function judge({
  packages,
  rows,
  pinned,
  allowed = allowedLicences(),
  restricted = restrictedLicences(),
}) {
  /** @type {string[]} */
  const problems = [];
  /** @param {string} message */
  const fail = (message) => problems.push(message);

  /** @type {Map<string, Row[]>} */
  const byName = new Map();
  for (const row of rows) {
    const found = byName.get(row.name);
    if (found === undefined) byName.set(row.name, [row]);
    else found.push(row);
  }

  /** Rows that answered for a package, by name and version. */
  const used = new Set();
  /** Packages already answered for; the stale-row pass has nothing to add. */
  const settled = new Set();

  for (const { name, version, licence, development } of packages) {
    const called = `${name} ${version}`;
    const key = `${name}@${version}`;
    if (licence === "") {
      fail(
        `${called} states no licence at all, which DEPENDENCIES.md forbids outright`,
      );
      settled.add(key);
      continue;
    }
    const forbidden = forbiddenIn(licence);
    if (forbidden.length > 0) {
      fail(
        `${called} is ${licence}, which DEPENDENCIES.md forbids ` +
          `(${forbidden.join(", ")}); no row may except it`,
      );
      settled.add(key);
      continue;
    }
    if (isUnreadable(licence)) {
      fail(
        `${called} states "${licence}", which names no licence anybody can look ` +
          "up; DEPENDENCIES.md says no exception may cover metadata nobody can read",
      );
      settled.add(key);
      continue;
    }
    if (isAllowed(licence, allowed)) continue;

    const candidates = byName.get(name) ?? [];
    const row = candidates.find((one) => one.version === version);
    if (row === undefined) {
      fail(
        candidates.length === 0
          ? `${called} is ${licence}, which is not on the allowed list and has no ` +
              `row in the "${TABLE_HEADING}" table of DEPENDENCIES.md`
          : `${called} is ${licence}, and DEPENDENCIES.md excepts ${name} only at ` +
              `${candidates.map((one) => one.version).join(", ")}: an exception holds ` +
              "for the version somebody read the licence in, so read it again and " +
              "add the row",
      );
      continue;
    }
    used.add(key);
    if (row.licence.toLowerCase() !== licence.toLowerCase()) {
      fail(
        `${called} states ${licence}, and DEPENDENCIES.md says ${row.licence} for ` +
          "it; a licence is re-checked on every bump",
      );
    }
    if (row.scope === "development" && !development) {
      fail(
        `${called} is excepted as development only, and the lockfile does not ` +
          "mark it dev: the runtime dependencies reach it, so it is shipped code",
      );
    }
    const carried = restrictedIn(licence, restricted);
    if (carried.length > 0 && row.scope !== "development") {
      fail(
        `${called} is ${carried.join(", ")}, which DEPENDENCIES.md restricts to ` +
          `an unmodified, unbundled dependency; a row with scope "${row.scope}" ` +
          "cannot carry it",
      );
    }
  }

  // A row that no longer earns its keep is a row nobody will read again --
  // whether the package is still here and no longer needs it, or is gone.
  const here = new Set(packages.map((one) => `${one.name}@${one.version}`));
  const locked = pinned ?? here;
  for (const row of rows) {
    const key = `${row.name}@${row.version}`;
    if (settled.has(key)) continue;
    if (here.has(key) && !used.has(key)) {
      fail(
        `DEPENDENCIES.md excepts ${row.name} ${row.version}, which is installed ` +
          "and needs no exception; remove the row",
      );
    } else if (!locked.has(key)) {
      fail(
        `DEPENDENCIES.md excepts ${row.name} ${row.version}, which the lockfile ` +
          "does not pin at any version; remove the row, or correct it",
      );
    }
  }
  return { problems, excepted: used.size };
}

function main() {
  /** @type {{problems: string[], excepted: number}} */
  let outcome;
  /** @type {string[]} */
  let pins;
  /** @type {number} */
  let read;
  /** @type {number} */
  let elsewhere;
  try {
    const tree = installed();
    read = tree.packages.length;
    elsewhere = tree.elsewhere.length;
    if (read === 0) {
      throw new GateError(
        "the lockfile pinned no installed package at all; run `npm ci`",
      );
    }
    outcome = judge({
      packages: tree.packages,
      rows: readTable(readPolicy()),
      pinned: tree.pinned,
    });
    /** @type {Record<string, unknown>} */
    let manifest;
    const where = resolve(FRONTEND, "package.json");
    try {
      manifest = JSON.parse(readFileSync(where, "utf8"));
    } catch (failure) {
      throw new GateError(
        `package.json could not be read (${describe(failure)})`,
      );
    }
    pins = pinProblems(manifest);
  } catch (failure) {
    if (failure instanceof GateError) {
      console.error(
        `the licence gate could not do its work: ${failure.message}`,
      );
      process.exit(2);
    }
    throw failure;
  }
  const problems = [...outcome.problems, ...pins];
  if (problems.length > 0) {
    for (const problem of problems) console.error(`  ${problem}`);
    console.error(
      `\n${problems.length} problem(s): see DEPENDENCIES.md and ` +
        "docs/contributing/js-dependencies.md.",
    );
    process.exit(1);
  }
  console.log(
    `All ${read} installed packages pass the licence policy ` +
      `(${outcome.excepted} by a named exception in DEPENDENCIES.md; ` +
      `${elsewhere} pinned for other platforms and not installed here), ` +
      "and every version in package.json is exact.",
  );
}

// Run only when run; the tests import the parts above. A gate that fell over
// judged nothing, so it exits 2 rather than letting node print a crash and
// exit 1 -- which a caller would read as "a dependency failed".
const invoked = process.argv[1];
if (
  invoked !== undefined &&
  resolve(invoked) === fileURLToPath(import.meta.url)
) {
  try {
    main();
  } catch (failure) {
    console.error(
      "the licence gate could not do its work: it fell over.\n" +
        (failure instanceof Error
          ? (failure.stack ?? failure.message)
          : String(failure)),
    );
    process.exit(2);
  }
}
