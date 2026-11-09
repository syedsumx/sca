"""Data residency: lakehouse namespaces pinned to a region, so their data is stored and processed only there and
leaves only for the regions the pin allows.

The rules are deployment configuration, not an API: one JSON file (``CAIRN_RESIDENCY_FILE``, a ConfigMap in
Kubernetes) that Cairn and Cairn Catalog both read, so a pin is changed by a reviewed deployment change and no
administrator, however privileged, moves pinned data by changing a setting::

    {"regions": {"eu": {"location": "s3://lake-eu/warehouse"}, "us": {"location": "s3://lake-us/warehouse"}},
     "pins": [{"namespaces": ["crm_eu", "hr_*"], "region": "eu"},
              {"namespaces": ["sales_us"], "region": "us", "allow": ["us", "eu"]}]}

* ``regions``: each region's storage location. Cairn Catalog keeps a pinned namespace's tables under
  ``<location>/<namespace>``, and refuses any other location for them (created, registered, moved, cloned,
  renamed in or out, ``write.*.path``), and any change of the namespace's own ``location``.
* ``pins``: namespaces (names or ``*`` patterns, as Trino and Spark name them) and their region. ``allow`` lists
  the regions their data may be processed in and sent to: their own region alone unless it says more.
* ``CAIRN_REGION``: the region this deployment runs in (Trino, Spark, the catalog's engines). A pinned table is
  read and written only when this region is in its pin's ``allow``: a standby, a restored copy or a test
  deployment elsewhere sees the table but no rows. Unset, pinned tables are processed nowhere.
* Leaving: a share's recipient and a reverse sync's destination carry a ``region``; a pinned table goes only
  to a region its pin allows. A destination without one is refused.
* Copies made by SQL follow the pin (``CopyGuard``): a Trino statement that reads a pinned table and writes a
  table (``CREATE TABLE ... AS``, ``INSERT ... SELECT``, ``UPDATE``, ``MERGE``, materialized views) is refused
  before it runs unless the written table's namespace is pinned to a region the source's pin allows. An
  unpinned namespace, or a table in another catalog, is a destination with no region: refused. Trino asks
  about the statement's tables one at a time; each question carries the statement's query id, and Cairn keeps
  what each statement read and writes (``cairn_residency_flows``, shared by Cairn's replicas, for a day), so
  whichever question completes a forbidden pair is refused. Namespaces never inherit a pin when they are
  created: pins come only from the rules file, so a copy goes into a namespace the file already pins.
* Cairn Store follows the same file (``CAIRN_STORE_RESIDENCY_FILE``): objects under a region's location are
  replicated, backed up, written back or tiered only to a destination whose ``residency_region`` their pin
  allows, checked when that is configured and again for every object sent.

Anything that cannot be decided (a file that does not load, a namespace that two pins name with different
regions) refuses: Cairn fails closed. A file that is set but invalid stops Cairn at start.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, PrimaryKeyConstraint, String, Table, Text, delete, insert, select

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

REGION = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
LOCATION = re.compile(r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](/[^\s]*)?$")
NAMESPACE = re.compile(r"^[A-Za-z0-9_*.?-]{1,256}$")


# What each Trino statement read from pinned namespaces and writes (CopyGuard): one row per statement, side and
# namespace, kept a day (a statement is analysed in seconds; the rows only have to outlive its analysis).
residency_flows = Table(
    "cairn_residency_flows", metadata,
    Column("query_id", String(128), nullable=False),
    Column("side", String(8), nullable=False),                 # source (a pinned namespace read) or target
    Column("namespace", String(512), nullable=False),          # catalog.schema
    Column("region", String(32), nullable=True),               # target: its pin's region (None: no region)
    Column("allow", Text, nullable=True),                      # source: JSON list of the regions its pin allows
    Column("created_ms", BigInteger, nullable=False, index=True),
    PrimaryKeyConstraint("query_id", "side", "namespace"),
)
FLOW_TTL_MS = 24 * 3600 * 1000


class ResidencyError(Exception):
    """The rules do not hold together, or a namespace cannot be decided."""


class Residency:
    def __init__(self, doc: Optional[dict] = None, region: Optional[str] = None):
        doc = {} if doc is None else doc
        if not isinstance(doc, dict):
            raise ResidencyError("the residency file is a JSON object")
        # Known keys only: a misspelt key would otherwise leave namespaces unpinned.
        if set(doc) - {"regions", "pins"}:
            raise ResidencyError(f"unknown keys in the residency file: {', '.join(sorted(set(doc) - {'regions', 'pins'}))}")
        self.region = (region or "").strip() or None
        if self.region is not None and not REGION.match(self.region):
            raise ResidencyError(f"CAIRN_REGION: a region name (got {self.region[:40]!r})")
        regions = doc.get("regions") or {}
        if not isinstance(regions, dict):
            raise ResidencyError("regions: an object of region: {location}")
        self.regions: dict[str, str] = {}
        for name, r in regions.items():
            loc = r.get("location") if isinstance(r, dict) and set(r) == {"location"} else None
            if not REGION.match(str(name)) or not isinstance(loc, str) or not LOCATION.match(loc):
                raise ResidencyError(f"regions.{name}: a region name and an s3:// location")
            self.regions[name] = loc.rstrip("/")
        self.pins: list[dict[str, Any]] = []
        for i, p in enumerate(doc.get("pins") or []):
            if not isinstance(p, dict) or set(p) - {"namespaces", "region", "allow"}:
                raise ResidencyError(f"pins[{i}]: an object of namespaces, region and allow")
            names, region = p.get("namespaces"), p.get("region")
            allow = p.get("allow") or [region]
            if not isinstance(names, list) or not names or not all(isinstance(n, str) and NAMESPACE.match(n) for n in names):
                raise ResidencyError(f"pins[{i}].namespaces: namespace names or patterns")
            if region not in self.regions:
                raise ResidencyError(f"pins[{i}].region: one of the regions ({', '.join(self.regions) or 'none'})")
            if not isinstance(allow, list) or region not in allow or not all(a in self.regions for a in allow):
                raise ResidencyError(f"pins[{i}].allow: regions, its own ({region}) among them")
            self.pins.append({"namespaces": [n.lower() for n in names], "region": region, "allow": sorted(set(allow))})

    @classmethod
    def from_env(cls) -> "Residency":
        path = os.environ.get("CAIRN_RESIDENCY_FILE", "").strip()
        region = os.environ.get("CAIRN_REGION", "")
        if not path:
            return cls({}, region)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            raise ResidencyError(f"CAIRN_RESIDENCY_FILE {path}: {e}") from e
        return cls(doc, region)

    @property
    def active(self) -> bool:
        return bool(self.pins)

    def pin(self, namespace: str) -> Optional[dict]:
        """The pin of a namespace (``a`` or ``a.b``), or None. Two pins naming it with different regions or
        allowances: a ResidencyError (callers refuse)."""
        ns = namespace.lower()
        found = [p for p in self.pins if any(fnmatch.fnmatchcase(ns, n) for n in p["namespaces"])]
        if not found:
            return None
        if any((p["region"], p["allow"]) != (found[0]["region"], found[0]["allow"]) for p in found[1:]):
            raise ResidencyError(f"{namespace} is pinned to more than one region")
        return {"region": found[0]["region"], "allow": list(found[0]["allow"]),
                "location": f"{self.regions[found[0]['region']]}/{ns.replace('.', '/')}"}

    def processing_allowed(self, namespace: str) -> bool:
        """May this deployment read and write the namespace's tables?"""
        try:
            p = self.pin(namespace)
        except ResidencyError as e:
            logger.warning("residency: %s", e)
            return False
        return p is None or (self.region is not None and self.region in p["allow"])

    def transfer_allowed(self, namespace: str, destination: Optional[str]) -> bool:
        """May the namespace's data go to a destination in ``destination`` (a region; None when unknown)?"""
        try:
            p = self.pin(namespace)
        except ResidencyError as e:
            logger.warning("residency: %s", e)
            return False
        return p is None or (destination is not None and destination in p["allow"])

    def check_transfer(self, namespaces, destination: Optional[str], what: str) -> None:
        """Refuses (ResidencyError, with the reason) when any of the namespaces may not go to ``destination``."""
        bad = sorted({ns for ns in namespaces if not self.transfer_allowed(ns, destination)})
        if bad:
            where = f"region {destination}" if destination else "a destination with no region"
            raise ResidencyError(f"{what}: {', '.join(bad)} may not leave for {where} (data residency)")

    def describe(self) -> dict:
        return {"region": self.region, "regions": dict(self.regions), "pins": [dict(p) for p in self.pins]}


