# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the built wheel actually carries.

The deliverable is one wheel: the backend, the schema, the built interface and
the licence files (``docs/specs/frontend.md``, ``docs/oss-checklist.md``).
Every one of those but the Python is a file a packaging default could quietly
leave behind, and reading them through ``importlib.resources`` proves nothing:
a development checkout is an editable install, where the package *is* the
source tree. So this builds a wheel and looks inside it.

Marked ``io``: it runs a build, into a temporary directory that is thrown
away, and needs no database. It takes a couple of seconds. It skips, with
the reason, on a machine with no ``uv`` on the path -- and on one where the
frontend has not been built, because a wheel without it is refused
(``backend/hatch_build.py``) and the checkout is simply not one a release comes
out of. ``scripts/check-wheel.sh`` is what CI runs, and it builds the frontend
first, so nothing here is skipped where it matters.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from robinauts.legacy.api import ASSETS, HASHED, INDEX, UI_DIRECTORY
from robinauts.legacy.datastore import schema_sql

pytestmark = pytest.mark.io

BACKEND = Path(__file__).resolve().parents[2]
REPOSITORY = BACKEND.parent
FRONTEND_BUILD = REPOSITORY / "frontend" / "dist"

SHIPPED = "robinauts/legacy/datastore/schema.sql"
PACKAGED_UI = f"robinauts/{UI_DIRECTORY}"
NOTICES = "THIRD_PARTY_LICENSES.txt"
LICENCE_FILES = ("LICENSE", "NOTICE", NOTICES)
"""What ``project.license-files`` names, and what a release carries them for."""

NOT_FROM_AN_SDIST = "the sdist is not the deliverable"
"""A fragment of what ``hatch_build.py`` refuses such a wheel with.

Written out rather than imported: that module imports hatchling, which is a
build dependency and is not in the environment the tests run in, and a test
that imported the sentence it is checking would pass whatever it said.
"""


def inside(built: zipfile.ZipFile, path: str) -> list[str]:
    """Every name in the wheel under ``path``, so a test can say what it wants."""
    return [name for name in built.namelist() if name.startswith(f"{path}/")]


def metadata_directory(built: zipfile.ZipFile) -> str:
    """The ``.dist-info`` this wheel carries, found rather than spelt with a version."""
    directories = {name.split("/", 1)[0] for name in built.namelist()}
    found = sorted(name for name in directories if name.endswith(".dist-info"))
    assert len(found) == 1, f"expected one .dist-info, got {found}"
    return found[0]


def left_behind(*, before: bool, after: bool) -> bool:
    """Whether the build added a ``dist/`` that was not there before it.

    A developer who ran ``uv build`` by hand already has one, and that is not
    this test's business: the question is what *this* build left, not what
    the tree looked like to begin with.
    """
    return after and not before


@pytest.fixture(scope="module")
def dist_before() -> bool:
    """Whether ``backend/dist`` was there **before** anything was built.

    The `wheel` fixture below asks for it, which is what makes this run
    first: read after the build, it would be the same value as the one the
    test compares it against, and the comparison would always hold.
    """
    return (BACKEND / "dist").exists()


@pytest.fixture(scope="module")
def wheel(dist_before: bool) -> Iterator[Path]:
    """A freshly built wheel, in a directory removed when the module is done.

    Built with ``--wheel`` alone: the source distribution would double the
    time and this is a question about the wheel.
    """
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("no uv on the path to build a wheel with")
    if not (FRONTEND_BUILD / INDEX).is_file():
        pytest.skip(
            f"{FRONTEND_BUILD} has no {INDEX}: a wheel is refused without the built"
            f" interface, so run `npm ci && npm run build` in frontend/ -- or"
            f" scripts/check-wheel.sh, which does the whole of this"
        )
    with tempfile.TemporaryDirectory(prefix="robinauts-wheel-") as out:
        built = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", out],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            check=False,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        wheels = list(Path(out).glob("*.whl"))
        assert len(wheels) == 1, f"expected one wheel, got {wheels}"
        yield wheels[0]


def test_the_schema_definition_is_in_the_wheel(wheel: Path) -> None:
    # Without it an installed Robinauts could not create its own database,
    # and nothing would say so until somebody ran the command on a new
    # deployment.
    with zipfile.ZipFile(wheel) as built:
        assert SHIPPED in built.namelist()


