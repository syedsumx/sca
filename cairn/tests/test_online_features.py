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


def test_a_feature_table_pointed_at_another_table_serves_none_of_the_first_tables_values():
    """Registering a feature table again with another table changes the table each lookup is decided on; the values
    synced from the first table must not be served under the second table's policy."""
    from cairn.common.core import InMemoryGraph
    from cairn.ml.features import FeatureTables

    a, b = SRC, "urn:li:dataset:(urn:li:dataPlatform:iceberg,shop.stats,PROD)"
    graph = InMemoryGraph()
    for urn in (a, b):
        graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c, "nativeDataType": t} for c, t in (
            ("customer_id", "bigint"), ("email", "varchar"), ("spend", "bigint"), ("updated", "bigint"))]}
    ft = FeatureTables(graph, lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{'.'.join(ns)}.{t},PROD)",
                       lambda p, privilege, urn: p.urn == ANA or (privilege == "VIEW_ENTITY_PAGE" and urn == b))
    tables = {("shop", "customers"): _Table(ROWS), ("shop", "stats"): _Table([{**ROWS[0], "spend": 7}])}
    tables[("shop", "stats")].snap = 2

    def decide(actor, urn):
        return (actor == ANA or urn == b), False, None
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    online_feature_tables.create(engine)
    online_feature_values.create(engine)
    store = OnlineFeatures(engine, ft, lambda: SimpleNamespace(load_table=lambda ident: tables[ident]), decide,
                           lambda p, privilege, urn: p.urn == ANA)
    ft.on_remove, ft.on_source_change = store._drop, store.source_changed           # as services.py wires them
    ft.register(_p(ANA), {"name": "activity", "table": "shop.customers", "keys": ["customer_id"],
                          "features": ["email", "spend"]})
    store.enable(_p(ANA), "activity", {})
    with pytest.raises(FeatureError):
        store.lookup(_p(BOB), "activity", [{"customer_id": 1}])            # Bob may not read shop.customers
    ft.register(_p(ANA), {"name": "activity", "table": "shop.stats", "keys": ["customer_id"],
                          "features": ["email", "spend"]})
    try:
        got = store.lookup(_p(BOB), "activity", [{"customer_id": 1}])["results"][0]
    except FeatureError as e:
        assert e.status == 409                                             # not filled from shop.stats yet
    else:
        assert got["features"]["spend"] == 7, got                          # shop.stats's value, never shop.customers'


def test_a_masked_key_or_event_time_says_nothing_of_the_raw_values():
    """A lookup by a key column masked for the caller would tell whether a raw value is in the table (Trino compares
    the masked value); a masked timestamp_column would come back raw as event_ms."""
    access = _Access()
    store, _ = _store(read_decider(lambda: access), features=_Features(keys=("email",), features=("spend",)),
                      ts="updated")
    hit = store.lookup(_p(BOB), "activity", [{"email": "jane@example.com"}])["results"][0]
    assert hit["status"] == "found" and hit["event_ms"] == 1_700_000_000_000
    access.plans[BOB] = DatasetPlan(fields={"email"}, field_strategies={"email": "partial"}, mask_queries=True)
    with pytest.raises(FeatureError) as e:
        store.lookup(_p(BOB), "activity", [{"email": "jane@example.com"}])
    assert e.value.status == 403 and "email is masked" in str(e.value)
    access.plans[BOB] = DatasetPlan(fields={"updated"}, field_strategies={"updated": "redact"}, mask_queries=True)
    hit = store.lookup(_p(BOB), "activity", [{"email": "jane@example.com"}])["results"][0]
    assert hit["status"] == "found" and hit["event_ms"] is None and hit["features"]["spend"] == 1234
    assert store.lookup(_p(ANA), "activity", [{"email": "jane@example.com"}])["results"][0]["event_ms"] == 1_700_000_000_000


def test_each_feature_is_masked_with_its_own_strategy_as_trino_masks_it():
    """Trino masks each column with the strongest strategy of the policies that hit that column (and a geo mask
    only coarsens location columns); the dataset's overall strongest strategy is not every column's."""
    access = _Access()
    store, _ = _store(read_decider(lambda: access))
    access.plans[BOB] = DatasetPlan(fields={"email", "spend"}, field_strategies={"email": "hash", "spend": "partial"},
                                    strategy="hash", mask_queries=True)
    got = store.lookup(_p(BOB), "activity", [{"customer_id": 1}])
    assert sorted(got["masked"]) == ["email", "spend"]
    feats = got["results"][0]["features"]
    assert feats["spend"] == 1000 and feats["email"].startswith("sha256:")
    access.plans[BOB] = DatasetPlan(fields={"email"}, field_strategies={"email": "geo"}, strategy="geo", mask_queries=True)
    assert store.lookup(_p(BOB), "activity", [{"customer_id": 1}])["results"][0]["features"]["email"] == "j•••@•••.com"
