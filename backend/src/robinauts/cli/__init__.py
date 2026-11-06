# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ``robinauts`` command, and what runs Robinauts: it builds the parts (``composition``),
starts the controller and the worker, in this process or its own, serves the HTTP server of
``web`` over them, and stops them in order. Nothing imports it."""
