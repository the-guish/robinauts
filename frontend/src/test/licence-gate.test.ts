// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The npm licence gate, tested on its own parts.
 *
 * `scripts/check-licences.mjs` passing over this tree proves that this tree
 * passes; it proves nothing about what the gate would refuse. These are the
 * refusals, over fixtures.
 *
 * The list of forbidden identifiers is shared with the Python gate through
 * `scripts/licence-fixtures.json`, which `backend/tests/unit/test_licence_gate.py`
 * reads too: the two gates enforce one policy, so they are held to one list
 * rather than to two that look alike.
 */
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, test } from "vitest";

import {
  ROOT,
  allowedLicences,
  categoryFrom,
  forbiddenIn,
  installed,
  statedClaims,
  isAllowed,
  isUnreadable,
  judge,
  pinProblems,
  readTable,
  readPolicy,
  restrictedIn,
  restrictedLicences,
  statedLicence,
  GateError,
} from "../../scripts/check-licences.mjs";

const fixtures = JSON.parse(
  readFileSync(`${ROOT}/scripts/licence-fixtures.json`, "utf8"),
) as {
  shared: [string, string][];
  npm_only: [string, string][];
  restricted: [string, string][];
};

describe("what the policy forbids outright", () => {
  for (const [identifier, verdict] of [
    ...fixtures.shared,
    ...fixtures.npm_only,
  ]) {
    test(`${identifier} is ${verdict}`, () => {
      // What it names is not the claim here -- `GPL v3` is refused for the
      // `GPL` in it -- only whether anything in it is forbidden.
      const found = forbiddenIn(identifier);
      if (verdict === "forbidden") expect(found.length).toBeGreaterThan(0);
      else expect(found).toEqual([]);
    });
  }

  test("UNLICENSED and Unlicense are one letter and two verdicts apart", () => {
    expect(forbiddenIn("UNLICENSED")).toEqual(["UNLICENSED"]);
    expect(isAllowed("Unlicense")).toBe(true);
  });

  test("a forbidden side of a choice is still forbidden", () => {
    expect(forbiddenIn("MIT OR GPL-3.0-only")).toEqual(["GPL-3.0-only"]);
  });
});

describe("the allowed list, over SPDX expressions", () => {
  test("a choice needs one allowed side", () => {
    expect(isAllowed("MIT OR CC0-1.0")).toBe(true);
    expect(isAllowed("(MIT OR SEE-LICENSE-IN-FILE)")).toBe(true);
    expect(isAllowed("MPL-2.0 OR EPL-2.0")).toBe(false);
  });

  test("a conjunction needs them all", () => {
    expect(isAllowed("MIT AND ISC")).toBe(true);
    expect(isAllowed("(MIT AND CC-BY-3.0)")).toBe(false);
  });

  test("CNRI-Python is allowed, and only under its own identifier", () => {
    // regex's expression is a conjunction of two allowed licences. The
    // GPL-compatible variant is a different SPDX identifier, on no list.
    expect(isAllowed("CNRI-Python")).toBe(true);
    expect(isAllowed("Apache-2.0 AND CNRI-Python")).toBe(true);
    expect(isAllowed("CNRI-Python-GPL-Compatible")).toBe(false);
    expect(isAllowed("Python-2.0")).toBe(false);
  });

  test("an exception makes one thing the list does not name", () => {
    expect(isAllowed("Apache-2.0 WITH LLVM-exception")).toBe(false);
  });

  test("what will not parse is not allowed: the gate fails closed", () => {
    expect(isAllowed("MIT AND")).toBe(false);
    expect(isAllowed("(MIT")).toBe(false);
    expect(isAllowed("")).toBe(false);
  });

  test("the restricted family is recognised as such", () => {
    expect(restrictedIn("MPL-2.0")).toEqual(["MPL-2.0"]);
    expect(restrictedIn("MIT")).toEqual([]);
  });
});

