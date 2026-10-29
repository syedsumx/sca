"""Git connectors: pull-request impact (comment and status, contracts checked against removed
columns), signed webhooks for GitHub, GitLab and Bitbucket, and dbt docs written back as a PR."""

import base64
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.quality.contracts import ContractSpec
from cairn.services import build_services
from cairn.sources.git import (MARKER, GitError, GitRepo, GitService, changed_models, model_datasets,
                               pull_request_from, set_descriptions)

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.core.orders,PROD)"
DASH = "urn:li:dashboard:(looker,sales)"

SCHEMA_BASE = """version: 2
models:
  # the orders model
  - name: orders
    description: >-
      Old text
      on two lines
    columns:
      - name: id
        tests: [unique]
      - name: amount
        description: "Order total"
  - name: customers
    columns:
      - name: id
"""
SCHEMA_HEAD = SCHEMA_BASE.replace("      - name: amount\n        description: \"Order total\"\n", "")


def test_set_descriptions_edits_only_what_it_must():
    out, changed = set_descriptions(SCHEMA_BASE, "orders", "One row per order.", {"id": "Order id", "amount": "Total, tax in"})
    assert changed and "# the orders model" in out and "Old text" not in out
    doc = __import__("yaml").safe_load(out)
    orders = doc["models"][0]
    assert orders["description"] == "One row per order."
    assert {c["name"]: c.get("description") for c in orders["columns"]} == {"id": "Order id", "amount": "Total, tax in"}
    assert orders["columns"][0]["tests"] == ["unique"] and doc["models"][1] == {"name": "customers", "columns": [{"name": "id"}]}
    again, changed = set_descriptions(out, "orders", "One row per order.", {"id": "Order id"})
    assert again == out and not changed
    cust, _ = set_descriptions(SCHEMA_BASE, "customers", "People who ordered", {})
    assert __import__("yaml").safe_load(cust)["models"][1]["description"] == "People who ordered"
    with pytest.raises(GitError):
        set_descriptions(SCHEMA_BASE, "nope", "x", {})


class FakeGitHub:
    """The bits of the GitHub REST API Cairn calls, over httpx.MockTransport."""

    def __init__(self):
        self.files = {"base": {"models/schema.yml": SCHEMA_BASE, "models/orders.sql": "select 1"},
                      "head": {"models/schema.yml": SCHEMA_HEAD, "models/orders.sql": "select 2"}}
        self.comments, self.statuses, self.puts, self.prs, self.refs = [], [], [], [], []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path.replace("/repos/acme/dbt", ""), req.method
        assert req.headers["authorization"] == "Bearer ghp_test"
        if m == "GET" and p == "/pulls/7":
            return httpx.Response(200, json={"base": {"sha": "base"}, "head": {"sha": "head"}})
        if m == "GET" and p == "/pulls/7/files":
            return httpx.Response(200, json=[{"filename": "models/schema.yml", "status": "modified"},
                                             {"filename": "models/orders.sql", "status": "modified"},
                                             {"filename": "README.md", "status": "modified"}])
        if m == "GET" and p.startswith("/contents/"):
            ref = req.url.params["ref"]
            text = self.files.get(ref if ref in self.files else "base", {}).get(p[len("/contents/"):])
            if req.headers.get("accept") == "application/vnd.github.raw+json":
                return httpx.Response(404) if text is None else httpx.Response(200, text=text)
            return httpx.Response(200, json={"sha": "blob1"})
        if m == "GET" and p == "/issues/7/comments":
            return httpx.Response(200, json=self.comments)
        if m == "POST" and p == "/issues/7/comments":
            self.comments.append({"id": 1, "body": json.loads(req.content)["body"]})
            return httpx.Response(201, json={})
        if m == "PATCH" and p == "/issues/comments/1":
            self.comments[0]["body"] = json.loads(req.content)["body"]
            return httpx.Response(200, json={})
        if m == "POST" and p.startswith("/statuses/"):
            self.statuses.append((p.split("/")[-1], json.loads(req.content)))
            return httpx.Response(201, json={})
        if m == "GET" and p == "/git/trees/main":
            return httpx.Response(200, json={"tree": [{"path": "models/schema.yml", "type": "blob"},
                                                      {"path": "models/orders.sql", "type": "blob"}]})
        if m == "GET" and p == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "mainsha"}})
        if m == "POST" and p == "/git/refs":
            self.refs.append(json.loads(req.content))
            return httpx.Response(201, json={})
        if m == "PUT" and p.startswith("/contents/"):
            self.puts.append(json.loads(req.content))
            return httpx.Response(200, json={})
        if m == "POST" and p == "/pulls":
            self.prs.append(json.loads(req.content))
            return httpx.Response(201, json={"html_url": "https://github.com/acme/dbt/pull/8"})
        raise AssertionError(f"{m} {p}")


REPO = GitRepo(id="analytics", provider="github", repo="acme/dbt", token="ghp_test", webhook_secret="s3cret",
               database="analytics", schema="core", platform="snowflake")


class Catalog:
    def lineage(self, urn, direction="DOWNSTREAM", max_hops=3, count=50):
        return [{"urn": DASH, "name": "Sales dashboard", "type": "DASHBOARD"}]


@pytest.fixture
def stack(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}, {"fieldPath": "amount"}]}
    graph.aspects[(ORDERS, "ownership")] = {"owners": [{"owner": ANA}]}
    gh = FakeGitHub()
    git = GitService({"analytics": REPO}, graph, Catalog(), s.contracts, s.attributes, s.audit,
                     "https://cairn.example.com", httpx.Client(transport=httpx.MockTransport(gh)))
    git.async_ = False
    s.git = git
    return s, gh, graph


