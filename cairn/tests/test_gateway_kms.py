"""The gateway's keys behind a key manager (L10): the real `cairn gateway` process with its identity wrapped by
HashiCorp Vault's transit engine and by a KMS-compatible server (nsmithuk/local-kms), and a real agent enrolling.

On disk the identity is only ciphertext; a restarted gateway unwraps it and agents see the same keys; a clear
identity is wrapped and removed; a revoked token or a disabled key stops the gateway at start and, once the
cached keys expire, refuses requests that need them (503); a wrapped identity without the key manager, or with
another key, never starts. Needs docker and the hashicorp/vault and nsmithuk/local-kms images."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from cairn.agent.client import GatewayHTTPError

VAULT_IMAGE = os.environ.get("CAIRN_VAULT_IMAGE", "hashicorp/vault:1.20")
KMS_IMAGE = os.environ.get("CAIRN_LOCAL_KMS_IMAGE", "nsmithuk/local-kms:3")


def _image(name: str) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", name], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not (_image(VAULT_IMAGE) and _image(KMS_IMAGE)),
                                reason=f"needs docker with {VAULT_IMAGE} and {KMS_IMAGE}")
ADMIN = "urn:li:corpuser:admin"


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def up(url: str, seconds: float = 60) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            httpx.get(url, timeout=2)
            return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise AssertionError(f"{url} did not come up")


@pytest.fixture(scope="module")
def vault():
    p, name = port(), f"cairn-gw-vault-{uuid.uuid4().hex[:8]}"
    root = "root-" + uuid.uuid4().hex                      # a dev server's root token, in this test only
    env = f"/tmp/{name}.env"
    with open(os.open(env, os.O_WRONLY | os.O_CREAT, 0o600), "w") as fh:
        fh.write(f"VAULT_DEV_ROOT_TOKEN_ID={root}\n")
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host", "-e", "SKIP_SETCAP=1",
                    "--env-file", env, VAULT_IMAGE, "server", "-dev", f"-dev-listen-address=127.0.0.1:{p}"],
                   check=True, capture_output=True, timeout=300)
    os.remove(env)
    addr = f"http://127.0.0.1:{p}"
    try:
        end = time.time() + 60
        while True:
            try:
                if httpx.get(f"{addr}/v1/sys/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.time() < end, "Vault did not start"
            time.sleep(0.3)

        def call(method, path, **kw):
            r = httpx.request(method, f"{addr}/v1/{path}", headers={"X-Vault-Token": root}, timeout=10, **kw)
            assert r.status_code < 300, r.text
            return r.json() if r.content else None
        call("POST", "sys/mounts/transit", json={"type": "transit"})
        call.addr = addr
        yield call
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def gateway_token(vault, key: str) -> str:
    """A token that may only encrypt and decrypt with `key` (what the gateway is given)."""
    vault("PUT", f"sys/policies/acl/gw-{key}", json={"policy": (
        f'path "transit/encrypt/{key}" {{ capabilities = ["update"] }}\n'
        f'path "transit/decrypt/{key}" {{ capabilities = ["update"] }}')})
    return vault("POST", "auth/token/create", json={"policies": [f"gw-{key}"], "no_default_policy": True})["auth"]["client_token"]


@pytest.fixture(scope="module")
def kms():
    p, name = port(), f"cairn-gw-kms-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:8080", KMS_IMAGE],
                   check=True, capture_output=True, timeout=300)
    addr = f"http://127.0.0.1:{p}"
    try:
        def call(target, body):
            r = httpx.post(addr + "/", json=body, timeout=10, headers={
                "X-Amz-Target": f"TrentService.{target}", "Content-Type": "application/x-amz-json-1.1"})
            assert r.status_code == 200, r.text
            return r.json() if r.content else None
        end = time.time() + 60
        while True:
            try:
                call("ListKeys", {})
                break
            except (httpx.HTTPError, AssertionError):
                assert time.time() < end, "local-kms did not start"
                time.sleep(0.3)
        call.addr = addr
        yield call
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


class Gateway:
    """`cairn gateway` as its own process, on its own database and key directory."""

    def __init__(self, tmp, kms_config: dict | None):
        self.tmp, self.kms_config, self.proc = tmp, kms_config, None
        self.keys = tmp / "keys"
        self.port = port()
        self.url = f"http://127.0.0.1:{self.port}"

    def env(self) -> dict:
        env = {**os.environ, "CAIRN_DATABASE_URL": f"sqlite:///{self.tmp}/cairn.db",
               "CAIRN_GATEWAY_KEY_DIR": str(self.keys), "CAIRN_SERVICE_TOKEN": "svc-" + "s" * 24,
               "PYTHONPATH": os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")}
        env.pop("CAIRN_GATEWAY_KEY_KMS", None)
        if self.kms_config is not None:
            (self.tmp / "kms.json").write_text(json.dumps(self.kms_config))
            env["CAIRN_GATEWAY_KEY_KMS"] = f"@{self.tmp / 'kms.json'}"
        return env

    def start(self, expect_ok: bool = True):
        self.proc = subprocess.Popen([sys.executable, "-m", "cairn.cli", "gateway", "--host", "127.0.0.1", "--port",
                                      str(self.port), "--no-dispatcher"], env=self.env(),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if not expect_ok:
            out, _ = self.proc.communicate(timeout=120)
            assert self.proc.returncode != 0, out
            return out
        end = time.time() + 90
        while True:
            assert self.proc.poll() is None, self.proc.stdout.read()
            try:
                httpx.get(f"{self.url}/health", timeout=2)
                return None
            except httpx.HTTPError:
                assert time.time() < end, "the gateway did not start"
                time.sleep(0.3)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(30)

    def enroll(self) -> httpx.Response:
        """A real agent enrolling: an enrolment token from the gateway's database, then the agent protocol."""
        from cairn.agent.client import AgentState, GatewayClient
        from cairn.agent.gateway import GatewayKeys, GatewayService, PoolSpec
        from cairn.common.db import get_engine, init_db

        engine = get_engine(f"sqlite:///{self.tmp}/cairn.db")
        init_db(engine)
        svc = GatewayService(engine, GatewayKeys(str(self.tmp / "unused")), None)
        if not any(p["name"] == "finance" for p in svc.list_pools()):
            svc.create_pool(PoolSpec(name="finance", allowed_platforms=["postgres"]), ADMIN)
        token = svc.create_enrollment_token("finance", ADMIN)["token"]
        client = GatewayClient(self.url, AgentState(str(self.tmp / f"agent-{uuid.uuid4().hex[:6]}")))
        return client.enroll(token, hostname="agent")


