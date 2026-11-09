#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# neorc-from-source: neorc publishes no wheels yet, so a release of Robinauts
# builds them. `git grep neorc-from-source` lists every part of this step, and
# docs/architecture/orchestration.md says how to take it out.
#
# It builds neorc, neorc-core and neorc-ui from the commit that
# backend/pyproject.toml pins, into the directory it is given, and refuses a
# commit outside neorc's main. It prints one hashed requirement per wheel.
# Building neorc-ui needs Node.js.
set -eu

if [ "$#" -ne 1 ]; then
    printf 'usage: %s <output-directory>\n' "$0" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repository=https://github.com/open-shipyard/neorc
revision=$(sed -n 's/^neorc = .*rev = "\([0-9a-f]*\)".*/\1/p' "$root/backend/pyproject.toml")
if [ -z "$revision" ]; then
    printf 'backend/pyproject.toml pins no commit of neorc\n' >&2
    exit 1
fi

rm -rf "$1"
mkdir -p "$1"
out=$(CDPATH= cd -- "$1" && pwd)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
trap 'rm -rf "$work"; exit 130' INT
trap 'rm -rf "$work"; exit 143' TERM

# The whole history: the packages take their version from it.
git clone --quiet "$repository" "$work/neorc"
cd "$work/neorc"
if ! git merge-base --is-ancestor "$revision" origin/main 2>/dev/null; then
    printf 'neorc commit %s is not on the main branch of %s\n' "$revision" "$repository" >&2
    exit 1
fi
git checkout --quiet --detach "$revision"

for package in neorc-core neorc-ui neorc; do
    uv build --quiet --package "$package" --wheel --out-dir "$out" >&2
done

for wheel in "$out"/*.whl; do
    file=$(basename "$wheel")
    name=$(printf '%s' "${file%%-*}" | tr _ -)
    rest=${file#*-}
    digest=$(sha256sum "$wheel" | cut -d ' ' -f 1)
    printf '%s==%s --hash=sha256:%s\n' "$name" "${rest%%-*}" "$digest"
done
