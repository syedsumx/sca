"""Everything as code, end to end through Cairn's real API (in-process): every registry kind whose service runs
here is made from its example, exported as a bundle and planned (no changes), changed in git and applied in place,
and pruned; one resource exported as Terraform and YAML from /v1/iac/export; the inventory counts them."""

import re

import pytest
import yaml
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.iac import bundle as b
from cairn.iac import export, registry
from cairn.iac.engine import Engine, tf_id
from iac_world import ADMIN, ANA_TOKEN, TOKEN, build, prerequisites


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    s = build(tmp_path_factory.mktemp("iac"))
    http = TestClient(create_api_app(s), headers={"Authorization": f"Bearer {TOKEN}"})
    c = b.Client("", "unused", http=http)
    prerequisites(Engine(c.call))
    return s, c, http


def runnable(s) -> list[registry.Kind]:
    """The kinds with an example whose service runs here (deleting a vector index drops its Iceberg table: that needs
    pyiceberg, the streams extra; the terraform job has it). An index needs a database with a table in it: that kind
    round-trips in tests/test_sre_agent_e2e.py, on a managed instance's database, through a change request."""
    try:
        import pyiceberg.exceptions  # noqa: F401
        lake = True
    except ImportError:
        lake = False
    return [k for k in registry.kinds() if k.example and getattr(s, k.service, None) is not None
            and (lake or k.name != "vectors") and k.name != "postgres_indexes"]


def ensure(e: Engine, k: registry.Kind, spec: dict) -> dict:
    """The example's resource: made, unless it is there already (a setting always is; a prerequisite may be)."""
    for r in e.records(k, {p: spec[p] for p in k.parents}):
        if k.singleton or b._rec_key(k, r) == b.key_of(k, spec):
            return e.update(k, r, spec) if k.singleton else r
    return e.create(k, spec)


KNOWN = {"NOTIFICATION_ROUTE_FINANCE_INCIDENTS_TARGET": "#finance-data"}   # a sensitive value, given as it is


def secrets_of(text: str) -> dict:
    return {v: KNOWN.get(v, f"value-of-{v.lower()}") for v in re.findall(r"\$\{([A-Z0-9_]+)\}", text)}


def test_every_kind_round_trips_through_export_and_a_bundle(world, tmp_path):
    s, c, _ = world
    e = Engine(c.call)
    kinds = runnable(s)
    assert len(kinds) >= 34, [k.name for k in kinds]           # the families: access, data, quality, BI, compute, AI
    assert {k.family for k in kinds} == set(registry.FAMILIES)
    made = {k.name: ensure(e, k, dict(k.example)) for k in kinds}

    # Exported as a bundle: planned against the same Cairn, nothing changes; applied, the bundle adopts them.
    text = export.export(c.call, "yaml", bundle="everything")
    assert "value-of" not in text and "${SECRET_FINANCE_DB_PASSWORD_VALUE}" in text   # a secret is a variable
    path = tmp_path / "cairn.yaml"
    path.write_text(text)
    spec = b.load(str(path), secrets_of(text))
    for k in kinds:
        assert spec[k.name], f"{k.name} was not exported"
    changes, state = b.plan(c, spec)
    assert [x.line() for x in changes if x.action != "keep"] == []
    b.apply(c, spec, changes, state, log=lambda _: None)

    # Changed in git: each kind's example change, applied in place (the same ids).
    doc = yaml.safe_load(text)
    for k in kinds:
        if not k.example_update:
            continue
        key = b.key_of(k, k.example)
        item = next(x for x in doc[k.name] if b.key_of(k, {**k.defaults, **x}) == key)
        item.update(k.example_update)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    spec = b.load(str(path), secrets_of(text))
    changes, state = b.plan(c, spec)
    updated = {x.kind for x in changes if x.action == "update"}
    assert updated == {k.name for k in kinds if k.example_update}, [x.line() for x in changes if x.action != "keep"]
    assert not [x for x in changes if x.action in ("create", "delete", "replace")]
    b.apply(c, spec, changes, state, log=lambda _: None)
    for k in kinds:
        if k.example_update and not k.singleton:
            rec = e.find(k, {p: k.example[p] for p in k.parents}, str(made[k.name].get(k.id)))
            assert rec is not None, k.name
            got = b.declared(k, rec)
            for f, v in k.example_update.items():
                assert b._same(v, got.get(f)), (k.name, f, v, got.get(f))
    assert [x.line() for x in b.plan(c, spec)[0] if x.action != "keep"] == []

    # A secret's value is sent again only when its version rises; Cairn holds the new one.
    doc["secrets"][0]["value_version"] = 2
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    spec = b.load(str(path), {**secrets_of(text), "SECRET_FINANCE_DB_PASSWORD_VALUE": "rotated"})
    changes, state = b.plan(c, spec)
    assert [(x.kind, sorted(x.diff)) for x in changes if x.action != "keep"] == [("secrets", ["value_version"])]
    b.apply(c, spec, changes, state, log=lambda _: None)
    assert s.secret_scopes.read(ADMIN, "finance-db", "password") == "rotated"
    assert {x.action for x in b.plan(c, spec)[0]} == {"keep"}

    # Dropped from git and pruned: what the examples made goes (settings stay: they always exist; so does what is
    # only forgotten, such as a tokenisation domain, whose key every token made in it needs).
    keep = {"secret_scopes", "policies"}
    doc2 = {"bundle": "everything", **{k: v for k, v in doc.items() if k in keep},
            "secret_scope_acls": [x for x in doc["secret_scope_acls"] if x["principal"] == ADMIN]}   # its last manager
    path.write_text(yaml.safe_dump(doc2, sort_keys=False))
    changes, state = b.plan(c, b.load(str(path)), prune=True)
    gone = {x.kind for x in changes if x.action == "delete"}
    assert gone >= {k.name for k in kinds if k.name not in keep}, gone
    b.apply(c, b.load(str(path)), changes, state, log=lambda _: None)
    for k in kinds:
        if k.name not in keep and not k.singleton and k.delete is not None:
            try:
                left = e.records(k, {p: k.example[p] for p in k.parents} if k.parents else None)
            except b.ApiError as err:                  # its parent was pruned too (an instance's parameters)
                assert k.parents and " 404 " in str(err), (k.name, err)
                left = []
            left = [r for r in left if not k.mine or r.get(k.owner_field) == ADMIN]   # not others' (the built-in ones)
            assert [b.key_of(k, {**k.defaults, **b.declared(k, r)}) for r in left] == (
                ["finance-db/" + ADMIN] if k.name == "secret_scope_acls" else []), k.name


