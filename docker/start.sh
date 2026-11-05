#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Robinauts in Docker: builds the images and starts the three containers of
# docker/compose.yaml -- the control plane, the sandbox worker and PostgreSQL
# -- and waits until the control plane answers. docker/stop.sh takes them down
# again, and docker/README.md is the whole of what this does.
#
# It reads docker/.env, which must be mode 0600 since it holds keys, and fills
# in the two values it may make itself, the sandbox workers' token and the
# database's password, when they are empty. No key or token is ever printed.
#
# Every failure is one line and a non-zero exit: 2 for something to put right
# before running it again, 1 for something that went wrong while starting.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
env_file="$here/.env"

fail() {
    printf 'docker/start.sh: %s\n' "$2" >&2
    exit "$1"
}

command -v docker >/dev/null 2>&1 || fail 2 "docker is not installed"
docker compose version >/dev/null 2>&1 || fail 2 "docker compose is not installed"

[ -f "$env_file" ] || fail 2 "no docker/.env: cp docker/.env.example docker/.env, chmod 600 it, and fill it in"

# The mode, as `ls -l` prints it, which reads the same on Linux and on macOS.
mode=$(ls -l "$env_file" | cut -c1-10)
[ "$mode" = "-rw-------" ] || fail 2 "docker/.env holds keys, and is read only when it is mode 0600: chmod 600 docker/.env"

# A value of docker/.env, read, never sourced: a .env that could run commands
# would run whatever was pasted into it.
value() {
    sed -n "s/^$1=//p" "$env_file" | tail -n 1
}

# A random secret, for a value docker/.env leaves empty.
random_secret() {
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

fill_in() {
    name=$1
    if [ -z "$(value "$name")" ]; then
        secret=$(random_secret)
        if grep -q "^$name=" "$env_file"; then
            # In place, through a file of the same mode, so the secret is never on a
            # command line another user's `ps` could read.
            tmp="$env_file.tmp"
            (umask 077 && sed "s/^$name=.*/$name=$secret/" "$env_file" >"$tmp")
            mv "$tmp" "$env_file"
        else
            # A last line with no newline would take the value as its own.
            if [ -n "$(tail -c 1 "$env_file")" ]; then
                printf '\n' >>"$env_file"
            fi
            printf '%s=%s\n' "$name" "$secret" >>"$env_file"
        fi
        printf 'docker/start.sh: made %s in docker/.env\n' "$name"
    fi
}

fill_in ROBINAUTS_SANDBOX_TOKEN
fill_in ROBINAUTS_POSTGRES_PASSWORD

if [ -z "$(value ANTHROPIC_API_KEY)$(value OPENROUTER_API_KEY)$(value OPENAI_API_KEY)" ]; then
    fail 2 "no model provider key in docker/.env: set the one docker/robinauts.toml names"
fi
if [ -z "$(value ROBINAUTS_GITHUB_TOKEN)" ]; then
    printf '%s\n' "docker/start.sh: no ROBINAUTS_GITHUB_TOKEN in docker/.env: the coder agent's turns will fail until it is set" >&2
fi

port=$(value ROBINAUTS_PORT)
port=${port:-8000}

cd "$here"
docker compose --env-file "$env_file" up --build --detach --wait \
    || fail 1 "the containers did not start: docker compose --env-file docker/.env logs, in docker/, says why"

printf '\nRobinauts is up: http://127.0.0.1:%s/\n' "$port"
printf '%s\n' "  the Assistant runs in the control plane; the Coder in the sandbox container."
printf '%s\n' "  logs: cd docker && docker compose logs -f api sandbox"
printf '%s\n' "  stop: docker/stop.sh (the conversations stay), docker/stop.sh --reset (they go)"
