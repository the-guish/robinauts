# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A clone, by the real git, from a local stand-in for the GitHub server: git's own
``http-backend`` behind HTTPS with a certificate made for the test, which asks for the token
when it is told to. What it checks: the token reaches the server in the header and nowhere
else, git asks no one for a password, and a refusal never repeats the token.

Skipped where git or openssl is missing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from robinauts_worker_common.clone import CloneError, clone, main
from robinauts_worker_common.github import GitHubError, authorization

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("openssl")), reason="needs git and openssl"
)

TOKEN = "github_pat_11ABCDEFG_made0by0the0person"
QUIET_GIT = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


@dataclass
class Server:
    """The stand-in: its URL, its certificate, and what it was sent."""

    url: str
    certificate: Path
    token: str | None = None
    """The token it asks for; None for a public repository."""
    seen: list[str | None] = field(default_factory=list)
    """The Authorization header of each request, None where there was none."""


def _git(*arguments: str, cwd: Path) -> None:
    environment = {**os.environ, **QUIET_GIT}
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, env=environment)


def _repositories(root: Path) -> None:
    """``octo/hello``, with one commit on ``main`` and a branch ``other``, as a bare
    repository under ``root``."""
    work = root / "work"
    work.mkdir()
    _git("init", "--quiet", "--initial-branch=main", ".", cwd=work)
    (work / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=work)
    _git("commit", "--quiet", "-m", "first", cwd=work)
    _git("branch", "other", cwd=work)
    (root / "octo").mkdir()
    _git("clone", "--quiet", "--bare", str(work), str(root / "octo" / "hello.git"), cwd=root)


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


def _handler(root: Path, server: Server) -> type[BaseHTTPRequestHandler]:
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
                "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1",
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


@pytest.fixture(scope="module")
def stand_in(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    root = tmp_path_factory.mktemp("github")
    _repositories(root)
    key, certificate = _certificate(tmp_path_factory.mktemp("tls"))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server = Server("", certificate)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(root, server))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    server.url = f"https://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield server
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def server(stand_in: Server) -> Iterator[Server]:
    stand_in.seen.clear()
    stand_in.token = None
    yield stand_in


def environ(server: Server, **more: str) -> dict[str, str]:
    """The worker's environment: what git needs to trust the stand-in, and ``more``."""
    return {"PATH": os.environ["PATH"], "GIT_SSL_CAINFO": str(server.certificate), **more}


def run(server: Server, destination: Path, **options) -> Path:
    environment = options.pop("environ", None) or environ(server)
    return asyncio.run(
        clone("octo/hello", destination, server=server.url, environ=environment, **options)
    )


def test_a_public_repository_is_cloned_without_a_token(server: Server, tmp_path: Path) -> None:
    target = run(server, tmp_path / "hello")
    assert (target / "README.md").read_text() == "hello\n"
    assert server.seen
    assert set(server.seen) == {None}


def test_the_token_goes_in_the_header_and_nowhere_else(server: Server, tmp_path: Path) -> None:
    server.token = TOKEN
    target = run(server, tmp_path / "hello", environ=environ(server, GITHUB_TOKEN=TOKEN))
    assert (target / "README.md").exists()
    assert set(server.seen) == {authorization(TOKEN)}
    written = b"".join(path.read_bytes() for path in (target / ".git").rglob("*") if path.is_file())
    assert TOKEN.encode() not in written
    assert authorization(TOKEN).encode() not in written


def test_a_private_repository_without_a_token_fails_at_once(server: Server, tmp_path: Path) -> None:
    server.token = TOKEN
    with pytest.raises(CloneError) as refused:
        run(server, tmp_path / "hello", timeout=30)
    assert "no GITHUB_TOKEN was handed to this worker" in str(refused.value)
    assert not (tmp_path / "hello").exists()


def test_a_refused_token_is_not_repeated(server: Server, tmp_path: Path) -> None:
    server.token = "github_pat_another"
    with pytest.raises(CloneError) as refused:
        run(server, tmp_path / "hello", environ=environ(server, GITHUB_TOKEN=TOKEN), timeout=30)
    assert TOKEN not in str(refused.value)
    assert "the server refused the token in GITHUB_TOKEN" in str(refused.value)


def test_a_branch_and_a_depth(server: Server, tmp_path: Path) -> None:
    target = run(server, tmp_path / "hello", branch="other", depth=1)
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target, capture_output=True, text=True
    )
    assert head.stdout.strip() == "other"
    assert (target / ".git" / "shallow").exists()


def test_a_destination_that_holds_something_is_refused(server: Server, tmp_path: Path) -> None:
    (tmp_path / "hello").mkdir()
    (tmp_path / "hello" / "mine").write_text("x")
    with pytest.raises(GitHubError, match="already holds something"):
        run(server, tmp_path / "hello")
    assert server.seen == []
    (tmp_path / "empty").mkdir()
    assert (run(server, tmp_path / "empty") / "README.md").exists()


def test_the_command_line(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("GIT_SSL_CAINFO", str(server.certificate))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    target = tmp_path / "hello"
    assert main(["octo/hello", str(target), "--server", server.url]) == 0
    assert capsys.readouterr().out.strip() == str(target)
    assert main(["gitlab.com/octo/hello", str(tmp_path / "x"), "--server", server.url]) == 1
    assert "is not a repository on" in capsys.readouterr().err
