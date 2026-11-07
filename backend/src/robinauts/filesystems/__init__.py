# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The file systems agents keep files on: one port, and an adapter per kind of storage.

``contract`` is the port and what crosses it: a folder, the paths inside it, the entries it
holds and the refusals. ``local`` is the one adapter so far, a directory on this machine. An
adapter for a bucket or a drive is another package here, keeping the same contract
(``docs/specs/files.md``). Nothing here imports anything else of Robinauts, and only
``cli.composition`` names an adapter: the tools and the worker see the contract alone.
"""
