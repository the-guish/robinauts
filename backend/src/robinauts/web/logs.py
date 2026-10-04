# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Request values made safe to put in a log line, and what every line says it is about.

A value from a request may carry a line break or another control character, which would let
it forge a log line of its own (CWE-117). Every such value goes through ``loggable`` on its way
to the log, validated or not, so that no log line depends on a check made elsewhere.

With several processes behind a load balancer, a line names the process it came from, and the
conversation and the turn it is about, when it is about one (``Context``).
"""

from __future__ import annotations

import logging
import os
import re
import socket

from robinauts.controller.contract.context import WORK

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_LONGEST = 200


def loggable(value: str) -> str:
    """``value`` with its control characters replaced by ``?`` and cut to 200 characters."""
    return _CONTROL.sub("?", value[:_LONGEST])


FORMAT = "%(levelname)s:%(name)s:[%(work)s] %(message)s"
"""Python's own default, with the context in brackets."""


class Context(logging.Filter):
    """Adds ``work`` to each record: ``pod=<id>``, ``ROBINAUTS_WORKER_ID`` or the host and the
    process id, then what ``WORK`` holds."""

    def __init__(self) -> None:
        super().__init__()
        pod = os.environ.get("ROBINAUTS_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"
        self.pod = f"pod={loggable(pod)}"

    def filter(self, record: logging.LogRecord) -> bool:
        record.work = f"{self.pod} {WORK.get()}".rstrip()
        return True
