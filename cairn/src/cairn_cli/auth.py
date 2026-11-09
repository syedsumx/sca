"""``cairn login`` (the device flow, or ``--token-stdin``), ``cairn logout``, ``cairn whoami`` and ``cairn profiles``."""

from __future__ import annotations

import platform
import socket
import sys
import time
import webbrowser
from typing import Optional

import httpx

from cairn_cli.config import Config, check_name, config_dir, normalize_url, selected_profile
from cairn_cli.credentials import open_store
from cairn_cli.errors import AUTH, ERROR, NOT_FOUND, USAGE, CliError
from cairn_cli.output import Style, emit_json, err_style, table
from cairn_cli.session import CLIENT_ID, Session, detail, http_client, stored_from_tokens, token_request

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _say(a, text: str) -> None:
    if not getattr(a, "quiet", False):
        print(text, file=sys.stderr)


def discover(url: str, verify) -> str:
    """The API's base URL from what the person typed: the API itself, or the site it is served under (/cairn/api)."""
    url = normalize_url(url)
    errors = []
    with http_client(verify, 15) as c:
        for base in (url, url + "/cairn/api"):
            try:
                r = c.get(base + "/health")
            except httpx.HTTPError as e:
                errors.append(f"{type(e).__name__}: {e}")
                continue
            try:
                if r.status_code == 200 and (r.json() or {}).get("service") == "api":
                    return base
            except ValueError:
                pass
    if len(errors) == 2:
        raise CliError(f"cannot reach Cairn at {url}: {errors[0]}")
    raise CliError(f"{url} does not look like Cairn (no API at {url}/health or {url}/cairn/api/health)")


def _device_name(profile: str) -> str:
    """How the device appears on the approval page and in Signed-in devices."""
    host = socket.gethostname() or "computer"
    return f"cairn CLI on {host} ({platform.system() or 'unknown'}, profile {profile})"


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 - no browser here: the URL is printed anyway
        return False


def cmd_login(a) -> int:
    config = Config.load()
    name = check_name(a.profile or selected_profile(None, config))
    existing = config.profiles.get(name) or {}
    url = a.url or existing.get("url")
    if not url:
        raise CliError(f"which Cairn? `cairn login --url https://cairn.example.com` (profile {name} has no URL yet)", USAGE)
    ca_file = a.ca_file if a.ca_file is not None else existing.get("ca_file")
    verify = ca_file or True
    base = discover(url, verify)
    store = open_store(config_dir(), getattr(a, "quiet", False))
    if a.token_stdin:
        token = sys.stdin.readline().strip()
        if not token:
            raise CliError("--token-stdin: no token on standard input", USAGE)
        with http_client(verify) as c:
            r = c.get(base + "/me", headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise CliError(f"Cairn refused the token ({detail(r)})", AUTH)
        me = r.json()
        cred = {"url": base, "kind": "static", "access_token": token, "access_expires": None, "refresh_token": None,
                "actor": me.get("urn"), "saved": int(time.time())}
    else:
        cred, me = _device_flow(a, base, verify, name)
    try:
        previous = store.get(name)
    except CliError:
        previous = None
    config.put(name, base, ca_file)
    if a.default:
        config.set_default(name)
    config.save()
    store.set(name, cred)
    if previous and previous.get("kind") == "device" and previous.get("refresh_token") != cred.get("refresh_token"):
        _revoke(previous, existing.get("ca_file") or True)      # the profile's earlier sign-in: signed out in Cairn
    out = {"profile": name, "url": base, "user": me.get("urn"), "credential_store": store.kind}
    if getattr(a, "output", None) == "json":
        emit_json(out)
    else:
        _say(a, f"Signed in to {base} as {me.get('urn')} (profile {name}; tokens in the {store.kind}).")
    return 0


def _device_flow(a, base: str, verify, profile: str) -> tuple[dict, dict]:
    style = err_style(getattr(a, "no_color", False))
    with http_client(verify) as c:
        try:
            r = c.post(base + "/oauth/device_authorization", data={"client_id": CLIENT_ID, "device_name": _device_name(profile)})
        except httpx.HTTPError as e:
            raise CliError(f"cannot reach Cairn at {base}: {type(e).__name__}")
        if r.status_code != 200:
            raise CliError(f"Cairn did not start the sign-in: {detail(r)}", AUTH if r.status_code in (400, 401) else ERROR)
        d = r.json()
        complete = d.get("verification_uri_complete") or d["verification_uri"]
        print(f"To sign in, open {style.bold(d['verification_uri'])} and enter the code {style.bold(d['user_code'])}",
              file=sys.stderr)
        if not a.no_browser and _open_browser(complete):
            print(style.dim("(a browser window was opened for you; check that it shows the same code)"), file=sys.stderr)
        interval = max(1, int(d.get("interval") or 5))
        deadline = time.time() + int(d.get("expires_in") or 600)
        print(style.dim("Waiting for you to approve it… (Ctrl-C to stop)"), file=sys.stderr)
        while True:
            time.sleep(interval)
            status, doc = token_request(c, base, {"grant_type": DEVICE_GRANT, "device_code": d["device_code"],
                                                  "client_id": CLIENT_ID})
            if status == 200 and doc.get("access_token"):
                break
            err = doc.get("error")
            if err == "authorization_pending":
                if time.time() > deadline:
                    raise CliError("the code expired before it was approved: run `cairn login` again", AUTH)
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err == "expired_token":
                raise CliError("the code expired before it was approved: run `cairn login` again", AUTH)
            if err == "access_denied":
                raise CliError("the sign-in was refused on the verification page", AUTH)
            raise CliError(f"sign-in failed: {doc.get('error_description') or err or status}", AUTH)
        me_r = c.get(base + "/me", headers={"Authorization": f"Bearer {doc['access_token']}"})
        me = me_r.json() if me_r.status_code == 200 else {}
    return stored_from_tokens(base, doc, me.get("urn")), me


def cmd_logout(a) -> int:
    config = Config.load()
    name = selected_profile(a.profile, config)
    if name not in config.profiles:
        raise CliError(f"no profile {name}", NOT_FOUND)
    store = open_store(config_dir(), getattr(a, "quiet", False))
    cred = store.get(name)
    store.delete(name)                          # gone here first, whatever Cairn answers
    if not cred:
        _say(a, f"Profile {name} was not signed in.")
        return 0
    revoked, why = _revoke(cred, config.profiles[name].get("ca_file") or True)
    if revoked:
        _say(a, f"Signed out of {config.profiles[name]['url']} (profile {name}); this device's tokens no longer work.")
        return 0
    print(f"cairn: warning: removed the tokens here, but Cairn could not be told ({why}): sign this device out on "
          "your profile page (Signed-in devices)", file=sys.stderr)
    return ERROR


def _revoke(cred: dict, verify) -> tuple[bool, str]:
    token = cred.get("refresh_token") or cred.get("access_token") or ""
    if cred.get("kind") != "device":
        return True, ""                          # a personal access token is managed where it was made
    try:
        with http_client(verify, 20) as c:
            r = c.post(cred["url"] + "/oauth/revoke", data={"token": token, "client_id": CLIENT_ID})
        return r.status_code == 200, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, type(e).__name__


def _when(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    left = ts - time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ts))
    if left <= 0:
        return f"{stamp} (expired)"
    if left < 3600:
        return f"{stamp} (in {int(left // 60)} min)"
    if left < 86400 * 2:
        return f"{stamp} (in {left / 3600:.1f} h)"
    return f"{stamp} (in {int(left // 86400)} days)"


