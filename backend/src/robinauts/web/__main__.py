# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Start the web shell locally: ``python -m robinauts.web``, on 127.0.0.1:8000, no sign-in.

The interface is ``frontend/dist`` of this checkout when it is built, or the directory
``ROBINAUTS_UI_DIR`` names. The controller runs one agent on the echo engine, in-memory storage.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from robinauts.controller.contract.domain import (
    AgentConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    StorageConfig,
    StorageKind,
)
from robinauts.controller.controller import RobinautsController
from robinauts.web.app import create_app

REPO = Path(__file__).resolve().parents[4]

# The echo engine reads no key: the provider is there because a model names one.
CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", model="echo", engine="echo")
    },
)


def main() -> None:
    named = os.environ.get("ROBINAUTS_UI_DIR")
    ui_dir = Path(named) if named else REPO / "frontend" / "dist"
    controller = RobinautsController(
        CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for=os.environ.get
    )
    uvicorn.run(create_app(controller, ui_dir=ui_dir), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