def _clear_bytes_absent(keys_dir, identity_doc: dict) -> None:
    """Nothing in the key directory holds the private keys in the clear."""
    blob = b"".join(p.read_bytes() for p in keys_dir.iterdir() if p.is_file())
    for v in identity_doc.values():
        assert v.encode() not in blob


def test_vault_transit_wraps_the_gateways_keys(vault, tmp_path):
    from cairn.agent import crypto, keywrap

    vault("POST", "transit/keys/cairn-gateway")
    (tmp_path / "vault-token").write_text(gateway_token(vault, "cairn-gateway"))
    cfg = {"type": "vault-transit", "addr": vault.addr, "key": "cairn-gateway",
           "token_file": str(tmp_path / "vault-token"), "cache_seconds": 2}
    # An identity in the clear from before: wrapped at the first start, the clear copy removed.
    tmp_path.joinpath("keys").mkdir()
    before = crypto.Identity.generate()
    before.save(str(tmp_path / "keys" / "gateway-identity.json"))
    gw = Gateway(tmp_path, cfg)
    gw.start()
    try:
        first = gw.enroll()
        assert first["gateway_encryption_public"] == before.encryption_public
        assert not (gw.keys / "gateway-identity.json").exists()
        wrapped = json.loads((gw.keys / "gateway-identity.kms.json").read_text())
        assert wrapped["kms"] == "vault-transit" and wrapped["ciphertext"].startswith("vault:v1:")
        _clear_bytes_absent(gw.keys, before.to_dict())
        # Vault decides: a token revoked, and once the cached keys expire the gateway refuses what needs them.
        token = (tmp_path / "vault-token").read_text()
        vault("POST", "auth/token/revoke", json={"token": token})
        time.sleep(2.5)
        with pytest.raises(GatewayHTTPError) as e:
            gw.enroll()
        assert e.value.status == 503 and "keys are unavailable" in str(e.value), e.value
    finally:
        gw.stop()
    # Restarted without a working token: it does not start.
    out = gw.start(expect_ok=False)
    assert "keys are unavailable" in out and "Vault refused decrypt" in out, out[-2000:]
    # With a new token it starts with the same keys; agents see the same gateway.
    (tmp_path / "vault-token").write_text(gateway_token(vault, "cairn-gateway"))
    gw.start()
    try:
        assert gw.enroll()["gateway_encryption_public"] == before.encryption_public
    finally:
        gw.stop()
    # Rotated in Vault: the old wrapping still opens.
    vault("POST", "transit/keys/cairn-gateway/rotate")
    gw.start()
    try:
        assert gw.enroll()["gateway_signing_public"] == before.signing_public
    finally:
        gw.stop()
    # Never started with keys the key manager does not vouch for: no configuration, another key, a token for
    # another key, or a wrapped file moved to another purpose.
    gw.kms_config = None
    assert "set CAIRN_GATEWAY_KEY_KMS" in gw.start(expect_ok=False)
    vault("POST", "transit/keys/other")
    (tmp_path / "other-token").write_text(gateway_token(vault, "other"))
    gw.kms_config = {**cfg, "key": "other", "token_file": str(tmp_path / "other-token")}
    assert "wrapped by vault-transit key cairn-gateway" in gw.start(expect_ok=False)
    gw.kms_config = {**cfg, "token_file": str(tmp_path / "other-token")}
    assert "permission denied" in gw.start(expect_ok=False)
    w = keywrap.from_config({k: v for k, v in cfg.items() if k != "cache_seconds"})
    with pytest.raises(keywrap.KmsError, match="not for this purpose"):
        w.decrypt(wrapped["ciphertext"], "something-else")
    # Settings that cannot work are refused.
    for bad in ({"type": "vault"}, {"type": "vault-transit", "addr": vault.addr},
                {**cfg, "extra": "x"}, {**cfg, "cache_seconds": -1}):
        os.environ["CAIRN_GATEWAY_KEY_KMS"] = json.dumps(bad)
        try:
            with pytest.raises(keywrap.KmsError):
                keywrap.from_env()
        finally:
            os.environ.pop("CAIRN_GATEWAY_KEY_KMS")