def test_the_schema_in_the_wheel_is_the_schema_the_code_reads(wheel: Path) -> None:
    # A wheel carrying a *different* schema.sql would be worse than one
    # carrying none: the version check would pass and the tables would not
    # match. Newlines are normalised on both sides, because the question is
    # what the file says and not how this checkout spells the end of a line.
    with zipfile.ZipFile(wheel) as built:
        shipped = built.read(SHIPPED).decode("utf-8").replace("\r\n", "\n")

    assert shipped == schema_sql().replace("\r\n", "\n")


def test_the_built_interface_is_in_the_wheel(wheel: Path) -> None:
    # `pip install` plus a PostgreSQL is a whole deployment
    # (docs/specs/frontend.md): a wheel without the interface installs, starts
    # and serves "the interface is not built" to everybody who asks for it.
    with zipfile.ZipFile(wheel) as built:
        carried = inside(built, PACKAGED_UI)
        page = built.read(f"{PACKAGED_UI}/{INDEX}").decode("utf-8")

    assert f"{PACKAGED_UI}/{INDEX}" in carried
    assert [name for name in carried if name.startswith(f"{PACKAGED_UI}/{ASSETS}/")], carried
    # And it is the page this build made, not an empty file: it names the
    # hashed assets beside it, which is the whole of what it is for.
    assert f"./{ASSETS}/" in page


def test_the_wheel_carries_every_licence_file_and_says_so(wheel: Path) -> None:
    # The release items of docs/oss-checklist.md: our own licence, the
    # attribution notice, and the notices of everything inside the JavaScript
    # bundle -- in the wheel *and* listed in its metadata, which is the half
    # that is usually missed.
    with zipfile.ZipFile(wheel) as built:
        where = metadata_directory(built)
        carried = inside(built, f"{where}/licenses")
        metadata = built.read(f"{where}/METADATA").decode("utf-8")
        notices = built.read(f"{where}/licenses/{NOTICES}").decode("utf-8")

    assert carried == [f"{where}/licenses/{name}" for name in sorted(LICENCE_FILES)]
    for name in LICENCE_FILES:
        assert f"License-File: {name}" in metadata
    # Not an empty file where a notice should be: the bundle really does carry
    # somebody else's code, and this is where their terms travel.
    assert "MIT" in notices


def test_every_asset_in_the_wheel_carries_a_hash_of_its_contents(wheel: Path) -> None:
    """What makes a year of ``immutable`` honest (``robinauts.legacy.api.ui.HASHED``).

    Asked of a **real** build rather than of the configuration that produced
    it: the rule the server applies is about file names, and this is where the
    file names really are.
    """
    with zipfile.ZipFile(wheel) as built:
        assets = inside(built, f"{PACKAGED_UI}/{ASSETS}")

    assert assets
    unhashed = [name for name in assets if not HASHED.search(name)]
    assert unhashed == [], (
        f"{unhashed} would be served with a year of `immutable` if it were hashed and"
        f" is revalidated instead; if the build stopped hashing what it emits, the"
        f" caching rule in robinauts.legacy.api.ui is what has to change"
    )


def test_nothing_hidden_is_packaged(wheel: Path) -> None:
    # A directory a build writes into is one other things write into too, and
    # a `.env` beside the bundle is a deployment's database url. It is not
    # served (robinauts.legacy.api.ui.hidden) and it is not shipped either.
    with zipfile.ZipFile(wheel) as built:
        carried = inside(built, PACKAGED_UI)

    hidden = [name for name in carried if any(part.startswith(".") for part in name.split("/"))]
    assert hidden == []


def test_the_build_left_nothing_new_in_the_tree(wheel: Path, dist_before: bool) -> None:
    # `uv build` writes to backend/dist/ unless it is told otherwise, and a
    # dist/ left behind is a directory `reuse lint` then reads. What is
    # asserted is that *this* build added none: a dist/ that was already
    # there is somebody's own, and failing over it would be this test
    # complaining about a tree it did not touch.
    assert wheel.parent != BACKEND / "dist"
    assert not left_behind(before=dist_before, after=(BACKEND / "dist").exists())
    # And the licence files the build staged beside pyproject.toml are gone
    # again: they are copies, made for one archive (backend/hatch_build.py).
    assert [name for name in LICENCE_FILES if (BACKEND / name).exists()] == []


@pytest.mark.parametrize(
    ("before", "after", "left"),
    [(False, True, True), (False, False, False), (True, True, False), (True, False, False)],
)
def test_only_a_dist_this_build_made_counts_as_left_behind(
    before: bool, after: bool, left: bool
) -> None:
    # The one case that must fail the test above is the one where the build
    # made the directory: `uv build` without `--out-dir` would do exactly
    # that, and `reuse lint` would then read whatever it put there.
    assert left_behind(before=before, after=after) is left


