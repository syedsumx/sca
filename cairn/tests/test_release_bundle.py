"""Air-gapped release bundles (L162), end to end with real parts: two real OCI registries (registry:2: the vendor's,
and the customer's own behind a password), real images built and pushed with Docker (one an OCI index of two),
charts packaged and pushed with Helm, a feed signed and published as a vendor does.

The vendor makes a bundle with its tool; the vendor's registry is then stopped (the install has no way out) and the
install imports it with ``cairn updates import``: verified, images pushed to its registry by digest, charts kept, the
feed installed last; the update agent's updater takes the chart from the import. And every way a bundle can be wrong
changes nothing: an altered layer or manifest, a path that leaves the bundle, a link, an older serial than the install
has seen, an install older than the bundle is for, an index older than the offline limit, wrong registry credentials.

Needs Docker and Helm (CI's terraform-modules job); skipped without them."""

import base64
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import pytest
import sqlalchemy

from cairn.common.db import metadata
from cairn.release import bundle, publish as pub
from cairn.release.feed import ReleaseFeed, state_table
from cairn.release.manifest import ReleaseError
from cairn.release.oci import INDEX_TYPES, Registry
from cairn.release.runner import Runner, StepFailed
from test_release_feed import keypair, manifest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "updater-app"
HELM = os.environ.get("HELM", "helm")

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0
    or shutil.which(HELM) is None, reason="needs Docker and Helm")


