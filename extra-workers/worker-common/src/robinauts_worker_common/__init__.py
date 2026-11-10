# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the extra workers share: their start, the GitHub token they may be handed, and cloning a
repository with it. A library, not a worker: each worker under ``extra-workers/`` that uses it
lists it as a dependency, and its image copies this folder (README.md)."""