# The source distribution, which is not the deliverable and is made honest.


@pytest.fixture(scope="module")
def sdist(dist_before: bool) -> Iterator[Path]:
    """The unpacked source distribution, in a directory of its own.

    Not skipped where the frontend has not been built: an sdist is a copy of
    ``backend/`` and carries no interface either way. It **is** skipped with
    no ``uv``, like every build here.
    """
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("no uv on the path to build a source distribution with")
    with tempfile.TemporaryDirectory(prefix="robinauts-sdist-") as out:
        built = subprocess.run(
            [uv, "build", "--sdist", "--out-dir", out],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            check=False,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        (archive,) = list(Path(out).glob("*.tar.gz"))
        unpacked = Path(out) / "unpacked"
        with tarfile.open(archive) as held:
            # `data` is what Python 3.14 will do by default and what a caller
            # of any version should ask for: no absolute paths, no links out.
            held.extractall(unpacked, filter="data")
        (root,) = list(unpacked.iterdir())
        yield root


def test_the_source_distribution_carries_this_projects_own_licence_and_no_other(
    sdist: Path,
) -> None:
    """Exactly two, in every checkout, whether the frontend was built or not.

    The third is the notices of everything inside the JavaScript bundle, and
    an sdist carries no bundle: shipping them would distribute somebody else's
    notice beside none of their code, and -- worse for a file people compare
    -- would make an sdist's metadata depend on whether whoever built it had
    run ``npm run build`` first.
    """
    listed = [
        line.split(":", 1)[1].strip()
        for line in (sdist / "PKG-INFO").read_text(encoding="utf-8").splitlines()
        if line.startswith("License-File:")
    ]

    assert listed == ["LICENSE", "NOTICE"]
    assert (sdist / "LICENSE").is_file()
    assert (sdist / "NOTICE").is_file()
    assert not (sdist / NOTICES).exists()


def test_building_the_source_distribution_leaves_no_licence_file_behind(sdist: Path) -> None:
    """The metadata hook stages them for **every** build; something must clear them.

    That something is the build hook, registered for the sdist target for this
    one reason: without it, ``uv build --sdist`` would leave three copies of
    somebody's licence beside ``pyproject.toml`` for the next commit to pick up.
    """
    assert [name for name in LICENCE_FILES if (BACKEND / name).exists()] == []


def test_a_wheel_built_from_the_source_distribution_is_refused(sdist: Path) -> None:
    """And says where a wheel does come from, which is a different thing to do.

    ``uv build`` with no argument builds the sdist and then the wheel **from**
    it. The interface is not under ``backend/``, so it is not in an sdist of
    ``backend/`` either, and a wheel made there would install and serve "the
    interface is not built" to everybody.
    """
    uv = shutil.which("uv")
    assert uv is not None  # the fixture skipped if it were not
    with tempfile.TemporaryDirectory(prefix="robinauts-from-sdist-") as out:
        built = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", out],
            cwd=sdist,
            capture_output=True,
            text=True,
            check=False,
        )

        assert built.returncode != 0
        assert list(Path(out).glob("*.whl")) == []
    assert NOT_FROM_AN_SDIST in built.stdout + built.stderr
    # And it left the sdist's **own** licence files where they were. They sit
    # exactly where this stages copies in a checkout, and taking a licence out
    # of somebody's copy of the project on the way to refusing them a wheel
    # would be a poor trade (`hatch_build.in_a_checkout`).
    assert (sdist / "LICENSE").is_file()
    assert (sdist / "NOTICE").is_file()


