#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Walk docs/deployment.md on this machine, as far as a machine with no domain
# name, no identity provider and no model key can be walked. A development
# tool. No check runs it, and no check lints it either: there is no
# shellcheck in this repository, for any script. Its Python half is judged by
# ruff and black through scripts/check-lint.sh, which reads scripts/ as well
# as the backend.
#
# What it does, in the order the guide does it: build the wheel, make a virtual
# environment and install it, create a throwaway role and database, create the
# schema with `robinauts db init`, make a self-signed certificate, and hand
# over to scripts/rehearse_deployment.py for everything that needs a browser
# (the stand-in identity providers, the TLS terminator, the sign-ins, the
# conversations, the stream). Then it puts the database back.
#
# Usage: scripts/rehearse-deployment.sh [work-directory] [admin-database-url]
#
# The admin url is a PostgreSQL this may create and drop a database on. The
# default is the throwaway cluster these notes are written against.
# ROBINAUTS_REHEARSAL_PORTS is "<backend> <proxy>" when a development machine
# already has something on 8000 or 8443; the rehearsal refuses to proxy to
# whatever else is listening rather than reporting on somebody else's server.
set -eu

work=${1:-${TMPDIR:-/tmp}/robinauts-rehearsal}
admin=${2:-postgresql://postgres@127.0.0.1:54329/postgres}
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

database=robinauts_rehearsal
# Not `robinauts`: the guide's own role is called that, and this script drops
# the role it made. A name nothing else would choose cannot be somebody's real
# deployment.
role=robinauts_rehearsal
password=rehearsal-only

mkdir -p "$work"
work=$(CDPATH= cd -- "$work" && pwd)
printf '\n== the wheel ==\n'
rm -rf "$work/wheel" "$work/venv" "$work/tls"
wheel=$("$root/scripts/build-wheel.sh" "$work/wheel")
printf 'built %s\n' "$wheel"

printf '\n== a virtual environment of its own ==\n'
python3 -m venv "$work/venv"
# The guide's two steps, and nothing before them -- no `pip install --upgrade
# pip` either, so that what is rehearsed is what the guide tells somebody to
# type. `ensurepip`'s own pip is what a fresh venv has, and it installs this.
"$work/venv/bin/pip" install --quiet --require-hashes -r "$work/wheel/requirements.txt" \
    --find-links "$work/wheel/neorc-wheels" # neorc-from-source
"$work/venv/bin/pip" install --quiet --no-deps "$wheel"
"$work/venv/bin/robinauts" version

# The deployment's url is built out of the admin one, so that a second
# argument naming another cluster is honoured all the way through rather than
# half of it. Only the host and the port are taken from it: the role, the
# password and the database are this script's own.
url=$("$work/venv/bin/python" - "$admin" "$role" "$password" "$database" <<'URL'
import sys
from urllib.parse import quote, urlsplit

admin, role, password, database = sys.argv[1:5]
where = urlsplit(admin)
if not where.hostname:
    raise SystemExit(f"{admin!r} names no host to put the rehearsal database on")
# `hostname` unwraps an IPv6 literal's brackets, and a url without them is
# not a url: a colon in the host would be read as the port.
host = f"[{where.hostname}]" if ":" in where.hostname else where.hostname
authority = host + (f":{where.port}" if where.port else "")
print(f"postgresql://{quote(role)}:{quote(password)}@{authority}/{database}")
URL
)

# `createuser` and `createdb` in the guide; this cluster has no client
# binaries, so the driver the wheel already carries does the same two
# statements. Dropped again at the end, whatever happens.
administer() {
    "$work/venv/bin/python" - "$admin" "$@" <<'PY'
import asyncio
import sys

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(sys.argv[1])
    try:
        for statement in sys.argv[2:]:
            print(statement, "->", await connection.execute(statement))
    finally:
        await connection.close()


asyncio.run(main())
PY
}

drop() {
    administer "DROP DATABASE IF EXISTS $database" "DROP ROLE IF EXISTS $role" || true
}
trap drop EXIT
trap 'drop; exit 130' INT
trap 'drop; exit 143' TERM

printf '\n== the database ==\n'
drop
administer "CREATE ROLE $role LOGIN PASSWORD '$password'" \
    "CREATE DATABASE $database OWNER $role"
ROBINAUTS_DATABASE_URL="$url" "$work/venv/bin/robinauts" db init

printf '\n== a certificate for the reverse proxy ==\n'
mkdir -p "$work/tls"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
    -subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1" \
    -keyout "$work/tls/key.pem" -out "$work/tls/certificate.pem" 2>/dev/null
printf 'self-signed, one day, for 127.0.0.1\n'

printf '\n== the live half ==\n'
rm -f "$work/server.log"
if [ -n "${ROBINAUTS_REHEARSAL_PORTS:-}" ]; then
    # Two words, split on purpose: "<backend> <proxy>".
    # shellcheck disable=SC2086
    set -- $ROBINAUTS_REHEARSAL_PORTS
    set -- --backend-port "$1" --proxy-port "$2"
else
    set --
fi
"$work/venv/bin/python" "$root/scripts/rehearse_deployment.py" \
    --work "$work" --venv "$work/venv" --database-url "$url" \
    --standin "$root/backend/tests" "$@"
