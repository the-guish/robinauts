# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stand-in for neorc's command, for the tests of the orchestrator ``robinauts start`` runs
beside itself: it answers the four commands the server runs as neorc would, as far as the
server can tell, and writes what each did to the JSON-lines file ``FAKE_NEORC_LOG`` names.

- ``manager start --port P``: answers ``GET /health`` on ``127.0.0.1:P`` until SIGTERM;
- ``flows upload DIR``: says it stored each flow file in ``DIR`` and exits;
- ``scheduler start``: waits for SIGTERM;
- ``worker start --code-location DIR``: opens the guessing flow's guesser conversation with
  the handlers in ``DIR``, as neorc's worker would once a run is started, then waits for
  SIGTERM.

Run as ``python fake_neorc.py <command> ...``; it imports nothing of Robinauts."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

stopping = threading.Event()


def record(**entry: Any) -> None:
    with open(os.environ["FAKE_NEORC_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


def option(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]


class Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def until_stopped(step: str) -> None:
    stopping.wait()
    record(step=step, stopped=True)


def main(arguments: list[str]) -> int:
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    step = arguments[0]
    record(step=step, arguments=arguments)
    if step == "manager":
        server = ThreadingHTTPServer(("127.0.0.1", int(option(arguments, "--port"))), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        until_stopped(step)
        server.shutdown()
    elif step == "flows":
        for flow in sorted(Path(arguments[2]).glob("*.yaml")):
            print(f"stored {flow.stem}")
    elif step == "scheduler":
        until_stopped(step)
    elif step == "worker":
        sys.path.insert(0, option(arguments, "--code-location"))
        import robinauts_tasks

        checked = ("ECHO_API_KEY", "ROBINAUTS_CONFIG", "ROBINAUTS_API_TOKEN")
        record(
            step=step,
            result=robinauts_tasks.open_guesser(),
            api_url=os.environ["ROBINAUTS_API_URL"],
            visible=sorted(k for k in checked if k in os.environ),
        )
        until_stopped(step)
    else:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
