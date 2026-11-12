"""Release bundles for air-gapped installs (L162): one file that carries a channel's releases into a network with no
way out, and its import there.

**The vendor** makes a bundle from its feed (``deploy/ops/release.py bundle``): the channel's signed index, the signed
manifests newer than the oldest version it is for (``--from``), and for every release on the way up from there, the
chart (``charts/<sha256>.tgz``) and the images, by digest, as an OCI image layout (``images/``). A table of contents
(``cairn-bundle.json``) names what is inside; nothing in it is trusted.

**An install** imports it (``cairn updates import BUNDLE --registry registry.internal/cairn``):

1. Unpack it safely (regular files and folders only, no path leaving the bundle, no links).
2. Verify it as a feed: the index and every manifest signed by the release keys baked into the image, the serial not
   older than one this install has seen, the index not older than ``CAIRN_RELEASE_OFFLINE_MAX_AGE_DAYS`` (180).
3. For each release on this install's way up: the chart's sha256 is the one its manifest signs, and every image is
   in the layout with each of its blobs hashed to its digest. Anything missing or altered: nothing is changed.
4. Push the images into the install's registry (``--registry``; the same digests, so the chart, rendered with
   ``global.cairn.registry`` set to that registry, pulls exactly what was signed), and keep the charts in
   ``<store>/charts``.
5. Only then replace ``<store>/feed`` (the install follows ``file://<store>/feed``) and check it, so the install is
   never offered a release whose images are not already in its registry.

The updater (L160) then plans and applies as with an online feed, taking charts from ``<store>/charts``
(``CAIRN_UPDATES_CHART_DIR``).
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from cairn.release.manifest import ReleaseError, version_key
from cairn.release.oci import Layout, Registry, parse_ref, pull, push, read_auths, split_target

TOC = "cairn-bundle.json"


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _verify_feed(feed_dir: Path, channel: str, current: str, engine=None, keys=None, dev_keys=None,
                 threshold: int = 1, max_age_days: int = 180):
    """The feed in ``feed_dir`` checked as an install checks one: (ReleaseFeed, result). With ``engine``, against
    that install's accepted serial; without, against a scratch one."""
    import sqlalchemy

    from cairn.common.db import metadata
    from cairn.release.feed import ReleaseFeed, state_table

    scratch = None
    if engine is None:
        scratch = tempfile.mkdtemp(prefix="cairn-feed-")
        engine = sqlalchemy.create_engine(f"sqlite:///{scratch}/state.db")
        metadata.create_all(engine, tables=[state_table])
    try:
        feed = ReleaseFeed(engine, feed_dir.resolve().as_uri(), channel, current, threshold, keys=keys,
                           dev_keys=dev_keys, offline_max_age_days=max_age_days)
        result, serial, digest = feed._verify()
        return feed, result, serial, digest
    finally:
        if scratch:
            engine.dispose()
            shutil.rmtree(scratch, ignore_errors=True)


