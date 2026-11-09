"""The command line's own pieces, without a Cairn: the profiles file, which profile and token a command uses, the
0600 credentials file, completion scripts and candidates, exit codes, and "did you mean"."""

import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

from cairn_cli import config as cfg
from cairn_cli.completion import SCRIPTS, candidates
from cairn_cli.credentials import FileStore, open_store
from cairn_cli.errors import AUTH, DENIED, ERROR, NOT_FOUND, USAGE, CliError, for_status
from cairn_cli.main import build_parser

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")


@pytest.fixture
def home(tmp_path, monkeypatch):
    for k in ("CAIRN_URL", "CAIRN_TOKEN", "CAIRN_TOKEN_FILE", "CAIRN_PROFILE", "XDG_CONFIG_HOME", "CAIRN_CREDENTIAL_STORE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CAIRN_CONFIG_DIR", str(tmp_path / "cfg"))
    try:                      # keyring keeps the backend it chose for the process: choose again in each test
        import keyring.core

        monkeypatch.setattr(keyring.core, "_keyring_backend", None)
    except ImportError:
        pass
    return tmp_path / "cfg"


def test_the_profiles_file_round_trips_and_holds_no_secrets(home):
    c = cfg.Config.load()
    assert c.profiles == {} and c.default is None
    c.put("prod", "https://cairn.example.com/cairn/api/")
    c.put("dev", "https://dev.example.com", ca_file='/etc/ssl/ca "dev".pem')
    c.save()
    text = (home / "config.toml").read_text()
    assert 'default_profile = "prod"' in text and 'url = "https://cairn.example.com/cairn/api"' in text
    assert stat.S_IMODE((home / "config.toml").stat().st_mode) == 0o600
    again = cfg.Config.load()
    assert again.profiles["dev"]["ca_file"] == '/etc/ssl/ca "dev".pem' and again.default == "prod"
    again.set_default("dev")
    again.remove("dev")
    assert again.default == "prod"
    with pytest.raises(CliError) as e:
        again.set_default("nope")
    assert e.value.code == NOT_FOUND
    with pytest.raises(CliError):
        cfg.check_name("../etc")
    (home / "config.toml").write_text("profiles = 3\n")
    with pytest.raises(CliError, match="must be a table"):
        cfg.Config.load()


def test_which_profile_and_token_a_command_uses(home, monkeypatch, tmp_path):
    c = cfg.Config.load()
    c.put("prod", "https://prod.example.com")
    c.put("dev", "https://dev.example.com")
    c.save()
    assert cfg.resolve(None).profile == "prod"                                   # the default
    monkeypatch.setenv("CAIRN_PROFILE", "dev")
    assert cfg.resolve(None).url == "https://dev.example.com"                    # CAIRN_PROFILE over the default
    assert cfg.resolve("prod").url == "https://prod.example.com"                 # --profile over CAIRN_PROFILE
    with pytest.raises(CliError) as e:
        cfg.resolve("missing")
    assert e.value.code == AUTH and "cairn login --profile missing" in str(e.value)
    # CAIRN_URL and CAIRN_TOKEN win over every profile (CI).
    monkeypatch.setenv("CAIRN_TOKEN", "pat-x")
    t = cfg.resolve("prod")
    assert t.source == "environment" and t.env_token == "pat-x" and t.url == "https://prod.example.com"
    monkeypatch.setenv("CAIRN_URL", "https://ci.example.com/")
    assert cfg.resolve("prod").url == "https://ci.example.com"
    monkeypatch.delenv("CAIRN_TOKEN")
    tf = tmp_path / "tok"
    tf.write_text("pat-from-file\n")
    monkeypatch.setenv("CAIRN_TOKEN_FILE", str(tf))
    assert cfg.resolve(None).env_token == "pat-from-file"
    monkeypatch.delenv("CAIRN_TOKEN_FILE")
    # A profile's tokens never go to a Cairn named only in CAIRN_URL.
    with pytest.raises(CliError, match="set CAIRN_TOKEN"):
        cfg.resolve("prod")
    monkeypatch.setenv("CAIRN_URL", "https://prod.example.com")
    assert cfg.resolve("prod").source == "profile"


