"""Models in a share, end to end (L169), with a real MLflow server and the real MLflow client: a person trains and
registers a model through Cairn and adds its version to a share; the share's recipient lists it, downloads its files
with the share's credential, and loads and runs the model from them. Nothing else of the registry is reachable with
the share's credential, and detaching or revoking ends it.

Needs CAIRN_MLFLOW_PYTHON: a Python with mlflow (3.x) and scikit-learn."""

import json
import os
import subprocess
import tempfile
import textwrap
import threading

import httpx
import pytest

from test_ml_e2e import PY, TOKENS, TRAIN, client, port, wait

pytestmark = pytest.mark.skipif(not (PY and os.path.exists(PY)), reason="needs CAIRN_MLFLOW_PYTHON (mlflow, scikit-learn)")

LOAD = textwrap.dedent('''
    import json, sys, mlflow, pandas as pd
    model = mlflow.pyfunc.load_model(sys.argv[1])
    out = model.predict(pd.DataFrame({"tenure": [1, 40], "spend": [5, 70]}))
    print("CAIRN-PREDICT " + json.dumps([int(x) for x in out]))
''')


@pytest.fixture(scope="module")
def env():
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import create_engine
    from starlette.requests import Request

    from cairn.common.audit import AuditLog
    from cairn.common.core import InMemoryGraph
    from cairn.common.db import init_db
    from cairn.common.security import Principal
    from cairn.dataaccess.share_models import ShareModels, build_share_models_router
    from cairn.dataaccess.share_packages import SharePackages
    from cairn.dataaccess.shares import Shares, build_shares_router
    from cairn.ml.mlflow_gateway import MlflowGateway, build_ml_router

    d = tempfile.mkdtemp(prefix="share-models-")
    mp = port()
    server = subprocess.Popen([f"{os.path.dirname(PY)}/mlflow", "server", "--backend-store-uri", f"sqlite:///{d}/mlflow.db",
                               "--artifacts-destination", f"{d}/artifacts", "--serve-artifacts",
                               "--host", "127.0.0.1", "--port", str(mp), "--workers", "1"],
                              stdout=open(f"{d}/mlflow.log", "w"), stderr=subprocess.STDOUT)
    wait(f"http://127.0.0.1:{mp}/health")
    engine = create_engine(f"sqlite:///{d}/cairn.db")
    init_db(engine)
    audit = AuditLog(engine)
    gw = MlflowGateway(f"http://127.0.0.1:{mp}", engine, audit, InMemoryGraph(),
                       lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{'.'.join(ns)}.{t},PROD)",
                       lambda p, priv, urn: False)
    from cairn.dataaccess.trino_auth import TrinoTokens

    # Shares of models only: no lakehouse in this test. The tokens are Cairn's own (a share's OAuth token).
    shares = Shares(engine, TrinoTokens(engine, "share-models-" + "k" * 24, "cairn"), None, audit)
    packages = SharePackages(engine, shares, None, lambda: None, None, audit)
    models = ShareModels(engine, shares, gw.upstream, gw.may_manage, packages.recipient, audit)

    def who(request: Request) -> Principal:
        urn = TOKENS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if urn is None:
            raise HTTPException(401, "sign in")
        return Principal(urn, "token")

    def authorize(p, privilege, urn, raise_on_deny=True):
        return False
    app = FastAPI()
    app.include_router(build_shares_router(shares, who, authorize, lambda urn: []))
    app.include_router(build_share_models_router(models, who, authorize))
    app.include_router(build_ml_router(gw, who))
    cp = port()
    api = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cp, timeout_keep_alive=75, log_level="warning"))
    threading.Thread(target=api.run, daemon=True).start()
    wait(f"http://127.0.0.1:{cp}/docs")
    yield {"cairn": f"http://127.0.0.1:{cp}", "mlflow": f"http://127.0.0.1:{mp}", "dir": d, "audit": audit, "shares": shares}
    api.should_exit = True
    server.terminate()
    server.wait(15)


