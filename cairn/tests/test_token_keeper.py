"""Service-token rotation logic (deploy/bootstrap/mint_core_token.py), without a live stack."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os

import pytest

_spec = importlib.util.spec_from_file_location(
    "mint_core_token", os.path.join(os.path.dirname(__file__), "..", "deploy", "bootstrap", "mint_core_token.py"))
mct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mct)

DAY = 86400.0


@pytest.fixture
def keeper(tmp_path, monkeypatch):
    calls = {"mint": [], "revoke": []}

    def fake_mint(core_url, token, actor, duration, name, description):
        calls["mint"].append((token, actor, duration))
        return f"tok{len(calls['mint'])}", f"id{len(calls['mint'])}"

    monkeypatch.setattr(mct, "mint", fake_mint)
    monkeypatch.setattr(mct, "revoke", lambda core_url, token, tid: calls["revoke"].append((token, tid)))
    out = str(tmp_path / "core_token")
    mct.write_state(out, "tok0", {"id": "id0", "actor": mct.SERVICE_USER, "issued_at": 0, "retired": []})
    args = argparse.Namespace(out=out, core_url="http://core", duration="ONE_MONTH", rotate_days=7, grace_minutes=15)
    return args, calls


def state(args):
    return mct.read_state(args.out)


def test_not_due_does_nothing(keeper):
    args, calls = keeper
    assert mct.rotate_once(args, now=6 * DAY) is False
    assert calls == {"mint": [], "revoke": []}
    assert state(args)[0] == "tok0"


def test_due_rotates_with_own_token_and_retires_old(keeper):
    args, calls = keeper
    assert mct.rotate_once(args, now=7 * DAY) is True
    assert calls["mint"] == [("tok0", mct.SERVICE_USER, "ONE_MONTH")]   # minted by the account itself
    token, meta = state(args)
    assert token == "tok1" and meta["id"] == "id1"
    assert meta["retired"] == [{"id": "id0", "retired_at": 7 * DAY}]
    assert calls["revoke"] == []                                          # still inside the grace period


def test_retired_token_revoked_after_grace(keeper):
    args, calls = keeper
    mct.rotate_once(args, now=7 * DAY)
    _, meta = state(args)
    meta["issued_at"] = 7 * DAY
    mct.write_state(args.out, "tok1", meta)
    mct.rotate_once(args, now=7 * DAY + 10 * 60)
    assert calls["revoke"] == []
    mct.rotate_once(args, now=7 * DAY + 16 * 60)
    assert calls["revoke"] == [("tok1", "id0")]                           # revoked with the new token
    assert state(args)[1]["retired"] == []


def test_failed_revoke_is_retried(keeper, monkeypatch):
    args, calls = keeper
    mct.rotate_once(args, now=7 * DAY)
    _, meta = state(args)
    meta["issued_at"] = 7 * DAY
    mct.write_state(args.out, "tok1", meta)

    def boom(*a):
        raise RuntimeError("core down")
    monkeypatch.setattr(mct, "revoke", boom)
    mct.rotate_once(args, now=8 * DAY)
    assert [r["id"] for r in state(args)[1]["retired"]] == ["id0"]


def test_state_files_are_world_readable_and_atomic(keeper):
    args, _ = keeper
    assert oct(os.stat(args.out).st_mode & 0o777) == "0o644"
    assert not os.path.exists(args.out + ".tmp")
    assert json.load(open(args.out + ".json"))["actor"] == mct.SERVICE_USER


def test_least_privilege_grant():
    dangerous = {"MANAGE_POLICIES", "MANAGE_USERS_AND_GROUPS", "MANAGE_USER_CREDENTIALS", "MANAGE_ACCESS_TOKENS",
                 "MANAGE_SECRETS", "MANAGE_SERVICE_ACCOUNTS", "MANAGE_SYSTEM_OPERATIONS", "MANAGE_GLOBAL_SETTINGS",
                 "RESTORE_INDICES_PRIVILEGE", "TRUNCATE_TIMESERIES_INDEX_PRIVILEGE", "SET_WRITEABLE_PRIVILEGE",
                 "DELETE_ENTITY"}
    assert not dangerous & set(mct.PLATFORM_PRIVILEGES + mct.METADATA_PRIVILEGES)


# ------------------------------------------------------------------------------ Kubernetes Secret store
@pytest.fixture
def fake_k8s(tmp_path, monkeypatch):
    """Minimal API server: GET/PATCH one Secret, bearer token required, merge-patch semantics."""
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    secrets = {"cairn-core-token": {"metadata": {"name": "cairn-core-token"}, "data": {}}}
    seen = []

    class H(BaseHTTPRequestHandler):
        def _secret(self):
            assert self.headers["Authorization"] == "Bearer sa-token"
            parts = self.path.split("/")
            assert parts[:5] == ["", "api", "v1", "namespaces", "cairn"]
            return parts[-1]

        def do_GET(self):
            name = self._secret()
            body = json.dumps(secrets[name]).encode() if name in secrets else b"{}"
            self.send_response(200 if name in secrets else 404)
            self.end_headers()
            self.wfile.write(body)

        def do_PATCH(self):
            name = self._secret()
            seen.append(self.headers["Content-Type"])
            patch = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if name not in secrets:
                self.send_response(404)
                self.end_headers()
                return
            secrets[name]["data"].update(patch["data"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(secrets[name]).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (tmp_path / "token").write_text("sa-token")
    (tmp_path / "namespace").write_text("cairn")
    monkeypatch.setenv("CAIRN_K8S_NAMESPACE_FILE", str(tmp_path / "namespace"))
    monkeypatch.setenv("CAIRN_K8S_API", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("CAIRN_K8S_TOKEN_FILE", str(tmp_path / "token"))
    yield secrets, seen, base64
    srv.shutdown()


def test_secret_store_round_trip(fake_k8s):
    secrets, seen, base64 = fake_k8s
    store = mct.SecretStore("cairn-core-token", namespace="cairn")
    assert store.read() == (None, {})
    store.write("tok1", {"id": "id1", "actor": mct.SERVICE_USER})
    assert seen == ["application/merge-patch+json"]
    assert base64.b64decode(secrets["cairn-core-token"]["data"]["core_token"]) == b"tok1"
    assert store.read() == ("tok1", {"id": "id1", "actor": mct.SERVICE_USER})


def test_secret_store_missing_secret_is_an_error(fake_k8s):
    store = mct.SecretStore("not-created-by-the-chart", namespace="cairn")
    assert store.read() == (None, {})
    with pytest.raises(RuntimeError, match="HTTP 404"):
        store.write("tok", {})


def test_rotation_through_secret_store(fake_k8s, monkeypatch):
    calls = []
    monkeypatch.setattr(mct, "mint", lambda *a: (calls.append(a[1]) or "tok2", "id2"))
    monkeypatch.setattr(mct, "revoke", lambda *a: None)
    store = mct.SecretStore("cairn-core-token")          # namespace read from the service-account mount
    store.write("tok1", {"id": "id1", "actor": mct.SERVICE_USER, "issued_at": 0, "retired": []})
    args = argparse.Namespace(out="/unused", k8s_secret="cairn-core-token", core_url="http://core",
                              duration="ONE_MONTH", rotate_days=7, grace_minutes=15)
    assert mct.rotate_once(args, now=8 * DAY)
    assert calls == ["tok1"] and store.read()[0] == "tok2"


# ------------------------------------------------------------------------------ service-account policies
def test_policy_urns_are_content_addressed():
    a, b = mct.desired_policies(), mct.desired_policies()
    assert a == b and len(a) == 2
    assert all(u.startswith(mct.POLICY_PREFIX) and not p["editable"] for u, p in a.items())


@pytest.fixture
def core_calls(monkeypatch):
    calls = {"ingest": [], "deleted": [], "present": set()}
    monkeypatch.setattr(mct, "ingest", lambda core, tok, et, urn, aspect, body: calls["ingest"].append((et, urn)))

    def fake_gql(core, tok, query, variables=None):
        if "listPolicies" in query:
            return {"listPolicies": {"total": len(calls["present"]), "policies": [{"urn": u} for u in calls["present"]]
                                     + [{"urn": "urn:li:dataHubPolicy:0"}]}}
        if "deletePolicy" in query:
            calls["deleted"].append(variables["urn"])
            return {"deletePolicy": variables["urn"]}
        raise AssertionError(query)
    monkeypatch.setattr(mct, "gql", fake_gql)
    return calls


def test_first_bootstrap_creates_user_and_policies(core_calls):
    mct.ensure_service_account("http://core", "admin")
    assert ("corpuser", mct.SERVICE_USER) in core_calls["ingest"]
    assert {u for et, u in core_calls["ingest"] if et == "dataHubPolicy"} == set(mct.desired_policies())
    assert core_calls["deleted"] == []


def test_rerun_leaves_unchanged_policies_alone(core_calls):
    core_calls["present"] = set(mct.desired_policies())
    mct.ensure_service_account("http://core", "admin")
    assert [u for et, u in core_calls["ingest"] if et == "dataHubPolicy"] == []   # non-editable: never re-written
    assert core_calls["deleted"] == []


def test_changed_grant_replaces_policy_and_deletes_old(core_calls):
    old = {"urn:li:dataHubPolicy:cairn-service-platform", "urn:li:dataHubPolicy:cairn-service-metadata"}
    core_calls["present"] = old | {next(iter(mct.desired_policies()))}
    mct.ensure_service_account("http://core", "admin")
    created = [u for et, u in core_calls["ingest"] if et == "dataHubPolicy"]
    assert len(created) == 1 and sorted(core_calls["deleted"]) == sorted(old)   # never root policies


def test_bootstrap_token_is_minted_by_the_account_itself(tmp_path, monkeypatch):
    log = []
    monkeypatch.setattr(mct, "admin_token", lambda url, pw: ("ADMIN", "admin-id"))
    monkeypatch.setattr(mct, "ensure_service_account", lambda core, tok: log.append(("ensure", tok)))
    monkeypatch.setattr(mct, "wait_effective", lambda core, tok: log.append(("wait", tok)))
    monkeypatch.setattr(mct, "whoami", lambda core, tok: None)

    def fake_mint(core, tok, actor, duration, name, desc):
        log.append(("mint", tok, duration))
        return ("SEED", "seed-id") if tok == "ADMIN" else ("LONG", "long-id")
    monkeypatch.setattr(mct, "mint", fake_mint)
    monkeypatch.setattr(mct, "revoke", lambda core, tok, tid: log.append(("revoke", tok, tid)))
    args = argparse.Namespace(out=str(tmp_path / "t"), core_url="http://core", frontend_url="http://web",
                              password="pw", duration="ONE_MONTH")
    mct.bootstrap(args)
    assert ("mint", "SEED", "ONE_MONTH") in log                 # long-lived token minted by the account
    assert mct.read_state(args.out)[0] == "LONG"
    assert ("revoke", "ADMIN", "seed-id") in log and ("revoke", "ADMIN", "admin-id") in log


def test_expired_retired_tokens_are_dropped(keeper, monkeypatch):
    args, calls = keeper
    mct.rotate_once(args, now=7 * DAY)
    _, meta = state(args)
    meta["issued_at"] = 7 * DAY
    mct.write_state(args.out, "tok1", meta)
    monkeypatch.setattr(mct, "revoke", lambda *a: (_ for _ in ()).throw(RuntimeError("403")))
    mct.rotate_once(args, now=8 * DAY)
    assert state(args)[1]["retired"]                             # still retried within its lifetime
    mct.rotate_once(args, now=7 * DAY + 31 * DAY)
    assert "id0" not in [r["id"] for r in state(args)[1]["retired"]]   # expired: nothing left to revoke
