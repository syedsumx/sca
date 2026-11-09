"""Storage credentials and external locations (L133): secrets only by reference, locations that never overlap each
other or Cairn's own storage, grants decided by the policy decision point (its storage layer), and Cairn Catalog's
questions decided, recorded and refused when anything is off. The end-to-end test against Azurite, Cairn Store, Cairn
Catalog and Trino is tests/test_external_locations_e2e.py."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from cairn.common.audit import AuditLog
from cairn.common.core import InMemoryGraph
from cairn.common.db import get_engine, init_db
from cairn.dataaccess.storage_locations import StorageError, StorageLocations, build_storage_router, parse_url
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.attributes import AttributeResolver
from cairn.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, ResourceRef

ADMIN, ANA, BOB = (f"urn:li:corpuser:{u}" for u in ("admin", "ana", "bob"))
ANALYSTS = "urn:li:corpGroup:analysts"
AZ = {"account": "contosolake", "auth": "account_key"}


@pytest.fixture
def st(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/s.db")
    init_db(engine)
    graph = InMemoryGraph()
    graph.aspects[(BOB, "groupMembership")] = {"groups": [ANALYSTS]}
    pdp = PolicyDecisionPoint(AttributeResolver(graph), "authoritative", [], 0)
    pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                 condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))

    def authorize(actor, privilege, urn):
        res = ResourceRef(type="externalLocation", urn=urn) if urn else None
        return pdp.decide(AuthorizeRequest(actor=actor, privilege=privilege, resource=res)).decision == "ALLOW"
    s = StorageLocations(engine, pdp, authorize, AuditLog(engine), reserved=["s3://lakehouse/warehouse"])
    s.audit_log = s.audit
    return s


def test_urls_are_parsed_like_the_catalog_parses_them():
    a = parse_url("abfss://sales@contosolake.dfs.core.windows.net//lake/")
    assert (a["kind"], a["authority"], a["path"]) == ("azure", "contosolake.core.windows.net/sales", "lake")
    assert parse_url("wasbs://sales@contosolake.blob.core.windows.net/lake")["authority"] == a["authority"]
    for bad in ("abfss://sales@contosolake.blob.core.windows.net/x", "abfss://contosolake.dfs.core.windows.net/x",
                "s3://b/a/../c", "file:///etc", "hdfs://nn/x", "abfss://Sales@contosolake.dfs.core.windows.net"):
        with pytest.raises(StorageError):
            parse_url(bad)


def test_credentials_hold_references_never_secrets(st):
    with pytest.raises(StorageError, match="never stored"):
        st.create_credential(ADMIN, "leaky", "azure", {**AZ, "account_key": "c2VjcmV0"}, "file:/run/k")
    for ref in ("c2VjcmV0", "env:AZURE_KEY", "file:relative", "k8s:only-a-name"):
        with pytest.raises(StorageError, match="reference"):
            st.create_credential(ADMIN, "leaky", "azure", AZ, ref)
    with pytest.raises(StorageError, match="needs a reference"):
        st.create_credential(ADMIN, "nokey", "azure", AZ, None)
    with pytest.raises(StorageError, match="tenant_id and client_id"):
        st.create_credential(ADMIN, "spn", "azure", {"account": "contosolake", "auth": "service_principal"}, "k8s:sp/secret")
    with pytest.raises(StorageError, match="role_arn"):
        st.create_credential(ADMIN, "aws", "s3", {"access_key_id": "AKIA1"}, "vault:kv/aws#secret")
    with pytest.raises(StorageError, match="unknown for s3"):
        st.create_credential(ADMIN, "aws", "s3", {"role_arn": "arn:aws:iam::1:role/r", "bucket": "x"}, "workload")
    with pytest.raises(StorageError, match="MANAGE_SYSTEM_OPERATIONS") as ei:
        st.create_credential(ANA, "mine", "azure", AZ, "file:/run/k")
    assert ei.value.status == 403
    c = st.create_credential(ADMIN, "lake-adls", "azure", AZ, "k8s:contoso/account-key", "sales lake")
    assert c["secret_ref"] == "k8s:contoso/account-key" and c["config"] == AZ
    st.create_credential(ADMIN, "aws", "s3", {"role_arn": "arn:aws:iam::1:role/r"}, "workload")
    with pytest.raises(StorageError) as ei:
        st.create_credential(ADMIN, "lake-adls", "azure", AZ, "file:/run/k")
    assert ei.value.status == 409
    entries = [e for e in st.audit.entries("storage") if e["action"] == "storage.credential_created"]
    assert entries and all("secret" not in str(e["details"]).replace("secret_ref", "") for e in entries)


def test_locations_never_overlap_and_match_their_credential(st):
    st.create_credential(ADMIN, "lake-adls", "azure", AZ, "k8s:contoso/account-key")
    st.create_credential(ADMIN, "blob", "azure", {**AZ, "hierarchical_namespace": False}, "k8s:contoso/account-key")
    st.create_credential(ADMIN, "aws", "s3", {"role_arn": "arn:aws:iam::1:role/r"}, "workload")
    st.create_location(ADMIN, "sales", "abfss://sales@contosolake.dfs.core.windows.net/lake", "lake-adls")
    for url, cred, why in (
            ("abfss://sales@contosolake.dfs.core.windows.net/lake/eu", "lake-adls", "overlaps external location sales"),
            ("wasbs://sales@contosolake.blob.core.windows.net/", "lake-adls", "overlaps external location sales"),
            ("abfss://sales@other.dfs.core.windows.net/x", "lake-adls", "storage account contosolake"),
            ("s3://bucket/x", "lake-adls", "the credential is for azure"),
            ("abfss://people@contosolake.dfs.core.windows.net/x", "blob", "must be a container"),
            ("s3://lakehouse/warehouse/sales", "aws", "Cairn's own storage"),
            ("s3://lakehouse", "aws", "Cairn's own storage")):
        with pytest.raises(StorageError, match=why):
            st.create_location(ADMIN, "x-" + str(abs(hash(url)) % 1000), url, cred)
    st.create_location(ADMIN, "people", "abfss://people@contosolake.dfs.core.windows.net", "blob")
    st.create_location(ADMIN, "landing", "s3://landing/in", "aws", read_only=True)
    with pytest.raises(StorageError, match="used by"):
        st.delete_credential(ADMIN, "aws")
    cfg = st.catalog_config()
    assert {loc["name"]: loc["read_only"] for loc in cfg["locations"]} == {"sales": False, "people": False, "landing": True}
    assert {c["name"]: c["secret_ref"] for c in cfg["credentials"]}["lake-adls"] == "k8s:contoso/account-key"


def test_grants_are_policies_decided_by_the_pdp_and_the_catalog_is_answered(st):
    st.create_credential(ADMIN, "lake-adls", "azure", AZ, "k8s:contoso/account-key")
    st.create_location(ADMIN, "sales", "abfss://sales@contosolake.dfs.core.windows.net/lake", "lake-adls")
    url = "abfss://sales@contosolake.dfs.core.windows.net/lake/orders-1"

    def ask(user, privilege, action="create_table", where=url, access="write"):
        return st.decide({"principal": f"user:{user}", "user": user, "location": "sales", "privilege": privilege,
                          "action": action, "url": where, "table": "s.orders", "access": access})
    assert not ask("ana", "CREATE_TABLE")["allowed"]
    st.grant(ADMIN, "sales", ANALYSTS, ["CREATE_TABLE", "READ_FILES"])
    st.grant(ADMIN, "sales", ANA, ["CREATE_TABLE"])
    assert ask("ana", "CREATE_TABLE")["allowed"] and ask("bob", "READ_FILES", "vend", access="read")["allowed"]  # bob: group
    assert not ask("ana", "WRITE_FILES", "vend")["allowed"]
    assert "WRITE_FILES" in ask("bob", "WRITE_FILES", "vend")["reason"]
    # Outside the location, a read-only location written to, an unknown location: refused, whoever asks.
    assert "not in external location" in ask("ana", "CREATE_TABLE", where="abfss://people@contosolake.dfs.core.windows.net/x")["reason"]
    assert not st.decide({"principal": "admin", "location": "nope", "action": "vend", "url": url})["allowed"]
    st.update_location(ADMIN, "sales", read_only=True)
    assert "read-only" in ask("ana", "CREATE_TABLE")["reason"]
    st.update_location(ADMIN, "sales", read_only=False)
    # The catalog's own tokens: recorded, allowed inside a location.
    assert st.decide({"principal": "admin", "location": "sales", "action": "vend", "url": url, "access": "read"})["allowed"]
    # Revoking takes effect on the next question.
    st.revoke(ADMIN, "sales", ANA)
    assert not ask("ana", "CREATE_TABLE")["allowed"]
    st.revoke(ADMIN, "sales", ANALYSTS, "READ_FILES")
    assert not ask("bob", "READ_FILES", "vend", access="read")["allowed"] and ask("bob", "CREATE_TABLE")["allowed"]
    # Every question is in the audit log, allowed or not, with who asked through which catalog token.
    vends = [e for e in st.audit.entries("storage", limit=500) if e["action"] in ("storage.vend", "storage.create_table")]
    assert {e["actor"] for e in vends} >= {ANA, BOB, "catalog-token:admin"}
    assert any(not e["details"]["allowed"] for e in vends) and any(e["details"]["allowed"] for e in vends)
    # A broken decision point refuses.
    st.authorize = lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
    assert not ask("bob", "CREATE_TABLE")["allowed"]


def test_people_see_the_locations_they_hold_a_privilege_on(st):
    st.create_credential(ADMIN, "lake-adls", "azure", AZ, "k8s:contoso/account-key")
    st.create_location(ADMIN, "sales", "abfss://sales@contosolake.dfs.core.windows.net/lake", "lake-adls")
    st.create_location(ADMIN, "people", "abfss://people@contosolake.dfs.core.windows.net/lake", "lake-adls")
    st.grant(ADMIN, "sales", ANA, ["READ_FILES"])
    assert [loc["name"] for loc in st.list_locations(ANA)] == ["sales"]
    assert st.list_locations(ANA)[0]["privileges"] == ["READ_FILES"] and "grants" not in st.list_locations(ANA)[0]
    assert st.list_locations(BOB) == []
    with pytest.raises(StorageError) as ei:
        st.get_location(BOB, "people")
    assert ei.value.status == 404
    assert {g["principal"] for g in st.get_location(ADMIN, "sales")["grants"]} == {ANA}


def test_the_api(st):
    class P:
        def __init__(self, urn):
            self.urn, self.is_service = urn, urn == "service"

    app = FastAPI()
    def principal(request: Request):
        return P(request.headers["x-user"])
    app.include_router(build_storage_router(st, principal))
    c = TestClient(app)
    adm, ana, svc = ({"x-user": u} for u in (ADMIN, ANA, "service"))
    assert c.post("/v1/storage/credentials", json={"name": "lake-adls", "provider": "azure", "config": AZ,
                                                   "secret_ref": "k8s:contoso/account-key"}, headers=adm).status_code == 201
    assert c.post("/v1/storage/credentials", json={"name": "x", "provider": "azure", "config": AZ},
                  headers=ana).status_code == 403
    r = c.post("/v1/storage/locations", json={"name": "sales", "url": "abfss://sales@contosolake.dfs.core.windows.net/lake",
                                              "credential": "lake-adls"}, headers=adm)
    assert r.status_code == 201 and r.json()["urn"] == "urn:li:externalLocation:sales"
    assert c.put("/v1/storage/locations/sales/grants", json={"principal": ANA, "privileges": ["READ_FILES", "BOGUS"]},
                 headers=adm).status_code == 422
    assert c.put("/v1/storage/locations/sales/grants", json={"principal": ANA, "privileges": ["READ_FILES"]},
                 headers=adm).json()["grants"][0]["privileges"] == ["READ_FILES"]
    assert c.get("/v1/storage/locations", headers=ana).json()["locations"][0]["name"] == "sales"
    assert c.patch("/v1/storage/credentials/lake-adls", json={"comment": "rotated"}, headers=adm).json()["comment"] == "rotated"
    assert c.get("/v1/storage/catalog-config", headers=ana).status_code == 403
    assert c.get("/v1/storage/catalog-config", headers=svc).json()["locations"][0]["credential"] == "lake-adls"
    assert c.post("/v1/storage/decide", json={"location": "sales"}, headers=ana).status_code == 403
    assert c.delete("/v1/storage/locations/sales/grants", params={"principal": ANA}, headers=adm).status_code == 200
    assert c.delete("/v1/storage/locations/sales", headers=adm).status_code == 204
    assert c.delete("/v1/storage/credentials/lake-adls", headers=adm).status_code == 204
