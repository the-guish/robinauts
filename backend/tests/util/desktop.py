# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``robinauts desktop`` on a data directory of a test's own: the command a person runs, which
oneberth's launcher runs the backend, the workers and the window with. The window is the stand-in
browser of ``util/stand_in_browser.py`` unless a test names another. The processes of the
desktop are found by the data directory their environment names, and none outlives the test.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

import httpx

from util.stack import ROBINAUTS

STAND_IN = Path(__file__).with_name("stand_in_browser.py")
START_SECONDS = 180.0
"""How long a start may take on a loaded machine: each worker imports every engine first."""
STOP_SECONDS = 60.0
"""How long a stop may take: a worker waits ten seconds for its turns before it ends them."""


def fields(line: str) -> dict[str, str]:
    """The ``key=value`` words of a line of the stand-in's report."""
    return dict(word.split("=", 1) for word in line.split(" ") if "=" in word)


def environ_of(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    return dict(e.split("=", 1) for e in raw.decode(errors="replace").split("\0") if "=" in e)


def processes_with(variable: str, value: str) -> list[int]:
    """The processes whose environment sets ``variable`` to ``value``."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and environ_of(int(entry.name)).get(variable) == value:
            found.append(int(entry.name))
    return found


def open_files(pid: int) -> list[Path]:
    """The files the process holds open."""
    opened = []
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        try:
            opened.append(Path(os.readlink(fd)))
        except OSError:
            continue
    return opened


def appeared(
    variable: str, value: str, seconds: float, beside: Sequence[int] = ()
) -> dict[int, dict[str, str]]:
    """The processes whose environment sets ``variable`` to ``value``, each with the environment
    it showed, as soon as there is one beside ``beside``; none after ``seconds``.

    A process that becomes another program shows no environment for that instant: neorc's
    worker says ``ready`` and then becomes neorc's own command. One look can fall in it."""
    deadline = time.monotonic() + seconds
    while True:
        shown = {p: environ_of(p) for p in processes_with(variable, value) if p not in beside}
        found = {p: environ for p, environ in shown.items() if environ.get(variable) == value}
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(0.05)


def gone(pids: Sequence[int], seconds: float) -> bool:
    """Whether every one of ``pids`` has ended within ``seconds``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not [p for p in pids if Path(f"/proc/{p}").exists() and not _zombie(p)]:
            return True
        time.sleep(0.2)
    return False


def _zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1].startswith("Z")
    except (OSError, IndexError):
        return True


class Desktop:
    """The desktop of one test: its data directory, its configuration, its window's report."""

    def __init__(
        self, tmp_path: Path, config: str, environment: Mapping[str, str] | None = None
    ) -> None:
        self.data = tmp_path / "data"
        self.config = tmp_path / "robinauts.toml"
        self.config.write_text(config)
        self.report = tmp_path / "window.txt"
        self.report.touch()
        self.log = tmp_path / "desktop.log"
        self.browser = tmp_path / "browser"
        self.browser.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STAND_IN}" "$@"\n')
        self.browser.chmod(0o755)
        ui = tmp_path / "ui"
        ui.mkdir()
        (ui / "index.html").write_text("<!doctype html><title>Robinauts</title>")
        self.environment = {
            **os.environ,
            "ROBINAUTS_CONFIG": str(self.config),
            "ROBINAUTS_UI_DIR": str(ui),
            "STAND_IN_REPORT": str(self.report),
            # A worker imports the handlers from the checkout: it must leave nothing there.
            "PYTHONDONTWRITEBYTECODE": "1",
            **(environment or {}),
        }
        for variable in ("ROBINAUTS_DATABASE_URL", "ROBINAUTS_DATA_DIR", "ONEBERTH_CONFIG"):
            self.environment.pop(variable, None)
        self.launched: list[subprocess.Popen[bytes]] = []
        self._read = 0

    def launch(
        self, browser: str | None = None, arguments: Sequence[str] = ()
    ) -> subprocess.Popen[bytes]:
        """``robinauts desktop`` on the data directory, its output in the log."""
        command = [str(ROBINAUTS), "desktop", "--data-dir", str(self.data)]
        command += ["--browser", browser or str(self.browser)]
        command += [f"--browser-argument={a}" for a in arguments]
        with self.log.open("ab") as log:
            launched = subprocess.Popen(
                command,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
        self.launched.append(launched)
        return launched

    def heard(self, prefix: str, seconds: float = START_SECONDS) -> dict[str, str]:
        """The next line of the window's report that starts with ``prefix``, as its fields."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            lines = self.report.read_text().splitlines()
            for index in range(self._read, len(lines)):
                if lines[index].startswith(prefix):
                    self._read = index + 1
                    return {"line": lines[index], **fields(lines[index])}
            if self.launched and self.launched[-1].poll() not in (None, 0):
                raise AssertionError(f"the desktop ended: {self.log.read_text()[-3000:]}")
            time.sleep(0.2)
        raise AssertionError(f"the window said no {prefix!r}: {self.log.read_text()[-3000:]}")

    def ended(self, launched: subprocess.Popen[bytes], seconds: float = STOP_SECONDS) -> int:
        try:
            return launched.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            log = self.log.read_text()[-3000:]
            raise AssertionError(f"the desktop did not stop: {log}") from None

    def parts(self) -> dict[str, int]:
        """The desktop's processes by part: ``backend``, the window, and each worker's name."""
        found = {}
        for pid in processes_with("ONEBERTH_DATA_DIR", str(self.data)):
            environ = environ_of(pid)
            arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if "ONEBERTH_FRONT_PORT" in environ:
                found["backend"] = pid
            elif "ONEBERTH_WORKER_NAME" in environ:
                found[environ["ONEBERTH_WORKER_NAME"]] = pid
            elif any(a.startswith(b"--app=") for a in arguments) and not any(
                a.startswith(b"--type=") for a in arguments
            ):
                found["window"] = pid
        return found

    def left_behind(self) -> list[int]:
        return [p for p in processes_with("ONEBERTH_DATA_DIR", str(self.data)) if not _zombie(p)]

    @property
    def front_url(self) -> str:
        port = json.loads((self.data / "launcher.json").read_text())["front_port"]
        return f"http://127.0.0.1:{port}"

    @contextmanager
    def client(self, opened: Mapping[str, str]) -> Iterator[httpx.Client]:
        """A client with the window's session cookie, and the ``Origin`` a browser sends."""
        headers = {"cookie": opened["cookie"], "origin": opened["origin"]}
        with httpx.Client(base_url=opened["origin"], headers=headers, timeout=60) as made:
            yield made

    def stop_all(self) -> None:
        """Kill whatever of this desktop still runs: the launchers and every part."""
        for launched in self.launched:
            if launched.poll() is None:
                launched.kill()
                launched.wait()
        for pid in self.left_behind():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
