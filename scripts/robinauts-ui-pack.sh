#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# robinauts-ui-pack: packs Robinauts' interface as the node package
# @robinauts/ui, for a host with applications of its own (docs/specs/frontend.md,
# "The interface as a package"), beside the tarballs of its two peers.
#
#     scripts/robinauts-ui-pack.sh <output-directory>
#
# The package is frontend/src without its tests and the application's entry,
# with the API's types generated from backend/openapi.json. Its manifest is
# frontend/package/package.json: the application's own package.json is not in
# it. Nothing is published. The script prints the path of each tarball:
# robinauts-ui-0.0.0.tgz, neorc-ui-0.0.0.tgz and oneberth-ui-0.0.0.tgz.
#
# It looks inside what it packed, and fails on a package that lacks an entry
# or holds a test. Needs git, Node.js and npm.
set -eu

if [ "$#" -ne 1 ]; then
    printf 'usage: %s <output-directory>\n' "$0" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$1"
out=$(CDPATH= cd -- "$1" && pwd)

"$root/scripts/ui-packages-from-source.sh"
cd "$root/frontend"
npm ci --ignore-scripts --silent >&2
npm run --silent generate >&2

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
trap 'rm -rf "$stage"; exit 130' INT
trap 'rm -rf "$stage"; exit 143' TERM

cp package/package.json package/README.md "$root/LICENSE" "$stage/"
cp -R src "$stage/src"
(cd "$stage" && npm pack --ignore-scripts --silent --pack-destination "$out" >/dev/null)
cp vendor/neorc-ui-0.0.0.tgz vendor/oneberth-ui-0.0.0.tgz "$out/"

packed="$out/robinauts-ui-0.0.0.tgz"
listing=$(tar -tzf "$packed")
for wanted in package.json LICENSE src/apps.tsx src/styles.css src/RobinautsApp.tsx \
    src/api/schema.d.ts; do
    if ! printf '%s\n' "$listing" | grep -qxF "package/$wanted"; then
        printf '%s lacks %s\n' "$packed" "$wanted" >&2
        exit 1
    fi
done
if printf '%s\n' "$listing" |
    grep -E '\.test\.tsx?$|^package/src/test/|^package/src/main\.tsx$' >&2; then
    printf '%s holds the files above, which are the application'"'"'s alone\n' "$packed" >&2
    exit 1
fi

for tarball in "$packed" "$out/neorc-ui-0.0.0.tgz" "$out/oneberth-ui-0.0.0.tgz"; do
    printf '%s\n' "$tarball"
done
