# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Enforce the dependency matrix from docs/layout.md with import-linter.

Two kinds of test, and the second is the one that matters. The first runs the
contracts over the package as it is and expects them kept, which is what CI
runs. The rest prove they have **teeth where it is easy to have none**: the
rule that confines an agent framework to its own sub-package is written with
``robinauts.adapters`` named as a *package*, so a module added to that layer
tomorrow is inside the rule without anybody remembering to list it. That is a
claim about a configuration file, and the only honest way to check it is to
add such a module and watch the contract break.

**The module that breaks the rule is added to a copy**, never to the package
this test is running from. Writing into the real tree would make two test
sessions collide -- one deleting the other's probe -- and would leave a
stranded module in the package if a run were killed between writing it and
deleting it. So the whole of ``src/`` is copied into the test's own
directory, the real ``pyproject.toml`` is copied beside it so that the
contracts checked are the contracts this repository states, and
``PYTHONPATH`` points import-linter at the copy.

A copy can go stale, and there are two guards against checking one: the
contracts of the copied configuration are asserted to be the real ones, and
every probe test asserts the contract **breaks** naming the probe module --
which it could only do if the copy really is what was analysed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]

PYPROJECT = ROOT / "pyproject.toml"

PACKAGE = Path("src") / "robinauts"

ADAPTERS = PACKAGE / "adapters"

PROBE = """# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

\"\"\"Written by a test, into a copy of the package; see test_architecture.py.

It imports a confined library from the wrong place on purpose.
\"\"\"

import {module}  # noqa: F401
"""
"""A module in the adapters layer that breaks the rule, header and all.

``{module}`` is filled in per framework: the same probe proves the same
claim about each contract, and one of them passing while the other has no
probe at all is exactly how a rule written for two engines ends up enforced
for one.

The licence header is there because this is a file with a Robinauts copyright
line in it, whatever directory it is written into; a file of ours without one
is a habit worth not having.
"""


@dataclass(frozen=True, slots=True)
class Framework:
    """One confined library, its contract, and the sub-package it belongs to.

    The two agent frameworks, and the MCP SDK -- which is confined the same
    way, to ``adapters/tools/mcp/``, before it is a dependency at all
    (``backend/pyproject.toml``): a probe that imports a package nobody has
    installed is still an import the contract sees, which is the point of
    writing the rule first.
    """

    module: str
    """What the probe imports: the library's top-level module."""
    contract: str
    """The contract's **whole** name, as ``backend/pyproject.toml`` states it.

    Whole rather than a fragment, because the verdict is read by looking for
    "<name> KEPT" or "<name> BROKEN" beside it -- and because a name that has
    drifted from the configuration is then a failure here rather than a test
    that quietly checks nothing (``verdict``).
    """
    sub_package: str
    """The directory under ``adapters/`` the exception names, as a relative path."""
    probe: str
    """A name for this framework's probe module, unique so that two can coexist."""


FRAMEWORKS = (
    Framework(
        module="langgraph",
        contract=(
            "LangGraph, LangChain, its provider clients and langsmith only under"
            " adapters.agents.langgraph"
        ),
        sub_package="agents/langgraph",
        probe="langgraph",
    ),
    Framework(
        module="pydantic_ai",
        contract=("Pydantic AI, logfire and OpenTelemetry only under adapters.agents.pydantic_ai"),
        sub_package="agents/pydantic_ai",
        probe="pydantic_ai",
    ),
    Framework(
        module="mcp",
        contract="the MCP SDK only under adapters.tools.mcp",
        sub_package="tools/mcp",
        probe="mcp",
    ),
)
"""Both frameworks and the SDK, so that every probe below runs against every contract."""


def lint_imports(tree: Path = ROOT) -> subprocess.CompletedProcess[str]:
    """Run the contracts over the package in ``tree``, cache and all off.

    ``PYTHONPATH`` is what decides which package is analysed: an entry there
    is searched before the path the editable install adds, so a copy wins over
    the real thing. ``--config`` names the configuration beside that copy.
    """
    return subprocess.run(
        [
            str(Path(sys.executable).parent / "lint-imports"),
            "--no-cache",
            "--config",
            str(tree / "pyproject.toml"),
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tree / "src")},
    )


def contracts_of(pyproject: Path) -> Any:
    """The ``[tool.importlinter]`` section of that file, as it is written."""
    with open(pyproject, "rb") as file:
        return tomllib.load(file)["tool"]["importlinter"]


def named(section: Any) -> list[str]:
    """The names of the contracts in that section, in order."""
    return [contract["name"] for contract in section["contracts"]]


def squashed(text: str) -> str:
    """``text`` with its line breaks flattened, for finding a wrapped name in it."""
    return " ".join(text.split())


@pytest.fixture
def copied_package(tmp_path: Path) -> Iterator[Path]:
    """The whole package and its configuration, copied where a test may write.

    ``__pycache__`` is left behind: it is not source, it is what
    ``tests/conftest.py`` goes out of its way not to write, and copying one
    would put unlicensed bytecode in a directory ``reuse lint`` could reach.
    """
    tree = tmp_path / "tree"
    tree.mkdir()
    shutil.copytree(ROOT / "src", tree / "src", ignore=shutil.ignore_patterns("__pycache__"))
    # Copied whole, and not rewritten: the contracts this test checks are the
    # contracts this repository states, down to the comments beside them.
    shutil.copy2(PYPROJECT, tree / "pyproject.toml")
    yield tree