describe("what a package.json states", () => {
  test("a string, an object and the legacy array all read", () => {
    expect(statedClaims({ license: "MIT" })).toEqual(["MIT"]);
    expect(statedClaims({ license: { type: "MIT" } })).toEqual(["MIT"]);
    expect(
      statedClaims({ licenses: [{ type: "MIT" }, { type: "ISC" }] }),
    ).toEqual(["MIT", "ISC"]);
    expect(statedClaims({})).toEqual([]);
    expect(statedLicence({})).toBe("");
  });

  test("an array of plain strings is claims, not silence", () => {
    // npm has published every shape of this field. A package saying
    // `"licenses": ["MIT"]` states MIT; reading that as "no licence at all"
    // would fail it for the one thing an exception may never cover.
    expect(statedClaims({ licenses: ["MIT"] })).toEqual(["MIT"]);
    expect(statedClaims({ license: ["MIT", "ISC"] })).toEqual(["MIT", "ISC"]);
    expect(statedClaims({ licenses: ["MIT", { type: "ISC" }] })).toEqual([
      "MIT",
      "ISC",
    ]);
    expect(isAllowed(statedLicence({ licenses: ["MIT"] }))).toBe(true);
    expect(forbiddenIn(statedLicence({ licenses: ["GPL-3.0"] }))).toEqual([
      "GPL-3.0",
    ]);
  });

  test("every field is read, and the worst of them decides", () => {
    // A package claiming MIT in one field and the GPL in another is a
    // question for a person, not something to settle in our own favour.
    const twoFaced = { license: "MIT", licenses: [{ type: "GPL-3.0" }] };
    expect(statedClaims(twoFaced)).toEqual(["MIT", "GPL-3.0"]);
    expect(statedLicence(twoFaced)).toBe("MIT AND GPL-3.0");
    expect(forbiddenIn(statedLicence(twoFaced))).toEqual(["GPL-3.0"]);

    const problems = problemsOf(
      [{ ...PACKAGE, licence: statedLicence(twoFaced) }],
      [{ ...ROW, licence: "MIT" }],
    );
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatch(/DEPENDENCIES\.md forbids/);

    // Two agreeable claims cost nothing.
    expect(
      isAllowed(statedLicence({ license: "MIT", licenses: [{ type: "ISC" }] })),
    ).toBe(true);
  });
});

const ROW = {
  name: "thing",
  version: "1.0.0",
  scope: "development",
  licence: "BlueOak-1.0.0",
};
const PACKAGE = {
  name: "thing",
  version: "1.0.0",
  licence: "BlueOak-1.0.0",
  development: true,
};
type Installed = typeof PACKAGE;
type Row = typeof ROW;
const problemsOf = (packages: Installed[], rows: Row[]) =>
  judge({ packages, rows }).problems;

describe("the exceptions table, against the tree", () => {
  test("a row that matches answers for the package", () => {
    expect(problemsOf([PACKAGE], [ROW])).toEqual([]);
  });

  test("no row at all", () => {
    expect(problemsOf([PACKAGE], [])[0]).toMatch(/has no row/);
  });

  test("a row for another version does not answer for this one", () => {
    expect(problemsOf([PACKAGE], [{ ...ROW, version: "2.0.0" }])[0]).toMatch(
      /excepts thing only at 2\.0\.0/,
    );
  });

  test("two versions of one package each need their own row", () => {
    const other = { ...PACKAGE, version: "2.0.0" };
    expect(
      problemsOf([PACKAGE, other], [ROW, { ...ROW, version: "2.0.0" }]),
    ).toEqual([]);
    expect(problemsOf([PACKAGE, other], [ROW])[0]).toMatch(/thing 2\.0\.0/);
  });

  test("a row naming another licence fails: a licence is re-read on a bump", () => {
    expect(problemsOf([PACKAGE], [{ ...ROW, licence: "MIT-0" }])[0]).toMatch(
      /a licence is re-checked on every bump/,
    );
  });

  test("a development row for something the runtime reaches fails", () => {
    expect(problemsOf([{ ...PACKAGE, development: false }], [ROW])[0]).toMatch(
      /the runtime dependencies reach it/,
    );
  });

  test("a restricted licence may only be carried by a development row", () => {
    const shipped = { ...PACKAGE, licence: "MPL-2.0", development: false };
    const problems = problemsOf(
      [shipped],
      [{ ...ROW, licence: "MPL-2.0", scope: "runtime" }],
    );
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatch(/cannot carry it/);

    // The same package with the honest row: the scope check catches it first.
    expect(problemsOf([shipped], [{ ...ROW, licence: "MPL-2.0" }])[0]).toMatch(
      /the runtime dependencies reach it/,
    );
    // And development-only, as lightningcss really is, it passes.
    expect(
      problemsOf(
        [{ ...shipped, development: true }],
        [{ ...ROW, licence: "MPL-2.0" }],
      ),
    ).toEqual([]);
  });

  test("a forbidden licence is refused whatever the row says", () => {
    const forbidden = { ...PACKAGE, licence: "LGPL-3.0-only" };
    const problems = problemsOf(
      [forbidden],
      [{ ...ROW, licence: "LGPL-3.0-only" }],
    );
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatch(/DEPENDENCIES\.md forbids/);
  });

  test("a stale row fails", () => {
    const allowed = { ...PACKAGE, licence: "MIT" };
    expect(problemsOf([allowed], [{ ...ROW, licence: "MIT" }])[0]).toMatch(
      /needs no exception; remove the row/,
    );
  });
});

