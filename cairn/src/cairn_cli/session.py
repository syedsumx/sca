"""Calls to Cairn as the signed-in person: the access token is refreshed before it expires (one process at a time,
under a lock, so two commands never spend the same refresh token), and Cairn's refusals become exit codes with
Cairn's own message (a 403 names the privilege and what it is on)."""

from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Any, Optional

import httpx

from cairn_cli import __version__
from cairn_cli.config import Config, Target, config_dir, resolve
from cairn_cli.credentials import open_store
from cairn_cli.errors import AUTH, ERROR, CliError, for_status

CLIENT_ID = "cairn-cli"
REFRESH_MARGIN_S = 60
USER_AGENT = f"cairn-cli/{__version__}"

_selected: dict[str, Any] = {"profile": None, "quiet": False}


def select(profile: Optional[str], quiet: bool = False) -> None:
    """The ``--profile`` and ``--quiet`` of this run (for commands that build their own clients, such as flows)."""
    _selected.update(profile=profile, quiet=quiet)


def detail(r: httpx.Response) -> str:
    try:
        doc = r.json()
    except ValueError:
        return (r.text or r.reason_phrase or "").strip()[:500]
    if isinstance(doc, dict):
        d = doc.get("detail") if "detail" in doc else doc.get("error_description") or doc.get("error")
        if isinstance(d, list):                # FastAPI's validation errors
            return "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg')}" for e in d)[:500]
        if d:
            return str(d)[:500]
    return (r.text or "")[:500]