class CopyGuard:
    """Copies made by SQL follow the pin: a statement that reads a pinned namespace writes only into a namespace
    pinned to a region that pin allows. Trino asks about each table of a statement separately, with the statement's
    query id; the guard remembers (in the database, so every Cairn replica sees it) the pinned namespaces each
    statement read and the namespaces it writes, and refuses the question that would complete a forbidden pair,
    whichever comes first. A question it cannot decide (no query id, the database down) is refused."""

    def __init__(self, engine, residency: Residency, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.engine, self.residency, self.clock = engine, residency, clock
        self._swept = 0

    def _rows(self, c, query_id: str, side: str) -> list:
        return list(c.execute(select(residency_flows).where(residency_flows.c.query_id == query_id,
                                                            residency_flows.c.side == side)))

    def _record(self, c, query_id: str, side: str, namespace: str, region=None, allow=None) -> None:
        now = self.clock()
        if now - self._swept > 600_000:                # now and then: statements analysed long ago
            self._swept = now
            c.execute(delete(residency_flows).where(residency_flows.c.created_ms < now - FLOW_TTL_MS))
        exists = c.execute(select(residency_flows.c.query_id).where(
            residency_flows.c.query_id == query_id, residency_flows.c.side == side,
            residency_flows.c.namespace == namespace)).first()
        if exists is None:
            c.execute(insert(residency_flows).values(query_id=query_id, side=side, namespace=namespace, region=region,
                                                     allow=None if allow is None else json.dumps(allow),
                                                     created_ms=now))

    def read(self, query_id: Optional[str], namespace: str, pinned_as: Optional[str]) -> Optional[str]:
        """A statement reads ``namespace`` (``catalog.schema``); ``pinned_as`` is the schema when the namespace is
        in the lakehouse catalog (pins name its schemas), else None. The reason to refuse, or None."""
        if pinned_as is None:
            return None
        try:
            pin = self.residency.pin(pinned_as)
        except ResidencyError as e:
            return str(e)
        if pin is None:
            return None
        if not query_id:
            return f"a read of {namespace} (pinned to {pin['region']}) carries no query id to follow"
        with self.engine.begin() as c:
            for t in self._rows(c, query_id, "target"):
                if t.region not in pin["allow"]:
                    return (f"{namespace} is pinned to {pin['region']} and may go only to {', '.join(pin['allow'])}: "
                            f"this statement writes into {t.namespace} ({_where(t.region)})")
            self._record(c, query_id, "source", namespace, allow=pin["allow"])
        return None

    def write(self, query_id: Optional[str], namespace: str, pinned_as: Optional[str]) -> Optional[str]:
        """A statement writes a table of ``namespace``: the reason to refuse, or None."""
        region = None
        if pinned_as is not None:
            try:
                pin = self.residency.pin(pinned_as)
            except ResidencyError as e:
                return str(e)
            region = pin["region"] if pin else None
        if not query_id:
            return f"a write into {namespace} carries no query id to follow"
        with self.engine.begin() as c:
            for s in self._rows(c, query_id, "source"):
                allow = json.loads(s.allow or "[]")
                if region not in allow:
                    return (f"this statement reads {s.namespace}, which may go only to {', '.join(allow)}, "
                            f"and writes into {namespace} ({_where(region)})")
            self._record(c, query_id, "target", namespace, region=region)
        return None


def _where(region: Optional[str]) -> str:
    return f"region {region}" if region else "a namespace with no region"


def valid_region(value: Any) -> Optional[str]:
    """A destination's region as given (None for none); a malformed one is refused."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not REGION.match(value):
        raise ResidencyError("region: a region name such as eu or us-east")
    return value


def build_residency_router(residency: Residency, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Depends

    router = APIRouter(prefix="/governance/residency", tags=["governance"])

    @router.get("")
    def rules(p=Depends(current_principal)):
        """The deployment's region and its residency pins (read only: they are deployment configuration)."""
        authorize(p, "MANAGE_POLICIES", None)
        return residency.describe()

    return router
