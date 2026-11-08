# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stand-in for neorc's command, for the tests of the orchestrator ``robinauts start`` runs
beside itself: it answers the four commands the server runs as neorc would, as far as the
server can tell, and writes what each did to the JSON-lines file ``FAKE_NEORC_LOG`` names.

- ``manager start --port P``: answers ``GET /health`` on ``127.0.0.1:P`` until SIGTERM, and
  ``GET /ui/`` with a page that says the query and the hash it was opened at, framed by the
  origins of its ``--frame-ancestors`` alone, as neorc's UI is (``'none'`` without one);
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


UI_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>neorc stand-in</title></head>
<body><p id="where">neorc stand-in</p>
<script>
function say() {
  document.getElementById("where").textContent = "neorc at " + location.search + location.hash;
}
say();
addEventListener("hashchange", say);
</script></body></html>
"""


def frame_ancestors(arguments: list[str]) -> str:
    """The policy's ``frame-ancestors`` the manager was started with, as neorc's would be."""
    origins = [arguments[at + 1] for at, a in enumerate(arguments) if a == "--frame-ancestors"]
    return " ".join(origins) or "'none'"


class Health(BaseHTTPRequestHandler):
    framed_by = "'none'"

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/ui/":
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header(
                "content-security-policy",
                "default-src 'none'; script-src 'unsafe-inline';"
                f" frame-ancestors {self.framed_by}",
            )
            self.end_headers()
            self.wfile.write(UI_PAGE)
            return
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
        Health.framed_by = frame_ancestors(arguments)
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
