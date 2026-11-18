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
    def make(target, key=ADMIN, *flags, endpoint=None):
        mnt = tmp_path / f"mnt{len(os.listdir(tmp_path))}"
        mnt.mkdir()
        env = {**os.environ, "CAIRN_STORE_ENDPOINT": endpoint or store.endpoint, "CAIRN_STORE_ACCESS_KEY": key[0],
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


def test_modes_owners_times_links_and_xattrs(s3, bucket, mounted):
    """POSIX metadata in the objects' user metadata (s3fs-fuse's headers): it survives remounts, renames
    and rewrites, and S3 clients see it."""
    s3.put_object(Bucket=bucket, Key="f.txt", Body=b"0123456789")
    s3.put_object(Bucket=bucket, Key="d/x", Body=b"x")
    m = mounted(bucket)
    # An object written by an S3 client shows the defaults.
    assert oct((m / "f.txt").stat().st_mode & 0o7777) == "0o644" and oct((m / "d").stat().st_mode & 0o7777) == "0o755"
    os.chmod(m / "f.txt", 0o600)
    os.utime(m / "f.txt", (1_600_000_000, 1_600_000_000))
    os.chown(m / "f.txt", 4242, 4343)
    st = (m / "f.txt").stat()
    assert (st.st_mode & 0o7777, st.st_mtime, st.st_uid, st.st_gid) == (0o600, 1_600_000_000, 4242, 4343)
    head = s3.head_object(Bucket=bucket, Key="f.txt")
    assert head["Metadata"]["mode"] == str(0o100600) and head["Metadata"]["uid"] == "4242"
    assert (m / "f.txt").read_bytes() == b"0123456789"  # the content is untouched
    # A directory that exists only by its keys gets a marker to keep its mode.
    os.chmod(m / "d", 0o750)
    assert (m / "d").stat().st_mode & 0o7777 == 0o750
    assert s3.head_object(Bucket=bucket, Key="d/")["Metadata"]["mode"] == str(0o040750)
    # New files and directories keep the mode they were created with.
    fd = os.open(m / "secret", os.O_CREAT | os.O_WRONLY, 0o640)
    os.write(fd, b"s")
    os.close(fd)
    os.mkdir(m / "private", 0o700)
    assert (m / "secret").stat().st_mode & 0o7777 == 0o640 and (m / "private").stat().st_mode & 0o7777 == 0o700
    # Rewriting a file keeps its metadata.
    with open(m / "f.txt", "r+b") as f:
        f.seek(2)
        f.write(b"XY")
    assert (m / "f.txt").stat().st_mode & 0o7777 == 0o600 and (m / "f.txt").read_bytes() == b"01XY456789"
    # Symbolic links: an object holding the target.
    os.symlink("f.txt", m / "link")
    os.symlink("../f.txt", m / "d" / "up")
    assert os.readlink(m / "link") == "f.txt" and (m / "link").is_symlink()
    assert (m / "link").read_bytes() == b"01XY456789" and (m / "d" / "up").read_bytes() == b"01XY456789"
    assert s3.get_object(Bucket=bucket, Key="link")["Body"].read() == b"f.txt"
    with pytest.raises(FileExistsError):
        os.symlink("other", m / "link")
    # Extended attributes (user. namespace).
    os.setxattr(m / "f.txt", "user.origin", b"lab-7")
    os.setxattr(m / "d", "user.owner", "\u00e9quipe".encode())
    assert os.getxattr(m / "f.txt", "user.origin") == b"lab-7"
    assert os.listxattr(m / "f.txt") == ["user.origin"] and os.getxattr(m / "d", "user.owner") == "\u00e9quipe".encode()
    with pytest.raises(FileExistsError):
        os.setxattr(m / "f.txt", "user.origin", b"x", os.XATTR_CREATE)
    with pytest.raises(OSError) as ei:
        os.setxattr(m / "f.txt", "trusted.x", b"x")
    # Only user. attributes are kept: the mount refuses others (EOPNOTSUPP); for a caller other than
    # root, the kernel refuses trusted. ones itself (EPERM). Either way nothing is stored.
    assert ei.value.errno in ((errno.EOPNOTSUPP,) if os.geteuid() == 0 else (errno.EOPNOTSUPP, errno.EPERM))
    assert "trusted.x" not in os.listxattr(m / "f.txt")
    with pytest.raises(OSError) as ei:
        os.setxattr(m / "f.txt", "user.big", b"x" * 4096)  # beyond S3's 2 KB of user metadata
    assert ei.value.errno == errno.E2BIG
    # Everything survives a rename and a new mount.
    os.rename(m / "f.txt", m / "g.txt")
    m2 = mounted(bucket)
    st = (m2 / "g.txt").stat()
    assert (st.st_mode & 0o7777, st.st_mtime, st.st_uid) == (0o600, 1_600_000_000, 4242)
    assert os.getxattr(m2 / "g.txt", "user.origin") == b"lab-7"
    os.removexattr(m2 / "g.txt", "user.origin")
    assert os.listxattr(m2 / "g.txt") == []
    assert os.readlink(m2 / "link") == "f.txt" and not (m2 / "link").exists()  # dangling now, as on any disk
    # Hard links cannot be expressed: two names for one object.
    with pytest.raises(OSError) as ei:
        os.link(m2 / "g.txt", m2 / "hard")
    assert ei.value.errno == errno.EPERM
    os.rename(m / "d", m / "e")  # a directory: one transaction in the store
    assert (m / "e" / "x").read_bytes() == b"x" and not (m / "d").exists()


def test_enforced_modes(s3, bucket, mounted):
    """--enforce-modes: the kernel checks modes before each access, as on a local disk."""
    if os.geteuid() != 0:
        pytest.skip("needs root to act as another user")
    s3.put_object(Bucket=bucket, Key="open.txt", Body=b"o")
    m = mounted(bucket, ADMIN, "--enforce-modes", "--allow-other")
    for p in m.parents:  # let the other user reach the mount point (pytest's directories are 0700)
        if str(p).startswith("/tmp/pytest-of-"):
            os.chmod(p, 0o755)
    (m / "mine.txt").write_bytes(b"m")
    os.chmod(m / "mine.txt", 0o600)

    def as_nobody(path):
        return subprocess.run(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "cat", str(path)],
                              capture_output=True)
    assert as_nobody(m / "open.txt").returncode == 0
    r = as_nobody(m / "mine.txt")
    assert r.returncode != 0 and b"Permission denied" in r.stderr


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


