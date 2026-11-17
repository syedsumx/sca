"""BI impact (``GET /sql/impact``): someone who may not see a dataset learns nothing about the BI assets that read
it, not even whose they are. Someone who may see it still learns, for the assets they cannot open, only how many
and whose."""

from sqlalchemy import insert

from cairn.common.db import get_engine, init_db
from cairn.sqlworkspace.bi_catalog import BiCatalog, bi_lineage

HR = "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)"
CFO, BOB = "urn:li:corpuser:cfo", "urn:li:corpuser:bob"


def catalog(may_see, tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    with engine.begin() as c:
        for i in range(3):
            c.execute(insert(bi_lineage).values(kind="query", asset_id=f"q{i}", dataset=HR, column="salary"))
    cat = BiCatalog(engine, None, None, None, None, None, lambda a, k, i: False, lambda a: False, may_see=may_see)
    cat._row = lambda kind, aid: {"owner": CFO, "name": f"pay {aid}"}
    return cat


def test_a_dataset_the_caller_may_not_see_reveals_no_owners(tmp_path):
    out = catalog(lambda actor, urn: False, tmp_path).impact(BOB, HR)
    assert out["assets"] == [] and out["others"] == []


def test_a_dataset_the_caller_may_see_still_counts_the_others(tmp_path):
    out = catalog(lambda actor, urn: True, tmp_path).impact(BOB, HR)
    assert out["assets"] == [] and out["others"] == [{"owner": CFO, "assets": 3}]


def test_when_it_cannot_tell_it_reveals_nothing(tmp_path):
    def boom(actor, urn):
        raise RuntimeError("policy store down")
    assert catalog(boom, tmp_path).impact(BOB, HR)["others"] == []
