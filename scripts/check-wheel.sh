#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The deliverable, end to end: build the wheel and the locked runtime set
# beside it, look inside the wheel, install both into a virtual environment
# that has nothing else in it -- the way docs/deployment.md tells a deployment
# to -- and run the command.
#
# What it is for is the half that no unit test can reach -- that what a person
# gets from `pip install` is a working Robinauts with its interface and the
# licences of everything it carries (docs/specs/operations.md,
# docs/oss-checklist.md). Everything it builds goes into a temporary directory
# that is removed however this ends, so the checkout is left exactly as it was.
#
# One optional argument: a directory to leave the wheel in, for a caller that
# wants to keep it (CI's `wheel` job uploads it as an artifact).
set -eu

if [ "$#" -gt 1 ]; then
    printf 'usage: %s [directory-to-keep-the-wheel-in]\n' "$0" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

work=$(mktemp -d)
# INT and TERM as well as EXIT: an interrupted check must not leave a wheel and
# a whole virtual environment behind in /tmp.
trap 'rm -rf "$work"' EXIT
trap 'rm -rf "$work"; exit 130' INT
trap 'rm -rf "$work"; exit 143' TERM

if [ "$#" -eq 1 ]; then
    mkdir -p "$1"
    into=$(CDPATH= cd -- "$1" && pwd)
else
    into="$work/dist"
fi

wheel=$("$root/scripts/build-wheel.sh" "$into")
printf 'built %s (%s bytes)\n' "$wheel" "$(wc -c <"$wheel")"

# The locked runtime set the build wrote beside the wheel. What is checked
# here is that it is *there and pinned*, because this is the directory CI
# uploads: a release whose requirements file went missing would be a
# deployment quietly installing whatever pip resolves. What it holds is
# checked against uv.lock by
# backend/tests/integration/test_wheel_contents.py.
#
# **Per package, not in total**: counting hash lines against pin lines would
# pass a file where one package carried every hash and the rest carried none,
# which is exactly the set `--require-hashes` would refuse.
requirements="$into/requirements.txt"
pins=$(grep -c '^[A-Za-z0-9]' "$requirements") || pins=0
unhashed=$(awk '
    /^[A-Za-z0-9]/ {
        if (name != "" && !hashed) { print name }
        name = $1
        hashed = ($0 ~ /--hash=sha256:/)
        next
    }
    /--hash=sha256:/ { hashed = 1 }
    END { if (name != "" && !hashed) { print name } }
' "$requirements") || unhashed="the file could not be read"
if [ "$pins" -lt 1 ] || [ -n "$unhashed" ]; then
    printf '%s pins %s package(s); a release set is pinned and every pin is hashed.\n' \
        "$requirements" "$pins" >&2
    printf 'Without a hash:\n%s\n' "$unhashed" >&2
    exit 1
fi
printf 'beside it, %s pins %s packages, every one of them hashed\n' "$requirements" "$pins"

# A virtual environment with nothing in it, so that what answers below can only
# have come out of the wheel -- installed **the way docs/deployment.md says**:
# the locked set first, under its hashes, and then the wheel with --no-deps so
# that nothing is resolved against an index at all. That makes this the
# end-to-end proof of the path a deployment really takes, rather than of a
# convenience nobody is told to use.
uv venv "$work/venv" >&2
python="$work/venv/bin/python"
uv pip install --python "$python" --require-hashes -r "$requirements" >&2
uv pip install --python "$python" --no-deps "$wheel" >&2

"$python" - "$wheel" <<'PYTHON'
"""Everything a release carries, named one at a time."""

import re
import sys
import zipfile

PACKAGE = "robinauts"
WANTED = (
    f"{PACKAGE}/ui/index.html",
    f"{PACKAGE}/controller/adapters/postgres/schema.sql",
)
UI = f"{PACKAGE}/ui/"
ASSETS = f"{UI}assets/"
LICENCES = ("LICENSE", "NOTICE", "THIRD_PARTY_LICENSES.txt")
# The rule hashed assets are cached by, asked of what
# the build really emitted: a year of `immutable` is honest only while every
# asset's name carries a digest of its contents.
HASHED = re.compile(r"-[A-Za-z0-9_-]{8}\.[A-Za-z0-9]+$")

with zipfile.ZipFile(sys.argv[1]) as built:
    names = built.namelist()
    tops = {name.split("/", 1)[0] for name in names}
    found = sorted(name for name in tops if name.endswith(".dist-info"))
    if len(found) != 1:
        print(
            f"a wheel holds exactly one .dist-info directory and this one holds"
            f" {len(found)}: {found}. That is a broken archive rather than a missing"
            f" file, so nothing else here was checked.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    (metadata_directory,) = found
    metadata = built.read(f"{metadata_directory}/METADATA").decode("utf-8")

missing = [name for name in WANTED if name not in names]
assets = [name for name in names if name.startswith(ASSETS)]
if not assets:
    missing.append(f"{ASSETS}*")

wrong = [name for name in assets if not HASHED.search(name)]
wrong += [
    name
    for name in names
    if name.startswith(UI) and any(part.startswith(".") for part in name.split("/"))
]
# In the wheel *and* listed in its metadata: the second is the half that is
# usually missed (docs/oss-checklist.md, "Releases").
missing.extend(
    f"{metadata_directory}/licenses/{name}"
    for name in LICENCES
    if f"{metadata_directory}/licenses/{name}" not in names
)
missing.extend(f"License-File: {name}" for name in LICENCES if f"License-File: {name}" not in metadata)

if missing:
    print("the wheel is missing:", file=sys.stderr)
    for name in missing:
        print(f"  {name}", file=sys.stderr)
    raise SystemExit(1)

if wrong:
    print(
        "the wheel carries interface files it should not: an asset whose name holds no"
        " digest of its contents (which the server would cache for a year), or a file"
        " whose name begins with a dot (which is not part of the interface):",
        file=sys.stderr,
    )
    for name in wrong:
        print(f"  {name}", file=sys.stderr)
    raise SystemExit(1)

print(
    f"the wheel carries {len(names)} files: the interface ({len(assets)} hashed assets),"
    f" the schema and every licence file"
)
PYTHON

# The console script, out of the installed wheel and nothing else: it says
# which build this is and which schema it wants, and it needs no database to.
"$work/venv/bin/robinauts" --help >/dev/null
"$work/venv/bin/robinauts" version

printf 'the wheel installs and answers.\n'
