// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * What `scripts/check-advisories.mjs` refuses, over fixtures: nothing here
 * asks the registry anything. The last part runs the script against stand-in
 * `npm` commands that fail, as `backend/tests/tooling/test_check_scripts.py`
 * does for the Python audit.
 */
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, test } from "vitest";

import {
  REVIEW_WITHIN_DAYS,
  TABLE_HEADING,
  findingsIn,
  forwardFix,
  isNewer,
  judge,
  pinnedIn,
  readLock,
  readReport,
  readRows,
} from "../../scripts/check-advisories.mjs";
import { FRONTEND, GateError, ROOT } from "../../scripts/check-licences.mjs";

const ADVISORY = "GHSA-aaaa-bbbb-cccc";
const TODAY = "2026-11-18";

/** The lockfile's packages: a matcher three tools bring, and one of them. */
const LOCKED = {
  "": { version: "0.0.0" },
  "node_modules/matcher": { version: "4.0.7" },
  "node_modules/builder": { version: "8.3.5" },
};

/** What npm says of an advisory nobody has fixed: a move back, or nothing. */
const MOVE_BACK = { name: "builder", version: "6.2.7", isSemVerMajor: true };

/** `npm audit --json`, for one advisory in `matcher` and what depends on it. */
const report = (fixAvailable: unknown = MOVE_BACK) => ({
  vulnerabilities: {
    matcher: {
      name: "matcher",
      severity: "high",
      via: [
        {
          source: 1,
          name: "matcher",
          title: "matcher is slow on purpose-built patterns",
          url: `https://github.com/advisories/${ADVISORY}`,
          severity: "high",
          range: "<=4.0.7",
        },
      ],
      effects: ["builder"],
      nodes: ["node_modules/matcher"],
      fixAvailable,
    },
    builder: {
      name: "builder",
      severity: "high",
      via: ["matcher"],
      effects: [],
      nodes: ["node_modules/builder"],
      fixAvailable,
    },
  },
});

const FINDING = {
  advisory: ADVISORY,
  name: "matcher",
  version: "4.0.7",
  title: "matcher is slow on purpose-built patterns",
  severity: "high",
  fix: null as string | null,
};
const ROW = {
  advisory: ADVISORY,
  name: "matcher",
  version: "4.0.7",
  review: "2026-11-25",
};
type Finding = typeof FINDING;
type Row = typeof ROW;
const problemsOf = (findings: Finding[], rows: Row[], today = TODAY) =>
  judge({ findings, rows, today }).problems;

describe("what npm reported, as advisories", () => {
  test("one advisory is one finding, however many packages depend on it", () => {
    // npm counts two "vulnerabilities" here, and thirteen for picomatch.
    expect(findingsIn(report(), LOCKED)).toEqual([FINDING]);
  });

  test("a package pinned at two versions is held at both", () => {
    const locked = {
      ...LOCKED,
      "node_modules/builder/node_modules/matcher": { version: "3.0.1" },
    };
    const twice = report();
    twice.vulnerabilities.matcher.nodes.push(
      "node_modules/builder/node_modules/matcher",
    );
    expect(findingsIn(twice, locked).map((one) => one.version)).toEqual([
      "4.0.7",
      "3.0.1",
    ]);
  });

  test("two copies at one version are one finding", () => {
    const locked = {
      ...LOCKED,
      "node_modules/builder/node_modules/matcher": { version: "4.0.7" },
    };
    const twice = report();
    twice.vulnerabilities.matcher.nodes.push(
      "node_modules/builder/node_modules/matcher",
    );
    expect(findingsIn(twice, locked)).toEqual([FINDING]);
  });

  test("a package the runtime dependencies reach is judged like any other", () => {
    // The lockfile's `dev` mark changes nothing: one rule for what ships and
    // for build tooling.
    const development = {
      ...LOCKED,
      "node_modules/matcher": { version: "4.0.7", dev: true },
    };
    expect(findingsIn(report(), development)).toEqual(
      findingsIn(report(), LOCKED),
    );
  });

  test("an advisory with no identifier is still an advisory", () => {
    const unnamed = report();
    for (const advisory of unnamed.vulnerabilities.matcher.via) {
      advisory.url = "https://example.test/1";
    }
    const findings = findingsIn(unnamed, LOCKED);
    expect(findings).toHaveLength(1);
    // No row can name it, so no row can except it.
    expect(problemsOf(findings, [ROW])[0]).toMatch(
      /example\.test\/1, in matcher 4\.0\.7/,
    );
  });

  test("a place in the tree the lockfile does not pin stops the gate", () => {
    const elsewhere = report();
    elsewhere.vulnerabilities.matcher.nodes = ["node_modules/elsewhere"];
    expect(() => findingsIn(elsewhere, LOCKED)).toThrow(GateError);
    expect(() => findingsIn(elsewhere, LOCKED)).toThrow(
      /the lockfile does not pin/,
    );
  });

  test("vulnerable packages with no advisory behind them stop the gate", () => {
    const { builder } = report().vulnerabilities;
    expect(() => findingsIn({ vulnerabilities: { builder } }, LOCKED)).toThrow(
      /no advisory behind them/,
    );
  });
});

