"""SFTP: paramiko and the OpenSSH sftp client against the store's SSH server. Operations are the S3
operations they stand for (same authorization, encryption and objects); what S3 cannot express is refused."""

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import time

import paramiko
import pytest

from conftest import ADMIN, READER, _free_port, credentials_file


def wait_port(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"SFTP port {port}")


@pytest.fixture
def sftp_store(custom_store, tmp_path):
    pub = None
    if shutil.which("ssh-keygen"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "id")], check=True)
        pub = (tmp_path / "id.pub").read_text().strip()
    creds = credentials_file()
    if pub:
        creds.append({"access_key": "sshuser", "secret_key": "ssh-user-secret-value", "ssh_keys": [pub], "admin": True})
    (tmp_path / "creds.json").write_text(json.dumps(creds))
    port = _free_port()
    env = {"CAIRN_STORE_SFTP_ADDR": f"127.0.0.1:{port}", "CAIRN_STORE_CREDENTIALS_FILE": str(tmp_path / "creds.json"),
           "CAIRN_STORE_MASTER_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}
    st = custom_store(env)
    wait_port(port)
    st.client().create_bucket(Bucket="ftp")
    return st, port, tmp_path, pub


def connect(port, key=ADMIN):
    t = paramiko.Transport(("127.0.0.1", port))
    t.connect(username=key[0], password=key[1])
    return t, paramiko.SFTPClient.from_transport(t)


def test_files_and_directories(sftp_store):
    st, port, _, _ = sftp_store
    t, s = connect(port)
    assert "ftp" in s.listdir("/")
    s.mkdir("/ftp/in")
    with s.open("/ftp/in/data.csv", "w") as f:
        f.write(b"a,b\n" * 50_000)  # 200 KB in many SFTP writes
    s.putfo(io.BytesIO(b"tiny"), "/ftp/in/tiny.txt")
    attrs = {a.filename: a for a in s.listdir_attr("/ftp/in")}
    assert attrs["data.csv"].st_size == 200_000 and stat.S_ISREG(attrs["data.csv"].st_mode)
    assert stat.S_ISDIR(s.stat("/ftp/in").st_mode) and stat.S_ISDIR(s.stat("/ftp").st_mode)
    out = io.BytesIO()
    s.getfo("/ftp/in/data.csv", out)
    assert out.getvalue() == b"a,b\n" * 50_000
    with s.open("/ftp/in/data.csv", "r") as f:
        f.seek(100_000)
        assert f.read(8) == b"a,b\na,b\n"
    s.rename("/ftp/in/tiny.txt", "/ftp/in/renamed.txt")
    assert sorted(s.listdir("/ftp/in")) == ["data.csv", "renamed.txt"]
    with pytest.raises(OSError):
        s.rmdir("/ftp/in")  # not empty
    # Objects are the S3 API's, encrypted at rest like any other.
    s3 = st.client()
    got = s3.get_object(Bucket="ftp", Key="in/renamed.txt")
    assert got["Body"].read() == b"tiny" and got["ServerSideEncryption"] == "AES256"
    s.remove("/ftp/in/data.csv")
    s.remove("/ftp/in/renamed.txt")
    s.rmdir("/ftp/in")
    assert s.listdir("/ftp") == []
    with pytest.raises(FileNotFoundError):
        s.stat("/ftp/in/data.csv")
    t.close()


def test_what_s3_cannot_express_is_refused(sftp_store):
    _, port, _, _ = sftp_store
    t, s = connect(port)
    s.putfo(io.BytesIO(b"0123456789"), "/ftp/f.txt")
    with pytest.raises(OSError):
        s.open("/ftp/f.txt", "a")  # append
    with pytest.raises(OSError):
        s.open("/ftp/f.txt", "r+")  # in-place update
    f = s.open("/ftp/g.txt", "w")
    f.write(b"abc")
    f.seek(100)
    with pytest.raises(OSError):
        f.write(b"x")  # writes are sequential
        f.flush()
    with pytest.raises(OSError):
        s.chmod("/ftp/f.txt", 0o600)  # setstat
    with pytest.raises(OSError):
        s.symlink("/ftp/f.txt", "/ftp/link")
    s.putfo(io.BytesIO(b"x"), "/ftp/h.txt")
    with pytest.raises(OSError):
        s.rename("/ftp/f.txt", "/ftp/h.txt")  # no overwrite
    # No shell or exec, only the sftp subsystem.
    ch = t.open_session()
    with pytest.raises(paramiko.SSHException):
        ch.exec_command("ls")
    t.close()


def test_sign_in_and_authorization(sftp_store):
    st, port, _, _ = sftp_store
    with pytest.raises(paramiko.AuthenticationException):
        connect(port, (ADMIN[0], "wrong"))
    with pytest.raises(paramiko.AuthenticationException):
        connect(port, ("nobody", "x"))
    st.client().put_object(Bucket="ftp", Key="r.txt", Body=b"readable")
    t, s = connect(port, READER)  # read-only key
    out = io.BytesIO()
    s.getfo("/ftp/r.txt", out)
    assert out.getvalue() == b"readable"
    with pytest.raises(PermissionError):
        s.putfo(io.BytesIO(b"x"), "/ftp/w.txt")
    with pytest.raises(PermissionError):
        s.remove("/ftp/r.txt")
    t.close()
    metrics = __import__("requests").get(f"{st.endpoint}/-/metrics").text
    assert "cairn_store_sftp_auth_failures_total 2" in metrics


@pytest.mark.skipif(not shutil.which("sftp"), reason="needs the OpenSSH sftp client")
def test_openssh_client_with_a_public_key(sftp_store):
    st, port, tmp, pub = sftp_store
    (tmp / "up.bin").write_bytes(os.urandom(1_500_000))
    (tmp / "batch").write_text(f"put {tmp / 'up.bin'} /ftp/up.bin\nget /ftp/up.bin {tmp / 'down.bin'}\nls -l /ftp\n")
    r = subprocess.run(["sftp", "-b", str(tmp / "batch"), "-i", str(tmp / "id"), "-P", str(port), "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null", "-o", "PasswordAuthentication=no", "sshuser@127.0.0.1"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert (tmp / "down.bin").read_bytes() == (tmp / "up.bin").read_bytes()
    assert st.client().head_object(Bucket="ftp", Key="up.bin")["ContentLength"] == 1_500_000


def test_host_key_is_kept_across_restarts(sftp_store):
    st, port, _, _ = sftp_store
    t, _ = connect(port)
    before = t.get_remote_server_key().get_fingerprint()
    t.close()
    st.stop()
    st.start()
    wait_port(port)
    t, _ = connect(port)
    assert t.get_remote_server_key().get_fingerprint() == before
    t.close()