def cmd_whoami(a) -> int:
    s = Session.open(a.profile)
    try:
        me = s.call("GET", "/me")
        cred = s.credentials() or {}
    finally:
        s.close()
    out = {"user": me.get("urn"), "groups": me.get("groups") or [], "via": me.get("via"),
           "profile": s.target.profile if s.target.source == "profile" else None, "source": s.target.source,
           "url": s.url, "device": me.get("device"), "credential_store": s.store.kind if s.store else "environment",
           "access_token_expires": cred.get("access_expires"), "sign_in_expires": cred.get("refresh_expires")}
    if a.output == "json":
        emit_json(out)
        return 0
    st = Style(sys.stdout, a.no_color)
    rows = [("User", st.bold(out["user"] or "?")), ("Groups", ", ".join(out["groups"]) or "-"),
            ("Profile", out["profile"] or "(CAIRN_URL / CAIRN_TOKEN from the environment)"), ("Cairn", out["url"]),
            ("Signed in with", {"cli": "cairn login (device)", "token": "a personal access token",
                                "agent": "an agent credential", "service": "the service token"}.get(out["via"], out["via"]))]
    if out["source"] == "profile":
        rows += [("Access token expires", _when(out["access_token_expires"])),
                 ("Sign-in expires", _when(out["sign_in_expires"]) if out["sign_in_expires"] else "-"),
                 ("Tokens kept in", out["credential_store"])]
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"{k.ljust(width)}  {v}")
    return 0


def cmd_profiles(a) -> int:
    config = Config.load()
    if a.profiles_cmd == "list":
        store = open_store(config_dir(), True)
        items = []
        for name in sorted(config.profiles):
            try:
                cred = store.get(name) or {}
            except CliError:
                cred = {}
            items.append({"name": name, "url": config.profiles[name].get("url"), "default": name == config.default,
                          "signed_in": bool(cred.get("access_token")), "user": cred.get("actor"),
                          "sign_in_expires": cred.get("refresh_expires")})
        if a.output == "json":
            emit_json({"profiles": items, "default": config.default})
            return 0
        if not items:
            _say(a, "No profiles yet: `cairn login --url https://cairn.example.com`")
            return 0
        st = Style(sys.stdout, a.no_color)
        print(table(["", "PROFILE", "URL", "USER", "SIGNED IN"],
                    [["*" if i["default"] else "", i["name"], i["url"], i["user"] or "",
                      "yes" if i["signed_in"] else "no"] for i in items], st))
        return 0
    name = check_name(a.name)
    if a.profiles_cmd == "use":
        config.set_default(name)
        config.save()
        _say(a, f"Default profile: {name} ({config.profiles[name]['url']})")
        return 0
    if a.profiles_cmd == "remove":
        if name not in config.profiles:
            raise CliError(f"no profile {name}", NOT_FOUND)
        store = open_store(config_dir(), True)
        cred = store.get(name)
        store.delete(name)
        if cred:
            _revoke(cred, config.profiles[name].get("ca_file") or True)
        config.remove(name)
        config.save()
        _say(a, f"Removed profile {name}" + (f"; the default is now {config.default}" if config.default else ""))
        return 0
    raise CliError(f"unknown profiles command {a.profiles_cmd}", USAGE)
