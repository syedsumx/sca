"""SCIM 2.0 provisioning (RFC 7643/7644): the identity provider creates, updates and deactivates
people and groups in Cairn.

Resources map onto the catalog's own identity entities, so SSO, policies and ownership all see them:

    User   id = userName → urn:li:corpuser:<userName>  (corpUserInfo, corpUserStatus, groupMembership)
    Group  id = slug(displayName) → urn:li:corpGroup:<id>  (corpGroupInfo, origin)

Group membership is stored on each member (``groupMembership``), as SSO sign-in does, so a SCIM
group and the same group from the identity provider's token are one group. Provisioning metadata
(externalId, created/modified, version) lives in ``cairn_scim_resources``. Deleting a user
deactivates it (``active: false``, status SUSPENDED); its ownership and history stay intact.
"""

import hmac
import os
import re
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, and_, select
from sqlalchemy.engine import Engine

from cairn.common.db import metadata, transaction
from cairn.common.proposals import make_mcp, now_ms

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
ACTOR = "urn:li:corpuser:__datahub_system"
_ID = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")

scim_resources = Table(
    "cairn_scim_resources", metadata,
    Column("resource_type", String(8), primary_key=True),
    Column("id", String(255), primary_key=True),
    Column("external_id", String(512), nullable=True, index=True),
    Column("display", String(512), nullable=True),
    Column("created_ms", BigInteger, nullable=False),
    Column("modified_ms", BigInteger, nullable=False),
    Column("version", Integer, nullable=False, default=1),
)


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: Optional[str] = None):
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type

    def body(self) -> dict:
        out = {"schemas": [ERROR_SCHEMA], "status": str(self.status), "detail": self.detail}
        if self.scim_type:
            out["scimType"] = self.scim_type
        return out


