"""An install's view of its updates (L159): the release feed it follows, checked and verified, and the way from the
version it runs to the newest one its channel offers.

Configuration (Helm ``cairn.updates``): ``CAIRN_RELEASE_FEED`` (the feed's base URL: https, or http to this machine
only), ``CAIRN_RELEASE_CHANNEL`` (``stable`` by default), ``CAIRN_RELEASE_THRESHOLD`` (how many release keys must
sign, 1 by default) and ``CAIRN_RELEASE_CA_FILE`` (a private CA for a mirror). The feed is plain files, so a mirror
inside the customer's network serves it as well as the vendor's: ``channels/<channel>.json`` and the manifests it
names (:mod:`cairn.release.manifest`). An air-gapped install follows ``file://`` the folder release bundles are
imported into (:mod:`cairn.release.bundle`, L162); there an index may be past its expiry but not older than
``CAIRN_RELEASE_OFFLINE_MAX_AGE_DAYS`` (180), and its serial still only rises.

A check fails closed. The channel index must be signed by the release keys baked into the image, not expired, not
older than an index this install has seen (its serial is kept), and each manifest must match the digest the index
gives and be signed too; anything else offers no update and says why. Nothing is installed here: L160's update
agent does that, from what this verified.

``GET /v1/system/updates`` (``MANAGE_SYSTEM_OPERATIONS``): the version running, the channel, what is newer, the path
(each step a release whose ``min_from`` the step before meets), the most severe release on it, its breaking changes,
and the last check's time or error. ``POST /v1/system/updates/check`` checks now.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, select
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata
from cairn.release.manifest import (CHANNEL, MAX_BYTES, SEVERITIES, Manifest, ReleaseError, check_index,
                                    check_manifest, file_digest, open_envelope, version_key)

logger = logging.getLogger(__name__)

state_table = Table(
    "cairn_release_state", metadata,
    Column("channel", String(32), primary_key=True),
    Column("serial", Integer, nullable=False),         # the newest index serial this install has accepted
    Column("digest", String(64), nullable=False),      # that index file's sha256: the same serial must not change
    Column("checked_ms", BigInteger, nullable=False),  # the last check, good or not
    Column("ok_ms", BigInteger, nullable=True),        # the last good check
    Column("error", Text, nullable=True),              # why the last check offered nothing, when it failed
    Column("result", JSONText, nullable=True),         # the last good check's findings
)

SKEW = timedelta(minutes=5)      # an index issued further in the future than this is refused
MAX_RELEASES = 200               # manifests fetched per check, newest first


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _feed_url(url: str) -> str:
    u = urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return url.rstrip("/") + "/"
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"):
        return url.rstrip("/") + "/"
    if u.scheme == "file" and not u.netloc and u.path.startswith("/"):      # a bundle imported here (L162)
        return url.rstrip("/") + "/"
    raise ReleaseError("the release feed is an https:// URL (plain http only to this machine), or file:///… for "
                       "release bundles imported into an air-gapped install")


def plan(current: str, manifests: list[Manifest]) -> dict:
    """What is newer than ``current``, and the way to the newest: each step the newest release whose ``min_from``
    the version before it meets. A gap no release bridges is ``blocked``, with the reason."""
    cur = version_key(current)
    newer = sorted((m for m in manifests if version_key(m.version) > cur), key=lambda m: version_key(m.version))
    path: list[Manifest] = []
    at, at_key = current, cur
    blocked = None
    while newer and at_key < version_key(newer[-1].version):
        steps = [m for m in newer if version_key(m.version) > at_key
                 and (m.min_from is None or version_key(m.min_from) <= at_key)]
        if not steps:
            nxt = next(m for m in newer if version_key(m.version) > at_key)
            blocked = (f"no release on this channel upgrades from {at}: {nxt.version} needs {nxt.min_from} or later "
                       "(ask for the release in between, or install it from a bundle)")
            break
        step = steps[-1]
        path.append(step)
        at, at_key = step.version, version_key(step.version)
    # What the path brings in: every newer release up to where it ends (a step that jumps over a release still
    # carries its fixes and its breaking changes).
    covered = [m for m in newer if version_key(m.version) <= at_key]
    severity = max((m.severity for m in covered), key=SEVERITIES.index, default=None)
    return {"available": [m.public() for m in reversed(newer)],
            "path": [m.version for m in path], "target": path[-1].version if path else None,
            "severity": severity, "blocked": blocked,
            "breaking": [{**b, "version": m.version} for m in covered for b in m.breaking],
            "steps": [{"version": m.version, "chart": m.chart, "images": m.images, "migrations": m.migrations,
                       "requires": m.requires} for m in path]}


class ReleaseFeed:
    def __init__(self, engine: Engine, url: str, channel: str = "stable", current: str = "",
                 threshold: int = 1, ca_file: Optional[str] = None, keys: Optional[dict] = None,
                 dev_keys: Optional[dict] = None, http=None, audit=None, offline_max_age_days: int = 180):
        from cairn import __version__

        if not CHANNEL.match(channel or ""):
            raise ReleaseError(f"{channel!r} is not a channel name")
        self.engine, self.url, self.channel = engine, _feed_url(url), channel
        self.current = current or __version__
        version_key(self.current)
        self.threshold, self.keys, self.dev_keys, self.audit = max(1, threshold), keys, dev_keys, audit
        self.ca_file, self._http = ca_file, http      # the client is made at the first check, not at start-up
        # A file:// feed is a bundle someone carried in (L162): its index may be past its expiry, which here guards
        # against nothing (the operator chose the file), but not older than this, and its serial still only rises.
        self.offline = self.url.startswith("file:")
        self.offline_max_age = timedelta(days=max(1, offline_max_age_days))
        self._lock = threading.Lock()

    # ---- fetching ----------------------------------------------------------------------------------------
    def _get(self, path: str) -> bytes:
        if self.offline:
            return self._read_file(path)
        if self._http is None:
            import ssl

            import httpx

            ctx = ssl.create_default_context(cafile=self.ca_file) if self.ca_file else True
            self._http = httpx.Client(timeout=30, follow_redirects=False, verify=ctx,
                                      headers={"User-Agent": "cairn-updates"})
        url = urljoin(self.url, path)
        if not url.startswith(self.url):
            raise ReleaseError(f"{path!r} is outside the feed")
        with self._http.stream("GET", url) as r:
            if r.status_code != 200:
                raise ReleaseError(f"{path}: the feed answered {r.status_code}")
            out = bytearray()
            for chunk in r.iter_bytes():
                out += chunk
                if len(out) > MAX_BYTES:
                    raise ReleaseError(f"{path}: larger than {MAX_BYTES} bytes")
        return bytes(out)

    def _read_file(self, path: str) -> bytes:
        root = Path(urlsplit(self.url).path).resolve()
        target = (root / path).resolve()
        if root not in target.parents:
            raise ReleaseError(f"{path!r} is outside the feed")
        try:
            if target.stat().st_size > MAX_BYTES:
                raise ReleaseError(f"{path}: larger than {MAX_BYTES} bytes")
            return target.read_bytes()
        except FileNotFoundError:
            raise ReleaseError(f"{path}: not in the imported feed (import a release bundle first)")

    def _open(self, raw: bytes):
        return open_envelope(raw, self.keys, self.dev_keys, self.threshold)

    # ---- checking ----------------------------------------------------------------------------------------
    def _row(self) -> Optional[dict]:
        with self.engine.connect() as c:
            r = c.execute(select(state_table).where(state_table.c.channel == self.channel)).mappings().first()
        return dict(r) if r else None

    def _save(self, **values) -> None:
        row = self._row()
        with self.engine.begin() as c:
            if row is None:
                c.execute(state_table.insert().values(channel=self.channel, serial=values.pop("serial", 0),
                                                      digest=values.pop("digest", ""), **values))
            else:
                c.execute(state_table.update().where(state_table.c.channel == self.channel).values(**values))

    def _accept(self, serial: int, digest: str, now: int, result: dict) -> bool:
        """Record a verified index, only if it is not older than what is recorded (another replica, or another
        process, may have accepted a newer one since this check read the state): the serial only moves forward."""
        from sqlalchemy import and_, or_
        from sqlalchemy.exc import IntegrityError

        values = dict(serial=serial, digest=digest, checked_ms=now, ok_ms=now, error=None, result=result)
        t = state_table
        for _ in range(2):
            with self.engine.begin() as c:
                done = c.execute(t.update().where(t.c.channel == self.channel).where(
                    or_(t.c.serial < serial, and_(t.c.serial == serial, t.c.digest == digest))).values(**values))
                if done.rowcount:
                    return True
                if c.execute(select(t.c.channel).where(t.c.channel == self.channel)).first() is not None:
                    return False
            try:
                with self.engine.begin() as c:
                    c.execute(t.insert().values(channel=self.channel, **values))
                return True
            except IntegrityError:
                continue                  # another process inserted it first: compare against theirs
        return False

    def _verify(self) -> tuple[dict, int, str]:
        raw = self._get(f"channels/{self.channel}.json")
        opened = self._open(raw)
        idx = check_index(opened.doc, opened.development)
        if idx.channel != self.channel:
            raise ReleaseError(f"the feed's {self.channel} index is for channel {idx.channel}")
        now = datetime.now(timezone.utc)
        if self.offline:
            if idx.issued_at < now - self.offline_max_age:
                raise ReleaseError(f"the imported {self.channel} index was issued {idx.issued_at:%Y-%m-%d}, more than "
                                   f"{self.offline_max_age.days} days ago: import a newer release bundle")
        elif idx.expires_at <= now:
            raise ReleaseError(f"the {self.channel} index expired at {idx.expires_at:%Y-%m-%dT%H:%M:%SZ}: the feed is "
                               "not being kept current, or something is holding this install on an old one")
        if idx.issued_at > now + SKEW:
            raise ReleaseError("the index was issued in the future: check this machine's clock")
        digest = file_digest(raw)
        row = self._row()
        if row is not None and row["serial"]:
            if idx.serial < row["serial"]:
                raise ReleaseError(f"the index (serial {idx.serial}) is older than one this install has already "
                                   f"seen ({row['serial']}): refused, so the feed cannot roll it back")
            if idx.serial == row["serial"] and digest != row["digest"]:
                raise ReleaseError(f"the index changed without its serial rising ({idx.serial}): refused")
        cur = version_key(self.current)
        wanted = sorted((r for r in idx.releases if version_key(r["version"]) > cur),
                        key=lambda r: version_key(r["version"]))[-MAX_RELEASES:]
        manifests = []
        for r in wanted:
            body = self._get(r["manifest"])
            if file_digest(body) != r["sha256"]:
                raise ReleaseError(f"{r['manifest']}: does not match the digest the index gives")
            m_opened = self._open(body)
            m = check_manifest(m_opened.doc, m_opened.development)
            if m.version != r["version"]:
                raise ReleaseError(f"{r['manifest']}: is {m.version}, the index says {r['version']}")
            if self.channel not in m.channels:
                raise ReleaseError(f"{m.version} is not released on {self.channel}")
            manifests.append(m)
        result = {**plan(self.current, manifests), "serial": idx.serial,
                  "index_expires_at": idx.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "index_issued_at": idx.issued_at.strftime("%Y-%m-%dT%H:%M:%SZ")}
        return result, idx.serial, digest

    def check(self, actor: Optional[str] = None) -> dict:
        """Check the feed now. A failure offers nothing and keeps the reason (and the serial already accepted)."""
        with self._lock:
            now = _now_ms()
            try:
                result, serial, digest = self._verify()
            except Exception as e:      # noqa: BLE001 - any failure (network, signature, content) offers nothing
                why = str(e) if isinstance(e, ReleaseError) else f"the feed could not be read: {type(e).__name__}"
                logger.warning("release feed %s: %s", self.channel, why)
                self._save(checked_ms=now, error=why[:2000])
                self._record(actor, "updates.check_failed", {"channel": self.channel, "error": why[:500]})
                return self.state()
            if not self._accept(serial, digest, now, result):
                why = (f"another replica accepted a newer index than serial {serial} meanwhile: refused, so the "
                       "serial never goes back")
                self._save(checked_ms=now, error=why)
                self._record(actor, "updates.check_failed", {"channel": self.channel, "error": why})
                return self.state()
            self._record(actor, "updates.checked", {"channel": self.channel, "serial": serial,
                                                    "target": result["target"], "severity": result["severity"]})
            return self.state()

    def _record(self, actor: Optional[str], action: str, details: dict) -> None:
        if self.audit is not None and actor is not None:
            try:
                self.audit.record("system", action, actor, f"urn:li:cairnRelease:{self.channel}", details)
            except Exception:   # noqa: BLE001 - the audit trail never decides a check
                logger.exception("audit of %s failed", action)

    def state(self) -> dict:
        row = self._row() or {}
        failed = bool(row.get("error"))
        result = {} if failed else (row.get("result") or {})
        return {"current": self.current, "channel": self.channel, "feed": self.url,
                "checked_at": _iso(row.get("checked_ms")), "last_good_at": _iso(row.get("ok_ms")),
                "serial": row.get("serial") or None, "error": row.get("error"),
                "available": result.get("available", []), "path": result.get("path", []),
                "target": result.get("target"), "severity": result.get("severity"),
                "blocked": result.get("blocked"), "breaking": result.get("breaking", []),
                "steps": result.get("steps", []), "index_expires_at": result.get("index_expires_at"),
                "index_issued_at": result.get("index_issued_at"), "offline": self.offline}


def from_env(engine: Engine, audit=None) -> Optional[ReleaseFeed]:
    url = os.environ.get("CAIRN_RELEASE_FEED", "").strip()
    if not url:
        return None
    return ReleaseFeed(engine, url, os.environ.get("CAIRN_RELEASE_CHANNEL", "stable").strip() or "stable",
                       threshold=int(os.environ.get("CAIRN_RELEASE_THRESHOLD", "1") or 1),
                       ca_file=os.environ.get("CAIRN_RELEASE_CA_FILE") or None, audit=audit,
                       offline_max_age_days=int(os.environ.get("CAIRN_RELEASE_OFFLINE_MAX_AGE_DAYS", "180") or 180))


def build_router(feed: ReleaseFeed, current_principal, authorize):
    from fastapi import APIRouter, Depends, HTTPException

    router = APIRouter(tags=["updates"])

    def admin(p) -> None:
        if authorize is None or getattr(p, "via", "") == "agent":
            raise HTTPException(403, "updates need MANAGE_SYSTEM_OPERATIONS")
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)

    @router.get("/v1/system/updates")
    def updates(p=Depends(current_principal)) -> dict[str, Any]:
        """The version running, the channel, what is newer, and the verified way to the newest."""
        admin(p)
        return feed.state()

    @router.post("/v1/system/updates/check")
    def check_now(p=Depends(current_principal)) -> dict[str, Any]:
        """Check the release feed now (signatures, expiry and serial verified; nothing is installed)."""
        admin(p)
        return feed.check(p.urn)

    return router

