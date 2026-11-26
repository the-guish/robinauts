# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where the server a worker serves is: on this machine, or elsewhere. A worker that runs on one
person's own credential, a subscription login, serves that person's own instance alone, and the
broker URL the server hands it says where that instance is."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

LOCAL_HOSTS = frozenset({"localhost", "host.docker.internal", "host-gateway"})
"""The names of this machine as a container on it sees it."""


def is_local(url: str) -> bool:
    """Whether the server is on this machine: loopback, a private address, Docker's name for the
    host, or a unix socket."""
    parts = urlsplit(url)
    if parts.scheme in ("unix", "http+unix"):
        return True
    host = (parts.hostname or "").lower()
    if host in LOCAL_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local
