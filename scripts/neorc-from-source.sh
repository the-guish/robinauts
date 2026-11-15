#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# neorc-from-source: neorc and oneberth publish no wheels yet, so a release of
# Robinauts builds them. `git grep neorc-from-source` lists every part of this
# step, and docs/architecture/orchestration.md says how to take it out.
#
# It builds neorc and neorc-core, and oneberth, from the commits that
# backend/pyproject.toml pins, into the directory it is given, and refuses a
# commit outside the main branch of its repository. It prints one hashed
# requirement per wheel. neorc's interface reaches Robinauts' as a node package
# (scripts/ui-packages-from-source.sh), so its Python wheel is not built.
set -eu

if [ "$#" -ne 1 ]; then
    printf 'usage: %s <output-directory>\n' "$0" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repository=https://github.com/the-guish/neorc
revision=$(sed -n 's/^neorc = .*rev = "\([0-9a-f]*\)".*/\1/p' "$root/backend/pyproject.toml")
if [ -z "$revision" ]; then
    printf 'backend/pyproject.toml pins no commit of neorc\n' >&2
    exit 1
fi
oneberth_repository=$(sed -n 's/^oneberth = .*git = "\([^"]*\)".*/\1/p' "$root/backend/pyproject.toml")
oneberth_revision=$(sed -n 's/^oneberth = .*rev = "\([0-9a-f]*\)".*/\1/p' "$root/backend/pyproject.toml")
if [ -z "$oneberth_repository" ] || [ -z "$oneberth_revision" ]; then
    printf 'backend/pyproject.toml pins no commit of oneberth\n' >&2
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

for package in neorc-core neorc; do
    uv build --quiet --package "$package" --wheel --out-dir "$out" >&2
done

git clone --quiet "$oneberth_repository" "$work/oneberth"
cd "$work/oneberth"
if ! git merge-base --is-ancestor "$oneberth_revision" origin/main 2>/dev/null; then
    printf 'oneberth commit %s is not on the main branch of %s\n' \
        "$oneberth_revision" "$oneberth_repository" >&2
    exit 1
fi
git checkout --quiet --detach "$oneberth_revision"
uv build --quiet --wheel --out-dir "$out" python/oneberth >&2

for wheel in "$out"/*.whl; do
    file=$(basename "$wheel")
    name=$(printf '%s' "${file%%-*}" | tr _ -)
    rest=${file#*-}
    digest=$(sha256sum "$wheel" | cut -d ' ' -f 1)
    printf '%s==%s --hash=sha256:%s\n' "$name" "${rest%%-*}" "$digest"
done
