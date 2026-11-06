# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Minimal script entry point. Usage: python main.py

``run`` exits with the command's status itself, as the ``robinauts`` console
script an installation gets does: a command that failed must not leave a shell
believing it worked.
"""

from robinauts.cli.main import run

if __name__ == "__main__":
    run()
