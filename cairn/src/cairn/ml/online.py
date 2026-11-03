"""Online feature store: a feature table's latest values per entity, for a model at prediction time, governed
like a read of the table.

A feature table (``cairn.ml.features``) is served online once its owners turn it on:
``PUT /v1/ml/feature-tables/<name>/online`` (``MANAGE_DATASET`` on its table) with optionally
``timestamp_column`` (the row's event time: the latest row per entity wins; without it a table holds one
row per entity) and ``ttl_seconds`` (values older than this are served as expired). Cairn fills it at once
and keeps it in step: each scheduler tick looks at the table's current snapshot and, when it moved, writes
only the entities whose values changed and removes those no longer in the table. ``POST .../online/sync``
syncs now; ``DELETE .../online`` stops serving and drops the values.

``POST /v1/ml/feature-tables/<name>/online/lookup`` with ``entities`` (each a map of the key columns) and
optionally ``features`` returns, per entity, ``status`` (``found``, ``missing`` or ``expired``), the feature
values, and ``event_ms``. The values are kept in Cairn's database (PostgreSQL in production; or
``CAIRN_ONLINE_STORE_URL``, a database of their own), read by primary key.

**Governed as a read.** The values are kept raw, and each lookup is decided for the caller on the feature
table's source table, as Trino would decide a read of it: no read access, no values; a **column mask** on a
feature masks it the same way (the same strategies per type: ``partial`` keeps an email's first letter and
a number's first figure, ``hash`` a stable value of the same type, ``redact`` a fixed one, ``null``); and a
**row filter** refuses the lookup, since a filter is SQL over the table that an entity lookup cannot apply:
read the table through Trino instead. Lookups are not audited per call; turning serving on and off is.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import threading
import time
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, insert, select, update

from cairn.common.db import metadata
from cairn.ml.features import FeatureError

logger = logging.getLogger(__name__)

MAX_ENTITIES = 1000
BATCH = 500

online_feature_tables = Table(
    "cairn_online_feature_tables", metadata,
    Column("name", String(64), primary_key=True),
    Column("timestamp_column", String(128), nullable=True),
    Column("ttl_seconds", Integer, nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("types", Text, nullable=True),                   # JSON {column: Iceberg type}, as of the last sync
    Column("synced_ms", BigInteger, nullable=True),
    Column("source_snapshot", BigInteger, nullable=True),
    Column("entities", Integer, nullable=True),
    Column("error", String(2000), nullable=True),
)

online_feature_values = Table(
    "cairn_online_feature_values", metadata,
    Column("feature_table", String(64), primary_key=True),
    Column("entity", String(64), primary_key=True),          # sha256 of the key values (canonical JSON)
    Column("key", Text, nullable=False),                     # the key values, JSON
    Column("vals", Text, nullable=False),                    # {feature: value}, JSON
    Column("hash", String(64), nullable=False),
    Column("event_ms", BigInteger, nullable=True),
    Column("updated_ms", BigInteger, nullable=False),
)


def _now() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- values and keys
def _base(iceberg_type: str) -> str:
    t = (iceberg_type or "").lower().strip()
    return t.split("(", 1)[0].strip()


INTS = {"int": 2 ** 31, "integer": 2 ** 31, "long": None, "bigint": None}
FLOATS = {"float", "double"}


def encode(value: Any) -> Any:
    """A value as JSON keeps it: dates and times as ISO text, decimals as text (exact), bytes as hex."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    return str(value)


