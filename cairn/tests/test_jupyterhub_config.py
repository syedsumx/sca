"""configure_hub without JupyterHub: the hub names each person exactly as Cairn does, so two Cairn users never
share one hub user (and its running server, files and CAIRN_TOKEN). The end-to-end test with a real
JupyterHub is test_jupyterhub_e2e.py."""

import asyncio
import os
import sys
import types

import pytest


class _Section(types.SimpleNamespace):
    pass


class _Config:
    def __getattr__(self, name):
        sec = _Section()
        setattr(self, name, sec)
        return sec


class _JupyterHubAuthenticator:
    """JupyterHub's own normalisation: names are lowercased, then mapped."""

    username_map: dict = {}

    def normalize_username(self, username):
        username = username.lower()
        return self.username_map.get(username, username)


@pytest.fixture()
def hub(monkeypatch):
    generic = types.ModuleType("oauthenticator.generic")
    generic.GenericOAuthenticator = type("GenericOAuthenticator", (_JupyterHubAuthenticator,), {})
    monkeypatch.setitem(sys.modules, "oauthenticator", types.ModuleType("oauthenticator"))
    monkeypatch.setitem(sys.modules, "oauthenticator.generic", generic)
    monkeypatch.syspath_prepend(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lakehouse-client", "src"))
    for m in ("cairn_lakehouse", "cairn_lakehouse.jupyter"):
        monkeypatch.delitem(sys.modules, m, raising=False)
    from cairn_lakehouse.jupyter import configure_hub

    c = _Config()
    configure_hub(c, cairn_url="http://cairn-api:8000", hub_url="https://notebooks.example.com",
                  client_secret="s" * 32)
    return c


def test_two_cairn_users_never_share_a_hub_user(hub):
    auth = hub.JupyterHub.authenticator_class()
    assert auth.normalize_username("Alice") == "Alice"
    assert auth.normalize_username("alice") == "alice"
    assert auth.normalize_username("Alice") != auth.normalize_username("alice")


def test_a_server_starts_only_for_the_person_cairn_issued_its_token_to(hub, monkeypatch):
    import httpx

    class _Resp:
        status_code = 200

        def json(self):
            return {"token": "t", "user": "Alice"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    class _User:
        def __init__(self, name):
            self.name = name

        async def get_auth_state(self):
            return {"access_token": "hub-token"}

    def spawner(name):
        return types.SimpleNamespace(user=_User(name), environment={}, args=[])

    ok = spawner("Alice")
    asyncio.run(hub.Spawner.pre_spawn_hook(ok))
    assert ok.environment["CAIRN_TOKEN"] == "t"
    other = spawner("alice")
    with pytest.raises(RuntimeError):
        asyncio.run(hub.Spawner.pre_spawn_hook(other))
    assert "CAIRN_TOKEN" not in other.environment
