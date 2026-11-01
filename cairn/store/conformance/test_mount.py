"""cairn-store mount: a bucket as a FUSE directory, exercised with ordinary file operations. Needs /dev/fuse
and the right to mount (root, or fusermount3); skipped otherwise."""

import errno
import hashlib
import os
import shutil
import subprocess
import time

import pytest

from conftest import ADMIN, READER


def can_mount():
    if not os.path.exists("/dev/fuse"):
        return False
    return os.geteuid() == 0 or bool(shutil.which("fusermount3") or shutil.which("fusermount"))


pytestmark = pytest.mark.skipif(not can_mount(), reason="needs FUSE (/dev/fuse and mount rights)")


def unmount(path):
    for cmd in (["fusermount3", "-u", path], ["fusermount", "-u", path], ["umount", path]):
        if shutil.which(cmd[0]) and subprocess.run(cmd, capture_output=True).returncode == 0:
            return


@pytest.fixture
def mounted(store, binary, tmp_path, request):
    def make(target, key=ADMIN, *flags):
        mnt = tmp_path / f"mnt{len(os.listdir(tmp_path))}"
        mnt.mkdir()
        env = {**os.environ, "CAIRN_STORE_ENDPOINT": store.endpoint, "CAIRN_STORE_ACCESS_KEY": key[0],
               "CAIRN_STORE_SECRET_KEY": key[1], "CAIRN_STORE_LOG_LEVEL": "error"}
        proc = subprocess.Popen([binary, "mount", target, str(mnt), *flags], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while not os.path.ismount(mnt):
            if proc.poll() is not None or time.time() > deadline:
                raise RuntimeError(f"mount failed: {proc.stderr.read().decode()}")
            time.sleep(0.1)
        request.addfinalizer(lambda: (unmount(str(mnt)), proc.wait(timeout=10)))
        return mnt
    return make


def test_files_through_the_mount(s3, bucket, mounted):
    s3.put_object(Bucket=bucket, Key="docs/hello.txt", Body=b"hello from S3\n")
    m = mounted(bucket)
    assert sorted(os.listdir(m)) == ["docs"] and (m / "docs").is_dir()
    assert (m / "docs" / "hello.txt").read_bytes() == b"hello from S3\n"
    assert (m / "docs" / "hello.txt").stat().st_size == 14
    (m / "docs" / "new.txt").write_text("written through FUSE\n")
    assert s3.get_object(Bucket=bucket, Key="docs/new.txt")["Body"].read() == b"written through FUSE\n"
    data = os.urandom(3_000_000)
    with open(m / "big.bin", "wb") as f:
        for i in range(0, len(data), 65536):
            f.write(data[i:i + 65536])
    assert hashlib.sha256((m / "big.bin").read_bytes()).digest() == hashlib.sha256(data).digest()
    with open(m / "big.bin", "rb") as f:
        f.seek(1_234_567)
        assert f.read(100) == data[1_234_567:1_234_667]
    # Overwrite (O_TRUNC) replaces the object.
    (m / "docs" / "new.txt").write_text("replaced\n")
    assert s3.get_object(Bucket=bucket, Key="docs/new.txt")["Body"].read() == b"replaced\n"
    (m / "sub" / "deeper").mkdir(parents=True)
    shutil.copy(m / "docs" / "hello.txt", m / "sub" / "deeper" / "copy.txt")
    os.rename(m / "docs" / "new.txt", m / "docs" / "moved.txt")
    assert sorted(os.listdir(m / "docs")) == ["hello.txt", "moved.txt"]
    with pytest.raises(OSError) as ei:
        os.rmdir(m / "sub")
    assert ei.value.errno == errno.ENOTEMPTY
    os.remove(m / "sub" / "deeper" / "copy.txt")
    os.rmdir(m / "sub" / "deeper")
    os.rmdir(m / "sub")
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"])
    assert keys == ["big.bin", "docs/hello.txt", "docs/moved.txt"]


def test_what_objects_cannot_express_is_refused(s3, bucket, mounted):
    s3.put_object(Bucket=bucket, Key="f.txt", Body=b"0123456789")
    s3.put_object(Bucket=bucket, Key="d/x", Body=b"x")
    m = mounted(bucket)
    for fn in (lambda: os.chmod(m / "f.txt", 0o600), lambda: os.symlink(m / "f.txt", m / "link")):
        with pytest.raises(OSError) as ei:
            fn()
        assert ei.value.errno in (errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM)
    os.rename(m / "d", m / "e")  # a directory: one transaction in the store
    assert (m / "e" / "x").read_bytes() == b"x" and not (m / "d").exists()
    assert (m / "f.txt").read_bytes() == b"0123456789"  # untouched


def received(store):
    import requests

    for line in requests.get(f"{store.endpoint}/-/metrics", timeout=5).text.splitlines():
        if line.startswith("cairn_store_received_bytes_total "):
            return int(line.split()[1])
    return 0


def test_appends_edits_holes_and_truncation(s3, bucket, mounted):
    s3.put_object(Bucket=bucket, Key="log.txt", Body=b"line 1\n")
    s3.put_object(Bucket=bucket, Key="f.bin", Body=b"0123456789")
    m = mounted(bucket)
    with open(m / "log.txt", "ab") as f:
        f.write(b"line 2\n")
        f.write(b"line 3\n")
    assert s3.get_object(Bucket=bucket, Key="log.txt")["Body"].read() == b"line 1\nline 2\nline 3\n"
    with open(m / "f.bin", "r+b") as f:
        f.seek(3)
        f.write(b"XY")
        f.seek(0)
        assert f.read() == b"012XY56789"  # reads see the edit before it is stored
    assert s3.get_object(Bucket=bucket, Key="f.bin")["Body"].read() == b"012XY56789"
    with open(m / "f.bin", "r+b") as f:
        f.seek(20)
        f.write(b"!")  # past the end: a hole of zeros
    assert s3.get_object(Bucket=bucket, Key="f.bin")["Body"].read() == b"012XY56789" + b"\0" * 10 + b"!"
    os.truncate(m / "f.bin", 4)  # without an open file
    assert s3.get_object(Bucket=bucket, Key="f.bin")["Body"].read() == b"012X"
    with open(m / "f.bin", "r+b") as f:
        f.truncate(8)  # extend with zeros
    assert s3.get_object(Bucket=bucket, Key="f.bin")["Body"].read() == b"012X\0\0\0\0"
    with open(m / "new.txt", "wb", buffering=0) as f:  # out-of-order writes into a new file
        f.seek(5)
        f.write(b"world")
        f.seek(0)
        f.write(b"hello")
    assert s3.get_object(Bucket=bucket, Key="new.txt")["Body"].read() == b"helloworld"


def test_editing_a_large_file_sends_only_what_changed(store, s3, bucket, mounted):
    big = os.urandom(40 << 20)  # five 8 MiB chunks
    s3.put_object(Bucket=bucket, Key="big.bin", Body=big)
    m = mounted(bucket)
    before = received(store)
    with open(m / "big.bin", "r+b") as f:
        f.seek(17 << 20)
        f.write(b"PATCHED")
    sent = received(store) - before
    data = s3.get_object(Bucket=bucket, Key="big.bin")["Body"].read()
    want = big[:17 << 20] + b"PATCHED" + big[(17 << 20) + 7:]
    assert hashlib.sha256(data).digest() == hashlib.sha256(want).digest()
    assert sent < 10 << 20, sent  # one chunk travelled; the other four were copied inside the store
    # Appending to it sends the new tail (and the last chunk), not the file.
    before = received(store)
    with open(m / "big.bin", "ab") as f:
        f.write(b"tail" * 1000)
    assert received(store) - before < 10 << 20
    assert s3.head_object(Bucket=bucket, Key="big.bin")["ContentLength"] == len(big) + 4000


def test_a_concurrent_change_is_not_overwritten(s3, bucket, mounted):
    s3.put_object(Bucket=bucket, Key="shared.txt", Body=b"original")
    m = mounted(bucket)
    f = open(m / "shared.txt", "r+b", buffering=0)
    f.seek(0)
    f.write(b"MINE")
    s3.put_object(Bucket=bucket, Key="shared.txt", Body=b"someone else's version")
    with pytest.raises(OSError) as ei:
        f.close()
    assert ei.value.errno == errno.ESTALE
    assert s3.get_object(Bucket=bucket, Key="shared.txt")["Body"].read() == b"someone else's version"


def test_prefix_read_only_and_authorization(s3, bucket, mounted):
    s3.put_object(Bucket=bucket, Key="team/a.txt", Body=b"a")
    s3.put_object(Bucket=bucket, Key="other/b.txt", Body=b"b")
    m = mounted(f"{bucket}/team")
    assert os.listdir(m) == ["a.txt"]
    ro = mounted(bucket, ADMIN, "--read-only")
    with pytest.raises(OSError) as ei:
        (ro / "x.txt").write_text("x")
    assert ei.value.errno == errno.EROFS
    reader = mounted(bucket, READER)  # a read-only key: the store refuses its writes
    assert (reader / "team" / "a.txt").read_bytes() == b"a"
    with pytest.raises(OSError) as ei:
        (reader / "w.txt").write_text("w")
    assert ei.value.errno == errno.EACCES
