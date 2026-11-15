# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``python -m robinauts.cli``: the ``robinauts`` command, as this interpreter runs it; ``start``
when no command is named."""

import sys

from robinauts.cli.main import run

run(sys.argv[1:] or ["start"])