def api(env, token, method, path, **kw):
    return httpx.request(method, f"{env['cairn']}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=60, **kw)


def test_a_recipient_downloads_a_shared_model_version_and_runs_it(env, tmp_path):
    # Ana trains and registers churn (version 1) through Cairn, twice: version 2 is hers too, and not shared.
    for _ in range(2):
        r = client(env, "tok-ana", TRAIN)
        assert r.returncode == 0, r.stderr[-3000:]
    made = {}
    for who, name in (("tok-ana", "partner-m"), ("tok-bob", "bob-m")):
        r = api(env, who, "POST", "/v1/shares", json={"name": name, "recipient": "Partner M", "expires_days": 7})
        assert r.status_code == 200, r.text
        made[name] = r.json()
    sid, secret = made["partner-m"]["credential"].split(":", 1)
    partner = httpx.Client(base_url=env["cairn"], auth=(sid, secret), timeout=60)
    assert partner.get("/v1/shares/recipient/models").json() == {"models": []}

    # Only who manages the model adds it, to a share of their own; and only a version that exists.
    assert api(env, "tok-bob", "POST", "/v1/shares/bob-m/models", json={"model": "churn", "version": 1}).status_code == 404
    assert api(env, "tok-bob", "POST", "/v1/shares/partner-m/models", json={"model": "churn", "version": 1}).status_code == 404
    assert api(env, "tok-ana", "POST", "/v1/shares/partner-m/models", json={"model": "churn", "version": 9}).status_code == 404
    assert api(env, "tok-ana", "POST", "/v1/shares/partner-m/models", json={"model": "../x", "version": 1}).status_code == 422
    r = api(env, "tok-ana", "POST", "/v1/shares/partner-m/models", json={"model": "churn", "version": 1})
    assert r.status_code == 200 and [(m["model"], m["version"]) for m in r.json()["models"]] == [("churn", "1")], r.text
    assert api(env, "tok-ana", "POST", "/v1/shares/partner-m/models", json={"model": "churn", "version": "1"}).status_code == 409

    # The recipient: the version, its files, and each file's bytes.
    assert [(m["model"], m["version"]) for m in partner.get("/v1/shares/recipient/models").json()["models"]] == [("churn", "1")]
    v = partner.get("/v1/shares/recipient/models/churn/versions/1").json()
    paths = [f["path"] for f in v["files"]]
    assert "MLmodel" in paths and any(p.endswith(".pkl") or p.endswith(".skops") for p in paths), paths
    assert "run_id" not in json.dumps(v) and "source" not in v
    folder = tmp_path / "churn"
    for f in v["files"]:
        r = partner.get(f["download"])
        assert r.status_code == 200 and len(r.content) == f["size"], f
        assert r.headers["content-disposition"] == "attachment"
        (folder / f["path"]).parent.mkdir(parents=True, exist_ok=True)
        (folder / f["path"]).write_bytes(r.content)
    # The folder is the model: MLflow loads it and it predicts (no registry, no tracking server).
    r = subprocess.run([PY, "-c", LOAD, str(folder)], capture_output=True, text=True, timeout=300, cwd=str(tmp_path),
                       env={k: v for k, v in os.environ.items() if not k.startswith("MLFLOW_")})
    assert r.returncode == 0, r.stderr[-3000:]
    assert json.loads(r.stdout.split("CAIRN-PREDICT ", 1)[1]) == [1, 0]

    # Nothing else: not version 2, not another model, not a path out of the model's folder, not the registry.
    base = "/v1/shares/recipient/models/churn/versions"
    for path in (f"{base}/2", f"{base}/2/files/MLmodel", "/v1/shares/recipient/models/other/versions/1",
                 f"{base}/1/files/missing.txt", f"{base}/1/files/..%2F..%2F..%2Fmlflow.db", f"{base}/1/files/%2e%2e/MLmodel",
                 f"{base}/1/files/a//MLmodel", f"{base}/1/files/"):
        assert partner.get(path).status_code == 404, path
    mlmodel = (folder / "MLmodel").read_bytes()
    escaped = httpx.get(f"{env['cairn']}{base}/1/files/../../1/files/MLmodel", auth=(sid, secret))      # the client normalises it
    assert escaped.status_code in (200, 404) and (escaped.status_code == 404 or escaped.content == mlmodel)
    for path in ("/v1/ml/api/2.0/mlflow/registered-models/search", "/v1/ml/api/2.0/mlflow/experiments/search",
                 "/v1/shares", "/v1/shares/partner-m/models"):
        assert partner.get(path).status_code == 401, path
    assert api(env, "tok-ana", "GET", "/v1/shares/recipient/models").status_code == 401
    token = httpx.post(f"{env['cairn']}/v1/shares/oauth/tokens", auth=(sid, secret), data={"grant_type": "client_credentials"},
                       timeout=10).json()["access_token"]
    assert api(env, token, "GET", f"{base}/1/files/MLmodel").content == mlmodel       # the share's OAuth token works too
    assert httpx.get(f"{env['cairn']}/v1/shares/recipient/models", auth=(sid, "wrong")).status_code == 401

    # Recorded: audited as the recipient, counted in the share's usage.
    actions = [e for e in env["audit"].entries("shares", limit=200)]
    assert {"share.model_attached", "share.model_file_read"} <= {e["action"] for e in actions}
    assert any(e["action"] == "share.model_file_read" and e["actor"] == f"urn:li:corpuser:share:{sid}" for e in actions)
    usage = api(env, "tok-ana", "GET", "/v1/shares/partner-m/usage").json()
    assert [t["table"] for t in usage["tables"] if t["loads"]] == ["model:churn"], usage

    # Detached: gone at the next request. Attached again, then the share is revoked: refused.
    assert api(env, "tok-bob", "DELETE", "/v1/shares/partner-m/models/churn").status_code == 404
    assert api(env, "tok-ana", "DELETE", "/v1/shares/partner-m/models/churn", params={"version": "1"}).json()["models"] == []
    assert partner.get(f"{base}/1").status_code == 404 and partner.get(f"{base}/1/files/MLmodel").status_code == 404
    assert api(env, "tok-ana", "POST", "/v1/shares/partner-m/models", json={"model": "churn", "version": 2}).status_code == 200
    assert partner.get(f"{base}/2/files/MLmodel").status_code == 200
    assert api(env, "tok-ana", "POST", f"/v1/shares/{sid}/revoke").json()["state"] == "revoked"
    for path in ("/v1/shares/recipient/models", f"{base}/2", f"{base}/2/files/MLmodel"):
        assert partner.get(path).status_code in (401, 403), path