describe("a fix that moves forward", () => {
  test("one that `npm audit fix` would take is a fix", () => {
    expect(forwardFix(true, LOCKED)).toMatch(/within the versions already/);
  });

  test("none at all is none", () => {
    expect(forwardFix(false, LOCKED)).toBeNull();
    expect(forwardFix(undefined, LOCKED)).toBeNull();
  });

  test("a move back is not a fix", () => {
    // What `npm audit fix --force` offers while no release has the fix: an
    // older release of our own dependency, from before it declared this one.
    expect(forwardFix(MOVE_BACK, LOCKED)).toBeNull();
    expect(
      forwardFix({ name: "builder", version: "8.3.5" }, LOCKED),
    ).toBeNull();
  });

  test("a newer release of our own dependency is", () => {
    expect(forwardFix({ name: "builder", version: "8.3.6" }, LOCKED)).toMatch(
      /builder 8\.3\.6 is clear of it, which is newer than the 8\.3\.5/,
    );
    expect(
      forwardFix({ name: "builder", version: "10.0.0" }, LOCKED),
    ).not.toBeNull();
  });

  test("what cannot be read is a reason to look", () => {
    expect(forwardFix({ name: "builder" }, LOCKED)).toMatch(/cannot read/);
    expect(
      forwardFix({ name: "stranger", version: "1.0.0" }, LOCKED),
    ).not.toBeNull();
    expect(isNewer("next", "8.3.5")).toBe(true);
  });

  test("versions are compared as numbers, not as text", () => {
    expect(isNewer("8.10.0", "8.9.0")).toBe(true);
    expect(isNewer("8.9.0", "8.10.0")).toBe(false);
    expect(isNewer("8.3.5", "8.3.5")).toBe(false);
  });
});

describe("the table, against what npm reported", () => {
  test("an advisory with no row fails, as it always did", () => {
    const [problem] = problemsOf([FINDING], []);
    expect(problem).toMatch(/GHSA-aaaa-bbbb-cccc, in matcher 4\.0\.7/);
    expect(problem).toMatch(/npm reports no fix that moves forward/);
  });

  test("and with a fix, it says to take the fix", () => {
    const fixed = { ...FINDING, fix: "npm reports a fix" };
    expect(problemsOf([fixed], [])[0]).toMatch(/npm reports a fix: take it/);
  });

  test("a row that matches answers for the advisory", () => {
    const outcome = judge({ findings: [FINDING], rows: [ROW], today: TODAY });
    expect(outcome.problems).toEqual([]);
    expect(outcome.excepted).toEqual([{ ...FINDING, review: ROW.review }]);
  });

  test("a row for another version does not answer for this one", () => {
    const problems = problemsOf([FINDING], [{ ...ROW, version: "4.0.6" }]);
    expect(problems[0]).toMatch(/records it for matcher only at 4\.0\.6/);
  });

  test("a row for another advisory does not answer for this one", () => {
    const other = { ...ROW, advisory: "GHSA-xxxx-yyyy-zzzz" };
    const problems = problemsOf([FINDING], [other]);
    expect(problems).toHaveLength(2);
    expect(problems[0]).toMatch(/GHSA-aaaa-bbbb-cccc, in matcher/);
    expect(problems[1]).toMatch(/GHSA-xxxx-yyyy-zzzz.*remove the row/);
  });

  test("an exception ends the day a fix can be reached", () => {
    const fixed = { ...FINDING, fix: "npm reports a fix" };
    expect(problemsOf([fixed], [ROW])[0]).toMatch(
      /records it as having no fix, and npm reports a fix; take the fix and remove the row/,
    );
  });

  test("a review date that has passed fails", () => {
    expect(problemsOf([FINDING], [ROW], "2026-11-26")[0]).toMatch(
      /was to be reviewed by 2026-11-25, which has passed/,
    );
    // The day itself is still inside it.
    expect(problemsOf([FINDING], [ROW], "2026-11-25")).toEqual([]);
  });

  test("a review date too far away is not a review date", () => {
    expect(REVIEW_WITHIN_DAYS).toBe(7);
    const row = (review: string) => [{ ...ROW, review }];
    expect(problemsOf([FINDING], row("2026-11-25"))).toEqual([]);
    expect(problemsOf([FINDING], row("2026-11-26"))[0]).toMatch(
      /more than 7 days after today, 2026-11-18/,
    );
    expect(problemsOf([FINDING], row("2099-01-01"))).toHaveLength(1);
  });

  test("a row for an advisory npm no longer reports fails", () => {
    expect(problemsOf([], [ROW])[0]).toMatch(
      /which npm audit does not report; remove the row/,
    );
  });

  test("two versions of one package each need their own row", () => {
    const other = { ...FINDING, version: "3.0.1" };
    expect(
      problemsOf([FINDING, other], [ROW, { ...ROW, version: "3.0.1" }]),
    ).toEqual([]);
    expect(problemsOf([FINDING, other], [ROW])[0]).toMatch(/matcher 3\.0\.1/);
  });
});

