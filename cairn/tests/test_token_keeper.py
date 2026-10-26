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