describe("the shape of the table", () => {
  const table = (body: string) => () =>
    readTable(`## ${"JavaScript build tooling"}\n\n${body}\n`);

  test("the real document parses, and every row is development", () => {
    const rows = readTable(readFileSync(`${ROOT}/DEPENDENCIES.md`, "utf8")) as {
      scope: string;
    }[];
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((row) => row.scope === "development")).toBe(true);
  });

  test("a renamed heading is the gate's own error, not a crash", () => {
    expect(() => readTable("## Something else\n\n| a |\n|---|\n")).toThrow(
      GateError,
    );
  });

  test("a missing column, a missing table and a bad scope all say so", () => {
    expect(table("| package | version | licence |\n|---|---|---|\n")).toThrow(
      /no "scope" column/,
    );
    expect(table("nothing here")).toThrow(/has no table/);
    expect(
      table(
        "| package | version | scope | licence |\n|---|---|---|---|\n" +
          "| `thing` | 1.0.0 | someday | MIT |\n",
      ),
    ).toThrow(/is not one of/);
    expect(
      table(
        "| package | version | scope | licence |\n|---|---|---|---|\n" +
          "| thing | 1.0.0 | development | MIT |\n",
      ),
    ).toThrow(/names no package in backticks/);
  });
});

describe("exact pins", () => {
  test("a range, a wildcard and a choice are all refused", () => {
    expect(pinProblems({ dependencies: { a: "1.0.0" } })).toEqual([]);
    expect(pinProblems({ dependencies: { a: "^1.0.0" } })).toHaveLength(1);
    expect(pinProblems({ devDependencies: { a: "*" } })).toHaveLength(1);
    expect(pinProblems({ peerDependencies: { a: ">=1" } })).toHaveLength(1);
    expect(pinProblems({ optionalDependencies: { a: "1.x" } })).toHaveLength(1);
    expect(pinProblems({ overrides: { a: { b: "~1.0.0" } } })[0]).toMatch(
      /overrides\.a\.b/,
    );
  });
});

describe("the lists come from the document, not from a second copy", () => {
  // Written out by hand, on purpose. A second parser beside the gate's would
  // agree with it for the same wrong reason; a list somebody typed out of
  // DEPENDENCIES.md disagrees the moment the document is shortened, which is
  // the failure worth catching.
  test("the allowed list is DEPENDENCIES.md's, in full", () => {
    expect([...allowedLicences()].sort()).toEqual([
      "0bsd",
      "apache-2.0",
      "bsd-2-clause",
      "bsd-3-clause",
      "cc0-1.0",
      "cnri-python",
      "isc",
      "mit",
      "mit-0",
      "postgresql",
      "psf-2.0",
      "unlicense",
      "zlib",
    ]);
  });

  test("the restricted list is DEPENDENCIES.md's, CDDL versions and all", () => {
    expect([...restrictedLicences()].sort()).toEqual([
      "cddl-1.0",
      "cddl-1.1",
      "epl-2.0",
      "mpl-2.0",
    ]);
  });

  test("the policy is read when it is asked for, not when the gate loads", () => {
    // Work that can fail must not happen while a module is being imported:
    // there it is a stack and exit 1, which reads as "a dependency failed".
    expect(() => readPolicy("/nowhere/DEPENDENCIES.md")).toThrow(GateError);
    expect(() => readPolicy("/nowhere/DEPENDENCIES.md")).toThrow(
      /could not be read/,
    );
    expect(() => categoryFrom("# Other\n\nMIT.\n", "Allowed")).toThrow(
      GateError,
    );
  });

  test("a category that contradicted the forbidden list stops the gate", () => {
    expect(() =>
      categoryFrom(
        "### Allowed\n\nMIT, GPL-3.0-only.\n\n### Next\n",
        "Allowed",
      ),
    ).toThrow(/cannot both be true/);
    expect(() =>
      categoryFrom("### Allowed\n\n\n### Next\n", "Allowed"),
    ).toThrow(GateError);
  });
});

describe("the restricted family, however it is spelt", () => {
  for (const [claim] of fixtures.restricted) {
    test(`${claim} is restricted`, () => {
      expect(restrictedIn(claim).length).toBeGreaterThan(0);
    });
  }

  test("and a permissive licence is not", () => {
    expect(restrictedIn("MIT")).toEqual([]);
    expect(restrictedIn("Apache-2.0")).toEqual([]);
  });

  test("so a runtime row cannot carry one, whatever it is called", () => {
    const shipped = {
      ...PACKAGE,
      licence: "Mozilla Public License 2.0",
      development: false,
    };
    const problems = problemsOf(
      [shipped],
      [{ ...ROW, licence: "Mozilla Public License 2.0", scope: "runtime" }],
    );
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatch(/cannot carry it/);
  });
});