describe("the shape of the table", () => {
  const HEADER =
    "| advisory | package | version | review by | why it is accepted |\n" +
    "|---|---|---|---|---|\n";
  const table = (body: string) => () =>
    readRows(`## ${TABLE_HEADING}\n\n${body}\n`);

  test("a row reads, and one row may name several packages", () => {
    expect(
      table(
        HEADER +
          `| ${ADVISORY} | \`matcher\`, \`matcher-linux-x64\` | 4.0.7 | 2026-11-25 | read |\n`,
      )(),
    ).toEqual([ROW, { ...ROW, name: "matcher-linux-x64" }]);
  });

  test("an empty table is a table", () => {
    expect(table(HEADER)()).toEqual([]);
  });

  test("the real document parses, and names what the lockfile pins", () => {
    // The registry's answer and today's date are the gate's to check.
    const rows = readRows(readFileSync(`${ROOT}/DEPENDENCIES.md`, "utf8"));
    const locked = Object.entries(readLock());
    for (const row of rows) {
      const copies = locked.filter(
        ([where, entry]) =>
          where.endsWith(`node_modules/${row.name}`) &&
          entry.version === row.version,
      );
      expect(copies.length, `${row.name} ${row.version}`).toBeGreaterThan(0);
    }
  });

  test("a renamed heading is the gate's own error, not a crash", () => {
    expect(() => readRows("## Something else\n\n| a |\n|---|\n")).toThrow(
      GateError,
    );
  });

  test("a missing column and a missing table both say so", () => {
    expect(
      table(
        "| advisory | package | version | review by |\n|---|---|---|---|\n",
      ),
    ).toThrow(/no "why it is accepted" column/);
    expect(table("nothing here")).toThrow(/has no table/);
  });

  test("a row that cannot be read stops the gate", () => {
    const row = (cells: string) => table(`${HEADER}| ${cells} |\n`);
    expect(row("CVE-2026-1 | `matcher` | 4.0.7 | 2026-11-25 | read")).toThrow(
      /not a GHSA identifier/,
    );
    expect(row(`${ADVISORY} | matcher | 4.0.7 | 2026-11-25 | read`)).toThrow(
      /names no package in backticks/,
    );
    expect(
      row(`${ADVISORY} | \`matcher\` | ^4.0.7 | 2026-11-25 | read`),
    ).toThrow(/not an exact version/);
    expect(row(`${ADVISORY} | \`matcher\` | 4.0.7 | soon | read`)).toThrow(
      /not a day written YYYY-MM-DD/,
    );
    // The thirtieth of February parses, as the second of March.
    expect(
      row(`${ADVISORY} | \`matcher\` | 4.0.7 | 2026-02-30 | read`),
    ).toThrow(/not a day written YYYY-MM-DD/);
    // An exception is a signature, not a switch.
    expect(row(`${ADVISORY} | \`matcher\` | 4.0.7 | 2026-11-25 | `)).toThrow(
      /does not say why/,
    );
  });
});