def probe(tree: Path, where: Path, framework: Framework = FRAMEWORKS[0]) -> None:
    """Write the rule-breaking module at that path inside the copy."""
    path = tree / where
    assert not path.exists(), f"{path} is in the way"
    path.write_text(PROBE.format(module=framework.module), encoding="utf-8")


def broken_import(module: str, framework: Framework) -> str:
    """The line ``lint-imports`` prints for that probe's forbidden import.

    Looked for in ``squashed`` output: a module name long enough makes
    ``lint-imports`` wrap the line, and a test that looked for it unwrapped
    would pass or fail on how long a probe happened to be called.
    """
    return f"{module} -> {framework.module}"


def verdict(result: subprocess.CompletedProcess[str], contract: str) -> str:
    """What ``lint-imports`` said about that contract: ``KEPT`` or ``BROKEN``.

    Read from the report rather than from the exit code, because the exit code
    is about the whole run: a probe that broke *some* contract would fail a
    test that only looked at it, whichever contract that was. The name is
    required to appear exactly once with a verdict beside it, so a contract
    renamed in the configuration and not here is a failure and not a silence.
    """
    printed = squashed(result.stdout)
    said = [word for word in ("KEPT", "BROKEN") if f"{squashed(contract)} {word}" in printed]
    assert len(said) == 1, f"lint-imports said {said} about {contract!r}:\n{result.stdout}"
    return said[0]


def only_broken(result: subprocess.CompletedProcess[str], tree: Path, contract: str) -> None:
    """That contract is broken and **every other one** of them is kept.

    The whole verdict, not half of it: a probe that broke three contracts, or
    that broke a different one from the one it was written for, would pass an
    assertion about its own and say nothing about the rest.
    """
    names = named(contracts_of(tree / "pyproject.toml"))
    assert contract in names, f"{contract!r} is not a contract this repository states"
    assert {name: verdict(result, name) for name in names} == {
        name: ("BROKEN" if name == contract else "KEPT") for name in names
    }


by_framework = pytest.mark.parametrize(
    "framework", FRAMEWORKS, ids=[framework.module for framework in FRAMEWORKS]
)
"""Every probe below, once per confined library: two engines and the SDK, three contracts."""


def test_import_contracts() -> None:
    result = lint_imports()

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.io  # it copies the package and runs a subprocess
def test_the_copy_is_checked_against_the_contracts_this_repository_states(
    copied_package: Path,
) -> None:
    """The copy's configuration is the real one, and the copy keeps it.

    Without this, every test below could pass against a configuration that
    had drifted from the one CI runs -- a copy is only worth what it is a
    copy of.
    """
    section = contracts_of(copied_package / "pyproject.toml")

    result = lint_imports(copied_package)

    assert section == contracts_of(PYPROJECT)
    assert result.returncode == 0, result.stdout + result.stderr
    printed = squashed(result.stdout)
    assert named(section), "the configuration states no contracts at all"
    for name in named(section):
        assert squashed(name) in printed


@pytest.mark.io
@by_framework
def test_a_new_adapter_module_is_inside_the_framework_rule_without_being_listed(
    copied_package: Path, framework: Framework
) -> None:
    """A module nobody listed, importing a framework, breaks its contract.

    The point is what is *not* done: the file is not named anywhere, in the
    configuration or in this test's expectations. It is simply a module of the
    adapters layer, which is what the contract's source is. Run for **both**
    frameworks, because the claim is about each rule and not about the one
    that happened to be written first.
    """
    where = f"_probe_outside_the_rule_{framework.probe}"
    probe(copied_package, ADAPTERS / f"{where}.py", framework)

    result = lint_imports(copied_package)

    assert result.returncode != 0, result.stdout + result.stderr
    only_broken(result, copied_package, framework.contract)
    assert broken_import(f"robinauts.adapters.{where}", framework) in squashed(result.stdout)


@pytest.mark.io
@by_framework
def test_the_exception_covers_the_sub_package_and_nothing_beside_it(
    copied_package: Path, framework: Framework
) -> None:
    """The same module, one directory further in, is still outside the exception.

    ``adapters/agents/`` is not ``adapters/agents/<framework>/``, and
    ``adapters/tools/`` is not ``adapters/tools/mcp/``: the exception names one
    sub-package, and a module beside it is held to the rule like every other.
    """
    beside = Path(framework.sub_package).parent
    where = f"_probe_beside_the_exception_{framework.probe}"
    probe(copied_package, ADAPTERS / beside / f"{where}.py", framework)

    result = lint_imports(copied_package)

    assert result.returncode != 0, result.stdout + result.stderr
    only_broken(result, copied_package, framework.contract)
    module = ".".join(("robinauts", "adapters", *beside.parts, where))
    assert broken_import(module, framework) in squashed(result.stdout)


@pytest.mark.io
@by_framework
def test_the_probe_is_allowed_inside_the_sub_package_the_exception_names(
    copied_package: Path, framework: Framework
) -> None:
    """And the exception really is an exception: there, the same import is fine.

    A rule that refused everywhere would pass the two tests above and be
    useless; this is the other half of the claim.
    """
    inside = ADAPTERS / framework.sub_package / "_probe_inside_the_exception.py"
    probe(copied_package, inside, framework)

    result = lint_imports(copied_package)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.io
def test_writing_a_probe_leaves_the_real_package_untouched(copied_package: Path) -> None:
    """Nothing here ever writes into the tree the suite is running from."""
    probe(copied_package, ADAPTERS / "_probe_outside_the_rule.py")

    assert not (ROOT / ADAPTERS / "_probe_outside_the_rule.py").exists()
    assert lint_imports().returncode == 0
