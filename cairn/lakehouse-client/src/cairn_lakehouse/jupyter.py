"""Jupyter and JupyterHub on Cairn.

**JupyterHub** (``jupyterhub_config.py``)::

    from cairn_lakehouse.jupyter import configure_hub
    configure_hub(c, cairn_url="https://cairn.example.com/cairn/api", hub_url="https://notebooks.example.com")

People sign in through Cairn (Cairn's OAuth2 server, client ``jupyterhub``; its secret in
``CAIRN_JUPYTERHUB_OAUTH_SECRET``). When a person's server starts, the hub exchanges their sign-in token
for a Cairn token of their own (``POST /v1/notebooks/token``) and gives it to the server as
``CAIRN_TOKEN``: ``%%sql``, ``%cairn spark`` and MLflow then run as them. ``JUPYTERHUB_CRYPT_KEY`` must be
set (the sign-in token is kept encrypted in the hub's database).

**Jupyter server** (``jupyter_server_config.py``, set by ``configure_hub`` for spawned servers)::

    c.FileContentsManager.post_save_hook = "cairn_lakehouse.jupyter.post_save_hook"

Every saved notebook is sent to Cairn (``POST /v1/notebooks/publish``) and becomes a catalog asset owned by
the person, with lineage from the tables its ``%%sql`` cells read. A failure to publish never stops the
save; it is logged.

**Git folders** (the server extension ``cairn_lakehouse.jupyter``, enabled by ``configure_hub``): the
notebooks of the person's git folders are written under ``git/<folder>/`` when the server starts, and again
on ``POST /cairn/git/sync`` (or ``sync_git_notebooks()``). Saving one there sends it to Cairn like any
notebook, and Cairn keeps it for the folder's next push. A file edited here that Cairn has not seen (its save
did not reach Cairn) is never overwritten: it is reported as kept.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

import httpx

logger = logging.getLogger("cairn_lakehouse.jupyter")


def _verify():
    return os.environ.get("CAIRN_CA_FILE") or True


def post_save_hook(model=None, os_path=None, contents_manager=None, **kwargs):
    """Jupyter's post-save hook: the saved notebook to Cairn's catalog."""
    if not model or model.get("type") != "notebook" or not os_path:
        return
    url, token = os.environ.get("CAIRN_URL", "").rstrip("/"), os.environ.get("CAIRN_TOKEN", "")
    if not url or not token:
        logger.info("cairn: CAIRN_URL and CAIRN_TOKEN unset; %s not published", os_path)
        return
    try:
        with open(os_path) as f:
            nb = json.load(f)
        path = model.get("path") or os.path.basename(os_path)
        r = httpx.post(f"{url}/v1/notebooks/publish", json={"path": path, "notebook": nb},
                       headers={"Authorization": f"Bearer {token}"}, timeout=30, verify=_verify())
        if r.status_code >= 300:
            logger.warning("cairn: publishing %s failed: %s %s", path, r.status_code, r.text[:300])
    except Exception as e:  # noqa: BLE001 - the save itself succeeded
        logger.warning("cairn: publishing %s failed: %s", os_path, e)


SYNC_MANIFEST = os.path.join(".cairn", "git-sync.json")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _render(nb: dict) -> str:
    return json.dumps(nb, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _canonical(text: str) -> str:
    """What a notebook says, outputs and layout aside (as Cairn compares them)."""
    try:
        nb = json.loads(text)
        for c in nb.get("cells") or []:
            if isinstance(c, dict) and c.get("cell_type") == "code":
                c["outputs"], c["execution_count"] = [], None
        return _render(nb)
    except (ValueError, TypeError, AttributeError):
        return text


def sync_git_notebooks(root_dir: str, url: str | None = None, token: str | None = None) -> dict:
    """Writes the person's git-folder notebooks under ``root_dir/git/<folder>/``.

    A file is written when it is missing, or when it is as this sync last wrote it; a file changed here
    since (and not as Cairn has it) is kept. Returns the paths ``written``, ``kept`` and ``unchanged``."""
    url = (url or os.environ.get("CAIRN_URL", "")).rstrip("/")
    token = token or os.environ.get("CAIRN_TOKEN", "")
    if not url or not token:
        raise RuntimeError("CAIRN_URL and CAIRN_TOKEN are needed to sync git folders")
    root = os.path.realpath(root_dir)
    manifest_path = os.path.join(root, SYNC_MANIFEST)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    out: dict[str, list[str]] = {"written": [], "kept": [], "unchanged": []}
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=60, verify=_verify(), headers=headers) as client:
        r = client.get(f"{url}/v1/git-folders")
        r.raise_for_status()
        for folder in r.json():
            r = client.get(f"{url}/v1/git-folders/{folder['id']}/notebooks")
            r.raise_for_status()
            for item in r.json():
                path = item["path"]
                full = os.path.realpath(os.path.join(root, path))
                if not full.startswith(os.path.join(root, "git") + os.sep) or not full.endswith(".ipynb"):
                    logger.warning("cairn: skipping %r: not under git/", path)
                    continue
                text = _render(item["notebook"])
                if os.path.exists(full):
                    with open(full, encoding="utf-8") as f:
                        current = f.read()
                    if current == text or _canonical(current) == _canonical(text):
                        manifest[path] = _sha(current)            # the same notebook (outputs may differ)
                        out["unchanged"].append(path)
                        continue
                    if manifest.get(path) != _sha(current):
                        out["kept"].append(path)                  # changed here; Cairn has not seen it
                        continue
                os.makedirs(os.path.dirname(full), exist_ok=True)
                tmp = f"{full}.cairn-sync"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, full)
                manifest[path] = _sha(text)
                out["written"].append(path)
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    return out


# ---- the Jupyter server extension --------------------------------------------------------------------
def _jupyter_server_extension_points():
    return [{"module": "cairn_lakehouse.jupyter"}]


def _load_jupyter_server_extension(serverapp):
    """Syncs git-folder notebooks when the server starts, and on ``POST /cairn/git/sync``."""
    import asyncio

    from jupyter_server.auth.decorator import authorized
    from jupyter_server.base.handlers import APIHandler
    from jupyter_server.utils import url_path_join
    from tornado import web

    root = serverapp.root_dir

    class SyncHandler(APIHandler):
        auth_resource = "contents"

        @web.authenticated
        @authorized
        async def post(self):
            try:
                out = await asyncio.to_thread(sync_git_notebooks, root)
            except Exception as e:  # noqa: BLE001 - reported to the caller
                self.set_status(502)
                self.finish(json.dumps({"error": str(e)[:500]}))
                return
            self.finish(json.dumps(out))

    serverapp.web_app.add_handlers(".*$", [(url_path_join(serverapp.web_app.settings["base_url"], "cairn/git/sync"),
                                            SyncHandler)])

    def first_sync():
        try:
            out = sync_git_notebooks(root)
            serverapp.log.info("cairn: git folders synced: %s", {k: len(v) for k, v in out.items()})
        except Exception as e:  # noqa: BLE001 - the server works without it
            serverapp.log.warning("cairn: syncing git folders failed: %s", e)

    if os.environ.get("CAIRN_URL") and os.environ.get("CAIRN_TOKEN"):
        threading.Thread(target=first_sync, name="cairn-git-sync", daemon=True).start()


def configure_hub(c, cairn_url: str, hub_url: str, client_secret: str | None = None, browser_url: str | None = None):
    """Configures JupyterHub ``c`` for sign-in through Cairn and kernels that act as the person.

    ``cairn_url``: Cairn's API as the hub reaches it; ``browser_url``: Cairn's API as browsers reach it
    (default: the same); ``hub_url``: the hub's public address (its OAuth callback is under it)."""
    from oauthenticator.generic import GenericOAuthenticator

    api = cairn_url.rstrip("/")
    browser = (browser_url or cairn_url).rstrip("/")
    c.JupyterHub.authenticator_class = GenericOAuthenticator
    c.GenericOAuthenticator.client_id = "jupyterhub"
    c.GenericOAuthenticator.client_secret = client_secret or os.environ["CAIRN_JUPYTERHUB_OAUTH_SECRET"]
    c.GenericOAuthenticator.oauth_callback_url = f"{hub_url.rstrip('/')}/hub/oauth_callback"
    c.GenericOAuthenticator.authorize_url = f"{browser}/v1/trino/oauth/authorize"
    c.GenericOAuthenticator.token_url = f"{api}/v1/trino/oauth/token"
    c.GenericOAuthenticator.userdata_url = f"{api}/v1/notebooks/userinfo"
    c.GenericOAuthenticator.username_claim = "sub"
    c.GenericOAuthenticator.scope = ["jupyterhub"]
    c.GenericOAuthenticator.allow_all = True                # who may sign in is decided by Cairn
    c.GenericOAuthenticator.enable_auth_state = True
    c.GenericOAuthenticator.manage_groups = False

    async def pre_spawn(spawner):
        state = await spawner.user.get_auth_state() or {}
        hub_token = state.get("access_token")
        if not hub_token:
            raise RuntimeError("no Cairn sign-in for this person: sign in again")
        async with httpx.AsyncClient(timeout=30, verify=_verify()) as client:
            r = await client.post(f"{api}/v1/notebooks/token", headers={"Authorization": f"Bearer {hub_token}"})
        if r.status_code != 200:
            raise RuntimeError(f"Cairn refused a notebook token ({r.status_code}): sign in again")
        spawner.environment.update({"CAIRN_URL": api, "CAIRN_TOKEN": r.json()["token"]})
        spawner.args = [*spawner.args,
                        "--FileContentsManager.post_save_hook=cairn_lakehouse.jupyter.post_save_hook",
                        "--ServerApp.jpserver_extensions=cairn_lakehouse.jupyter=True"]

    c.Spawner.pre_spawn_hook = pre_spawn
    # A start needs a current sign-in (the exchange checks it): an expired one sends the person to sign in again.
    c.Authenticator.refresh_pre_spawn = True
