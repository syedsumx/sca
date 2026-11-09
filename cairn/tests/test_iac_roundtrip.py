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
    pyiceberg, the streams extra; the terraform job has it)."""
    try:
        import pyiceberg.exceptions  # noqa: F401
        lake = True
    except ImportError:
        lake = False
    return [k for k in registry.kinds() if k.example and getattr(s, k.service, None) is not None
            and (lake or k.name != "vectors")]


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
    assert {x.action for x in b.plan(c, spec)[0]} == {"keep"}

    # A secret's value is sent again only when its version rises; Cairn holds the new one.
    doc["secrets"][0]["value_version"] = 2
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    spec = b.load(str(path), {**secrets_of(text), "SECRET_FINANCE_DB_PASSWORD_VALUE": "rotated"})
    changes, state = b.plan(c, spec)
    assert [(x.kind, sorted(x.diff)) for x in changes if x.action != "keep"] == [("secrets", ["value_version"])]
    b.apply(c, spec, changes, state, log=lambda _: None)
    assert s.secret_scopes.read(ADMIN, "finance-db", "password") == "rotated"
    assert {x.action for x in b.plan(c, spec)[0]} == {"keep"}

    # Dropped from git and pruned: what the examples made goes (settings stay: they always exist).
    keep = {"secret_scopes", "policies"}
    doc2 = {"bundle": "everything", **{k: v for k, v in doc.items() if k in keep},
            "secret_scope_acls": [x for x in doc["secret_scope_acls"] if x["principal"] == ADMIN]}   # its last manager
    path.write_text(yaml.safe_dump(doc2, sort_keys=False))
    changes, state = b.plan(c, b.load(str(path)), prune=True)
    gone = {x.kind for x in changes if x.action == "delete"}
    assert gone >= {k.name for k in kinds if k.name not in keep}, gone
    b.apply(c, b.load(str(path)), changes, state, log=lambda _: None)
    for k in kinds:
        if k.name not in keep and not k.singleton:
            left = e.records(k, {p: k.example[p] for p in k.parents} if k.parents else None)
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
    assert inv["model_deployments"] is None                       # switched off here
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
