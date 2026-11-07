"""Operational databases: PostgreSQL databases for applications, made and handed out through Cairn.

Operators point Cairn at a PostgreSQL server it may administer (``CAIRN_DATABASES_ADMIN_URL``, or ``..._FILE``:
a role with ``CREATEDB`` and ``CREATEROLE``) and say where clients reach it (``CAIRN_DATABASES_HOST``,
``CAIRN_DATABASES_PORT``, ``CAIRN_DATABASES_SSLMODE``). Then:

* ``POST /v1/databases`` ``{"name"}`` makes a database (``CREATE_DATABASE``). Its owner and reader are roles
  that cannot log in (``cdb_<name>_owner``, ``cdb_<name>_read``), and only they may connect to it. Whoever
  makes it gets ``MANAGE_DATABASE`` through the policies that name it.
* ``POST /v1/databases/<name>/credentials`` ``{"access": "read" | "write", "ttl_minutes"}`` gives a person a
  login of their own that works for at most a day:
  - ``write`` needs ``USE_DATABASE`` on ``urn:li:pgDatabase:<name>``; the login acts as the owner role, so what
    it creates belongs to the database, not to the login;
  - ``read`` needs ``VIEW_DATABASE``, and its sessions are read-only.

  The password is shown once and never stored. The login (``cdb_u_<id>``) is recorded with who asked, is
  dropped when it expires (sessions ended), and is audited (``database.credentials``).
* ``POST /v1/databases/<name>/branches`` ``{"name"}`` copies the database to a new one
  (``CREATE DATABASE ... TEMPLATE``), with its own roles, for a test or a migration rehearsal
  (``MANAGE_DATABASE`` on the source, ``CREATE_DATABASE``). PostgreSQL copies only a database nobody is connected
  to: a busy source answers 409.
* ``DELETE /v1/databases/<name>`` ends its sessions and drops it, its roles and its logins (``MANAGE_DATABASE``).

People never get the administrator's role; nothing here takes SQL from a caller, and every name is checked and
quoted.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, String, Table, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

databases_table = Table(
    "cairn_databases", metadata,
    Column("name", String(31), primary_key=True),
    Column("created_by", String(512), nullable=False),
    Column("branch_of", String(31), nullable=True),
    Column("created_ms", BigInteger, nullable=False),
)
logins_table = Table(
    "cairn_database_logins", metadata,
    Column("role", String(63), primary_key=True),
    Column("database", String(31), nullable=False, index=True),
    Column("actor", String(512), nullable=False, index=True),
    Column("access", String(8), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("expires_ms", BigInteger, nullable=False, index=True),
    Column("dropped", Boolean, nullable=False),
)

NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
MAX_TTL_MINUTES = 24 * 60
RESERVED = {"postgres", "template0", "template1"}


class DatabaseError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def urn(name: str) -> str:
    return f"urn:li:pgDatabase:{name}"


class Databases:
    """Databases on one PostgreSQL server Cairn administers. ``allowed(actor, privilege, urn_or_None)`` is the PDP."""

    def __init__(self, engine, admin_url: str, host: str, port: int, allowed: Callable[[str, str, Optional[str]], bool],
                 audit=None, sslmode: str = "require", clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        if not admin_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseError("CAIRN_DATABASES_ADMIN_URL is a postgresql:// URL", 503)
        self.engine, self.admin_url, self.host, self.port = engine, admin_url, host, port
        self.allowed, self.audit, self.sslmode, self.clock = allowed, audit, sslmode, clock

    # ---- the server --------------------------------------------------------------------------
    def _admin(self, dbname: Optional[str] = None):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        info = conninfo_to_dict(self.admin_url)
        if dbname:
            info["dbname"] = dbname
        return psycopg.connect(make_conninfo(**info), autocommit=True, connect_timeout=10)

    @staticmethod
    def _roles(name: str) -> tuple[str, str]:
        return f"cdb_{name}_owner", f"cdb_{name}_read"

    def _check(self, name: str) -> str:
        if not NAME.match(name or "") or name in RESERVED:
            raise DatabaseError("a database name is lowercase letters, digits and '_' (up to 31), starting with a letter")
        return name

    def _need(self, actor: str, privilege: str, target: Optional[str]) -> None:
        try:
            ok = self.allowed(actor, privilege, target)
        except Exception:  # noqa: BLE001 - fail closed
            logger.warning("database decision failed (%s %s %s)", actor, privilege, target, exc_info=True)
            ok = False
        if not ok:
            raise DatabaseError(f"{privilege} is needed" + (f" on {target}" if target else ""), 403)

    def _row(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(databases_table).where(databases_table.c.name == name)).first()
        if row is None:
            raise DatabaseError("no such database", 404)
        return row

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("data", action, actor, urn(name), details)

    def _setup(self, name: str, template: Optional[str] = None) -> None:
        """Makes the database (as a copy of ``template`` if given) with its owner and reader roles."""
        from psycopg import errors, sql

        owner, reader = self._roles(name)
        with self._admin() as conn:
            for role in (owner, reader):
                try:
                    conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
                except errors.DuplicateObject:
                    pass
            # The administrator must be a member of the owner role to give it the database (PostgreSQL 16).
            conn.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(owner)))
            stmt = sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(owner))
            if template:
                stmt = sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {}").format(
                    sql.Identifier(name), sql.Identifier(owner), sql.Identifier(template))
            try:
                conn.execute(stmt)
            except errors.DuplicateDatabase:
                raise DatabaseError(f"{name} exists on the server", 409)
            except errors.ObjectInUse:
                raise DatabaseError(f"{template} has open sessions: PostgreSQL copies only a database nobody is "
                                    "connected to", 409)
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
            conn.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}, {}").format(
                sql.Identifier(name), sql.Identifier(owner), sql.Identifier(reader)))
        with self._admin(name) as conn:
            conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            conn.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(owner)))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(reader)))
            conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(reader)))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}")
                         .format(sql.Identifier(owner), sql.Identifier(reader)))
            if template:
                # A copy keeps the source's objects, owned by the source's owner: they become this database's.
                # Object by object: REASSIGN OWNED would also hand over the source database itself.
                self._own_objects(conn, self._roles(template)[0], owner)

    @staticmethod
    def _own_objects(conn, old: str, new: str) -> None:
        """Gives ``new`` the objects ``old`` owns in the connected database (schemas, tables, views, sequences,
        functions, types)."""
        from psycopg import sql

        rels = conn.execute(
            "SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relowner = %s::regrole AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') "
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid "
            "AND d.deptype IN ('a', 'i'))", (old,)).fetchall()
        kinds = {"r": "TABLE", "p": "TABLE", "f": "FOREIGN TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW",
                 "S": "SEQUENCE"}
        for schema, rel, kind in rels:                 # a table's own sequences follow the table
            conn.execute(sql.SQL("ALTER {} {}.{} OWNER TO {}").format(
                sql.SQL(kinds[kind]), sql.Identifier(schema), sql.Identifier(rel), sql.Identifier(new)))
        for (schema,) in conn.execute("SELECT nspname FROM pg_namespace WHERE nspowner = %s::regrole", (old,)).fetchall():
            conn.execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(sql.Identifier(schema), sql.Identifier(new)))
        for (sig,) in conn.execute("SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.proowner = %s::regrole",
                                   (old,)).fetchall():
            conn.execute(sql.SQL("ALTER ROUTINE {} OWNER TO {}").format(sql.SQL(sig), sql.Identifier(new)))
        for (typ,) in conn.execute(
                "SELECT t.oid::regtype::text FROM pg_type t WHERE t.typowner = %s::regrole AND t.typtype IN ('e', 'd', 'r') "
                "AND t.typelem = 0", (old,)).fetchall():
            conn.execute(sql.SQL("ALTER TYPE {} OWNER TO {}").format(sql.SQL(typ), sql.Identifier(new)))

    # ---- what people do --------------------------------------------------------------------------
    def create(self, actor: str, name: str) -> dict:
        self._check(name)
        self._need(actor, "CREATE_DATABASE", None)
        with self.engine.connect() as c:
            if c.execute(select(databases_table.c.name).where(databases_table.c.name == name)).first():
                raise DatabaseError(f"{name} exists", 409)
        self._setup(name)
        with self.engine.begin() as c:
            c.execute(insert(databases_table).values(name=name, created_by=actor, branch_of=None, created_ms=self.clock()))
        self._audit("database.created", actor, name, {})
        return self.get(actor, name)

    def branch(self, actor: str, source: str, name: str) -> dict:
        self._check(name)
        self._row(source)
        self._need(actor, "MANAGE_DATABASE", urn(source))
        self._need(actor, "CREATE_DATABASE", None)
        with self.engine.connect() as c:
            if c.execute(select(databases_table.c.name).where(databases_table.c.name == name)).first():
                raise DatabaseError(f"{name} exists", 409)
        self._setup(name, template=source)
        with self.engine.begin() as c:
            c.execute(insert(databases_table).values(name=name, created_by=actor, branch_of=source, created_ms=self.clock()))
        self._audit("database.branched", actor, name, {"from": source})
        return self.get(actor, name)

    def get(self, actor: str, name: str) -> dict:
        row = self._row(name)
        if not any(self.allowed(actor, p, urn(name)) for p in ("VIEW_DATABASE", "USE_DATABASE", "MANAGE_DATABASE")):
            raise DatabaseError("no such database", 404)
        return {"name": row.name, "urn": urn(row.name), "created_by": row.created_by, "branch_of": row.branch_of,
                "created_ms": row.created_ms, "host": self.host, "port": self.port, "database": row.name}

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as c:
            names = [r.name for r in c.execute(select(databases_table.c.name).order_by(databases_table.c.name))]
        out = []
        for n in names:
            try:
                out.append(self.get(actor, n))
            except DatabaseError:
                continue
        return out

    def credentials(self, actor: str, name: str, access: str = "read", ttl_minutes: int = 60) -> dict:
        from psycopg import sql

        self._row(name)
        if access not in ("read", "write"):
            raise DatabaseError("access is read or write")
        if not isinstance(ttl_minutes, int) or not 5 <= ttl_minutes <= MAX_TTL_MINUTES:
            raise DatabaseError(f"ttl_minutes is 5 to {MAX_TTL_MINUTES}")
        self._need(actor, "USE_DATABASE" if access == "write" else "VIEW_DATABASE", urn(name))
        owner, reader = self._roles(name)
        role = "cdb_u_" + hashlib.sha256(f"{actor}:{secrets.token_hex(8)}".encode()).hexdigest()[:20]
        password = secrets.token_urlsafe(24)
        now = self.clock()
        expires = now + ttl_minutes * 60_000
        until = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime(expires / 1000))
        with self._admin() as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} VALID UNTIL {} IN ROLE {} CONNECTION LIMIT 20").format(
                sql.Identifier(role), sql.Literal(password), sql.Literal(until),
                sql.Identifier(owner if access == "write" else reader)))
            if access == "write":
                # What the login creates belongs to the database's owner role, so it outlives the login.
                conn.execute(sql.SQL("ALTER ROLE {} SET role = {}").format(sql.Identifier(role), sql.Literal(owner)))
            else:
                conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(role)))
        with self.engine.begin() as c:
            c.execute(insert(logins_table).values(role=role, database=name, actor=actor, access=access, created_ms=now,
                                                  expires_ms=expires, dropped=False))
        self._audit("database.credentials", actor, name, {"role": role, "access": access, "ttl_minutes": ttl_minutes})
        return {"host": self.host, "port": self.port, "database": name, "user": role, "password": password,
                "sslmode": self.sslmode, "access": access, "expires_ms": expires}
        # No connection URL: a password in a URL ends up in shell history and logs.

    def _drop_login(self, conn, role: str) -> None:
        from psycopg import sql

        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (role,))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))

    def expire(self) -> int:
        """Drops the logins that have expired (their sessions ended). Run by the scheduler."""
        now = self.clock()
        with self.engine.connect() as c:
            roles = [r.role for r in c.execute(select(logins_table.c.role).where(
                logins_table.c.expires_ms <= now, logins_table.c.dropped.is_(False)))]
        if not roles:
            return 0
        with self._admin() as conn:
            for role in roles:
                self._drop_login(conn, role)
        with self.engine.begin() as c:
            c.execute(update(logins_table).where(logins_table.c.role.in_(roles)).values(dropped=True))
        return len(roles)

    def revoke(self, actor: str, name: str, role: str) -> None:
        """Ends one login now: its owner may, and so may the database's managers."""
        with self.engine.connect() as c:
            row = c.execute(select(logins_table).where(logins_table.c.role == role, logins_table.c.database == name)).first()
        if row is None or row.dropped:
            raise DatabaseError("no such login", 404)
        if row.actor != actor:
            self._need(actor, "MANAGE_DATABASE", urn(name))
        with self._admin() as conn:
            self._drop_login(conn, role)
        with self.engine.begin() as c:
            c.execute(update(logins_table).where(logins_table.c.role == role).values(dropped=True))
        self._audit("database.login_revoked", actor, name, {"role": role})

    def logins(self, actor: str, name: str) -> list[dict]:
        self._row(name)
        manager = self.allowed(actor, "MANAGE_DATABASE", urn(name))
        q = select(logins_table).where(logins_table.c.database == name, logins_table.c.dropped.is_(False))
        if not manager:
            q = q.where(logins_table.c.actor == actor)
        with self.engine.connect() as c:
            rows = c.execute(q.order_by(logins_table.c.created_ms)).all()
        return [{"user": r.role, "actor": r.actor, "access": r.access, "expires_ms": r.expires_ms} for r in rows]

    def drop(self, actor: str, name: str) -> None:
        from psycopg import sql

        self._row(name)
        self._need(actor, "MANAGE_DATABASE", urn(name))
        owner, reader = self._roles(name)
        with self.engine.connect() as c:
            roles = [r.role for r in c.execute(select(logins_table.c.role).where(
                logins_table.c.database == name, logins_table.c.dropped.is_(False)))]
        with self._admin() as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,))
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in roles:
                self._drop_login(conn, role)
            for role in (owner, reader):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        with self.engine.begin() as c:
            c.execute(update(logins_table).where(logins_table.c.database == name).values(dropped=True))
            c.execute(databases_table.delete().where(databases_table.c.name == name))
        self._audit("database.dropped", actor, name, {"logins_ended": len(roles)})