def entity_id(key: list) -> str:
    return hashlib.sha256(json.dumps(key, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def coerce_key(value: Any, iceberg_type: str) -> Any:
    """A requested key value as the table holds it (so ``"7"`` finds the entity whose bigint key is 7)."""
    base = _base(iceberg_type)
    if value is None:
        raise FeatureError("a key value is never null")
    try:
        if base in INTS:
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError
            return int(value)
        if base in FLOATS:
            return float(value)
        if base == "decimal":
            return str(Decimal(str(value)))
        if base == "boolean":
            if isinstance(value, bool):
                return value
            raise ValueError
        if base == "date":
            return dt.date.fromisoformat(str(value)).isoformat()
    except (ValueError, ArithmeticError):
        raise FeatureError(f"a key value {value!r} is not a {iceberg_type}")
    return str(value) if not isinstance(value, str) else value


def _hash64(value: Any) -> int:
    """As Trino's masks compute it: the first 8 bytes of sha256 of the value as text, signed big-endian."""
    return int.from_bytes(hashlib.sha256(str(value).encode()).digest()[:8], "big", signed=True)


def _mod(a: int, b: int) -> int:
    """SQL's mod: the sign of the dividend."""
    r = abs(a) % b
    return -r if a < 0 else r


def mask_value(value: Any, iceberg_type: str, strategy: str) -> Any:
    """``value`` (as encoded) masked as Trino's column mask for that type and strategy masks it."""
    from cairn.coreproxy.masking import mask_scalar

    if value is None or strategy == "null":
        return None
    base = _base(iceberg_type)
    if base in ("string", "varchar", "uuid"):
        return mask_scalar(value, strategy)
    if base in INTS:
        v = int(value)
        if strategy == "redact":
            return 0
        if strategy == "hash":
            bound = INTS[base]
            return _hash64(v) if bound is None else _mod(_hash64(v), bound)
        if v == 0:
            return 0
        unit = 10 ** int(math.floor(math.log10(abs(v))))
        return int(math.copysign(abs(v) // unit * unit, v))
    if base in FLOATS:
        v = float(value)
        if strategy == "redact":
            return 0.0
        if strategy == "hash":
            return _hash64(value) / 1e9
        if v == 0 or not math.isfinite(v):
            return v
        unit = 10.0 ** math.floor(math.log10(abs(v)))
        return math.copysign(math.floor(abs(v) / unit) * unit, v)
    if base == "decimal":
        d = Decimal(str(value))
        inner = iceberg_type[iceberg_type.find("(") + 1:iceberg_type.rfind(")")].split(",") if "(" in iceberg_type else ["38", "0"]
        precision, scale = int(inner[0]), int(inner[1]) if len(inner) > 1 else 0
        if strategy == "redact":
            return str(Decimal(0).scaleb(-scale))
        if strategy == "hash":
            return str(Decimal(_mod(_hash64(value), 10 ** min(18, max(1, precision - scale)))).scaleb(-scale)
                       .quantize(Decimal(1).scaleb(-scale)))
        if d == 0:
            return str(d)
        exp = d.copy_abs().adjusted()
        cut = d.quantize(Decimal(1).scaleb(exp), rounding=ROUND_DOWN)
        return str(cut.quantize(Decimal(1).scaleb(-scale)) if exp >= -scale else Decimal(0).scaleb(-scale))
    if base == "date":
        if strategy == "redact":
            return "1970-01-01"
        if strategy == "hash":
            return (dt.date(1970, 1, 1) + dt.timedelta(days=abs(_mod(_hash64(value), 36525)))).isoformat()
        return dt.date.fromisoformat(str(value)).replace(day=1).isoformat()
    if base in ("timestamp", "timestamptz", "timestamp_ns", "timestamptz_ns"):
        t = dt.datetime.fromisoformat(str(value))
        epoch = dt.datetime(1970, 1, 1, tzinfo=t.tzinfo)
        if strategy == "redact":
            return epoch.isoformat()
        if strategy == "hash":
            h = _hash64(value)
            return (epoch + dt.timedelta(days=abs(_mod(h, 36525)), seconds=abs(_mod(h, 86400)))).isoformat()
        return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    if base == "time":
        t = dt.time.fromisoformat(str(value))
        if strategy == "partial":
            return t.replace(minute=0, second=0, microsecond=0).isoformat()
        if strategy == "hash":
            s = abs(_mod(_hash64(value), 86400))
            return dt.time(s // 3600, s // 60 % 60, s % 60).isoformat()
        return "00:00:00"
    return None                                   # booleans, binary, nested types: nothing partial to show


def _event_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return int(value.timestamp() * 1000)
    if isinstance(value, dt.date):
        return int(dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    raise FeatureError("timestamp_column: a timestamp, date or number column")


# --------------------------------------------------------------------------- the store
class OnlineFeatures:
    def __init__(self, engine, features, catalog: Callable[[], Any],
                 policy: Callable[[str, str], tuple[bool, bool, Any]],
                 may: Callable[[Any, str, str], bool], audit=None, store_engine=None):
        """``features``: FeatureTables; ``catalog()``: Cairn's own Iceberg catalog (pyiceberg), which reads
        the source tables unmasked; ``policy(actor, dataset_urn)``: (may read, row-filtered, mask plan) for
        a person on a table; ``may(principal, privilege, urn)``: Cairn's authorization."""
        self.engine, self.features, self.catalog, self.policy, self.may, self.audit = \
            engine, features, catalog, policy, may, audit
        self.store = store_engine or engine
        if self.store is not engine:
            online_feature_values.create(self.store, checkfirst=True)
        self._locks: dict[str, threading.Lock] = {}

    # ---- configuration -------------------------------------------------------------------------
    def _definition(self, name: str) -> dict:
        d = self.features.definition(name)
        if d is None:
            raise FeatureError("no such feature table", 404)
        return d

    def _config(self, name: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(online_feature_tables).where(online_feature_tables.c.name == name)).first()
        return dict(r._mapping) if r else None

    def _owner(self, p, d: dict) -> None:
        if not self.may(p, "MANAGE_DATASET", d["source"]):
            raise FeatureError(f"serving {d['name']} online needs MANAGE_DATASET on {d['table']}", 403)

    def status(self, p, name: str) -> dict:
        self.features.get(p, name)                    # 404 unless the caller may see its table
        c = self._config(name)
        if c is None:
            raise FeatureError(f"{name} is not served online", 404)
        return self._public(c)

    def _public(self, c: dict) -> dict:
        return {"name": c["name"], "timestamp_column": c["timestamp_column"], "ttl_seconds": c["ttl_seconds"],
                "enabled": c["enabled"], "created_by": c["created_by"], "synced_ms": c["synced_ms"],
                "source_snapshot": c["source_snapshot"], "entities": c["entities"], "error": c["error"]}

    def enable(self, p, name: str, body: dict) -> dict:
        d = self._definition(name)
        self.features.get(p, name)
        self._owner(p, d)
        ts = body.get("timestamp_column") or None
        ttl = body.get("ttl_seconds")
        if ts is not None and (not isinstance(ts, str) or ts in d["keys"]):
            raise FeatureError("timestamp_column: a column of the table, not a key")
        if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1):
            raise FeatureError("ttl_seconds: a whole number of seconds, at least 1")
        existed = self._config(name)
        values = {"timestamp_column": ts, "ttl_seconds": ttl, "enabled": True}
        with self.engine.begin() as conn:
            if existed:
                conn.execute(update(online_feature_tables).where(online_feature_tables.c.name == name).values(
                    **values, source_snapshot=None))      # the next sync rereads it whole
            else:
                conn.execute(insert(online_feature_tables).values(name=name, created_by=p.urn, created_ms=_now(), **values))
        try:
            self.sync(name)
        except Exception as e:
            if not existed:                            # nothing half-made: not served without its values
                self._drop(name)
            raise FeatureError(f"{name} could not be filled: {e}", 400) if not isinstance(e, FeatureError) else e
        if self.audit is not None:
            self.audit.record("ml", "ml.online_features_enabled", p.urn, f"urn:li:mlFeatureTable:{name}",
                              {"timestamp_column": ts, "ttl_seconds": ttl})
        return self._public(self._config(name))

    def _drop(self, name: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(online_feature_tables).where(online_feature_tables.c.name == name))
        with self.store.begin() as conn:
            conn.execute(delete(online_feature_values).where(online_feature_values.c.feature_table == name))

    def disable(self, p, name: str) -> None:
        d = self._definition(name)
        self.features.get(p, name)
        self._owner(p, d)
        if self._config(name) is None:
            raise FeatureError(f"{name} is not served online", 404)
        self._drop(name)
        if self.audit is not None:
            self.audit.record("ml", "ml.online_features_disabled", p.urn, f"urn:li:mlFeatureTable:{name}", {})

    def sync_now(self, p, name: str) -> dict:
        d = self._definition(name)
        self.features.get(p, name)
        self._owner(p, d)
        if self._config(name) is None:
            raise FeatureError(f"{name} is not served online", 404)
        return self.sync(name, force=True)

    # ---- keeping it in step ----------------------------------------------------------------------
    def sync(self, name: str, force: bool = False) -> dict:
        """Writes the entities whose values changed since the last sync and removes those gone; nothing
        is read when the source table's snapshot has not moved (unless ``force``)."""
        d, c = self._definition(name), self._config(name)
        if c is None:
            raise FeatureError(f"{name} is not served online", 404)
        lock = self._locks.setdefault(name, threading.Lock())
        with lock:
            table = self.catalog().load_table(tuple(d["table"].split(".")))
            snap = table.current_snapshot()
            sid = snap.snapshot_id if snap else 0
            if not force and c["source_snapshot"] is not None and c["source_snapshot"] == sid:
                return {"feature_table": name, "changed": False, "written": 0, "removed": 0, "entities": c["entities"]}
            types = {f.name: str(f.field_type) for f in table.schema().fields}
            ts = c["timestamp_column"]
            missing = [x for x in [*d["keys"], *d["features"], *([ts] if ts else [])] if x not in types]
            if missing:
                raise FeatureError(f"{d['table']} has no column {missing[0]} (any more)")
            cols = list(dict.fromkeys([*d["keys"], *d["features"], *([ts] if ts else [])]))
            want: dict[str, tuple] = {}                # entity -> (key, vals, event_ms, hash)
            duplicates = 0
            for batch in table.scan(selected_fields=tuple(cols)).to_arrow().to_batches():
                for r in batch.to_pylist():
                    key = [encode(r[k]) for k in d["keys"]]
                    if any(k is None for k in key):
                        continue                       # an entity is never keyed by null
                    eid = entity_id(key)
                    event = _event_ms(r[ts]) if ts else None
                    if eid in want:
                        if not ts:
                            duplicates += 1
                        elif (event or -1) <= (want[eid][2] or -1):
                            continue
                    vals = {f: encode(r[f]) for f in d["features"]}
                    body = json.dumps([vals, event], sort_keys=True, separators=(",", ":"), default=str)
                    want[eid] = (key, vals, event, hashlib.sha256(body.encode()).hexdigest())
            with self.store.connect() as conn:
                have = dict(conn.execute(select(online_feature_values.c.entity, online_feature_values.c.hash).where(
                    online_feature_values.c.feature_table == name)).all())
            changed = [e for e, w in want.items() if have.get(e) != w[3]]
            gone = [e for e in have if e not in want]
            now = _now()
            with self.store.begin() as conn:
                for i in range(0, len(changed) + len(gone), BATCH):
                    ids = (changed + gone)[i:i + BATCH]
                    conn.execute(delete(online_feature_values).where(online_feature_values.c.feature_table == name,
                                                                     online_feature_values.c.entity.in_(ids)))
                for i in range(0, len(changed), BATCH):
                    conn.execute(insert(online_feature_values), [
                        {"feature_table": name, "entity": e, "key": json.dumps(want[e][0], default=str),
                         "vals": json.dumps(want[e][1], default=str), "hash": want[e][3], "event_ms": want[e][2],
                         "updated_ms": now} for e in changed[i:i + BATCH]])
            with self.engine.begin() as conn:
                conn.execute(update(online_feature_tables).where(online_feature_tables.c.name == name).values(
                    synced_ms=now, source_snapshot=sid, entities=len(want), types=json.dumps(types), error=None))
        if duplicates:
            logger.warning("online features %s: %d rows repeat an entity (no timestamp_column): the last read wins",
                           name, duplicates)
        return {"feature_table": name, "changed": True, "written": len(changed), "removed": len(gone),
                "entities": len(want), "duplicates": duplicates, "snapshot": sid}

    def run_due(self) -> int:
        """The scheduler's tick: syncs every served feature table whose source table has a new snapshot."""
        n = 0
        with self.engine.connect() as conn:
            names = [r.name for r in conn.execute(select(online_feature_tables.c.name)
                                                  .where(online_feature_tables.c.enabled.is_(True))).all()]
        for name in names:
            try:
                if self.sync(name)["changed"]:
                    n += 1
            except Exception as e:  # noqa: BLE001 - recorded; the others go on
                logger.warning("online features %s: %s", name, e)
                with self.engine.begin() as conn:
                    conn.execute(update(online_feature_tables).where(online_feature_tables.c.name == name)
                                 .values(error=str(e)[:2000]))
        return n

    # ---- serving -----------------------------------------------------------------------------------
    def lookup(self, p, name: str, entities: list, features: Optional[list] = None) -> dict:
        d = self.features.get(p, name)                 # 404 unless the caller may see its table
        c = self._config(name)
        if c is None or not c["enabled"]:
            raise FeatureError(f"{name} is not served online", 404)
        if c["synced_ms"] is None:
            raise FeatureError(f"{name} has not been filled yet", 409)
        read, filtered, plan = self.policy(p.urn, d["source"])
        if not read:
            raise FeatureError(f"looking up {name} needs read access to {d['table']}", 403)
        if filtered:
            raise FeatureError(f"a row filter applies to you on {d['table']}; an entity lookup cannot apply it, "
                               "so read the table through Trino instead", 403)
        wanted = list(features) if features else list(d["features"])
        unknown = [f for f in wanted if f not in d["features"]]
        if unknown:
            raise FeatureError(f"{name} has no feature {unknown[0]}")
        if not isinstance(entities, list) or not entities:
            raise FeatureError("entities: a list of key maps")
        if len(entities) > MAX_ENTITIES:
            raise FeatureError(f"at most {MAX_ENTITIES} entities per lookup")
        types = json.loads(c["types"] or "{}")
        ids = []
        for e in entities:
            if not isinstance(e, dict) or set(e) != set(d["keys"]):
                raise FeatureError(f"each entity names exactly the keys {', '.join(d['keys'])}")
            ids.append(entity_id([coerce_key(e[k], types.get(k, "string")) for k in d["keys"]]))
        rows: dict[str, Any] = {}
        with self.store.connect() as conn:
            for i in range(0, len(ids), BATCH):
                for r in conn.execute(select(online_feature_values).where(
                        online_feature_values.c.feature_table == name,
                        online_feature_values.c.entity.in_(ids[i:i + BATCH]))).all():
                    rows[r.entity] = r
        masked = [f for f in wanted if plan is not None and plan.masks_field(f)]
        strategy = getattr(plan, "strategy", "partial")
        now = _now()
        out = []
        for e, eid in zip(entities, ids):
            r = rows.get(eid)
            if r is None:
                out.append({"key": e, "status": "missing", "features": {f: None for f in wanted}, "event_ms": None})
                continue
            age_from = r.event_ms if r.event_ms is not None else r.updated_ms
            if c["ttl_seconds"] and now - age_from > c["ttl_seconds"] * 1000:
                out.append({"key": e, "status": "expired", "features": {f: None for f in wanted}, "event_ms": r.event_ms})
                continue
            vals = json.loads(r.vals)
            out.append({"key": e, "status": "found", "event_ms": r.event_ms, "features": {
                f: mask_value(vals.get(f), types.get(f, "string"), strategy) if f in masked else vals.get(f)
                for f in wanted}})
        return {"feature_table": name, "snapshot": c["source_snapshot"], "synced_ms": c["synced_ms"],
                "masked": masked, "results": out}


def build_online_router(store: OnlineFeatures, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/ml/feature-tables", tags=["ml"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except FeatureError as e:
            raise HTTPException(e.status, str(e))

    def person(p):
        if p.is_service:
            raise HTTPException(403, "features are looked up as a person or a pipeline user")
        return p

    @router.get("/{name}/online")
    async def status(name: str, p=Depends(current_principal)):
        return await call(lambda: store.status(p, name))

    @router.put("/{name}/online")
    async def enable(name: str, body: dict = Body(default_factory=dict), p=Depends(current_principal)):
        return await call(lambda: store.enable(person(p), name, body))

    @router.delete("/{name}/online", status_code=204)
    async def disable(name: str, p=Depends(current_principal)):
        await call(lambda: store.disable(person(p), name))

    @router.post("/{name}/online/sync")
    async def sync(name: str, p=Depends(current_principal)):
        return await call(lambda: store.sync_now(person(p), name))

    @router.post("/{name}/online/lookup")
    async def lookup(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: store.lookup(person(p), name, body.get("entities"), body.get("features")))

    return router
