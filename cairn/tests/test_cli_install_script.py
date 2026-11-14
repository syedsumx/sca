"""deploy/cli/install.sh, run by sh against an HTTPS mirror of a release (CAIRN_DOWNLOAD_URL): it installs the binary
for this computer after checking SHA256SUMS, and refuses one whose checksum does not match."""

import hashlib
import http.server
import os
import platform
import shutil
import ssl
import subprocess
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "cli", "install.sh")
pytestmark = pytest.mark.skipif(platform.system() not in ("Linux", "Darwin") or not shutil.which("curl"),
                                reason="the install script is for Linux and macOS, with curl")


@pytest.fixture
def mirror(tmp_path):
    from test_trino_e2e import write_tls

    os.makedirs(tmp_path / "tls")
    write_tls(tmp_path / "tls")
    site = tmp_path / "site"
    site.mkdir()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
    asset = f"cairn-{'macos' if platform.system() == 'Darwin' else 'linux'}-{arch}"
    (site / asset).write_text('#!/bin/sh\necho "cairn 9.9.9"\n')
    digest = hashlib.sha256((site / asset).read_bytes()).hexdigest()
    (site / "SHA256SUMS").write_text(f"{digest}  {asset}\n{'0' * 64}  cairn-windows-x86_64.exe\n")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(site), **kw)

        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tmp_path / "tls" / "tls.pem"))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}", site, asset, str(tmp_path / "tls" / "ca.pem")
    server.shutdown()


def install(url, ca, target):
    env = {**os.environ, "CAIRN_DOWNLOAD_URL": url, "CURL_CA_BUNDLE": ca, "CAIRN_INSTALL_DIR": str(target),
           "PATH": os.environ["PATH"]}
    return subprocess.run(["sh", SCRIPT], env=env, capture_output=True, text=True, timeout=120)


def test_installs_the_checked_binary_and_refuses_a_tampered_one(mirror, tmp_path):
    url, site, asset, ca = mirror
    r = install(url, ca, tmp_path / "bin")
    assert r.returncode == 0, r.stderr
    assert "Installed cairn 9.9.9" in r.stdout and os.access(tmp_path / "bin" / "cairn", os.X_OK)
    (site / asset).write_text('#!/bin/sh\necho "something else"\n')            # not what SHA256SUMS says
    r = install(url, ca, tmp_path / "bin2")
    assert r.returncode == 1 and "checksum mismatch" in r.stderr and not (tmp_path / "bin2" / "cairn").exists()
    r = install(url.replace("https://", "http://"), ca, tmp_path / "bin3")
    assert r.returncode == 1 and "must be https" in r.stderr
