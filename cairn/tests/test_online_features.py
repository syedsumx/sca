"""Online feature store (cairn.ml.online), without a lakehouse: a lookup is decided for the caller as a read of the
feature table's source table, as the data preview and serving tables decide it. The end-to-end test with the real
store and catalog is test_online_features_e2e.py."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from cairn.coreproxy.masking import DatasetPlan
from cairn.dataaccess.service import DataAccessError
from cairn.ml.features import FeatureError
from cairn.ml.online import OnlineFeatures, online_feature_tables, online_feature_values, read_decider

SRC = "urn:li:dataset:(urn:li:dataPlatform:iceberg,shop.customers,PROD)"
ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
ROWS = [{"customer_id": 1, "email": "jane@example.com", "spend": 1234, "updated": 1_700_000_000_000},
        {"customer_id": 2, "email": "joe@example.com", "spend": 56, "updated": 1_700_000_100_000}]


class _Batch:
    def __init__(self, rows):
        self.rows = rows

    def to_pylist(self):
        return self.rows


class _Table:
    def __init__(self, rows):
        self.rows, self.snap = rows, 1

    def current_snapshot(self):
        return SimpleNamespace(snapshot_id=self.snap)

    def schema(self):
        return SimpleNamespace(fields=[SimpleNamespace(name="customer_id", field_type="long"),
                                       SimpleNamespace(name="email", field_type="string"),
                                       SimpleNamespace(name="spend", field_type="long"),
                                       SimpleNamespace(name="updated", field_type="long")])

    def scan(self, selected_fields=()):
        rows = [{k: r[k] for k in selected_fields} for r in self.rows]
        return SimpleNamespace(to_arrow=lambda: SimpleNamespace(to_batches=lambda: [_Batch(rows)]))


class _Features:
    def __init__(self, keys=("customer_id",), features=("email", "spend")):
        self.d = {"name": "activity", "table": "shop.customers", "source": SRC, "keys": list(keys),
                  "features": list(features)}

    def definition(self, name):
        return dict(self.d) if name == "activity" else None

    def get(self, p, name):
        if name != "activity":
            raise FeatureError("no such feature table", 404)
        return dict(self.d)


class _Access:
    """The parts of DataAccessService a read decision uses."""

    def __init__(self):
        self.readers, self.filters, self.aggregated, self.plans = {ANA, BOB}, {}, set(), {}
        self.row_filters = lambda actor, urn, dialect: self.filters.get(actor, [])

    def known(self, urn):
        return urn == SRC

    def can_read(self, actor, urn):
        return actor in self.readers

    def _policies(self, actor, urns):
        return {u: object() for u in urns if actor in self.aggregated}

    def _conditions(self, actor, urn, dialect):
        return "cond" if self.row_filters(actor, urn, dialect) else None

    def plan_for(self, actor, urn):
        return self.plans.get(actor) or DatasetPlan()


def _store(policy, features=None, rows=None, ts=None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    online_feature_tables.create(engine)
    online_feature_values.create(engine)
    table = _Table(rows or ROWS)
    store = OnlineFeatures(engine, features or _Features(), lambda: SimpleNamespace(load_table=lambda ident: table),
                           policy, lambda p, privilege, urn: p.urn == ANA)
    store.enable(SimpleNamespace(urn=ANA, is_service=False), "activity", {"timestamp_column": ts} if ts else {})
    return store, table


def _p(urn):
    return SimpleNamespace(urn=urn, is_service=False)


def test_a_lookup_is_decided_as_the_data_preview_decides_a_read():
    """Consent filters (a person who withdrew from the caller's purpose) and aggregation or projection policies hold
    on a Trino read; a lookup by entity key cannot apply either, so it is refused, as a policy row filter is."""
    access = _Access()
    store, _ = _store(read_decider(lambda: access))
    key = [{"customer_id": 1}]
    assert store.lookup(_p(BOB), "activity", key)["results"][0]["features"]["spend"] == 1234
    access.filters[BOB] = ["sha256(email) NOT IN ('...')"]            # a consent filter
    with pytest.raises(FeatureError) as e:
        store.lookup(_p(BOB), "activity", key)
    assert e.value.status == 403 and "row filter" in str(e.value)
    access.filters.clear()
    access.aggregated.add(BOB)
    with pytest.raises(FeatureError) as e:
        store.lookup(_p(BOB), "activity", key)
    assert e.value.status == 403 and "aggregation or projection" in str(e.value)
    access.aggregated.clear()
    access.readers.discard(BOB)
    with pytest.raises(FeatureError) as e:
        store.lookup(_p(BOB), "activity", key)
    assert e.value.status == 403
    # A decision that cannot be made refuses.
    access.readers.add(BOB)

    def broken(actor, urns):
        raise DataAccessError("the table's query policies could not be checked", 503)
    access._policies = broken
    with pytest.raises(FeatureError) as e:
        store.lookup(_p(BOB), "activity", key)
    assert e.value.status == 503
    store.policy = read_decider(lambda: None)
    with pytest.raises(FeatureError) as e:
        store.lookup(_p(BOB), "activity", key)
    assert e.value.status == 503
