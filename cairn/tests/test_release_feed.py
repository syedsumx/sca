"""Signed releases and channels (L159), end to end: a feed published with the vendor tooling, served by a real HTTP
server, checked by an install as Cairn's API runs it. Everything that should offer no update offers none and says why:
an altered file, an unknown key, the development key off the dev channel, an older index (a rollback), the same
serial with other content, an expired index, too few signatures; a release with a tag instead of a digest is never
signed. The way up is worked out across each release's min_from, with the most severe release and the breaking
changes on it."""

import base64
import functools
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import sqlalchemy
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from cairn.common.db import metadata
from cairn.release import publish as pub
from cairn.release.feed import ReleaseFeed, state_table
from cairn.release.manifest import DEV_KEYS_PATH, ReleaseError, check_manifest, load_keys, open_envelope

ROOT = Path(__file__).resolve().parents[1]
D = "sha256:" + "a" * 64


def manifest(version, min_from=None, channels=("stable",), severity="normal", breaking=()):
    return {"type": "cairn-release", "schema": 1, "version": version, "released_at": "2026-11-12T09:00:00Z",
            "channels": list(channels), "min_from": min_from, "severity": severity, "summary": f"Cairn {version}",
            "notes_url": f"https://cairn.example/releases/{version}", "breaking": list(breaking),
            "migrations": {"head": "0148"},
            "chart": {"name": "cairn", "version": version, "url": "oci://registry.example.com/charts/cairn",
                      "digest": D},
            "images": {"api": f"registry.example.com/cairn/api@sha256:{'b' * 64}",
                       "ui": f"registry.example.com/cairn/ui@sha256:{'c' * 64}"},
            "requires": {"kubernetes": "1.28"}}


def keypair(kid):
    k = Ed25519PrivateKey.generate()
    return (kid, k), {kid: k.public_key()}


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture()
def served(tmp_path):
    """A feed directory served over HTTP on this machine, as a vendor's site or a customer's mirror serves it."""
    feed = tmp_path / "feed"
    feed.mkdir()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(feed)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield feed, f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def engine(tmp_path, name="state"):
    e = sqlalchemy.create_engine(f"sqlite:///{tmp_path}/{name}.db")
    metadata.create_all(e, tables=[state_table])
    return e


def test_the_way_up_is_verified_and_planned_across_min_from(served, tmp_path):
    feed, url = served
    signer, keys = keypair("rel-1")
    for m in (manifest("2.0.0", "1.0.0"),
              manifest("2.1.0", "2.0.0", severity="security",
                       breaking=[{"id": "trino-490", "text": "Trino 490", "action": "re-run the catalog sync"}]),
              manifest("2.2.0", "2.0.0"),
              manifest("3.0.0-rc.1", "2.2.0", channels=("early",)),
              manifest("3.0.0", "2.2.0", channels=("stable", "early"))):
        pub.publish(feed, pub.sign_manifest(m, [signer]), [signer])

    s = ReleaseFeed(engine(tmp_path), url, "stable", "1.5.0", keys=keys, dev_keys={}).check()
    assert s["error"] is None, s["error"]
    assert s["path"] == ["2.0.0", "2.2.0", "3.0.0"]                 # 2.2.0 takes 2.0.0 directly: 2.1.0 is jumped
    assert [a["version"] for a in s["available"]] == ["3.0.0", "2.2.0", "2.1.0", "2.0.0"]
    assert s["severity"] == "security"                               # what is jumped over is still brought in
    assert [(b["version"], b["id"]) for b in s["breaking"]] == [("2.1.0", "trino-490")]
    assert s["steps"][0]["images"]["api"].endswith("b" * 64) and s["serial"] == 4             # stable re-signed once per release on it

    early = ReleaseFeed(engine(tmp_path, "early"), url, "early", "2.2.0", keys=keys, dev_keys={}).check()
    assert early["path"] == ["3.0.0"] and [a["version"] for a in early["available"]] == ["3.0.0", "3.0.0-rc.1"]

    old = ReleaseFeed(engine(tmp_path, "old"), url, "stable", "0.9.0", keys=keys, dev_keys={}).check()
    assert old["path"] == [] and "needs 1.0.0" in old["blocked"]

    latest = ReleaseFeed(engine(tmp_path, "latest"), url, "stable", "3.0.0", keys=keys, dev_keys={}).check()
    assert latest["available"] == [] and latest["target"] is None and latest["error"] is None


