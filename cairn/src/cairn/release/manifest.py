"""Signed release documents (L159): a release's manifest and a channel's index.

Both are envelopes, ``{"payload": "<base64url JSON>", "signatures": [{"kid", "sig"}]}``; each signature is Ed25519 over
the payload bytes, checked against public keys baked into the image (:data:`KEYS_PATH`), never against keys from
configuration. Several signatures (``threshold``) let a key be rotated: a new key signs next to the old one for a
while. Documents signed with the development key in the source tree are accepted only on the ``dev`` channel.

A **manifest** says what a version is made of, by digest, so what an install pulls is exactly what was signed::

    {"type": "cairn-release", "schema": 1, "version": "2.14.0", "released_at": "2026-11-12T09:00:00Z",
     "channels": ["stable", "early"], "min_from": "2.12.0",
     "severity": "normal" | "security" | "critical", "summary": "...", "notes_url": "https://...",
     "breaking": [{"id": "trino-490", "text": "...", "action": "..."}],
     "migrations": {"head": "0152"},
     "chart": {"name": "cairn", "version": "2.14.0", "url": "oci://registry.example.com/charts/cairn",
               "digest": "sha256:<64 hex>"},
     "images": {"api": "registry.example.com/cairn/api@sha256:<64 hex>", ...},
     "requires": {"kubernetes": "1.28", "helm": "3.12"}}

``min_from`` is the oldest version that may upgrade to it directly (its migrations and chart changes assume it);
older installs step through intermediate releases.

A **channel index** lists a channel's releases and is re-signed whenever it changes, with a rising ``serial`` and an
``expires_at`` (an install refuses an index older than one it has seen, or past its expiry, so a mirror cannot hold
an install on an old release or replay one)::

    {"type": "cairn-channel", "schema": 1, "channel": "stable", "serial": 42,
     "issued_at": "2026-11-12T09:00:00Z", "expires_at": "2026-12-12T09:00:00Z",
     "releases": [{"version": "2.14.0", "manifest": "releases/2.14.0.json", "sha256": "<64 hex of that file>"}]}
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

KEYS_PATH = Path("/usr/share/cairn/release-keys.json")
DEV_KEYS_PATH = Path(__file__).with_name("release_dev_keys.json")
DEV_CHANNEL = "dev"
CHANNEL = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
VERSION = re.compile(r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})(?:-([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,250}@sha256:[0-9a-f]{64}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
SEVERITIES = ("normal", "security", "critical")
MAX_BYTES = 1 << 20          # an index or a manifest larger than this is refused unread


class ReleaseError(ValueError):
    """A release document that is not valid here, and why."""


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def load_keys(path: Path = KEYS_PATH) -> dict[str, Ed25519PublicKey]:
    """``{"keys": {"<kid>": "<base64 raw public key>"}}``; a missing file means no production keys."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    return {kid: Ed25519PublicKey.from_public_bytes(base64.b64decode(k)) for kid, k in (raw.get("keys") or {}).items()}


def version_key(v: str) -> tuple:
    """Semantic version order: 2.14.0-rc.1 < 2.14.0 < 2.14.1 (numeric pre-release parts compare as numbers)."""
    m = VERSION.match(v or "")
    if not m:
        raise ReleaseError(f"{v!r} is not a version (MAJOR.MINOR.PATCH, optionally -pre.release)")
    pre = m.group(4)
    tail: tuple = (1,) if pre is None else (0,) + tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                                                        for p in pre.split("."))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) + tail


def _when(v: Any, what: str) -> datetime:
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise ReleaseError(f"{what}: an ISO 8601 time (UTC)")
    if t.tzinfo is None:
        raise ReleaseError(f"{what}: an ISO 8601 time with its zone (…Z)")
    return t.astimezone(timezone.utc)


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def sign(doc: dict, keys: list[tuple[str, Any]]) -> dict:
    """An envelope of ``doc`` signed by each ``(kid, Ed25519PrivateKey)`` (vendor side: deploy/ops/release.py)."""
    payload = canonical(doc)
    return {"payload": _b64e(payload), "signatures": [{"kid": kid, "sig": _b64e(k.sign(payload))} for kid, k in keys]}


