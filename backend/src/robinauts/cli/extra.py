# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A deployment's extra applications: the components ``ROBINAUTS_COMPONENTS`` names, which the
backend composes after Robinauts' own and neorc's (``docs/architecture/web.md``, "Extra
applications").

The variable holds names separated by commas, each ``<module>:<function>`` of a package the
server's Python finds. The function takes the server's environment and returns the
application's oneberth ``Component``. The environment names the storage:
``ROBINAUTS_DATABASE_URL``, or ``ROBINAUTS_DATA_DIR``, the folder an application keeps a file
of its own in. A module is imported only when the variable names it.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping

from oneberth.web import Component

from robinauts.controller.contract.domain import ConfigError

VARIABLE = "ROBINAUTS_COMPONENTS"

NAME = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")
"""``<module>:<function>``: a dotted module, and one name in it."""


def named(environ: Mapping[str, str]) -> list[str]:
    """The names the variable holds, in its order; none when it is unset or empty."""
    return [name for part in environ.get(VARIABLE, "").split(",") if (name := part.strip())]


def extra_components(environ: Mapping[str, str]) -> list[Component]:
    """The components of the extra applications, in the variable's order. ``ConfigError`` names
    every one that cannot be had: a name spelt another way, a module that does not import, a
    name the module lacks, and a function that returns something else than a component."""
    components: list[Component] = []
    problems: list[str] = []
    for name in named(environ):
        if not NAME.fullmatch(name):
            problems.append(f"{VARIABLE}: {name!r} is not <module>:<function>")
            continue
        module_name, _, function_name = name.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as missing:
            problems.append(f"{VARIABLE}: {name}: {missing}")
            continue
        function = getattr(module, function_name, None)
        if not callable(function):
            problems.append(f"{VARIABLE}: {name}: {module_name} has no function {function_name}")
            continue
        component = function(environ)
        if not isinstance(component, Component):
            problems.append(
                f"{VARIABLE}: {name} returned {type(component).__name__}, not a oneberth Component"
            )
            continue
        components.append(component)
    if problems:
        raise ConfigError("\n".join(problems))
    return components