def test_a_large_object_marked_as_a_link(s3, bucket, mounted):
    """An S3 client can mark any object as a link: one larger than a link target can be (PATH_MAX) is refused
    without being read into the mount's memory."""
    s3.put_object(Bucket=bucket, Key="big", Body=b"a" * (8 << 20), Metadata={"mode": str(0o120777)})
    m = mounted(bucket)
    with pytest.raises(OSError) as ei:
        os.readlink(m / "big")
    assert ei.value.errno == errno.ENAMETOOLONG


def test_only_user_xattrs_show(s3, bucket, mounted):
    """The mount keeps user. extended attributes only. Others an S3 client put in the metadata (security.,
    trusted., system.) are the kernel's namespaces: they do not show through the mount, where the kernel
    would act on them (file capabilities, ACLs), but a change through the mount keeps them."""
    import base64
    import json
    import urllib.parse

    xattr = {"security.capability": base64.b64encode(b"\x01\x00\x00\x02").decode(), "trusted.t": "eA==",
             "system.posix_acl_access": "eA==", "user.a": "eA=="}
    s3.put_object(Bucket=bucket, Key="x", Body=b"x", Metadata={"xattr": urllib.parse.quote(json.dumps(xattr), safe="")})
    m = mounted(bucket)
    assert os.listxattr(m / "x") == ["user.a"] and os.getxattr(m / "x", "user.a") == b"x"
    for name in ("security.capability", "trusted.t"):
        with pytest.raises(OSError) as ei:
            os.getxattr(m / "x", name)
        assert ei.value.errno == errno.ENODATA
    with pytest.raises(OSError):
        os.removexattr(m / "x", "trusted.t")
    os.setxattr(m / "x", "user.b", b"y")
    kept = json.loads(urllib.parse.unquote(s3.head_object(Bucket=bucket, Key="x")["Metadata"]["xattr"]))
    assert set(kept) == {*xattr, "user.b"}


