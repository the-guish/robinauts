#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The gates of channels/, the bridge to chat platforms: formatting, types and
# tests, then the audits. The bridge is a container of its own and ships no
# bundle, so there is no build to check.
#
# Takes no arguments: what it does is the whole of the channels CI job.
set -eu

if [ "$#" -ne 0 ]; then
    printf '%s takes no arguments (got: %s)\n' "$0" "$*" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/channels"

if ! command -v node >/dev/null 2>&1; then
    printf 'no node on the PATH; install the version in channels/.nvmrc\n' >&2
    exit 2
fi
# Node runs the bridge's TypeScript as it is, which needs a Node that strips
# types by default: the major in .nvmrc, or a newer one.
wanted=$(cat .nvmrc)
running=$(node --version | sed 's/^v//; s/\..*//')
if [ "$running" -lt "$wanted" ]; then
    printf 'node %s is older than the %s channels/.nvmrc asks for\n' \
        "$(node --version)" "$wanted" >&2
    exit 2
fi

# --ignore-scripts as well as .npmrc: no package's own code runs while it is
# being installed.
npm ci --ignore-scripts
npm run check

# Advisories for everything installed; signatures and provenance for what
# ships in the image.
npm audit --audit-level=low
npm audit signatures --omit=dev