def test_aws_kms_wraps_the_gateways_keys(kms, tmp_path):
    key = kms("CreateKey", {"Description": "cairn gateway"})["KeyMetadata"]["KeyId"]
    kms("CreateAlias", {"AliasName": "alias/cairn-gateway", "TargetKeyId": key})
    (tmp_path / "secret").write_text("local-kms-does-not-check-" + "s" * 8)
    cfg = {"type": "aws-kms", "region": "eu-west-1", "addr": kms.addr, "key": "alias/cairn-gateway",
           "access_key": "AKIDEXAMPLE", "secret_key_file": str(tmp_path / "secret"), "cache_seconds": 1}
    gw = Gateway(tmp_path, cfg)
    gw.start()
    try:
        first = gw.enroll()
        assert sorted(p.name for p in gw.keys.iterdir()) == ["gateway-identity.kms.json"]
        assert json.loads((gw.keys / "gateway-identity.kms.json").read_text())["kms"] == "aws-kms"
        # A disabled key: refused once the cached keys expire.
        kms("DisableKey", {"KeyId": key})
        time.sleep(1.5)
        with pytest.raises(GatewayHTTPError) as e:
            gw.enroll()
        assert e.value.status == 503 and "keys are unavailable" in str(e.value), e.value
    finally:
        gw.stop()
    out = gw.start(expect_ok=False)
    assert "keys are unavailable" in out and "KMS refused Decrypt" in out, out[-2000:]
    kms("EnableKey", {"KeyId": key})
    gw.start()
    try:
        assert gw.enroll()["gateway_encryption_public"] == first["gateway_encryption_public"]
    finally:
        gw.stop()
    # The CLI wraps an identity for a Secret the replicas share.
    d = tmp_path / "for-secret"
    out = subprocess.run([sys.executable, "-m", "cairn.cli", "gateway-keys", "wrap", "--key-dir", str(d)],
                         env=gw.env(), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    made = json.loads(out.stdout)
    assert made["kms"] == "aws-kms" and os.path.exists(made["wrapped"]) and not (d / "gateway-identity.json").exists()