@contextlib.contextmanager
def _locked(path):
    """An exclusive lock on ``path`` across processes (refreshing a token)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            for _ in range(600):
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise CliError("another cairn command holds the sign-in lock")
            try:
                yield
            finally:
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def http_client(verify, timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(verify=verify, timeout=httpx.Timeout(timeout, connect=15.0), headers={"User-Agent": USER_AGENT})


def token_request(client: httpx.Client, url: str, form: dict) -> tuple[int, dict]:
    try:
        r = client.post(f"{url}/oauth/token", data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise CliError(f"cannot reach Cairn at {url}: {type(e).__name__}")
    try:
        doc = r.json()
    except ValueError:
        doc = {"error": "server_error", "error_description": r.text[:300]}
    return r.status_code, doc if isinstance(doc, dict) else {}


def stored_from_tokens(url: str, doc: dict, actor: Optional[str] = None) -> dict:
    now = int(time.time())
    return {"url": url, "kind": "device", "access_token": doc["access_token"],
            "access_expires": now + int(doc.get("expires_in") or 3600),
            "refresh_token": doc.get("refresh_token"),
            "refresh_expires": now + int(doc["refresh_expires_in"]) if doc.get("refresh_expires_in") else None,
            "device": doc.get("session"), "actor": actor, "saved": now}


class Session:
    """A Cairn, signed in. ``Session.open(--profile)`` resolves the profile (or CAIRN_URL/CAIRN_TOKEN)."""

    def __init__(self, target: Target, store=None, quiet: bool = False):
        self.target = target
        self.quiet = quiet
        self.store = store
        self.http = http_client(target.verify)

    @classmethod
    def open(cls, profile: Optional[str] = None, quiet: Optional[bool] = None) -> "Session":
        quiet = _selected["quiet"] if quiet is None else quiet
        target = resolve(profile if profile is not None else _selected["profile"], Config.load())
        store = None if target.env_token else open_store(config_dir(), quiet)
        return cls(target, store, quiet)

    @property
    def url(self) -> str:
        return self.target.url

    # ---- the token -----------------------------------------------------------------------------------
    def credentials(self) -> Optional[dict]:
        if self.target.env_token:
            return {"kind": "environment"}
        return self.store.get(self.target.profile)

    def _signed_out(self, why: str) -> CliError:
        return CliError(f"{why}: run `cairn login --profile {self.target.profile}`", AUTH)

    def token(self, force_refresh: bool = False) -> str:
        if self.target.env_token:
            return self.target.env_token
        cred = self.store.get(self.target.profile)
        if not cred or not cred.get("access_token"):
            raise self._signed_out(f"not signed in to profile {self.target.profile}")
        if cred.get("url") and cred["url"] != self.target.url:
            # Tokens only go to the Cairn they came from, even if the profile's URL was edited since.
            raise self._signed_out(f"the stored sign-in is for {cred['url']}, not {self.target.url}")
        if cred.get("kind") != "device" or not cred.get("refresh_token"):     # a token given with --token-stdin
            if cred.get("access_expires") and cred["access_expires"] < time.time():
                raise self._signed_out("the stored token has expired")
            return cred["access_token"]
        if not force_refresh and (cred.get("access_expires") or 0) - REFRESH_MARGIN_S > time.time():
            return cred["access_token"]
        return self._refresh(cred["access_token"])

    def _refresh(self, seen: str) -> str:
        with _locked(config_dir() / f".{self.target.profile}.lock"):
            cred = self.store.get(self.target.profile) or {}
            if cred.get("access_token") and cred["access_token"] != seen and \
                    (cred.get("access_expires") or 0) - REFRESH_MARGIN_S > time.time():
                return cred["access_token"]               # another cairn command refreshed it meanwhile
            if not cred.get("refresh_token"):
                raise self._signed_out("not signed in")
            status, doc = token_request(self.http, self.url, {"grant_type": "refresh_token", "client_id": CLIENT_ID,
                                                              "refresh_token": cred["refresh_token"]})
            if status != 200 or not doc.get("access_token"):
                if doc.get("error") in ("invalid_grant", "invalid_client", "unauthorized_client"):
                    self.store.delete(self.target.profile)       # it can never work again
                    raise self._signed_out(f"your sign-in ended ({doc.get('error_description') or doc.get('error')})")
                raise CliError(f"could not refresh the sign-in: {doc.get('error_description') or status}")
            fresh = stored_from_tokens(self.url, doc, cred.get("actor"))
            self.store.set(self.target.profile, fresh)
            return fresh["access_token"]

    # ---- calls ---------------------------------------------------------------------------------------
    def request(self, method: str, path: str, *, json: Any = None, params: Any = None, timeout: Optional[float] = None,
                ok: tuple = ()) -> httpx.Response:
        """The response (2xx, or a status in ``ok``); otherwise CliError with Cairn's message and the exit code."""
        token = self.token()
        for attempt in (1, 2):
            try:
                r = self.http.request(method, self.url + path, json=json, params=params,
                                      headers={"Authorization": f"Bearer {token}"},
                                      **({"timeout": timeout} if timeout else {}))
            except httpx.TimeoutException:
                raise CliError(f"Cairn at {self.url} did not answer in time")
            except httpx.HTTPError as e:
                raise CliError(f"cannot reach Cairn at {self.url}: {type(e).__name__}: {e}")
            if r.status_code == 401 and attempt == 1 and not self.target.env_token:
                cred = self.store.get(self.target.profile) or {}
                if cred.get("kind") == "device" and cred.get("refresh_token"):
                    token = self.token(force_refresh=True)
                    continue
            break
        if r.status_code < 300 or r.status_code in ok:
            return r
        why = detail(r)
        if r.status_code == 401:
            if self.target.env_token:
                raise CliError(f"Cairn refused CAIRN_TOKEN ({why})", AUTH)
            raise self._signed_out(f"Cairn refused the sign-in ({why})")
        if r.status_code == 403:
            raise CliError(f"permission denied: {why}", for_status(403))
        if r.status_code == 429:
            raise CliError(f"too many requests: {why} (retry after {r.headers.get('Retry-After', 'a while')} s)", ERROR)
        raise CliError(why or f"{method} {path}: HTTP {r.status_code}", for_status(r.status_code))

    def call(self, method: str, path: str, body: Any = None, **kw) -> Any:
        r = self.request(method, path, json=body, **kw)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            raise CliError(f"{method} {path}: Cairn's answer was not JSON", ERROR)

    def close(self) -> None:
        self.http.close()


def api_credentials(profile: Optional[str] = None) -> tuple[str, str, Any]:
    """URL, a valid token and TLS verification for code that makes its own client (``cairn flows``, ``cairn iac``)."""
    s = Session.open(profile)
    try:
        return s.url, s.token(), s.target.verify
    finally:
        s.close()
