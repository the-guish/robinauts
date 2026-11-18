// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
// @ts-check

/**
 * The npm advisory gate: `npm audit` over everything the lockfile pins.
 *
 * An advisory fails the build unless the "Advisories with no fix" table of
 * DEPENDENCIES.md records it. That section says what a row is held to.
 *
 *     node scripts/check-advisories.mjs
 *
 * Exit 1 is "an advisory fails the policy"; exit 2 is "the gate could not do
 * its work": npm did not answer, or answered for less than the lock pins.
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  EXACT,
  FRONTEND,
  GateError,
  namedIn,
  readPolicy,
  tableUnder,
} from "./check-licences.mjs";

export const TABLE_HEADING = "Advisories with no fix";

/** How far ahead a row's `review by` may be. */
export const REVIEW_WITHIN_DAYS = 7;

/** A GitHub advisory identifier, which is what a row names. */
const ADVISORY = /^GHSA(?:-[0-9a-z]{4}){3}$/;

/** What went wrong, as a sentence. @param {unknown} failure */
function describe(failure) {
  return failure instanceof Error ? failure.message : String(failure);
}

/**
 * A calendar day, as `YYYY-MM-DD`, or null when the text is not one.
 * @param {string} text
 * @returns {string | null}
 */
function day(text) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
  const moment = new Date(`${text}T00:00:00Z`);
  // `2026-02-30` parses, as the second of March; a day is one that survives
  // being written out again.
  return !Number.isNaN(moment.getTime()) &&
    moment.toISOString().slice(0, 10) === text
    ? text
    : null;
}

/**
 * The day a number of days after another.
 * @param {string} from
 * @param {number} days
 */
function later(from, days) {
  return new Date(Date.parse(`${from}T00:00:00Z`) + days * 86_400_000)
    .toISOString()
    .slice(0, 10);
}

/** @typedef {{advisory: string, name: string, version: string, review: string}} Row */

/**
 * The rows under the fixed heading of DEPENDENCIES.md. A row that cannot be
 * read stops the gate: the table is the gate's own input.
 *
 * @param {string} document
 * @returns {Row[]}
 */
export function readRows(document) {
  /** @type {Row[]} */
  const rows = [];
  const why = "why it is accepted";
  for (const cells of tableUnder(document, TABLE_HEADING, [
    "advisory",
    "package",
    "version",
    "review by",
    why,
  ])) {
    /** @param {string} name */
    const cell = (name) => (cells.get(name) ?? "").replaceAll("`", "").trim();
    const advisory = cell("advisory");
    if (!ADVISORY.test(advisory)) {
      throw new GateError(
        `a row of the ${TABLE_HEADING} table names the advisory "${advisory}", ` +
          "which is not a GHSA identifier",
      );
    }
    const packages = namedIn(cells.get("package") ?? "");
    if (packages.length === 0) {
      throw new GateError(
        `the ${advisory} row of the ${TABLE_HEADING} table names no package in ` +
          "backticks",
      );
    }
    const version = cell("version");
    if (!EXACT.test(version)) {
      throw new GateError(
        `the ${advisory} row of the ${TABLE_HEADING} table states the version ` +
          `"${version}", which is not an exact version`,
      );
    }
    const review = day(cell("review by"));
    if (review === null) {
      throw new GateError(
        `the ${advisory} row of the ${TABLE_HEADING} table is to be reviewed by ` +
          `"${cell("review by")}", which is not a day written YYYY-MM-DD`,
      );
    }
    if (cell(why) === "") {
      throw new GateError(
        `the ${advisory} row of the ${TABLE_HEADING} table does not say why it ` +
          "is accepted",
      );
    }
    for (const name of packages) {
      rows.push({ advisory, name, version, review });
    }
  }
  return rows;
}

/**
 * The lockfile's packages, by their path in the tree.
 * @typedef {Record<string, {version?: unknown, link?: unknown}>} Locked
 */

/**
 * The lockfile, read.
 * @param {string} [root]
 * @returns {Locked}
 */
