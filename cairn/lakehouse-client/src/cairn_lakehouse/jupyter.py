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
"""

from __future__ import annotations

import json
import logging
import os

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
                        "--FileContentsManager.post_save_hook=cairn_lakehouse.jupyter.post_save_hook"]

    c.Spawner.pre_spawn_hook = pre_spawn
    # A start needs a current sign-in (the exchange checks it): an expired one sends the person to sign in again.
    c.Authenticator.refresh_pre_spawn = True
