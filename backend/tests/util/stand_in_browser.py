# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A browser in Chrome's place, for the tests of the desktop: the window the launcher opens.

    python stand_in_browser.py --user-data-dir=PROFILE --app=URL [...]

It opens ``URL`` with a jar of cookies, as the window does, and appends what it got to the file
``STAND_IN_REPORT`` names: its process id, the origin, where it landed and the session cookie.
It runs until SIGTERM, which closes the window. A second start on the same profile hands its
address to the first one and ends, as Chrome does: it reports the address, unopened.
"""

from __future__ import annotations

import os
import signal
import sys
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlsplit


def report(text: str) -> None:
    with Path(os.environ["STAND_IN_REPORT"]).open("a") as to:
        to.write(f"{text}\n")


def argument(name: str) -> str:
    return next(a for a in sys.argv[1:] if a.startswith(name)).removeprefix(name)


def running(pid_file: Path) -> bool:
    try:
        os.kill(int(pid_file.read_text()), 0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return False
    return True


def main() -> None:
    profile, url = Path(argument("--user-data-dir=")), argument("--app=")
    pid_file = profile / "stand-in.pid"
    if running(pid_file):
        report(f"forwarded {url}")
        return
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    pid_file.write_text(str(os.getpid()))
    jar = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)
    )
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
    with opener.open(url, timeout=30) as page:
        landed = page.geturl().removeprefix(origin)
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
    report(f"opened pid={os.getpid()} origin={origin} at={landed} cookie={cookie}")
    signal.sigwait({signal.SIGTERM})
    pid_file.unlink()


if __name__ == "__main__":
    main()
