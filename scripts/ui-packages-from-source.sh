#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# ui-packages-from-source: neorc and oneberth publish no node packages yet, so
# the interface installs @neorc/ui and @oneberth/ui from the neorc commit that
# backend/pyproject.toml pins for the Python packages. That neorc commit pins
# the oneberth commit, so one pin serves the Python packages and the node
# packages of both (oneberth's docs/working-notes/plan.md, "Pins").
#
# It checks the neorc commit out and runs neorc's own scripts/neorc-ui-pack.sh,
# which packs frontend/vendor/neorc-ui-0.0.0.tgz and
# frontend/vendor/oneberth-ui-0.0.0.tgz. frontend/package.json names both, and
# package-lock.json holds the hash of each, so `npm ci` refuses a tarball packed
# from another commit. CI and a developer run this script before `npm ci`; the
# check scripts run it themselves. Nothing of either repository is committed.
#
# It packs again only when the pin moved: frontend/vendor/neorc.rev names the
# commit the tarballs came from. NEORC_REPOSITORY names another clone to fetch
# from, such as a local checkout, and ONEBERTH_REPOSITORY does the same for
# neorc's script. Needs git, Node.js and npm.
set -eu

if [ "$#" -ne 0 ]; then
    printf '%s takes no arguments (got: %s)\n' "$0" "$*" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
pins="$root/backend/pyproject.toml"
repository=$(sed -n 's/^neorc = .*git = "\([^"]*\)".*/\1/p' "$pins")
revision=$(sed -n 's/^neorc = .*rev = "\([0-9a-f]*\)".*/\1/p' "$pins")
if [ -z "$repository" ] || [ -z "$revision" ]; then
    printf 'backend/pyproject.toml pins no commit of neorc\n' >&2
    exit 1
fi
repository=${NEORC_REPOSITORY:-$repository}
vendor="$root/frontend/vendor"
stamp="$vendor/neorc.rev"

if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$revision" ] &&
    [ -f "$vendor/neorc-ui-0.0.0.tgz" ] && [ -f "$vendor/oneberth-ui-0.0.0.tgz" ]; then
    printf '@neorc/ui and @oneberth/ui from neorc %s: already in %s\n' "$revision" "$vendor" >&2
    exit 0
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
trap 'rm -rf "$work"; exit 130' INT
trap 'rm -rf "$work"; exit 143' TERM

git init --quiet "$work/neorc"
git -C "$work/neorc" fetch --quiet --depth 1 "$repository" "$revision"
git -C "$work/neorc" checkout --quiet --detach FETCH_HEAD
fetched=$(git -C "$work/neorc" rev-parse HEAD)
if [ "$fetched" != "$revision" ]; then
    printf 'fetched %s from %s, and backend/pyproject.toml pins %s\n' \
        "$fetched" "$repository" "$revision" >&2
    exit 1
fi

# Packed into a directory of the run's own and moved afterwards, so that a pack
# that fails part way leaves no tarball behind for `npm ci` to refuse.
"$work/neorc/scripts/neorc-ui-pack.sh" "$work/packed" >/dev/null
mkdir -p "$vendor"
rm -f "$stamp"
mv "$work/packed/neorc-ui-0.0.0.tgz" "$work/packed/oneberth-ui-0.0.0.tgz" "$vendor/"
printf '%s\n' "$revision" >"$stamp"
printf '@neorc/ui and @oneberth/ui from neorc %s: %s\n' "$revision" "$vendor" >&2
