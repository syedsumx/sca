"""The Cairn Core entrypoint shim (core/rootfs/usr/local/bin/cairn-env)."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHIM = ROOT / "core" / "rootfs" / "usr" / "local" / "bin" / "cairn-env"
ETC = ROOT / "core" / "rootfs" / "etc" / "cairn"


def run(tmp_path, env: dict, maps=("00-common.map", "10-core.map")) -> dict:
    (tmp_path / "env.d").mkdir(exist_ok=True)
    (tmp_path / "init.d").mkdir(exist_ok=True)
    for m in maps:
        shutil.copy(ETC / "env.d" / m, tmp_path / "env.d" / m)
    for hook in (ETC / "init.d").glob("*.sh"):
        shutil.copy(hook, tmp_path / "init.d" / hook.name)
    base = {"PATH": os.environ["PATH"], "CAIRN_ENV_ROOT": str(tmp_path)}
    out = subprocess.run(["sh", str(SHIM), "env"], env={**base, **env}, capture_output=True, text=True, check=True)
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def test_cairn_setting_overrides_baked_engine_default(tmp_path):
    # The image bakes AUTH_POLICIES_ENABLED=true; authoritative mode must be able to turn it off.
    env = run(tmp_path, {"AUTH_POLICIES_ENABLED": "true", "CAIRN_NATIVE_POLICIES_ENABLED": "false",
                         "VIEW_AUTHORIZATION_ENABLED": "true", "CAIRN_VIEW_AUTHORIZATION_ENABLED": "false",
                         "JAVA_OPTS": "-Xmx1g", "CAIRN_JAVA_OPTS": "-Xmx4g", "REACT_APP_TITLE": "Cairn",
                         "CAIRN_APP_TITLE": "Acme Catalog"})
    assert env["AUTH_POLICIES_ENABLED"] == "false"
    assert env["VIEW_AUTHORIZATION_ENABLED"] == "false"
    assert env["JAVA_OPTS"] == "-Xmx4g"
    assert env["REACT_APP_TITLE"] == "Acme Catalog"


def test_engine_variable_applies_when_cairn_setting_unset(tmp_path):
    env = run(tmp_path, {"AUTH_POLICIES_ENABLED": "false"})
    assert env["AUTH_POLICIES_ENABLED"] == "false"


def test_secrets_and_db_url_are_mapped(tmp_path):
    env = run(tmp_path, {"CAIRN_SYSTEM_CLIENT_SECRET": "s3", "CAIRN_SESSION_SIGNING_KEY": "k",
                         "CAIRN_DB_HOST": "mysql:3306", "CAIRN_DB_NAME": "cairn"})
    assert env["DATAHUB_SYSTEM_CLIENT_SECRET"] == "s3"
    assert env["DATAHUB_TOKEN_SERVICE_SIGNING_KEY"] == "k"
    assert env["EBEAN_DATASOURCE_URL"].startswith("jdbc:mysql://mysql:3306/cairn?")
    assert env["EBEAN_DATASOURCE_DRIVER"] == "com.mysql.cj.jdbc.Driver"


def test_catalog_can_live_in_postgres(tmp_path):
    # The image bakes the MySQL driver; the type must win over it.
    env = run(tmp_path, {"CAIRN_DB_TYPE": "postgres", "CAIRN_DB_HOST": "cairn-postgres:5432",
                         "CAIRN_DB_USER": "cairn", "EBEAN_DATASOURCE_DRIVER": "com.mysql.jdbc.Driver"})
    assert env["EBEAN_DATASOURCE_URL"] == "jdbc:postgresql://cairn-postgres:5432/cairn_catalog?sslmode=prefer"
    assert env["EBEAN_DATASOURCE_DRIVER"] == "org.postgresql.Driver"
    assert env["EBEAN_DATASOURCE_HOST"] == "cairn-postgres:5432" and env["EBEAN_DATASOURCE_USERNAME"] == "cairn"
    explicit = run(tmp_path, {"CAIRN_DB_TYPE": "postgres", "CAIRN_DB_URL": "jdbc:postgresql://db/x?sslmode=require"})
    assert explicit["EBEAN_DATASOURCE_URL"] == "jdbc:postgresql://db/x?sslmode=require"
    with pytest.raises(subprocess.CalledProcessError):
        run(tmp_path, {"CAIRN_DB_TYPE": "oracle"})


def test_schema_registry_derived_from_core_address(tmp_path):
    env = run(tmp_path, {"CAIRN_CORE_API_HOST": "cairn-core", "CAIRN_CORE_API_PORT": "8080"}, maps=("00-common.map",))
    assert env["KAFKA_SCHEMAREGISTRY_URL"] == "http://cairn-core:8080/schema-registry/api/"
    assert env["SCHEMA_REGISTRY_URL"] == env["KAFKA_SCHEMAREGISTRY_URL"]


def test_web_admin_login_requires_password(tmp_path):
    users = tmp_path / "user.props"
    with pytest.raises(subprocess.CalledProcessError):
        run(tmp_path, {"CAIRN_WEB_USERS_FILE": str(users)}, maps=("00-common.map", "10-web.map"))
    run(tmp_path, {"CAIRN_WEB_USERS_FILE": str(users), "CAIRN_ADMIN_PASSWORD": "pw"}, maps=("00-common.map", "10-web.map"))
    assert users.read_text() == "admin:pw\n" and oct(users.stat().st_mode & 0o777) == "0o600"


def test_secrets_can_be_files(tmp_path):
    secret = tmp_path / "db-password"
    secret.write_text("s3cr3t-from-file\n")
    env = run(tmp_path, {"CAIRN_DB_PASSWORD_FILE": str(secret)})
    assert env["CAIRN_DB_PASSWORD"] == "s3cr3t-from-file"
    assert env["EBEAN_DATASOURCE_PASSWORD"] == "s3cr3t-from-file"        # and mapped like any setting


def test_secret_file_conflicts_and_missing_files_fail(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("x")
    for env in ({"CAIRN_DB_PASSWORD": "y", "CAIRN_DB_PASSWORD_FILE": str(secret)},
                {"CAIRN_DB_PASSWORD_FILE": str(tmp_path / "missing")}):
        with pytest.raises(subprocess.CalledProcessError) as e:
            run(tmp_path, env)
        assert e.value.returncode == 64


def test_path_settings_are_not_treated_as_secrets(tmp_path):
    users = tmp_path / "users"
    users.write_text("alice:pw\n")
    env = run(tmp_path, {"CAIRN_LOCAL_USERS_FILE": str(users)})
    assert "CAIRN_LOCAL_USERS" not in env


def test_python_services_load_secret_files(tmp_path):
    from cairn.common.config import load_secret_files

    (tmp_path / "k").write_text("sk-test\n")
    environ = {"ANTHROPIC_API_KEY_FILE": str(tmp_path / "k"), "CAIRN_SERVICE_TOKEN_FILE": str(tmp_path / "k"),
               "CAIRN_CORE_TOKEN_FILE": "/run/cairn/core_token", "OTHER_FILE": "/x"}
    assert sorted(load_secret_files(environ)) == ["ANTHROPIC_API_KEY", "CAIRN_SERVICE_TOKEN"]
    assert environ["ANTHROPIC_API_KEY"] == "sk-test" and "CAIRN_CORE_TOKEN" not in environ
    with pytest.raises(RuntimeError, match="both"):
        load_secret_files({"CAIRN_X": "a", "CAIRN_X_FILE": str(tmp_path / "k")})
    with pytest.raises(RuntimeError, match="cannot read"):
        load_secret_files({"CAIRN_X_FILE": str(tmp_path / "nope")})
