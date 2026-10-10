# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The serve runner: ``robinauts-connectors serve teams whatsapp --port 8080``.

One HTTP application, behind the company's HTTPS proxy: ``/<name>`` for each connector, handed to
its ``Webhook`` as a ``WebhookRequest`` with the raw body, and ``/health``. Stateless, so it scales
out. The one module that imports FastAPI and uvicorn, installed by the ``webhook`` extra; a
connector's handler is plain Python, so that a framework is chosen once, here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from robinauts_connectors.bridge import Bridge
from robinauts_connectors.connector import Connector


def application(connectors: Sequence[Connector], bridge: Bridge) -> Any:
    """The ASGI application. ``Any`` because FastAPI is imported when this is called, and only
    then: a process that only connects need not have it installed."""
    raise NotImplementedError


def serve(connectors: Sequence[Connector], bridge: Bridge, *, host: str, port: int) -> None:
    raise NotImplementedError
