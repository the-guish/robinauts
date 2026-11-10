# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestrator: flows, the runs started from them, and the orchestration engine that runs
them, neorc today (``docs/architecture/orchestration.md``).

A sibling of the controller. It may see the controller's contract; the controller sees nothing
of it. neorc is imported under ``adapters/neorc`` alone, so taking neorc out is taking out that
package and its wiring in ``cli.orchestration``.
"""