def build_databases_router(dbs: Databases, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/databases", tags=["databases"])

    def person(p) -> str:
        if p.via in ("agent",):
            raise HTTPException(403, "database logins are for people and services, not agents")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except DatabaseError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return {"databases": await call(lambda: dbs.list(p.urn))}

    @router.post("", status_code=201)
    async def create(name: str = Body(..., embed=True), p=Depends(current_principal)):
        u = person(p)
        return await call(lambda: dbs.create(u, name))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: dbs.get(p.urn, name))

    @router.post("/{name}/branches", status_code=201)
    async def branch(name: str, new: str = Body(..., embed=True, alias="name"), p=Depends(current_principal)):
        u = person(p)
        return await call(lambda: dbs.branch(u, name, new))

    @router.post("/{name}/credentials", status_code=201)
    async def credentials(name: str, access: str = Body("read", embed=True), ttl_minutes: int = Body(60, embed=True),
                          p=Depends(current_principal)):
        u = person(p)
        return await call(lambda: dbs.credentials(u, name, access, ttl_minutes))

    @router.get("/{name}/logins")
    async def logins(name: str, p=Depends(current_principal)):
        return {"logins": await call(lambda: dbs.logins(p.urn, name))}

    @router.delete("/{name}/logins/{role}", status_code=204)
    async def revoke(name: str, role: str, p=Depends(current_principal)):
        await call(lambda: dbs.revoke(p.urn, name, role))
        return Response(status_code=204)

    @router.delete("/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        u = person(p)
        await call(lambda: dbs.drop(u, name))
        return Response(status_code=204)

    return router