def test_one_resource_as_terraform_or_yaml_and_the_inventory(world):
    s, c, http = world
    e = Engine(c.call)
    k = registry.kind("pipes")
    rec = e.create(k, {**k.example, "name": "for-export"})
    r = http.get("/v1/iac/export", params={"kind": "pipes", "id": tf_id(k, rec)})
    assert r.status_code == 200, r.text
    assert 'resource "cairn_pipe" "for_export"' in r.text and f'id = "{rec["id"]}"' in r.text
    assert "import {" in r.text and "bucket = \"landing\"" in r.text
    r = http.get("/v1/iac/export", params={"kind": "pipe", "id": tf_id(k, rec), "format": "yaml"})
    assert r.status_code == 200 and yaml.safe_load(r.text)["pipes"][0]["name"] == "for-export"
    assert http.get("/v1/iac/export", params={"kind": "pipes", "id": "nope"}).status_code == 404
    assert http.get("/v1/iac/export", params={"kind": "nope"}).status_code == 404
    inv = http.get("/v1/iac/inventory").json()["kinds"]
    assert inv["pipes"] >= 1 and set(inv) == {x.name for x in registry.kinds()}
    assert inv["system_table_retention"] is None                  # switched off here (no system tables catalog)
    # The schema is for anyone signed in; export is as the caller, so another person sees only what they may.
    ana = TestClient(http.app, headers={"Authorization": f"Bearer {ANA_TOKEN}"})
    schema = ana.get("/v1/iac/schema")
    assert schema.status_code == 200 and len(schema.json()["kinds"]) == len(registry.kinds())
    assert TestClient(http.app).get("/v1/iac/schema").status_code == 401             # signed in, though
    assert ana.get("/v1/iac/inventory").json()["kinds"]["pipes"] is None             # pipes are operators'
    assert ana.get("/v1/iac/export", params={"kind": "pipes", "id": tf_id(k, rec)}).status_code == 404
    e.delete(k, rec)


def test_a_write_only_value_never_leaves_cairn(world, tmp_path):
    s, c, http = world
    e = Engine(c.call)
    k = registry.kind("secrets")
    e.create(k, {"scope": "finance-db", "key": "token", "value": "s3cr3t-value"})
    for fmt in ("terraform", "yaml"):
        text = export.export(c.call, fmt, ["secrets"])
        assert "s3cr3t-value" not in text
    tf = export.export(c.call, "terraform", ["secrets"])
    assert 'variable "secret_finance_db_token_value"' in tf and "sensitive   = true" in tf
    assert "value = var.secret_finance_db_token_value" in tf


