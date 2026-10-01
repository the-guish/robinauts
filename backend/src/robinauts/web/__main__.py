# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Start the web shell locally: ``python -m robinauts.web``, on 127.0.0.1:8000, no sign-in.

The interface is ``frontend/dist`` of this checkout when it is built, or the directory
``ROBINAUTS_UI_DIR`` names. The controller runs on an empty configuration and in-memory storage.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from robinauts.controller.contract.domain import Config, StorageConfig, StorageKind
from robinauts.controller.controller import RobinautsController
from robinauts.web.app import create_app

REPO = Path(__file__).resolve().parents[4]


def main() -> None:
    named = os.environ.get("ROBINAUTS_UI_DIR")
    ui_dir = Path(named) if named else REPO / "frontend" / "dist"
    controller = RobinautsController(
        Config(), storage=StorageConfig(StorageKind.IN_MEMORY), secret_for=os.environ.get
    )
    uvicorn.run(create_app(controller, ui_dir=ui_dir), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
