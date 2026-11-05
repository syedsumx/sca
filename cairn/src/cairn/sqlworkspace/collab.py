"""BI folders, comments and usage: organizing dashboards, workbooks and saved queries, talking about them, and
seeing who uses them.

* **Folders** (``/sql/folders``): a person makes folders (nested, up to 8 deep) and files assets they may open in
  them; a folder ``shared`` is listed for everyone. Listing a folder shows only the assets the viewer may open, so
  a folder never reveals what is not shared with them. Only the folder's owner changes it.
* **Comments** (``/sql/comments/<kind>/<id>``): anyone who may open an asset reads and writes its comments,
  optionally about one tile (``tile``) and as a reply (``parent``). ``@user`` mentions tell that person, if they
  may open the asset; the owner is told of new comments. Authors edit and delete their own (a deleted comment
  keeps its place in a thread, without its text); the asset's owner may delete any.
* **Usage** (``/sql/usage``): each view (a dashboard rendered, a workbook or saved query opened or run) is
  counted per asset, person and day. ``GET /sql/usage/<kind>/<id>`` (owner and editors) gives views and distinct
  viewers per day; ``GET /sql/usage/popular`` the most viewed assets the caller may open, and ``GET /sql/usage/stale`` the caller's
  own assets nobody viewed in ``days`` (default 30) days. Kept
  ``CAIRN_BI_USAGE_DAYS`` (default 180) days.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import (BigInteger, Boolean, Column, Index, Integer, PrimaryKeyConstraint, String, Table, Text, and_, delete,
                        func, insert, select, update)
from sqlalchemy.exc import IntegrityError

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

KINDS = ("query", "workbook", "dashboard")
MENTION = re.compile(r"@([A-Za-z0-9._-]{1,128})")
MAX_DEPTH = 8

bi_folders = Table(
    "cairn_bi_folders", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("parent_id", String(32), nullable=True, index=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("shared", Boolean, nullable=False, default=False),
    Column("created_ms", BigInteger, nullable=False),
)
bi_folder_items = Table(
    "cairn_bi_folder_items", metadata,
    Column("folder_id", String(32), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("asset_id", String(64), nullable=False),
    PrimaryKeyConstraint("folder_id", "kind", "asset_id"),
)
bi_comments = Table(
    "cairn_bi_comments", metadata,
    Column("id", String(32), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("asset_id", String(64), nullable=False),
    Column("tile", Integer, nullable=True),
    Column("parent_id", String(32), nullable=True),
    Column("author", String(256), nullable=False),
    Column("body", Text, nullable=True),
    Column("deleted", Boolean, nullable=False, default=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("edited_ms", BigInteger, nullable=True),
    Index("ix_cairn_bi_comments_asset", "kind", "asset_id"),
)
bi_views = Table(
    "cairn_bi_views", metadata,
    Column("kind", String(16), nullable=False),
    Column("asset_id", String(64), nullable=False),
    Column("viewer", String(256), nullable=False),
    Column("day", String(10), nullable=False),
    Column("count", Integer, nullable=False),
    PrimaryKeyConstraint("kind", "asset_id", "viewer", "day"),
    Index("ix_cairn_bi_views_day", "day"),
)


class CollabError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _day(ms: Optional[int] = None) -> str:
    return datetime.fromtimestamp((ms or _now()) / 1000, timezone.utc).strftime("%Y-%m-%d")


class BiCollab:
    def __init__(self, engine, level: Callable[[str, str, str], int], notify=None, audit=None, keep_days: int = 180):
        """``level(actor, kind, id)``: the actor's access to an asset (0 none, 1 viewer, 2 editor, 3 owner)."""
        self.engine, self.level, self.notify, self.audit, self.keep_days = engine, level, notify, audit, keep_days
        self._pruned = ""

    def _need(self, actor: str, kind: str, aid: str, at_least: int = 1) -> int:
        if kind not in KINDS:
            raise CollabError(f"kind: one of {', '.join(KINDS)}", 404)
        lvl = self.level(actor, kind, aid)
        if not lvl:
            raise CollabError(f"no such {kind}", 404)
        if lvl < at_least:
            raise CollabError("not allowed on this asset", 403)
        return lvl

    # ---- folders ----------------------------------------------------------------------------------
    def _folder(self, fid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(bi_folders).where(bi_folders.c.id == fid)).first()
        if r is None:
            raise CollabError("no such folder", 404)
        return dict(r._mapping)

    def _visible(self, actor: str, f: dict) -> bool:
        return f["owner"] == actor or bool(f["shared"])

    def folders(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(bi_folders).where((bi_folders.c.owner == actor) | bi_folders.c.shared)
                                .order_by(bi_folders.c.name)).all()
        return [dict(r._mapping) for r in rows]

    def make_folder(self, actor: str, body: dict) -> dict:
        name = str((body or {}).get("name") or "").strip()
        if not 1 <= len(name) <= 200:
            raise CollabError("name: 1 to 200 characters")
        parent = body.get("parent")
        if parent:
            p = self._folder(str(parent))
            if p["owner"] != actor:
                raise CollabError("folders go in folders of your own", 403)
            depth, cur = 1, p
            while cur.get("parent_id"):
                depth += 1
                cur = self._folder(cur["parent_id"])
            if depth >= MAX_DEPTH:
                raise CollabError(f"folders nest at most {MAX_DEPTH} deep")
        fid = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(insert(bi_folders).values(id=fid, name=name, parent_id=parent or None, owner=actor,
                                                   shared=bool(body.get("shared")), created_ms=_now()))
        return self._folder(fid)

    def change_folder(self, actor: str, fid: str, body: dict) -> dict:
        f = self._folder(fid)
        if f["owner"] != actor:
            raise CollabError("only the folder's owner changes it", 403 if self._visible(actor, f) else 404)
        values = {}
        if "name" in body:
            name = str(body["name"] or "").strip()
            if not 1 <= len(name) <= 200:
                raise CollabError("name: 1 to 200 characters")
            values["name"] = name
        if "shared" in body:
            values["shared"] = bool(body["shared"])
        if values:
            with self.engine.begin() as conn:
                conn.execute(update(bi_folders).where(bi_folders.c.id == fid).values(**values))
        return self._folder(fid)

    def delete_folder(self, actor: str, fid: str) -> None:
        f = self._folder(fid)
        if f["owner"] != actor:
            raise CollabError("only the folder's owner deletes it", 403 if self._visible(actor, f) else 404)
        with self.engine.connect() as conn:
            if conn.execute(select(bi_folders.c.id).where(bi_folders.c.parent_id == fid).limit(1)).first():
                raise CollabError("the folder has folders in it: move or delete them first", 409)
        with self.engine.begin() as conn:
            conn.execute(delete(bi_folder_items).where(bi_folder_items.c.folder_id == fid))
            conn.execute(delete(bi_folders).where(bi_folders.c.id == fid))

    def file(self, actor: str, fid: str, kind: str, aid: str, remove: bool = False) -> dict:
        f = self._folder(fid)
        if f["owner"] != actor:
            raise CollabError("only the folder's owner files into it", 403 if self._visible(actor, f) else 404)
        if not remove:
            self._need(actor, kind, aid)                         # only what the owner may open
        with self.engine.begin() as conn:
            if remove:
                conn.execute(delete(bi_folder_items).where(and_(bi_folder_items.c.folder_id == fid,
                                                                bi_folder_items.c.kind == kind,
                                                                bi_folder_items.c.asset_id == aid)))
            else:
                try:
                    with conn.begin_nested():
                        conn.execute(insert(bi_folder_items).values(folder_id=fid, kind=kind, asset_id=aid))
                except IntegrityError:
                    pass                                         # already there
        return self.folder(actor, fid)

    def folder(self, actor: str, fid: str) -> dict:
        f = self._folder(fid)
        if not self._visible(actor, f):
            raise CollabError("no such folder", 404)
        with self.engine.connect() as conn:
            items = conn.execute(select(bi_folder_items).where(bi_folder_items.c.folder_id == fid)).all()
            subs = conn.execute(select(bi_folders).where(bi_folders.c.parent_id == fid).order_by(bi_folders.c.name)).all()
        visible = [{"kind": i.kind, "id": i.asset_id} for i in items if self.level(actor, i.kind, i.asset_id)]
        return {**f, "items": visible, "folders": [dict(s._mapping) for s in subs if self._visible(actor, dict(s._mapping))]}

    # ---- comments ---------------------------------------------------------------------------------
    def comments(self, actor: str, kind: str, aid: str) -> list[dict]:
        self._need(actor, kind, aid)
        with self.engine.connect() as conn:
            rows = conn.execute(select(bi_comments).where(and_(bi_comments.c.kind == kind, bi_comments.c.asset_id == aid))
                                .order_by(bi_comments.c.created_ms)).all()
        return [{**dict(r._mapping), "body": None if r.deleted else r.body} for r in rows]

    def comment(self, actor: str, kind: str, aid: str, body: dict, owner_of: Callable[[str, str], str]) -> dict:
        self._need(actor, kind, aid)
        text = str((body or {}).get("body") or "").strip()
        if not 1 <= len(text) <= 10_000:
            raise CollabError("body: 1 to 10,000 characters")
        tile = body.get("tile")
        if tile is not None and (not isinstance(tile, int) or isinstance(tile, bool) or not 0 <= tile < 100):
            raise CollabError("tile: the tile's position (from 0)")
        parent = body.get("parent")
        if parent:
            with self.engine.connect() as conn:
                p = conn.execute(select(bi_comments).where(bi_comments.c.id == str(parent))).first()
            if p is None or (p.kind, p.asset_id) != (kind, aid):
                raise CollabError("parent: a comment on the same asset", 404)
        cid = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(insert(bi_comments).values(id=cid, kind=kind, asset_id=aid, tile=tile, parent_id=parent or None,
                                                    author=actor, body=text, deleted=False, created_ms=_now()))
        if self.audit is not None:
            self.audit.record("sql", "bi.commented", actor, f"{kind}:{aid}", {"comment": cid})
        # Tell those mentioned who may open it, and the owner.
        told = set()
        for name in dict.fromkeys(MENTION.findall(text)):
            who = f"urn:li:corpuser:{name}"
            if who != actor and self.level(who, kind, aid):
                told.add(who)
                self._tell("bi.mentioned", f"{actor.rsplit(':', 1)[-1]} mentioned you", text[:300], [who],
                           {"kind": kind, "id": aid, "comment": cid})
        owner = owner_of(kind, aid)
        if owner and owner != actor and owner not in told:
            self._tell("bi.commented", f"New comment from {actor.rsplit(':', 1)[-1]}", text[:300], [owner],
                       {"kind": kind, "id": aid, "comment": cid})
        return self._comment(cid)

    def _comment(self, cid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(bi_comments).where(bi_comments.c.id == cid)).first()
        if r is None:
            raise CollabError("no such comment", 404)
        return {**dict(r._mapping), "body": None if r.deleted else r.body}

    def edit_comment(self, actor: str, cid: str, text: str) -> dict:
        c = self._comment(cid)
        self._need(actor, c["kind"], c["asset_id"])
        if c["author"] != actor or c["deleted"]:
            raise CollabError("only its author edits a comment", 403)
        text = (text or "").strip()
        if not 1 <= len(text) <= 10_000:
            raise CollabError("body: 1 to 10,000 characters")
        with self.engine.begin() as conn:
            conn.execute(update(bi_comments).where(bi_comments.c.id == cid).values(body=text, edited_ms=_now()))
        return self._comment(cid)

    def delete_comment(self, actor: str, cid: str) -> None:
        c = self._comment(cid)
        lvl = self._need(actor, c["kind"], c["asset_id"])
        if c["author"] != actor and lvl < 3:
            raise CollabError("only its author or the asset's owner deletes a comment", 403)
        with self.engine.begin() as conn:
            conn.execute(update(bi_comments).where(bi_comments.c.id == cid).values(deleted=True, body=None))
        if self.audit is not None:
            self.audit.record("sql", "bi.comment_deleted", actor, f"{c['kind']}:{c['asset_id']}", {"comment": cid})

    # ---- usage ------------------------------------------------------------------------------------
    def viewed(self, kind: str, aid: str, actor: str) -> None:
        """Counts one view (never fails what is being viewed); the order of the save and view hooks."""
        try:
            day = _day()
            with self.engine.begin() as conn:
                q = update(bi_views).where(and_(bi_views.c.kind == kind, bi_views.c.asset_id == aid,
                                                bi_views.c.viewer == actor, bi_views.c.day == day))
                if not conn.execute(q.values(count=bi_views.c.count + 1)).rowcount:
                    try:
                        with conn.begin_nested():
                            conn.execute(insert(bi_views).values(kind=kind, asset_id=aid, viewer=actor, day=day, count=1))
                    except IntegrityError:
                        conn.execute(q.values(count=bi_views.c.count + 1))
            if day != self._pruned:
                self._pruned = day
                cutoff = _day(_now() - self.keep_days * 86_400_000)
                with self.engine.begin() as conn:
                    conn.execute(delete(bi_views).where(bi_views.c.day < cutoff))
        except Exception:  # noqa: BLE001 - counting a view never fails what is viewed
            logger.exception("counting a view of %s %s failed", kind, aid)

    def usage(self, actor: str, kind: str, aid: str, days: int = 30) -> dict:
        self._need(actor, kind, aid, at_least=2)
        since = _day(_now() - max(1, min(days, self.keep_days)) * 86_400_000)
        with self.engine.connect() as conn:
            rows = conn.execute(select(bi_views.c.day, func.sum(bi_views.c.count), func.count(bi_views.c.viewer))
                                .where(and_(bi_views.c.kind == kind, bi_views.c.asset_id == aid, bi_views.c.day >= since))
                                .group_by(bi_views.c.day).order_by(bi_views.c.day)).all()
            viewers = conn.execute(select(func.count(func.distinct(bi_views.c.viewer))).where(and_(
                bi_views.c.kind == kind, bi_views.c.asset_id == aid, bi_views.c.day >= since))).scalar_one()
        return {"kind": kind, "id": aid, "since": since, "viewers": int(viewers),
                "views": int(sum(r[1] for r in rows)),
                "days": [{"day": r[0], "views": int(r[1]), "viewers": int(r[2])} for r in rows]}

    def popular(self, actor: str, days: int = 30, limit: int = 20) -> list[dict]:
        since = _day(_now() - max(1, min(days, self.keep_days)) * 86_400_000)
        with self.engine.connect() as conn:
            rows = conn.execute(select(bi_views.c.kind, bi_views.c.asset_id, func.sum(bi_views.c.count).label("views"),
                                       func.count(func.distinct(bi_views.c.viewer)).label("viewers"))
                                .where(bi_views.c.day >= since).group_by(bi_views.c.kind, bi_views.c.asset_id)
                                .order_by(func.sum(bi_views.c.count).desc()).limit(500)).all()
        out = [{"kind": r.kind, "id": r.asset_id, "views": int(r.views), "viewers": int(r.viewers)}
               for r in rows if self.level(actor, r.kind, r.asset_id)]
        return out[:max(1, min(limit, 100))]

    def stale(self, actor: str, days: int = 30) -> list[dict]:
        """The actor's own assets nobody has viewed in ``days`` days: candidates to tidy away."""
        from cairn.sqlworkspace.sharing import _table

        days = max(1, min(days, self.keep_days))
        since = _day(_now() - days * 86_400_000)
        out = []
        with self.engine.connect() as conn:
            seen = {(r.kind, r.asset_id) for r in conn.execute(
                select(bi_views.c.kind, bi_views.c.asset_id).where(bi_views.c.day >= since).distinct())}
            for kind in KINDS:
                t = _table(kind)
                for r in conn.execute(select(t.c.id, t.c.name, t.c.updated_ms).where(t.c.owner == actor)):
                    if (kind, r.id) not in seen:
                        out.append({"kind": kind, "id": r.id, "name": r.name, "updated_ms": r.updated_ms})
        return sorted(out, key=lambda x: x["updated_ms"] or 0)

    def _tell(self, kind: str, title: str, body: str, who: list[str], payload: dict) -> None:
        if self.notify is not None:
            try:
                self.notify(kind, title, body, who, payload)
            except Exception:  # noqa: BLE001
                pass


