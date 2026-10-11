#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Unofficial example: stop what start.sh beside it started: the chat server,
# then PostgreSQL and the tools (compose.yaml). The conversations stay in the
# database's volume, and the files in agents-fs/.
#
# Usage: stop.sh [--reset]    --reset also deletes the volume: every conversation.

set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
reset=no
case "${1:-}" in
"") ;;
--reset) reset=yes ;;
*) echo "usage: stop.sh [--reset]" >&2; exit 2 ;;
esac

docker_() {
    if docker info >/dev/null 2>&1; then
        docker "$@"
    else
        sg docker -c "$(printf '%q ' docker "$@")"
    fi
}

# compose.yaml names the files tool's folder by this, and refuses to run without it.
export DEMO_FILES_DIR="$here/agents-fs"

# The chat server, by the pid start.sh wrote, and only while that pid is still
# a demo environment's python, not a number handed out again.
pid_file="$here/.state/server.pid"
if [ -f "$pid_file" ]; then
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null &&
        tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q "/.state/venv/bin/python - "; then
        echo "Stopping the chat server (pid $pid) ..."
        kill "$pid"
        for _ in $(seq 150); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        kill -9 "$pid" 2>/dev/null || true
    else
        echo "No chat server was running."
    fi
    rm -f "$pid_file"
else
    echo "No chat server was running."
fi

if [ "$reset" = yes ]; then
    echo "Stopping PostgreSQL and the tools, and deleting the database volume ..."
    docker_ compose -f "$here/compose.yaml" --profile search down --volumes
    rm -f "$here/.state/pg-password"
else
    echo "Stopping PostgreSQL and the tools (the conversations stay) ..."
    docker_ compose -f "$here/compose.yaml" --profile search down
fi