def test_a_wheel_from_an_sdist_unpacked_beside_a_frontend_is_refused_too(sdist: Path) -> None:
    """An sdist is an sdist wherever it was unpacked, a checkout included.

    Somebody unpacks the tarball in the repository -- or anywhere with a
    ``frontend/`` beside it -- and a rule that read "is there a frontend
    directory next door" would call it the repository: it would clear the
    sdist's own licence files and build it a wheel out of **somebody else's**
    bundle. So the rule is ``PKG-INFO``, which every sdist carries and no
    checkout has.

    The frontend beside it is built here rather than borrowed: the real
    repository is not a place for a test to unpack things into.
    """
    uv = shutil.which("uv")
    assert uv is not None  # the fixture skipped if it were not
    beside = sdist.parent
    (beside / "LICENSE").write_text("not this project's", encoding="utf-8")
    (beside / "NOTICE").write_text("nor this", encoding="utf-8")
    assets = beside / "frontend" / "dist" / ASSETS
    assets.mkdir(parents=True)
    (assets / "index-D93cLA-w.js").write_text("somebody else's bundle", encoding="utf-8")
    (beside / "frontend" / "dist" / INDEX).write_text("<!doctype html>", encoding="utf-8")
    (beside / "frontend" / "dist" / NOTICES).write_text("their notices", encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="robinauts-beside-") as out:
        built = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", out],
            cwd=sdist,
            capture_output=True,
            text=True,
            check=False,
        )

        assert built.returncode != 0
        assert list(Path(out).glob("*.whl")) == []
    assert NOT_FROM_AN_SDIST in built.stdout + built.stderr
    assert (sdist / "LICENSE").read_text(encoding="utf-8") != "not this project's"
    assert (sdist / "NOTICE").is_file()


# The locked runtime set the build writes beside the wheel.


BUILD_SCRIPT = REPOSITORY / "scripts" / "build-wheel.sh"
LOCK = BACKEND / "uv.lock"
PYPROJECT = BACKEND / "pyproject.toml"

DEVELOPMENT_ONLY = ("pytest", "ruff", "black", "import-linter")
"""What ``[dependency-groups].dev`` names: none of it is in a deployment."""

PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ \;]+)", re.MULTILINE)
"""A pinned requirement at the beginning of a line; markers and hashes follow."""


def export_command() -> list[str]:
    """The ``uv export`` line out of ``scripts/build-wheel.sh``, as written there.

    Read from the script rather than repeated here, so that this is a test of
    **what the build does**: a flag changed there -- ``--no-dev`` dropped, the
    project emitted after all -- has to come through this, or the test would be
    checking a command nothing runs.
    """
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"^uv export .*?--format requirements\.txt", text, re.MULTILINE | re.DOTALL)
    assert found is not None, f"{BUILD_SCRIPT} no longer exports a requirements file"
    return found.group(0).replace("\\\n", " ").split()


@pytest.fixture(scope="module")
def exported() -> str:
    """What that command writes: the locked runtime set, with its hashes."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("no uv on the path to export the lock with")
    command = export_command()
    done = subprocess.run(
        [uv, *command[1:]], cwd=BACKEND, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def locked_versions() -> dict[str, str]:
    """Every package ``uv.lock`` pins, by name."""
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    return {package["name"]: package["version"] for package in lock["package"]}


def test_the_export_pins_the_versions_the_lock_pins(exported: str) -> None:
    # The whole point of the file: `pip install robinauts-*.whl` resolves the
    # wheel's ranges against PyPI on the day, and can land on a version no gate
    # has ever seen. This is the set the gates judged, so every line of it has
    # to be a line of uv.lock.
    locked = locked_versions()
    pinned = dict(PIN.findall(exported))

    assert pinned, "the export pins nothing at all"
    assert {name: version for name, version in pinned.items() if locked.get(name) != version} == {}


def test_the_export_is_the_runtime_set_and_not_the_development_one(exported: str) -> None:
    pinned = dict(PIN.findall(exported))
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

    # Every direct runtime dependency is there ...
    for requirement in declared["dependencies"]:
        name = re.split(r"[<>=!~\[ ]", requirement, maxsplit=1)[0]
        assert name in pinned, f"{name} is a runtime dependency and is not in the export"
    # ... and nothing a deployment has no use for, the project included: the
    # wheel is installed separately, with --no-deps (docs/deployment.md).
    assert declared["name"] not in pinned
    for name in DEVELOPMENT_ONLY:
        assert name not in pinned


def test_every_pin_in_the_export_carries_its_hashes(exported: str) -> None:
    # `pip install --require-hashes` is only possible when every requirement
    # has one, and a set installed without hashes is a set whose files nobody
    # checked (docs/oss-checklist.md).
    #
    # Split **per package**, at the beginning of each requirement rather than
    # on blank lines: uv writes none, so a blank-line split is one chunk with
    # every hash in it, and "each pin has a hash" would be true of a file
    # where only the first one did.
    entries = [entry for entry in re.split(r"(?m)^(?=[A-Za-z0-9])", exported) if entry.strip()]
    pinned = [(name, entry) for entry in entries for name, _ in PIN.findall(entry)]

    assert len(pinned) > 1, f"the export was read as {len(pinned)} requirement(s)"
    assert [name for name, entry in pinned if "--hash=sha256:" not in entry] == []
