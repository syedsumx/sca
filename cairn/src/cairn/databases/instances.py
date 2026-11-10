"""Managed PostgreSQL: instances (clusters) per team or workload, run by Cairn, with backups, point-in-time restore,
lifecycle and health. The databases of ``cairn.dataaccess.databases`` live on them.

An instance is run by one runtime: CloudNativePG on Kubernetes (``cairn.databases.cnpg``: primary and standbys
with automatic failover, PgBouncer) or Docker for small deployments (``cairn.databases.docker_runtime``). The
server Cairn was already given for databases (``CAIRN_DATABASES_ADMIN_URL``) is the instance ``default``: Cairn
reads its health and metrics, and its lifecycle stays with whoever runs it.

* ``POST /v1/databases/instances`` (``CREATE_DATABASE_INSTANCE``) makes one: ``version``, ``cpu``, ``memory_mb``,
  ``storage_gb``, ``replicas``, ``pooler``, ``maintenance_window``. Its parameters
  (``PUT .../parameters/<name>``, allow-listed) and backup schedules (``PUT .../backup-schedules/<name>``) are its
  own resources. Everything else on ``urn:li:pgInstance:<name>`` needs ``MANAGE_DATABASE_INSTANCE``; status and
  metrics need ``VIEW_DATABASE_INSTANCE`` (or MANAGE); slow queries (statement text) need MANAGE.
* Changes that restart PostgreSQL (a new image, a parameter that needs a restart) wait for the maintenance window
  when there is one (``pending``), unless ``POST .../apply-pending``.
* Backups go to the store under ``<bucket>/<instance>/<uid>/``: continuous WAL archiving and base backups, kept for
  their schedule's ``retention_days`` (the newest is always kept). ``POST .../backups/<id>/verify`` restores one
  into a throwaway instance and checks it opens.
* ``POST .../restore`` makes a new instance from the backups: as of ``target_time`` (point in time) or a backup.
  Its Cairn databases are the source's, renamed (``databases``: {old: new}) or dropped; logins never survive.

The superuser's password is derived from Cairn's key (``CAIRN_PG_INSTANCE_KEY``) and the instance; it is never
stored, shown or put on a command line. People reach instances only through database logins.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, insert, select, update

from cairn.common.db import metadata
from cairn.databases.docker_runtime import RuntimeError_
from cairn.databases.spec import (PARAMETERS, SpecError, check_name, check_parameter, check_schedule, check_spec,
                                  in_window, parse_time, pg_time)

logger = logging.getLogger(__name__)

instances_table = Table(
    "cairn_pg_instances", metadata,
    Column("name", String(31), primary_key=True),
    Column("uid", String(16), nullable=False),
    Column("runtime", String(16), nullable=False),
    Column("spec", Text, nullable=False),
    Column("image", String(255), nullable=True),
    Column("state", String(16), nullable=False),
    Column("host", String(255), nullable=True),
    Column("port", Integer, nullable=True),
    Column("restored_from", Text, nullable=True),
    Column("pending", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
backups_table = Table(
    "cairn_pg_backups", metadata,
    Column("id", String(48), primary_key=True),
    Column("instance", String(31), nullable=False, index=True),
    Column("uid", String(16), nullable=False),
    Column("schedule", String(31), nullable=False),
    Column("state", String(12), nullable=False),
    Column("started_ms", BigInteger, nullable=False),
    Column("finished_ms", BigInteger, nullable=True),
    Column("bytes", BigInteger, nullable=True),
    Column("start_lsn", String(32), nullable=True),
    Column("end_lsn", String(32), nullable=True),
    Column("timeline", Integer, nullable=True),
    Column("location", String(512), nullable=True),
    Column("verified", String(12), nullable=True),
    Column("verified_ms", BigInteger, nullable=True),
    Column("error", Text, nullable=True),
    Column("expired", Boolean, nullable=False),
)

DEFAULT = "default"
STUCK_MS = 6 * 3600 * 1000            # a backup "running" this long was cut off (a restart): failed
SEGMENT = 16 * 1024 * 1024


class InstanceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def urn(name: str) -> str:
    return f"urn:li:pgInstance:{name}"


def lsn_to_int(lsn: str) -> int:
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def wal_name(timeline: int, lsn: str) -> str:
    seg = lsn_to_int(lsn) // SEGMENT
    per = 0x100000000 // SEGMENT
    return f"{timeline:08X}{seg // per:08X}{seg % per:08X}"


class BackupStore:
    """Cairn's own view of the backups bucket: list, read and delete (retention), with its key."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "us-east-1"):
        self.endpoint, self.bucket, self.ak, self.sk, self.region = endpoint.rstrip("/"), bucket, access_key, secret_key, region

    def _req(self, method: str, key: str = "", query: Optional[dict] = None, ok=(200, 204)):
        import httpx
        from urllib.parse import quote, urlencode

        from cairn.storage.store_admin import sigv4_headers

        url = f"{self.endpoint}/{self.bucket}" + (f"/{quote(key, safe='/-_.~')}" if key else "")
        if query:
            url += "?" + urlencode(sorted(query.items()), quote_via=quote)
        try:
            r = httpx.request(method, url, headers=sigv4_headers(method, url, b"", self.ak, self.sk, self.region),
                              timeout=60)
        except httpx.HTTPError as e:
            raise InstanceError(f"the backup store is not reachable: {type(e).__name__}", 502) from e
        if r.status_code not in ok:
            raise InstanceError(f"backup store: {method} {key}: {r.status_code}", 502)
        return r

    def ensure_bucket(self) -> None:
        r = self._req("HEAD", ok=(200, 404))
        if r.status_code == 404:
            self._req("PUT", ok=(200, 409))

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token = ""
        while True:
            q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                q["continuation-token"] = token
            text = self._req("GET", query=q).text
            keys += re.findall(r"<Key>([^<]+)</Key>", text)
            m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", text)
            if not m:
                return keys
            token = m.group(1)

    def get(self, key: str) -> Optional[bytes]:
        r = self._req("GET", key, ok=(200, 404))
        return None if r.status_code == 404 else r.content

    def delete(self, key: str) -> None:
        self._req("DELETE", key, ok=(200, 204, 404))