def test_what_should_offer_nothing_offers_nothing_and_says_why(served, tmp_path):
    feed, url = served
    signer, keys = keypair("rel-1")
    pub.publish(feed, pub.sign_manifest(manifest("2.0.0"), [signer]), [signer])
    e = engine(tmp_path)
    f = ReleaseFeed(e, url, "stable", "1.0.0", keys=keys, dev_keys={})
    assert f.check()["target"] == "2.0.0"
    good_index = (feed / "channels/stable.json").read_bytes()

    def refused(why):
        s = f.check()
        assert s["error"] and why in s["error"], s["error"]
        assert s["available"] == [] and s["target"] is None          # fails closed
        return s

    # A manifest altered after the index named it.
    m = feed / "releases/2.0.0.json"
    orig = m.read_bytes()
    m.write_bytes(orig.replace(b'"payload"', b'"payload" ', 1))
    refused("does not match the digest")
    m.write_bytes(orig)
    assert f.check()["error"] is None

    # An index whose payload was changed under its signature.
    env = json.loads(good_index)
    doc = json.loads(base64.urlsafe_b64decode(env["payload"] + "=="))
    doc["serial"] += 10
    env["payload"] = base64.urlsafe_b64encode(json.dumps(doc).encode()).rstrip(b"=").decode()
    (feed / "channels/stable.json").write_text(json.dumps(env))
    refused("altered")

    # Signed by a key this install does not know.
    stranger, _ = keypair("someone-else")
    shutil.rmtree(feed / "channels")
    pub.publish(feed, (feed / "releases/2.0.0.json").read_bytes(), [stranger])
    refused("not signed by a release key")

    # The development key: fine on dev, never on stable.
    dev_key = serialization.load_pem_private_key((ROOT / "deploy/release/dev-private-key.pem").read_bytes(), None)
    dev = [("cairn-release-dev", dev_key)]
    shutil.rmtree(feed / "channels")
    (feed / "channels").mkdir()
    (feed / "channels/stable.json").write_bytes(good_index)
    opened = open_envelope(pub.sign_manifest(manifest("2.0.1"), dev), {}, load_keys(DEV_KEYS_PATH))
    assert opened.development
    with pytest.raises(ReleaseError, match="dev channel only"):
        check_manifest(opened.doc, opened.development)
    devfeed = tmp_path / "devfeed"
    pub.publish(devfeed, pub.sign_manifest(manifest("2.0.1", channels=("dev",)), dev), dev)
    shutil.copytree(devfeed, feed, dirs_exist_ok=True)
    d = ReleaseFeed(engine(tmp_path, "d1"), url, "dev", "1.0.0", keys=keys).check()   # the image's dev key
    assert d["error"] is None and d["target"] == "2.0.1"
    (feed / "channels/stable.json").write_bytes((devfeed / "channels/dev.json").read_bytes())
    s = ReleaseFeed(engine(tmp_path, "d2"), url, "stable", "1.0.0", keys=keys).check()
    assert "is for channel dev" in s["error"] and s["available"] == [], s["error"]
    (feed / "channels/stable.json").write_bytes(good_index)

    # A rollback: the feed serves an index older than one this install accepted.
    assert f.check()["error"] is None
    pub.refresh(feed, "stable", [signer])
    assert f.check()["serial"] == 2
    (feed / "channels/stable.json").write_bytes(good_index)
    s = refused("older than one this install has already seen")
    assert s["serial"] == 2                                          # what was accepted is kept

    # Two replicas: one has accepted serial 2; another, which read the state before, must not write serial 1 over it.
    assert f._accept(1, "0" * 64, 0, {}) is False and f._row()["serial"] == 2

    # The same serial with other content.
    later = datetime.now(timezone.utc) + timedelta(minutes=1)
    pub.refresh(feed, "stable", [signer], now=later)   # serial 2 again (from the old index), signed at another time
    refused("without its serial rising")

    # Expired: nobody re-signed it in time (or it is being held back).
    shutil.rmtree(feed / "channels")
    long_ago = datetime.now(timezone.utc) - timedelta(days=40)
    pub.publish(feed, (feed / "releases/2.0.0.json").read_bytes(), [signer], days=30, now=long_ago)
    exp = ReleaseFeed(engine(tmp_path, "exp"), url, "stable", "1.0.0", keys=keys, dev_keys={}).check()
    assert "expired" in exp["error"] and exp["available"] == []


