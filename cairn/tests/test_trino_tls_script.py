"""deploy/trino/make-tls.sh (Compose's one-off Trino certificate), run for real with keytool: the keystore password
never appears on a command line, where any process in the container (``ps``, /proc/<pid>/cmdline) could read it."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "keystore-pass-" + "p" * 20


@pytest.mark.skipif(shutil.which("keytool") is None, reason="needs keytool (a JDK)")
def test_the_keystore_password_never_reaches_a_command_line(tmp_path):
    out = tmp_path / "tls"
    out.mkdir()
    with open(os.path.join(ROOT, "deploy/trino/make-tls.sh")) as f:
        script = f.read()
    assert "d=/etc/trino/tls\n" in script
    (tmp_path / "make-tls.sh").write_text(script.replace("d=/etc/trino/tls\n", f"d={out}\n"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "keytool"                     # records every command line, then runs the real keytool
    shim.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >>"{tmp_path}/argv"\nexec {shutil.which("keytool")} "$@"\n')
    shim.chmod(0o755)
    (bin_dir / "chown").write_text("#!/bin/sh\nexit 0\n")   # the Trino user's uid: root's to give, not this test's
    (bin_dir / "chown").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CAIRN_TRINO_KEYSTORE_PASSWORD": PASSWORD}
    r = subprocess.run(["/bin/sh", str(tmp_path / "make-tls.sh")], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    argv = (tmp_path / "argv").read_text()
    assert "-genkeypair" in argv and "-exportcert" in argv
    assert PASSWORD not in argv
    assert PASSWORD not in r.stdout + r.stderr
    assert (out / "ca.pem").read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert oct((out / "keystore.p12").stat().st_mode & 0o777) == "0o640"
    # The keystore opens with the password (Trino's http-server.https.keystore.key), and not without it.
    listing = subprocess.run(["keytool", "-list", "-keystore", str(out / "keystore.p12"), "-storetype", "PKCS12",
                              "-storepass:env", "CAIRN_TRINO_KEYSTORE_PASSWORD"], env=env, capture_output=True, text=True)
    assert listing.returncode == 0 and "trino" in listing.stdout, listing.stderr
