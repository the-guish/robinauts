# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Minimal script entry point. Usage: python main.py

``SystemExit`` with what the command answered, exactly as the ``robinauts``
console script an installation gets does: a command that failed must not leave
a shell believing it worked.
"""

from robinauts.legacy.cli import run

if __name__ == "__main__":
    raise SystemExit(run())