class Instances:
    """Managed instances. ``allowed(actor, privilege, urn_or_None)`` is the PDP; ``runtime`` is a DockerRuntime or
    CnpgRuntime (None: only the default instance, read-only)."""

    def __init__(self, engine, allowed: Callable[[str, str, Optional[str]], bool], audit=None, key: bytes = b"",
                 runtime=None, store: Optional[BackupStore] = None, bucket: str = "",
                 default_admin_url: str = "", default_host: str = "", default_port: int = 5432,
                 sslmode: str = "require", clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        if runtime is not None and len(key) < 16:
            raise InstanceError("managed instances need CAIRN_PG_INSTANCE_KEY (16 bytes or more)", 503)
        self.engine, self.allowed, self.audit, self.key, self.runtime = engine, allowed, audit, key, runtime
        self.store, self.bucket = store, bucket
        self.default_admin_url, self.default_host, self.default_port = default_admin_url, default_host, default_port
        self.sslmode, self.clock = sslmode, clock
        self.databases = None            # cairn.dataaccess.databases.Databases, set when both are built
        self._threads: dict[str, threading.Thread] = {}

    # ---- helpers ---------------------------------------------------------------------------------------------
    def _need(self, actor: str, privilege: str, target: Optional[str]) -> None:
        try:
            ok = self.allowed(actor, privilege, target)
        except Exception:  # noqa: BLE001 - fail closed
            logger.warning("instance decision failed (%s %s %s)", actor, privilege, target, exc_info=True)
            ok = False
        if not ok:
            raise InstanceError(f"{privilege} is needed" + (f" on {target}" if target else ""), 403)

    def _may(self, actor: str, privileges: tuple[str, ...], target: str) -> bool:
        for p in privileges:
            try:
                if self.allowed(actor, p, target):
                    return True
            except Exception:  # noqa: BLE001 - fail closed
                logger.warning("instance decision failed (%s %s %s)", actor, p, target, exc_info=True)
        return False

    def _see(self, actor: str, name: str) -> None:
        if not self._may(actor, ("VIEW_DATABASE_INSTANCE", "MANAGE_DATABASE_INSTANCE", "USE_DATABASE_INSTANCE"), urn(name)):
            raise InstanceError("no such instance", 404)

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("data", action, actor, urn(name), details)

    def password(self, name: str, uid: str) -> str:
        mac = hmac.new(self.key, f"pg-superuser:{name}:{uid}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def _row(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(instances_table).where(instances_table.c.name == name)).first()
        if row is None:
            raise InstanceError("no such instance", 404)
        return row

    def _inst(self, row) -> dict:
        return {"name": row.name, "uid": row.uid, "spec": json.loads(row.spec), "image": row.image, "port": row.port,
                "host": row.host, "backup_prefix": self._prefix(row.name, row.uid)}

    def _prefix(self, name: str, uid: str) -> str:
        return f"{name}/{uid}"

    def _set(self, name: str, **values) -> None:
        values["updated_ms"] = self.clock()
        with self.engine.begin() as c:
            c.execute(update(instances_table).where(instances_table.c.name == name).values(**values))

    def _claim(self, name: str, to: str, frm: tuple[str, ...] = ("ready",)) -> None:
        """Moves the instance to state ``to`` if it is in one of ``frm`` (one change at a time, across replicas)."""
        with self.engine.begin() as c:
            n = c.execute(update(instances_table).where(instances_table.c.name == name,
                                                        instances_table.c.state.in_(frm))
                          .values(state=to, updated_ms=self.clock())).rowcount
        if n != 1:
            raise InstanceError(f"{name} is busy ({self._row(name).state}): try again when it is ready", 409)

    def _runtime(self):
        if self.runtime is None:
            raise InstanceError("managed instances are not enabled here (CAIRN_PG_RUNTIME)", 503)
        return self.runtime

    def _managed(self, name: str):
        if name == DEFAULT:
            raise InstanceError("the default instance is run outside Cairn: its lifecycle and backups are its "
                                "operator's", 409)
        return self._row(name)

    def _call(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except RuntimeError_ as e:
            raise InstanceError(str(e), e.status) from e
        except SpecError as e:
            raise InstanceError(str(e), e.status) from e

    # ---- connections -----------------------------------------------------------------------------------------
    def admin_conninfo(self, name: str, dbname: Optional[str] = None) -> str:
        """The superuser's connection string for an instance (Cairn's own use; never returned by the API)."""
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        if name in (None, "", DEFAULT):
            if not self.default_admin_url:
                raise InstanceError("there is no default instance (CAIRN_DATABASES_ADMIN_URL)", 503)
            info = conninfo_to_dict(self.default_admin_url)
        else:
            row = self._row(name)
            if row.state in ("creating", "restoring", "failed", "deleting"):
                raise InstanceError(f"{name} is {row.state}", 409)
            host, port = self._call(self._runtime().endpoint, self._inst(row))
            info = {"host": host, "port": str(port), "user": "postgres", "password": self.password(row.name, row.uid),
                    "dbname": "postgres", "sslmode": "require"}
        if dbname:
            info["dbname"] = dbname
        info.setdefault("connect_timeout", "10")
        return make_conninfo(**info)

    def _connect(self, name: str, dbname: Optional[str] = None):
        import psycopg

        try:
            return psycopg.connect(self.admin_conninfo(name, dbname), autocommit=True)
        except psycopg.OperationalError as e:
            raise InstanceError(f"{name} does not answer: {str(e).splitlines()[0][:200]}", 503) from e

    def client_endpoint(self, name: Optional[str]) -> tuple[str, int]:
        """Where database logins connect (the pooler when there is one)."""
        if name in (None, "", DEFAULT):
            return self.default_host, self.default_port
        row = self._row(name)
        rt = self._runtime()
        inst = self._inst(row)
        fn = getattr(rt, "client_endpoint", None) or rt.endpoint
        try:
            return fn(inst)
        except RuntimeError_:
            return row.host or "", int(row.port or 5432)

    # ---- the record ------------------------------------------------------------------------------------------
    def _view(self, row, detail: bool = True) -> dict:
        spec = json.loads(row.spec)
        out = {"name": row.name, "urn": urn(row.name), "runtime": row.runtime, "state": row.state,
               "version": spec["version"], "cpu": spec["cpu"], "memory_mb": spec["memory_mb"],
               "storage_gb": spec["storage_gb"], "replicas": spec["replicas"], "pooler": spec.get("pooler"),
               "maintenance_window": spec.get("maintenance_window"), "image": spec.get("image"),
               "running_image": row.image, "created_by": row.created_by, "created_ms": row.created_ms,
               "restored_from": json.loads(row.restored_from) if row.restored_from else None,
               "pending": json.loads(row.pending) if row.pending else [], "error": row.error,
               "parameters": [{"name": k, "value": v} for k, v in sorted((spec.get("parameters") or {}).items())],
               "backup_schedules": sorted(spec.get("backup_schedules") or [], key=lambda s: s["name"])}
        if detail and row.state not in ("creating", "restoring", "failed"):
            try:
                host, port = self.client_endpoint(row.name)
                out["host"], out["port"] = host, port
            except InstanceError:
                out["host"], out["port"] = row.host, row.port
        else:
            out["host"], out["port"] = row.host, row.port
        return out

    def _default_view(self) -> dict:
        return {"name": DEFAULT, "urn": urn(DEFAULT), "runtime": "external", "state": "ready", "host": self.default_host,
                "port": self.default_port, "version": None, "pending": [], "parameters": [], "backup_schedules": [],
                "replicas": None, "pooler": None, "maintenance_window": None, "created_by": None,
                "note": "the server given to Cairn (CAIRN_DATABASES_ADMIN_URL): run outside Cairn"}

    def list(self, actor: str) -> list[dict]:
        out = []
        if self.default_admin_url and self._may(actor, ("VIEW_DATABASE_INSTANCE", "MANAGE_DATABASE_INSTANCE",
                                                        "USE_DATABASE_INSTANCE", "CREATE_DATABASE"), urn(DEFAULT)):
            out.append(self._default_view())
        with self.engine.connect() as c:
            rows = c.execute(select(instances_table).order_by(instances_table.c.name)).all()
        for r in rows:
            if self._may(actor, ("VIEW_DATABASE_INSTANCE", "MANAGE_DATABASE_INSTANCE", "USE_DATABASE_INSTANCE"), urn(r.name)):
                out.append(self._view(r, detail=False))
        return out

    def get(self, actor: str, name: str) -> dict:
        if name == DEFAULT:
            self._see(actor, name)
            if not self.default_admin_url:
                raise InstanceError("no such instance", 404)
            return self._default_view()
        row = self._row(name)
        self._see(actor, name)
        if row.state in ("creating", "restoring") and getattr(self.runtime, "name", "") == "kubernetes":
            self.reconcile(name)
            row = self._row(name)
        return self._view(row)

    # ---- create, change, delete ------------------------------------------------------------------------------
    def create(self, actor: str, body: dict) -> dict:
        rt = self._runtime()
        name = check_name(body.get("name"))
        self._need(actor, "CREATE_DATABASE_INSTANCE", None)
        spec = self._call(check_spec, body)
        self._fits(spec)
        uid = secrets.token_hex(6)
        now = self.clock()
        with self.engine.begin() as c:
            if c.execute(select(instances_table.c.name).where(instances_table.c.name == name)).first():
                raise InstanceError(f"{name} exists", 409)
            c.execute(insert(instances_table).values(
                name=name, uid=uid, runtime=rt.name, spec=json.dumps(spec), image=None, state="creating", host=None,
                port=None, restored_from=None, pending=None, error=None, created_by=actor, created_ms=now, updated_ms=now))
        inst = {"name": name, "uid": uid, "spec": spec, "image": spec.get("image"), "port": None,
                "backup_prefix": self._prefix(name, uid)}
        if self.store is not None:
            self.store.ensure_bucket()
        try:
            where = self._call(rt.create, inst, self.password(name, uid))
        except InstanceError as e:
            self._set(name, state="failed", error=str(e)[:2000])
            self._audit("pg_instance.failed", actor, name, {"error": str(e)[:300]})
            raise
        self._set(name, image=where.get("image"), host=where.get("host") or None, port=where.get("port"))
        self._audit("pg_instance.created", actor, name, {"runtime": rt.name, "version": spec["version"],
                                                         "replicas": spec["replicas"]})
        if rt.name == "docker":
            self._after_ready(name)
        return self.get(actor, name)

    def _fits(self, spec: dict) -> None:
        rt = self._runtime()
        if spec["replicas"] and not rt.supports_replicas:
            raise InstanceError("standbys (replicas) need the Kubernetes runtime: Docker instances are single", 400)
        if spec.get("pooler") and not rt.supports_pooler:
            raise InstanceError("connection pooling needs the Kubernetes runtime", 400)
        self._call(rt.image_for, spec["version"], spec.get("image") or "")

    def _after_ready(self, name: str) -> None:
        """First steps once an instance answers: ready; the statement statistics extension."""
        self._set(name, state="ready", error=None)
        try:
            with self._connect(name) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        except Exception as e:  # noqa: BLE001 - statistics are not worth failing an instance over
            logger.warning("instance %s: pg_stat_statements: %s", name, e)

    def reconcile(self, name: str) -> None:
        """Kubernetes: an instance being created or restored becomes ready once the operator says it is healthy."""
        row = self._row(name)
        if row.state not in ("creating", "restoring"):
            return
        inst = self._inst(row)
        try:
            st = self.runtime.state(inst)
        except RuntimeError_ as e:
            logger.warning("instance %s: %s", name, e)
            return
        if not (st.get("running") and st.get("status") == "Cluster in healthy state"):
            if "fail" in str(st.get("status") or "").lower():
                self._set(name, state="failed", error=str(st.get("status")))
            return
        try:
            if row.state == "restoring":
                self._after_restore(name)
            self._after_ready(name)
        except InstanceError as e:
            logger.warning("instance %s: not ready yet: %s", name, e)

    def update(self, actor: str, name: str, body: dict) -> dict:
        """Resize, replicas, pooler, maintenance window, image (a minor upgrade). What restarts PostgreSQL waits
        for the maintenance window when there is one."""
        row = self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        rt = self._runtime()
        old = json.loads(row.spec)
        if "version" in body and str(body["version"]) != old["version"]:
            raise InstanceError("a major version is not changed in place: restore or import into a new instance "
                                "(see Managed Postgres, upgrades)", 400)
        spec = self._call(check_spec, {k: v for k, v in body.items() if k != "name"}, base=old)
        self._fits(spec)
        self._claim(name, "updating")
        try:
            inst = {**self._inst(row), "spec": spec}
            pending = json.loads(row.pending) if row.pending else []
            if rt.name == "docker":
                if (spec["cpu"], spec["memory_mb"]) != (old["cpu"], old["memory_mb"]):
                    self._call(rt.resize, inst)
                if spec.get("image") != old.get("image"):
                    pending = [p for p in pending if p.get("op") != "image"] + [{"op": "image", "image": spec.get("image"),
                                                                                  "since_ms": self.clock()}]
            else:
                if spec.get("image") != old.get("image") and spec.get("maintenance_window"):
                    pending = [p for p in pending if p.get("op") != "image"] + [{"op": "image", "image": spec.get("image"),
                                                                                  "since_ms": self.clock()}]
                    spec_now = {**spec, "image": old.get("image")}
                else:
                    spec_now = spec
                self._call(rt.apply, {**inst, "spec": spec_now})
            self._set(name, spec=json.dumps(spec), pending=json.dumps(pending) if pending else None)
        finally:
            self._set(name, state="ready")
        self._audit("pg_instance.updated", actor, name, {k: body[k] for k in body if k != "name"})
        if pending and in_window(spec.get("maintenance_window"), self._now()):
            self.apply_pending(actor, name)
        return self.get(actor, name)

    def _now(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.clock() / 1000, dt.timezone.utc)

    def apply_pending(self, actor: str, name: str) -> dict:
        """Applies the changes waiting for the maintenance window, now (a rolling restart on Kubernetes; a stop and
        start on Docker)."""
        row = self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        pending = json.loads(row.pending) if row.pending else []
        if not pending:
            return self.get(actor, name)
        rt = self._runtime()
        self._claim(name, "updating")
        inst = self._inst(row)
        try:
            if rt.name == "docker":
                image = rt.image_for(inst["spec"]["version"], inst["spec"].get("image") or "")
                where = self._call(rt.recreate, inst, image)
                self._set(name, image=where["image"], port=where["port"])
            else:
                if any(p["op"] == "image" for p in pending):
                    self._call(rt.apply, inst)
                if any(p["op"] == "restart" for p in pending):
                    self._call(rt.restart, inst)
            self._set(name, pending=None, error=None)
        except InstanceError as e:
            self._set(name, error=str(e)[:2000])
            raise
        finally:
            self._set(name, state="ready")
        self._audit("pg_instance.maintenance", actor, name, {"applied": [p["op"] for p in pending]})
        return self.get(actor, name)

    def restart(self, actor: str, name: str) -> dict:
        self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        self._claim(name, "updating")
        try:
            self._call(self._runtime().restart, self._inst(self._row(name)))
            with self.engine.begin() as c:
                row = c.execute(select(instances_table.c.pending).where(instances_table.c.name == name)).first()
            left = [p for p in (json.loads(row.pending) if row.pending else []) if p["op"] != "restart"]
            self._set(name, pending=json.dumps(left) if left else None)
        finally:
            self._set(name, state="ready")
        self._audit("pg_instance.restarted", actor, name, {})
        return self.get(actor, name)

    def switchover(self, actor: str, name: str, target: str = "") -> dict:
        row = self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        rt = self._runtime()
        if not hasattr(rt, "switchover"):
            raise InstanceError("a switchover needs standbys: Docker instances are single", 409)
        to = self._call(rt.switchover, self._inst(row), target)
        self._audit("pg_instance.switchover", actor, name, {"to": to})
        return {"name": name, "switching_to": to}

    def delete(self, actor: str, name: str) -> None:
        row = self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        if self.databases is not None:
            on = self.databases.on_instance(name)
            if on:
                raise InstanceError(f"{name} still holds Cairn databases ({', '.join(on)}): delete them first", 409)
        self._claim(name, "deleting", ("ready", "failed", "creating", "restoring", "updating"))
        try:
            self._call(self._runtime().delete, self._inst(row), True)
        except InstanceError:
            self._set(name, state="failed")
            raise
        with self.engine.begin() as c:
            c.execute(instances_table.delete().where(instances_table.c.name == name))
        # Backups stay in the store (under the old uid) until their retention ends: a deleted instance can be restored.
        self._audit("pg_instance.deleted", actor, name, {})

    # ---- parameters and backup schedules (their own resources) ------------------------------------------------
    def set_parameter(self, actor: str, name: str, param: str, value: Any) -> dict:
        row = self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        v = self._call(check_parameter, param, value)
        spec = json.loads(row.spec)
        params = dict(spec.get("parameters") or {})
        params[param] = v
        restart = self._apply_parameters(row, {**spec, "parameters": params}, {param: v})
        self._audit("pg_instance.parameter_set", actor, name, {"parameter": param, "value": v, "restart": restart})
        return {"instance": name, "name": param, "value": v, "pending_restart": restart}

    def drop_parameter(self, actor: str, name: str, param: str) -> None:
        row = self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        spec = json.loads(row.spec)
        params = dict(spec.get("parameters") or {})
        if param not in params:
            raise InstanceError("no such parameter set", 404)
        params.pop(param)
        self._apply_parameters(row, {**spec, "parameters": params}, {param: None})
        self._audit("pg_instance.parameter_reset", actor, name, {"parameter": param})

    def _apply_parameters(self, row, spec: dict, changed: dict) -> bool:
        """Sets the parameters on the instance: reloaded now; one that needs a restart waits for the window."""
        from psycopg import sql

        rt = self._runtime()
        name = row.name
        self._claim(name, "updating")
        restart = False
        try:
            if rt.name == "docker":
                with self._connect(name) as conn:
                    for p, v in changed.items():
                        assert p in PARAMETERS
                        if v is None:
                            conn.execute(sql.SQL("ALTER SYSTEM RESET {}").format(sql.SQL(p)))
                        else:
                            conn.execute(sql.SQL("ALTER SYSTEM SET {} = {}").format(sql.SQL(p), sql.Literal(v)))
                    conn.execute("SELECT pg_reload_conf()")
                    time.sleep(0.5)
                    restart = bool(conn.execute("SELECT count(*) FROM pg_settings WHERE pending_restart").fetchone()[0])
            else:
                self._call(rt.apply, {**self._inst(row), "spec": spec})       # the operator reloads or rolls
            pending = json.loads(row.pending) if row.pending else []
            if restart and not any(p["op"] == "restart" for p in pending):
                pending.append({"op": "restart", "reason": "parameters", "since_ms": self.clock()})
            self._set(name, spec=json.dumps(spec), pending=json.dumps(pending) if pending else None)
        finally:
            self._set(name, state="ready")
        if restart and in_window(spec.get("maintenance_window"), self._now()):
            self._restart_now(name)
        return restart

    def _restart_now(self, name: str) -> None:
        self._claim(name, "updating")
        try:
            self._call(self._runtime().restart, self._inst(self._row(name)))
            row = self._row(name)
            left = [p for p in (json.loads(row.pending) if row.pending else []) if p["op"] != "restart"]
            self._set(name, pending=json.dumps(left) if left else None)
        finally:
            self._set(name, state="ready")

    def set_schedule(self, actor: str, name: str, sched: str, body: dict) -> dict:
        row = self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        if self.store is None:
            raise InstanceError("backups are not set up here (CAIRN_PG_BACKUP_STORE)", 503)
        s = self._call(check_schedule, sched, body)
        spec = json.loads(row.spec)
        scheds = [x for x in spec.get("backup_schedules") or [] if x["name"] != sched] + [s]
        spec["backup_schedules"] = sorted(scheds, key=lambda x: x["name"])
        if self.runtime.name == "kubernetes":
            self._call(self.runtime.apply, {**self._inst(row), "spec": spec})
        self._set(name, spec=json.dumps(spec))
        self._audit("pg_instance.schedule_set", actor, name, s)
        return {"instance": name, **s}

    def drop_schedule(self, actor: str, name: str, sched: str) -> None:
        row = self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        spec = json.loads(row.spec)
        left = [x for x in spec.get("backup_schedules") or [] if x["name"] != sched]
        if len(left) == len(spec.get("backup_schedules") or []):
            raise InstanceError("no such schedule", 404)
        spec["backup_schedules"] = left
        if self.runtime.name == "kubernetes":
            self._call(self.runtime.apply, {**self._inst(row), "spec": spec})
        self._set(name, spec=json.dumps(spec))
        self._audit("pg_instance.schedule_dropped", actor, name, {"schedule": sched})

    # ---- backups ---------------------------------------------------------------------------------------------
    def _backup_view(self, r) -> dict:
        return {"id": r.id, "instance": r.instance, "schedule": r.schedule, "state": r.state, "started_ms": r.started_ms,
                "finished_ms": r.finished_ms, "bytes": r.bytes, "start_lsn": r.start_lsn, "end_lsn": r.end_lsn,
                "timeline": r.timeline, "verified": r.verified, "verified_ms": r.verified_ms, "error": r.error,
                "expired": r.expired}

    def backups(self, actor: str, name: str) -> list[dict]:
        self._managed(name)
        self._see(actor, name)
        with self.engine.connect() as c:
            rows = c.execute(select(backups_table).where(backups_table.c.instance == name,
                                                         backups_table.c.expired.is_(False))
                             .order_by(backups_table.c.started_ms.desc())).all()
        return [self._backup_view(r) for r in rows]

    def backup(self, actor: str, name: str, schedule: str = "manual") -> dict:
        row = self._managed(name)
        if actor:
            self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        if self.store is None:
            raise InstanceError("backups are not set up here (CAIRN_PG_BACKUP_STORE)", 503)
        if row.state != "ready":
            raise InstanceError(f"{name} is {row.state}", 409)
        now = self.clock()
        bid = dt.datetime.fromtimestamp(now / 1000, dt.timezone.utc).strftime("%Y%m%dt%H%M%S") + "-" + secrets.token_hex(3)
        with self.engine.begin() as c:
            running = c.execute(select(backups_table.c.id).where(backups_table.c.instance == name,
                                                                 backups_table.c.state == "running",
                                                                 backups_table.c.started_ms > now - STUCK_MS)).first()
            if running:
                raise InstanceError(f"a backup of {name} is running ({running.id})", 409)
            c.execute(insert(backups_table).values(
                id=bid, instance=name, uid=row.uid, schedule=schedule, state="running", started_ms=now,
                finished_ms=None, bytes=None, start_lsn=None, end_lsn=None, timeline=None,
                location=f"{self._prefix(name, row.uid)}/base/{bid}", verified=None, verified_ms=None, error=None,
                expired=False))
        try:
            meta = self._call(self.runtime.backup, self._inst(row), bid)
        except InstanceError as e:
            with self.engine.begin() as c:
                c.execute(update(backups_table).where(backups_table.c.id == bid).values(
                    state="failed", finished_ms=self.clock(), error=str(e)[:2000]))
            self._audit("pg_instance.backup_failed", actor or "cairn", name, {"backup": bid, "error": str(e)[:300]})
            raise
        with self.engine.begin() as c:
            c.execute(update(backups_table).where(backups_table.c.id == bid).values(
                state="completed", finished_ms=self.clock(), bytes=meta.get("bytes"), start_lsn=meta.get("start_lsn"),
                end_lsn=meta.get("end_lsn"), timeline=meta.get("timeline"),
                location=meta.get("backup_id") and f"cnpg:{meta['backup_id']}" or
                f"{self._prefix(name, row.uid)}/base/{bid}"))
        self._audit("pg_instance.backup", actor or "cairn", name, {"backup": bid, "schedule": schedule,
                                                                  "bytes": meta.get("bytes")})
        return self._backup(bid)

    def _backup(self, bid: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(backups_table).where(backups_table.c.id == bid)).first()
        if r is None:
            raise InstanceError("no such backup", 404)
        return self._backup_view(r)

    def delete_backup(self, actor: str, name: str, bid: str) -> None:
        self._managed(name)
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        b = self._backup(bid)
        if b["instance"] != name or b["expired"]:
            raise InstanceError("no such backup", 404)
        self._expire_backup(bid)
        self._audit("pg_instance.backup_deleted", actor, name, {"backup": bid})

    def _expire_backup(self, bid: str) -> None:
        with self.engine.connect() as c:
            r = c.execute(select(backups_table).where(backups_table.c.id == bid)).first()
        if self.store is not None and r.location and not r.location.startswith("cnpg:"):
            for key in self.store.list(r.location + "/"):
                self.store.delete(key)
        with self.engine.begin() as c:
            c.execute(update(backups_table).where(backups_table.c.id == bid).values(expired=True))

    def verify(self, actor: str, name: str, bid: str) -> dict:
        """A restore check: the backup restored into a throwaway instance that must open, every database readable."""
        row = self._managed(name)
        if actor:
            self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        b = self._backup(bid)
        if b["instance"] != name or b["state"] != "completed" or b["expired"]:
            raise InstanceError("that backup cannot be restored (not completed, or expired)", 409)
        rt = self._runtime()
        if rt.name != "docker":
            raise InstanceError("restore checks on Kubernetes: restore into a new instance (POST .../restore)", 409)
        scratch = "verify-" + secrets.token_hex(4)
        inst = self._inst(row)
        verdict, error = "passed", None
        try:
            with self.engine.connect() as c:
                loc = c.execute(select(backups_table.c.location, backups_table.c.uid).where(backups_table.c.id == bid)).first()
            out = self._call(rt.restore, inst, loc.location, self._prefix(name, loc.uid), {"immediate": True},
                             archive=False, scratch=scratch)
            dbs = rt.psql(out["container"], "SELECT string_agg(datname, ',') FROM pg_database WHERE datallowconn")
            for db in [d for d in dbs.split(",") if d]:
                rt.psql(out["container"], "SELECT count(*) FROM pg_class", db=db)
        except (InstanceError, RuntimeError_) as e:
            verdict, error = "failed", str(e)[:2000]
        finally:
            try:
                rt.delete({"name": scratch}, purge=True)
            except RuntimeError_:
                logger.warning("restore check %s: could not remove %s", bid, scratch, exc_info=True)
        with self.engine.begin() as c:
            c.execute(update(backups_table).where(backups_table.c.id == bid).values(
                verified=verdict, verified_ms=self.clock(), error=error))
        self._audit("pg_instance.backup_verified", actor or "cairn", name, {"backup": bid, "result": verdict})
        return self._backup(bid)

    # ---- restore ---------------------------------------------------------------------------------------------
    def restore(self, actor: str, source: str, body: dict) -> dict:
        """A new instance from ``source``'s backups: as of ``target_time`` (point in time), of ``backup``, or as late
        as the archive goes. ``databases`` {old: new} renames the Cairn databases kept; the others are dropped."""
        src = self._managed(source)
        rt = self._runtime()
        new = check_name(body.get("name"))
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(source))
        self._need(actor, "CREATE_DATABASE_INSTANCE", None)
        if self.store is None:
            raise InstanceError("backups are not set up here (CAIRN_PG_BACKUP_STORE)", 503)
        extra = set(body) - {"name", "target_time", "backup", "databases", "cpu", "memory_mb", "storage_gb", "replicas"}
        if extra:
            raise InstanceError(f"unknown fields: {', '.join(sorted(extra))}")
        if body.get("target_time") and body.get("backup"):
            raise InstanceError("give target_time or backup, not both")
        mapping = body.get("databases") or {}
        if not isinstance(mapping, dict):
            raise InstanceError('databases is {"<database on the source>": "<its name on the new instance>"}')
        if self.databases is not None:
            self.databases.check_new_names(source, mapping)
        target_ms = parse_time(body["target_time"]) if body.get("target_time") else None
        if target_ms is not None and target_ms > self.clock():
            raise InstanceError("target_time is in the future")
        if rt.name == "kubernetes" and not body.get("backup"):
            base = {"id": None, "uid": src.uid, "location": None, "backup": None}   # the operator picks the backup
        else:
            base = self._choose_backup(source, target_ms, body.get("backup"))
        spec = json.loads(src.spec)
        spec = self._call(check_spec, {k: body[k] for k in ("cpu", "memory_mb", "storage_gb", "replicas") if k in body},
                          base=spec)
        spec["backup_schedules"] = list(spec.get("backup_schedules") or [])
        self._fits(spec)
        # What was written up to now must be in the archive before it is replayed.
        if src.state == "ready" and rt.name == "docker" and (target_ms is not None or not body.get("backup")):
            self._call(rt.switch_wal, self._inst(src))
        uid = secrets.token_hex(6)
        now = self.clock()
        origin = {"instance": source, "uid": src.uid, "target_time": body.get("target_time"),
                  "backup": base["id"], "databases": mapping}
        with self.engine.begin() as c:
            if c.execute(select(instances_table.c.name).where(instances_table.c.name == new)).first():
                raise InstanceError(f"{new} exists", 409)
            c.execute(insert(instances_table).values(
                name=new, uid=uid, runtime=rt.name, spec=json.dumps(spec), image=None, state="restoring", host=None,
                port=None, restored_from=json.dumps(origin), pending=None, error=None, created_by=actor,
                created_ms=now, updated_ms=now))
        inst = {"name": new, "uid": uid, "spec": spec, "image": spec.get("image"), "port": None,
                "backup_prefix": self._prefix(new, uid)}
        try:
            if rt.name == "docker":
                recovery = ({"target_time": pg_time(target_ms)} if target_ms is not None
                            else {"immediate": True} if body.get("backup") else {})
                where = self._call(rt.restore, inst, base["location"], self._prefix(source, base["uid"]), recovery)
            else:
                recovery = {"source_prefix": self._prefix(source, base["uid"]), "source_name": source}
                if target_ms is not None:
                    recovery["target_time"] = pg_time(target_ms)
                elif body.get("backup"):
                    recovery["backup_id"] = base["location"].removeprefix("cnpg:")
                where = self._call(rt.restore_cluster, inst, self.password(new, uid), recovery)
        except InstanceError as e:
            self._set(new, state="failed", error=str(e)[:2000])
            self._audit("pg_instance.restore_failed", actor, new, {"from": source, "error": str(e)[:300]})
            raise
        self._set(new, image=where.get("image"), host=where.get("host") or None, port=where.get("port"))
        self._audit("pg_instance.restored", actor, new, {"from": source, "target_time": body.get("target_time"),
                                                          "backup": base["id"]})
        if rt.name == "docker":
            self._after_restore(new)
            self._after_ready(new)
            # Its own first base backup: the new instance (a new timeline) is recoverable on its own from now on.
            try:
                self.backup("", new, "restore")
            except InstanceError as e:
                logger.warning("instance %s: first backup after the restore failed: %s", new, e)
        return self.get(actor, new)

    def _choose_backup(self, source: str, target_ms: Optional[int], bid: Optional[str]) -> dict:
        with self.engine.connect() as c:
            q = select(backups_table).where(backups_table.c.instance == source, backups_table.c.state == "completed",
                                            backups_table.c.expired.is_(False))
            rows = c.execute(q.order_by(backups_table.c.finished_ms.desc())).all()
        if bid:
            for r in rows:
                if r.id == bid:
                    return {"id": r.id, "location": r.location, "uid": r.uid, "backup": r.id}
            raise InstanceError("no such backup of that instance (completed and kept)", 404)
        for r in rows:
            if target_ms is None or (r.finished_ms or 0) <= target_ms:
                return {"id": r.id, "location": r.location, "uid": r.uid, "backup": None}
        if not rows:
            raise InstanceError(f"{source} has no backup yet", 409)
        earliest = min(r.finished_ms for r in rows)
        raise InstanceError(f"no backup before that time: the earliest point is "
                            f"{dt.datetime.fromtimestamp(earliest / 1000, dt.timezone.utc).isoformat()}", 409)

    def _after_restore(self, name: str) -> None:
        """A restored instance gets its own superuser password; the source's logins are dropped; its Cairn databases
        are renamed as asked, the others dropped, and the renamed ones registered."""
        import psycopg
        from psycopg import sql

        row = self._row(name)
        origin = json.loads(row.restored_from)
        mine = self.password(name, row.uid)
        theirs = self.password(origin["instance"], origin["uid"])
        host, port = self._call(self._runtime().endpoint, self._inst(row))

        def conn_as(pw: str, db: str = "postgres"):
            return psycopg.connect(host=host, port=port, user="postgres", password=pw, dbname=db, sslmode="require",
                                   connect_timeout=10, autocommit=True)
        try:
            conn = conn_as(mine)
        except psycopg.OperationalError:
            conn = conn_as(theirs)
        with conn:
            conn.execute("SET log_statement = 'none'")
            conn.execute(sql.SQL("ALTER ROLE postgres PASSWORD {}").format(sql.Literal(mine)))
        dbs = self.databases
        source_dbs = dbs.on_instance(origin["instance"], include_dropped=True) if dbs is not None else []
        mapping = origin.get("databases") or {}
        with conn_as(mine) as conn:
            present = {r[0] for r in conn.execute("SELECT datname FROM pg_database").fetchall()}
            for (role,) in conn.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'cdb\\_u\\_%'").fetchall():
                conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (role,))
                try:
                    conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
                except psycopg.Error:
                    conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(role)))
            keep, drop = [], []
            for db in source_dbs:
                if db not in present:
                    continue
                (keep if db in mapping else drop).append(db)
            for db in drop:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db)))
            for db in keep:
                new = mapping[db]
                conn.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(db), sql.Identifier(new)))
                for old_r, new_r in ((f"cdb_{db}_owner", f"cdb_{new}_owner"), (f"cdb_{db}_read", f"cdb_{new}_read")):
                    conn.execute(sql.SQL("ALTER ROLE {} RENAME TO {}").format(sql.Identifier(old_r), sql.Identifier(new_r)))
        if dbs is not None:
            for db in drop:
                dbs.forget_roles_on(name, [mapping[k] for k in keep], [f"cdb_{db}_owner", f"cdb_{db}_read"])
            for db in keep:
                dbs.register_restored(mapping[db], name, db, row.created_by)

    # ---- health, metrics, slow queries -----------------------------------------------------------------------
    def _sql_health(self, name: str) -> dict:
        with self._connect(name) as conn:
            q = lambda s: conn.execute(s).fetchone()  # noqa: E731
            recovery = q("SELECT pg_is_in_recovery()")[0]
            conns = q("SELECT count(*), count(*) FILTER (WHERE state = 'active'), "
                      "count(*) FILTER (WHERE state LIKE 'idle in transaction%') FROM pg_stat_activity "
                      "WHERE backend_type = 'client backend'")
            maxc = int(q("SHOW max_connections")[0])
            size = q("SELECT coalesce(sum(pg_database_size(datname)), 0) FROM pg_database")[0]
            try:
                wal = q("SELECT coalesce(sum(size), 0) FROM pg_ls_waldir()")[0]
            except Exception:  # noqa: BLE001 - needs the superuser or pg_monitor
                wal = None
            arch = conn.execute("SELECT archived_count, last_archived_wal, last_archived_time, failed_count, "
                                "last_failed_wal, last_failed_time FROM pg_stat_archiver").fetchone()
            mode = q("SHOW archive_mode")[0]
            repl = conn.execute(
                "SELECT application_name, client_addr::text, state, sync_state, "
                "extract(epoch FROM replay_lag)::float, "
                "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint FROM pg_stat_replication").fetchall()
            uptime = q("SELECT extract(epoch FROM now() - pg_postmaster_start_time())::bigint")[0]
            version = q("SHOW server_version")[0]
        ts = lambda t: int(t.timestamp() * 1000) if t else None  # noqa: E731
        archiving = {"enabled": mode != "off", "archived_count": arch[0], "last_archived_wal": arch[1],
                     "last_archived_ms": ts(arch[2]), "failed_count": arch[3], "last_failed_wal": arch[4],
                     "last_failed_ms": ts(arch[5])}
        archiving["ok"] = mode != "off" and not (arch[5] and (not arch[2] or arch[5] > arch[2]))
        return {"in_recovery": recovery, "server_version": version, "uptime_s": uptime,
                "connections": {"total": conns[0], "active": conns[1], "idle_in_transaction": conns[2], "max": maxc,
                                "percent": round(100.0 * conns[0] / maxc, 1) if maxc else None},
                "storage": {"databases_bytes": int(size), "wal_bytes": int(wal) if wal is not None else None},
                "replication": [{"standby": r[0], "address": r[1], "state": r[2], "sync": r[3],
                                 "replay_lag_s": r[4], "lag_bytes": r[5]} for r in repl],
                "wal_archiving": archiving}

    def status(self, actor: str, name: str) -> dict:
        """Health: the runtime's view, connections, storage, replication lag, last backup, WAL archiving."""
        self._see(actor, name)
        out: dict[str, Any] = {"name": name}
        problems: list[str] = []
        if name != DEFAULT:
            row = self._row(name)
            if row.state in ("creating", "restoring") and self.runtime is not None and self.runtime.name == "kubernetes":
                self.reconcile(name)
                row = self._row(name)
            spec = json.loads(row.spec)
            out.update({"state": row.state, "runtime": row.runtime, "pending": json.loads(row.pending) if row.pending else []})
            try:
                out["runtime_state"] = self._call(self._runtime().state, self._inst(row))
            except InstanceError as e:
                out["runtime_state"] = {"error": str(e)}
                problems.append("the runtime does not answer")
            with self.engine.connect() as c:
                last = c.execute(select(backups_table).where(backups_table.c.instance == name,
                                                             backups_table.c.state == "completed")
                                 .order_by(backups_table.c.finished_ms.desc())).first()
                failed = c.execute(select(backups_table).where(backups_table.c.instance == name,
                                                               backups_table.c.state == "failed")
                                   .order_by(backups_table.c.started_ms.desc())).first()
            out["last_backup"] = self._backup_view(last) if last else None
            if failed and (not last or failed.started_ms > last.started_ms):
                problems.append(f"the last backup failed: {(failed.error or '')[:200]}")
            scheds = spec.get("backup_schedules") or []
            if scheds:
                every = min(s["every_hours"] for s in scheds) * 3600 * 1000
                if not last or self.clock() - (last.finished_ms or 0) > 2 * every:
                    problems.append("no backup within twice its schedule")
            limit = int(spec["storage_gb"]) * 1024 ** 3
        else:
            out.update({"state": "ready", "runtime": "external"})
            limit = None
        if out.get("state") in ("creating", "restoring", "failed", "deleting"):
            out["health"] = out["state"]
            return out
        try:
            sqlh = self._sql_health(name)
            out.update(sqlh)
            used = sqlh["storage"]["databases_bytes"] + (sqlh["storage"]["wal_bytes"] or 0)
            if limit:
                out["storage"]["limit_bytes"] = limit
                out["storage"]["percent"] = round(100.0 * used / limit, 1)
                if used > 0.85 * limit:
                    problems.append("storage is over 85% full")
            if sqlh["connections"]["percent"] and sqlh["connections"]["percent"] > 90:
                problems.append("connections are over 90% of max_connections")
            if sqlh["wal_archiving"]["enabled"] and not sqlh["wal_archiving"]["ok"]:
                problems.append("WAL archiving is failing")
            for r in sqlh["replication"]:
                if (r["lag_bytes"] or 0) > 256 * 1024 * 1024:
                    problems.append(f"standby {r['standby']} is {r['lag_bytes'] // (1024 * 1024)} MB behind")
            rs = out.get("runtime_state") or {}
            if rs.get("wanted_instances") and rs.get("ready_instances", 0) < rs["wanted_instances"]:
                problems.append(f"{rs['ready_instances']} of {rs['wanted_instances']} PostgreSQL pods are ready")
        except InstanceError as e:
            out["health"] = "down"
            out["problems"] = problems + [str(e)]
            return out
        out["problems"] = problems
        out["health"] = "degraded" if problems else "healthy"
        return out

    def metrics(self, actor: str, name: str) -> dict:
        """Numbers for dashboards and the SRE agent: per database (commits, rollbacks, cache hits, rows, deadlocks,
        temporary files, size), connections, replication, WAL, checkpoints; on Kubernetes, the operator's exporter."""
        self._see(actor, name)
        if name != DEFAULT:
            row = self._row(name)
            if row.state not in ("ready", "updating"):
                raise InstanceError(f"{name} is {row.state}", 409)
        with self._connect(name) as conn:
            per_db = conn.execute(
                "SELECT datname, numbackends, xact_commit, xact_rollback, blks_read, blks_hit, tup_returned, "
                "tup_fetched, tup_inserted, tup_updated, tup_deleted, conflicts, temp_files, temp_bytes, deadlocks, "
                "pg_database_size(datname) FROM pg_stat_database WHERE datname IS NOT NULL ORDER BY datname").fetchall()
            waiting = conn.execute("SELECT count(*) FROM pg_locks WHERE NOT granted").fetchone()[0]
            oldest = conn.execute("SELECT coalesce(max(extract(epoch FROM now() - xact_start)), 0)::float "
                                  "FROM pg_stat_activity WHERE backend_type = 'client backend'").fetchone()[0]
            wal_lsn = None if conn.execute("SELECT pg_is_in_recovery()").fetchone()[0] else \
                conn.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint").fetchone()[0]
            try:
                ck = conn.execute("SELECT num_timed, num_requested FROM pg_stat_checkpointer").fetchone()
            except Exception:  # noqa: BLE001 - before PostgreSQL 17
                ck = conn.execute("SELECT checkpoints_timed, checkpoints_req FROM pg_stat_bgwriter").fetchone()
        dbs = []
        for r in per_db:
            reads = (r[4] or 0) + (r[5] or 0)
            dbs.append({"database": r[0], "connections": r[1], "commits": r[2], "rollbacks": r[3],
                        "blocks_read": r[4], "blocks_hit": r[5],
                        "cache_hit_ratio": round(r[5] / reads, 4) if reads else None,
                        "rows_returned": r[6], "rows_fetched": r[7], "rows_inserted": r[8], "rows_updated": r[9],
                        "rows_deleted": r[10], "conflicts": r[11], "temp_files": r[12], "temp_bytes": r[13],
                        "deadlocks": r[14], "size_bytes": r[15]})
        health = self._sql_health(name)
        out = {"name": name, "at_ms": self.clock(), "databases": dbs, "connections": health["connections"],
               "replication": health["replication"], "wal_archiving": health["wal_archiving"],
               "storage": health["storage"], "locks_waiting": waiting, "longest_transaction_s": oldest,
               "wal_position_bytes": wal_lsn, "checkpoints": {"timed": ck[0], "requested": ck[1]},
               "uptime_s": health["uptime_s"]}
        if name != DEFAULT and getattr(self.runtime, "name", "") == "kubernetes":
            try:
                out["exporter"] = self.runtime.exporter(self._inst(self._row(name)),
                                                        ("cnpg_collector_", "cnpg_pg_replication", "cnpg_pg_stat_archiver"))
            except RuntimeError_ as e:
                out["exporter"] = {"error": str(e)}
        return out

    def slow_queries(self, actor: str, name: str, limit: int = 20, order: str = "total") -> dict:
        """The statements that took longest (pg_stat_statements: normalised text, constants replaced)."""
        self._need(actor, "MANAGE_DATABASE_INSTANCE", urn(name))
        if name != DEFAULT:
            self._row(name)
        if not 1 <= limit <= 200:
            raise InstanceError("limit is 1 to 200")
        col = {"total": "total_exec_time", "mean": "mean_exec_time", "calls": "calls", "rows": "rows"}.get(order)
        if col is None:
            raise InstanceError("order is total, mean, calls or rows")
        import psycopg

        with self._connect(name) as conn:
            try:
                rows = conn.execute(
                    "SELECT s.queryid::text, d.datname, r.rolname, s.calls, s.total_exec_time, s.mean_exec_time, "
                    "s.max_exec_time, s.rows, s.shared_blks_hit, s.shared_blks_read, s.temp_blks_written, "
                    "left(s.query, 4000) FROM pg_stat_statements s LEFT JOIN pg_database d ON d.oid = s.dbid "
                    f"LEFT JOIN pg_roles r ON r.oid = s.userid ORDER BY s.{col} DESC LIMIT %s", (limit,)).fetchall()
            except psycopg.errors.UndefinedTable:
                raise InstanceError("pg_stat_statements is not installed on this instance", 409)
            except psycopg.errors.ObjectNotInPrerequisiteState:
                raise InstanceError("pg_stat_statements is not loaded (shared_preload_libraries)", 409)
        return {"name": name, "order": order, "queries": [
            {"queryid": r[0], "database": r[1], "role": r[2], "calls": r[3], "total_ms": round(r[4], 3),
             "mean_ms": round(r[5], 3), "max_ms": round(r[6], 3), "rows": r[7], "shared_blocks_hit": r[8],
             "shared_blocks_read": r[9], "temp_blocks_written": r[10], "query": r[11]} for r in rows]}

    # ---- the scheduler ---------------------------------------------------------------------------------------
    def run_due(self) -> int:
        """Scheduled backups (and their restore checks), retention, changes due in their maintenance window, and
        instances the operator is still making. Long work runs in its own thread; one per instance at a time."""
        if self.runtime is None:
            return 0
        now = self.clock()
        did = 0
        with self.engine.begin() as c:                  # backups cut off by a restart
            c.execute(update(backups_table).where(backups_table.c.state == "running",
                                                  backups_table.c.started_ms < now - STUCK_MS)
                      .values(state="failed", error="cut off (Cairn restarted during the backup)"))
            rows = c.execute(select(instances_table)).all()
        if self.runtime.name == "docker":
            try:
                self.runtime.sweep(max(7200, getattr(self.runtime, "restore_timeout", 900) * 2))
            except RuntimeError_ as e:
                logger.warning("restore checks left behind: %s", e)
        for row in rows:
            if row.state in ("creating", "restoring") and self.runtime.name == "kubernetes":
                self.reconcile(row.name)
                continue
            if row.state != "ready":
                continue
            t = self._threads.get(row.name)
            if t is not None and t.is_alive():
                continue
            spec = json.loads(row.spec)
            pending = json.loads(row.pending) if row.pending else []
            if pending and spec.get("maintenance_window") and in_window(spec["maintenance_window"], self._now()):
                self._spawn(row.name, lambda n=row.name: self.apply_pending(self._system_actor(n), n))
                did += 1
                continue
            if self.store is None or self.runtime.name != "docker":
                continue
            try:
                self.runtime.heal(self._inst(row))
            except RuntimeError_ as e:
                logger.warning("instance %s: %s", row.name, e)
            due = self._due_schedule(row.name, spec, now)
            if due is not None:
                def work(n=row.name, s=due):
                    b = self.backup("", n, s["name"])
                    if s.get("verify"):
                        self.verify("", n, b["id"])
                self._spawn(row.name, work)
                did += 1
                continue
            did += self._retention(row.name, spec, now)
        return did

    def _system_actor(self, name: str) -> str:
        """Scheduled maintenance acts with the rights of whoever made the instance."""
        return self._row(name).created_by

    def _spawn(self, name: str, fn) -> None:
        def run():
            try:
                fn()
            except Exception:  # noqa: BLE001 - recorded on the backup or the instance
                logger.warning("instance %s: scheduled work failed", name, exc_info=True)
        t = threading.Thread(target=run, daemon=True, name=f"cairn-pg-{name}")
        self._threads[name] = t
        t.start()

    def _due_schedule(self, name: str, spec: dict, now: int) -> Optional[dict]:
        for s in spec.get("backup_schedules") or []:
            with self.engine.connect() as c:
                last = c.execute(select(backups_table.c.started_ms).where(
                    backups_table.c.instance == name, backups_table.c.schedule == s["name"])
                    .order_by(backups_table.c.started_ms.desc())).first()
            if last is None or now - last.started_ms >= s["every_hours"] * 3600 * 1000:
                return s
        return None

    def _retention(self, name: str, spec: dict, now: int) -> int:
        """Backups past their schedule's retention are deleted (never the newest), then WAL older than the oldest
        backup kept."""
        keep_days = {s["name"]: s["retention_days"] for s in spec.get("backup_schedules") or []}
        with self.engine.connect() as c:
            rows = c.execute(select(backups_table).where(backups_table.c.instance == name,
                                                         backups_table.c.expired.is_(False),
                                                         backups_table.c.state.in_(("completed", "failed")))
                             .order_by(backups_table.c.started_ms.desc())).all()
        completed = [r for r in rows if r.state == "completed"]
        newest = completed[0].id if completed else None
        gone = 0
        for r in rows:
            days = keep_days.get(r.schedule)
            if days is None or r.id == newest:
                continue
            if now - r.started_ms > days * 86400 * 1000:
                self._expire_backup(r.id)
                gone += 1
        if gone and self.store is not None:
            kept = [r for r in completed if r.id == newest or keep_days.get(r.schedule) is None
                    or now - r.started_ms <= keep_days[r.schedule] * 86400 * 1000]
            oldest = min(kept, key=lambda r: r.started_ms) if kept else None
            if oldest is not None and oldest.start_lsn and oldest.timeline:
                floor = wal_name(int(oldest.timeline), oldest.start_lsn)[8:]
                prefix = f"{self._prefix(name, oldest.uid)}/wal/"
                for key in self.store.list(prefix):
                    f = key[len(prefix):]
                    if len(f) >= 24 and re.match(r"^[0-9A-F]{24}", f) and f[8:24] < floor:
                        self.store.delete(key)
        return gone


