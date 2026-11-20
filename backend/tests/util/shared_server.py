# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a test module adds to the one server that ``tests/api`` shares per run.

A module whose tests need agents, models or tool servers of their own adds them to the shared
server's configuration rather than starting a server of its own. Starting one costs seconds
of imports in every process it runs (``db init``, ``store init``, the backend, each worker);
adding a few agents to the shared one costs nothing.

A module registers a context manager at import time, with ``@contributes``. It starts what
its agents need -- an MCP server, say -- and yields a :class:`Contribution`: the TOML it adds
and the environment the server needs for it. ``tests/api/conftest.py`` enters every
registered one before it starts the shared server, and leaves them after it stops it.
Collection imports every module before any fixture runs, so the configuration is complete in
every process of a run, ``pytest-xdist``'s workers included, however the run was narrowed.

What a contribution may add is limited to what no other test can see: agents, models and tool
servers under names of its own. A test that changes what every agent does -- ``[work]``, the
number of workers -- still starts its own server.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Contribution:
    """TOML appended to the shared server's configuration, and the environment it needs."""

    config: str
    env: Mapping[str, str] = field(default_factory=dict)


Contributor = Callable[[], AbstractContextManager[Contribution]]

_CONTRIBUTORS: list[Contributor] = []


def contributes(contributor: Contributor) -> Contributor:
    """Register ``contributor`` with the shared server. Used as a decorator, under
    ``@contextmanager``."""
    _CONTRIBUTORS.append(contributor)
    return contributor


@contextmanager
def contributions() -> Iterator[Contribution]:
    """Every registered contribution entered, as one: their TOML joined and their
    environments merged. Each is left, in the reverse order, when this is."""
    with ExitStack() as stack:
        entered = [stack.enter_context(contributor()) for contributor in _CONTRIBUTORS]
        env: dict[str, str] = {}
        for each in entered:
            env.update(each.env)
        yield Contribution("".join(each.config for each in entered), env)