@dataclass
class Opened:
    doc: dict
    kids: list[str]
    development: bool


def open_envelope(raw: bytes, keys: Optional[dict] = None, dev_keys: Optional[dict] = None,
                  threshold: int = 1) -> Opened:
    """The signed document in ``raw``, once ``threshold`` distinct known keys have signed it (or the dev key has:
    then it is a development document, which only the dev channel takes)."""
    if len(raw) > MAX_BYTES:
        raise ReleaseError(f"larger than {MAX_BYTES} bytes")
    keys = load_keys() if keys is None else keys
    dev_keys = load_keys(DEV_KEYS_PATH) if dev_keys is None else dev_keys
    try:
        env = json.loads(raw)
        payload = _b64d(env["payload"])
        sigs = env["signatures"]
        doc = json.loads(payload)
    except (ValueError, KeyError, TypeError):
        raise ReleaseError("not a signed Cairn release document")
    if not isinstance(sigs, list) or not isinstance(doc, dict):
        raise ReleaseError("not a signed Cairn release document")
    good, dev = [], []
    for s in sigs:
        kid = s.get("kid") if isinstance(s, dict) else None
        key = keys.get(kid) if isinstance(kid, str) else None
        pool = good
        if key is None and isinstance(kid, str) and kid in dev_keys:
            key, pool = dev_keys[kid], dev
        if key is None or kid in good or kid in dev:
            continue
        try:
            key.verify(_b64d(str(s.get("sig") or "")), payload)
        except (InvalidSignature, ValueError):
            raise ReleaseError(f"the signature by {kid!r} does not match: the document was altered")
        pool.append(kid)
    if len(good) >= max(1, threshold):
        return Opened(doc, good, False)
    if dev and not good:
        return Opened(doc, dev, True)
    known = len(good)
    raise ReleaseError(f"signed by {known} of the {max(1, threshold)} release keys this install needs"
                       if known else "not signed by a release key this install knows")


@dataclass
class Manifest:
    version: str
    released_at: datetime
    channels: list[str]
    min_from: Optional[str]
    severity: str
    summary: str
    notes_url: str
    breaking: list[dict]
    migrations: dict
    chart: dict
    images: dict[str, str]
    requires: dict[str, str] = field(default_factory=dict)
    development: bool = False

    def public(self) -> dict:
        return {"version": self.version, "released_at": self.released_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "channels": self.channels, "min_from": self.min_from, "severity": self.severity,
                "summary": self.summary, "notes_url": self.notes_url, "breaking": self.breaking,
                "migrations": self.migrations, "chart": self.chart, "images": self.images, "requires": self.requires,
                "development": self.development}


