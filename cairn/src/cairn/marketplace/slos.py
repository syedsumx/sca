"""Service level objectives for data products, with error budgets.

A product's owner sets objectives over a rolling window, for example "99% of freshness checks pass
over 28 days". An SLO counts the evaluations of the quality monitors on the product's datasets:

* ``freshness``: freshness monitors; ``volume``: volume monitors; ``quality``: field, distribution,
  custom SQL and schema monitors; ``all``: every monitor;
* the SLI is the share of evaluations that passed (``PASS`` or ``WARN``) in the window; learning-phase
  evaluations don't count;
* the error budget is what the objective allows to fail (``1 - objective``); ``budget_remaining`` is the
  share of it not used yet, and the burn rate is how fast the last day used it (1 = exactly on budget).

States: ``met``, ``at_risk`` (less than a quarter of the budget left, or burning faster than 2× over the
last day), ``breached`` (the SLI is below the objective), ``no_data``. The notifier checks hourly and
tells the product's owners when an SLO becomes at risk or breached, and when it recovers.
"""

from __future__ import annotations

import time
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, String, Table, select

from cairn.common.db import JSONText, metadata

product_slos = Table(
    "cairn_product_slos", metadata,
    Column("id", String(64), primary_key=True),
    Column("product_urn", String(512), nullable=False, index=True),
    Column("doc", JSONText, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
slo_state = Table(
    "cairn_slo_state", metadata,
    Column("id", String(64), primary_key=True),
    Column("state", String(12), nullable=False),
    Column("since_ms", BigInteger, nullable=False),
)

KINDS = {"freshness": {"freshness"}, "volume": {"volume"},
         "quality": {"field_metric", "distribution", "custom_sql", "schema"}, "all": None}
DAY = 86_400_000


class SLOIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    product_urn: str = Field(pattern=r"^urn:li:dataProduct:")
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["freshness", "volume", "quality", "all"]
    objective: float = Field(ge=50.0, le=99.999, description="percent of evaluations that must pass")
    window_days: int = Field(default=28, ge=1, le=90)
    description: str = Field(default="", max_length=1000)


class SLOError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class SLOService:
    def __init__(self, engine, store, product_datasets: Callable[[str], list[str]],
                 owners: Callable[[str], list[str]] = lambda urn: [], notify=None, audit=None):
        """``store``: the QualityStore; ``product_datasets(product)``: the product's dataset urns."""
        self.engine = engine
        self.store = store
        self.product_datasets = product_datasets
        self.owners = owners
        self.notify = notify
        self.audit = audit

    # ---- definitions ---------------------------------------------------------------------------
    def put(self, slo: SLOIn, actor: str) -> dict:
        with self.engine.begin() as conn:
            conn.execute(product_slos.delete().where(product_slos.c.id == slo.id))
            conn.execute(product_slos.insert().values(id=slo.id, product_urn=slo.product_urn, doc=slo.model_dump(),
                                                      updated_ms=int(time.time() * 1000)))
        if self.audit is not None:
            self.audit.record("marketplace", "slo.set", actor, slo.product_urn, slo.model_dump())
        return self.status(slo)

    def get(self, sid: str) -> SLOIn:
        with self.engine.connect() as conn:
            r = conn.execute(select(product_slos).where(product_slos.c.id == sid)).first()
        if r is None:
            raise SLOError("no such SLO", 404)
        return SLOIn.model_validate(r.doc)

    def delete(self, sid: str, actor: str) -> None:
        slo = self.get(sid)
        with self.engine.begin() as conn:
            conn.execute(product_slos.delete().where(product_slos.c.id == sid))
            conn.execute(slo_state.delete().where(slo_state.c.id == sid))
        if self.audit is not None:
            self.audit.record("marketplace", "slo.removed", actor, slo.product_urn, {"id": sid})

    def definitions(self, product_urn: Optional[str] = None) -> list[SLOIn]:
        q = select(product_slos).order_by(product_slos.c.product_urn, product_slos.c.id)
        if product_urn:
            q = q.where(product_slos.c.product_urn == product_urn)
        with self.engine.connect() as conn:
            return [SLOIn.model_validate(r.doc) for r in conn.execute(q)]

    # ---- measuring -------------------------------------------------------------------------------
    def status(self, slo: SLOIn, now_ms: Optional[int] = None) -> dict:
        now = now_ms or int(time.time() * 1000)
        start, day_start = now - slo.window_days * DAY, now - DAY
        types = KINDS[slo.kind]
        datasets = self.product_datasets(slo.product_urn)
        good = total = bad_day = total_day = 0
        monitors = []
        for urn in datasets:
            for m in self.store.list_monitors(urn):
                if not m.enabled or (types is not None and m.type not in types):
                    continue
                monitors.append(m.id)
                for ev in self.store.evaluations(m.id, limit=5000):
                    if ev.ts_ms < start or ev.ts_ms > now or ev.status == "TRAINING":
                        continue
                    ok = ev.status in ("PASS", "WARN")
                    total += 1
                    good += ok
                    if ev.ts_ms >= day_start:
                        total_day += 1
                        bad_day += not ok
        allowed = 1 - slo.objective / 100
        out = {**slo.model_dump(), "datasets": datasets, "monitors": len(monitors), "good": good, "total": total}
        if not total:
            return {**out, "sli": None, "budget_remaining": None, "burn_rate": None, "state": "no_data"}
        sli = good / total
        used = (1 - sli) / allowed if allowed else (0.0 if sli == 1 else float("inf"))
        burn = ((bad_day / total_day) / allowed) if total_day and allowed else 0.0
        remaining = max(0.0, 1 - used)
        state = ("breached" if sli * 100 < slo.objective else
                 "at_risk" if remaining < 0.25 or burn > 2 else "met")
        return {**out, "sli": round(sli * 100, 3), "budget_remaining": round(remaining * 100, 1),
                "burn_rate": round(burn, 2), "state": state}

    def dashboard(self, now_ms: Optional[int] = None) -> list[dict]:
        rank = {"breached": 0, "at_risk": 1, "met": 2, "no_data": 3}
        rows = [self.status(s, now_ms) for s in self.definitions()]
        return sorted(rows, key=lambda r: (rank[r["state"]], r["budget_remaining"] if r["budget_remaining"] is not None
                                           else 101, r["id"]))

    def check(self, now_ms: Optional[int] = None) -> dict:
        """Tell owners when an SLO becomes at risk or breached, and when it is met again."""
        now = now_ms or int(time.time() * 1000)
        told = 0
        for row in self.dashboard(now):
            with self.engine.begin() as conn:
                prev = conn.execute(select(slo_state).where(slo_state.c.id == row["id"])).first()
                if prev is not None and prev.state == row["state"]:
                    continue
                conn.execute(slo_state.delete().where(slo_state.c.id == row["id"]))
                conn.execute(slo_state.insert().values(id=row["id"], state=row["state"], since_ms=now))
            before = prev.state if prev is not None else "met"
            if row["state"] in ("at_risk", "breached") or (row["state"] == "met" and before in ("at_risk", "breached")):
                told += self._tell(row, before)
        return {"slos": len(self.definitions()), "told": told}

    def _tell(self, row: dict, before: str) -> int:
        if self.notify is None:
            return 0
        from cairn.notify.hub import Event

        what = {"at_risk": "is at risk", "breached": "is breached", "met": "is met again"}[row["state"]]
        body = (f"{row['name']}: {row['sli']}% of {row['kind']} checks passed over {row['window_days']} days "
                f"(objective {row['objective']}%); {row['budget_remaining']}% of the error budget is left, "
                f"burn rate {row['burn_rate']}.")
        try:
            self.notify.publish(Event(type="product.slo_changed", title=f"SLO {row['name']} {what}", body=body,
                                      entity_urn=row["product_urn"], recipients=self.owners(row["product_urn"]),
                                      include_followers=True, payload={"slo": row["id"], "state": row["state"],
                                                                       "before": before}))
            return 1
        except Exception:  # noqa: BLE001 - the SLO state stands even if nobody could be told
            return 0