def test_staging_files_are_private(s3, bucket, mounted, tmp_path, monkeypatch):
    """A file being written is staged on local disk; other local users must not read it from there (the
    store's policies decide who reads the bucket, not the staging directory's permissions)."""
    staging = tmp_path / "staging"
    staging.mkdir()
    os.chmod(staging, 0o1777)  # a shared temporary directory, like /tmp
    monkeypatch.setenv("CAIRN_STORE_MOUNT_STAGING", str(staging))
    old = os.umask(0o022)  # the usual umask (the mount inherits it), whatever the test runner's
    try:
        m = mounted(bucket)
        with open(m / "payroll.csv", "wb", buffering=0) as f:
            f.write(b"name,salary\n")
            staged = list(staging.iterdir())
            assert staged, "nothing staged"
            for p in staged:
                assert p.stat().st_mode & 0o077 == 0, oct(p.stat().st_mode)
    finally:
        os.umask(old)
    assert s3.get_object(Bucket=bucket, Key="payroll.csv")["Body"].read() == b"name,salary\n"


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


def test_rewrites_keep_headers_and_encryption(custom_store, tmp_path, mounted):
    """An edit, an append, an overwrite, a truncate (to 0 too, without an open file) and a chmod through the
    mount keep the object's content type, cache headers and the KMS key it was encrypted with."""
    import base64
    import json

    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"keys": {"k1": base64.b64encode(os.urandom(32)).decode(),
                                            "finance": base64.b64encode(os.urandom(32)).decode()}, "sse_s3_key": "k1"}))
    st = custom_store({"CAIRN_STORE_KEYRING_FILE": str(keyring)})
    s3 = st.client()
    s3.create_bucket(Bucket="kept")
    for k in ("edit.csv", "append.csv", "over.csv", "trunc.csv", "zero.csv", "mode.csv"):
        s3.put_object(Bucket="kept", Key=k, Body=b"a,b\n1,2\n", ContentType="text/csv", CacheControl="max-age=60",
                      ServerSideEncryption="aws:kms", SSEKMSKeyId="finance")
    m = mounted("kept", endpoint=st.endpoint)
    with open(m / "edit.csv", "r+b") as f:
        f.write(b"x")
    with open(m / "append.csv", "ab") as f:
        f.write(b"3,4\n")
    (m / "over.csv").write_bytes(b"c,d\n")
    os.truncate(m / "trunc.csv", 4)
    os.truncate(m / "zero.csv", 0)
    os.chmod(m / "mode.csv", 0o600)
    for k in ("edit.csv", "append.csv", "over.csv", "trunc.csv", "zero.csv", "mode.csv"):
        h = s3.head_object(Bucket="kept", Key=k)
        assert (k, h["ContentType"], h.get("CacheControl")) == (k, "text/csv", "max-age=60")
        assert (k, h.get("ServerSideEncryption"), h.get("SSEKMSKeyId")) == (k, "aws:kms", "finance")
    assert s3.get_object(Bucket="kept", Key="zero.csv")["Body"].read() == b""
    assert s3.get_object(Bucket="kept", Key="edit.csv")["Body"].read() == b"x,b\n1,2\n"


def test_truncating_then_writing_past_the_end_leaves_zeros(s3, bucket, mounted):
    """Bytes cut off by a truncate are gone: a later write further on leaves zeros in between, not the old
    content of the version the edit started from."""
    old = os.urandom(9 << 20)  # two chunks
    s3.put_object(Bucket=bucket, Key="t.bin", Body=old)
    m = mounted(bucket)
    with open(m / "t.bin", "r+b", buffering=0) as f:
        f.truncate(2)
        f.seek((8 << 20) + 10)
        f.write(b"!")
    want = old[:2] + b"\0" * ((8 << 20) + 8) + b"!"
    got = s3.get_object(Bucket=bucket, Key="t.bin")["Body"].read()
    assert len(got) == len(want), len(got)
    assert got == want, next(i for i, (a, b) in enumerate(zip(got, want)) if a != b)


def test_an_overwrite_does_not_replace_a_concurrent_change(s3, bucket, mounted):
    """`>` (an O_TRUNC open) replaces the version it opened, not one written meanwhile."""
    s3.put_object(Bucket=bucket, Key="o.txt", Body=b"original")
    m = mounted(bucket)
    f = open(m / "o.txt", "wb", buffering=0)
    f.write(b"MINE")
    s3.put_object(Bucket=bucket, Key="o.txt", Body=b"someone else's version")
    with pytest.raises(OSError) as ei:
        f.close()
    assert ei.value.errno == errno.ESTALE
    assert s3.get_object(Bucket=bucket, Key="o.txt")["Body"].read() == b"someone else's version"


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
