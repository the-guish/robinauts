# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent engines: one per framework, each keeping ``docs/specs/agent-engines.md``.

``installed()`` is how a caller learns which engines this build has, by name, without
naming any: an engine whose package cannot be imported, because its extra is not
installed, is simply not in the answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module

from robinauts.agent_engines.contract.ports import EngineFactory

_SHIPPED: Mapping[str, tuple[str, str]] = {
    "langchain": ("robinauts.agent_engines.langchain_engine", "init_langchain"),
    "pydantic-ai": ("robinauts.agent_engines.pydantic_ai_engine", "init_pydantic_ai"),
}
"""Every engine this package ships: its name, and where its init function is."""


def installed() -> Mapping[str, EngineFactory]:
    """The engines this build can run, by the name a configuration uses."""
    found: dict[str, EngineFactory] = {}
    for name, (module, function) in _SHIPPED.items():
        try:
            found[name] = getattr(import_module(module), function)
        except ImportError:
            continue
    return found