def test_without_a_keychain_tokens_go_to_a_0600_file_and_loose_permissions_are_refused(home, monkeypatch, capsys):
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    store = open_store(home)
    assert store.kind == "file" and "no OS keychain available" in capsys.readouterr().err
    store.set("prod", {"access_token": "cla_secret", "url": "https://x"})
    path = home / "credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(home.stat().st_mode) == 0o700
    assert store.get("prod")["access_token"] == "cla_secret"
    os.chmod(path, 0o640)
    with pytest.raises(CliError) as e:
        store.get("prod")
    assert e.value.code == AUTH and "chmod 600" in str(e.value) and "cla_secret" not in str(e.value)
    os.chmod(path, 0o600)
    store.delete("prod")
    assert json.loads(path.read_text()) == {}
    monkeypatch.setenv("CAIRN_CREDENTIAL_STORE", "keyring")
    with pytest.raises(CliError, match="no OS keychain"):
        open_store(home)
    monkeypatch.setenv("CAIRN_CREDENTIAL_STORE", "file")
    assert isinstance(open_store(home), FileStore)


def test_a_keyring_backend_is_used_when_there_is_one(home, monkeypatch, tmp_path):
    pytest.importorskip("keyrings.alt")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyrings.alt.file.PlaintextKeyring")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    store = open_store(home)
    store.set("prod", {"access_token": "cla_x"})
    assert store.kind == "keyring" and store.get("prod") == {"access_token": "cla_x"}
    assert not (home / "credentials.json").exists()
    store.delete("prod")
    assert store.get("prod") is None


def test_exit_codes():
    assert [for_status(s) for s in (401, 403, 404, 410, 400, 500)] == [AUTH, DENIED, NOT_FOUND, NOT_FOUND, ERROR, ERROR]
    assert (USAGE, AUTH, DENIED, NOT_FOUND) == (2, 3, 4, 5)
    run = subprocess.run([sys.executable, "-m", "cairn_cli.main", "qurey"], env={**os.environ, "PYTHONPATH": SRC},
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 2 and "did you mean 'query'" in run.stderr


def test_completion_scripts_and_candidates(home, monkeypatch):
    p = build_parser()
    assert "query" in candidates(p, [], "q") and "__complete" not in candidates(p, [], "")
    assert candidates(p, ["query"], "--fo") == ["--format"]
    assert candidates(p, ["query", "--format"], "j") == ["json", "jsonl"]
    assert candidates(p, ["profiles"], "") == ["list", "use", "remove"]
    c = cfg.Config.load()
    c.put("prod", "https://prod.example.com")
    c.put("dev", "https://dev.example.com")
    c.save()
    assert candidates(p, ["profiles", "use"], "") == ["dev", "prod"]
    assert candidates(p, ["whoami", "--profile"], "p") == ["prod"]
    assert candidates(p, ["completion"], "") == ["bash", "zsh", "fish", "powershell"]
    assert candidates(p, ["flows", "run"], "") == []                      # no Cairn reachable: nothing, quietly
    for shell, script in SCRIPTS.items():
        out = subprocess.run([sys.executable, "-m", "cairn_cli.main", "completion", shell],
                             env={**os.environ, "PYTHONPATH": SRC}, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and out.stdout == script and "__complete" in script
    for shell, flag in (("bash", "-n"), ("zsh", "-n"), ("fish", "--no-execute")):
        if shutil.which(shell):
            check = subprocess.run([shell, flag], input=SCRIPTS[shell], capture_output=True, text=True, timeout=30)
            assert check.returncode == 0, (shell, check.stderr)
    if shutil.which("bash"):          # the bash function, run as bash runs it on TAB
        script = SCRIPTS["bash"] + 'COMP_WORDS=(cairn profiles us); COMP_CWORD=2; _cairn_complete; echo "${COMPREPLY[@]}"'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "PATH": _cairn_on_path(home) + os.pathsep + os.environ["PATH"]})
        assert out.stdout.split() == ["use"], out.stderr


def _cairn_on_path(home) -> str:
    """A `cairn` on PATH that runs this checkout's command line (as an installed one would)."""
    bindir = home.parent / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "cairn"
    exe.write_text(f"#!/bin/sh\nPYTHONPATH={SRC} exec {sys.executable} -m cairn_cli.main \"$@\"\n")
    exe.chmod(0o755)
    return str(bindir)
