#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Unofficial example: Robinauts with MCP tools in Docker. README.md beside this
# script says what it does and what it needs.
#
# Self-contained: everything it runs on lives in this folder.
#   robinauts.toml   the configuration: three agents, the web tools, Composio
#   compose.yaml     PostgreSQL and the tools, in Docker
#   .env             the keys, if they are not in the environment (.env.example)
#   .state/          made here: the Python environment, the database password,
#                    the files tool's key, the pid
#   agents-fs/       made here: the one folder the files tool reads and writes
# From the repo it takes only the source, two levels up: the backend, installed
# from its lock into .state/venv, and the interface, built at every start.
#
# Usage: start.sh
# Env:   ROBINAUTS_DEMO_PORT (8000)
#        ROBINAUTS_DEMO_HOST (127.0.0.1; 0.0.0.0 serves the LAN, with no sign-in)
# Stop:  Ctrl-C (the chat server only), or stop.sh (everything).

set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
state="$here/.state"
venv="$state/venv"
port=${ROBINAUTS_DEMO_PORT:-8000}
host=${ROBINAUTS_DEMO_HOST:-127.0.0.1}

docker_() {
    if docker info >/dev/null 2>&1; then
        docker "$@"
    else
        sg docker -c "$(printf '%q ' docker "$@")"
    fi
}

if [ ! -f "$repo/backend/pyproject.toml" ] || [ ! -f "$repo/frontend/package.json" ]; then
    echo "$repo is not a Robinauts checkout: keep this folder at demo/docker-compose-with-mcp." >&2
    exit 2
fi
for tool in docker uv python3; do
    command -v "$tool" >/dev/null || { echo "$tool is not installed." >&2; exit 2; }
done

if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "Something already listens on port $port: run stop.sh, or set ROBINAUTS_DEMO_PORT." >&2
    exit 2
fi

mkdir -p "$state"
chmod 700 "$state"

# The keys: the environment first, then .env beside this script. The file is
# read only when no one else can read it.
if [ -f "$here/.env" ]; then
    if [ "$(stat -c %a "$here/.env")" != 600 ]; then
        echo "$here/.env must be readable by you alone: chmod 600 $here/.env" >&2
        exit 2
    fi
    set -a
    # shellcheck source=/dev/null
    source "$here/.env"
    set +a
fi
: "${OPENROUTER_API_KEY:?is not set: export it, or put it in .env (see .env.example)}"
export OPENROUTER_API_KEY

# 1. The database password, made once. Only the first start of an empty
#    volume reads it; after that it is what the database already has.
if [ ! -s "$state/pg-password" ]; then
    (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(24))' >"$state/pg-password")
fi
DEMO_PG_PASSWORD=$(cat "$state/pg-password")
export DEMO_PG_PASSWORD

# The files tool's key, made once: the bearer the server sends and supergateway
# requires. The folder it serves is made here, so that it is the user's and not
# root's, which is what Docker would make of a missing one.
if [ ! -s "$state/files-key" ]; then
    (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(32))' >"$state/files-key")
fi
DEMO_FILES_KEY=$(cat "$state/files-key")
DEMO_UID=$(id -u)
DEMO_GID=$(id -g)
DEMO_FILES_DIR="$here/agents-fs"
export DEMO_FILES_KEY DEMO_UID DEMO_GID DEMO_FILES_DIR
mkdir -p "$DEMO_FILES_DIR"

# 2. PostgreSQL and the tools. Search only with a key: Brave's server will not
#    start without one.
if [ -n "${BRAVE_API_KEY:-}" ]; then
    export BRAVE_API_KEY
    export COMPOSE_PROFILES=search
fi
echo "Starting PostgreSQL and the tools (docker compose) ..."
docker_ compose -f "$here/compose.yaml" up -d --build --wait

# 3. The interface, built, and the backend installed from its lock,
#    non-editably, so that the built interface is inside the package the
#    server imports. Both every start, so the demo runs the source as it is now.
: "${NVM_DIR:=$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    set +eu
    # shellcheck source=/dev/null
    . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
    nvm use "$(cat "$repo/frontend/.nvmrc")" >/dev/null 2>&1
    set -eu
fi
command -v npm >/dev/null || { echo "npm is not installed: see frontend/.nvmrc for the Node.js version." >&2; exit 2; }
echo "Building the interface (npm ci, npm run build) ..."
(cd "$repo/frontend" && npm ci --silent && npm run build --silent >/dev/null)
echo "Installing the backend into $venv ..."
(cd "$repo/backend" && UV_PROJECT_ENVIRONMENT="$venv" uv sync --locked --no-dev \
    --python 3.12 --no-editable --reinstall-package robinauts --quiet)

# 4. The configuration: this folder's, or a copy without the tools that have
#    no key.
missing=()
[ -n "${BRAVE_API_KEY:-}" ] || missing+=(search)
if [ -n "${COMPOSIO_API_KEY:-}" ]; then
    export COMPOSIO_API_KEY
else
    missing+=(composio)
fi
if [ ${#missing[@]} -eq 0 ]; then
    config="$here/robinauts.toml"
else
    config="$state/robinauts.toml"
    python3 - "$here/robinauts.toml" "$config" "${missing[@]}" <<'PY'
import re
import sys

source, target, *missing = sys.argv[1:]


def without(match):
    tools = [t for t in re.findall(r'"([^"]+)"', match.group(1)) if t not in missing]
    return "tools = [" + ", ".join(f'"{t}"' for t in tools) + "]"


text = open(source).read()
open(target, "w").write(re.sub(r"^tools = \[(.*)\]$", without, text, flags=re.M))
PY
fi
export ROBINAUTS_CONFIG="$config"
export ROBINAUTS_DATABASE_URL="postgresql://robinauts:$DEMO_PG_PASSWORD@127.0.0.1:54391/robinauts"

# 5. The schema; `db init` is idempotent.
"$venv/bin/robinauts" db init

echo
echo "Agents: Researcher and Pi Sidekick, with fetch, browser, files (agents-fs/)${BRAVE_API_KEY:+, search}${COMPOSIO_API_KEY:+ and Composio}."
[ ${#missing[@]} -eq 0 ] || echo "Left out for lack of a key: ${missing[*]}."
echo "Configuration: $config"
if [ "$host" = 127.0.0.1 ] || [ "$host" = localhost ]; then
    echo "Open http://127.0.0.1:$port/"
else
    lan_ip=$(ip -4 route get 1.1.1.1 2>/dev/null |
        awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')
    lan_ip=${lan_ip:-$(hostname -I | awk '{print $1}')}
    echo "Serving on $host:$port: open http://$lan_ip:$port/ from another device."
    echo "No sign-in: anyone on this network can use the chat, and your keys through it."
fi
echo "Ctrl-C stops the chat server only; stop.sh also stops PostgreSQL and the tools."
echo

# 6. The server. The local mode refuses a non-loopback host
#    (backend/src/robinauts/web/cli.py); for ROBINAUTS_DEMO_HOST that check is
#    patched out, in this process only. exec keeps this pid, so stop.sh can
#    find the server by it.
echo $$ >"$state/server.pid"
exec "$venv/bin/python" - "$host" "$port" <<'PY'
import sys

import robinauts.web.cli as cli

cli.is_loopback = lambda host: True
cli.run(["start", "--dev-no-sign-in", "--host", sys.argv[1], "--port", sys.argv[2]])
PY
