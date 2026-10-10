# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The connectors installed: every entry point of the group ``robinauts.connectors``, this
distribution's and any other's."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from robinauts_connectors.connector import Connector
from robinauts_connectors.domain import Mode

GROUP = "robinauts.connectors"


def available() -> dict[str, type[Connector]]:
    """By name. An entry point whose name is not its class's ``name`` is refused, so that the
    command line, the route and the ids agree."""
    raise NotImplementedError


def load(names: Sequence[str], mode: Mode, environ: Mapping[str, str]) -> list[Connector]:
    """The connectors ``names`` asks for, each from its settings. ``ConfigError`` naming every
    problem at once: a name not installed (with the extra that installs it), a connector that
    cannot ``mode``, a connector's own settings."""
    raise NotImplementedError
