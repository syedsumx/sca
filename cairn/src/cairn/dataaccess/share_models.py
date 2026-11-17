"""Models in a share: a registered model's version, downloaded by the share's recipient (L169).

A model registered in MLflow through Cairn (``cairn.ml.mlflow_gateway``) belongs to its owner and is governed like
any asset. This adds a version of it to a share, so a partner can load the model itself.

* **Attaching.** ``POST /v1/shares/<share>/models`` with ``{"model": "churn", "version": "3"}``, by the share's maker
  or an operator, who must be allowed to manage the model (its owner, ``MANAGE_ML_MODEL`` on it, or an operator).
  A version, not the model: what is shared never changes under the recipient, and a new version is shared by
  attaching it. ``DELETE /v1/shares/<share>/models/<model>`` (``?version=``) takes it out; ``GET`` lists them.
* **The recipient** uses the share's own credential: ``GET /v1/shares/recipient/models`` lists the shared versions;
  ``.../models/<model>/versions/<version>`` describes one (description, tags, its files with sizes), and
  ``.../versions/<version>/files/<path>`` downloads a file (``MLmodel``, the weights, ``requirements.txt``, ...).
  The folder, saved as it is, loads with ``mlflow.pyfunc.load_model``.
* **What the recipient does not get.** The run that trained the model, its experiment, its metrics and data, other
  versions and other models: none are reachable with a share's credential. Only the version's own files are, read by
  Cairn from MLflow's artifact service (MLflow must serve artifacts: ``--serve-artifacts``; a registry that hands
  out storage paths instead is refused rather than opened).
* **Ending.** The share is checked on every call: revoked, expired or detached, the next request is refused. A
  deleted version answers as one that was never shared.

Every download is audited as the recipient (``share:<id>``) and counted in the share's usage (``model:<name>``).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote

from sqlalchemy import BigInteger, Column, PrimaryKeyConstraint, String, Table, and_, delete, insert, select
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata
from cairn.dataaccess.shares import PREFIX, ShareError

logger = logging.getLogger(__name__)

MAX_PER_SHARE = 50
MAX_FILES = 2000
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,255}$")
VERSION = re.compile(r"^[1-9][0-9]{0,8}$")
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._ +=@-]{0,254}$")
SCHEME = "mlflow-artifacts:"

share_models = Table(
    "cairn_share_models", metadata,
    Column("share_id", String(32), nullable=False, index=True),
    Column("model", String(256), nullable=False),
    Column("version", String(16), nullable=False),
    Column("attached_by", String(256), nullable=False),
    Column("attached_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("share_id", "model", "version"),
)


def relative(path: str) -> str:
    """A file's path inside a model's folder: plain segments, no ``.``, ``..`` or empty ones."""
    parts = str(path or "").split("/")
    if not path or len(path) > 1024 or any(not SEGMENT.match(p) or p in (".", "..") for p in parts):
        raise ShareError("no such file", 404)
    return path


class ShareModels:
    def __init__(self, engine, shares, upstream: str, may_manage: Callable[[Any, str], bool],
                 recipient: Callable[[str], dict], audit=None, http=None):
        """``upstream``: MLflow's address; ``may_manage(principal, model)``: Cairn's decision on the registry
        (``MlflowGateway.may_manage``); ``recipient(authorization)``: the active share behind a credential."""
        import httpx

        self.engine, self.shares, self.upstream = engine, shares, upstream.rstrip("/")
        self.may_manage, self.recipient, self.audit = may_manage, recipient, audit
        self.http = http or httpx.Client(timeout=60)

    # ---- MLflow ------------------------------------------------------------------------------------------
    def _get(self, api: str, **params) -> dict:
        import httpx

        try:
            r = self.http.get(f"{self.upstream}/api/2.0/{api}", params=params)
        except httpx.HTTPError as e:
            raise ShareError(f"the model registry is not reachable: {type(e).__name__}", 502)
        if r.status_code == 404 or (r.status_code == 400 and "RESOURCE_DOES_NOT_EXIST" in r.text):
            raise ShareError("no such model version", 404)
        if r.status_code != 200:
            raise ShareError(f"the model registry answered {r.status_code}", 502)
        return r.json()

    def _version(self, model: str, version: str) -> dict:
        return self._get("mlflow/model-versions/get", name=model, version=version).get("model_version") or {}

    def _root(self, model: str, version: str) -> str:
        """Where the version's files are in MLflow's artifact service (a path under ``mlflow-artifacts:``)."""
        uri = str(self._get("mlflow/model-versions/get-download-uri", name=model, version=version).get("artifact_uri") or "")
        if not uri.startswith(SCHEME):
            raise ShareError("this registry does not serve its artifacts through MLflow (--serve-artifacts): "
                             "models cannot be shared from it", 409)
        root = uri[len(SCHEME):].lstrip("/").rstrip("/")
        if not root or any(p in ("", ".", "..") for p in root.split("/")):
            raise ShareError("the model registry gave no usable location for the version", 502)
        return root

    def _files(self, root: str) -> list[dict]:
        out: list[dict] = []
        todo = [""]
        while todo:
            sub = todo.pop()
            got = self._get("mlflow-artifacts/artifacts", path=f"{root}/{sub}".rstrip("/"))
            for f in got.get("files") or []:
                name = str(f.get("path") or "").rsplit("/", 1)[-1]
                if not SEGMENT.match(name) or name in (".", ".."):
                    continue                              # never offered: it could not be asked for either
                rel = f"{sub}/{name}".lstrip("/")
                if f.get("is_dir"):
                    todo.append(rel)
                else:
                    out.append({"path": rel, "size": int(f.get("file_size") or 0)})
                if len(out) > MAX_FILES:
                    raise ShareError(f"the version has over {MAX_FILES} files", 409)
        return sorted(out, key=lambda f: f["path"])

    # ---- the provider's side -----------------------------------------------------------------------------
    def _attached(self, share_id: str, model: Optional[str] = None) -> list[dict]:
        where = share_models.c.share_id == share_id
        if model is not None:
            where = and_(where, share_models.c.model == model)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(share_models).where(where).order_by(share_models.c.model, share_models.c.version))]

    def list(self, actor: str, everyone: bool, ref: str) -> dict:
        row = self.shares._find(ref, actor, everyone)
        return {"share": row["name"], "models": [
            {"model": r["model"], "version": r["version"], "attached_by": r["attached_by"],
             "attached_ms": r["attached_ms"]} for r in self._attached(row["id"])]}

    def attach(self, p, everyone: bool, ref: str, model: str, version: str) -> dict:
        row = self.shares._find(ref, p.urn, everyone)
        if not self.shares._active(row):
            raise ShareError("the share has ended", 409)
        model, version = str(model or ""), str(version or "")
        if not MODEL.match(model) or not VERSION.match(version):
            raise ShareError("model: a registered model's name; version: its number")
        if not self.may_manage(p, model):
            raise ShareError(f"no model {model}", 404)       # one the person may not manage: not told it exists
        self._version(model, version)
        self._root(model, version)                           # shareable at all (served artifacts), said now
        have = self._attached(row["id"])
        if any(r["model"] == model and r["version"] == version for r in have):
            raise ShareError(f"{model} version {version} is in the share already", 409)
        if len(have) >= MAX_PER_SHARE:
            raise ShareError(f"a share carries at most {MAX_PER_SHARE} model versions")
        with self.engine.begin() as conn:
            conn.execute(insert(share_models).values(share_id=row["id"], model=model, version=version,
                                                     attached_by=p.urn, attached_ms=int(time.time() * 1000)))
            if self.audit is not None:
                self.audit.record("shares", "share.model_attached", p.urn, None,
                                  {"share": row["id"], "name": row["name"], "recipient": row["recipient"],
                                   "model": model, "version": version}, conn=conn)
        return self.list(p.urn, everyone, ref)

    def detach(self, actor: str, everyone: bool, ref: str, model: str, version: Optional[str] = None) -> dict:
        """Takes a model's version out of the share; with no ``version``, every version of that model."""
        row = self.shares._find(ref, actor, everyone)
        where = and_(share_models.c.share_id == row["id"], share_models.c.model == model)
        if version is not None:
            where = and_(where, share_models.c.version == str(version))
        with self.engine.begin() as conn:
            if not conn.execute(delete(share_models).where(where)).rowcount:
                raise ShareError(f"{model} is not in the share", 404)
            if self.audit is not None:
                self.audit.record("shares", "share.model_detached", actor, None,
                                  {"share": row["id"], "name": row["name"], "model": model, "version": version}, conn=conn)
        return self.list(actor, everyone, ref)

    # ---- the recipient's side ----------------------------------------------------------------------------
    def _shared(self, row: dict, model: str, version: str) -> None:
        if not any(r["version"] == str(version) for r in self._attached(row["id"], model)):
            self.shares._count(row["id"], f"model:{model}"[:512], refusals=1)
            raise ShareError("no such model version in the share", 404)

    def recipient_models(self, row: dict) -> list[dict]:
        self.shares._count(row["id"], "", requests=1)
        return [{"model": r["model"], "version": r["version"],
                 "describe": f"/v1/shares/recipient/models/{quote(r['model'])}/versions/{r['version']}"}
                for r in self._attached(row["id"])]

    def recipient_version(self, row: dict, model: str, version: str) -> dict:
        """The shared version: what it is and its files. Nothing of the run or experiment behind it."""
        self.shares._count(row["id"], "", requests=1)
        self._shared(row, model, version)
        v = self._version(model, version)
        base = f"/v1/shares/recipient/models/{quote(model)}/versions/{version}/files"
        return {"model": model, "version": str(version), "description": v.get("description") or "",
                "created_ms": int(v.get("creation_timestamp") or 0),
                "tags": {t["key"]: t.get("value", "") for t in v.get("tags") or [] if not str(t.get("key", "")).startswith("mlflow.")},
                "files": [{**f, "download": f"{base}/{quote(f['path'])}"} for f in self._files(self._root(model, version))]}

    def recipient_read(self, row: dict, model: str, version: str, path: str) -> tuple[dict, Iterator[bytes]]:
        import httpx

        self.shares._count(row["id"], "", requests=1)
        self._shared(row, model, version)
        rel = relative(path)
        root = self._root(model, version)
        # Only a file the version's own listing names is fetched (MLflow answers a missing one with an error).
        parent, _, name = rel.rpartition("/")
        listed = self._get("mlflow-artifacts/artifacts", path=f"{root}/{parent}".rstrip("/")).get("files") or []
        if not any(str(f.get("path") or "").rsplit("/", 1)[-1] == name and not f.get("is_dir") for f in listed):
            raise ShareError("no such file", 404)
        url = f"{self.upstream}/api/2.0/mlflow-artifacts/artifacts/{quote(root)}/{quote(rel)}"
        try:
            r = self.http.send(self.http.build_request("GET", url), stream=True)
        except httpx.HTTPError as e:
            raise ShareError(f"the model registry is not reachable: {type(e).__name__}", 502)
        if r.status_code != 200:
            r.close()
            raise ShareError("no such file", 404) if r.status_code in (400, 404) else \
                ShareError(f"the model registry answered {r.status_code}", 502)
        self.shares._count(row["id"], f"model:{model}"[:512], loads=1)
        if self.audit is not None:
            self.audit.record("shares", "share.model_file_read", f"urn:li:corpuser:{PREFIX}{row['id']}", f"model:{model}",
                              {"share": row["id"], "name": row["name"], "recipient": row["recipient"],
                               "version": str(version), "path": rel})

        def body():
            try:
                yield from r.iter_bytes(1 << 20)
            finally:
                r.close()
        return {"size": r.headers.get("content-length")}, body()


