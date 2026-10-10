# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Chat platforms reaching a Robinauts deployment through its HTTP API.

A connector receives a platform's messages and shows the answers there; the bridge decides who may
ask, which conversation a message continues, and when its turn runs; the API client asks the
deployment. ``connector.py`` is the contract between a platform and the rest
(docs/clients/connectors/working-notes/warm-up/plan.md).

A stub: the classes and their contract are here, and their behaviour raises
``NotImplementedError``.
"""