describe("claims nobody can look up", () => {
  test("they are refused, and no row may cover them", () => {
    expect(isUnreadable("SEE LICENSE IN LICENSE.txt")).toBe(true);
    expect(isUnreadable("SEE LICENCE IN COPYING")).toBe(true);
    expect(isUnreadable("NOASSERTION")).toBe(true);
    expect(isUnreadable("MIT*")).toBe(true);
    expect(isUnreadable("MIT")).toBe(false);
    // One unreadable side of a choice makes the claim unreadable: picking the
    // readable side would be settling it in our own favour.
    expect(isUnreadable("MIT OR NOASSERTION")).toBe(true);
    expect(isUnreadable("(MIT AND UNKNOWN)")).toBe(true);
    expect(isUnreadable("MIT OR ISC")).toBe(false);

    const vague = { ...PACKAGE, licence: "NOASSERTION" };
    const problems = problemsOf([vague], [{ ...ROW, licence: "NOASSERTION" }]);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatch(/names no licence anybody can look up/);
  });
});

describe("rows the tree has outgrown", () => {
  test("a row for something the lockfile no longer pins fails", () => {
    const pinned = new Set([`${PACKAGE.name}@${PACKAGE.version}`]);
    const gone = { ...ROW, name: "left", version: "9.9.9" };
    const { problems } = judge({
      packages: [PACKAGE],
      rows: [ROW, gone],
      pinned,
    });
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatch(
      /excepts left 9\.9\.9, which the lockfile does not pin at any version/,
    );
  });

  test("a row for a version the lockfile still pins elsewhere is left alone", () => {
    // Another platform's binary: pinned, not installed here, and its row is
    // read on the machine that does install it.
    const pinned = new Set([
      `${PACKAGE.name}@${PACKAGE.version}`,
      "thing-win32@1.0.0",
    ]);
    const other = { ...ROW, name: "thing-win32" };
    expect(
      judge({ packages: [PACKAGE], rows: [ROW, other], pinned }).problems,
    ).toEqual([]);
  });
});

describe("reading the tree", () => {
  const scratches: string[] = [];
  const scratch = () => {
    const where = mkdtempSync(join(tmpdir(), "robinauts-gate-"));
    scratches.push(where);
    return where;
  };
  afterEach(() => {
    for (const where of scratches.splice(0))
      rmSync(where, { recursive: true, force: true });
  });

  /** A tree with one package, written to disk. */
  function tree(lock: unknown, packages: Record<string, unknown> = {}) {
    const where = scratch();
    writeFileSync(join(where, "package-lock.json"), JSON.stringify(lock));
    for (const [path, own] of Object.entries(packages)) {
      mkdirSync(join(where, path), { recursive: true });
      writeFileSync(join(where, path, "package.json"), JSON.stringify(own));
    }
    return where;
  }

  test("a lockfile that is missing or will not parse stops the gate", () => {
    expect(() => installed(scratch())).toThrow(GateError);
    const where = scratch();
    writeFileSync(join(where, "package-lock.json"), "{ not json");
    expect(() => installed(where)).toThrow(
      /package-lock\.json could not be read/,
    );
  });

  test("a package.json that will not parse stops the gate", () => {
    const where = tree({
      packages: { "": {}, "node_modules/a": { version: "1.0.0" } },
    });
    mkdirSync(join(where, "node_modules/a"), { recursive: true });
    writeFileSync(join(where, "node_modules/a/package.json"), "{ not json");
    expect(() => installed(where)).toThrow(/cannot read/);
  });

  test("a locked package that is simply not installed stops the gate", () => {
    const where = tree({
      packages: { "": {}, "node_modules/a": { version: "1.0.0" } },
    });
    expect(() => installed(where)).toThrow(/is not installed; run `npm ci`/);
  });

  test("an optional package for another platform is counted, not a failure", () => {
    const where = tree({
      packages: {
        "": {},
        "node_modules/a": { version: "1.0.0" },
        "node_modules/b-win32": { version: "1.0.0", optional: true },
        "node_modules/c-win32": { version: "1.0.0", devOptional: true },
      },
    });
    mkdirSync(join(where, "node_modules/a"), { recursive: true });
    writeFileSync(
      join(where, "node_modules/a/package.json"),
      JSON.stringify({ name: "a", version: "1.0.0", license: "MIT" }),
    );

    const { packages, elsewhere, pinned } = installed(where);
    expect(packages.map((one) => one.name)).toEqual(["a"]);
    // devOptional means npm may leave it out just as optional does.
    expect(elsewhere).toEqual(["b-win32 1.0.0", "c-win32 1.0.0"]);
    expect(pinned.has("c-win32@1.0.0")).toBe(true);
  });
});
