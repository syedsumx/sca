"""The web UI's forms (L127): generated from the registry and the API's OpenAPI, current, and covering every action.

``ui/src/consumer/resources/forms.generated.json`` is what the UI draws each create, change and run action with;
``python -m cairn.iac.forms`` writes it. These tests hold it to its sources."""

import json

import pytest

from cairn.iac import forms, registry


@pytest.fixture(scope="module")
def doc():
    return forms.openapi_document()


@pytest.fixture(scope="module")
def built(doc):
    return forms.build(doc=doc)


def test_the_generated_forms_are_current(built):
    with open(forms.OUT, encoding="utf-8") as fh:
        current = fh.read()
    assert current == forms.render(built), "run: python -m cairn.iac.forms"
    assert json.loads(current)["forms"] == built["forms"]


def test_every_action_that_sends_a_body_has_a_form(built):
    catalog = forms.catalog_specs()
    missing = [f"{k}: {forms.form_key(a)}" for k, s in catalog.items() for a in s.get("actions") or []
               if forms.needs_form(a) and forms.form_key(a) not in built["forms"]]
    assert not missing
    assert len(built["forms"]) > 100


def test_registry_forms_follow_the_registry(built):
    seen = 0
    for key, form in built["forms"].items():
        if not form["source"].startswith("registry:"):
            continue
        seen += 1
        k = registry.kind(form["source"].split(":", 1)[1])
        names = {f.name: f for f in k.fields}
        method, path = key.split(" ", 1)
        op = k.create if (k.create.method, forms._norm(k.create.path)) == (method, forms._norm(path)) else k.update
        for f in form["fields"]:
            if f["name"] not in names:
                continue        # a field the catalog's example adds (the API takes it, the registry does not keep it)
            field = names[f["name"]]
            assert f.get("required", False) == field.required, (key, f["name"])
            if field.write_only:
                assert f["type"] == "password", (key, f["name"])
            if op.only and f["name"] not in op.only:
                assert "path" in f and f.get("in_body") is False, (key, f["name"])
            if f["name"] in op.omit:
                assert f.get("in_body") is False, (key, f["name"])
        for required in k.required:
            if not op.only or required in op.only:
                assert required in {f["name"] for f in form["fields"]}, (key, required)
    assert seen > 40


def test_openapi_forms_ask_for_what_the_route_takes(built, doc):
    for key, form in built["forms"].items():
        if form["source"] != "openapi":
            continue
        method, path = key.split(" ", 1)
        schema = forms.request_schema(doc, method, path) or {}
        props = set(schema.get("properties") or {})
        for f in form["fields"]:
            if f.get("in_body") is False:
                continue
            assert f["name"] in props, (key, f["name"])
            if f["name"] in set(schema.get("required") or []):
                assert f.get("required"), (key, f["name"])


def test_widgets_and_pickers(built):
    f = {k: {x["name"]: x for x in v["fields"]} for k, v in built["forms"].items()}
    assert f["PUT /v1/secret-scopes/{name}/secrets/{key}"]["value"]["type"] == "password"
    assert f["PUT /v1/secret-scopes/{name}/secrets/{key}"]["key"]["path"] == "key"
    assert f["PUT /v1/secret-scopes/{name}/acl"]["principal"]["picker"] == "principal"
    assert f["POST /v1/external-catalogs"]["credential_ref"]["picker"] == "secret_ref"
    assert f["POST /sql/saved"]["connection"]["picker"] == "connection"
    assert f["POST /sql/saved"]["sql"]["type"] == "sql"
    assert f["POST /quality/monitors"]["dataset_urn"]["picker"] == "dataset"
    assert f["POST /quality/monitors"]["type"]["enum"][:2] == ["freshness", "volume"]
    assert f["POST /v1/jobs"]["cron"]["type"] == "cron"
    assert f["POST /v1/jobs"]["timeout_minutes"] == {**f["POST /v1/jobs"]["timeout_minutes"], "type": "duration", "unit": "minutes"}
    assert f["PUT /notify/routes/{id}"]["target"]["type"] == "password"      # a webhook URL carries a token
    assert f["PUT /notify/routes/{id}"]["channel"]["enum"][0] == "slack"
    assert f["POST /v1/document-sources"]["secret_scope"]["ref"]["read"] == "/v1/secret-scopes"
    assert f["PUT /maintenance/policies/{scope}"]["scope"]["in_body"] is False
    assert f["POST /v1/budgets"]["amount_usd"]["unit"] == "USD"
    assert f["POST /v1/derived"]["sql"]["type"] == "sql"
    assert f["POST /v1/vectors"]["every_minutes"]["type"] == "duration"


def test_enums_from_descriptions():
    assert forms._enum_from_description("csv, json (lines) or parquet") == (["csv", "json", "parquet"], False)
    assert forms._enum_from_description("freshness, volume, field_metric, schema, sql, ...") == (
        ["freshness", "volume", "field_metric", "schema", "sql"], True)
    assert forms._enum_from_description("lowercase letters, digits and -") == ([], False)
    assert forms._enum_from_description("default, or an agent pool") == ([], False)
    assert forms._enum_from_pattern("^(slack|teams)$") == ["slack", "teams"]
