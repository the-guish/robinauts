# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Start the web shell locally: ``python -m robinauts.web``, on 127.0.0.1:8000, no sign-in.

The configuration is the TOML file ``ROBINAUTS_CONFIG`` names. The interface is
``frontend/dist`` of this checkout when it is built, or the directory ``ROBINAUTS_UI_DIR``
names. Storage is in memory.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from robinauts.controller.composition import build, load
from robinauts.controller.contract.domain import StorageConfig, StorageKind
from robinauts.web.app import create_app

REPO = Path(__file__).resolve().parents[4]


def main() -> None:
    named = os.environ.get("ROBINAUTS_UI_DIR")
    ui_dir = Path(named) if named else REPO / "frontend" / "dist"
    config, secret_for = load(Path(os.environ["ROBINAUTS_CONFIG"]), os.environ)
    controller = build(config, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for=secret_for)
    uvicorn.run(create_app(controller, ui_dir=ui_dir), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
