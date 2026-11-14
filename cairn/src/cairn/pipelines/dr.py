"""Disaster recovery for the lakehouse: after failing over to a standby region, make every table readable.

The standby has two copies made independently: the catalog's database (PostgreSQL streaming replication)
and the store's objects (bucket replication to the standby store). Either can be ahead. Where the database
is ahead, a table points at a metadata file, manifest or data file that never reached the standby store,
and cannot be read. ``reconcile`` finds those tables and moves each back to its newest commit whose files
all arrived (and says how many commits that loses); tables whose files are all there are left alone.

    cairn dr reconcile --catalog https://catalog.dr.example.com --token ... --store https://store.dr.example.com \\
        --access-key ... --secret-key-file ... [--apply]

Without ``--apply`` it only reports. Each repair is: drop the table without purging its files, then register
it at the chosen metadata file, so the files of the lost commits stay in the store for inspection.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
META = re.compile(r"^(\d+)-[0-9a-f-]+\.metadata\.json$")


@dataclass
class TableReport:
    table: str
    readable: bool
    action: str = "none"                 # none | rolled-back | unrecoverable
    lost_commits: int = 0
    metadata: Optional[str] = None
    missing: list[str] = field(default_factory=list)


def _split(uri: str) -> tuple[str, str]:
    u = urlsplit(uri)
    return u.netloc, u.path.lstrip("/")


class Reconciler:
    def __init__(self, catalog, s3):
        """``catalog``: a pyiceberg catalog on the standby; ``s3``: a boto3 S3 client on the standby store."""
        self.catalog, self.s3 = catalog, s3

    def _exists(self, uri: str) -> bool:
        from botocore.exceptions import ClientError

        bucket, key = _split(uri)
        try:
            self.s3.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError:
            return False

    def _read_json(self, uri: str) -> dict:
        bucket, key = _split(uri)
        return json.loads(self.s3.get_object(Bucket=bucket, Key=key)["Body"].read())

    def missing_files(self, metadata_uri: str) -> list[str]:
        """What the table at ``metadata_uri`` needs and the store does not have: the metadata file, the current
        snapshot's manifest list, its manifests and their live data files."""
        from pyiceberg.io.pyarrow import PyArrowFileIO
        from pyiceberg.manifest import ManifestFile, read_manifest_list

        if not self._exists(metadata_uri):
            return [metadata_uri]
        meta = self._read_json(metadata_uri)
        cur = meta.get("current-snapshot-id")
        snap = next((s for s in meta.get("snapshots") or [] if s.get("snapshot-id") == cur), None)
        if snap is None:                                   # an empty table: the metadata file is all it needs
            return []
        mlist = snap.get("manifest-list")
        if not mlist or not self._exists(mlist):
            return [mlist or "manifest list"]
        io = self.catalog._load_file_io() if hasattr(self.catalog, "_load_file_io") else PyArrowFileIO(self.catalog.properties)
        missing: list[str] = []
        manifests: list[ManifestFile] = list(read_manifest_list(io.new_input(mlist)))
        for m in manifests:
            if not self._exists(m.manifest_path):
                missing.append(m.manifest_path)
                continue
            for entry in m.fetch_manifest_entry(io, discard_deleted=True):
                if not self._exists(entry.data_file.file_path):
                    missing.append(entry.data_file.file_path)
        return missing

    def _candidates(self, location: str) -> list[str]:
        """The table's metadata files in the standby store, newest first."""
        bucket, prefix = _split(location.rstrip("/") + "/metadata/")
        found = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents") or []:
                name = o["Key"].rsplit("/", 1)[-1]
                m = META.match(name)
                if m:
                    found.append((int(m.group(1)), f"s3://{bucket}/{o['Key']}"))
        return [uri for _, uri in sorted(found, reverse=True)]

    def check(self, ident: str) -> tuple[TableReport, Optional[str], Optional[str]]:
        """(report, current metadata location, table location) for one table."""
        meta_loc, location = self._pointer(ident)
        missing = self.missing_files(meta_loc)
        return TableReport(ident, not missing, metadata=meta_loc, missing=missing[:20]), meta_loc, location

    def _pointer(self, ident: str) -> tuple[str, str]:
        """Where the catalog says the table's current metadata is, without reading that file (it may be
        missing): the catalog's load-table answer names it."""
        ns, name = ident.rsplit(".", 1)
        r = self.catalog._session.get(self.catalog.url(f"namespaces/{ns}/tables/{name}", prefixed=True))
        if r.status_code >= 400:
            body = r.text
            # A catalog that reads the metadata file to answer: the location is in its error, or unknown.
            m = re.search(r"s3://[^\s\"']+\.metadata\.json", body)
            if not m:
                raise RuntimeError(f"{ident}: the catalog cannot say where its metadata is ({r.status_code} {body[:200]})")
            return m.group(0), m.group(0).rsplit("/metadata/", 1)[0]
        data = r.json()
        return data["metadata-location"], data["metadata"]["location"]

    def reconcile(self, apply: bool = False) -> list[TableReport]:
        out = []
        for ns in self.catalog.list_namespaces():
            for ident in self.catalog.list_tables(ns):
                name = ".".join(ident)
                try:
                    rep, meta_loc, location = self.check(name)
                except Exception as e:  # noqa: BLE001 - reported, the others go on
                    out.append(TableReport(name, False, "unrecoverable", missing=[str(e)[:300]]))
                    continue
                if rep.readable:
                    out.append(rep)
                    continue
                chosen, lost = None, 0
                for uri in self._candidates(location):
                    if uri == meta_loc:
                        continue
                    lost += 1
                    if not self.missing_files(uri):
                        chosen = uri
                        break
                if chosen is None:
                    rep.action = "unrecoverable"
                    out.append(rep)
                    continue
                rep.action, rep.lost_commits, rep.metadata = "rolled-back", self._commits_between(chosen, meta_loc), chosen
                if apply:
                    self.catalog.drop_table(name)           # not purged: the files stay
                    self.catalog.register_table(name, chosen)
                    rep.readable = True
                out.append(rep)
        return out

    def _commits_between(self, older: str, newer: str) -> int:
        a, b = META.match(older.rsplit("/", 1)[-1]), META.match(newer.rsplit("/", 1)[-1])
        return int(b.group(1)) - int(a.group(1)) if a and b else 0


def main(args) -> int:
    import boto3
    from pyiceberg.catalog.rest import RestCatalog

    secret = open(args.secret_key_file).read().strip() if args.secret_key_file else args.secret_key
    s3 = boto3.client("s3", endpoint_url=args.store, aws_access_key_id=args.access_key, aws_secret_access_key=secret,
                      region_name=args.region)
    cat = RestCatalog("cairn-dr", uri=args.catalog, token=args.token, **{
        "s3.endpoint": args.store, "s3.access-key-id": args.access_key, "s3.secret-access-key": secret,
        "s3.region": args.region, "s3.path-style-access": "true"})
    started = time.time()
    reports = Reconciler(cat, s3).reconcile(apply=args.apply)
    bad = [r for r in reports if not r.readable]
    for r in reports:
        if r.action != "none" or not r.readable:
            print(json.dumps(r.__dict__))
    print(f"{len(reports)} tables: {sum(r.readable for r in reports)} readable, "
          f"{sum(r.action == 'rolled-back' for r in reports)} rolled back"
          f"{'' if args.apply else ' (to roll back; --apply to do it)'}, "
          f"{sum(r.action == 'unrecoverable' for r in reports)} unrecoverable, in {time.time() - started:.1f} s")
    return 1 if bad else 0
