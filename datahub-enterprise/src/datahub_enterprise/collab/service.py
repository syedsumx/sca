"""Collaboration: bookmarks, follows, views → trending, discussion threads, activity feed.

Trending score for an asset (per caller-visible window)::

    score = Σ_views e^(−Δt/τ) + 3·bookmarks + 2·unique_viewers_7d        (τ = 3 days)

"new" = first view within the last 7 days; "up" = this week's views ≥ 1.5× last week's.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, Integer, String, Table, and_, delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from datahub_enterprise.common.db import JSONText, metadata

DAY_MS = 86_400_000
TAU_MS = 3 * DAY_MS
ALLOWED_EMOJI = {"👍", "🎉", "❤️", "👀", "✅", "❓", "🚀", "😕"}
_MENTION = re.compile(r"@(urn:li:corp(?:user|Group):[A-Za-z0-9_.@\-]+|[A-Za-z0-9_.\-]+(?:@[A-Za-z0-9.\-]+)?)")

bookmarks = Table(
    "dhe_bookmarks", metadata,
    Column("actor", String(512), primary_key=True),
    Column("urn", String(1024), primary_key=True),
    Column("note", String(500), nullable=True),
    Column("created_at_ms", BigInteger, nullable=False),
)
follows = Table(
    "dhe_follows", metadata,
    Column("actor", String(512), primary_key=True),
    Column("urn", String(1024), primary_key=True),
    Column("created_at_ms", BigInteger, nullable=False),
)
views = Table(
    "dhe_views", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor", String(512), nullable=False, index=True),
    Column("urn", String(1024), nullable=False, index=True),
    Column("ts_ms", BigInteger, nullable=False, index=True),
)
threads = Table(
    "dhe_threads", metadata,
    Column("id", String(40), primary_key=True),
    Column("entity_urn", String(1024), nullable=False, index=True),
    Column("field_path", String(500), nullable=True),
    Column("status", String(16), nullable=False),
    Column("created_by", String(512), nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
)
comments = Table(
    "dhe_comments", metadata,
    Column("id", String(40), primary_key=True),
    Column("thread_id", String(40), nullable=False, index=True),
    Column("author", String(512), nullable=False),
    Column("body_md", String(10_000), nullable=False),
    Column("mentions", JSONText, nullable=True),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("edited_at_ms", BigInteger, nullable=True),
)
reactions = Table(
    "dhe_reactions", metadata,
    Column("comment_id", String(40), primary_key=True),
    Column("actor", String(512), primary_key=True),
    Column("emoji", String(16), primary_key=True),
)


class CommentIn(BaseModel):
    body_md: str = Field(min_length=1, max_length=10_000)


class ThreadIn(CommentIn):
    entity_urn: str = Field(pattern=r"^urn:li:")
    field_path: Optional[str] = Field(default=None, max_length=500)


def _now() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None


def parse_mentions(body: str) -> list[str]:
    """``@jdoe``, ``@jane.doe@corp.com`` and ``@urn:li:corpuser:x`` → corpuser/corpGroup urns."""
    out: list[str] = []
    for m in _MENTION.finditer(body):
        token = m.group(1)
        urn = token if token.startswith("urn:li:") else f"urn:li:corpuser:{token}"
        if urn not in out:
            out.append(urn)
    return out


class CollabService:
    def __init__(self, engine: Engine, notify=None, entity_name: Optional[Callable[[str], str]] = None):
        self.engine = engine
        self.notify = notify
        self.entity_name = entity_name or (lambda urn: urn)

    # ---- bookmarks / follows --------------------------------------------------------
    def bookmark(self, actor: str, urn: str, note: Optional[str] = None) -> dict:
        with self.engine.begin() as conn:
            if conn.execute(select(bookmarks).where(and_(bookmarks.c.actor == actor, bookmarks.c.urn == urn))).first():
                conn.execute(update(bookmarks).where(and_(bookmarks.c.actor == actor, bookmarks.c.urn == urn))
                             .values(note=note))
            else:
                conn.execute(bookmarks.insert().values(actor=actor, urn=urn, note=note, created_at_ms=_now()))
        return {"urn": urn, "note": note}

    def unbookmark(self, actor: str, urn: str) -> bool:
        with self.engine.begin() as conn:
            return bool(conn.execute(delete(bookmarks).where(and_(bookmarks.c.actor == actor,
                                                                   bookmarks.c.urn == urn))).rowcount)

    def list_bookmarks(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(bookmarks).where(bookmarks.c.actor == actor)
                                .order_by(bookmarks.c.created_at_ms.desc())).fetchall()
        return [{"urn": r.urn, "note": r.note, "created_at": _iso(r.created_at_ms)} for r in rows]

    def follow(self, actor: str, urn: str) -> dict:
        try:
            with self.engine.begin() as conn:
                conn.execute(follows.insert().values(actor=actor, urn=urn, created_at_ms=_now()))
        except IntegrityError:
            pass
        return {"urn": urn}

    def unfollow(self, actor: str, urn: str) -> bool:
        with self.engine.begin() as conn:
            return bool(conn.execute(delete(follows).where(and_(follows.c.actor == actor,
                                                                 follows.c.urn == urn))).rowcount)

    def list_follows(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(follows).where(follows.c.actor == actor)
                                .order_by(follows.c.created_at_ms.desc())).fetchall()
        return [{"urn": r.urn, "created_at": _iso(r.created_at_ms)} for r in rows]

    def followers(self, urn: str) -> list[str]:
        with self.engine.connect() as conn:
            return [r.actor for r in conn.execute(select(follows.c.actor).where(follows.c.urn == urn))]

    # ---- views / trending -----------------------------------------------------------
    def record_view(self, actor: str, urn: str, ts_ms: Optional[int] = None) -> None:
        ts = ts_ms or _now()
        with self.engine.begin() as conn:
            # Collapse repeated views by the same user within 10 minutes.
            recent = conn.execute(select(views.c.id).where(and_(
                views.c.actor == actor, views.c.urn == urn, views.c.ts_ms > ts - 600_000))).first()
            if not recent:
                conn.execute(views.insert().values(actor=actor, urn=urn, ts_ms=ts))

    def recent(self, actor: str, limit: int = 20) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(views.c.urn, func.max(views.c.ts_ms).label("ts")).where(views.c.actor == actor)
                                .group_by(views.c.urn).order_by(func.max(views.c.ts_ms).desc()).limit(limit)).fetchall()
        return [{"urn": r.urn, "last_viewed_at": _iso(r.ts)} for r in rows]

    def trending(self, limit: int = 20, now_ms: Optional[int] = None,
                 candidate_filter: Optional[Callable[[str], bool]] = None) -> list[dict]:
        now = now_ms or _now()
        since = now - 28 * DAY_MS
        with self.engine.connect() as conn:
            rows = conn.execute(select(views.c.urn, views.c.actor, views.c.ts_ms).where(views.c.ts_ms >= since)).fetchall()
            bm = dict(conn.execute(select(bookmarks.c.urn, func.count()).group_by(bookmarks.c.urn)).fetchall())
        stats: dict[str, dict] = {}
        for urn, actor, ts in rows:
            s = stats.setdefault(urn, {"score": 0.0, "views_7d": 0, "prev_7d": 0, "viewers": set(), "first": ts})
            s["score"] += math.exp(-(now - ts) / TAU_MS)
            s["first"] = min(s["first"], ts)
            if ts >= now - 7 * DAY_MS:
                s["views_7d"] += 1
                s["viewers"].add(actor)
            elif ts >= now - 14 * DAY_MS:
                s["prev_7d"] += 1
        out = []
        for urn, s in stats.items():
            if candidate_filter and not candidate_filter(urn):
                continue
            bookmarks_n = int(bm.get(urn, 0))
            score = s["score"] + 3 * bookmarks_n + 2 * len(s["viewers"])
            if s["first"] >= now - 7 * DAY_MS:
                trend = "new"
            elif s["views_7d"] >= 1.5 * max(s["prev_7d"], 1):
                trend = "up"
            else:
                trend = "steady"
            out.append({"urn": urn, "score": round(score, 3), "views_7d": s["views_7d"],
                        "unique_viewers_7d": len(s["viewers"]), "bookmarks": bookmarks_n, "trend": trend})
        out.sort(key=lambda x: -x["score"])
        return out[:limit]

    def engagement(self, actor: str, urns: list[str]) -> dict[str, dict]:
        if not urns:
            return {}
        since = _now() - 7 * DAY_MS
        with self.engine.connect() as conn:
            v = dict(conn.execute(select(views.c.urn, func.count()).where(and_(views.c.urn.in_(urns),
                                  views.c.ts_ms >= since)).group_by(views.c.urn)).fetchall())
            b = dict(conn.execute(select(bookmarks.c.urn, func.count()).where(bookmarks.c.urn.in_(urns))
                                  .group_by(bookmarks.c.urn)).fetchall())
            mine_b = {r[0] for r in conn.execute(select(bookmarks.c.urn).where(and_(
                bookmarks.c.actor == actor, bookmarks.c.urn.in_(urns))))}
            mine_f = {r[0] for r in conn.execute(select(follows.c.urn).where(and_(
                follows.c.actor == actor, follows.c.urn.in_(urns))))}
            t = dict(conn.execute(select(threads.c.entity_urn, func.count()).where(and_(
                threads.c.entity_urn.in_(urns), threads.c.status == "OPEN")).group_by(threads.c.entity_urn)).fetchall())
        return {u: {"views_7d": int(v.get(u, 0)), "bookmarks": int(b.get(u, 0)), "bookmarked": u in mine_b,
                    "following": u in mine_f, "threads_open": int(t.get(u, 0))} for u in urns}

    # ---- discussions ------------------------------------------------------------------
    def _thread_dict(self, conn, row, actor: Optional[str] = None) -> dict:
        crow = conn.execute(select(comments).where(comments.c.thread_id == row.id)
                            .order_by(comments.c.created_at_ms)).fetchall()
        ids = [c.id for c in crow]
        rmap: dict[str, dict[str, list[str]]] = {}
        if ids:
            for r in conn.execute(select(reactions).where(reactions.c.comment_id.in_(ids))):
                rmap.setdefault(r.comment_id, {}).setdefault(r.emoji, []).append(r.actor)
        return {"id": row.id, "entity_urn": row.entity_urn, "field_path": row.field_path, "status": row.status,
                "created_by": row.created_by, "created_at": _iso(row.created_at_ms),
                "comments": [{"id": c.id, "thread_id": c.thread_id, "author": c.author, "body_md": c.body_md,
                              "created_at": _iso(c.created_at_ms), "edited_at": _iso(c.edited_at_ms),
                              "mentions": c.mentions or [], "reactions": rmap.get(c.id, {})} for c in crow]}

    def list_threads(self, entity_urn: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(threads).where(threads.c.entity_urn == entity_urn)
                                .order_by(threads.c.updated_at_ms.desc())).fetchall()
            return [self._thread_dict(conn, r) for r in rows]

    def get_thread(self, thread_id: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(select(threads).where(threads.c.id == thread_id)).first()
            return self._thread_dict(conn, row) if row else None

    def create_thread(self, actor: str, req: ThreadIn) -> dict:
        tid = f"thr_{uuid.uuid4().hex[:16]}"
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(threads.insert().values(id=tid, entity_urn=req.entity_urn, field_path=req.field_path,
                                                 status="OPEN", created_by=actor, created_at_ms=now, updated_at_ms=now))
        self._add_comment(actor, tid, req.body_md, req.entity_urn, req.field_path, new_thread=True)
        return self.get_thread(tid)  # type: ignore[return-value]

    def add_comment(self, actor: str, thread_id: str, body_md: str) -> dict:
        t = self.get_thread(thread_id)
        if t is None:
            raise KeyError("thread not found")
        self._add_comment(actor, thread_id, body_md, t["entity_urn"], t["field_path"], new_thread=False,
                          participants={c["author"] for c in t["comments"]} | {t["created_by"]})
        return self.get_thread(thread_id)  # type: ignore[return-value]

    def _add_comment(self, actor, thread_id, body_md, entity_urn, field_path, new_thread, participants=None):
        mentions = parse_mentions(body_md)
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(comments.insert().values(id=f"cmt_{uuid.uuid4().hex[:16]}", thread_id=thread_id, author=actor,
                                                  body_md=body_md, mentions=mentions, created_at_ms=now))
            conn.execute(update(threads).where(threads.c.id == thread_id).values(updated_at_ms=now, status="OPEN"))
        if self.notify is None:
            return
        from datahub_enterprise.notify.hub import Event

        where = self.entity_name(entity_urn) + (f".{field_path}" if field_path else "")
        snippet = body_md[:280]
        if mentions:
            self.notify.publish(Event(type="discussion.mention", title=f"You were mentioned on {where}",
                                      body=snippet, actor=actor, entity_urn=entity_urn, recipients=mentions))
        others = sorted((participants or set()) - set(mentions))
        self.notify.publish(Event(type="discussion.reply", title=f"{'New discussion' if new_thread else 'New reply'} "
                                  f"on {where}", body=snippet, actor=actor, entity_urn=entity_urn,
                                  recipients=others, include_followers=True))

    def set_status(self, actor: str, thread_id: str, status: str) -> dict:
        with self.engine.begin() as conn:
            res = conn.execute(update(threads).where(threads.c.id == thread_id).values(status=status,
                                                                                        updated_at_ms=_now()))
        if not res.rowcount:
            raise KeyError("thread not found")
        return self.get_thread(thread_id)  # type: ignore[return-value]

    def toggle_reaction(self, actor: str, comment_id: str, emoji: str, active: Optional[bool] = None) -> dict:
        """Toggle a reaction, or set it explicitly with ``active`` (idempotent add/remove)."""
        if emoji not in ALLOWED_EMOJI:
            raise ValueError(f"emoji must be one of {sorted(ALLOWED_EMOJI)}")
        with self.engine.begin() as conn:
            if not conn.execute(select(comments.c.id).where(comments.c.id == comment_id)).first():
                raise KeyError("comment not found")
            key = and_(reactions.c.comment_id == comment_id, reactions.c.actor == actor, reactions.c.emoji == emoji)
            exists = conn.execute(select(reactions).where(key)).first() is not None
            if active is None:
                active = not exists
            if exists and not active:
                conn.execute(delete(reactions).where(key))
            elif active and not exists:
                conn.execute(reactions.insert().values(comment_id=comment_id, actor=actor, emoji=emoji))
            actors = [r.actor for r in conn.execute(select(reactions.c.actor).where(and_(
                reactions.c.comment_id == comment_id, reactions.c.emoji == emoji)))]
        return {"comment_id": comment_id, "emoji": emoji, "active": active, "actors": actors}

    def thread_author_of_comment(self, comment_id: str) -> Optional[str]:
        with self.engine.connect() as conn:
            row = conn.execute(select(comments.c.thread_id).where(comments.c.id == comment_id)).first()
            if not row:
                return None
            t = conn.execute(select(threads.c.entity_urn).where(threads.c.id == row.thread_id)).first()
        return t.entity_urn if t else None

    # ---- activity -------------------------------------------------------------------
    def activity(self, actor: str, limit: int = 30) -> list[dict]:
        """Recent discussion activity on followed assets + mentions of the actor."""
        followed = {f["urn"] for f in self.list_follows(actor)}
        with self.engine.connect() as conn:
            rows = conn.execute(select(comments, threads.c.entity_urn, threads.c.field_path)
                                .join(threads, threads.c.id == comments.c.thread_id)
                                .order_by(comments.c.created_at_ms.desc()).limit(500)).fetchall()
        out = []
        for r in rows:
            mentioned = actor in (r.mentions or [])
            if r.author == actor or not (mentioned or r.entity_urn in followed):
                continue
            name = self.entity_name(r.entity_urn)
            out.append({"id": r.id, "type": "mention" if mentioned else "comment", "actor": r.author,
                        "entity_urn": r.entity_urn, "entity_name": name,
                        "summary": f"{'mentioned you' if mentioned else 'commented'} on {name}"
                                   f"{'.' + r.field_path if r.field_path else ''}: {r.body_md[:140]}",
                        "created_at": _iso(r.created_at_ms)})
            if len(out) >= limit:
                break
        return out
