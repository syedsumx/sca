"""Access provisioning: an approved access request grants real access, and ending it takes it away.

A data product's request policy lists ``provision`` steps, each naming a configured **target** and
what to grant::

    provision:
      - {target: snowflake_prod, role: ANALYTICS_ORDERS_READ}
      - {target: okta, group: 00g1abcd2EFGH3ijk4x7}

Targets are reviewed YAML files in ``config/provisioning`` (or ``CAIRN_PROVISIONING``)::

    targets:
      - id: snowflake_prod
        type: snowflake          # snowflake | postgres | redshift | databricks | bigquery | okta | entra
        connection: warehouse    # a connection in config/connections, with a role allowed to grant
        user: "{username_upper}" # how a person is named there: {username} {username_upper} {email}
      - id: okta
        type: okta
        url: https://acme.okta.com
        token: ${OKTA_API_TOKEN}

What each type does, and undoes when access ends (expiry, revocation):

* **snowflake** ``GRANT ROLE r TO USER u``; **postgres** / **redshift** ``GRANT "r" TO "u"``;
* **databricks** ``GRANT <privilege> ON <securable> TO `u``` (``grant: {privilege, on}``);
* **bigquery** ``GRANT `roles/...` ON SCHEMA p.d TO "user:u"`` (``grant: {role, on}``);
* **okta** adds the person to a group; **entra** (Microsoft Entra ID) adds them to a group.

Every step's outcome is kept on the request (``provisioning``) and in the audit log. A failed step
does not undo the approval: approvers are told, and the notifier retries it a few times.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Literal, Optional

import httpx
import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)
IDENT = re.compile(r"^[A-Za-z0-9_$.@+\-/: ]{1,255}$")
MAX_ATTEMPTS = 5


class ProvisionError(ValueError):
    pass


class ProvisionStep(BaseModel):
    target: str
    role: Optional[str] = None
    group: Optional[str] = None
    grant: Optional[dict[str, str]] = Field(default=None, description="databricks/bigquery: privilege or role, on")

    @model_validator(mode="after")
    def _one(self) -> "ProvisionStep":
        if sum(x is not None for x in (self.role, self.group, self.grant)) != 1:
            raise ValueError("a provisioning step grants exactly one of role, group or grant")
        for v in [self.role, self.group, *(self.grant or {}).values()]:
            if v is not None and not IDENT.match(v):
                raise ValueError(f"{v!r} is not a plain name")
        return self

    def label(self) -> str:
        if self.role:
            return f"role {self.role}"
        if self.group:
            return f"group {self.group}"
        g = self.grant or {}
        return f"{g.get('privilege') or g.get('role')} on {g.get('on')}"


class Target(BaseModel):
    id: str
    type: Literal["snowflake", "postgres", "redshift", "databricks", "bigquery", "okta", "entra"]
    connection: Optional[str] = None
    user: str = "{username}"
    url: Optional[str] = None
    token: str = ""
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m.group(1), ""), v)
    return v


def load_targets(path: Optional[str] = None) -> dict[str, Target]:
    path = path or os.environ.get("CAIRN_PROVISIONING", "/etc/cairn/config/provisioning")
    if not path or not os.path.exists(path):
        return {}
    files = [path] if os.path.isfile(path) else sorted(
        os.path.join(path, f) for f in os.listdir(path) if f.endswith((".yaml", ".yml")) and ".example." not in f)
    out: dict[str, Target] = {}
    for f in files:
        with open(f) as fh:
            for raw in (yaml.safe_load(fh) or {}).get("targets") or []:
                t = Target.model_validate({k: _expand(v) for k, v in raw.items()})
                out[t.id] = t
    return out


def _q(name: str, quote: str = '"') -> str:
    if not IDENT.match(name):
        raise ProvisionError(f"{name!r} is not a plain name")
    return f"{quote}{name.replace(quote, quote * 2)}{quote}"


def statements(target: Target, step: ProvisionStep, user: str, grant: bool) -> list[str]:
    """The SQL a warehouse target runs to grant (or revoke) a step for a user."""
    t = target.type
    if t == "snowflake" and step.role:
        return [f"GRANT ROLE {_q(step.role)} TO USER {_q(user)}" if grant else
                f"REVOKE ROLE {_q(step.role)} FROM USER {_q(user)}"]
    if t in ("postgres", "redshift") and step.role:
        return [f"GRANT {_q(step.role)} TO {_q(user)}" if grant else f"REVOKE {_q(step.role)} FROM {_q(user)}"]
    if t == "databricks" and step.grant:
        priv, on = step.grant.get("privilege", "SELECT").upper(), step.grant.get("on", "")
        if not re.match(r"^(SELECT|USE CATALOG|USE SCHEMA|READ VOLUME|EXECUTE|BROWSE)$", priv):
            raise ProvisionError(f"privilege {priv} is not a read privilege")
        kind, _, name = on.partition(" ")
        if kind.upper() not in ("TABLE", "SCHEMA", "CATALOG", "VOLUME", "FUNCTION") or not name:
            raise ProvisionError("on is '<TABLE|SCHEMA|CATALOG|VOLUME|FUNCTION> <name>'")
        obj = ".".join(_q(p, "`") for p in name.split("."))
        return [f"GRANT {priv} ON {kind.upper()} {obj} TO {_q(user, '`')}" if grant else
                f"REVOKE {priv} ON {kind.upper()} {obj} FROM {_q(user, '`')}"]
    if t == "bigquery" and step.grant:
        role, on = step.grant.get("role", "roles/bigquery.dataViewer"), step.grant.get("on", "")
        if not role.startswith("roles/bigquery."):
            raise ProvisionError("BigQuery grants use roles/bigquery.* roles")
        obj = ".".join(_q(p, "`") for p in on.split("."))
        who = f'"user:{user}"'
        return [f"GRANT {_q(role, '`')} ON SCHEMA {obj} TO {who}" if grant else
                f"REVOKE {_q(role, '`')} ON SCHEMA {obj} FROM {who}"]
    raise ProvisionError(f"a {t} target cannot grant {step.label()}")


class Provisioner:
    def __init__(self, targets: dict[str, Target], connections=None, person=None, audit=None,
                 http: Optional[httpx.Client] = None):
        """``person(urn) -> {"username", "email"}``; ``connections``: the ConnectionRegistry."""
        self.targets = targets
        self.connections = connections
        self.person = person or (lambda urn: {"username": urn.split(":")[-1], "email": None})
        self.audit = audit
        self.http = http or httpx.Client(timeout=20)
        self._tokens: dict[str, tuple[float, str]] = {}

    def check(self, steps: list[ProvisionStep]) -> None:
        for s in steps:
            t = self.targets.get(s.target)
            if t is None:
                raise ProvisionError(f"no provisioning target {s.target!r}")
            if t.type in ("okta", "entra") and not s.group:
                raise ProvisionError(f"a {t.type} target grants groups")
            if t.type not in ("okta", "entra"):
                statements(t, s, "someone", True)

    def _user(self, target: Target, urn: str) -> str:
        p = self.person(urn) or {}
        username = p.get("username") or urn.split(":")[-1]
        email = p.get("email") or ""
        if "{email}" in target.user and not email:
            raise ProvisionError(f"{urn} has no email address in the catalog")
        return target.user.format(username=username, username_upper=username.upper(), email=email,
                                  email_lower=email.lower())

    # ---- doing it -----------------------------------------------------------------------------
    def apply(self, step: ProvisionStep, urn: str, grant: bool) -> str:
        target = self.targets.get(step.target)
        if target is None:
            raise ProvisionError(f"no provisioning target {step.target!r}")
        user = self._user(target, urn)
        if target.type == "okta":
            self._okta(target, step.group, user, grant)
        elif target.type == "entra":
            self._entra(target, step.group, user, grant)
        else:
            if self.connections is None or not target.connection:
                raise ProvisionError(f"target {target.id} has no connection")
            from sqlalchemy import text

            engine = self.connections.engine(target.connection)
            with engine.begin() as conn:
                for sql in statements(target, step, user, grant):
                    conn.execute(text(sql))
        return user

    def _okta(self, t: Target, group: str, login: str, grant: bool) -> None:
        base, headers = (t.url or "").rstrip("/"), {"Authorization": f"SSWS {t.token}", "Accept": "application/json"}
        r = self.http.get(f"{base}/api/v1/users/{login}", headers=headers)
        if r.status_code == 404:
            raise ProvisionError(f"Okta has no user {login}")
        r.raise_for_status()
        uid = r.json()["id"]
        r = (self.http.put if grant else self.http.delete)(f"{base}/api/v1/groups/{group}/users/{uid}", headers=headers)
        if r.status_code not in (200, 204) and not (r.status_code == 404 and not grant):
            raise ProvisionError(f"Okta said {r.status_code}: {r.text[:200]}")

    def _graph_token(self, t: Target) -> str:
        cached = self._tokens.get(t.id)
        if cached and cached[0] > time.time() + 60:
            return cached[1]
        r = self.http.post(f"https://login.microsoftonline.com/{t.tenant_id}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": t.client_id, "client_secret": t.client_secret,
            "scope": "https://graph.microsoft.com/.default"})
        if r.status_code != 200:
            raise ProvisionError(f"Entra ID refused the credentials: {r.text[:200]}")
        body = r.json()
        self._tokens[t.id] = (time.time() + int(body.get("expires_in", 3600)), body["access_token"])
        return body["access_token"]

    def _entra(self, t: Target, group: str, upn: str, grant: bool) -> None:
        base = (t.url or "https://graph.microsoft.com").rstrip("/") + "/v1.0"
        headers = {"Authorization": f"Bearer {self._graph_token(t)}"}
        r = self.http.get(f"{base}/users/{upn}", headers=headers, params={"$select": "id"})
        if r.status_code == 404:
            raise ProvisionError(f"Entra ID has no user {upn}")
        r.raise_for_status()
        uid = r.json()["id"]
        if grant:
            r = self.http.post(f"{base}/groups/{group}/members/$ref", headers=headers,
                               json={"@odata.id": f"{base}/directoryObjects/{uid}"})
            if r.status_code == 400 and "already exist" in r.text:
                return
        else:
            r = self.http.delete(f"{base}/groups/{group}/members/{uid}/$ref", headers=headers)
            if r.status_code == 404:
                return
        if r.status_code not in (200, 204):
            raise ProvisionError(f"Entra ID said {r.status_code}: {r.text[:200]}")

    # ---- over a request ---------------------------------------------------------------------------
    def run(self, steps: list[dict], urn: str, grant: bool, previous: Optional[list[dict]] = None,
            actor: str = "urn:li:corpuser:cairn-provisioning") -> list[dict]:
        """Do (or undo) every step; returns their outcomes. With ``previous``, only steps not already in
        the wanted state are tried (a retry), up to MAX_ATTEMPTS each."""
        before = dict(enumerate(previous or []))
        action, wanted = ("grant", "granted") if grant else ("revoke", "revoked")
        out = []
        for i, raw in enumerate(steps):
            step = ProvisionStep.model_validate(raw)
            prev = before.get(i) or {}
            if prev.get("status") == wanted:
                out.append(prev)
                continue
            if not grant and not prev.get("granted_once"):
                out.append({**prev, "target": step.target, "what": step.label(), "status": "skipped"})
                continue                                   # never granted: nothing to take away
            if prev.get("action") == action and int(prev.get("attempts") or 0) >= MAX_ATTEMPTS:
                out.append(prev)                           # given up: a person has to look
                continue
            rec = {"target": step.target, "what": step.label(), "action": action,
                   "attempts": int(prev.get("attempts") or 0) + 1 if prev.get("action") == action else 1,
                   "granted_once": bool(prev.get("granted_once")), "at": int(time.time() * 1000)}
            try:
                rec["user"] = self.apply(step, urn, grant)
                rec["status"] = wanted
                rec["granted_once"] = rec["granted_once"] or grant
            except Exception as e:  # noqa: BLE001 - recorded on the request, retried by the sweep
                rec["status"], rec["error"] = "failed", str(e)[:500]
                logger.warning("provisioning %s for %s failed: %s", step.label(), urn, e)
            if self.audit is not None:
                self.audit.record("marketplace", f"access.{rec['action']}_{'ok' if rec['status'] == wanted else 'failed'}",
                                  actor, urn, {k: rec.get(k) for k in ("target", "what", "user", "error")})
            out.append(rec)
        return out