def check_manifest(doc: dict, development: bool = False) -> Manifest:
    """The manifest in ``doc`` checked field by field (a document that is signed can still be wrong)."""
    if doc.get("type") != "cairn-release" or doc.get("schema") != 1:
        raise ReleaseError("not a Cairn release manifest (type cairn-release, schema 1)")
    version = str(doc.get("version") or "")
    version_key(version)
    min_from = doc.get("min_from")
    if min_from is not None:
        if version_key(str(min_from)) >= version_key(version):
            raise ReleaseError("min_from is a version older than the release")
    channels = doc.get("channels")
    if not isinstance(channels, list) or not channels or not all(isinstance(c, str) and CHANNEL.match(c)
                                                                  for c in channels):
        raise ReleaseError("channels: a list of channel names")
    if development and set(channels) != {DEV_CHANNEL}:
        raise ReleaseError("a release signed with the development key is for the dev channel only")
    severity = doc.get("severity", "normal")
    if severity not in SEVERITIES:
        raise ReleaseError(f"severity is one of {', '.join(SEVERITIES)}")
    notes_url = str(doc.get("notes_url") or "")
    if notes_url and not notes_url.startswith("https://"):
        raise ReleaseError("notes_url is an https:// link")
    breaking = doc.get("breaking") or []
    if not isinstance(breaking, list) or not all(isinstance(b, dict) and isinstance(b.get("id"), str)
                                                 and isinstance(b.get("text"), str) for b in breaking):
        raise ReleaseError("breaking: a list of {id, text, action}")
    chart = doc.get("chart")
    if not isinstance(chart, dict) or not chart.get("name") or not DIGEST.match(str(chart.get("digest") or "")):
        raise ReleaseError("chart: {name, version, url, digest: sha256:...}")
    version_key(str(chart.get("version") or ""))
    if not str(chart.get("url") or "").startswith(("oci://", "https://")):
        raise ReleaseError("chart.url is an oci:// or https:// location")
    images = doc.get("images")
    if not isinstance(images, dict) or not images:
        raise ReleaseError("images: {component: image reference}")
    for name, ref in images.items():
        if not isinstance(ref, str) or not IMAGE.match(ref):
            raise ReleaseError(f"images.{name}: pinned by digest (registry/repository@sha256:...), never a tag")
    migrations = doc.get("migrations") or {}
    if not isinstance(migrations, dict) or (migrations.get("head") is not None
                                            and not re.match(r"^\d{4}$", str(migrations["head"]))):
        raise ReleaseError("migrations: {head: the newest migration's number}")
    requires = doc.get("requires") or {}
    if not isinstance(requires, dict) or not all(isinstance(v, str) for v in requires.values()):
        raise ReleaseError("requires: {component: minimum version}")
    return Manifest(version=version, released_at=_when(doc.get("released_at"), "released_at"),
                    channels=list(channels), min_from=str(min_from) if min_from is not None else None,
                    severity=severity, summary=str(doc.get("summary") or "")[:2000], notes_url=notes_url,
                    breaking=[{"id": b["id"], "text": b["text"], "action": str(b.get("action") or "")}
                              for b in breaking],
                    migrations=dict(migrations), chart=dict(chart), images=dict(images), requires=dict(requires),
                    development=development)


@dataclass
class Index:
    channel: str
    serial: int
    issued_at: datetime
    expires_at: datetime
    releases: list[dict]
    development: bool = False


def check_index(doc: dict, development: bool = False) -> Index:
    if doc.get("type") != "cairn-channel" or doc.get("schema") != 1:
        raise ReleaseError("not a Cairn channel index (type cairn-channel, schema 1)")
    channel = str(doc.get("channel") or "")
    if not CHANNEL.match(channel):
        raise ReleaseError("channel: a channel name")
    if development and channel != DEV_CHANNEL:
        raise ReleaseError("an index signed with the development key is for the dev channel only")
    serial = doc.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 1:
        raise ReleaseError("serial: a whole number that rises with each index")
    issued, expires = _when(doc.get("issued_at"), "issued_at"), _when(doc.get("expires_at"), "expires_at")
    if expires <= issued:
        raise ReleaseError("expires_at is after issued_at")
    releases, seen = doc.get("releases"), set()
    if not isinstance(releases, list):
        raise ReleaseError("releases: a list")
    for r in releases:
        if not isinstance(r, dict):
            raise ReleaseError("releases: {version, manifest, sha256} each")
        version_key(str(r.get("version") or ""))
        path = str(r.get("manifest") or "")
        if not PATH.match(path) or ".." in path.split("/"):
            raise ReleaseError(f"releases: {path!r} is a path under the feed")
        if not HEX64.match(str(r.get("sha256") or "")):
            raise ReleaseError("releases: sha256 is the manifest file's hex digest")
        if r["version"] in seen:
            raise ReleaseError(f"releases: {r['version']} is listed twice")
        seen.add(r["version"])
    return Index(channel, serial, issued, expires, [{k: r[k] for k in ("version", "manifest", "sha256")}
                                                    for r in releases], development)


def file_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
