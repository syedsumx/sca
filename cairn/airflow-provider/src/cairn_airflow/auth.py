"""Sign-in to Airflow's UI through Cairn (OAuth2 with FAB's auth manager).

Cairn is the authorization server (client ``airflow``): Airflow sends people to Cairn, Cairn knows them
from their Cairn session (or has them sign in), and returns them with a one-time code that Airflow
exchanges, with its client secret, for a token Cairn signed for audience ``airflow``. Airflow checks the
token's signature against Cairn's published key (no shared secret can mint one) and maps the person's
Cairn groups (the token's ``groups``) to Airflow roles.

Fails closed: a person in none of the mapped groups gets the ``Public`` role, which can see nothing, and
roles are synced at every sign-in, so removing someone from a Cairn group removes their Airflow access at
their next sign-in. Use from ``webserver_config.py``::

    from cairn_airflow.auth import configure
    configure(globals())

Environment:

    CAIRN_URL                    Cairn's API as Airflow reaches it (token exchange, keys)
    CAIRN_PUBLIC_URL             Cairn as people's browsers reach it (sign-in page), e.g. https://cairn.example.com
    CAIRN_AIRFLOW_OAUTH_SECRET   the client secret (the same value Cairn has)
    CAIRN_AIRFLOW_ROLES          Cairn group -> Airflow roles: data-platform=Admin;data-engineers=User (or JSON)
    CAIRN_AIRFLOW_TEAM_ROLES     true: each Cairn group a person is in also gives them the Airflow role
                                 team:<group> (no permissions of its own: DAGs grant it, with
                                 access_control={"team:finance": {"DAGs": {"can_read", "can_edit"}}}), and the
                                 role CairnMember (Viewer, without every DAG): people see the DAGs of their teams
    CAIRN_TOKEN_ISSUER           the tokens' issuer (cairn)
    CAIRN_CA_FILE                a CA for Cairn's certificate (default: the system's)
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import jwt

AUDIENCE = "airflow"


def _jwks_key(url: str, kid: str, verify) -> Any:
    keys = httpx.get(f"{url}/v1/trino/jwks.json", timeout=10, verify=verify).json().get("keys") or []
    for k in keys:
        if k.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k))
    raise jwt.InvalidTokenError("the token's key is not one Cairn publishes")


def user_info(access_token: str) -> dict:
    """The signed-in person from Cairn's token: checked signature, issuer, audience and expiry."""
    url = os.environ["CAIRN_URL"].rstrip("/")
    verify = os.environ.get("CAIRN_CA_FILE") or True
    kid = jwt.get_unverified_header(access_token).get("kid", "")
    claims = jwt.decode(access_token, _jwks_key(url, kid, verify), algorithms=["RS256"], audience=AUDIENCE,
                        issuer=os.environ.get("CAIRN_TOKEN_ISSUER", "cairn"), options={"require": ["exp", "sub"]})
    user = claims["sub"]
    return {"username": user, "email": user if "@" in user else f"{user}@cairn.invalid", "first_name": user,
            "last_name": "", "role_keys": list(claims.get("groups") or [])}


def parse_roles(raw: str) -> dict[str, list[str]]:
    """Cairn group -> Airflow roles: JSON (``{"data-engineers": ["User"]}``) or ``group=Role,Role;group2=Role``."""
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        return {str(g): [str(r) for r in (rs if isinstance(rs, list) else [rs])] for g, rs in json.loads(raw).items()}
    out: dict[str, list[str]] = {}
    for part in raw.split(";"):
        if part.strip():
            group, _, roles = part.partition("=")
            out[group.strip()] = [r.strip() for r in roles.split(",") if r.strip()]
    return out


TEAM_PREFIX = "team:"
MEMBER_ROLE = "CairnMember"
# What a member may not do globally: see every DAG. DAGs grant their teams access (access_control).
MEMBER_WITHHELD = {"DAGs"}


def _teams_enabled() -> bool:
    return os.environ.get("CAIRN_AIRFLOW_TEAM_ROLES", "").lower() in ("1", "true", "yes")