def sh(*args, **kw) -> str:
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise AssertionError(f"{' '.join(map(str, args))}: {r.stderr or r.stdout}")
    return r.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_registry(tmp: Path, user: str = "", password: str = "") -> tuple[str, str]:
    port = free_port()
    name = f"cairn-bundle-registry-{port}"
    args = ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:5000"]
    if user:
        auth = tmp / f"htpasswd-{port}"
        auth.write_text(f"{user}:{bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=5)).decode()}\n")
        auth.chmod(0o644)
        args += ["-v", f"{auth}:/auth/htpasswd:ro", "-e", "REGISTRY_AUTH=htpasswd",
                 "-e", "REGISTRY_AUTH_HTPASSWD_REALM=cairn", "-e", "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd"]
    sh(*args, "registry:2")
    for _ in range(40):
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", f"http://127.0.0.1:{port}/v2/"],
                           capture_output=True, text=True)
        if r.stdout in ("200", "401"):
            break
        time.sleep(0.5)
    return f"127.0.0.1:{port}", name


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """The vendor's side: a registry with two images (and an index of both), two chart versions, a signed feed."""
    tmp = tmp_path_factory.mktemp("vendor")
    vendor, vname = start_registry(tmp)
    customer, cname = start_registry(tmp, "ops", "s3cret-pw")
    try:
        digests = {}
        for comp in ("api", "pdp"):
            ctx = tmp / f"img-{comp}"
            ctx.mkdir()
            (ctx / "hello.txt").write_bytes(os.urandom(200_000))       # a layer worth hashing (incompressible)
            (ctx / "Dockerfile").write_text("FROM scratch\nCOPY hello.txt /hello.txt\n")
            ref = f"{vendor}/cairn/{comp}:1"
            sh("docker", "build", "-q", "-t", ref, str(ctx))
            sh("docker", "push", "-q", ref)
            repo_digest = sh("docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref).strip()
            digests[comp] = repo_digest.split("@", 1)[1]
            sh("docker", "tag", ref, f"{vendor}/cairn/multi:{comp}")
            sh("docker", "push", "-q", f"{vendor}/cairn/multi:{comp}")
        # An OCI index of both (a multi-platform image), put by digest with Cairn's own client.
        reg = Registry(vendor, plain_http=True)
        entries = []
        for comp, plat in (("api", "amd64"), ("pdp", "arm64")):
            raw, media = reg.manifest("cairn/multi", digests[comp])
            entries.append({"mediaType": media, "digest": digests[comp], "size": len(raw),
                            "platform": {"os": "linux", "architecture": plat}})
        index = json.dumps({"schemaVersion": 2, "mediaType": INDEX_TYPES[0], "manifests": entries}).encode()
        index_digest = "sha256:" + hashlib.sha256(index).hexdigest()
        reg.put_manifest("cairn/multi", index_digest, index, INDEX_TYPES[0])

        feed = tmp / "feed"
        feed.mkdir()
        signer, keys = keypair("rel-1")
        manifests = {}
        for version, min_from in (("0.2.0", "0.1.0"), ("0.3.0", "0.2.0")):
            src = tmp / f"chart-{version}"
            shutil.copytree(FIXTURE, src)
            out = tmp / f"pkg-{version}"
            out.mkdir()
            sh(HELM, "package", src, "--version", version, "--app-version", version, "--destination", out)
            tgz = next(out.glob("*.tgz"))
            sh(HELM, "push", tgz, f"oci://{vendor}/charts", "--plain-http")
            m = manifest(version, min_from)
            m["chart"] = {"name": "updater-app", "version": version, "url": f"oci://{vendor}/charts/updater-app",
                          "digest": "sha256:" + hashlib.sha256(tgz.read_bytes()).hexdigest()}
            m["images"] = {"api": f"{vendor}/cairn/api@{digests['api']}",
                           "pdp": f"{vendor}/cairn/pdp@{digests['pdp']}",
                           "multi": f"{vendor}/cairn/multi@{index_digest}"}
            manifests[version] = m
            pub.publish(feed, pub.sign_manifest(m, [signer]), [signer])
        yield {"tmp": tmp, "vendor": vendor, "vname": vname, "customer": customer, "feed": feed, "signer": signer,
               "keys": keys, "digests": digests, "index": index_digest, "manifests": manifests}
    finally:
        for n in (vname, cname):
            subprocess.run(["docker", "rm", "-f", n], capture_output=True)


def engine(tmp: Path, name: str):
    e = sqlalchemy.create_engine(f"sqlite:///{tmp}/{name}.db")
    metadata.create_all(e, tables=[state_table])
    return e


def auth_file(tmp: Path, host: str, user: str, password: str) -> str:
    p = tmp / f"docker-config-{user}.json"
    p.write_text(json.dumps({"auths": {host: {"auth": base64.b64encode(f"{user}:{password}".encode()).decode()}}}))
    p.chmod(0o600)
    return str(p)


@pytest.fixture(scope="module")
def made(world):
    """The vendor's tool makes the bundle (from installs at 0.1.0), then the vendor's registry goes away."""
    out = world["tmp"] / "cairn-0.3.0.tar"
    keys_file = world["tmp"] / "keys.json"
    from cryptography.hazmat.primitives import serialization

    pubs = {kid: base64.b64encode(k.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
            for kid, k in world["keys"].items()}
    keys_file.write_text(json.dumps({"keys": pubs}))
    r = subprocess.run([sys.executable, str(ROOT / "deploy" / "ops" / "release.py"), "bundle", "--feed", str(world["feed"]),
                        "--channel", "stable", "--from", "0.1.0", "--out", str(out), "--plain-http", "--helm", HELM,
                        "--keys-file", str(keys_file)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    toc = json.loads(r.stdout)
    assert toc["path"] == ["0.2.0", "0.3.0"] and toc["to"] == "0.3.0"
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert names[0] == "cairn-bundle.json" and "feed/channels/stable.json" in names
    assert sum(n.startswith("charts/") and n.endswith(".tgz") for n in names) == 2
    assert "images/index.json" in names and "images/oci-layout" in names
    subprocess.run(["docker", "stop", world["vname"]], capture_output=True)        # no way out from here on
    return out


def tamper(src: Path, dst: Path, pick, change) -> None:
    """A copy of the bundle with one member's bytes changed."""
    with tarfile.open(src) as tin, tarfile.open(dst, "w") as tout:
        done = False
        for m in tin:
            data = tin.extractfile(m).read() if m.isfile() else None
            if data is not None and not done and pick(m.name, data):
                data, done = change(data), True
                m.size = len(data)
            tout.addfile(m, io.BytesIO(data) if data is not None else None)
    assert done


def do_import(world, bundle_file, store, eng, current="0.1.0", password="s3cret-pw", **kw):
    return bundle.import_bundle(bundle_file, store, f"{world['customer']}/mirror/cairn", engine=eng, channel="stable",
                                current=current, registry_auth=auth_file(world["tmp"], world["customer"], "ops", password),
                                plain_http=True, keys=world["keys"], dev_keys={}, log=lambda m: None, **kw)


def customer_has(world, name: str, digest: str) -> bool:
    reg = Registry(world["customer"], ("ops", "s3cret-pw"), plain_http=True)
    try:
        reg.manifest(f"mirror/cairn/{name}", digest)
        return True
    except ReleaseError:
        return False


def test_an_altered_bundle_changes_nothing(world, made, tmp_path):
    big = world["digests"]["api"]
    layers = []
    with tarfile.open(made) as tar:
        for m in tar:
            if m.name.startswith("images/blobs/sha256/") and m.size > 100_000:
                layers.append(m.name)
    assert layers                                                   # the 200 kB layers are in it
    cases = {
        "layer": (lambda n, d: n == layers[0], lambda d: d[:-1] + bytes([d[-1] ^ 1])),
        "manifest": (lambda n, d: n.startswith("feed/releases/"), lambda d: d.replace(b"payload", b"payload ", 1)),
        "chart": (lambda n, d: n.startswith("charts/"), lambda d: d + b"\0"),
    }
    for what, (pick, change) in cases.items():
        bad = tmp_path / f"bad-{what}.tar"
        tamper(made, bad, pick, change)
        store = tmp_path / f"store-{what}"
        with pytest.raises(ReleaseError):
            do_import(world, bad, store, engine(tmp_path, what))
        assert not (store / "feed").exists() and not (store / "charts").exists(), what
    assert not customer_has(world, "api", big)                       # nothing reached the registry

    # A path that leaves the bundle, and a link: refused before anything is read.
    for what, member in (("escape", tarfile.TarInfo("../outside.txt")), ("link", tarfile.TarInfo("feed/x"))):
        bad = tmp_path / f"bad-{what}.tar"
        shutil.copyfile(made, bad)
        with tarfile.open(bad, "a") as tar:
            if what == "link":
                member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(member)
            else:
                member.size = 2
                tar.addfile(member, io.BytesIO(b"hi"))
        with pytest.raises(ReleaseError, match="outside itself|not a file or a folder"):
            do_import(world, bad, tmp_path / f"store-{what}", engine(tmp_path, what))
        assert not (tmp_path / "outside.txt").exists()

    # Wrong credentials for the customer's registry: refused, the feed not installed.
    store = tmp_path / "store-auth"
    with pytest.raises(ReleaseError, match="credentials were refused|sign-in needed"):
        do_import(world, made, store, engine(tmp_path, "auth"), password="wrong")
    assert not (store / "feed").exists()


def test_import_then_update_without_a_way_out(world, made, tmp_path):
    store, eng = tmp_path / "store", engine(tmp_path, "install")
    r = subprocess.run([sys.executable, "-m", "cairn.cli", "updates", "import", str(made), "--plain-http"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert r.returncode != 0 and "--registry" in r.stderr                  # where the images go is not guessed

    out = do_import(world, made, store, eng)
    assert out["path"] == ["0.2.0", "0.3.0"] and out["serial"] == 2
    for name in ("api", "pdp"):
        assert customer_has(world, name, world["digests"][name])
    assert customer_has(world, "multi", world["index"])                    # the index and both of its images
    # Docker itself pulls what was imported, by digest, from the customer's registry (its own sign-in, its own config).
    cfg = tmp_path / "docker-cfg"
    cfg.mkdir()
    subprocess.run(["docker", "--config", str(cfg), "login", world["customer"], "-u", "ops", "--password-stdin"],
                   input="s3cret-pw", capture_output=True, text=True, check=True)
    ref = f"{world['customer']}/mirror/cairn/api@{world['digests']['api']}"
    sh("docker", "--config", cfg, "pull", "-q", ref)
    assert sh("docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref).strip().endswith(world["digests"]["api"])
    assert len(list((store / "charts").glob("*.tgz"))) == 2 and not list(store.glob("incoming-*"))

    # The install follows the imported feed: verified, planned, and no older than what it has seen.
    feed = ReleaseFeed(eng, (store / "feed").as_uri(), "stable", "0.1.0", keys=world["keys"], dev_keys={})
    st = feed.check()
    assert st["error"] is None and st["path"] == ["0.2.0", "0.3.0"] and st["offline"] is True

    # The updater takes each chart from the import (the vendor's registry is gone), its digest checked.
    runner = Runner("app", "ns", "", helm=HELM, chart_dir=str(store / "charts"))
    for step in st["steps"]:
        with __import__("tempfile").TemporaryDirectory(dir=tmp_path) as d:
            tgz = runner.fetch_chart(step, Path(d))
            assert tgz.name == f"updater-app-{step['version']}.tgz"
    missing = {**st["steps"][0], "chart": {**st["steps"][0]["chart"], "digest": "sha256:" + "0" * 64}}
    with pytest.raises(StepFailed, match="not among the imported ones"):
        runner.fetch_chart(missing, tmp_path)

    # Imported again: the registry already has every blob; nothing changes.
    again = do_import(world, made, store, eng)
    assert again["serial"] == 2

    # An install older than the bundle is for (0.1.0 or later): the chart it would need first is not in it.
    world_old = engine(tmp_path, "old")
    pub_feed = world["feed"]
    m0 = manifest("0.1.0", "0.0.1")
    m0["chart"] = {**world["manifests"]["0.2.0"]["chart"], "version": "0.1.0", "digest": "sha256:" + "1" * 64}
    m0["images"] = world["manifests"]["0.2.0"]["images"]
    pub.publish(pub_feed, pub.sign_manifest(m0, [world["signer"]]), [world["signer"]])    # serial 3, 0.1.0 listed
    newer = tmp_path / "newer.tar"
    shutil.copyfile(made, newer)
    with tarfile.open(newer, "a") as tar:                                   # the newer index and 0.1.0's manifest
        for rel in ("feed/channels/stable.json", "feed/releases/0.1.0.json"):
            src = pub_feed / rel.split("/", 1)[1]
            info = tar.gettarinfo(str(src), arcname=rel)
            with src.open("rb") as f:
                tar.addfile(info, f)
    with pytest.raises(ReleaseError, match="lacks the chart of 0.1.0"):
        do_import(world, newer, tmp_path / "store-old", world_old, current="0.0.5")

    # The newer index accepted at 0.1.0; then the first bundle (serial 2) is older than what was seen: refused.
    assert do_import(world, newer, store, eng)["serial"] == 3
    with pytest.raises(ReleaseError, match="older than one this install has already seen"):
        do_import(world, made, store, eng)


def test_an_index_older_than_the_offline_limit(world, tmp_path):
    feed = tmp_path / "feed"
    feed.mkdir()
    old = datetime.now(timezone.utc) - timedelta(days=200)
    pub.publish(feed, pub.sign_manifest(world["manifests"]["0.2.0"], [world["signer"]]), [world["signer"]], now=old)
    eng = engine(tmp_path, "aged")
    st = ReleaseFeed(eng, feed.as_uri(), "stable", "0.1.0", keys=world["keys"], dev_keys={}).check()
    assert "more than 180 days ago" in st["error"] and st["path"] == []
    st = ReleaseFeed(eng, feed.as_uri(), "stable", "0.1.0", keys=world["keys"], dev_keys={},
                     offline_max_age_days=365).check()
    assert st["error"] is None and st["path"] == ["0.2.0"]             # past its expiry, within the limit set
    # Online, the same index is refused for its expiry, as before.
    with pytest.raises(ReleaseError, match="expired"):
        ReleaseFeed(engine(tmp_path, "online"), "http://127.0.0.1:9/", "stable", "0.1.0",
                    keys=world["keys"], dev_keys={}, http=_Files(feed))._verify()
    with pytest.raises(ReleaseError, match="outside the feed"):
        ReleaseFeed(eng, feed.as_uri(), "stable", "0.1.0", keys=world["keys"], dev_keys={})._read_file("../x")


class _Files:
    """httpx-shaped access to a folder, for the online check of the same files."""

    def __init__(self, root: Path):
        self.root = root

    def stream(self, method, url):
        import contextlib

        path = self.root / url.split("/", 3)[3]

        @contextlib.contextmanager
        def cm():
            class R:
                status_code = 200 if path.exists() else 404

                @staticmethod
                def iter_bytes():
                    yield path.read_bytes()
            yield R()
        return cm()
