"""Where a profile's tokens are kept: the operating system's keychain, through ``keyring`` (macOS Keychain, Windows
Credential Manager, the Secret Service on Linux).

Without a usable keychain (a server, a container, no D-Bus session) they go to ``credentials.json`` in the
configuration directory, readable by its owner only (0600, directory 0700), with a warning. A credentials file that
others can read, or that another user owns, is refused until its permissions are fixed. ``CAIRN_CREDENTIAL_STORE``
chooses: ``auto`` (default), ``keyring`` (fail without a keychain) or ``file``.

What is kept per profile (one entry, JSON): the Cairn URL the tokens belong to, the access token and its expiry, the
refresh token and its expiry, the signed-in device's id, and who signed in. Tokens are never printed.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional

from cairn_cli.errors import AUTH, CliError

SERVICE = "cairn-cli"


def _warn(message: str) -> None:
    print(f"cairn: warning: {message}", file=sys.stderr)


class FileStore:
    kind = "file"
    backend = "file"

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "credentials.json"

    def _check(self) -> None:
        if sys.platform == "win32":          # the user profile's ACLs protect it there
            return
        st = self.path.stat()
        if st.st_uid != os.getuid():
            raise CliError(f"{self.path} belongs to another user: refusing to use it", AUTH)
        if st.st_mode & 0o077:
            raise CliError(f"{self.path} can be read by others (mode {stat.S_IMODE(st.st_mode):04o}): refusing to use "
                           f"it. Run `chmod 600 {self.path}` (and sign in again if others may have read it)", AUTH)

    def _all(self) -> dict:
        if not self.path.exists():
            return {}
        self._check()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as e:
            raise CliError(f"{self.path}: cannot read it ({type(e).__name__}); remove it and sign in again", AUTH)
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if sys.platform != "win32":
            os.chmod(self.directory, 0o700)
        tmp = self.directory / f".credentials.json.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)

    def get(self, profile: str) -> Optional[dict]:
        return self._all().get(profile)

    def set(self, profile: str, value: dict) -> None:
        data = self._all()
        data[profile] = value
        self._write(data)

    def delete(self, profile: str) -> None:
        data = self._all()
        if data.pop(profile, None) is not None:
            self._write(data)


class KeyringStore:
    kind = "keyring"

    def __init__(self, backend):
        self.backend = backend

    @property
    def name(self) -> str:
        """The keyring backend in use (e.g. keyring.backends.macOS.Keyring); a chain names the backends it chains."""
        b = self.backend
        inner = getattr(b, "backends", None) if type(b).__name__ == "ChainerBackend" else None
        return ",".join(type(x).__module__ + "." + type(x).__name__ for x in (inner or [b]))

    def get(self, profile: str) -> Optional[dict]:
        raw = self.backend.get_password(SERVICE, profile)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def set(self, profile: str, value: dict) -> None:
        self.backend.set_password(SERVICE, profile, json.dumps(value))

    def delete(self, profile: str) -> None:
        from keyring.errors import PasswordDeleteError

        try:
            self.backend.delete_password(SERVICE, profile)
        except PasswordDeleteError:
            pass


def _usable_keyring():
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - a broken keyring configuration: no keychain
        return None
    if isinstance(backend, fail.Keyring):
        return None
    if type(backend).__name__ == "ChainerBackend" and not getattr(backend, "backends", None):
        return None
    if getattr(backend, "priority", 1) <= 0:
        return None
    return backend


def open_store(directory: Path, quiet: bool = False):
    """The keychain when there is one; else the 0600 file, with a warning (``CAIRN_CREDENTIAL_STORE`` chooses)."""
    choice = os.environ.get("CAIRN_CREDENTIAL_STORE", "auto").strip().lower() or "auto"
    if choice not in ("auto", "keyring", "file"):
        raise CliError("CAIRN_CREDENTIAL_STORE: auto, keyring or file")
    if choice == "file":
        return FileStore(directory)
    backend = _usable_keyring()
    if backend is not None:
        return _Guarded(KeyringStore(backend), directory, choice == "keyring", quiet)
    if choice == "keyring":
        raise CliError("no OS keychain is available (CAIRN_CREDENTIAL_STORE=keyring); install and unlock one, or "
                       "use CAIRN_CREDENTIAL_STORE=file", AUTH)
    if not quiet:
        _warn(f"no OS keychain available: tokens are kept in {directory / 'credentials.json'} (readable by you only)")
    return FileStore(directory)


class _Guarded:
    """The keychain, falling back to the file when it fails at use (a locked or absent Secret Service)."""

    def __init__(self, store: KeyringStore, directory: Path, strict: bool, quiet: bool):
        self.store, self.directory, self.strict, self.quiet = store, directory, strict, quiet
        self._fallback: Optional[FileStore] = None

    @property
    def kind(self) -> str:
        return self._fallback.kind if self._fallback else self.store.kind

    @property
    def backend(self) -> str:
        """Which keychain: the keyring backend's name, or "file" after falling back to credentials.json."""
        return self._fallback.backend if self._fallback else self.store.name

    def _call(self, name: str, *a):
        if self._fallback is not None:
            return getattr(self._fallback, name)(*a)
        try:
            return getattr(self.store, name)(*a)
        except CliError:
            raise
        except Exception as e:  # noqa: BLE001 - keyring.errors.KeyringError and D-Bus errors
            if self.strict:
                raise CliError(f"the OS keychain failed ({type(e).__name__}): unlock it, or use "
                               "CAIRN_CREDENTIAL_STORE=file", AUTH)
            if not self.quiet:
                _warn(f"the OS keychain failed ({type(e).__name__}): tokens are kept in "
                      f"{self.directory / 'credentials.json'} (readable by you only)")
            self._fallback = FileStore(self.directory)
            return getattr(self._fallback, name)(*a)

    def get(self, profile: str) -> Optional[dict]:
        found = self._call("get", profile)
        if found is None and self._fallback is None:       # signed in earlier without a keychain
            file = FileStore(self.directory)
            if file.path.exists():
                return file.get(profile)
        return found

    def set(self, profile: str, value: dict) -> None:
        self._call("set", profile, value)

    def delete(self, profile: str) -> None:
        self._call("delete", profile)
        if self._fallback is None:
            file = FileStore(self.directory)
            if file.path.exists():
                file.delete(profile)
