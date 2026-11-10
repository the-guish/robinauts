# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An extra worker's process, ``python -m robinauts_worker_common``: the base worker's command.

It checks what the backend's worker module, ``robinauts.cli.worker_process``, cannot know about
an extra worker, the GitHub token it may be handed (github.py), and then runs that module. What
it refuses, it says all at once on its standard error and exits 2, before the secret is read.
A worker with checks of its own runs ``problems_of`` beside them, as the Claude Code worker does.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from robinauts.cli.worker_process import main as run_worker
from robinauts_worker_common.github import token_problems


def problems_of(environ: Mapping[str, str]) -> list[str]:
    """Everything shared by the extra workers that keeps one from starting, one line each."""
    return token_problems(environ)


def main() -> int:
    problems = problems_of(os.environ)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 2
    run_worker()
    return 0


if __name__ == "__main__":
    sys.exit(main())
