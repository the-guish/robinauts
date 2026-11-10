# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A local stand-in for the GitHub server: git's own ``http-backend`` behind HTTPS, with a
certificate made for the test, which asks for the token when it is told to and records the
header of every request. Its repositories are bare ones under a folder of the test's, made with
``repository``; each takes pushes."""

from __future__ import annotations

import os
import ssl
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from robinauts_worker_common.github import authorization

QUIET_GIT = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
"""git as the test runs it beside the stand-in: no configuration of the machine's."""


@dataclass
class Server:
    """The stand-in: its URL, its certificate, where its repositories are, and what it was
    sent."""

    url: str
    certificate: Path
    root: Path
    token: str | None = None
    """The token it asks for; None for public repositories."""
    seen: list[str | None] = field(default_factory=list)
    """The Authorization header of each request, None where there was none."""

    def environ(self, **more: str) -> dict[str, str]:
        """A worker's environment: what git needs to trust the stand-in, and ``more``."""
        return {"PATH": os.environ["PATH"], "GIT_SSL_CAINFO": str(self.certificate), **more}


def git(*arguments: str, cwd: Path) -> str:
    environment = {**os.environ, **QUIET_GIT}
    ran = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, env=environment, text=True
    )
    return ran.stdout


def repository(server: Server, name: str, *, branch: str = "main") -> Path:
    """A bare repository ``name`` (``owner/repo``) on the stand-in, with one commit, a
    ``README.md`` saying hello, on ``branch``, its default, and a branch ``other``. It takes
    pushes."""
    work = server.root / "work" / name
    work.mkdir(parents=True)
    git("init", "--quiet", f"--initial-branch={branch}", ".", cwd=work)
    (work / "README.md").write_text("hello\n")
    git("add", "README.md", cwd=work)
    git("commit", "--quiet", "-m", "first", cwd=work)
    git("branch", "other", cwd=work)
    bare = server.root / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "--quiet", "--bare", str(work), str(bare), cwd=server.root)
    git("config", "http.receivepack", "true", cwd=bare)
    return bare


def commit(server: Server, name: str, path: str, text: str) -> None:
    """A new commit on the default branch of ``name``, made outside the stand-in."""
    work = server.root / "work" / name
    git("pull", "--quiet", str(server.root / f"{name}.git"), "HEAD", cwd=work)
    (work / path).write_text(text)
    git("add", path, cwd=work)
    git("commit", "--quiet", "-m", f"add {path}", cwd=work)
    git("push", "--quiet", str(server.root / f"{name}.git"), "HEAD", cwd=work)


def _certificate(folder: Path) -> tuple[Path, Path]:
    key, certificate = folder / "key.pem", folder / "certificate.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(certificate),
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return key, certificate


def _handler(server: Server) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def do_GET(self) -> None:
            self.serve()

        def do_POST(self) -> None:
            self.serve()

        def serve(self) -> None:
            header = self.headers.get("Authorization")
            server.seen.append(header)
            if server.token is not None and header != authorization(server.token):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="GitHub"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            environment = {
                "PATH": os.environ["PATH"],
                "GIT_PROJECT_ROOT": str(server.root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "REQUEST_METHOD": self.command,
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                "REMOTE_ADDR": "127.0.0.1",
            }
            ran = subprocess.run(
                ["git", "http-backend"], input=body, env=environment, capture_output=True
            )
            head, _, payload = ran.stdout.partition(b"\r\n\r\n")
            headers = [line.split(":", 1) for line in head.decode().split("\r\n") if line]
            status = next((int(v.split()[0]) for k, v in headers if k == "Status"), 200)
            self.send_response(status)
            for name, value in headers:
                if name != "Status":
                    self.send_header(name, value.strip())
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


@contextmanager
def serving(root: Path, tls: Path) -> Iterator[Server]:
    """The stand-in, serving the repositories under ``root``, with its certificate in ``tls``."""
    key, certificate = _certificate(tls)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server = Server("", certificate, root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(server))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    server.url = f"https://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        httpd.shutdown()
        httpd.server_close()
