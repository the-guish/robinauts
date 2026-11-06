#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Takes down what docker/start.sh started. The database's volume stays, and the
# conversations with it, unless --reset is given. The sandbox's memory goes
# either way: it lives in the sandbox worker's process, so a conversation with
# the coder is read-only once the sandbox container is gone
# (docs/architecture/sandbox-workers.md).
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here"

env=""
[ -f .env ] && env="--env-file .env"

case "${1:-}" in
    "")
        # shellcheck disable=SC2086
        docker compose $env down
        ;;
    --reset)
        # shellcheck disable=SC2086
        docker compose $env down --volumes
        ;;
    *)
        printf 'docker/stop.sh: unknown argument %s; the one there is is --reset\n' "$1" >&2
        exit 2
        ;;
esac
