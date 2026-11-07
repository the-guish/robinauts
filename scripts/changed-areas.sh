#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Says which areas a pull request changes, in the form $GITHUB_OUTPUT reads.
# The areas are backend, frontend, contract (backend/openapi.json, which is in
# backend too), android (clients/android), extra_workers (extra-workers) and
# repo. A file in no other area marks repo, and so does an empty diff. CI runs
# every suite on repo. A rename counts as both of its paths.
#
#     scripts/changed-areas.sh <base> <head>
set -eu

if [ "$#" -ne 2 ]; then
    printf 'usage: %s <base> <head>\n' "$0" >&2
    exit 2
fi

changed=$(git diff --no-renames --name-only "$1...$2")

backend=false
frontend=false
contract=false
android=false
extra_workers=false
repo=false

[ -n "$changed" ] || repo=true

while IFS= read -r path; do
    case "$path" in
        '') ;;
        backend/openapi.json) backend=true contract=true ;;
        backend/*) backend=true ;;
        frontend/*) frontend=true ;;
        clients/android/*) android=true ;;
        extra-workers/*) extra_workers=true ;;
        *) repo=true ;;
    esac
done <<EOF
$changed
EOF

echo "backend=$backend"
echo "frontend=$frontend"
echo "contract=$contract"
echo "android=$android"
echo "extra_workers=$extra_workers"
echo "repo=$repo"
