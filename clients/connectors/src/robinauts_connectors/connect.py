# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The connect runner: ``robinauts-connectors connect slack telegram``.

Every connector's ``connect`` side by side, in one process with no public URL, until ``SIGTERM`` or
``SIGINT``. One connection that cannot be made stops them all, so that a half-working process is
not taken for a working one; a restart is the container's. Usually one replica: most platforms
admit one connection per bot.
"""

from __future__ import annotations

from collections.abc import Sequence

from robinauts_connectors.bridge import Bridge
from robinauts_connectors.connector import Connector


async def connect(connectors: Sequence[Connector], bridge: Bridge) -> None:
    raise NotImplementedError