export function readLock(root = FRONTEND) {
  try {
    const lock = JSON.parse(
      readFileSync(resolve(root, "package-lock.json"), "utf8"),
    );
    return lock.packages ?? {};
  } catch (failure) {
    throw new GateError(
      `package-lock.json could not be read (${describe(failure)}); the gate has ` +
        "nothing to hold the audit to",
    );
  }
}

/**
 * How many packages the lockfile pins: everything but the project itself and
 * a link, which is a directory of ours.
 * @param {Locked} locked
 */
export function pinnedIn(locked) {
  return Object.entries(locked).filter(
    ([where, entry]) => where !== "" && entry.link !== true,
  ).length;
}

/**
 * What `npm audit --json` printed, as a report this gate can judge.
 *
 * An audit of nothing passes everything, so a report is held to covering
 * every package the lock pins (scripts/check-audit.sh does the same for
 * pip-audit).
 *
 * @param {string} printed
 * @param {number | null} status npm's exit status
 * @param {number} pinned how many packages the lockfile pins
 * @returns {{vulnerabilities: Record<string, Record<string, unknown>>}}
 */
export function readReport(printed, status, pinned) {
  /** @type {Record<string, unknown>} */
  let report;
  try {
    report = JSON.parse(printed);
  } catch {
    throw new GateError(
      `npm audit produced no report to read (it exited ${status})`,
    );
  }
  const vulnerabilities = report?.vulnerabilities;
  if (
    report?.auditReportVersion !== 2 ||
    vulnerabilities === null ||
    typeof vulnerabilities !== "object" ||
    Array.isArray(vulnerabilities)
  ) {
    // What npm prints when the registry does not answer: a `message`, and an
    // `error` whose summary is as often empty as not.
    const { message, error } =
      /** @type {{message?: unknown, error?: {summary?: unknown}}} */ (
        report ?? {}
      );
    const said = error?.summary || message;
    throw new GateError(
      "npm audit answered with something other than a version 2 report" +
        (said ? ` (${String(said)})` : "") +
        `, and exited ${status}`,
    );
  }
  const metadata =
    /** @type {{dependencies?: {total?: unknown}} | undefined} */ (
      report.metadata
    );
  const audited = metadata?.dependencies?.total;
  if (pinned === 0) {
    throw new GateError("the lockfile pins no package at all");
  }
  if (audited !== pinned) {
    throw new GateError(
      `npm audit looked at ${String(audited)} of the ${pinned} packages the ` +
        "lock pins",
    );
  }
  const found = Object.keys(vulnerabilities).length;
  if (found === 0 && status !== 0) {
    throw new GateError(
      `npm audit named no advisory and exited ${status}; read what it printed`,
    );
  }
  return {
    vulnerabilities: /** @type {Record<string, Record<string, unknown>>} */ (
      vulnerabilities
    ),
  };
}

/**
 * Whether one version is newer than another, by the three numbers of each.
 * A version that cannot be read is taken to be newer, so that a person looks.
 *
 * @param {string} one
 * @param {string} other
 */
export function isNewer(one, other) {
  /** @param {string} version */
  const numbers = (version) =>
    (/^(\d+)\.(\d+)\.(\d+)/.exec(version) ?? []).slice(1).map(Number);
  const [left, right] = [numbers(one), numbers(other)];
  if (left.length !== 3 || right.length !== 3) return true;
  for (let at = 0; at < 3; at++) {
    const [mine, theirs] = [left[at] ?? 0, right[at] ?? 0];
    if (mine !== theirs) return mine > theirs;
  }
  return false;
}

/**
 * The fix npm reports for a vulnerable package, if it is one that moves
 * forward, as words; null when there is none.
 *
 * npm's `fixAvailable` is `true` when `npm audit fix` would clear it, `false`
 * when nothing would, and otherwise the one of our own dependencies it would
 * move, and to which version. When no release has the fix, that version is an
 * older one, from before the dependency was declared: not a fix.
 *
 * @param {unknown} offered
 * @param {Locked} locked
 * @returns {string | null}
 */