def test_two_keys_for_rotation_and_what_is_never_signed(served, tmp_path):
    feed, url = served
    old, old_pub = keypair("rel-2025")
    new, new_pub = keypair("rel-2026")
    both = {**old_pub, **new_pub}
    pub.publish(feed, pub.sign_manifest(manifest("2.0.0"), [old]), [old])
    f2 = ReleaseFeed(engine(tmp_path), url, "stable", "1.0.0", threshold=2, keys=both, dev_keys={})
    assert "signed by 1 of the 2" in f2.check()["error"]
    shutil.rmtree(feed)
    pub.publish(feed, pub.sign_manifest(manifest("2.0.0"), [old, new]), [old, new])
    assert f2.check()["target"] == "2.0.0"
    # During rotation an install with only the new key takes what both signed.
    assert ReleaseFeed(engine(tmp_path, "n"), url, "stable", "1.0.0", keys=new_pub, dev_keys={}).check()["target"] \
        == "2.0.0"

    bad = manifest("2.1.0")
    bad["images"]["api"] = "registry.example.com/cairn/api:2.1.0"
    with pytest.raises(ReleaseError, match="pinned by digest"):
        pub.sign_manifest(bad, [new])
    with pytest.raises(ReleaseError, match="older than the release"):
        pub.sign_manifest(manifest("2.1.0", "2.1.0"), [new])
    other = manifest("2.0.0")
    other["summary"] = "something else"
    with pytest.raises(ReleaseError, match="already published"):
        pub.publish(feed, pub.sign_manifest(other, [old, new]), [old, new])
    pub.withdraw(feed, "stable", "2.0.0", [old, new])
    assert f2.check()["available"] == []
    with pytest.raises(ReleaseError, match="https"):
        ReleaseFeed(engine(tmp_path, "x"), "http://releases.example.com/", "stable", "1.0.0")


def test_the_api_shows_updates_to_operators_only(served, tmp_path):
    sys.path.insert(0, str(ROOT / "tests"))
    from iac_world import ANA_TOKEN, TOKEN, build

    from cairn.app import create_api_app

    feed, url = served
    signer, keys = keypair("rel-1")
    pub.publish(feed, pub.sign_manifest(manifest("9.0.0", "0.1.0", severity="critical"), [signer]), [signer])
    s = build(tmp_path, env={"CAIRN_RELEASE_FEED": url, "CAIRN_RELEASE_CHANNEL": "stable"})
    assert s.release_feed is not None
    s.release_feed.keys, s.release_feed.dev_keys = keys, {}           # the image would carry the vendor's keys
    http = TestClient(create_api_app(s))
    admin = {"Authorization": f"Bearer {TOKEN}"}
    assert http.get("/v1/system/updates", headers={"Authorization": f"Bearer {ANA_TOKEN}"}).status_code == 403
    assert http.post("/v1/system/updates/check", headers={"Authorization": f"Bearer {ANA_TOKEN}"}).status_code == 403
    before = http.get("/v1/system/updates", headers=admin).json()
    assert before["checked_at"] is None and before["current"] == "0.1.0"
    after = http.post("/v1/system/updates/check", headers=admin).json()
    assert after["target"] == "9.0.0" and after["severity"] == "critical" and after["error"] is None
    assert http.get("/v1/system/updates", headers=admin).json()["target"] == "9.0.0"
    checked = [e for e in s.audit.about("urn:li:cairnRelease:stable") if e["action"] == "updates.checked"]
    assert checked and checked[0]["details"]["target"] == "9.0.0"


def test_the_vendor_tool_end_to_end(served, tmp_path):
    feed, url = served
    tool = [sys.executable, str(ROOT / "deploy/ops/release.py")]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    key = tmp_path / "rel.pem"
    out = subprocess.run(tool + ["keygen", "--kid", "rel-1", "--out", str(key)], capture_output=True, text=True,
                         env=env, check=True)
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    (tmp_path / "keys.json").write_text(json.dumps({"keys": json.loads(out.stdout)}))
    (tmp_path / "2.0.0.yaml").write_text(yaml.safe_dump(manifest("2.0.0", "0.1.0")))
    signed = subprocess.run(tool + ["sign", "--key-file", str(key), "--kid", "rel-1", str(tmp_path / "2.0.0.yaml")],
                            capture_output=True, env=env, check=True).stdout
    (tmp_path / "2.0.0.json").write_bytes(signed)
    r = subprocess.run(tool + ["publish", "--feed", str(feed), "--key-file", str(key), "--kid", "rel-1",
                               str(tmp_path / "2.0.0.json")], capture_output=True, text=True, env=env, check=True)
    assert "stable: serial 1" in r.stdout
    v = subprocess.run(tool + ["verify", "--feed", url, "--current", "0.1.0", "--keys-file", str(tmp_path / "keys.json")],
                       capture_output=True, text=True, env=env)
    assert v.returncode == 0, v.stderr + v.stdout
    assert json.loads(v.stdout)["target"] == "2.0.0"
    v = subprocess.run(tool + ["verify", "--feed", url, "--current", "0.1.0"], capture_output=True, text=True, env=env)
    assert v.returncode == 1 and "not signed by a release key" in v.stdout   # the image has no production keys here