def build_collab_router(c: BiCollab, current_principal, owner_of) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/sql", tags=["sql"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except CollabError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/folders")
    async def folders(p=Depends(current_principal)):
        return await call(lambda: c.folders(p.urn))

    @router.post("/folders", status_code=201)
    async def make(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: c.make_folder(p.urn, body))

    @router.get("/folders/{fid}")
    async def folder(fid: str, p=Depends(current_principal)):
        return await call(lambda: c.folder(p.urn, fid))

    @router.patch("/folders/{fid}")
    async def change(fid: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: c.change_folder(p.urn, fid, body))

    @router.delete("/folders/{fid}", status_code=204)
    async def drop(fid: str, p=Depends(current_principal)):
        await call(lambda: c.delete_folder(p.urn, fid))

    @router.put("/folders/{fid}/items/{kind}/{aid}")
    async def file(fid: str, kind: str, aid: str, p=Depends(current_principal)):
        return await call(lambda: c.file(p.urn, fid, kind, aid))

    @router.delete("/folders/{fid}/items/{kind}/{aid}")
    async def unfile(fid: str, kind: str, aid: str, p=Depends(current_principal)):
        return await call(lambda: c.file(p.urn, fid, kind, aid, remove=True))

    @router.get("/comments/{kind}/{aid}")
    async def comments(kind: str, aid: str, p=Depends(current_principal)):
        return await call(lambda: c.comments(p.urn, kind, aid))

    @router.post("/comments/{kind}/{aid}", status_code=201)
    async def comment(kind: str, aid: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: c.comment(p.urn, kind, aid, body, owner_of))

    @router.patch("/comments/{cid}")
    async def edit(cid: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: c.edit_comment(p.urn, cid, str(body.get("body") or "")))

    @router.delete("/comments/{cid}", status_code=204)
    async def remove(cid: str, p=Depends(current_principal)):
        await call(lambda: c.delete_comment(p.urn, cid))

    @router.get("/usage/popular")
    async def popular(days: int = 30, limit: int = 20, p=Depends(current_principal)):
        return await call(lambda: c.popular(p.urn, days, limit))

    @router.get("/usage/stale")
    async def stale(days: int = 30, p=Depends(current_principal)):
        return await call(lambda: c.stale(p.urn, days))

    @router.get("/usage/{kind}/{aid}")
    async def usage(kind: str, aid: str, days: int = 30, p=Depends(current_principal)):
        return await call(lambda: c.usage(p.urn, kind, aid, days))

    return router
