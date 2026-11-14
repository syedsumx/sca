"""Publishing data products outside the company: Snowflake listings and Delta Sharing shares.

Cairn turns a data product into what the target needs, from what the catalog already knows:

* **Snowflake**: a share with the product's tables, and an external listing whose manifest carries
  the description, the data dictionary, the contract and sample queries mined from real use.
* **Databricks**: a Delta Sharing share with the product's tables, and grants to recipients.

``plan`` shows the statements (and the listing manifest) without running anything. ``publish``
runs them through a named warehouse connection (or records a publication done by hand) and keeps
a fingerprint of what was published. When the product changes (tables added or removed, docs,
contract), the plan's fingerprint no longer matches: ``outdated`` lists those publications and the
notifier tells the product's owners. Publishing again brings the target up to date: tables are added
and removed to match, and the listing manifest is replaced.

Only tables on the target's platform are published; anything else is reported as skipped.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Literal, Optional

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, String, Table, select, update

from cairn.common.db import JSONText, metadata

TARGETS = {"snowflake": "snowflake", "databricks": "databricks"}
IDENT = r"^[A-Za-z_][A-Za-z0-9_]{0,254}$"           # share and listing names: plain identifiers

publications = Table(
    "cairn_product_publications", metadata,
    Column("product_urn", String(512), primary_key=True),
    Column("target", String(16), primary_key=True),
    Column("options", JSONText, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("tables", JSONText, nullable=False),
    Column("published_by", String(512), nullable=False),
    Column("published_ms", BigInteger, nullable=False),
    Column("notified_fingerprint", String(64), nullable=True),
)


class PublishError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class PublishOptions(BaseModel):
    share: str = Field(pattern=IDENT, description="share name in the target")
    listing: Optional[str] = Field(default=None, pattern=IDENT, description="snowflake: listing name")
    consumers: list[str] = Field(default_factory=list,
                                 description="snowflake: consumer accounts (ORG.ACCOUNT); databricks: recipients")
    terms: Literal["STANDARD", "OFFLINE", "CUSTOM"] = "STANDARD"
    terms_link: Optional[str] = Field(default=None, pattern=r"^https://")
    refresh_rate: Optional[str] = Field(default=None, max_length=40, description="e.g. DAILY, HOURLY")


def _quote_sf(name: str) -> str:
    return ".".join(f'"{p.upper()}"' for p in name.split("."))


def _quote_db(name: str) -> str:
    return ".".join(f"`{p}`" for p in name.split("."))


def _sql_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "''") + "'"


class ProductPublisher:
    def __init__(self, engine, graph, contracts=None, audit=None, notify=None,
                 run_sql: Optional[Callable[[str, list[str]], list[Optional[str]]]] = None):
        self.engine = engine
        self.graph = graph
        self.contracts = contracts
        self.audit = audit
        self.notify = notify
        self.run_sql = run_sql

    # ---- what the product is ----------------------------------------------------------
    def _product(self, urn: str) -> dict:
        props = self.graph.get_aspect(urn, "dataProductProperties")
        if not props:
            raise PublishError(f"data product {urn} not found", 404)
        return props

    def _tables(self, props: dict, platform: str) -> tuple[list[dict], list[str]]:
        tables, skipped = [], []
        for a in props.get("assets") or []:
            urn = a.get("destinationUrn") or ""
            if not urn.startswith(f"urn:li:dataset:(urn:li:dataPlatform:{platform},"):
                skipped.append(urn)
                continue
            name = urn.split(",")[1]
            if name.count(".") != 2:
                skipped.append(urn)
                continue
            ds = self.graph.get_aspect(urn, "datasetProperties") or {}
            ed = self.graph.get_aspect(urn, "editableDatasetProperties") or {}
            schema = self.graph.get_aspect(urn, "schemaMetadata") or {}
            tables.append({"urn": urn, "name": name, "description": ed.get("description") or ds.get("description") or "",
                           "columns": [{"name": f.get("fieldPath"), "type": f.get("nativeDataType")}
                                       for f in schema.get("fields", []) if f.get("fieldPath")],
                           "contract": self._contract(urn)})
        return sorted(tables, key=lambda t: t["name"]), skipped

    def _contract(self, urn: str) -> Optional[dict]:
        if self.contracts is None:
            return None
        c = self.contracts.get(urn)
        if not c:
            return None
        spec = c.get("spec") or {}
        return {"version": c.get("version"), "status": c.get("status"),
                "freshness": (spec.get("freshness") or {}).get("max_age"), "owner": c.get("owner")}

    def _examples(self, tables: list[dict], limit: int = 3) -> list[dict]:
        from cairn.context.layer import query_patterns

        wanted = {t["urn"] for t in tables}
        with self.engine.connect() as conn:
            rows = conn.execute(select(query_patterns.c.title, query_patterns.c.sql, query_patterns.c.tables)
                                .order_by(query_patterns.c.updated_ms.desc()).limit(500)).all()
        out = []
        for title, sql, used in rows:
            if set(used or []) and set(used) <= wanted:
                out.append({"title": title[:110], "query": sql})
            if len(out) >= limit:
                break
        return out

    # ---- planning ---------------------------------------------------------------------
    def plan(self, product_urn: str, target: str, opts: PublishOptions) -> dict:
        if target not in TARGETS:
            raise PublishError(f"target must be one of {sorted(TARGETS)}")
        props = self._product(product_urn)
        tables, skipped = self._tables(props, TARGETS[target])
        if not tables:
            raise PublishError(f"the product has no {target} tables to publish")
        published = self._publication(product_urn, target)
        before = set(published["tables"]) if published else set()
        names = [t["name"] for t in tables]
        if target == "snowflake":
            manifest = self._snowflake_manifest(props, tables, opts)
            statements = self._snowflake_sql(names, before, manifest, opts, update_listing=published is not None)
        else:
            manifest = None
            statements = self._databricks_sql(props, tables, before, opts)
        doc = {"tables": tables, "manifest": manifest, "options": opts.model_dump()}
        fingerprint = hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()
        return {"product": product_urn, "target": target, "tables": names, "skipped": skipped,
                "manifest": manifest, "statements": statements, "fingerprint": fingerprint,
                "published": {k: published[k] for k in ("fingerprint", "published_by", "published_ms")}
                if published else None,
                "in_sync": bool(published and published["fingerprint"] == fingerprint)}

    def _snowflake_manifest(self, props: dict, tables: list[dict], opts: PublishOptions) -> str:
        custom = {p.get("key"): p.get("value") for p in props.get("customProperties") or []} \
            if isinstance(props.get("customProperties"), list) else (props.get("customProperties") or {})
        desc = props.get("description") or ""
        contracts = [f"- {t['name']}: contract v{t['contract']['version']}"
                     + (f", fresh within {t['contract']['freshness']}" if t['contract'].get('freshness') else "")
                     for t in tables if t.get("contract")]
        if contracts:
            desc += "\n\n**Data contracts**\n" + "\n".join(contracts)
        m: dict[str, Any] = {
            "title": (props.get("name") or "Data product")[:110],
            "subtitle": (desc.splitlines()[0] if desc else props.get("name") or "")[:110],
            "description": desc or props.get("name") or "",
            "listing_terms": {"type": opts.terms, **({"link": opts.terms_link} if opts.terms_link else {})},
            "data_dictionary": {"featured": {"database_objects": [
                {"name": t["name"].upper(), "domain": "TABLE"} for t in tables[:5]]}},
        }
        if opts.consumers:
            m["targets"] = {"accounts": opts.consumers}
        examples = self._examples(tables)
        if examples:
            m["usage_examples"] = [{"title": e["title"], "description": e["title"], "query": e["query"]}
                                   for e in examples]
        if opts.refresh_rate or custom.get("sla.freshness"):
            m["data_attributes"] = {"refresh_rate": (opts.refresh_rate or str(custom.get("sla.freshness"))).upper()}
        return yaml.safe_dump(m, sort_keys=False, allow_unicode=True)

    def _snowflake_sql(self, names: list[str], before: set[str], manifest: str, opts: PublishOptions,
                       update_listing: bool) -> list[str]:
        share = f'"{opts.share.upper()}"'
        out = [f"CREATE SHARE IF NOT EXISTS {share}"]
        for db in sorted({n.split(".")[0] for n in names}):
            out.append(f'GRANT USAGE ON DATABASE "{db.upper()}" TO SHARE {share}')
        for sch in sorted({".".join(n.split(".")[:2]) for n in names}):
            out.append(f"GRANT USAGE ON SCHEMA {_quote_sf(sch)} TO SHARE {share}")
        for n in names:
            out.append(f"GRANT SELECT ON TABLE {_quote_sf(n)} TO SHARE {share}")
        for n in sorted(before - set(names)):
            out.append(f"REVOKE SELECT ON TABLE {_quote_sf(n)} FROM SHARE {share}")
        listing = f'"{(opts.listing or opts.share).upper()}"'
        body = "$$\n" + manifest + "$$"
        if update_listing:
            out.append(f"ALTER LISTING {listing} AS {body}")
        else:
            out.append(f"CREATE EXTERNAL LISTING IF NOT EXISTS {listing} SHARE {share} AS {body} "
                       "PUBLISH = FALSE REVIEW = FALSE")
        return out

    def _databricks_sql(self, props: dict, tables: list[dict], before: set[str], opts: PublishOptions) -> list[str]:
        share = f"`{opts.share}`"
        comment = (props.get("description") or props.get("name") or "")[:1000]
        out = [f"CREATE SHARE IF NOT EXISTS {share} COMMENT {_sql_str(comment)}",
               f"COMMENT ON SHARE {share} IS {_sql_str(comment)}"]
        for t in tables:
            if t["name"] not in before:
                out.append(f"ALTER SHARE {share} ADD TABLE {_quote_db(t['name'])}"
                           + (f" COMMENT {_sql_str(t['description'][:1000])}" if t["description"] else ""))
        for n in sorted(before - {t["name"] for t in tables}):
            out.append(f"ALTER SHARE {share} REMOVE TABLE {_quote_db(n)}")
        for r in opts.consumers:
            out.append(f"GRANT SELECT ON SHARE {share} TO RECIPIENT `{r}`")
        return out

    # ---- publishing -------------------------------------------------------------------
    def _publication(self, product_urn: str, target: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(publications).where(publications.c.product_urn == product_urn,
                                                        publications.c.target == target)).first()
        return dict(r._mapping) if r else None

    def publish(self, product_urn: str, target: str, opts: PublishOptions, actor: str,
                connection: Optional[str] = None) -> dict:
        """Run the plan through ``connection`` (or record that it was applied by hand)."""
        p = self.plan(product_urn, target, opts)
        errors: list[Optional[str]] = []
        if connection:
            if self.run_sql is None:
                raise PublishError("no warehouse connections are configured")
            errors = self.run_sql(connection, p["statements"])
            if any(errors):
                failed = [{"sql": s[:300], "error": e} for s, e in zip(p["statements"], errors) if e]
                if self.audit:
                    self.audit.record("marketplace", "product.publish_failed", actor, product_urn,
                                      {"target": target, "failed": failed})
                return {**p, "applied": False, "failed": failed}
        row = dict(product_urn=product_urn, target=target, options=opts.model_dump(), fingerprint=p["fingerprint"],
                   tables=p["tables"], published_by=actor, published_ms=int(time.time() * 1000),
                   notified_fingerprint=None)
        with self.engine.begin() as conn:
            if self._publication(product_urn, target):
                conn.execute(update(publications).where(publications.c.product_urn == product_urn,
                                                        publications.c.target == target).values(**row))
            else:
                conn.execute(publications.insert().values(**row))
        if self.audit:
            self.audit.record("marketplace", "product.published", actor, product_urn,
                              {"target": target, "share": opts.share, "tables": p["tables"],
                               "via": connection or "by hand"})
        return {**self.plan(product_urn, target, opts), "applied": True, "failed": []}

    def publications(self, product_urn: Optional[str] = None) -> list[dict]:
        stmt = select(publications)
        if product_urn:
            stmt = stmt.where(publications.c.product_urn == product_urn)
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(stmt)]
        out = []
        for r in rows:
            try:
                current = self.plan(r["product_urn"], r["target"], PublishOptions(**r["options"]))
                in_sync, problem = current["fingerprint"] == r["fingerprint"], None
            except PublishError as e:
                in_sync, problem = False, str(e)
            out.append({"product": r["product_urn"], "target": r["target"], "share": r["options"]["share"],
                        "tables": r["tables"], "published_by": r["published_by"], "published_ms": r["published_ms"],
                        "in_sync": in_sync, "problem": problem})
        return out

    def sweep(self) -> int:
        """Tell owners once about each publication that no longer matches its product."""
        if self.notify is None:
            return 0
        from cairn.notify.hub import Event

        told = 0
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(publications))]
        for r in rows:
            try:
                fp = self.plan(r["product_urn"], r["target"], PublishOptions(**r["options"]))["fingerprint"]
            except PublishError:
                continue
            if fp == r["fingerprint"] or fp == r["notified_fingerprint"]:
                continue
            self.notify.publish(Event(
                type="product.listing_outdated", entity_urn=r["product_urn"], include_owners=True,
                title=f"The {r['target']} share {r['options']['share']} no longer matches its data product",
                body="Tables, documentation or contracts changed since it was published. Publish it again "
                     "from the product page."))
            with self.engine.begin() as conn:
                conn.execute(update(publications).where(publications.c.product_urn == r["product_urn"],
                                                        publications.c.target == r["target"])
                             .values(notified_fingerprint=fp))
            told += 1
        return told
