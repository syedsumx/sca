"""The vendor side of a release feed (L159): a directory of signed files, served as is from any web server, bucket or
customer mirror. Used by deploy/ops/release.py and the tests; an install only reads feeds (cairn.release.feed).

    <feed>/releases/<version>.json      a signed manifest, written once and never changed
    <feed>/channels/<channel>.json      a channel's signed index, re-signed with a rising serial on every change

:func:`publish` adds a release to its channels, :func:`withdraw` takes one off a channel (a release found bad), and
:func:`refresh` re-signs an index unchanged but for a new expiry, which a scheduled job does well before the old one
runs out (an install refuses an expired index).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from cairn.release.manifest import (CHANNEL, ReleaseError, _b64d, check_index, check_manifest, file_digest, sign,
                                    version_key)

Keys = list[tuple[str, Any]]      # (kid, Ed25519PrivateKey)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload(raw: bytes) -> dict:
    return json.loads(_b64d(json.loads(raw)["payload"]))


def sign_manifest(doc: dict, keys: Keys) -> bytes:
    check_manifest(doc)                      # nothing invalid is signed
    return (json.dumps(sign(doc, keys), indent=2) + "\n").encode()


def _index(feed: Path, channel: str) -> dict:
    path = feed / "channels" / f"{channel}.json"
    if not path.exists():
        return {"type": "cairn-channel", "schema": 1, "channel": channel, "serial": 0, "releases": []}
    return _payload(path.read_bytes())


def _sign_index(feed: Path, doc: dict, keys: Keys, days: int, now: Optional[datetime]) -> dict:
    now = now or datetime.now(timezone.utc)
    if not 1 <= days <= 366:
        raise ReleaseError("an index lasts 1 to 366 days")
    doc = {**doc, "serial": int(doc.get("serial") or 0) + 1, "issued_at": _stamp(now),
           "expires_at": _stamp(now + timedelta(days=days)),
           "releases": sorted(doc["releases"], key=lambda r: version_key(r["version"]))}
    check_index(doc)
    _write(feed / "channels" / f"{doc['channel']}.json", (json.dumps(sign(doc, keys), indent=2) + "\n").encode())
    return doc


def publish(feed: Path, signed_manifest: bytes, keys: Keys, days: int = 30, now: Optional[datetime] = None) -> list[dict]:
    """Add a signed manifest to the feed and to each of its channels' indexes. A version is published once: the
    same file again is a no-op for the file, a different one is refused."""
    doc = check_manifest(_payload(signed_manifest))
    path = feed / "releases" / f"{doc.version}.json"
    if path.exists() and path.read_bytes() != signed_manifest:
        raise ReleaseError(f"{doc.version} is already published with other content: a release never changes, "
                           "publish a new version")
    _write(path, signed_manifest)
    entry = {"version": doc.version, "manifest": f"releases/{doc.version}.json", "sha256": file_digest(signed_manifest)}
    out = []
    for channel in doc.channels:
        idx = _index(feed, channel)
        idx["releases"] = [r for r in idx["releases"] if r["version"] != doc.version] + [entry]
        out.append(_sign_index(feed, idx, keys, days, now))
    return out


def withdraw(feed: Path, channel: str, version: str, keys: Keys, days: int = 30,
             now: Optional[datetime] = None) -> dict:
    """Take a release off a channel (its manifest stays, so installs that already run it can still read it)."""
    if not CHANNEL.match(channel):
        raise ReleaseError(f"{channel!r} is not a channel name")
    idx = _index(feed, channel)
    kept = [r for r in idx["releases"] if r["version"] != version]
    if len(kept) == len(idx["releases"]):
        raise ReleaseError(f"{version} is not on {channel}")
    return _sign_index(feed, {**idx, "releases": kept}, keys, days, now)


def refresh(feed: Path, channel: str, keys: Keys, days: int = 30, now: Optional[datetime] = None) -> dict:
    """Re-sign a channel's index as it is, with a new serial and expiry."""
    if not (feed / "channels" / f"{channel}.json").exists():
        raise ReleaseError(f"there is no {channel} channel in {feed}")
    return _sign_index(feed, _index(feed, channel), keys, days, now)
