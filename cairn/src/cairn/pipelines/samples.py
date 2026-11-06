"""Sample datasets for new tenants (queue row 26): a lakehouse to try Cairn on before your own data arrives.

``retail`` is three tables in the ``samples`` namespace: ``customers`` (2,000), ``products`` (120) and ``orders``
(20,000 over the last year). Every tenant gets the same data: it is generated from a fixed seed, nothing comes
from a person. Loading it:

* writes the tables through the lakehouse catalog (the one pipes write to), replacing earlier sample rows;
* documents each table and column in the catalog, as a data owner would, so search, lineage and Ask-your-data
  have something to show;
* saves starter queries, shared, on the lakehouse connection.

``POST /v1/samples/retail`` loads it, ``DELETE`` removes the tables and queries, and ``GET /v1/samples`` lists the
sets and their state; all three need ``MANAGE_SYSTEM_OPERATIONS``. ``cairn samples load|remove`` does the same
from a shell. ``CAIRN_SAMPLES_ON_START=retail`` (``tenant.sh --samples``) loads it once when a tenant first
starts; once removed it stays removed.
"""

from __future__ import annotations

import datetime as dt
import random
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, select

from cairn.common.db import metadata

sample_sets = Table(
    "cairn_sample_sets", metadata,
    Column("name", String(64), primary_key=True),
    Column("state", String(16), nullable=False),        # loaded | removed
    Column("actor", String(256), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

NAMESPACE = "samples"
SEED = 20261106
# The starter queries' owner, whoever loads the set: any administrator can then replace or remove them (a saved
# query is changed and deleted by its owner only).
OWNER = "urn:li:corpuser:cairn-samples"
REGIONS = ["EU", "US", "APAC", "LATAM"]
CATEGORIES = ["Outdoor", "Kitchen", "Books", "Garden", "Toys", "Office"]
FIRST = ["Ada", "Ben", "Chloe", "Dev", "Ema", "Finn", "Gita", "Hugo", "Iris", "Jon", "Kai", "Lena", "Milo", "Nia"]
LAST = ["Arden", "Brook", "Cole", "Dale", "Ellis", "Ford", "Grey", "Hale", "Ivers", "Joss", "Kerr", "Lowe"]

DOCS = {
    "customers": ("People who have bought from the sample shop: one row per customer.", {
        "customer_id": "The customer's key; orders.customer_id refers to it.",
        "name": "The customer's name (made up).",
        "email": "Their contact address, at the reserved example.com domain.",
        "region": "Where they buy from: EU, US, APAC or LATAM.",
        "signed_up": "The day they made an account."}),
    "products": ("What the sample shop sells: one row per product.", {
        "product_id": "The product's key; orders.product_id refers to it.",
        "name": "The product's name.",
        "category": "Its department.",
        "price": "List price in euros."}),
    "orders": ("Every order line in the last year: one row per order.", {
        "order_id": "The order's key.",
        "customer_id": "Who ordered (customers.customer_id).",
        "product_id": "What they ordered (products.product_id).",
        "quantity": "How many.",
        "amount": "What they paid in euros: price times quantity, less any discount.",
        "ordered_at": "When the order was placed (UTC).",
        "status": "placed, shipped, delivered or returned."}),
}

QUERIES = [
    ("Sample: revenue by region and month", "Delivered revenue per region per month.",
     "SELECT c.region, date_trunc('month', o.ordered_at) AS month, round(sum(o.amount), 2) AS revenue "
     "FROM {cat}.samples.orders o JOIN {cat}.samples.customers c ON o.customer_id = c.customer_id "
     "WHERE o.status = 'delivered' GROUP BY 1, 2 ORDER BY 2, 1"),
    ("Sample: best-selling products", "The ten products with the most units sold.",
     "SELECT p.name, p.category, sum(o.quantity) AS units FROM {cat}.samples.orders o "
     "JOIN {cat}.samples.products p ON o.product_id = p.product_id GROUP BY 1, 2 ORDER BY units DESC LIMIT 10"),
    ("Sample: return rate by category", "Share of orders returned, per product category.",
     "SELECT p.category, round(avg(CASE WHEN o.status = 'returned' THEN 1.0 ELSE 0.0 END), 3) AS return_rate "
     "FROM {cat}.samples.orders o JOIN {cat}.samples.products p ON o.product_id = p.product_id GROUP BY 1 ORDER BY 2 DESC"),
]


class SampleError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def retail(today: Optional[dt.date] = None) -> dict[str, Any]:
    """The retail tables as Arrow, the same for every tenant (one seed; dates end today)."""
    import pyarrow as pa

    rnd = random.Random(SEED)
    today = today or dt.datetime.now(dt.timezone.utc).date()
    customers = {"customer_id": [], "name": [], "email": [], "region": [], "signed_up": []}
    for i in range(1, 2001):
        first, last = rnd.choice(FIRST), rnd.choice(LAST)
        customers["customer_id"].append(i)
        customers["name"].append(f"{first} {last}")
        customers["email"].append(f"{first.lower()}.{last.lower()}{i}@example.com")
        customers["region"].append(rnd.choices(REGIONS, weights=[4, 3, 2, 1])[0])
        customers["signed_up"].append(today - dt.timedelta(days=rnd.randint(30, 1500)))
    products = {"product_id": [], "name": [], "category": [], "price": []}
    for i in range(1, 121):
        cat = CATEGORIES[(i - 1) % len(CATEGORIES)]
        products["product_id"].append(i)
        products["name"].append(f"{cat} item {i:03d}")
        products["category"].append(cat)
        products["price"].append(round(rnd.uniform(4, 250), 2))
    orders = {"order_id": [], "customer_id": [], "product_id": [], "quantity": [], "amount": [], "ordered_at": [],
              "status": []}
    start = dt.datetime.combine(today - dt.timedelta(days=365), dt.time(), dt.timezone.utc)
    for i in range(1, 20001):
        pid, qty = rnd.randint(1, 120), rnd.choices([1, 2, 3, 4, 5], weights=[60, 20, 10, 6, 4])[0]
        discount = rnd.choice([0, 0, 0, 0.1, 0.2])
        orders["order_id"].append(i)
        orders["customer_id"].append(rnd.randint(1, 2000))
        orders["product_id"].append(pid)
        orders["quantity"].append(qty)
        orders["amount"].append(round(products["price"][pid - 1] * qty * (1 - discount), 2))
        orders["ordered_at"].append(start + dt.timedelta(seconds=rnd.randint(0, 365 * 86400 - 1)))
        orders["status"].append(rnd.choices(["placed", "shipped", "delivered", "returned"], weights=[5, 10, 80, 5])[0])
    ts = pa.timestamp("us", tz="UTC")
    return {
        "customers": pa.table(customers, schema=pa.schema([("customer_id", pa.int64()), ("name", pa.string()),
                                                           ("email", pa.string()), ("region", pa.string()),
                                                           ("signed_up", pa.date32())])),
        "products": pa.table(products, schema=pa.schema([("product_id", pa.int64()), ("name", pa.string()),
                                                         ("category", pa.string()), ("price", pa.float64())])),
        "orders": pa.table(orders, schema=pa.schema([("order_id", pa.int64()), ("customer_id", pa.int64()),
                                                     ("product_id", pa.int64()), ("quantity", pa.int32()),
                                                     ("amount", pa.float64()), ("ordered_at", ts), ("status", pa.string())])),
    }


SETS: dict[str, Callable[[], dict[str, Any]]] = {"retail": retail}


class Samples:
    def __init__(self, engine, catalog_props: Optional[dict], graph=None, workspace=None, audit=None,
                 connection: str = "lakehouse", trino_catalog: str = "lakehouse",
                 urn: Optional[Callable[[str, str], str]] = None):
        self.engine, self.catalog_props, self.graph, self.workspace, self.audit = engine, catalog_props, graph, workspace, audit
        self.connection, self.trino_catalog = connection, trino_catalog
        self.urn = urn or (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")

    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        if not self.catalog_props:
            raise SampleError("sample data needs the lakehouse catalog Cairn writes to (CAIRN_PIPES_CATALOG)", 503)
        return RestCatalog("cairn-samples", **self.catalog_props)

    def _state(self, name: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(sample_sets).where(sample_sets.c.name == name)).first()
        return dict(r._mapping) if r else None

    def _set_state(self, name: str, state: str, actor: str, details: dict) -> None:
        row = {"name": name, "state": state, "actor": actor, "updated_ms": int(time.time() * 1000)}
        with self.engine.begin() as conn:
            if conn.execute(select(sample_sets.c.name).where(sample_sets.c.name == name)).first():
                conn.execute(sample_sets.update().where(sample_sets.c.name == name).values(**row))
            else:
                conn.execute(sample_sets.insert().values(**row))
            if self.audit is not None:
                self.audit.record("pipelines", f"samples.{state}", actor, f"urn:li:sampleSet:{name}", details, conn=conn)

    def list(self) -> list[dict]:
        out = []
        for name in SETS:
            st = self._state(name) or {}
            out.append({"name": name, "state": st.get("state", "not loaded"), "by": st.get("actor"),
                        "updated_ms": st.get("updated_ms"), "namespace": NAMESPACE, "tables": sorted(DOCS)})
        return out

    def load(self, actor: str, name: str) -> dict:
        from cairn.pipelines.pipes import open_table

        if name not in SETS:
            raise SampleError(f"no sample set {name!r} (there is: {', '.join(SETS)})", 404)
        cat = self._catalog()
        tables = SETS[name]()
        rows = {}
        for table, data in tables.items():
            t = open_table(cat, f"{NAMESPACE}.{table}", data.schema, True)
            t.overwrite(data)                                   # a reload replaces the sample rows
            rows[table] = data.num_rows
        self._document()
        queries = self._save_queries(actor)
        self._set_state(name, "loaded", actor, {"rows": rows, "queries": len(queries)})
        return {"name": name, "namespace": NAMESPACE, "rows": rows, "queries": queries}

    def remove(self, actor: str, name: str) -> dict:
        if name not in SETS:
            raise SampleError(f"no sample set {name!r}", 404)
        cat = self._catalog()
        from pyiceberg.exceptions import NoSuchTableError

        dropped = []
        for table in DOCS:
            try:
                cat.purge_table((NAMESPACE, table))
                dropped.append(table)
            except NoSuchTableError:
                pass
        removed = self._remove_queries(actor)
        self._set_state(name, "removed", actor, {"tables": dropped, "queries": removed})
        return {"name": name, "tables_dropped": dropped, "queries_removed": removed}

    def load_on_start(self, names: list[str], actor: str = OWNER) -> list[str]:
        """``CAIRN_SAMPLES_ON_START``: each named set, once per tenant; never again once loaded or removed."""
        done = []
        for name in names:
            if name in SETS and self._state(name) is None:
                self.load(actor, name)
                done.append(name)
        return done

    # ---- catalog documentation and starter queries ------------------------------------------------------
    def _document(self) -> None:
        if self.graph is None:
            return
        from cairn.common.proposals import audit_stamp, make_mcp

        stamp = audit_stamp(OWNER)
        mcps = []
        for table, (desc, cols) in DOCS.items():
            urn = self.urn(NAMESPACE, table)
            mcps.append(make_mcp(urn, "editableDatasetProperties", {"description": desc, "created": stamp, "lastModified": stamp}))
            mcps.append(make_mcp(urn, "editableSchemaMetadata", {
                "editableSchemaFieldInfo": [{"fieldPath": c, "description": d} for c, d in cols.items()],
                "created": stamp, "lastModified": stamp}))
            mcps.append(make_mcp(urn, "globalTags", {"tags": [{"tag": "urn:li:tag:sample-data"}]}))
        self.graph.emit(mcps)

    def _save_queries(self, actor: str) -> list[str]:
        if self.workspace is None:
            return []
        self._remove_queries(actor)
        out = []
        for qname, desc, sql in QUERIES:
            q = self.workspace.save(OWNER, {"name": qname, "description": desc, "sql": sql.format(cat=self.trino_catalog),
                                            "connection": self.connection, "shared": True})
            out.append(q["id"])
        return out

    def _remove_queries(self, actor: str) -> int:
        if self.workspace is None:
            return 0
        names = {q[0] for q in QUERIES}
        removed = 0
        for q in self.workspace.saved(OWNER):             # the set's own queries, not people's of the same name
            if q.get("name") in names and q.get("owner") == OWNER:
                self.workspace.delete(OWNER, q["id"])
                removed += 1
        return removed


def build_samples_router(s: Samples, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/samples", tags=["lakehouse"])

    def admin(pr):
        authorize(pr, "MANAGE_SYSTEM_OPERATIONS", None)
        return pr.urn

    async def call(fn, *a):
        try:
            return await run_in_threadpool(fn, *a)
        except SampleError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def listing(p=Depends(current_principal)):
        admin(p)
        return await call(s.list)

    @router.post("/{name}")
    async def load(name: str, p=Depends(current_principal)):
        return await call(s.load, admin(p), name)

    @router.delete("/{name}")
    async def remove(name: str, p=Depends(current_principal)):
        return await call(s.remove, admin(p), name)

    return router