export function forwardFix(offered, locked) {
  if (offered === true) {
    return "npm reports a fix within the versions already asked for";
  }
  if (offered === null || typeof offered !== "object") return null;
  const { name, version } = /** @type {{name?: unknown, version?: unknown}} */ (
    offered
  );
  if (typeof name !== "string" || typeof version !== "string") {
    return "npm reports a fix the gate cannot read";
  }
  const pinned = locked[`node_modules/${name}`]?.version;
  if (typeof pinned !== "string") {
    return `npm reports that ${name} ${version} is clear of it`;
  }
  return isNewer(version, pinned)
    ? `npm reports that ${name} ${version} is clear of it, which is newer ` +
        `than the ${pinned} the lock pins`
    : null;
}

/**
 * One advisory against one pinned version of one package.
 * @typedef {{advisory: string, name: string, version: string,
 *            title: string, severity: string, fix: string | null}} Finding
 */

/**
 * Every advisory the report names, against each version the lock pins of the
 * package it is about.
 *
 * npm counts every package that depends on a vulnerable one as a
 * vulnerability too. Only the entry an advisory is about carries it, as an
 * object in `via`, and only those are judged.
 *
 * Where a package is pinned at two versions and npm marks both, each advisory
 * is held against both: telling them apart would take a semver
 * implementation, and holding one too many fails closed.
 *
 * @param {{vulnerabilities: Record<string, Record<string, unknown>>}} report
 * @param {Locked} locked
 * @returns {Finding[]}
 */
