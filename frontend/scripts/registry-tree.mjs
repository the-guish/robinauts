// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The shipped tree as the registry knows it, for `npm audit signatures`.
 *
 * `@neorc/ui` and `@oneberth/ui` are tarballs packed from the pinned neorc
 * commit (`scripts/ui-packages-from-source.sh`): the registry has neither, and
 * `npm audit signatures` stops at the first package it cannot find there. What
 * they bring from the registry -- the query library, the two graph libraries
 * and their trees -- ships all the same, and is what the audit is for.
 *
 * So this writes, into the directory it is given, a `package.json` and a
 * `package-lock.json` whose root depends on the runtime dependencies of
 * `frontend/package.json` that come from the registry, and on the registry
 * dependencies of the vendored packages in their place, at the versions the
 * lock pins. The lock is the committed one with the vendored packages taken
 * out; nothing is resolved again. `scripts/check-frontend.sh` installs it with
 * `npm ci --omit=dev` and audits that.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const out = process.argv[2];
if (out === undefined) {
  console.error("usage: node scripts/registry-tree.mjs <directory>");
  process.exit(2);
}

const here = resolve(import.meta.dirname, "..");
const manifest = JSON.parse(readFileSync(join(here, "package.json"), "utf8"));
const lock = JSON.parse(readFileSync(join(here, "package-lock.json"), "utf8"));

const vendored = Object.entries(manifest.dependencies ?? {})
  .filter(([, spec]) => String(spec).startsWith("file:"))
  .map(([name]) => name);

/** @type {Record<string, string>} */
const dependencies = {};
for (const [name, spec] of Object.entries(manifest.dependencies ?? {})) {
  if (!vendored.includes(name)) dependencies[name] = spec;
}
for (const name of vendored) {
  const entry = lock.packages[`node_modules/${name}`];
  if (entry === undefined) {
    console.error(`package-lock.json has no entry for ${name}`);
    process.exit(1);
  }
  for (const needed of Object.keys(entry.dependencies ?? {})) {
    const pinned = lock.packages[`node_modules/${needed}`];
    if (pinned === undefined) {
      console.error(`package-lock.json has no top-level entry for ${needed}`);
      process.exit(1);
    }
    dependencies[needed] ??= pinned.version;
  }
  delete lock.packages[`node_modules/${name}`];
}

const tree = {
  name: "robinauts-frontend-registry-tree",
  private: true,
  version: "0.0.0",
  dependencies,
  overrides: manifest.overrides ?? {},
};
lock.name = tree.name;
const root = lock.packages[""];
root.name = tree.name;
root.dependencies = dependencies;
delete root.devDependencies;

writeFileSync(join(out, "package.json"), `${JSON.stringify(tree, null, 2)}\n`);
writeFileSync(
  join(out, "package-lock.json"),
  `${JSON.stringify(lock, null, 2)}\n`,
);
