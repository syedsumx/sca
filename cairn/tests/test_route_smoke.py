"""Every route of the API, policy-decision and gateway apps, called as an admin with placeholder input
(an empty body, then one built from the route's own request schema): none may fail with a server error.
503 "not configured" is the documented answer of an optional module that is switched off."""

import re

from fastapi.testclient import TestClient

from cairn.app import create_api_app, create_gateway_app, create_pdp_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services
from conftest import DS

ADMIN = "urn:li:corpuser:admin"
PATH_VALUES = {"urn": DS, "target": "snowflake"}


def example(schema, spec, depth=0):
    """The smallest value that satisfies ``schema``: required properties only, one item per array."""
    if "$ref" in schema:
        return example(spec["components"]["schemas"][schema["$ref"].split("/")[-1]], spec, depth)
    for k in ("anyOf", "oneOf", "allOf"):
        if k in schema:
            options = [o for o in schema[k] if o.get("type") != "null"] or schema[k]
            return example(options[0], spec, depth)
    if schema.get("default") is not None:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {} if depth > 4 else {k: example(v, spec, depth + 1) for k, v in props.items()
                                     if depth == 0 or k in schema.get("required", [])}
    if kind == "array":
        return [example(schema.get("items", {}), spec, depth + 1)] if depth < 3 else []
    if kind == "integer":
        return max(1, schema.get("minimum", 1))
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    return DS if "urn" in (schema.get("title") or "").lower() else "x" * max(1, schema.get("minLength", 1))


def admin_services(settings, graph, with_gateway, with_ai=False):
    s = build_services(settings, graph=graph, with_ai=with_ai, with_gateway=with_gateway)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    return s


def test_no_route_fails_with_a_server_error(settings, graph):
    plain, full = admin_services(settings, graph, False), admin_services(settings, graph, True, with_ai=True)
    failures, calls = [], 0
    for app in (create_api_app(plain), create_api_app(full), create_pdp_app(plain), create_gateway_app(full)):
        client, spec = TestClient(app, raise_server_exceptions=False), app.openapi()
        for path, ops in spec["paths"].items():
            url = re.sub(r"\{([^}:]+)(:path)?\}", lambda m: PATH_VALUES.get(m.group(1), "x1"), path)
            for method, op in ops.items():
                schema = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
                bodies = [None] if method in ("get", "delete") else [{}, example(schema, spec) if schema else {}]
                for body in bodies:
                    calls += 1
                    r = client.request(method.upper(), url, json=body, headers={"X-Cairn-Actor": ADMIN})
                    if r.status_code >= 500 and not (r.status_code == 503 and "not configured" in r.text):
                        failures.append(f"{method.upper()} {path} {body!r:.80} -> {r.status_code} {r.text[:120]}")
    assert calls > 500
    assert not failures, "\n".join(failures)


def test_pii_classify_validates_fields(settings, graph):
    c = TestClient(create_api_app(admin_services(settings, graph, False)))
    url, h = "/governance/pii/classify", {"X-Cairn-Actor": ADMIN}
    assert c.post(url, json={"fields": [{}]}, headers=h).status_code == 422
    assert c.post(url, json={"fields": [{"fieldPath": None}]}, headers=h).status_code == 422
    r = c.post(url, json={"fields": [{"fieldPath": "email", "nativeDataType": "VARCHAR", "description": "x"}]}, headers=h)
    assert r.status_code == 200 and r.json()["classifications"][0]["kind"] == "Email"


def test_operation_ids_are_unique(settings, graph):
    """Client generators need one operationId per operation."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for s in (admin_services(settings, graph, False), admin_services(settings, graph, True)):
            create_api_app(s).openapi()
