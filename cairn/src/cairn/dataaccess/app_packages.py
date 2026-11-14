"""App packages: data apps one team publishes and others install, on their own data, with their consent.

**Publishing.** A provider registers a package from git (``POST /v1/app-packages`` ``{"name", "git": {url | repo,
branch, path}}``; ``CREATE_APP_PACKAGE``) and releases versions of it (``POST /v1/app-packages/<name>/versions``
``{"version": "1.2.0"}``; ``MANAGE_APP_PACKAGE`` on ``urn:li:appPackage:<name>``). A version is the branch's commit
at that moment and the manifest found there, ``cairn-app.json`` in the package's folder::

    {"title": "Churn explorer", "command": ["streamlit", "run", "app.py", "--server.port", "{port}", ...],
     "references": [{"name": "customers", "privilege": "VIEW_DATASET_PROFILE",
                     "description": "one row per customer, with its plan and signup date"}]}

A version never changes: a fix is a new version.

**Installing.** A consumer (``USE_APP_PACKAGE`` on the package) installs a version under a name of their own
(``POST /v1/app-installs`` ``{"name", "package", "version", "references": {"customers": "<dataset urn>"}}``) and
binds each reference the manifest declares to one of their own datasets. Binding is consent: the installer must
hold the reference's privilege on that dataset themselves. The app is then a data app pinned to the version's
commit. It is opened like any data app (``USE_APP`` on ``urn:li:dataApp:<name>``; the installer gets
``MANAGE_DATA_APP`` through the policies that name it), and it runs with a minimal environment: nothing of
Cairn's.

**What the app may read.** Besides the viewer's own token (``X-Cairn-Trino-Token``), each request carries
``X-Cairn-App-Trino-Token``: a token for the install's own identity, ``cairnapp-<name>``. Trino lets that identity
read the bound datasets and nothing else: no other table, no writes, no AI or stored functions. Its reads are
decided as the installer's, so the installer's masks and row filters apply. Unbinding happens by uninstalling or
by rebinding with an upgrade, and both take effect on the app's next query.

**Upgrading.** ``POST /v1/app-installs/<name>/upgrade`` ``{"version", "references"}`` moves an install to another
version (a later one, or back) and redeploys it. References the new version adds must be bound then.
``DELETE /v1/app-installs/<name>`` removes it. Every publish, install, upgrade and uninstall is audited.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

packages_table = Table(
    "cairn_app_packages", metadata,
    Column("name", String(64), primary_key=True),
    Column("git", Text, nullable=False),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)
versions_table = Table(
    "cairn_app_package_versions", metadata,
    Column("package", String(64), primary_key=True),
    Column("version", String(32), primary_key=True),
    Column("commit", String(64), nullable=False),
    Column("manifest", Text, nullable=False),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)
installs_table = Table(
    "cairn_app_installs", metadata,
    Column("name", String(64), primary_key=True),
    Column("package", String(64), nullable=False, index=True),
    Column("version", String(32), nullable=False),
    Column("references", Text, nullable=False),          # {reference: dataset urn}
    Column("installed_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}(?:-[0-9a-z.]{1,20})?$")
REF = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
IDENTITY_PREFIX = "cairnapp-"
READ_PRIVILEGES = {"VIEW_DATASET_PROFILE"}
MANIFEST = "cairn-app.json"


class PackageError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def package_urn(name: str) -> str:
    return f"urn:li:appPackage:{name}"


def check_manifest(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise PackageError(f"{MANIFEST} is a JSON object")
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        raise PackageError(f"{MANIFEST}: command is a list of strings, the program first")
    refs = raw.get("references") or []
    if not isinstance(refs, list):
        raise PackageError(f"{MANIFEST}: references is a list")
    seen = set()
    out_refs = []
    for r in refs:
        if not isinstance(r, dict) or not REF.match(str(r.get("name") or "")) or r["name"] in seen:
            raise PackageError(f"{MANIFEST}: each reference has a unique name (lowercase letters, digits, '_')")
        priv = str(r.get("privilege") or "VIEW_DATASET_PROFILE")
        if priv not in READ_PRIVILEGES:
            raise PackageError(f"{MANIFEST}: reference {r['name']}: an app may only ask to read "
                               f"({', '.join(sorted(READ_PRIVILEGES))})")
        seen.add(r["name"])
        out_refs.append({"name": r["name"], "privilege": priv, "description": str(r.get("description") or "")[:500]})
    idle = raw.get("idle_minutes", 30)
    if isinstance(idle, bool) or not isinstance(idle, int) or not 1 <= idle <= 1440:
        raise PackageError(f"{MANIFEST}: idle_minutes is 1-1440")
    return {"title": str(raw.get("title") or "")[:256], "command": command, "references": out_refs,
            "idle_minutes": idle}


class AppPackages:
    """``apps``: DataApps; ``allowed(actor, privilege, urn)``: the PDP; ``mint(identity_urn)``: a Trino token."""

    def __init__(self, engine, apps, allowed: Callable[[str, str, Optional[str]], bool], audit=None,
                 mint: Optional[Callable[[str], Optional[str]]] = None, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.engine, self.apps, self.allowed, self.audit, self.mint, self.clock = engine, apps, allowed, audit, mint, clock
        apps.isolated = self.is_install
        apps.app_token = self.app_token

    # ---- helpers ------------------------------------------------------------------------------
    def _need(self, actor: str, privilege: str, target: Optional[str]) -> None:
        try:
            ok = self.allowed(actor, privilege, target)
        except Exception:  # noqa: BLE001 - fail closed
            ok = False
        if not ok:
            raise PackageError(f"{privilege} is needed" + (f" on {target}" if target else ""), 403)

    def _audit(self, action: str, actor: str, target: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("apps", action, actor, target, details)

    def _package(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(packages_table).where(packages_table.c.name == name)).first()
        if row is None:
            raise PackageError("no such app package", 404)
        return row

    def _version(self, package: str, version: str):
        with self.engine.connect() as c:
            row = c.execute(select(versions_table).where(versions_table.c.package == package,
                                                         versions_table.c.version == version)).first()
        if row is None:
            raise PackageError(f"{package} has no version {version}", 404)
        return row

    def _install(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(installs_table).where(installs_table.c.name == name)).first()
        if row is None:
            raise PackageError("no such install", 404)
        return row

    # ---- publishing -------------------------------------------------------------------------------
    def create_package(self, actor: str, name: str, git: dict) -> dict:
        from cairn.dataaccess.dataapps import AppError

        if not NAME.match(name or ""):
            raise PackageError("name: lowercase letters, digits and '-'")
        self._need(actor, "CREATE_APP_PACKAGE", None)
        try:
            checked = json.loads(self.apps._check_git({k: v for k, v in (git or {}).items() if k != "commit"}) or "null")
        except AppError as e:
            raise PackageError(str(e), e.status)
        if not checked:
            raise PackageError("git: where the package's code is")
        with self.engine.begin() as c:
            if c.execute(select(packages_table.c.name).where(packages_table.c.name == name)).first():
                raise PackageError(f"{name} exists", 409)
            c.execute(insert(packages_table).values(name=name, git=json.dumps(checked), created_by=actor,
                                                    created_ms=self.clock()))
        self._audit("app_package.created", actor, package_urn(name), {"git": {k: v for k, v in checked.items() if v}})
        return self.get_package(actor, name)

    def _read_manifest(self, git: dict) -> tuple[str, dict]:
        """(the branch's commit, the manifest there), read from a fresh clone."""
        from cairn.dataaccess.dataapps import AppError

        url, header = self.apps._remote(git)
        d = tempfile.mkdtemp(prefix="cairn-pkg-", dir=self.apps.workdir if os.path.isdir(self.apps.workdir) else None)
        try:
            self.apps._git(None, "clone", "--quiet", "--no-tags", "--depth", "1", "--branch", git["branch"], "--", url, d,
                           header=header)
            commit = self.apps._git(d, "rev-parse", "HEAD").strip()
            path = os.path.join(d, git.get("path") or "", MANIFEST)
            if not os.path.isfile(path):
                raise PackageError(f"no {MANIFEST} in {git.get('path') or 'the repository root'}", 422)
            with open(path) as fh:
                try:
                    raw = json.load(fh)
                except ValueError as e:
                    raise PackageError(f"{MANIFEST} is not JSON: {e}", 422)
            return commit, check_manifest(raw)
        except AppError as e:
            raise PackageError(str(e), e.status)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def release(self, actor: str, package: str, version: str) -> dict:
        row = self._package(package)
        self._need(actor, "MANAGE_APP_PACKAGE", package_urn(package))
        if not VERSION.match(version or ""):
            raise PackageError("version: major.minor.patch (and an optional -suffix)")
        with self.engine.connect() as c:
            if c.execute(select(versions_table.c.version).where(versions_table.c.package == package,
                                                                versions_table.c.version == version)).first():
                raise PackageError(f"{package} {version} exists: a version never changes", 409)
        commit, manifest = self._read_manifest(json.loads(row.git))
        with self.engine.begin() as c:
            c.execute(insert(versions_table).values(package=package, version=version, commit=commit,
                                                    manifest=json.dumps(manifest), created_by=actor, created_ms=self.clock()))
        self._audit("app_package.released", actor, package_urn(package), {"version": version, "commit": commit})
        return {"package": package, "version": version, "commit": commit, "manifest": manifest}

    def get_package(self, actor: str, name: str) -> dict:
        row = self._package(name)
        if not any(self.allowed(actor, p, package_urn(name)) for p in ("USE_APP_PACKAGE", "MANAGE_APP_PACKAGE")):
            raise PackageError("no such app package", 404)
        with self.engine.connect() as c:
            versions = c.execute(select(versions_table).where(versions_table.c.package == name)
                                 .order_by(versions_table.c.created_ms)).all()
        return {"name": row.name, "urn": package_urn(row.name), "created_by": row.created_by,
                "versions": [{"version": v.version, "commit": v.commit, "manifest": json.loads(v.manifest),
                              "created_ms": v.created_ms} for v in versions]}

    # ---- installing -----------------------------------------------------------------------------
    def _bind(self, actor: str, manifest: dict, given: dict) -> dict:
        if not isinstance(given, dict):
            raise PackageError("references: {reference: dataset urn}")
        declared = {r["name"]: r for r in manifest["references"]}
        unknown = sorted(set(given) - set(declared))
        if unknown:
            raise PackageError(f"the app does not ask for {', '.join(unknown)}")
        missing = sorted(set(declared) - set(given))
        if missing:
            raise PackageError(f"bind {', '.join(missing)} to datasets of yours")
        out = {}
        for ref, urn in given.items():
            if not isinstance(urn, str) or not urn.startswith("urn:li:dataset:"):
                raise PackageError(f"{ref}: a dataset urn")
            # Consent: you may only hand the app what you may read yourself.
            if not self.allowed(actor, declared[ref]["privilege"], urn):
                raise PackageError(f"{ref}: you may not grant what you cannot read ({declared[ref]['privilege']} "
                                   f"on {urn})", 403)
            out[ref] = urn
        return out

    def _app_body(self, package_row, version_row, name: str) -> dict:
        manifest = json.loads(version_row.manifest)
        git = json.loads(package_row.git)
        return {"name": name, "title": manifest["title"] or name, "command": manifest["command"],
                "idle_minutes": manifest["idle_minutes"], "runtime": "local",
                "git": {k: v for k, v in {**git, "commit": version_row.commit}.items() if v is not None}}

    async def install(self, actor: str, body: dict) -> dict:
        name, package, version = str(body.get("name") or ""), str(body.get("package") or ""), str(body.get("version") or "")
        if not NAME.match(name):
            raise PackageError("name: lowercase letters, digits and '-'")
        prow = self._package(package)
        self._need(actor, "USE_APP_PACKAGE", package_urn(package))
        vrow = self._version(package, version)
        refs = self._bind(actor, json.loads(vrow.manifest), body.get("references") or {})
        from cairn.dataaccess.dataapps import AppError

        try:
            self.apps.get(name)
            raise PackageError(f"an app named {name} exists", 409)
        except AppError as e:
            if e.status != 404:
                raise PackageError(str(e), e.status)
        # The install row first: while it exists the app runs isolated (none of Cairn's environment), so the app is
        # never, even for a moment or after a failure, an ordinary data app running another party's code.
        now = self.clock()
        with self.engine.begin() as c:
            c.execute(insert(installs_table).values(name=name, package=package, version=version,
                                                    references=json.dumps(refs), installed_by=actor,
                                                    created_ms=now, updated_ms=now))
        try:
            self.apps.create(actor, self._app_body(prow, vrow, name))
        except AppError as e:
            with self.engine.begin() as c:
                c.execute(installs_table.delete().where(installs_table.c.name == name))
            raise PackageError(str(e), e.status)
        try:
            await self.apps.deploy(actor, name)
        except AppError as e:
            raise PackageError(f"installed, but the deploy failed: {e}", e.status)
        self._audit("app.installed", actor, self.apps.urn(name),
                    {"package": package, "version": version, "references": refs})
        return self.get_install(actor, name)

    async def upgrade(self, actor: str, name: str, version: str, references: Optional[dict] = None) -> dict:
        irow = self._install(name)
        self._need_manage(actor, irow)
        prow, vrow = self._package(irow.package), self._version(irow.package, version)
        self._need(actor, "USE_APP_PACKAGE", package_urn(irow.package))
        manifest = json.loads(vrow.manifest)
        declared = {r["name"] for r in manifest["references"]}
        # Bindings the target version does not ask for are dropped (a rollback unbinds what it no longer uses).
        kept = {k: v for k, v in json.loads(irow.references).items() if k in declared}
        # The app reads as its installer, so bindings are the installer's consent: only they bind data to it. A
        # manager may move it between versions within what the installer bound, re-checked as the installer.
        if actor != irow.installed_by and (references or set(declared) - set(kept)):
            raise PackageError("only the installer binds data to an app: this version needs bindings "
                               f"({', '.join(sorted(set(references or {}) | (set(declared) - set(kept))))})", 403)
        refs = self._bind(irow.installed_by, manifest, {**kept, **(references or {})})
        from cairn.dataaccess.dataapps import AppError

        body = self._app_body(prow, vrow, name)
        try:
            self.apps.update(actor, name, body)
            await self.apps.deploy(actor, name)
        except AppError as e:
            raise PackageError(str(e), e.status)
        with self.engine.begin() as c:
            c.execute(update(installs_table).where(installs_table.c.name == name).values(
                version=version, references=json.dumps(refs), updated_ms=self.clock()))
        self._audit("app.upgraded", actor, self.apps.urn(name), {"from": irow.version, "to": version, "references": refs})
        return self.get_install(actor, name)

    def _need_manage(self, actor: str, irow) -> None:
        if irow.installed_by != actor:
            self._need(actor, "MANAGE_DATA_APP", self.apps.urn(irow.name))

    async def uninstall(self, actor: str, name: str) -> None:
        irow = self._install(name)
        self._need_manage(actor, irow)
        from cairn.dataaccess.dataapps import AppError

        # Unbound first: the app's identity reads nothing from now. The row itself goes last: while the app exists it
        # must stay isolated, so a delete that fails leaves an isolated app with nothing bound, to be removed again.
        with self.engine.begin() as c:
            c.execute(update(installs_table).where(installs_table.c.name == name).values(
                references=json.dumps({}), updated_ms=self.clock()))
        try:
            await self.apps.delete(actor, name)
        except AppError as e:
            if e.status != 404:
                raise PackageError(f"unbound, but the app could not be removed (try again): {e}", e.status)
        with self.engine.begin() as c:
            c.execute(installs_table.delete().where(installs_table.c.name == name))
        self._audit("app.uninstalled", actor, self.apps.urn(name), {"package": irow.package, "version": irow.version})

    def _may_see(self, actor: str, irow) -> bool:
        """Its installer, and whoever may open or manage the app; to anyone else an install does not exist."""
        if irow.installed_by == actor:
            return True
        try:
            return any(self.allowed(actor, p, self.apps.urn(irow.name)) for p in ("USE_APP", "MANAGE_DATA_APP"))
        except Exception:  # noqa: BLE001 - fail closed
            return False

    def get_install(self, actor: str, name: str) -> dict:
        irow = self._install(name)
        if not self._may_see(actor, irow):
            raise PackageError("no such install", 404)
        app = self.apps.get(name)
        return {"name": name, "package": irow.package, "version": irow.version,
                "references": json.loads(irow.references), "installed_by": irow.installed_by,
                "commit": app.get("commit"), "identity": f"urn:li:corpuser:{IDENTITY_PREFIX}{name}",
                "created_ms": irow.created_ms, "updated_ms": irow.updated_ms}

    def installs(self, actor: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(installs_table).order_by(installs_table.c.name)).all()
        return [{"name": r.name, "package": r.package, "version": r.version, "installed_by": r.installed_by}
                for r in rows if self._may_see(actor, r)]

    # ---- what the app's own identity may read ---------------------------------------------------------
    def is_install(self, name: str) -> bool:
        with self.engine.connect() as c:
            return c.execute(select(installs_table.c.name).where(installs_table.c.name == name)).first() is not None

    def identity(self, user: str) -> Optional[dict]:
        """For a Trino user ``cairnapp-<install>``: {actor: the installer, tables: the bound dataset urns}."""
        if not user.startswith(IDENTITY_PREFIX):
            return None
        name = user.removeprefix(IDENTITY_PREFIX)
        with self.engine.connect() as c:
            row = c.execute(select(installs_table).where(installs_table.c.name == name)).first()
        if row is None:
            return {"actor": None, "tables": set()}      # uninstalled: nobody, nothing
        return {"actor": row.installed_by, "tables": set(json.loads(row.references).values())}

    def app_token(self, name: str) -> Optional[str]:
        if self.mint is None or not self.is_install(name):
            return None
        return self.mint(f"urn:li:corpuser:{IDENTITY_PREFIX}{name}")


def build_app_packages_router(pk: AppPackages, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1", tags=["apps"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except PackageError as e:
            raise HTTPException(e.status, str(e))

    async def acall(coro):
        try:
            return await coro
        except PackageError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/app-packages", status_code=201)
    async def create(name: str = Body(..., embed=True), git: dict = Body(..., embed=True), p=Depends(current_principal)):
        return await call(lambda: pk.create_package(p.urn, name, git))

    @router.get("/app-packages/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: pk.get_package(p.urn, name))

    @router.post("/app-packages/{name}/versions", status_code=201)
    async def release(name: str, version: str = Body(..., embed=True), p=Depends(current_principal)):
        return await call(lambda: pk.release(p.urn, name, version))

    @router.get("/app-installs")
    async def installs(p=Depends(current_principal)):
        return {"installs": await call(lambda: pk.installs(p.urn))}

    @router.post("/app-installs", status_code=201)
    async def install(body: dict = Body(...), p=Depends(current_principal)):
        return await acall(pk.install(p.urn, body))

    @router.get("/app-installs/{name}")
    async def get_install(name: str, p=Depends(current_principal)):
        return await call(lambda: pk.get_install(p.urn, name))

    @router.post("/app-installs/{name}/upgrade")
    async def upgrade(name: str, version: str = Body(..., embed=True), references: Optional[dict] = Body(None, embed=True),
                      p=Depends(current_principal)):
        return await acall(pk.upgrade(p.urn, name, version, references))

    @router.delete("/app-installs/{name}", status_code=204)
    async def uninstall(name: str, p=Depends(current_principal)):
        await acall(pk.uninstall(p.urn, name))
        return Response(status_code=204)

    return router
