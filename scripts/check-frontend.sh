#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Every gate the frontend has, in the order that fails fastest: formatting,
# then lint, then types, then tests, then the build with its licence gate, its
# size budget and its bundled-package list, then the two audits, which are the
# only steps that need the network for anything but installing.
#
# Takes no arguments: what it does is the whole of the frontend's CI job, and
# an argument that changed one step would quietly turn it off.
set -eu

if [ "$#" -ne 0 ]; then
    printf '%s takes no arguments (got: %s)\n' "$0" "$*" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/frontend"

# nvm, if this machine has it, so that a laptop with an older Node in its PATH
# runs the version .nvmrc names -- the one CI uses. It is a bash script, and
# this one is not, so the re-exec goes through bash; a machine without either
# carries on with whatever `node` is, and the version check below decides.
#
# Not on CI, whatever the runner has installed. GitHub's runners do ship nvm,
# and setup-node has already put the Node of .nvmrc on the PATH; sourcing nvm
# there would hand the job over to nvm's own default alias instead, which is
# some other version and is not what the cache was keyed on.
if [ -z "${CI:-}" ] && [ -z "${ROBINAUTS_NODE_CHOSEN:-}" ] &&
    [ -s "$HOME/.nvm/nvm.sh" ] && command -v bash >/dev/null 2>&1; then
    ROBINAUTS_NODE_CHOSEN=1
    export ROBINAUTS_NODE_CHOSEN
    # An absolute path, because the re-exec happens from frontend/ and $0 may
    # well be the relative one the caller typed.
    exec bash -c '. "$HOME/.nvm/nvm.sh" >/dev/null 2>&1 && nvm use >/dev/null 2>&1
        exec "$@"' bash "$root/scripts/check-frontend.sh" "$@"
fi

if ! command -v node >/dev/null 2>&1; then
    printf 'no node on the PATH; install the version in frontend/.nvmrc\n' >&2
    exit 2
fi

# .nvmrc may name a whole version, a line, or an alias; only the major is
# compared, and only when it is a number, because "lts/krypton" is not one.
nvmrc=$(cat .nvmrc)
wanted=${nvmrc%%.*}
running=$(node --version | sed 's/^v//; s/\..*//')
case $wanted in
    '' | *[!0-9]*)
        printf 'frontend/.nvmrc says %s, which is not a version to compare\n' \
            "$nvmrc" >&2
        exit 2
        ;;
    *) ;;
esac
if [ "$running" -lt "$wanted" ]; then
    printf 'node %s is older than the %s frontend/.nvmrc asks for\n' \
        "$(node --version)" "$wanted" >&2
    exit 2
fi

# @neorc/ui and @oneberth/ui, packed from the neorc commit that
# backend/pyproject.toml pins; the lock holds the hash of each.
"$root/scripts/ui-packages-from-source.sh"

# --ignore-scripts as well as .npmrc: the file is the rule, this is the
# reminder that no package's own code runs while it is being installed.
npm ci --ignore-scripts

# The licence policy over everything installed, and the exact pins of
# package.json. The bundle has a gate of its own, inside the build below.
node scripts/check-licences.mjs

npm run check-format
npm run lint
npm run typecheck
npm test -- --run

# CHECK_BUNDLED makes the build compare bundled-packages.txt with what it
# would have written instead of rewriting it, so a package that entered the
# bundle fails here rather than being silently recorded. Writing it is what a
# plain `npm run build` does, which is how the line gets into the diff.
CHECK_BUNDLED=1 npm run build

# Advisories for everything installed; signatures and provenance for what
# ships. The registry does not serve attestations for every dev-only package,
# and a 404 there is not a reason to fail.
npm audit --audit-level=low
# The registry has neither @neorc/ui nor @oneberth/ui, which are packed from the
# pinned neorc commit, and `npm audit signatures` stops at a package it cannot
# find there. What they bring from the registry ships, so the signatures are
# read over the shipped tree as the registry knows it: the lock with the two
# tarballs taken out and their dependencies in their place, installed as it pins
# them (scripts/registry-tree.mjs).
tree=$(mktemp -d)
trap 'rm -rf "$tree"' EXIT
trap 'rm -rf "$tree"; exit 130' INT
trap 'rm -rf "$tree"; exit 143' TERM
node scripts/registry-tree.mjs "$tree"
cp .npmrc "$tree/.npmrc"
(cd "$tree" && npm ci --ignore-scripts --omit=dev --no-audit --no-fund &&
    npm audit signatures --omit=dev)
