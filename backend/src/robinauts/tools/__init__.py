# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Robinauts' own tools: run in this process, and given to the model like any other tool.

Each tool is a package of its own here, and holds everything about itself. Its ``load()``
returns a ``ToolProvider``, and ``controller.composition`` calls it from a fixed list: the one
place that names a tool. Everything else sees ``tools.contract`` alone.
"""
