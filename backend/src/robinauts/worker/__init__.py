# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker: claims queued turns through a ``TurnBroker`` and runs them on the agent engines.

A sibling of ``controller``, not a part of it. Of the controller it imports the turn broker's
contract, ``controller.contract.broker``, and nothing else; of the rest of Robinauts,
``shared_domain``, ``web.contract.broker`` (the HTTP its broker speaks), and the engines' and
the tools' contracts. ``cli.composition`` builds it, and
alone imports it.
"""
