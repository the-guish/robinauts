# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``robinauts-connectors``, the one command:

    robinauts-connectors connect <platform>...                  no public URL, one replica
    robinauts-connectors serve <platform>... [--host] [--port]  public HTTPS, scales out
    robinauts-connectors list                                   what is installed, and its modes

``connect`` and ``serve`` read the settings (``settings.py``, then each connector's), probe the
deployment, and refuse to start, saying why, before anything connects or binds.
"""

from __future__ import annotations

from collections.abc import Sequence


def run(argv: Sequence[str] | None = None) -> None:
    raise NotImplementedError