def group_id(display_name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", display_name.strip().lower()).strip("-")
    if not slug:
        raise ScimError(400, "displayName is required", "invalidValue")
    return slug[:128]


def _user_urn(uid: str) -> str:
    return f"urn:li:corpuser:{uid}"


def _group_urn(gid: str) -> str:
    return f"urn:li:corpGroup:{gid}"


_FILTER = re.compile(r'^\s*(\w+(?:\.\w+)?)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.I)


def parse_filter(expr: Optional[str]) -> Optional[tuple[str, str]]:
    """Only ``attr eq "value"``: what identity providers send to look a resource up."""
    if not expr:
        return None
    m = _FILTER.match(expr)
    if not m:
        raise ScimError(400, f"unsupported filter: {expr}", "invalidFilter")
    return m.group(1).lower(), m.group(2).replace('\\"', '"')


class ScimService:
    def __init__(self, engine: Engine, graph, audit=None, base_url: str = "/scim/v2",
                 on_change: Optional[Callable[[str], Any]] = None):
        self.engine, self.graph, self.audit = engine, graph, audit
        self.base_url = base_url.rstrip("/")
        self.on_change = on_change

    # ---- bookkeeping ----------------------------------------------------------------
    def _row(self, rtype: str, rid: str) -> Optional[dict]:
        with self.engine.connect() as c:
            r = c.execute(select(scim_resources).where(and_(scim_resources.c.resource_type == rtype,
                                                            scim_resources.c.id == rid))).mappings().first()
        return dict(r) if r else None

    def _touch(self, rtype: str, rid: str, external_id: Optional[str], display: Optional[str]) -> dict:
        now = now_ms()
        with transaction(self.engine) as c:
            cur = c.execute(select(scim_resources).where(and_(scim_resources.c.resource_type == rtype,
                                                              scim_resources.c.id == rid))).mappings().first()
            if cur is None:
                row = {"resource_type": rtype, "id": rid, "external_id": external_id, "display": display,
                       "created_ms": now, "modified_ms": now, "version": 1}
                c.execute(scim_resources.insert().values(**row))
                return row
            row = {"external_id": external_id if external_id is not None else cur["external_id"],
                   "display": display or cur["display"], "modified_ms": now, "version": cur["version"] + 1}
            c.execute(scim_resources.update().where(and_(scim_resources.c.resource_type == rtype,
                                                         scim_resources.c.id == rid)).values(**row))
            return {**dict(cur), **row}

    def _rows(self, rtype: str, attr: Optional[str] = None, value: Optional[str] = None) -> list[dict]:
        q = select(scim_resources).where(scim_resources.c.resource_type == rtype)
        if attr == "externalid":
            q = q.where(scim_resources.c.external_id == value)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q.order_by(scim_resources.c.id)).mappings()]

    def _meta(self, rtype: str, rid: str, row: Optional[dict]) -> dict:
        row = row or {}
        iso = lambda ms: __import__("datetime").datetime.fromtimestamp(ms / 1000, __import__("datetime").timezone.utc) \
            .isoformat().replace("+00:00", "Z") if ms else None  # noqa: E731
        return {"resourceType": rtype, "location": f"{self.base_url}/{rtype}s/{rid}",
                "created": iso(row.get("created_ms")), "lastModified": iso(row.get("modified_ms")),
                "version": f'W/"{row.get("version", 1)}"'}

    def _record(self, action: str, urn: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("scim", action, "scim", urn, details)
        if self.on_change:
            self.on_change(urn)

    # ---- users ----------------------------------------------------------------------
    def _user_doc(self, uid: str, row: Optional[dict] = None) -> Optional[dict]:
        urn = _user_urn(uid)
        info = self.graph.get_aspect(urn, "corpUserInfo")
        if info is None and row is None:
            return None
        info = info or {}
        status = (self.graph.get_aspect(urn, "corpUserStatus") or {}).get("status", "ACTIVE")
        groups = (self.graph.get_aspect(urn, "groupMembership") or {}).get("groups", [])
        row = row or self._row("User", uid)
        doc: dict[str, Any] = {
            "schemas": [USER_SCHEMA], "id": uid, "userName": uid,
            "active": bool(info.get("active", True)) and status != "SUSPENDED",
            "name": {k: v for k, v in (("givenName", info.get("firstName")), ("familyName", info.get("lastName")),
                                        ("formatted", info.get("fullName"))) if v},
            "displayName": info.get("displayName") or info.get("fullName"),
            "title": info.get("title"),
            "emails": [{"value": info["email"], "primary": True, "type": "work"}] if info.get("email") else [],
            "groups": [{"value": g.split(":", 3)[-1], "$ref": f"{self.base_url}/Groups/{g.split(':', 3)[-1]}",
                        "display": g.split(":", 3)[-1]} for g in groups],
            "meta": self._meta("User", uid, row),
        }
        if info.get("departmentName"):
            doc["urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"] = {"department": info["departmentName"]}
        if row and row.get("external_id"):
            doc["externalId"] = row["external_id"]
        return {k: v for k, v in doc.items() if v not in (None, {}, [])} | {"active": doc["active"]}

    def _write_user(self, uid: str, body: dict) -> None:
        name = body.get("name") or {}
        emails = body.get("emails") or []
        email = next((e.get("value") for e in emails if e.get("primary")), None) or (emails[0].get("value")
                                                                                     if emails else None)
        active = bool(body.get("active", True))
        enterprise = body.get("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User") or {}
        full = name.get("formatted") or " ".join(x for x in (name.get("givenName"), name.get("familyName")) if x)
        info = {k: v for k, v in {
            "active": active, "displayName": body.get("displayName") or full or uid, "email": email,
            "firstName": name.get("givenName"), "lastName": name.get("familyName"), "fullName": full or None,
            "title": body.get("title"), "departmentName": enterprise.get("department"),
        }.items() if v is not None}
        urn = _user_urn(uid)
        stamp = {"time": now_ms(), "actor": ACTOR}
        self.graph.emit([make_mcp(urn, "corpUserInfo", info),
                         make_mcp(urn, "corpUserStatus", {"status": "ACTIVE" if active else "SUSPENDED",
                                                          "lastModified": stamp})])

    def create_user(self, body: dict) -> dict:
        uid = (body.get("userName") or "").strip()
        if not _ID.match(uid):
            raise ScimError(400, "userName must be 1-128 characters of letters, digits and ._@+-", "invalidValue")
        if self._row("User", uid) is not None:
            raise ScimError(409, f"user {uid} already exists", "uniqueness")
        self._write_user(uid, body)
        row = self._touch("User", uid, body.get("externalId"), body.get("displayName"))
        self._record("user.create", _user_urn(uid), {"externalId": body.get("externalId")})
        return self._user_doc(uid, row)

    def get_user(self, uid: str) -> dict:
        doc = self._user_doc(uid)
        if doc is None:
            raise ScimError(404, f"user {uid} not found")
        return doc

    def replace_user(self, uid: str, body: dict) -> dict:
        self.get_user(uid)
        if body.get("userName") and body["userName"] != uid:
            raise ScimError(400, "userName cannot change", "mutability")
        self._write_user(uid, body)
        row = self._touch("User", uid, body.get("externalId"), body.get("displayName"))
        self._record("user.replace", _user_urn(uid), {"active": body.get("active", True)})
        return self._user_doc(uid, row)

    def patch_user(self, uid: str, body: dict) -> dict:
        current = self.get_user(uid)
        doc = {k: v for k, v in current.items() if k not in ("meta", "groups", "schemas", "id")}
        for op in _operations(body):
            path, value = op.get("path"), op.get("value")
            kind = (op.get("op") or "").lower()
            if kind not in ("add", "replace", "remove"):
                raise ScimError(400, f"unsupported op {op.get('op')}", "invalidSyntax")
            if not path:
                if not isinstance(value, dict):
                    raise ScimError(400, "value must be an object when path is absent", "invalidValue")
                for k, v in value.items():
                    _set_path(doc, k, v)
            elif kind == "remove":
                _set_path(doc, path, None)
            else:
                _set_path(doc, path, value)
        return self.replace_user(uid, doc)

    def delete_user(self, uid: str) -> None:
        current = self.get_user(uid)
        self._write_user(uid, {**current, "active": False})
        self._touch("User", uid, None, None)
        self._record("user.deactivate", _user_urn(uid), {})

    def list_users(self, flt: Optional[str], start: int, count: int) -> dict:
        parsed = parse_filter(flt)
        if parsed and parsed[0] in ("username", "id"):
            doc = self._user_doc(parsed[1])
            docs = [doc] if doc else []
        else:
            if parsed and parsed[0] != "externalid":
                raise ScimError(400, f"unsupported filter attribute {parsed[0]}", "invalidFilter")
            rows = self._rows("User", *(parsed or (None, None)))
            docs = [d for d in (self._user_doc(r["id"], r) for r in rows) if d]
        return _list(docs, start, count)

    # ---- groups ---------------------------------------------------------------------
    def _members(self, gid: str) -> list[str]:
        urn = _group_urn(gid)
        rows = self.graph.scroll_entities("corpuser", ["groupMembership"])
        return sorted(r["urn"].split(":", 3)[-1] for r in rows if urn in (r.get("groupMembership") or {}).get("groups", []))

    def _set_membership(self, uid: str, gid: str, member: bool) -> None:
        urn = _user_urn(uid)
        if self.graph.get_aspect(urn, "corpUserInfo") is None:
            raise ScimError(400, f"member {uid} is not a provisioned user", "invalidValue")
        groups = list((self.graph.get_aspect(urn, "groupMembership") or {}).get("groups", []))
        g = _group_urn(gid)
        if member and g not in groups:
            groups.append(g)
        elif not member and g in groups:
            groups.remove(g)
        else:
            return
        self.graph.emit([make_mcp(urn, "groupMembership", {"groups": groups})])
        if self.on_change:
            self.on_change(urn)

    def _group_doc(self, gid: str, row: Optional[dict] = None) -> Optional[dict]:
        info = self.graph.get_aspect(_group_urn(gid), "corpGroupInfo")
        if info is None:
            return None
        row = row or self._row("Group", gid)
        doc = {"schemas": [GROUP_SCHEMA], "id": gid, "displayName": info.get("displayName") or gid,
               "members": [{"value": m, "$ref": f"{self.base_url}/Users/{m}", "display": m}
                           for m in self._members(gid)],
               "meta": self._meta("Group", gid, row)}
        if row and row.get("external_id"):
            doc["externalId"] = row["external_id"]
        return doc

    def _write_group(self, gid: str, display: str, description: Optional[str]) -> None:
        urn = _group_urn(gid)
        info = {"displayName": display, "admins": [], "members": [], "groups": []}
        if description:
            info["description"] = description
        self.graph.emit([make_mcp(urn, "corpGroupInfo", info),
                         make_mcp(urn, "origin", {"type": "EXTERNAL", "externalType": "SCIM"})])

    def create_group(self, body: dict) -> dict:
        display = (body.get("displayName") or "").strip()
        gid = group_id(display)
        if self._row("Group", gid) is not None:
            raise ScimError(409, f"group {display} already exists", "uniqueness")
        self._write_group(gid, display, body.get("description"))
        for m in body.get("members") or []:
            self._set_membership(m["value"], gid, True)
        row = self._touch("Group", gid, body.get("externalId"), display)
        self._record("group.create", _group_urn(gid), {"members": len(body.get("members") or [])})
        return self._group_doc(gid, row)

    def get_group(self, gid: str) -> dict:
        doc = self._group_doc(gid)
        if doc is None:
            raise ScimError(404, f"group {gid} not found")
        return doc

    def replace_group(self, gid: str, body: dict) -> dict:
        current = self.get_group(gid)
        display = body.get("displayName") or current["displayName"]
        self._write_group(gid, display, body.get("description"))
        if "members" in body:
            want = {m["value"] for m in body.get("members") or []}
            have = {m["value"] for m in current.get("members", [])}
            for uid in want - have:
                self._set_membership(uid, gid, True)
            for uid in have - want:
                self._set_membership(uid, gid, False)
        row = self._touch("Group", gid, body.get("externalId"), display)
        self._record("group.replace", _group_urn(gid), {})
        return self._group_doc(gid, row)

    def patch_group(self, gid: str, body: dict) -> dict:
        current = self.get_group(gid)
        display = current["displayName"]
        for op in _operations(body):
            kind, path, value = (op.get("op") or "").lower(), op.get("path") or "", op.get("value")
            m = re.match(r'^members\[value eq "([^"]+)"\]$', path)
            if path.lower() == "members" or m:
                if kind in ("add", "replace") and path.lower() == "members":
                    if kind == "replace":
                        for old in {x["value"] for x in self.get_group(gid).get("members", [])}:
                            self._set_membership(old, gid, False)
                    for item in value or []:
                        self._set_membership(item["value"], gid, True)
                elif kind == "remove":
                    targets = [m.group(1)] if m else [x["value"] for x in (value or [])] or \
                        [x["value"] for x in self.get_group(gid).get("members", [])]
                    for uid in targets:
                        self._set_membership(uid, gid, False)
                else:
                    raise ScimError(400, f"unsupported op {kind} on {path}", "invalidSyntax")
            elif path.lower() == "displayname" or (not path and isinstance(value, dict) and "displayName" in value):
                display = value if isinstance(value, str) else value["displayName"]
                self._write_group(gid, display, None)
            elif not path and isinstance(value, dict) and set(value) <= {"id", "externalId"}:
                continue
            else:
                raise ScimError(400, f"unsupported path {path!r}", "invalidPath")
        row = self._touch("Group", gid, None, display)
        self._record("group.patch", _group_urn(gid), {"operations": len(_operations(body))})
        return self._group_doc(gid, row)

    def delete_group(self, gid: str) -> None:
        self.get_group(gid)
        for uid in self._members(gid):
            self._set_membership(uid, gid, False)
        self.graph.emit([make_mcp(_group_urn(gid), "status", {"removed": True})])
        with transaction(self.engine) as c:
            c.execute(scim_resources.delete().where(and_(scim_resources.c.resource_type == "Group",
                                                         scim_resources.c.id == gid)))
        self._record("group.delete", _group_urn(gid), {})

    def list_groups(self, flt: Optional[str], start: int, count: int) -> dict:
        parsed = parse_filter(flt)
        if parsed and parsed[0] in ("displayname", "id"):
            gid = parsed[1] if parsed[0] == "id" else group_id(parsed[1])
            doc = self._group_doc(gid)
            docs = [doc] if doc else []
        else:
            if parsed and parsed[0] != "externalid":
                raise ScimError(400, f"unsupported filter attribute {parsed[0]}", "invalidFilter")
            rows = self._rows("Group", *(parsed or (None, None)))
            docs = [d for d in (self._group_doc(r["id"], r) for r in rows) if d]
        return _list(docs, start, count)


def _operations(body: dict) -> list[dict]:
    ops = body.get("Operations") or body.get("operations")
    if not isinstance(ops, list):
        raise ScimError(400, "Operations is required", "invalidSyntax")
    return ops


def _set_path(doc: dict, path: str, value: Any) -> None:
    """Simple attribute paths: ``active``, ``displayName``, ``name.givenName``, ``emails[type eq "work"].value``."""
    if path.startswith("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:"):
        doc.setdefault("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User", {})[path.rsplit(":", 1)[1]] = value
        return
    m = re.match(r'^emails(\[.*\])?(\.value)?$', path)
    if m:
        doc["emails"] = [{"value": value if isinstance(value, str) else
                          (value[0]["value"] if value else None), "primary": True}] if value else []
        return
    head, _, rest = path.partition(".")
    if rest:
        sub = doc.setdefault(head, {}) or {}
        sub[rest] = value
        doc[head] = sub
    else:
        doc[head] = value


def _list(docs: list[dict], start: int, count: int) -> dict:
    start = max(start, 1)
    page = docs[start - 1:start - 1 + max(count, 0)]
    return {"schemas": [LIST_SCHEMA], "totalResults": len(docs), "startIndex": start, "itemsPerPage": len(page),
            "Resources": page}


# --------------------------------------------------------------------------- HTTP
def scim_token() -> Optional[str]:
    path = os.environ.get("CAIRN_SCIM_TOKEN_FILE")
    if path and os.path.exists(path):
        return open(path).read().strip() or None
    return os.environ.get("CAIRN_SCIM_TOKEN") or None


def build_router(service: ScimService, token: Callable[[], Optional[str]] = scim_token):
    # Imported here: the table above registers on every service's metadata, the server extra is optional.
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import JSONResponse, Response

    media = "application/scim+json"

    def auth(request: Request) -> None:
        expected = token()
        given = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not expected:
            raise ScimError(503, "SCIM provisioning is not configured (CAIRN_SCIM_TOKEN_FILE)")
        if not given or not hmac.compare_digest(given.encode(), expected.encode()):
            raise ScimError(401, "invalid SCIM token")

    router = APIRouter(prefix="/scim/v2", dependencies=[Depends(auth)], tags=["scim"])

    def ok(doc: dict, status: int = 200) -> JSONResponse:
        headers = {"ETag": doc["meta"]["version"], "Location": doc["meta"]["location"]} if "meta" in doc else {}
        return JSONResponse(doc, status_code=status, media_type=media, headers=headers)

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise ScimError(400, "body must be JSON", "invalidSyntax") from None
        if not isinstance(data, dict):
            raise ScimError(400, "body must be a JSON object", "invalidSyntax")
        return data

    @router.get("/ServiceProviderConfig")
    def provider_config():
        return JSONResponse({
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True}, "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 1000}, "changePassword": {"supported": False},
            "sort": {"supported": False}, "etag": {"supported": True},
            "authenticationSchemes": [{"type": "oauthbearertoken", "name": "Bearer token",
                                       "description": "Token from CAIRN_SCIM_TOKEN_FILE", "primary": True}]},
            media_type=media)

    @router.get("/ResourceTypes")
    def resource_types():
        types = [{"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": n, "name": n,
                  "endpoint": f"/{n}s", "schema": s} for n, s in (("User", USER_SCHEMA), ("Group", GROUP_SCHEMA))]
        return JSONResponse(_list(types, 1, 10), media_type=media)

    @router.get("/Schemas")
    def schemas():
        docs = [{"id": USER_SCHEMA, "name": "User"}, {"id": GROUP_SCHEMA, "name": "Group"}]
        return JSONResponse(_list(docs, 1, 10), media_type=media)

    @router.get("/Users")
    def list_users(filter: Optional[str] = None, startIndex: int = 1, count: int = 100):  # noqa: A002, N803
        return JSONResponse(service.list_users(filter, startIndex, min(count, 1000)), media_type=media)

    @router.post("/Users")
    async def create_user(request: Request):
        return ok(service.create_user(await body(request)), 201)

    @router.get("/Users/{uid}")
    def get_user(uid: str):
        return ok(service.get_user(uid))

    @router.put("/Users/{uid}")
    async def replace_user(uid: str, request: Request):
        return ok(service.replace_user(uid, await body(request)))

    @router.patch("/Users/{uid}")
    async def patch_user(uid: str, request: Request):
        return ok(service.patch_user(uid, await body(request)))

    @router.delete("/Users/{uid}", status_code=204)
    def delete_user(uid: str):
        service.delete_user(uid)
        return Response(status_code=204)

    @router.get("/Groups")
    def list_groups(filter: Optional[str] = None, startIndex: int = 1, count: int = 100):  # noqa: A002, N803
        return JSONResponse(service.list_groups(filter, startIndex, min(count, 1000)), media_type=media)

    @router.post("/Groups")
    async def create_group(request: Request):
        return ok(service.create_group(await body(request)), 201)

    @router.get("/Groups/{gid}")
    def get_group(gid: str):
        return ok(service.get_group(gid))

    @router.put("/Groups/{gid}")
    async def replace_group(gid: str, request: Request):
        return ok(service.replace_group(gid, await body(request)))

    @router.patch("/Groups/{gid}")
    async def patch_group(gid: str, request: Request):
        return ok(service.patch_group(gid, await body(request)))

    @router.delete("/Groups/{gid}", status_code=204)
    def delete_group(gid: str):
        service.delete_group(gid)
        return Response(status_code=204)

    return router


def install_error_handler(app) -> None:
    from fastapi.responses import JSONResponse

    @app.exception_handler(ScimError)
    async def scim_error(_request, exc: ScimError):
        return JSONResponse(exc.body(), status_code=exc.status, media_type="application/scim+json")
