# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a worker in a process of its own reads of its supervisor's variables
(``cli/worker_process.py``): the launcher's, and a server's where they are absent."""

from __future__ import annotations

import pytest
from oneberth import launcher

from robinauts.cli import worker_process
from robinauts.shared_domain.domain import StorageConfig, StorageKind


@pytest.mark.parametrize(
    ("environ", "url", "name", "storage"),
    [
        (
            {
                "ONEBERTH_WORKER_URL": "http://127.0.0.1:9001",
                "ONEBERTH_WORKER_NAME": "gpu",
                "ONEBERTH_DATA_DIR": "/data",
                "ROBINAUTS_DATABASE_URL": "postgresql://elsewhere",
                "ROBINAUTS_WORKER_BROKER_URL": "http://elsewhere/robinauts",
                "ROBINAUTS_WORKER_NAME": "other",
            },
            "http://127.0.0.1:9001/robinauts",
            "gpu",
            StorageConfig(StorageKind.LOCAL, path="/data"),
        ),
        (
            {"ROBINAUTS_WORKER_BROKER_URL": "http://127.0.0.1:8001/robinauts"},
            "http://127.0.0.1:8001/robinauts",
            "default",
            StorageConfig(StorageKind.IN_PROCESS),
        ),
        (
            {
                "ROBINAUTS_WORKER_BROKER_URL": "http://127.0.0.1:8001/robinauts",
                "ROBINAUTS_WORKER_NAME": "gpu",
                "ROBINAUTS_DATABASE_URL": "postgresql://db",
            },
            "http://127.0.0.1:8001/robinauts",
            "gpu",
            StorageConfig(StorageKind.POSTGRES, url="postgresql://db"),
        ),
    ],
    ids=["the launcher's", "a server's", "a server's on PostgreSQL"],
)
def test_a_worker_reads_the_launcher_s_variables_before_a_server_s(
    environ: dict[str, str], url: str, name: str, storage: StorageConfig
) -> None:
    assert worker_process.broker_url(environ) == url
    assert worker_process.worker_name(environ) == name
    assert worker_process.worker_storage(environ) == storage


def test_the_worker_s_names_of_the_launcher_s_variables_are_oneberth_s() -> None:
    # The worker's module imports nothing of oneberth: the extra workers' images leave it out.
    assert worker_process.WORKER_URL_VARIABLE == launcher.WORKER_URL
    assert worker_process.LAUNCHED_NAME_VARIABLE == launcher.WORKER_NAME
    assert worker_process.LAUNCHED_DATA_DIR_VARIABLE == launcher.DATA_DIR
    assert worker_process.LAUNCHER_PID_VARIABLE == launcher.LAUNCHER_PID
    assert worker_process.TOKEN_FILE_VARIABLE == launcher.TOKEN_FILE
