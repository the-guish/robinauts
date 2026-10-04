# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the work in hand is about, for the log lines written while it runs."""

from __future__ import annotations

from contextvars import ContextVar

WORK: ContextVar[str] = ContextVar("robinauts_work", default="")
"""Set by the task that runs a turn, as ``conversation=<id> turn=<id>``; empty elsewhere."""