def _security_manager_class():
    from airflow.providers.fab.auth_manager.security_manager.override import FabAirflowSecurityManagerOverride

    class CairnSecurityManager(FabAirflowSecurityManagerOverride):
        def get_oauth_user_info(self, provider: str, resp: dict) -> dict:
            if provider != "cairn":
                return {}
            return user_info(resp["access_token"])

        def _member_role(self):
            role = self.find_role(MEMBER_ROLE)
            if role is None:
                role = self.add_role(MEMBER_ROLE)
                viewer = self.find_role("Viewer")
                for perm in (viewer.permissions if viewer else []):
                    resource = perm.resource.name
                    if resource in MEMBER_WITHHELD or resource.startswith("DAG:"):
                        continue
                    self.add_permission_to_role(role, perm)
            return role

        def _team_role(self, group: str):
            name = f"{TEAM_PREFIX}{group}"[:64]
            return self.find_role(name) or self.add_role(name)

        def sync_perm_for_dag(self, dag_id, access_control=None):
            # A DAG may name its team before anyone in it has signed in: create the team's role.
            if _teams_enabled():
                for role in (access_control or {}):
                    if str(role).startswith(TEAM_PREFIX):
                        self._team_role(str(role)[len(TEAM_PREFIX):])
            return super().sync_perm_for_dag(dag_id, access_control)

        def _oauth_calculate_user_roles(self, userinfo) -> list:
            roles = list(super()._oauth_calculate_user_roles(userinfo))
            groups = [g for g in userinfo.get("role_keys") or [] if g]
            if _teams_enabled() and groups:
                roles += [self._member_role(), *(self._team_role(g) for g in groups)]
            seen, out = set(), []
            for r in roles:
                if r is not None and r.name not in seen:
                    seen.add(r.name)
                    out.append(r)
            return out

    return CairnSecurityManager


def configure(conf: dict) -> None:
    """Sets FAB's settings in ``webserver_config.py``'s globals for sign-in through Cairn."""
    from flask_appbuilder.const import AUTH_OAUTH

    url = os.environ["CAIRN_URL"].rstrip("/")
    public = os.environ.get("CAIRN_PUBLIC_URL", "").rstrip("/")
    if not public.startswith("https://") and os.environ.get("CAIRN_AIRFLOW_ALLOW_HTTP") != "true":
        raise RuntimeError("CAIRN_PUBLIC_URL must be Cairn's https:// address (sign-in travels over TLS only)")
    secret = os.environ.get("CAIRN_AIRFLOW_OAUTH_SECRET", "")
    if len(secret) < 24:
        raise RuntimeError("CAIRN_AIRFLOW_OAUTH_SECRET: the client secret Cairn has for Airflow (24+ characters)")
    roles = parse_roles(os.environ.get("CAIRN_AIRFLOW_ROLES", ""))
    conf.update(
        AUTH_TYPE=AUTH_OAUTH,
        AUTH_USER_REGISTRATION=True,
        AUTH_USER_REGISTRATION_ROLE="Public",  # in no mapped group: no access
        AUTH_ROLES_MAPPING={g: list(r) for g, r in roles.items()},
        AUTH_ROLES_SYNC_AT_LOGIN=True,
        SECURITY_MANAGER_CLASS=_security_manager_class(),
        OAUTH_PROVIDERS=[{
            "name": "cairn", "icon": "fa-sign-in", "token_key": "access_token",
            "remote_app": {
                "client_id": "airflow", "client_secret": secret, "api_base_url": url,
                "access_token_url": f"{url}/v1/trino/oauth/token",
                "authorize_url": f"{public}/cairn/api/v1/trino/oauth/authorize",
                "client_kwargs": {"scope": AUDIENCE, "code_challenge_method": "S256",
                                  "verify": os.environ.get("CAIRN_CA_FILE") or True},
                "access_token_params": {"client_id": "airflow"},
            },
        }],
    )

