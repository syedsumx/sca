"""Files in a share, end to end (L169): a folder of a governed volume is attached to a share, and the share's recipient
lists and downloads its files through Cairn, and reads them with temporary store credentials narrowed to that folder.
Everything else (other folders, other volumes, writing) is refused, and detaching, revoking or deleting ends it.

Needs docker-free binaries only: Cairn Store (with STS) and Cairn Catalog (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import httpx
import pytest

pytest.importorskip("pyarrow")
boto3 = pytest.importorskip("boto3")

from test_spark_e2e import ANA, PATS, lake, people  # noqa: E402, F401  (fixtures)
from test_trino_e2e import STORE_KEY, _ready, cairn  # noqa: E402, F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")

ADMIN = "pat-admin-" + "d" * 20
PATS[ADMIN] = "urn:li:corpuser:admin"


def api(url, token, method, path, **kw):
    return httpx.request(method, f"{url}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=60, **kw)


@pytest.fixture(scope="module")
def volumes(cairn, lake):  # noqa: F811
    """The API's volumes on the test's store (in a deployment: CAIRN_STORE_URL and the volumes key)."""
    from cairn.dataaccess.volumes import StoreClient

    url, s = cairn
    before = s.volumes.store, s.volumes.endpoint
    s.volumes.store, s.volumes.endpoint = StoreClient(lake["store"], *STORE_KEY), lake["store"]
    files = {"contracts": {"2026/master.pdf": b"%PDF-1.7 master agreement", "2026/q1/terms.txt": b"net 30",
                           "2025/old.txt": b"superseded", "private/salaries.csv": b"name,pay\njane,100\n"},
             "designs": {"logo.svg": b"<svg/>"}}
    for name, items in files.items():
        assert api(url, ADMIN, "POST", "/v1/volumes", json={"name": name}).status_code == 201
        for path, body in items.items():
            assert api(url, ADMIN, "PUT", f"/v1/volumes/{name}/files/{path}", content=body).status_code < 300
    yield files
    s.volumes.store, s.volumes.endpoint = before


def test_a_recipient_reads_the_shared_folder_of_a_volume_and_nothing_else(cairn, lake, volumes):  # noqa: F811
    from botocore.exceptions import ClientError

    url, s = cairn
    # A share of files only: no table in it.
    r = api(url, ADMIN, "POST", "/v1/shares", json={"name": "partner-files", "recipient": "Partner F", "expires_days": 7})
    assert r.status_code == 200, r.text
    share = r.json()
    sid, secret = share["credential"].split(":", 1)
    basic = httpx.Client(base_url=url, auth=(sid, secret), timeout=60)
    assert basic.get("/v1/shares/recipient/volumes").json() == {"volumes": []}

    # Attaching needs the share and the volume: Ana has a share of her own, but does not manage the volume.
    mine = api(url, ANA, "POST", "/v1/shares", json={"name": "ana-files", "recipient": "x", "tables": ["analytics.products"],
                                                     "expires_days": 7}).json()
    r = api(url, ANA, "POST", "/v1/shares/ana-files/volumes", json={"volume": "contracts"})
    assert r.status_code == 404, r.text                         # a volume she cannot see does not exist to her
    assert api(url, ANA, "POST", "/v1/shares/partner-files/volumes", json={"volume": "contracts"}).status_code == 404
    for bad in ("2026", "../", "2026//", "/2026/"):
        assert api(url, ADMIN, "POST", "/v1/shares/partner-files/volumes",
                   json={"volume": "contracts", "prefix": bad}).status_code == 422, bad
    r = api(url, ADMIN, "POST", "/v1/shares/partner-files/volumes", json={"volume": "contracts", "prefix": "2026/"})
    assert r.status_code == 200 and [(v["volume"], v["prefix"]) for v in r.json()["volumes"]] == [("contracts", "2026/")]
    assert api(url, ADMIN, "POST", "/v1/shares/partner-files/volumes",
               json={"volume": "contracts", "prefix": "2026/"}).status_code == 409

    # The recipient, with the credential or the share's token: the folder's files, and nothing else.
    token = httpx.post(f"{url}/v1/shares/oauth/tokens", auth=(sid, secret), data={"grant_type": "client_credentials"},
                       timeout=10).json()["access_token"]
    bearer = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    for c in (basic, bearer):
        assert c.get("/v1/shares/recipient/volumes").json()["volumes"] == [
            {"volume": "contracts", "folders": ["2026/"], "files": "/v1/shares/recipient/volumes/contracts/files"}]
        got = c.get("/v1/shares/recipient/volumes/contracts/files").json()
        assert [f["path"] for f in got["files"]] == ["2026/master.pdf", "2026/q1/terms.txt"] and not got["truncated"]
        assert [f["path"] for f in c.get("/v1/shares/recipient/volumes/contracts/files",
                                         params={"prefix": "2026/q1/"}).json()["files"]] == ["2026/q1/terms.txt"]
        r = c.get("/v1/shares/recipient/volumes/contracts/files/2026/master.pdf")
        assert r.status_code == 200 and r.content == volumes["contracts"]["2026/master.pdf"]
        assert r.headers["content-disposition"] == "attachment"
        for path in ("contracts/files/2025/old.txt", "contracts/files/private/salaries.csv", "designs/files/logo.svg",
                     "contracts/files/2026/missing.txt", "contracts/files/2026/../private/salaries.csv"):
            assert c.get(f"/v1/shares/recipient/volumes/{path}").status_code in (400, 404), path
        for prefix in ("2025/", "private/", "20"):
            r = c.get("/v1/shares/recipient/volumes/contracts/files", params={"prefix": prefix})
            assert r.status_code == 404 or [f["path"][:5] for f in r.json()["files"]] == ["2026/", "2026/"], prefix
        assert c.get("/v1/shares/recipient/volumes/designs/files").status_code == 404
        assert c.put("/v1/shares/recipient/volumes/contracts/files/2026/new.txt", content=b"x").status_code == 405
    # A person's session is not a share's credential, and the recipient's is not a person's.
    assert api(url, ADMIN, "GET", "/v1/shares/recipient/volumes").status_code == 401
    assert bearer.get("/v1/volumes").status_code in (401, 403)
    assert bearer.get("/v1/volumes/contracts/files/2026/master.pdf").status_code in (401, 403)

    # For engines: temporary store credentials that read the folder and no more.
    creds = basic.post("/v1/shares/recipient/volumes/contracts/credentials").json()
    assert creds["access"] == "read" and creds["folders"] == ["2026/"] and creds["paths"] == ["s3a://cairn-vol-contracts/2026/"]
    s3 = boto3.client("s3", endpoint_url=creds["endpoint"], region_name="us-east-1", aws_access_key_id=creds["access_key_id"],
                      aws_secret_access_key=creds["secret_access_key"], aws_session_token=creds["session_token"])
    assert s3.get_object(Bucket="cairn-vol-contracts", Key="2026/q1/terms.txt")["Body"].read() == b"net 30"
    assert [o["Key"] for o in s3.list_objects_v2(Bucket="cairn-vol-contracts", Prefix="2026/")["Contents"]] == [
        "2026/master.pdf", "2026/q1/terms.txt"]
    for refused in (lambda: s3.get_object(Bucket="cairn-vol-contracts", Key="private/salaries.csv"),
                    lambda: s3.get_object(Bucket="cairn-vol-contracts", Key="2025/old.txt"),
                    lambda: s3.list_objects_v2(Bucket="cairn-vol-contracts"),
                    lambda: s3.list_objects_v2(Bucket="cairn-vol-contracts", Prefix="private/"),
                    lambda: s3.put_object(Bucket="cairn-vol-contracts", Key="2026/new.txt", Body=b"x"),
                    lambda: s3.delete_object(Bucket="cairn-vol-contracts", Key="2026/master.pdf"),
                    lambda: s3.get_object(Bucket="cairn-vol-designs", Key="logo.svg"),
                    lambda: s3.list_objects_v2(Bucket="lakehouse")):
        with pytest.raises(ClientError) as e:
            refused()
        assert e.value.response["Error"]["Code"] in ("AccessDenied", "403"), e.value.response
    assert basic.post("/v1/shares/recipient/volumes/designs/credentials").status_code == 404

    # Recorded: audited as the recipient, and counted in the share's usage.
    actions = {e["action"] for e in s.audit.entries("shares", limit=200)}
    assert {"share.volume_attached", "share.volume_file_read", "share.volume_credentials"} <= actions, actions
    usage = api(url, ADMIN, "GET", "/v1/shares/partner-files/usage").json()
    files = [t for t in usage["tables"] if t["table"] == "volume:contracts"]
    assert files and files[0]["loads"] >= 3 and files[0]["refusals"] >= 2, usage

    # Detached: gone for the recipient at the next request. The whole volume attached: every folder.
    assert api(url, ANA, "DELETE", "/v1/shares/partner-files/volumes/contracts").status_code == 404
    r = api(url, ADMIN, "DELETE", "/v1/shares/partner-files/volumes/contracts", params={"prefix": "2026/"})
    assert r.status_code == 200 and r.json()["volumes"] == []
    assert basic.get("/v1/shares/recipient/volumes/contracts/files/2026/master.pdf").status_code == 404
    assert basic.get("/v1/shares/recipient/volumes").json() == {"volumes": []}
    for body in ({"volume": "contracts", "prefix": "2025/"}, {"volume": "contracts", "prefix": "2026/q1/"}, {"volume": "designs"}):
        assert api(url, ADMIN, "POST", "/v1/shares/partner-files/volumes", json=body).status_code == 200
    got = basic.get("/v1/shares/recipient/volumes/contracts/files").json()
    assert [f["path"] for f in got["files"]] == ["2025/old.txt", "2026/q1/terms.txt"]
    assert basic.get("/v1/shares/recipient/volumes/contracts/files/2026/master.pdf").status_code == 404
    assert basic.get("/v1/shares/recipient/volumes/designs/files/logo.svg").content == b"<svg/>"

    # A deleted volume leaves the share: a new volume of that name is not shared by the old entry.
    assert api(url, ADMIN, "DELETE", "/v1/volumes/designs/files/logo.svg").status_code < 300
    assert api(url, ADMIN, "DELETE", "/v1/volumes/designs").status_code == 204
    assert api(url, ADMIN, "POST", "/v1/volumes", json={"name": "designs"}).status_code == 201
    assert api(url, ADMIN, "PUT", "/v1/volumes/designs/files/next.svg", content=b"<svg/>").status_code < 300
    assert basic.get("/v1/shares/recipient/volumes/designs/files").status_code == 404
    assert [v["volume"] for v in basic.get("/v1/shares/recipient/volumes").json()["volumes"]] == ["contracts"]

    # Revoked: refused at once, with the credential and with a token minted before.
    assert api(url, ADMIN, "POST", f"/v1/shares/{sid}/revoke").json()["state"] == "revoked"
    for c in (basic, bearer):
        assert c.get("/v1/shares/recipient/volumes").status_code in (401, 403)
        assert c.get("/v1/shares/recipient/volumes/contracts/files/2025/old.txt").status_code in (401, 403)
        assert c.post("/v1/shares/recipient/volumes/contracts/credentials").status_code in (401, 403)
    assert api(url, ADMIN, "POST", f"/v1/shares/{sid}/volumes", json={"volume": "contracts"}).status_code == 409
    api(url, ANA, "POST", f"/v1/shares/{mine['id']}/revoke")