export function findingsIn(report, locked) {
  /** @type {Finding[]} */
  const findings = [];
  for (const [name, entry] of Object.entries(report.vulnerabilities)) {
    const via = Array.isArray(entry.via) ? entry.via : [];
    const nodes = Array.isArray(entry.nodes) ? entry.nodes.map(String) : [];
    const fix = forwardFix(entry.fixAvailable, locked);
    /** @type {Set<string>} */
    const versions = new Set();
    for (const where of nodes) {
      const pinned = locked[where]?.version;
      if (typeof pinned !== "string") {
        throw new GateError(
          `npm audit reports ${name} at ${where}, which the lockfile does not pin`,
        );
      }
      versions.add(pinned);
    }
    for (const one of via) {
      if (one === null || typeof one !== "object") continue;
      const { url, title, severity } =
        /** @type {{url?: unknown, title?: unknown, severity?: unknown}} */ (
          one
        );
      const advisory = String(url ?? "").replace(/^.*\//, "");
      if (versions.size === 0) {
        throw new GateError(
          `npm audit reports ${advisory} for ${name} and names no place in the ` +
            "tree it applies to",
        );
      }
      for (const version of versions) {
        findings.push({
          // An advisory no row could name is still one, and fails.
          advisory: ADVISORY.test(advisory) ? advisory : String(url ?? title),
          name,
          version,
          title: String(title ?? ""),
          severity: String(severity ?? "unknown"),
          fix,
        });
      }
    }
  }
  if (
    findings.length === 0 &&
    Object.keys(report.vulnerabilities).length !== 0
  ) {
    throw new GateError(
      "npm audit names vulnerable packages and no advisory behind them",
    );
  }
  return findings;
}

/**
 * Every problem the policy has with what npm reported. Empty means it passes.
 *
 * @param {{findings: Finding[], rows: Row[], today: string}} what
 * @returns {{problems: string[], excepted: (Finding & {review: string})[]}}
 */
export function judge({ findings, rows, today }) {
  /** @type {string[]} */
  const problems = [];
  /** @param {string} message */
  const fail = (message) => problems.push(message);
  /** @type {(Finding & {review: string})[]} */
  const excepted = [];
  /** @param {{advisory: string, name: string, version: string}} one */
  const key = (one) => `${one.advisory} ${one.name}@${one.version}`;
  const used = new Set();

  for (const finding of findings) {
    const { advisory, name, version, title, severity, fix } = finding;
    const called = `${advisory}, in ${name} ${version}`;
    const candidates = rows.filter(
      (row) => row.advisory === advisory && row.name === name,
    );
    const row = candidates.find((one) => one.version === version);
    if (row === undefined) {
      fail(
        candidates.length > 0
          ? `${called}: DEPENDENCIES.md records it for ${name} only at ` +
              `${candidates.map((one) => one.version).join(", ")}; a row holds ` +
              "for the version it names, so read the advisory again"
          : `${called}: ${title} (${severity}). ` +
              (fix === null
                ? "npm reports no fix that moves forward; DEPENDENCIES.md, " +
                  `"${TABLE_HEADING}", says when such an advisory may be recorded`
                : `${fix}: take it`),
      );
      continue;
    }
    used.add(key(row));
    const before = problems.length;
    if (fix !== null) {
      fail(
        `${called}: DEPENDENCIES.md records it as having no fix, and ${fix}; ` +
          "take the fix and remove the row",
      );
    }
    if (row.review < today) {
      fail(
        `${called}: the row was to be reviewed by ${row.review}, which has ` +
          "passed. Read the advisory again: take the fix if there is one, or " +
          "write the new date and what you found",
      );
    } else if (row.review > later(today, REVIEW_WITHIN_DAYS)) {
      fail(
        `${called}: the row is to be reviewed by ${row.review}, more than ` +
          `${REVIEW_WITHIN_DAYS} days after today, ${today}; a row holds for ` +
          `${REVIEW_WITHIN_DAYS} days at most`,
      );
    }
    if (problems.length === before) {
      excepted.push({ ...finding, review: row.review });
    }
  }

  for (const row of rows) {
    if (!used.has(key(row))) {
      fail(
        `DEPENDENCIES.md records ${row.advisory} for ${row.name} ${row.version}, ` +
          "which npm audit does not report; remove the row",
      );
    }
  }
  return { problems, excepted };
}

function main() {
  /** @type {ReturnType<typeof judge>} */
  let outcome;
  /** @type {number} */
  let pinned;
  try {
    const locked = readLock();
    pinned = pinnedIn(locked);
    const rows = readRows(readPolicy());
    // The report is read, not the exit status: every severity counts.
    const audit = spawnSync("npm", ["audit", "--json"], {
      cwd: FRONTEND,
      encoding: "utf8",
      maxBuffer: 256 * 1024 * 1024,
      stdio: ["ignore", "pipe", "inherit"],
    });
    if (audit.error !== undefined) {
      throw new GateError(
        `npm audit could not be run (${describe(audit.error)})`,
      );
    }
    const report = readReport(audit.stdout, audit.status, pinned);
    outcome = judge({
      findings: findingsIn(report, locked),
      rows,
      today: new Date().toISOString().slice(0, 10),
    });
  } catch (failure) {
    if (failure instanceof GateError) {
      console.error(
        `the advisory gate could not do its work: ${failure.message}`,
      );
      process.exit(2);
    }
    throw failure;
  }
  if (outcome.problems.length > 0) {
    for (const problem of outcome.problems) console.error(`  ${problem}`);
    console.error(
      `\n${outcome.problems.length} problem(s): see DEPENDENCIES.md, ` +
        `"${TABLE_HEADING}", and docs/contributing/js-dependencies.md. ` +
        "`npm audit` prints everything that depends on each.",
    );
    process.exit(1);
  }
  console.log(
    outcome.excepted.length === 0
      ? `All ${pinned} locked packages audited; no known advisories.`
      : `All ${pinned} locked packages audited. No fix exists yet for ` +
          outcome.excepted
            .map(
              ({ advisory, name, version, review }) =>
                `${advisory} in ${name} ${version}, to be reviewed by ${review}`,
            )
            .join("; ") +
          `: recorded in DEPENDENCIES.md, "${TABLE_HEADING}".`,
  );
}

// Run only when run; the tests import the parts above. A gate that fell over
// judged nothing, so it exits 2 rather than letting node print a crash and
// exit 1 -- which a caller would read as "an advisory failed".
const invoked = process.argv[1];
if (
  invoked !== undefined &&
  resolve(invoked) === fileURLToPath(import.meta.url)
) {
  try {
    main();
  } catch (failure) {
    console.error(
      "the advisory gate could not do its work: it fell over.\n" +
        (failure instanceof Error
          ? (failure.stack ?? failure.message)
          : String(failure)),
    );
    process.exit(2);
  }
}