describe("what the lockfile pins", () => {
  test("everything but the project itself and a link to a directory of ours", () => {
    expect(pinnedIn(LOCKED)).toBe(2);
    expect(pinnedIn({ ...LOCKED, "node_modules/ours": { link: true } })).toBe(
      2,
    );
    expect(pinnedIn({ "": {} })).toBe(0);
  });
});

describe("an audit that did not happen", () => {
  const printed = (total: number, vulnerabilities = {}) =>
    JSON.stringify({
      auditReportVersion: 2,
      vulnerabilities,
      metadata: { dependencies: { total } },
    });

  test("a clean report on everything the lock pins is an audit", () => {
    expect(readReport(printed(2), 0, 2)).toEqual({ vulnerabilities: {} });
    const found = report().vulnerabilities;
    expect(readReport(printed(2, found), 1, 2).vulnerabilities).toEqual(found);
  });

  test("nothing to read is not a clean report", () => {
    expect(() => readReport("", 1, 2)).toThrow(GateError);
    expect(() => readReport("npm error code ENOTFOUND", 1, 2)).toThrow(
      /produced no report to read \(it exited 1\)/,
    );
  });

  test("an error where the report should be says what npm said", () => {
    const failed = JSON.stringify({
      error: { code: "ENOAUDIT", summary: "the registry did not answer" },
    });
    expect(() => readReport(failed, 1, 2)).toThrow(
      /something other than a version 2 report \(the registry did not answer\)/,
    );
    // The registry not answering at all, as npm 11 prints it.
    const refused = JSON.stringify({
      message: "request to the registry failed",
      error: { summary: "", detail: "" },
    });
    expect(() => readReport(refused, 1, 2)).toThrow(
      /\(request to the registry failed\), and exited 1/,
    );
    expect(() => readReport("null", 0, 2)).toThrow(GateError);
    expect(() => readReport("[]", 0, 2)).toThrow(GateError);
  });

  test("a report on fewer packages than the lock pins is not an audit", () => {
    expect(() => readReport(printed(1), 0, 2)).toThrow(
      /looked at 1 of the 2 packages the lock pins/,
    );
    expect(() => readReport(printed(0), 0, 0)).toThrow(/pins no package/);
    expect(() =>
      readReport(
        JSON.stringify({ auditReportVersion: 2, vulnerabilities: {} }),
        0,
        2,
      ),
    ).toThrow(/looked at undefined of the 2/);
  });

  test("a failure that names no advisory is not a pass", () => {
    expect(() => readReport(printed(2), 1, 2)).toThrow(
      /named no advisory and exited 1/,
    );
  });
});

describe("the script, when npm lets it down", () => {
  const SCRIPT = join(FRONTEND, "scripts", "check-advisories.mjs");
  const made: string[] = [];
  afterEach(() => {
    for (const directory of made.splice(0)) {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  /** Runs the gate with `npm` replaced by a script of ours, or by nothing. */
  const run = (npm: string | null) => {
    const directory = mkdtempSync(join(tmpdir(), "advisory-gate-"));
    made.push(directory);
    if (npm !== null) {
      writeFileSync(join(directory, "npm"), `#!/bin/sh\n${npm}\n`);
      chmodSync(join(directory, "npm"), 0o755);
    }
    return spawnSync(process.execPath, [SCRIPT], {
      encoding: "utf8",
      // Nothing but the stand-in: the real npm must not be found behind it.
      env: { PATH: directory },
    });
  };

  test("an npm that fails is not an empty audit", () => {
    const result = run('echo "npm error network" >&2\nexit 1');
    expect(result.status).toBe(2);
    expect(result.stderr).toMatch(/could not do its work.*no report to read/);
    expect(result.stdout).not.toMatch(/audited/);
  });

  test("an npm that answers for too little is not an audit", () => {
    const result = run(
      `echo '{"auditReportVersion": 2, "vulnerabilities": {}, ` +
        `"metadata": {"dependencies": {"total": 1}}}'`,
    );
    expect(result.status).toBe(2);
    expect(result.stderr).toMatch(/looked at 1 of the \d+ packages/);
    expect(result.stdout).not.toMatch(/audited/);
  });

  test("no npm at all is not an audit either", () => {
    const result = run(null);
    expect(result.status).toBe(2);
    expect(result.stderr).toMatch(/npm audit could not be run/);
  });
});
