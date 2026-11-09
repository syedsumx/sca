"""Sharing lakehouse tables with recipients outside the organisation, through Iceberg REST.

A share names a recipient (another organisation, a partner), the tables it may read, and when it ends.
The recipient gets one credential and reads the tables with any Iceberg REST client (pyiceberg, Spark,
Trino, Snowflake, DuckDB) from Cairn Catalog:

* the client exchanges the credential for a token at ``POST /v1/shares/oauth/tokens`` (OAuth 2.0 client
  credentials, as Iceberg's REST client does by itself). Tokens last ``CAIRN_SHARES_TOKEN_TTL`` (15
  minutes) and are renewed with the same credential while the share lasts;
* Cairn Catalog asks Cairn about every request the recipient makes (no caching), so the recipient sees
  only the shared tables and their namespaces, read-only, and nothing else in the catalog;
* revoking a share, or its expiry, stops it at once: the next request is refused, and so is the next
  renewal. Storage keys already handed out are the catalog's scoped keys for one table, read-only, and
  last at most the catalog's credential lifetime (``CAIRN_CATALOG_VEND_SECONDS``);
* a table to which a column mask or row filter applies is never shared: its files hold what the mask or
  filter hides. That is checked when the share is made and again on every request, so a mask added
  later stops the table from being read through the share;
* a table pinned to a region (data residency, ``cairn.governance.residency``) is shared only with a recipient
  whose ``region`` its pin allows, also checked on every request: a recipient with no region gets no pinned
  table.

The same share is served through the open Delta Sharing protocol too (``cairn.dataaccess.delta_sharing``): the
answer to making a share holds a Delta Sharing profile (``delta_sharing``) with the same credential as a bearer
token, under the same rules.

Only people who may manage every table they share (``MANAGE`` on it) make shares; operators
(``MANAGE_SYSTEM_OPERATIONS``) see and revoke all of them. Only a hash of the credential is kept. Every
change is audited in the same transaction, and so is every token the recipient gets.

**Usage.** Each question Cairn Catalog asks about a recipient, and each token, is counted per share, day and
table (``cairn_share_usage``, kept ``CAIRN_SHARES_USAGE_RETENTION_DAYS``, 90 by default): requests, table
loads (a question about one table: loading its metadata, snapshot and files), tables refused, failures
(decisions that could not be made, wrong secrets) and tokens, with the last access. Counts are gathered in
memory and written every few seconds. ``GET /v1/shares/<name or id>/usage`` gives one share's;
``GET /v1/shares/usage/overview`` all the caller's shares: unused ones, those expiring soon, and recipients
reading tables whose quality is failing (the quality module's status of each table read). The share's
maker is notified (``share.expiring``, ``share.unused``) once per expiry and once per unused spell.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from typing import Optional

import threading

from sqlalchemy import BigInteger, Column, Integer, PrimaryKeyConstraint, String, Table, Text, delete, select, update
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

PREFIX = "share:"                       # the user a recipient is to Cairn Catalog: share:<id>
AUDIENCE = "lakehouse"
NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
TABLE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_$-]*(\.[A-Za-z0-9_][A-Za-z0-9_$-]*)+$")

lakehouse_shares = Table(
    "cairn_lakehouse_shares", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("recipient", String(256), nullable=False),
    Column("tables", Text, nullable=False),               # JSON list of "namespace.table"
    Column("secret_hash", String(64), nullable=False),
    Column("created_by", String(256), nullable=False),
    Column("created_s", BigInteger, nullable=False),
    Column("expires_s", BigInteger, nullable=False),
    Column("revoked_s", BigInteger, nullable=True),
    Column("revoked_by", String(256), nullable=True),
    Column("last_used_s", BigInteger, nullable=True),
    Column("region", String(32), nullable=True),           # where the recipient keeps what it reads (residency)
)


share_usage = Table(
    "cairn_share_usage", metadata,
    Column("share_id", String(32), nullable=False),
    Column("day", Integer, nullable=False),                # yyyymmdd (UTC)
    Column("table_name", String(512), nullable=False),     # "" for the share as a whole
    Column("requests", BigInteger, nullable=False),
    Column("loads", BigInteger, nullable=False),
    Column("refusals", BigInteger, nullable=False),
    Column("failures", BigInteger, nullable=False),
    Column("tokens", BigInteger, nullable=False),
    Column("last_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("share_id", "day", "table_name"),
)
share_notices = Table(
    "cairn_share_notices", metadata,
    Column("share_id", String(32), nullable=False),
    Column("kind", String(16), nullable=False),            # expiring, unused
    Column("sent_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("share_id", "kind"),
)
COUNTS = ("requests", "loads", "refusals", "failures", "tokens")


def _day(ms: int) -> int:
    return int(time.strftime("%Y%m%d", time.gmtime(ms / 1000)))


class ShareError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _split(table: str) -> tuple[list[str], str]:
    parts = table.split(".")
    return parts[:-1], parts[-1]


class Shares:
    def __init__(self, engine, tokens, lake, audit=None, token_ttl: Optional[int] = None,
                 max_days: Optional[int] = None, catalog_url: str = "", token_url: str = ""):
        self.engine, self.tokens, self.lake, self.audit = engine, tokens, lake, audit
        self.token_ttl = token_ttl or int(os.environ.get("CAIRN_SHARES_TOKEN_TTL", "900"))
        if not 60 <= self.token_ttl <= 3600:
            raise ValueError("CAIRN_SHARES_TOKEN_TTL must be between 60 and 3600 seconds")
        self.max_days = max_days or int(os.environ.get("CAIRN_SHARES_MAX_DAYS", "365"))
        # What the recipient connects to: the catalog and the token endpoint as they reach them.
        self.catalog_url = catalog_url or os.environ.get("CAIRN_SHARES_CATALOG_URL", "")
        self.token_url = token_url or os.environ.get("CAIRN_SHARES_TOKEN_URL", "")
        self.residency = None                  # cairn.governance.residency.Residency, when rules are in force
        self.delta = None                      # cairn.dataaccess.delta_sharing.DeltaSharing: the open protocol too
        self.step_up = None                    # (principal, request, action): a passkey check (cairn.identity.passkeys)
        # quality_status(urns) -> {urn: HEALTHY | WARNING | FAILING | ERROR | None} (the quality module's)
        self.quality_status = None
        self.notify = None                     # cairn.notify.hub.NotificationHub: the share's maker is told
        self.retention_days = int(os.environ.get("CAIRN_SHARES_USAGE_RETENTION_DAYS", "90"))
        self._usage: dict[tuple[str, int, str], list[int]] = {}
        self._usage_lock = threading.Lock()
        self._flushed = time.time()
        self._pruned = 0.0
        self._swept = 0.0

    # ---- the share's own records -----------------------------------------------------------------
    def _row(self, conn, sid: str) -> Optional[dict]:
        row = conn.execute(select(lakehouse_shares).where(lakehouse_shares.c.id == sid)).first()
        return dict(row._mapping) if row else None

    @staticmethod
    def _public(row: dict) -> dict:
        now = int(time.time())
        state = "revoked" if row["revoked_s"] else ("expired" if now >= row["expires_s"] else "active")
        return {"id": row["id"], "name": row["name"], "recipient": row["recipient"], "tables": json.loads(row["tables"]),
                "created_by": row["created_by"], "created_at": row["created_s"], "expires_at": row["expires_s"],
                "revoked_at": row["revoked_s"], "revoked_by": row["revoked_by"], "last_used_at": row["last_used_s"],
                "region": row.get("region"), "state": state}

    def _may_leave(self, table: str, region: Optional[str]) -> bool:
        return self.residency is None or self.residency.transfer_allowed(".".join(_split(table)[0]), region)

    def _restricted(self, sid: str, urn: str) -> bool:
        return self.lake._restricted(f"urn:li:corpuser:{PREFIX}{sid}", [], urn)

    def create(self, actor: str, groups: list[str], name: str, recipient: str, tables: list[str],
               expires_days: int, region: Optional[str] = None) -> dict:
        if not NAME.match(name or ""):
            raise ShareError("name: lowercase letters, digits, '.', '_' and '-'")
        recipient = (recipient or "").strip()
        if not recipient or len(recipient) > 256:
            raise ShareError("recipient: who the share is for (an organisation or a contact), at most 256 characters")
        tables = sorted({t.strip() for t in tables or [] if t and t.strip()})
        if not tables or len(tables) > 500:
            raise ShareError("tables: between 1 and 500 tables, each namespace.table")
        bad = [t for t in tables if not TABLE.match(t)]
        if bad:
            raise ShareError(f"tables are named namespace.table: {', '.join(bad)}")
        if not 1 <= int(expires_days or 0) <= self.max_days:
            raise ShareError(f"expires_days: between 1 and {self.max_days}")
        from cairn.governance.residency import ResidencyError, valid_region

        try:
            region = valid_region(region)
        except ResidencyError as e:
            raise ShareError(str(e))
        pinned = [t for t in tables if not self._may_leave(t, region)]
        if pinned:
            where = f"a recipient in {region}" if region else "a recipient with no region"
            raise ShareError(f"{', '.join(pinned)} may not be shared with {where}: data residency", 409)
        sid = uuid.uuid4().hex
        from cairn.dataaccess.trino import MANAGE

        for t in tables:
            urn = self.lake.urn(*_split(t))
            if not self.lake.access.allowed(actor, groups, MANAGE, urn):
                raise ShareError(f"sharing {t} needs MANAGE on it", 403)
            if self._restricted(sid, urn):
                raise ShareError(f"{t} has a column mask or row filter: its files hold what they hide, so it is not shared",
                                 409)
        secret = secrets.token_urlsafe(32)
        now = int(time.time())
        row = {"id": sid, "name": name, "recipient": recipient, "tables": json.dumps(tables),
               "secret_hash": _hash(secret), "created_by": actor, "created_s": now,
               "expires_s": now + int(expires_days) * 86400, "revoked_s": None, "revoked_by": None, "last_used_s": None,
               "region": region}
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(lakehouse_shares.insert().values(**row))
                if self.audit is not None:
                    self.audit.record("shares", "share.created", actor, None,
                                      {"share": sid, "name": name, "recipient": recipient, "tables": tables,
                                       "region": region, "expires_at": row["expires_s"]}, conn=conn)
        except IntegrityError:
            raise ShareError(f"a share named {name} exists", 409)
        out = self._public(row)
        out["credential"] = f"{sid}:{secret}"
        out["connect"] = {"type": "rest", "uri": self.catalog_url, "oauth2-server-uri": self.token_url,
                          "credential": out["credential"], "scope": "catalog"}
        return out

    def list(self, actor: str, everyone: bool) -> list[dict]:
        q = select(lakehouse_shares).order_by(lakehouse_shares.c.created_s.desc())
        if not everyone:
            q = q.where(lakehouse_shares.c.created_by == actor)
        with self.engine.connect() as conn:
            return [self._public(dict(r._mapping)) for r in conn.execute(q)]

    def get(self, sid: str, actor: str, everyone: bool) -> dict:
        with self.engine.connect() as conn:
            row = self._row(conn, sid)
        if row is None or not (everyone or row["created_by"] == actor):
            raise ShareError("no such share", 404)
        return self._public(row)

    def revoke(self, sid: str, actor: str, everyone: bool) -> dict:
        now = int(time.time())
        with self.engine.begin() as conn:
            row = self._row(conn, sid)
            if row is None or not (everyone or row["created_by"] == actor):
                raise ShareError("no such share", 404)
            if row["revoked_s"] is None:
                conn.execute(lakehouse_shares.update().where(lakehouse_shares.c.id == sid)
                             .values(revoked_s=now, revoked_by=actor))
                row.update(revoked_s=now, revoked_by=actor)
                if self.audit is not None:
                    self.audit.record("shares", "share.revoked", actor, None, {"share": sid, "name": row["name"]}, conn=conn)
        return self._public(row)

    # ---- the recipient -------------------------------------------------------------------------
    def _active(self, row: Optional[dict]) -> bool:
        return row is not None and row["revoked_s"] is None and int(time.time()) < row["expires_s"]

    def token(self, client_id: str, client_secret: str) -> dict:
        """A token for the recipient, for at most the rest of the share's life."""
        with self.engine.begin() as conn:
            row = self._row(conn, client_id) if client_id else None
            if row is None or not hmac.compare_digest(row["secret_hash"], _hash(client_secret or "")):
                if row is not None:
                    self._count(row["id"], "", failures=1)
                raise ShareError("unknown share credential", 401)
            if row["revoked_s"] is not None:
                self._count(row["id"], "", refusals=1)
                raise ShareError("the share was revoked", 400)
            now = int(time.time())
            if now >= row["expires_s"]:
                self._count(row["id"], "", refusals=1)
                raise ShareError("the share has expired", 400)
            self._count(row["id"], "", tokens=1)
            ttl = max(60, min(self.token_ttl, row["expires_s"] - now))
            out = self.tokens.mint(f"urn:li:corpuser:{PREFIX}{row['id']}", ttl, audience=AUDIENCE,
                                   extra={"share": row["id"]})
            conn.execute(lakehouse_shares.update().where(lakehouse_shares.c.id == row["id"]).values(last_used_s=now))
            if self.audit is not None:
                self.audit.record("shares", "share.token_issued", f"urn:li:corpuser:{PREFIX}{row['id']}", None,
                                  {"share": row["id"], "name": row["name"], "token_id": out["token_id"],
                                   "expires_at": out["expires_at"]}, conn=conn)
        return out

    def decide(self, user: str, tables: list[dict]) -> dict:
        """Cairn Catalog's question for a recipient: the namespaces it may see (none: refused outright)
        and, per table, read-only access to the shared ones. Any error decides nothing."""
        from cairn.dataaccess.lakehouse import NOTHING

        nothing = {"schemas": False, "namespaces": [], "tables": [dict(NOTHING) for _ in tables]}
        sid = user.removeprefix(PREFIX)
        row = None
        try:
            with self.engine.connect() as conn:
                row = self._row(conn, sid)
            if not self._active(row):
                if row is not None:
                    self._count(sid, "", requests=1, refusals=1)
                return nothing
            shared = set(json.loads(row["tables"]))
            out = []
            self._count(sid, "", requests=1)
            for t in tables:
                ns, name = [str(x) for x in t.get("namespace") or []], str(t.get("name") or "")
                full = ".".join(ns + [name])
                if full in shared and self._may_leave(full, row.get("region")) \
                        and not self._restricted(sid, self.lake.urn(ns, name)):
                    out.append({"view": True, "read": True, "write": False, "manage": False, "restricted": False})
                    if len(tables) == 1:              # one table asked about: it is being loaded
                        self._count(sid, full, loads=1)
                else:
                    out.append(dict(NOTHING))
                    if len(tables) == 1:              # a table asked for and refused (listings only filter)
                        self._count(sid, full[:512], refusals=1)
            namespaces = sorted({".".join(_split(t)[0]) for t in shared})
            return {"schemas": False, "namespaces": namespaces, "tables": out}
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("share decision failed for %s: %s", user, e)
            if row is not None:
                self._count(sid, "", failures=1)
            return nothing

    # ---- usage ---------------------------------------------------------------------------------------
    def _count(self, sid: str, table: str, **counts: int) -> None:
        """Adds to the share's counts in memory; written every few seconds (never costs the decision)."""
        try:
            now = int(time.time() * 1000)
            with self._usage_lock:
                c = self._usage.setdefault((sid, _day(now), table), [0, 0, 0, 0, 0, 0])
                for i, k in enumerate(COUNTS):
                    c[i] += int(counts.get(k, 0))
                c[5] = max(c[5], now)
            if time.time() - self._flushed > 5:
                self.flush_usage()
        except Exception as e:  # noqa: BLE001 - usage is never worth a refusal
            logger.warning("share usage not recorded: %s", e)

    def flush_usage(self) -> int:
        with self._usage_lock:
            pending, self._usage = self._usage, {}
            self._flushed = time.time()
        if not pending:
            return 0
        from sqlalchemy.exc import IntegrityError

        u = share_usage
        try:
            for (sid, day, table), c in pending.items():
                key = (u.c.share_id == sid) & (u.c.day == day) & (u.c.table_name == table)
                inc = {k: getattr(u.c, k) + c[i] for i, k in enumerate(COUNTS)}
                for _ in range(2):
                    try:
                        with self.engine.begin() as conn:
                            last = conn.execute(select(u.c.last_ms).where(key)).scalar()
                            if last is None:
                                conn.execute(u.insert().values(share_id=sid, day=day, table_name=table, last_ms=c[5],
                                                               **{k: c[i] for i, k in enumerate(COUNTS)}))
                            else:
                                conn.execute(update(u).where(key).values(**inc, last_ms=max(int(last), c[5])))
                        break
                    except IntegrityError:            # another replica wrote the row first: add to it
                        continue
        except Exception as e:  # noqa: BLE001 - kept for the next flush
            logger.warning("share usage could not be written now: %s", e)
            with self._usage_lock:
                for k, c in pending.items():
                    mine = self._usage.setdefault(k, [0, 0, 0, 0, 0, 0])
                    for i in range(5):
                        mine[i] += c[i]
                    mine[5] = max(mine[5], c[5])
            return 0
        if time.time() - self._pruned > 3600:
            self._pruned = time.time()
            cutoff = _day(int((time.time() - self.retention_days * 86400) * 1000))
            with self.engine.begin() as conn:
                conn.execute(delete(u).where(u.c.day < cutoff))
        return len(pending)

    def _find(self, ref: str, actor: str, everyone: bool) -> dict:
        with self.engine.connect() as conn:
            row = self._row(conn, ref)
            if row is None:
                r = conn.execute(select(lakehouse_shares).where(lakehouse_shares.c.name == ref)).first()
                row = dict(r._mapping) if r else None
        if row is None or not (everyone or row["created_by"] == actor):
            raise ShareError("no such share", 404)
        return row

    def _rows(self, ids: list[str], since_day: int) -> list[dict]:
        if not ids:
            return []
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(share_usage).where(
                share_usage.c.share_id.in_(ids) & (share_usage.c.day >= since_day)))]

    @staticmethod
    def _summary(row: dict, rows: list[dict]) -> dict:
        totals = {k: sum(int(r[k]) for r in rows) for k in COUNTS}
        tables: dict[str, dict] = {}
        for r in rows:
            if r["table_name"]:
                t = tables.setdefault(r["table_name"], {"table": r["table_name"], "loads": 0, "refusals": 0,
                                                        "last_access_ms": 0})
                t["loads"] += int(r["loads"])
                t["refusals"] += int(r["refusals"])
                t["last_access_ms"] = max(t["last_access_ms"], int(r["last_ms"]))
        last = max([int(r["last_ms"]) for r in rows if r["requests"] or r["tokens"] or r["loads"]] or [0]) or None
        if row.get("last_used_s") and (last is None or row["last_used_s"] * 1000 > last):
            last = row["last_used_s"] * 1000
        shared = set(json.loads(row["tables"]))
        read = sorted(t for t, v in tables.items() if v["loads"] and t in shared)
        return {"totals": {**totals, "table_loads": totals["loads"], "tables_read": len(read)},
                "tables_read": read, "tables": sorted(tables.values(), key=lambda x: x["table"]),
                "last_access_ms": last}

    def usage(self, ref: str, actor: str, everyone: bool, days: int = 30) -> dict:
        """One share's usage over the last ``days`` days: totals, per table and per day."""
        if not 1 <= int(days) <= max(1, self.retention_days):
            raise ShareError(f"days: between 1 and {self.retention_days}")
        row = self._find(ref, actor, everyone)
        self.flush_usage()
        since = _day(int((time.time() - (int(days) - 1) * 86400) * 1000))
        rows = self._rows([row["id"]], since)
        per_day: dict[int, dict] = {}
        for r in rows:
            d = per_day.setdefault(r["day"], {"day": r["day"], **{k: 0 for k in COUNTS}})
            for k in COUNTS:
                d[k] += int(r[k])
        return {**self._public(row), "days": int(days), **self._summary(row, rows),
                "by_day": [per_day[k] for k in sorted(per_day)]}

    def overview(self, actor: str, everyone: bool, unused_days: int = 30, expiring_days: int = 14) -> dict:
        """Every share the caller sees, with what needs attention: unused, expiring soon, and recipients
        reading tables whose quality is failing."""
        self.flush_usage()
        now = int(time.time())
        shares = self.list(actor, everyone)
        with self.engine.connect() as conn:
            raw = {r.id: dict(r._mapping) for r in conn.execute(select(lakehouse_shares).where(
                lakehouse_shares.c.id.in_([x["id"] for x in shares])))} if shares else {}
        since = _day(int((time.time() - max(unused_days, 1) * 86400) * 1000))
        rows = self._rows(list(raw), since)
        out, read_urns = [], {}
        for sh in shares:
            row = raw[sh["id"]]
            summary = self._summary(row, [r for r in rows if r["share_id"] == sh["id"]])
            last = summary["last_access_ms"]
            active = sh["state"] == "active"
            item = {"id": sh["id"], "name": sh["name"], "recipient": sh["recipient"], "state": sh["state"],
                    "created_by": sh["created_by"], "expires_at": sh["expires_at"], "last_access_ms": last,
                    "requests": summary["totals"]["requests"], "table_loads": summary["totals"]["loads"],
                    "refusals": summary["totals"]["refusals"], "failures": summary["totals"]["failures"],
                    "tables_read": summary["tables_read"],
                    "unused": active and (last or row["created_s"] * 1000) <= (now - unused_days * 86400) * 1000,
                    "expiring_soon": active and sh["expires_at"] - now <= expiring_days * 86400,
                    "quality_failing": []}
            for t in summary["tables_read"]:
                read_urns[self.lake.urn(*_split(t))] = t
            out.append(item)
        status: dict[str, Optional[str]] = {}
        if self.quality_status is not None and read_urns:
            try:
                status = self.quality_status(list(read_urns))
            except Exception as e:  # noqa: BLE001 - the overview still answers, without quality
                logger.warning("quality status for shared tables failed: %s", e)
        for item in out:
            item["quality_failing"] = [{"table": t, "urn": u, "status": status[u]} for u, t in read_urns.items()
                                       if t in item["tables_read"] and status.get(u) in ("FAILING", "ERROR")]
        return {"shares": out, "unused": [x["name"] for x in out if x["unused"]],
                "expiring_soon": [x["name"] for x in out if x["expiring_soon"]],
                "quality_failing": [{"share": x["name"], "recipient": x["recipient"], "tables": x["quality_failing"]}
                                    for x in out if x["quality_failing"]],
                "unused_days": unused_days, "expiring_days": expiring_days}

    def sweep(self, unused_days: Optional[int] = None, expiring_days: Optional[int] = None, every_s: float = 0) -> list[dict]:
        """Tells each share's maker once when it is about to expire, and once per spell without use."""
        if every_s and time.time() - self._swept < every_s:
            return []
        self._swept = time.time()
        unused_days = unused_days if unused_days is not None else int(os.environ.get("CAIRN_SHARES_UNUSED_DAYS", "30"))
        expiring_days = expiring_days if expiring_days is not None else int(os.environ.get("CAIRN_SHARES_EXPIRING_DAYS", "14"))
        if self.notify is None:
            return []
        from sqlalchemy.exc import IntegrityError

        from cairn.notify.hub import Event

        sent = []
        view = self.overview("", True, unused_days, expiring_days)
        with self.engine.connect() as conn:
            notices = {(r.share_id, r.kind): r.sent_ms for r in conn.execute(select(share_notices))}
        now = int(time.time() * 1000)
        for item in view["shares"]:
            for kind, due in (("expiring", item["expiring_soon"]), ("unused", item["unused"])):
                if not due:
                    continue
                before = notices.get((item["id"], kind))
                if before is not None and (kind == "expiring" or (item["last_access_ms"] or 0) <= before):
                    continue                           # told already (an unused spell ends with the next use)
                try:
                    with self.engine.begin() as conn:
                        if before is None:
                            conn.execute(share_notices.insert().values(share_id=item["id"], kind=kind, sent_ms=now))
                        elif conn.execute(update(share_notices).where(
                                (share_notices.c.share_id == item["id"]) & (share_notices.c.kind == kind)
                                & (share_notices.c.sent_ms == before)).values(sent_ms=now)).rowcount != 1:
                            continue
                except IntegrityError:                 # another replica told them
                    continue
                if kind == "expiring":
                    days = max(0, (item["expires_at"] - now // 1000) // 86400)
                    title = f"Share {item['name']} ends in {days} day(s)"
                    body = f"The share with {item['recipient']} expires; make a new one if it should go on."
                else:
                    title = f"Share {item['name']} is not used"
                    body = f"{item['recipient']} has not read through it for {unused_days} days; revoke it if it is no longer needed."
                try:
                    self.notify.publish(Event(type=f"share.{kind}", title=title, body=body, recipients=[item["created_by"]],
                                              payload={"share": item["id"], "name": item["name"],
                                                       "recipient": item["recipient"], "expires_at": item["expires_at"]}))
                except Exception as e:  # noqa: BLE001
                    logger.warning("share notice %s for %s failed: %s", kind, item["name"], e)
                sent.append({"share": item["name"], "kind": kind})
        return sent


def build_shares_router(shares: Shares, current_principal, authorize, groups_of):
    import base64
    from urllib.parse import unquote

    from fastapi import APIRouter, Body, Depends, Form, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/v1/shares", tags=["shares"])

    def operator(p) -> bool:
        return bool(authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "shares are made by people")
        return p

    def call(fn, *a):
        try:
            return fn(*a)
        except ShareError as e:
            raise HTTPException(e.status, str(e))

    @router.post("")
    def create(request: Request, name: str = Body(...), recipient: str = Body(...), tables: list[str] = Body(...),
               expires_days: int = Body(30), region: Optional[str] = Body(None), p=Depends(current_principal)):
        """Shares tables with a recipient outside the organisation. The credential is shown once. ``region``:
        where the recipient keeps what it reads (needed for tables pinned to a region)."""
        person(p)
        if shares.step_up is not None:
            from cairn.identity.passkeys import PasskeyError

            try:
                shares.step_up(p, request, "share.create")
            except PasskeyError as e:
                raise HTTPException(e.status, str(e), headers=e.headers)
        out = call(shares.create, p.urn, list(groups_of(p.urn)), name, recipient, tables, expires_days, region)
        if shares.delta is not None:          # the Delta Sharing profile file: the same credential, as a bearer token
            out["delta_sharing"] = shares.delta.profile(out, str(request.base_url))
        return out

    @router.get("")
    def list_(p=Depends(current_principal)):
        return {"shares": shares.list(p.urn, operator(p))}

    @router.get("/usage/overview")
    def usage_overview(unused_days: int = 30, expiring_days: int = 14, p=Depends(current_principal)):
        """Every share the caller sees: unused, expiring soon, recipients reading tables whose quality fails."""
        if not (0 <= unused_days <= 365 and 0 <= expiring_days <= 365):
            raise HTTPException(422, "unused_days and expiring_days: 0 to 365")
        return call(shares.overview, p.urn, operator(p), unused_days, expiring_days)

    @router.get("/{ref}/usage")
    def usage(ref: str, days: int = 30, p=Depends(current_principal)):
        """A share's usage (by name or id): requests, table loads, refusals, failures, per table and day."""
        return call(shares.usage, ref, p.urn, operator(p), days)

    @router.get("/{sid}")
    def get(sid: str, p=Depends(current_principal)):
        return call(shares.get, sid, p.urn, operator(p))

    @router.post("/{sid}/revoke")
    def revoke(sid: str, p=Depends(current_principal)):
        """Ends the share at once: the recipient's next request and next renewal are refused."""
        return call(shares.revoke, sid, p.urn, operator(p))

    @router.post("/oauth/tokens")
    def oauth_tokens(request: Request, grant_type: str = Form(""), client_id: str = Form(""),
                     client_secret: str = Form(""), scope: str = Form("")):
        """The recipient's Iceberg REST client gets its token here (OAuth 2.0 client credentials)."""
        def oauth_error(code: str, text: str, status: int = 400):
            headers = {"Cache-Control": "no-store"}
            if code == "invalid_client":
                status, headers["WWW-Authenticate"] = 401, 'Basic realm="shares"'
            return JSONResponse({"error": code, "error_description": text}, status_code=status, headers=headers)

        if grant_type != "client_credentials":
            return oauth_error("unsupported_grant_type", "share tokens come from client credentials")
        basic = request.headers.get("authorization", "")
        if not client_id and basic[:6].lower() == "basic ":
            try:
                client_id, _, client_secret = base64.b64decode(basic[6:]).decode().partition(":")
            except ValueError:
                return oauth_error("invalid_client", "malformed client authentication")
        try:
            out = shares.token(unquote(client_id), unquote(client_secret))
        except ShareError as e:
            return oauth_error("invalid_client" if e.status == 401 else "invalid_grant", str(e))
        return JSONResponse({"access_token": out["token"], "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                             "token_type": "Bearer", "expires_in": out["expires_in"]},
                            headers={"Cache-Control": "no-store"})

    return router