def build_instances_router(inst: Instances, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/databases/instances", tags=["databases"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except InstanceError as e:
            raise HTTPException(e.status, str(e))
        except SpecError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return {"instances": await call(lambda: inst.list(p.urn)),
                "runtime": getattr(inst.runtime, "name", None), "backups": inst.store is not None,
                "parameters": sorted(PARAMETERS)}

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: inst.create(p.urn, body))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: inst.get(p.urn, name))

    @router.patch("/{name}")
    async def patch(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: inst.update(p.urn, name, body))

    @router.delete("/{name}", status_code=204)
    async def delete(name: str, p=Depends(current_principal)):
        await call(lambda: inst.delete(p.urn, name))
        return Response(status_code=204)

    @router.put("/{name}/parameters/{param}")
    async def set_param(name: str, param: str, value: Any = Body(..., embed=True), p=Depends(current_principal)):
        return await call(lambda: inst.set_parameter(p.urn, name, param, value))

    @router.delete("/{name}/parameters/{param}", status_code=204)
    async def drop_param(name: str, param: str, p=Depends(current_principal)):
        await call(lambda: inst.drop_parameter(p.urn, name, param))
        return Response(status_code=204)

    @router.put("/{name}/backup-schedules/{sched}")
    async def set_sched(name: str, sched: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: inst.set_schedule(p.urn, name, sched, body))

    @router.delete("/{name}/backup-schedules/{sched}", status_code=204)
    async def drop_sched(name: str, sched: str, p=Depends(current_principal)):
        await call(lambda: inst.drop_schedule(p.urn, name, sched))
        return Response(status_code=204)

    @router.get("/{name}/backups")
    async def backups(name: str, p=Depends(current_principal)):
        return {"backups": await call(lambda: inst.backups(p.urn, name))}

    @router.post("/{name}/backups", status_code=201)
    async def backup(name: str, p=Depends(current_principal)):
        return await call(lambda: inst.backup(p.urn, name))

    @router.post("/{name}/backups/{bid}/verify")
    async def verify(name: str, bid: str, p=Depends(current_principal)):
        return await call(lambda: inst.verify(p.urn, name, bid))

    @router.delete("/{name}/backups/{bid}", status_code=204)
    async def delete_backup(name: str, bid: str, p=Depends(current_principal)):
        await call(lambda: inst.delete_backup(p.urn, name, bid))
        return Response(status_code=204)

    @router.post("/{name}/restore", status_code=201)
    async def restore(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: inst.restore(p.urn, name, body))

    @router.post("/{name}/switchover")
    async def switchover(name: str, target: str = Body("", embed=True), p=Depends(current_principal)):
        return await call(lambda: inst.switchover(p.urn, name, target))

    @router.post("/{name}/restart")
    async def restart(name: str, p=Depends(current_principal)):
        return await call(lambda: inst.restart(p.urn, name))

    @router.post("/{name}/apply-pending")
    async def apply_pending(name: str, p=Depends(current_principal)):
        return await call(lambda: inst.apply_pending(p.urn, name))

    @router.get("/{name}/status")
    async def status(name: str, p=Depends(current_principal)):
        return await call(lambda: inst.status(p.urn, name))

    @router.get("/{name}/metrics")
    async def metrics(name: str, p=Depends(current_principal)):
        return await call(lambda: inst.metrics(p.urn, name))

    @router.get("/{name}/slow-queries")
    async def slow(name: str, limit: int = Query(20), order: str = Query("total"), p=Depends(current_principal)):
        return await call(lambda: inst.slow_queries(p.urn, name, limit, order))

    return router