# ---- making one (the vendor) -----------------------------------------------------------------------------------
def make(feed_dir: Path, channel: str, from_version: str, out: Path, *, registry_auth: Optional[str] = None,
         ca_file: Optional[str] = None, plain_http: bool = False, helm: str = "helm", keys=None, dev_keys=None,
         threshold: int = 1, log: Callable[[str], None] = print) -> dict:
    """A bundle of ``channel`` for installs at ``from_version`` or later, written to ``out`` (a tar)."""
    from cairn.release.runner import Runner, StepFailed

    feed_dir = Path(feed_dir)
    _, result, serial, _ = _verify_feed(feed_dir, channel, from_version, keys=keys, dev_keys=dev_keys,
                                        threshold=threshold, max_age_days=36500)
    if result["blocked"]:
        raise ReleaseError(result["blocked"])
    if not result["steps"]:
        raise ReleaseError(f"nothing on {channel} is newer than {from_version}")
    auths = read_auths(registry_auth)
    with tempfile.TemporaryDirectory(prefix="cairn-bundle-", dir=out.parent) as tmp:
        root = Path(tmp)
        feed_out = root / "feed"
        (feed_out / "channels").mkdir(parents=True)
        shutil.copyfile(feed_dir / "channels" / f"{channel}.json", feed_out / "channels" / f"{channel}.json")
        # Every manifest an install at from_version (or later) reads: those the index lists newer than it.
        from cairn.release.manifest import check_index, open_envelope

        opened = open_envelope((feed_dir / "channels" / f"{channel}.json").read_bytes(), keys, dev_keys, threshold)
        for r in check_index(opened.doc, opened.development).releases:
            if version_key(r["version"]) > version_key(from_version):
                dest = feed_out / r["manifest"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(feed_dir / r["manifest"], dest)
        charts = root / "charts"
        charts.mkdir()
        layout = Layout(root / "images")
        layout.init()
        registries: dict[str, Registry] = {}
        images: dict[str, dict] = {}
        fetcher = Runner("", "", "", helm=helm, plain_http=plain_http)
        for step in result["steps"]:
            name = step["chart"]["digest"].split(":", 1)[1] + ".tgz"
            if not (charts / name).exists():
                with tempfile.TemporaryDirectory(dir=root) as d:
                    try:
                        tgz = fetcher.fetch_chart(step, Path(d))         # its sha256 checked against the manifest
                    except StepFailed as e:
                        raise ReleaseError(str(e))
                    shutil.move(str(tgz), charts / name)
                log(f"{step['version']}: chart {step['chart']['name']} {step['chart']['version']}")
            for ref in step["images"].values():
                if ref in images:
                    continue
                host, repo, digest = parse_ref(ref)
                if host not in registries:
                    registries[host] = Registry(host, auths.get(host), ca_file, plain_http)
                media = pull(registries[host], repo, digest, layout, ref)
                images[ref] = {"digest": digest, "media": media, "bytes": layout.verify(digest, media)}
                log(f"{step['version']}: {ref} ({images[ref]['bytes'] // 1024} KiB)")
        toc = {"type": "cairn-bundle", "schema": 1, "channel": channel, "from": from_version,
               "to": result["target"], "path": result["path"], "serial": serial,
               "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "images": images, "charts": sorted(p.name for p in charts.iterdir())}
        (root / TOC).write_text(json.dumps(toc, indent=1))
        part = out.with_name(out.name + ".part")
        with tarfile.open(part, "w") as tar:
            for p in [root / TOC, *sorted(x for x in root.rglob("*") if x.name != TOC)]:
                if p.is_dir() or p.is_file():
                    tar.add(p, arcname=str(p.relative_to(root)), recursive=False, filter=_clean)
        os.replace(part, out)
    return toc


def _clean(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.mode = 0o755 if info.isdir() else 0o644
    return info


# ---- importing one (the install) ---------------------------------------------------------------------------------
def _unpack(bundle: Path, into: Path, max_bytes: int) -> None:
    """Regular files and folders only, each inside ``into``; links, devices and paths that leave it are refused."""
    total = 0
    with tarfile.open(bundle, "r:*") as tar:
        for m in tar:
            name = m.name
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise ReleaseError(f"the bundle names a path outside itself ({name!r}): refused")
            if not (m.isfile() or m.isdir()):
                raise ReleaseError(f"the bundle holds {name!r}, which is not a file or a folder: refused")
            total += m.size
            if total > max_bytes:
                raise ReleaseError(f"the bundle unpacks to more than {max_bytes} bytes: refused")
            target = into / name
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            with target.open("wb") as f:
                shutil.copyfileobj(src, f, 1 << 20)


def import_bundle(bundle: Path, store: Path, target: str, *, engine, channel: str, current: str,
                  registry_auth: Optional[str] = None, ca_file: Optional[str] = None, plain_http: bool = False,
                  keys=None, dev_keys=None, threshold: int = 1, max_age_days: int = 180, audit=None,
                  actor: str = "cli", max_bytes: int = 200 << 30, log: Callable[[str], None] = print) -> dict:
    """Verify ``bundle`` for this install, push its images to ``target``, keep its charts and install its feed under
    ``store``. Nothing is changed unless all of it verifies."""
    import fcntl

    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    host, prefix = split_target(target)
    lock = open(store / ".import.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)        # one import at a time: they swap the same feed
    except BlockingIOError:
        lock.close()
        raise ReleaseError("another import is running on this store: wait for it to finish")
    work = Path(tempfile.mkdtemp(prefix="incoming-", dir=store))
    try:
        try:
            _unpack(Path(bundle), work, max_bytes)
        except (OSError, tarfile.TarError) as e:
            raise ReleaseError(f"the bundle could not be unpacked ({type(e).__name__}: {e})")
        if not (work / "feed" / "channels" / f"{channel}.json").is_file():
            raise ReleaseError(f"the bundle carries no {channel} channel (this install follows {channel})")
        feed, result, serial, _ = _verify_feed(work / "feed", channel, current, engine, keys, dev_keys, threshold,
                                               max_age_days)
        if result["blocked"]:
            raise ReleaseError(result["blocked"])
        layout = Layout(work / "images")
        by_digest = {e["digest"]: e.get("mediaType", "") for e in layout.entries()}
        checked: dict[str, str] = {}
        for step in result["steps"]:
            chart = work / "charts" / (step["chart"]["digest"].split(":", 1)[1] + ".tgz")
            if not chart.is_file():
                raise ReleaseError(f"the bundle lacks the chart of {step['version']}: this bundle is for installs at "
                                   f"its 'from' version or later; this install runs {current}")
            if _sha256_file(chart) != step["chart"]["digest"]:
                raise ReleaseError(f"the chart of {step['version']} in the bundle is not the one its release signs: "
                                   "refused")
            for ref in step["images"].values():
                _, _, digest = parse_ref(ref)
                if digest in checked:
                    continue
                if digest not in by_digest:
                    raise ReleaseError(f"the bundle lacks {ref} ({step['version']})")
                layout.verify(digest, by_digest[digest])
                checked[digest] = ref
        log(f"verified: {len(result['steps'])} release(s) ({', '.join(result['path']) or 'none newer'}), "
            f"{len(checked)} image(s), serial {serial}")

        # Images first: the feed is installed only once everything it offers can be pulled here.
        reg = Registry(host, read_auths(registry_auth).get(host), ca_file, plain_http)
        pushed = []
        for digest, ref in sorted(checked.items(), key=lambda kv: kv[1]):
            _, repo, _ = parse_ref(ref)
            dest = f"{prefix}/{repo.rsplit('/', 1)[-1]}" if prefix else repo.rsplit("/", 1)[-1]
            sent = push(layout, digest, by_digest[digest], reg, dest)
            reg.manifest(dest, digest)                     # read back: the registry serves exactly that digest
            pushed.append(f"{host}/{dest}@{digest}")
            log(f"pushed {host}/{dest}@{digest[:19]}… ({sent} new blob(s))")
        charts = store / "charts"
        charts.mkdir(exist_ok=True)
        for step in result["steps"]:
            name = step["chart"]["digest"].split(":", 1)[1] + ".tgz"
            if not (charts / name).exists():
                shutil.copyfile(work / "charts" / name, charts / (name + ".part"))
                os.replace(charts / (name + ".part"), charts / name)

        # The feed last, swapped whole.
        new, live, old = store / "feed.new", store / "feed", store / "feed.old"
        shutil.rmtree(new, ignore_errors=True)
        shutil.copytree(work / "feed", new)
        shutil.rmtree(old, ignore_errors=True)
        if live.exists():
            os.replace(live, old)
        os.replace(new, live)
        shutil.rmtree(old, ignore_errors=True)
        from cairn.release.feed import ReleaseFeed

        state = ReleaseFeed(engine, live.resolve().as_uri(), channel, current, threshold, keys=keys,
                            dev_keys=dev_keys, offline_max_age_days=max_age_days, audit=audit).check(actor)
        if state["error"]:
            raise ReleaseError(f"the imported feed did not check: {state['error']}")
        out = {"channel": channel, "serial": serial, "path": result["path"], "target": result["target"],
               "images": pushed, "registry": target, "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if audit is not None:
            try:
                audit.record("system", "updates.bundle_imported", actor, f"urn:li:cairnRelease:{channel}",
                             {k: out[k] for k in ("serial", "path", "registry")})
            except Exception:   # noqa: BLE001 - the audit trail never undoes an import
                pass
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)
        lock.close()


__all__ = ["import_bundle", "make"]
