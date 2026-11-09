# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stand-in for neorc's command, for the tests of the orchestrator ``robinauts start`` runs
beside itself: it answers the four commands the server runs as neorc would, as far as the
server can tell, and writes what each did to the JSON-lines file ``FAKE_NEORC_LOG`` names.

- ``manager start --port P``: answers on ``127.0.0.1:P`` until SIGTERM: ``GET /health``, and,
  for the Flows page's proxy, the few routes of ``Manager``, shaped as neorc's are;
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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

stopping = threading.Event()


def record(**entry: Any) -> None:
    with open(os.environ["FAKE_NEORC_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


def option(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]


UI_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline';"
    " img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none';"
    " form-action 'self'; frame-ancestors 'none'"
)
"""neorc's policy for its UI, word for word (``neorc/manager/_ui.py``)."""

UI_PAGE = b"<!doctype html><title>neorc</title><script src=./assets/index.js></script>"

EVENT_DELAY_SECONDS = 0.6
"""How long ``/events`` holds its answer between its two events, like a long poll."""


class Manager(BaseHTTPRequestHandler):
    """``/health``; ``/`` to ``/ui/``, as neorc sends it; ``/ui/``, neorc's page with its
    headers and a cookie neorc has no business setting; ``/runs``, a list; ``/events``, two
    events ``EVENT_DELAY_SECONDS`` apart in a chunked answer; ``POST /flows/<name>/runs``,
    the run it started, with the body it was sent. Every request is written to the log, with
    the headers it came with."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path != "/health":
            record(step="manager-request", method="GET", path=self.path, headers=self._headers())
        if self.path == "/health":
            self._send(200, b"ok", "text/plain")
        elif self.path == "/":
            self.send_response(307)
            self.send_header("location", "/ui/")
            self.send_header("content-length", "0")
            self.end_headers()
        elif self.path == "/ui/":
            extra = {
                "content-security-policy": UI_POLICY,
                "x-content-type-options": "nosniff",
                "referrer-policy": "same-origin",
                "cache-control": "no-cache",
                "set-cookie": "neorc=1; Path=/",
            }
            self._send(200, UI_PAGE, "text/html; charset=utf-8", extra)
        elif self.path.startswith("/runs"):
            self._send(200, json.dumps({"items": [], "next": None}).encode(), "application/json")
        elif self.path.startswith("/events"):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for piece in (b'[{"sequence": 1}', b', {"sequence": 2}]'):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                self.wfile.flush()
                time.sleep(EVENT_DELAY_SECONDS)
            self.wfile.write(b"0\r\n\r\n")
        else:
            self._send(404, b'{"error": "NotFound", "detail": "no such route"}', "application/json")

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode()
        record(
            step="manager-request",
            method="POST",
            path=self.path,
            headers=self._headers(),
            body=body,
        )
        run = {"id": "run-1", "flow": self.path.split("/")[2], "inputs": json.loads(body)}
        self._send(201, json.dumps(run).encode(), "application/json")

    def _headers(self) -> dict[str, str]:
        return {name.lower(): value for name, value in self.headers.items()}

    def _send(
        self, status: int, body: bytes, media_type: str, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("content-type", media_type)
        self.send_header("content-length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

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
        server = ThreadingHTTPServer(("127.0.0.1", int(option(arguments, "--port"))), Manager)
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
