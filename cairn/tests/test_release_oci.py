"""The checks of cairn.release.oci and cairn.release.bundle that need no registry (L162): an altered manifest in an
image layout is refused before it is read (two that point at each other cannot loop), references name a registry and
a digest, and a bundle that cannot be unpacked is a refusal, not a crash."""

import hashlib
import io
import json
import tarfile

import pytest

from cairn.release.bundle import _unpack
from cairn.release.manifest import ReleaseError
from cairn.release.oci import INDEX_TYPES, Layout, parse_ref, split_target


def test_altered_manifests_are_refused_before_they_are_read(tmp_path):
    lay = Layout(tmp_path / "images")
    lay.init()
    a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64          # each claims to be what the other lists
    for me, other in ((a, b), (b, a)):
        lay.blob(me).write_text(json.dumps({"schemaVersion": 2, "mediaType": INDEX_TYPES[0],
                                            "manifests": [{"digest": other, "mediaType": INDEX_TYPES[0]}]}))
    with pytest.raises(ReleaseError, match="is not that digest"):
        lay.verify(a, INDEX_TYPES[0])
    good = json.dumps({"schemaVersion": 2, "mediaType": INDEX_TYPES[0], "manifests": []}).encode()
    d = "sha256:" + hashlib.sha256(good).hexdigest()
    lay.blob(d).write_bytes(good)
    assert lay.verify(d, INDEX_TYPES[0]) == len(good)
    with pytest.raises(ReleaseError, match="lacks manifest"):
        lay.verify("sha256:" + "c" * 64, INDEX_TYPES[0])
    with pytest.raises(ReleaseError, match="not a sha256 digest"):
        lay.blob("sha256:../../etc")


def test_references_name_a_registry_and_a_digest():
    d = "sha256:" + "e" * 64
    assert parse_ref(f"registry.example.com/cairn/api@{d}") == ("registry.example.com", "cairn/api", d)
    assert parse_ref(f"127.0.0.1:5000/cairn/api@{d}")[0] == "127.0.0.1:5000"
    for bad in (f"cairn/api@{d}", "registry.example.com/cairn/api:1.0", f"registry.example.com/Cairn/API@{d}"):
        with pytest.raises(ReleaseError):
            parse_ref(bad)
    assert split_target("registry.internal:5000/mirror/cairn") == ("registry.internal:5000", "mirror/cairn")
    with pytest.raises(ReleaseError):
        split_target("https://registry.internal/x")


def test_a_bundle_that_cannot_be_unpacked_is_refused(tmp_path):
    from cairn.release.bundle import import_bundle

    bad = tmp_path / "clash.tar"
    with tarfile.open(bad, "w") as tar:
        for name in ("feed", "feed/channels/stable.json"):              # a file where a folder must go
            info = tarfile.TarInfo(name)
            info.size = 2
            tar.addfile(info, io.BytesIO(b"{}"))
    with pytest.raises(OSError):
        _unpack(bad, tmp_path / "u", 1 << 20)
    with pytest.raises(ReleaseError, match="could not be unpacked"):
        import_bundle(bad, tmp_path / "store", "registry.internal/cairn", engine=None, channel="stable",
                      current="1.0.0", log=lambda m: None)
    assert [p.name for p in (tmp_path / "store").iterdir()] == [".import.lock"]      # nothing else left behind
    big = tmp_path / "big.tar"
    with tarfile.open(big, "w") as tar:
        info = tarfile.TarInfo("feed/x")
        info.size = 4096
        tar.addfile(info, io.BytesIO(b"x" * 4096))
    with pytest.raises(ReleaseError, match="more than 1000 bytes"):
        _unpack(big, tmp_path / "u2", 1000)
