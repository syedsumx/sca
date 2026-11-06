"""Vector search on lakehouse data: an index over a table's text, searched by meaning, under Cairn's policies.

A vector index names a lakehouse ``source`` table (``namespace.table``), its ``key`` column and its ``text``
column (the text of a files pipe, product descriptions, support tickets...). Cairn splits each row's text
into chunks (``chunk_chars``, 1,500, with a little overlap), embeds them with the deployment's embedding
model (the same as Cairn's search by meaning: ``CAIRN_EMBEDDINGS``), and keeps them in a lakehouse table of
its own (``<source>__<index>_vectors``: key, chunk, text, hash, embedding), so the vectors live in the lakehouse too.

* **Kept in step.** ``POST /v1/vectors/<name>/refresh``, and the scheduler every ``every_minutes``: rows
  whose text changed are embedded again, new rows added, removed rows dropped; unchanged rows are not
  embedded again. One commit per refresh.
* **Search.** ``GET /v1/vectors/<name>/search?q=...&k=10`` returns the closest chunks (cosine), each with its
  row key, the chunk's text and its score. The caller needs READ on the source table, and is refused when a
  mask or row filter applies to them there (the index holds the raw text). Searches are not audited per
  call; index changes are.
* **Approximate search (IVF).** With ``ann`` (``{"lists": 256, "probes": 8}``; ``"auto"``, the default, turns it on from
  ``CAIRN_VECTORS_ANN_MIN`` chunks, 20,000; ``false`` keeps exact search), each refresh trains cluster centres on a
  sample of the vectors (spherical k-means), assigns every chunk to its nearest centre and stores that index beside
  the vectors (``<vectors table>/cairn-ann/<snapshot>.npz``), for the snapshot it was built from. A search scores the
  query against the centres, then only the chunks in the ``probes`` nearest lists: work grows with
  ``probes / lists`` of the index instead of all of it, at the cost of sometimes missing a neighbour in a list it
  did not visit (``probes`` trades speed for recall). When the stored index is not for the table's current snapshot,
  the search is exact. Results say which (``method``).
* **Who defines indexes.** ``MANAGE_INGESTION``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, select, update

from cairn.common.db import metadata
from cairn.pipelines.pipes import TARGET, PipeError, open_table

logger = logging.getLogger(__name__)

vector_indexes = Table(
    "cairn_vector_indexes", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("source", String(512), nullable=False),
    Column("key_column", String(128), nullable=False),
    Column("text_column", String(128), nullable=False),
    Column("chunk_chars", Integer, nullable=False, default=1500),
    Column("every_minutes", Integer, nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("refreshed_ms", BigInteger, nullable=True),
    Column("source_snapshot", BigInteger, nullable=True),
    Column("chunks", Integer, nullable=True),
    Column("error", String(2000), nullable=True),
    Column("ann_lists", Integer, nullable=True),          # None: automatic; 0: exact search
    Column("ann_probes", Integer, nullable=True),
    Column("ann_snapshot", BigInteger, nullable=True),    # the vectors snapshot the stored IVF index was built from
)
ANN_MIN = int(os.environ.get("CAIRN_VECTORS_ANN_MIN", "20000"))


def kmeans(m, k: int, iters: int = 12, seed: int = 7, sample: int = 100_000):
    """Spherical k-means on unit vectors ``m`` (n x d): ``k`` unit centres, trained on a sample."""
    import numpy as np

    rng = np.random.default_rng(seed)
    train = m if len(m) <= sample else m[rng.choice(len(m), sample, replace=False)]
    k = max(1, min(k, len(train)))
    c = train[rng.choice(len(train), k, replace=False)].copy()
    for _ in range(iters):
        near = assign(train, c)
        for j in range(k):
            members = train[near == j]
            if len(members):
                v = members.sum(axis=0)
                c[j] = v / max(float(np.linalg.norm(v)), 1e-12)
            else:                                           # an empty list takes the worst-served point
                c[j] = train[int(np.argmin((train * c[near]).sum(axis=1)))]
    return c


def assign(m, centres, batch: int = 8192):
    """The nearest centre (by cosine) of each unit vector."""
    import numpy as np

    out = np.empty(len(m), dtype=np.int32)
    for i in range(0, len(m), batch):
        out[i:i + batch] = np.argmax(m[i:i + batch] @ centres.T, axis=1)
    return out


def _public(r) -> dict:
    d = dict(r._mapping)
    return {**d, "key": d["key_column"], "text": d["text_column"]}       # as they are given


def chunks(text: str, size: int) -> list[str]:
    """Pieces of about ``size`` characters, cut at whitespace, overlapping by a tenth."""
    text = (text or "").strip()
    if not text:
        return []
    out, start, overlap = [], 0, max(0, size // 10)
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind(" ", start + size // 2, end)
            end = cut if cut > start else end
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in out if c]


class VectorIndexes:
    def __init__(self, engine, catalog: dict, embedder, audit=None,
                 may_read: Optional[Callable[[Any, str], tuple[bool, bool]]] = None):
        """``may_read(principal, table) -> (read, restricted)``: Cairn's decision on the source table."""
        self.engine, self.catalog_props, self.embedder, self.audit = engine, catalog, embedder, audit
        self.may_read = may_read
        self._cache: dict[str, tuple] = {}       # index id -> (snapshot, matrix, rows, ivf or None)
        self._locks: dict[str, threading.Lock] = {}

    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        return RestCatalog("cairn-vectors", **self.catalog_props)

    # ---- definitions --------------------------------------------------------------------------
    def create(self, body: dict, actor: str) -> dict:
        name, source = str(body.get("name") or "").strip(), str(body.get("source") or "")
        if not name or len(name) > 128 or not TARGET.match(source):
            raise PipeError("an index needs a name and a source table (namespace.table)")
        key, text, size, every = self._check(body)
        lists, probes = self._ann(body.get("ann", "auto"))
        row = {"id": uuid.uuid4().hex, "name": name, "source": source, "key_column": key, "text_column": text,
               "chunk_chars": size, "every_minutes": every, "enabled": True, "created_by": actor,
               "created_ms": int(time.time() * 1000), "ann_lists": lists, "ann_probes": probes}
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(vector_indexes.insert().values(**row))
                if self.audit is not None:
                    self.audit.record("lakehouse", "vector_index.created", actor, None, row, conn=conn)
        except IntegrityError:
            raise PipeError("an index with that name exists", 409)
        return self.get(name)

    @staticmethod
    def _ann(value) -> tuple[Optional[int], Optional[int]]:
        """``"auto"`` (None, None), ``false`` (0, None) or ``{"lists", "probes"}``."""
        if value in (None, "auto"):
            return None, None
        if value is False:
            return 0, None
        if isinstance(value, dict):
            lists, probes = value.get("lists"), value.get("probes", 8)
            ok = all(isinstance(x, int) and not isinstance(x, bool) for x in (lists, probes))
            if ok and 2 <= lists <= 65536 and 1 <= probes <= lists:
                return lists, probes
        raise PipeError('ann: "auto", false, or {"lists": 2-65536, "probes": 1-lists}')

    def _plan(self, ix: dict, n: int) -> tuple[int, int]:
        """(lists, probes) for ``n`` chunks: (0, 0) for exact search."""
        if ix.get("ann_lists") == 0 or (ix.get("ann_lists") is None and n < ANN_MIN) or n < 2:
            return 0, 0
        if ix.get("ann_lists"):
            return min(ix["ann_lists"], n), min(ix.get("ann_probes") or 8, ix["ann_lists"])
        lists = max(2, int(math.sqrt(n)))
        return lists, max(4, lists // 16)

    @staticmethod
    def _check(body: dict) -> tuple[str, str, int, Optional[int]]:
        key, text = str(body.get("key") or ""), str(body.get("text") or "")
        try:
            size = int(body.get("chunk_chars") or 1500)
        except (TypeError, ValueError):
            raise PipeError("chunk_chars is 200-20000")
        every = body.get("every_minutes")
        if not key or not text:
            raise PipeError("key and text are the source's key column and text column")
        if not 200 <= size <= 20000:
            raise PipeError("chunk_chars is 200-20000")
        if every is not None and (isinstance(every, bool) or not isinstance(every, int) or not 5 <= every <= 10080):
            raise PipeError("every_minutes is 5-10080")
        return key, text, size, every

    def get(self, name: str) -> dict:
        """An index by its name (or its id)."""
        with self.engine.connect() as conn:
            r = conn.execute(select(vector_indexes).where(vector_indexes.c.name == name)).first()
            if r is None:
                r = conn.execute(select(vector_indexes).where(vector_indexes.c.id == name)).first()
        if r is None:
            raise PipeError("no such vector index", 404)
        return _public(r)

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [_public(r) for r in conn.execute(select(vector_indexes).order_by(vector_indexes.c.name))]

    def update(self, ref: str, body: dict, actor: str) -> dict:
        """Changes an index's key, text, chunking, schedule or enabled state (its source stays: a new
        source is a new index). A changed key, text or chunking is embedded again on the next refresh."""
        ix = self.get(ref)
        if body.get("source") not in (None, ix["source"]):
            raise PipeError(f"{ix['name']} indexes {ix['source']}: delete it and create one to index another table", 409)
        if body.get("name") not in (None, ix["name"]):
            raise PipeError("an index keeps its name", 409)
        merged = {"name": ix["name"], "source": ix["source"], "key": body.get("key", ix["key_column"]),
                  "text": body.get("text", ix["text_column"]), "chunk_chars": body.get("chunk_chars", ix["chunk_chars"]),
                  "every_minutes": body.get("every_minutes", ix["every_minutes"])}
        key, text, size, every = self._check(merged)
        values = {"key_column": key, "text_column": text, "chunk_chars": size, "every_minutes": every,
                  "enabled": bool(body.get("enabled", ix["enabled"]))}
        if "ann" in body:
            values["ann_lists"], values["ann_probes"] = self._ann(body["ann"])
            values["ann_snapshot"] = None                    # built again, with the new settings, on the next refresh
        with self.engine.begin() as conn:
            conn.execute(update(vector_indexes).where(vector_indexes.c.id == ix["id"]).values(**values))
            if self.audit is not None:
                self.audit.record("lakehouse", "vector_index.updated", actor, None, {"name": ix["name"], **values}, conn=conn)
        self._cache.pop(ix["id"], None)
        return self.get(ix["name"])

    def delete(self, ref: str, actor: str) -> None:
        """Removes the index and its vectors table (it holds the source's raw text)."""
        from pyiceberg.exceptions import NoSuchTableError

        ix = self.get(ref)
        with self._locks.setdefault(ix["id"], threading.Lock()):
            try:
                self._catalog().purge_table(tuple(self.target(ix).split(".")))
            except NoSuchTableError:
                pass
            with self.engine.begin() as conn:
                conn.execute(vector_indexes.delete().where(vector_indexes.c.id == ix["id"]))
                if self.audit is not None:
                    self.audit.record("lakehouse", "vector_index.deleted", actor, None, {"name": ix["name"]}, conn=conn)
        self._cache.pop(ix["id"], None)

    def target(self, ix: dict) -> str:
        return f"{ix['source']}__{re.sub(r'[^a-z0-9_]', '_', ix['name'].lower())}_vectors"     # one per index

    # ---- keeping it in step ---------------------------------------------------------------------
    def refresh(self, name: str) -> dict:
        import pyarrow as pa
        from pyiceberg.exceptions import NoSuchTableError

        ix = self.get(name)
        lock = self._locks.setdefault(ix["id"], threading.Lock())
        with lock:
            cat = self._catalog()
            src = cat.load_table(tuple(ix["source"].split(".")))
            snap = src.current_snapshot()
            sid = snap.snapshot_id if snap else 0
            rows = src.scan(selected_fields=(ix["key_column"], ix["text_column"])).to_arrow().to_pylist()
            want: dict[str, tuple[str, str]] = {}
            for r in rows:
                k, t = r.get(ix["key_column"]), r.get(ix["text_column"])
                if k is None:
                    continue
                t = "" if t is None else str(t)
                want[str(k)] = (t, hashlib.sha256(f"{ix['chunk_chars']}:{t}".encode()).hexdigest())
            schema = pa.schema([("key", pa.string()), ("chunk", pa.int32()), ("text", pa.large_string()),
                                ("hash", pa.string()), ("embedding", pa.list_(pa.float32())),
                                ("model", pa.string())])
            ident = tuple(self.target(ix).split("."))
            try:
                vt = cat.load_table(ident)
                have = vt.scan().to_arrow().to_pylist()
            except NoSuchTableError:
                vt, have = None, []
            model = f"{getattr(self.embedder, 'name', 'embedder')}:{getattr(self.embedder, 'dim', '')}"
            kept = [h for h in have if h["key"] in want and want[h["key"]][1] == h["hash"] and h.get("model") == model]
            kept_keys = {h["key"] for h in kept}
            todo = [(k, t, h) for k, (t, h) in want.items() if k not in kept_keys]
            pieces = [(k, i, c, h) for k, t, h in todo for i, c in enumerate(chunks(t, ix["chunk_chars"]))]
            vectors: list[list[float]] = []
            for i in range(0, len(pieces), 64):
                vectors += self.embedder.embed([p[2] for p in pieces[i:i + 64]])
            new = [{"key": k, "chunk": i, "text": c, "hash": h, "embedding": v, "model": model}
                   for (k, i, c, h), v in zip(pieces, vectors)]
            data = pa.Table.from_pylist(kept + new, schema=schema)
            if vt is None:
                vt = open_table(cat, self.target(ix), schema, True)
            if len(kept) == len(have) and not new:
                pass                                                  # nothing changed
            else:
                vt.overwrite(data)
            n = len(kept) + len(new)
            ann = self._build_ann(ix, vt.refresh(), n)
            with self.engine.begin() as conn:
                conn.execute(update(vector_indexes).where(vector_indexes.c.id == ix["id"]).values(
                    refreshed_ms=int(time.time() * 1000), source_snapshot=sid, chunks=n, error=None, **ann))
            self._cache.pop(ix["id"], None)
        return {"index": name, "chunks": n, "embedded": len(new), "kept": len(kept),
                "removed": len({h["key"] for h in have} - set(want)),
                "method": "ivf" if ann.get("ann_snapshot") else "exact"}

    def _ann_path(self, vt, snapshot: int) -> str:
        return f"{vt.location().rstrip('/')}/cairn-ann/{snapshot}.npz"

    def _build_ann(self, ix: dict, vt, n: int) -> dict:
        """Trains and stores the IVF index for the vectors' current snapshot (when the index uses one)."""
        import numpy as np

        lists, _ = self._plan(ix, n)
        snap = vt.current_snapshot()
        if not lists or snap is None:
            return {"ann_snapshot": None}
        if ix.get("ann_snapshot") == snap.snapshot_id:
            return {}                                          # already built for this snapshot
        rows = vt.scan(selected_fields=("key", "chunk", "embedding")).to_arrow().to_pylist()
        m = np.array([r["embedding"] for r in rows], dtype=np.float32)
        m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
        centres = kmeans(m, lists)
        near = assign(m, centres)
        buf = io.BytesIO()
        np.savez_compressed(buf, centres=centres, lists=near, keys=np.array([str(r["key"]) for r in rows], dtype=str),
                            chunks=np.array([r["chunk"] for r in rows], dtype=np.int32))
        with vt.io.new_output(self._ann_path(vt, snap.snapshot_id)).create(overwrite=True) as out:
            out.write(buf.getvalue())
        logger.info("vector index %s: IVF with %d lists over %d chunks", ix["name"], len(centres), n)
        return {"ann_snapshot": snap.snapshot_id}

    def run_due(self) -> int:
        n = 0
        now = int(time.time() * 1000)
        for ix in self.list():
            if not ix["enabled"] or not ix["every_minutes"]:
                continue
            if ix["refreshed_ms"] and now - ix["refreshed_ms"] < ix["every_minutes"] * 60_000:
                continue
            try:
                self.refresh(ix["name"])
                n += 1
            except Exception as e:  # noqa: BLE001 - recorded; the others go on
                logger.warning("vector index %s: %s", ix["name"], e)
                with self.engine.begin() as conn:
                    conn.execute(update(vector_indexes).where(vector_indexes.c.id == ix["id"]).values(error=str(e)[:2000]))
        return n

    # ---- search ---------------------------------------------------------------------------------
    def _matrix(self, ix: dict):
        """(unit vectors, rows, ivf): ivf is (centres, list -> row indexes) when an index for this snapshot exists."""
        import numpy as np

        vt = self._catalog().load_table(tuple(self.target(ix).split(".")))
        snap = vt.current_snapshot()
        sid = snap.snapshot_id if snap else 0
        hit = self._cache.get(ix["id"])
        if hit and hit[0] == sid:
            return hit[1], hit[2], hit[3]
        rows = vt.scan(selected_fields=("key", "chunk", "text", "embedding")).to_arrow().to_pylist()
        m = np.array([r["embedding"] for r in rows], dtype=np.float32) if rows else np.zeros((0, 1), np.float32)
        if len(m):
            m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
        meta = [{"key": r["key"], "chunk": r["chunk"], "text": r["text"]} for r in rows]
        ivf = None
        if rows and ix.get("ann_snapshot") == sid:
            try:
                with vt.io.new_input(self._ann_path(vt, sid)).open() as f:
                    z = np.load(io.BytesIO(f.read()), allow_pickle=False)     # arrays only: nothing to unpickle
                where = {(str(k), int(c)): i for i, (k, c) in enumerate(zip(z["keys"], z["chunks"]))}
                order = [where.get((r["key"], int(r["chunk"]))) for r in rows]
                if None not in order:                       # the index covers exactly these rows
                    lists = z["lists"][np.array(order)]
                    members = {int(j): np.flatnonzero(lists == j) for j in np.unique(lists)}
                    ivf = (z["centres"], members)
            except Exception as e:  # noqa: BLE001 - a missing or unreadable index: exact search
                logger.warning("vector index %s: its IVF index could not be read (%s); searching exactly", ix["name"], e)
        self._cache[ix["id"]] = (sid, m, meta, ivf)
        return m, meta, ivf

    def search(self, p, name: str, q: str, k: int = 10) -> dict:
        import numpy as np

        ix = self.get(name)
        if self.may_read is not None:
            read, restricted = self.may_read(p, ix["source"])
            if not read:
                raise PipeError(f"searching {name} needs read access to {ix['source']}", 403)
            if restricted:
                raise PipeError(f"a mask or row filter applies to you on {ix['source']}: its index holds the raw text, "
                                "so it is not searched for you", 403)
        if not q.strip():
            raise PipeError("q: what to search for")
        if ix["refreshed_ms"] is None:
            raise PipeError(f"{name} has not been built yet (refresh it)", 409)
        m, meta, ivf = self._matrix(ix)
        if not len(meta):
            return {"index": name, "results": [], "method": "exact"}
        v = np.array(self.embedder.embed([q])[0], dtype=np.float32)
        v /= max(float(np.linalg.norm(v)), 1e-12)
        k = max(1, min(int(k), 100))
        if ivf is None:
            scores = m @ v
            top = np.argsort(-scores)[:k]
            return {"index": name, "method": "exact",
                    "results": [{**meta[i], "score": round(float(scores[i]), 4)} for i in top]}
        centres, members = ivf
        _, probes = self._plan(ix, len(meta))
        nearest = np.argsort(-(centres @ v))[:probes]
        cand = np.concatenate([members.get(int(j), np.zeros(0, dtype=np.int64)) for j in nearest])
        scores = m[cand] @ v
        top = np.argsort(-scores)[:k]
        return {"index": name, "method": "ivf", "searched": int(len(cand)),
                "results": [{**meta[cand[i]], "score": round(float(scores[i]), 4)} for i in top]}


def build_vectors_router(ix: VectorIndexes, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/vectors", tags=["lakehouse"])

    def wrap(fn):
        try:
            return fn()
        except PipeError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_indexes(p=Depends(current_principal)):
        return ix.list()

    @router.post("", status_code=201)
    def create(body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return wrap(lambda: ix.create(body, p.urn))

    @router.put("/{ref}")
    def change(ref: str, body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return wrap(lambda: ix.update(ref, body, p.urn))

    @router.delete("/{ref}", status_code=204)
    def remove(ref: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        wrap(lambda: ix.delete(ref, p.urn))

    @router.post("/{name}/refresh")
    def refresh(name: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return wrap(lambda: ix.refresh(name))

    @router.get("/{name}/search")
    def search(name: str, q: str, k: int = 10, p=Depends(current_principal)):
        return wrap(lambda: ix.search(p, name, q, k))

    return router