def build_share_models_router(sm: ShareModels, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import StreamingResponse

    router = APIRouter(prefix="/v1/shares", tags=["shares"])

    def operator(p) -> bool:
        return bool(authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "shares are managed by people")
        return p

    def call(fn, *a):
        try:
            return fn(*a)
        except ShareError as e:
            raise HTTPException(e.status, str(e))

    def recipient(request: Request) -> dict:
        try:
            return sm.recipient(request.headers.get("authorization", ""))
        except ShareError as e:
            headers = {"WWW-Authenticate": 'Bearer realm="shares"'} if e.status == 401 else None
            raise HTTPException(e.status, str(e), headers=headers)

    # The recipient's endpoints: authenticated by the share's own credential, never a person's session.
    @router.get("/recipient/models")
    def recipient_models(request: Request):
        return {"models": call(sm.recipient_models, recipient(request))}

    @router.get("/recipient/models/{model}/versions/{version}")
    def recipient_version(model: str, version: str, request: Request):
        return call(sm.recipient_version, recipient(request), model, version)

    @router.get("/recipient/models/{model}/versions/{version}/files/{path:path}")
    def recipient_read(model: str, version: str, path: str, request: Request):
        meta, body = call(sm.recipient_read, recipient(request), model, version, path)
        headers = {"content-disposition": "attachment", "content-security-policy": "sandbox; default-src 'none'"}
        if meta["size"]:
            headers["content-length"] = str(meta["size"])
        return StreamingResponse(body, media_type="application/octet-stream", headers=headers)

    @router.get("/{ref}/models")
    def list_(ref: str, p=Depends(current_principal)):
        return call(sm.list, p.urn, operator(p), ref)

    @router.post("/{ref}/models")
    def attach(ref: str, model: str = Body(...), version: int | str = Body(...), p=Depends(current_principal)):
        """Adds a registered model's version to the share. Needs the share (its maker, or an operator) and the
        model (its owner, MANAGE_ML_MODEL on it, or an operator)."""
        p = person(p)
        return call(sm.attach, p, operator(p), ref, model, str(version))

    @router.delete("/{ref}/models/{model}")
    def detach(ref: str, model: str, version: Optional[str] = None, p=Depends(current_principal)):
        p = person(p)
        return call(sm.detach, p.urn, operator(p), ref, model, version)

    return router