def test_what_names_another_resource_by_id_round_trips(world, tmp_path):
    """A persona's grants and a query's shares (their parent is another resource's id): made, exported as a bundle,
    planned with no changes, changed in place, and pruned."""
    s, c, _ = world
    e = Engine(c.call)
    persona = ensure(e, registry.kind("personas"), {"name": "Grant holders", "members": {"groups": ["urn:li:corpGroup:x"]}})
    query = ensure(e, registry.kind("queries"), {"name": "to-share", "connection": "lakehouse", "sql": "SELECT 2 AS n"})
    grant = e.create(registry.kind("persona_grants"), {"persona": persona["id"], "actor": "urn:li:corpuser:ana"})
    assert grant["source"] == "manual"
    share = e.create(registry.kind("sql_shares"), {"kind": "query", "item": query["id"],
                                                   "principal": "urn:li:corpGroup:analysts", "role": "VIEWER"})
    assert share["owner"] == ADMIN
    names = ["personas", "persona_grants", "queries", "sql_shares"]
    text = export.export(c.call, "yaml", names, bundle="refs")
    doc = yaml.safe_load(text)
    assert {"persona": persona["id"], "actor": "urn:li:corpuser:ana"} in doc["persona_grants"]
    path = tmp_path / "cairn.yaml"
    path.write_text(text)
    changes, state = b.plan(c, b.load(str(path)))
    assert [x.line() for x in changes if x.action != "keep"] == []
    next(x for x in doc["sql_shares"] if x["item"] == query["id"])["role"] = "EDITOR"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    changes, state = b.plan(c, b.load(str(path)))
    assert [(x.kind, x.action) for x in changes if x.action != "keep"] == [("sql_shares", "update")]
    b.apply(c, b.load(str(path)), changes, state, log=lambda _: None)
    assert s.bi_sharing.mine(ADMIN)[0]["role"] == "EDITOR"
    doc = {"bundle": "refs", "personas": doc["personas"], "queries": doc["queries"]}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    changes, state = b.plan(c, b.load(str(path)), prune=True)
    assert {(x.kind, x.action) for x in changes if x.action != "keep"} == {("persona_grants", "delete"),
                                                                            ("sql_shares", "delete")}
    b.apply(c, b.load(str(path)), changes, state, log=lambda _: None)
    assert s.personas.list_grants(persona["id"]) == [] and s.bi_sharing.mine(ADMIN) == []


def test_a_data_product_written_in_short_forms_plans_no_change_after_apply(world, tmp_path):
    """``domain: sales``, ``owners: [ana]`` and assets in any order are what Cairn stores as URNs, sorted: a bundle
    written that way is applied once and then plans nothing (not an update on every run)."""
    s, c, _ = world
    a, z = ("urn:li:dataset:(urn:li:dataPlatform:iceberg,short.zz,PROD)",
            "urn:li:dataset:(urn:li:dataPlatform:iceberg,short.aa,PROD)")
    path = tmp_path / "cairn.yaml"
    path.write_text(yaml.safe_dump({"bundle": "short", "data_products": [
        {"id": "short-forms", "name": "Short forms", "domain": "sales", "owners": ["ana", "urn:li:corpGroup:team"],
         "assets": [a, z], "output_ports": [a]}]}, sort_keys=False))
    spec = b.load(str(path))
    assert spec["data_products"]["short-forms"]["domain"] == "urn:li:domain:sales"
    assert spec["data_products"]["short-forms"]["owners"] == ["urn:li:corpGroup:team", "urn:li:corpuser:ana"]
    assert spec["data_products"]["short-forms"]["assets"] == [z, a]
    changes, state = b.plan(c, spec)
    assert [(x.kind, x.action) for x in changes] == [("data_products", "create")]
    b.apply(c, spec, changes, state, log=lambda _: None)
    assert [x.line() for x in b.plan(c, b.load(str(path)))[0] if x.action != "keep"] == []
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"bundle": "short", "data_products": [
        {"id": "bad", "name": "Bad", "owners": ["urn:li:dataset:x"]}]}))
    with pytest.raises(b.BundleError, match="data_products/bad"):
        b.load(str(bad))
