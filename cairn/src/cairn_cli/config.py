"""Profiles: several Cairns in one configuration file, ``config.toml`` in the configuration directory.

The directory is ``CAIRN_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/cairn``, else ``%APPDATA%\\cairn`` on Windows, else
``~/.config/cairn``. The file holds no secrets (tokens live in the OS keychain, see :mod:`cairn_cli.credentials`)::

    default_profile = "prod"

    [profiles.prod]
    url = "https://cairn.example.com/cairn/api"

    [profiles.dev]
    url = "https://cairn.dev.example.com/cairn/api"
    ca_file = "/etc/ssl/dev-ca.pem"

Which profile a command uses: ``--profile``, else ``CAIRN_PROFILE``, else ``default_profile``, else ``default``.
``CAIRN_URL`` and ``CAIRN_TOKEN`` (or ``CAIRN_TOKEN_FILE``) win over any profile, for CI.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cairn_cli.errors import AUTH, NOT_FOUND, USAGE, CliError

PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
KEYS = ("url", "ca_file")


def config_dir() -> Path:
    explicit = os.environ.get("CAIRN_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "cairn"
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "cairn"
    return Path.home() / ".config" / "cairn"


def _loads(text: str) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - Python 3.10
        import tomli as tomllib
    return tomllib.loads(text)


def _quote(value: str) -> str:
    # A TOML basic string: JSON's escapes are TOML's (non-ASCII is written as itself, so no surrogate pairs).
    return json.dumps(str(value), ensure_ascii=False)


def _dumps(data: dict) -> str:
    lines = ["# Cairn command line: profiles (written by `cairn login` and `cairn profiles`; no secrets here)."]
    if data.get("default_profile"):
        lines.append(f"default_profile = {_quote(data['default_profile'])}")
    for name in sorted(data.get("profiles") or {}):
        lines += ["", f"[profiles.{_quote(name)}]"]
        for k, v in sorted((data["profiles"][name] or {}).items()):
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            elif v is not None:
                lines.append(f"{k} = {_quote(v)}")
    return "\n".join(lines) + "\n"


def check_name(name: str) -> str:
    if not PROFILE_NAME.match(name or ""):
        raise CliError(f"profile name {name!r}: letters, digits, '.', '_' and '-' (at most 63)", USAGE)
    return name


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+", url):
        raise CliError(f"{url or 'the URL'} is not an http(s) URL", USAGE)
    return url


@dataclass
class Config:
    path: Path
    data: dict = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Optional[Path] = None) -> "Config":
        path = (directory or config_dir()) / "config.toml"
        if not path.exists():
            return cls(path, {"profiles": {}})
        try:
            data = _loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 - tomllib.TOMLDecodeError, OSError, UnicodeDecodeError
            raise CliError(f"{path}: cannot read it ({e}); fix or remove it")
        profiles = data.get("profiles")
        if profiles is not None and not isinstance(profiles, dict):
            raise CliError(f"{path}: [profiles] must be a table")
        data["profiles"] = {str(k): dict(v) for k, v in (profiles or {}).items() if isinstance(v, dict)}
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_name(f".config.toml.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # "\n" on every OS: the same bytes Cairn Desktop writes (desktop/src/profiles.js), Windows included.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_dumps(self.data))
        os.replace(tmp, self.path)

    @property
    def profiles(self) -> dict[str, dict]:
        return self.data.setdefault("profiles", {})

    @property
    def default(self) -> Optional[str]:
        return self.data.get("default_profile")

    def set_default(self, name: str) -> None:
        if name not in self.profiles:
            raise CliError(f"no profile {name}; `cairn profiles list` shows them", NOT_FOUND)
        self.data["default_profile"] = name

    def put(self, name: str, url: str, ca_file: Optional[str] = None) -> None:
        check_name(name)
        entry = dict(self.profiles.get(name) or {})
        entry["url"] = normalize_url(url)
        if ca_file is not None:
            entry["ca_file"] = ca_file
        self.profiles[name] = entry
        if not self.default or self.default not in self.profiles:
            self.data["default_profile"] = name

    def remove(self, name: str) -> None:
        if name not in self.profiles:
            raise CliError(f"no profile {name}", NOT_FOUND)
        del self.profiles[name]
        if self.default == name:
            self.data.pop("default_profile", None)
            if self.profiles:
                self.data["default_profile"] = sorted(self.profiles)[0]


@dataclass
class Target:
    """Where a command goes, and how it signs in."""
    profile: str
    url: str
    ca_file: Optional[str]
    env_token: Optional[str]            # CAIRN_TOKEN / CAIRN_TOKEN_FILE: used as is, never stored
    source: str                          # "profile" or "environment"

    @property
    def verify(self):
        return self.ca_file or os.environ.get("CAIRN_CA_FILE") or True


def selected_profile(flag: Optional[str], config: Config) -> str:
    """``--profile``, then ``CAIRN_PROFILE``, then the default profile, then ``default``."""
    return check_name(flag or os.environ.get("CAIRN_PROFILE", "").strip() or config.default or "default")


def env_token() -> Optional[str]:
    token = os.environ.get("CAIRN_TOKEN", "").strip()
    path = os.environ.get("CAIRN_TOKEN_FILE", "").strip()
    if not token and path:
        try:
            with open(path, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as e:
            raise CliError(f"CAIRN_TOKEN_FILE: {e.strerror}", AUTH)
    return token or None


def resolve(flag: Optional[str], config: Optional[Config] = None) -> Target:
    """The target of a command, with CAIRN_URL/CAIRN_TOKEN winning (CI) over the profile."""
    config = config or Config.load()
    name = selected_profile(flag, config)
    entry = config.profiles.get(name) or {}
    env_url = os.environ.get("CAIRN_URL", "").strip()
    token = env_token()
    if token:
        url = env_url or entry.get("url")
        if not url:
            raise CliError("CAIRN_TOKEN is set but CAIRN_URL is not (and no profile names a URL)", AUTH)
        return Target(name, normalize_url(url), entry.get("ca_file") if not env_url else None, token, "environment")
    if env_url and (not entry or normalize_url(env_url) != normalize_url(entry.get("url", ""))):
        # A profile's tokens only ever go to that profile's Cairn.
        raise CliError(f"CAIRN_URL is {env_url} but no token is set for it: set CAIRN_TOKEN (or CAIRN_TOKEN_FILE), or "
                       f"run `cairn login --url {env_url}`", AUTH)
    if not entry:
        if flag or os.environ.get("CAIRN_PROFILE"):
            raise CliError(f"no profile {name}: run `cairn login --profile {name} --url <your Cairn>`", AUTH)
        raise CliError("not signed in: run `cairn login --url <your Cairn>`", AUTH)
    return Target(name, normalize_url(entry["url"]), entry.get("ca_file"), None, "profile")
