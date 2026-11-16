"""Share usage (L106) stays bounded however a recipient spells the tables it asks for: names outside the share are the
recipient's to choose, so only the first few a day are counted by name and the rest count against the share as a
whole. Real database (SQLite) and the real decision path Cairn Catalog calls; the lakehouse is not reached for
tables outside the share."""

import json
import time

from sqlalchemy import create_engine, func, select

from cairn.common.db import metadata
from cairn.dataaccess.shares import PREFIX, Shares, lakehouse_shares, share_usage


def test_refused_table_names_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_SHARES_USAGE_REFUSED_TABLES", "5")
    engine = create_engine(f"sqlite:///{tmp_path / 'u.db'}")
    metadata.create_all(engine, tables=[lakehouse_shares, share_usage])
    now = int(time.time())
    with engine.begin() as conn:
        conn.execute(lakehouse_shares.insert().values(
            id="s1", name="usage", recipient="Partner", tables=json.dumps(["analytics.products"]), secret_hash="x",
            created_by="urn:li:corpuser:ana", created_s=now, expires_s=now + 86400))
    shares = Shares(engine, None, None)
    for i in range(200):                              # a recipient probing names of its own choosing
        out = shares.decide(PREFIX + "s1", [{"namespace": ["analytics"], "name": f"probe_{i}_" + "x" * 400}])
        assert out["tables"][0]["read"] is False
    shares.decide(PREFIX + "s1", [{"namespace": ["analytics"], "name": "probe_0_" + "x" * 400}])   # seen: by name
    shares.flush_usage()
    with engine.connect() as conn:
        rows = conn.execute(select(func.count()).select_from(share_usage)).scalar()
        refusals = conn.execute(select(func.sum(share_usage.c.refusals))).scalar()
        first = conn.execute(select(share_usage.c.refusals).where(
            share_usage.c.table_name == "analytics.probe_0_" + "x" * 400)).scalar()
    assert rows == 1 + 5, rows                        # the share as a whole, and the first five names
    assert refusals == 201 and first == 2             # every refusal is still counted