def test_changed_models_and_naming():
    gh = FakeGitHub()
    from cairn.sources.git import GitHub

    prov = GitHub(REPO, httpx.Client(transport=httpx.MockTransport(gh)))
    models = changed_models(prov, REPO, prov.changed_files("7"), "base", "head")
    assert models == {"orders": {"change": "changed", "removed_columns": ["amount"], "file": "models/orders.sql"}}
    yml_only = changed_models(prov, REPO, [{"path": "models/schema.yml", "status": "modified"}], "base", "head")
    assert yml_only["orders"]["change"] == "properties" and "customers" not in yml_only
    assert model_datasets(REPO, "Orders") == [ORDERS, "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.core.orders,PROD)"]


def test_impact_comment_status_and_contract(stack):
    s, gh, _ = stack
    out = s.git.impact("analytics", "7")
    assert out["ok"] and gh.statuses[-1] == ("head", {"state": "success", "context": "cairn/impact",
                                                      "description": out["summary"], "target_url": "https://cairn.example.com/cairn/search?q=dbt"})
    [comment] = gh.comments
    assert MARKER in comment["body"] and "`amount`" in comment["body"] and "Sales dashboard" in comment["body"]
    assert "| 1 | 0 | 1 |" in comment["body"] and "ana" in comment["body"]
    s.contracts.upsert(ContractSpec.model_validate({"dataset": ORDERS, "version": "2.0",
                                                    "schema": {"columns": [{"name": "id"}, {"name": "amount"}]}}), ADMIN)
    out = s.git.impact("analytics", "7")
    assert not out["ok"] and out["summary"] == "breaks the contract of orders"
    assert gh.statuses[-1][1]["state"] == "failure" and len(gh.comments) == 1         # one comment, updated
    assert "**breaks** (amount)" in gh.comments[0]["body"]


def sign(body: bytes, secret: str = "s3cret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhooks_are_verified_per_provider(stack):
    s, gh, _ = stack
    c = TestClient(create_api_app(s))
    body = json.dumps({"action": "synchronize", "pull_request": {"number": 7}}).encode()
    bad = c.post("/integrations/git/analytics/webhook", content=body,
                 headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=0"})
    assert bad.status_code == 401
    ok = c.post("/integrations/git/analytics/webhook", content=body,
                headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)})
    assert ok.status_code == 202 and ok.json()["pull_request"] == "7" and gh.comments
    closed = json.dumps({"action": "closed", "pull_request": {"number": 7}}).encode()
    assert c.post("/integrations/git/analytics/webhook", content=closed, headers={
        "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(closed)}).json() == {"ignored": True}
    assert c.post("/integrations/git/nope/webhook", content=body).status_code == 404

    gl = GitRepo(id="gl", provider="gitlab", repo="acme/dbt", webhook_secret="tok")
    mr = json.dumps({"object_kind": "merge_request", "object_attributes": {"iid": 12, "action": "update"}}).encode()
    assert pull_request_from(gl, {"X-Gitlab-Token": "tok"}, mr) == "12"
    with pytest.raises(GitError):
        pull_request_from(gl, {"X-Gitlab-Token": "nope"}, mr)
    bb = GitRepo(id="bb", provider="bitbucket", repo="acme/dbt", webhook_secret="s3cret")
    pr = json.dumps({"pullrequest": {"id": 3}}).encode()
    assert pull_request_from(bb, {"X-Event-Key": "pullrequest:updated", "X-Hub-Signature": sign(pr)}, pr) == "3"
    assert pull_request_from(bb, {"X-Event-Key": "repo:push", "X-Hub-Signature": sign(pr)}, pr) is None


def test_docs_written_back_as_a_pull_request(stack):
    s, gh, graph = stack
    c = TestClient(create_api_app(s))
    assert c.get("/integrations/git/match", params={"urn": ORDERS}, headers={"X-Cairn-Actor": ANA}).json() == \
        {"repo": "analytics", "model": "orders"}
    assert c.post("/integrations/git/writeback", json={"urn": ORDERS}, headers={"X-Cairn-Actor": ANA}).status_code == 403
    nothing = c.post("/integrations/git/writeback", json={"urn": ORDERS}, headers={"X-Cairn-Actor": ADMIN})
    assert nothing.status_code == 422 and "nobody has written" in nothing.text
    graph.aspects[(ORDERS, "editableDatasetProperties")] = {"description": "One row per order, net of refunds."}
    graph.aspects[(ORDERS, "editableSchemaMetadata")] = {"editableSchemaFieldInfo": [
        {"fieldPath": "amount", "description": "Total in EUR, tax included"}]}
    r = c.post("/integrations/git/writeback", json={"urn": ORDERS}, headers={"X-Cairn-Actor": ADMIN})
    assert r.status_code == 200 and r.json()["pull_request"] == "https://github.com/acme/dbt/pull/8", r.text
    assert gh.refs[0]["sha"] == "mainsha" and gh.refs[0]["ref"].startswith("refs/heads/cairn/docs-orders-")
    written = base64.b64decode(gh.puts[0]["content"]).decode()
    model = __import__("yaml").safe_load(written)["models"][0]
    assert model["description"] == "One row per order, net of refunds." and gh.puts[0]["sha"] == "blob1"
    assert model["columns"][1]["description"] == "Total in EUR, tax included"
    assert gh.prs[0]["base"] == "main" and "column `amount`" in gh.prs[0]["body"]
    assert c.get("/integrations/git/repos", headers={"X-Cairn-Actor": ADMIN}).json()[0]["token_set"] is True
