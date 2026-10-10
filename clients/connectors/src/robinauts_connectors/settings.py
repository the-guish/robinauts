# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The settings every connector process shares, from the environment alone. A platform's own
settings are its connector's (``Connector.from_environment``)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

API_URL = "ROBINAUTS_API_URL"
"""The deployment's base URL, as this process reaches it."""

SECRET = "ROBINAUTS_CONNECTORS_SECRET"
"""The secret the deployment shares with its connectors: at least 32 characters. To be replaced
by a token minted for one connector."""

AGENT_ID = "ROBINAUTS_AGENT_ID"
"""The agent the platforms talk to."""

MODEL_ID = "ROBINAUTS_MODEL_ID"
"""Optional: a model of the deployment's; unset, the agent's default."""

LINKS_FILE = "CONNECTORS_LINKS_FILE"
"""Where the thread links are kept (``links.py``); ``state/links.json`` by default."""


@dataclass(frozen=True, slots=True)
class Settings:
    api_url: str
    secret: str
    agent_id: str
    model_id: str | None
    links_file: Path

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Self:
        """``ConfigError`` naming every problem at once: a variable missing, a URL that is not
        http(s), a secret too short to be one."""
        raise NotImplementedError
