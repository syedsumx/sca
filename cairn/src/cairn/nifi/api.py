"""NiFi's REST API, as Cairn's single NiFi user: deploying a flow definition into its own process group, setting its
sensitive parameters, starting and stopping it, its status and bulletins, and removing it.

Cairn reaches NiFi over HTTPS with the deployment's CA pinned (nothing else is trusted; the address Cairn uses is in
NiFi's allowed Host headers, ``nifi.web.proxy.host``) and signs in with the single user's password, which never leaves
Cairn; the token NiFi gives is kept in memory and renewed before it ends."""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import uuid
from typing import Any, Optional

import httpx

from cairn.nifi.keys import USER

logger = logging.getLogger(__name__)
TOKEN_REUSE_S = 3600


class NiFiError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class NiFi:
    def __init__(self, base: str, password: str, ca_pem: bytes, timeout: float = 60):
        self.base = base.rstrip("/")
        self._password = password
        self.timeout = timeout
        self._ca = tempfile.NamedTemporaryFile(prefix="cairn-nifi-ca-", suffix=".pem", delete=False)
        self._ca.write(ca_pem)
        self._ca.flush()
        self._token: Optional[str] = None
        self._token_at = 0.0
        self._lock = threading.Lock()
        self.client_id = f"cairn-{uuid.uuid4().hex[:12]}"

    @property
    def verify(self) -> str:
        return self._ca.name

    def _http(self) -> httpx.Client:
        return httpx.Client(base_url=self.base, verify=self._ca.name, timeout=self.timeout, trust_env=False)

    def token(self, fresh: bool = False) -> str:
        with self._lock:
            if self._token and not fresh and time.time() - self._token_at < TOKEN_REUSE_S:
                return self._token
            try:
                with self._http() as c:
                    r = c.post("/nifi-api/access/token", data={"username": USER, "password": self._password})
            except httpx.HTTPError as e:
                raise NiFiError(f"NiFi does not answer: {type(e).__name__}", 503) from e
            if r.status_code not in (200, 201):
                raise NiFiError(f"NiFi refused Cairn's sign-in: {r.status_code}", 502)
            self._token, self._token_at = r.text.strip(), time.time()
            return self._token

    def call(self, method: str, path: str, body: Any = None, params: Optional[dict] = None, ok=(200, 201, 202),
             files: Optional[dict] = None, data: Optional[dict] = None, raw: bool = False) -> Any:
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self.token(fresh=attempt == 1)}"}
            try:
                with self._http() as c:
                    r = c.request(method, "/nifi-api" + path, json=body, params=params, files=files, data=data,
                                  headers=headers)
            except httpx.HTTPError as e:
                raise NiFiError(f"NiFi does not answer: {type(e).__name__}", 503) from e
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code not in ok:
                raise NiFiError(f"NiFi: {method} {path}: {r.status_code} {r.text[:400]}",
                                404 if r.status_code == 404 else 409 if r.status_code == 409 else 502)
            if raw:
                return r.content
            return r.json() if r.content and "json" in r.headers.get("content-type", "") else {}
        raise NiFiError("NiFi refused Cairn's token", 502)

    # ---- the canvas ------------------------------------------------------------------------------------------------
    def root(self) -> str:
        return self.call("GET", "/flow/process-groups/root")["processGroupFlow"]["id"]

    def about(self) -> dict:
        return self.call("GET", "/flow/about").get("about") or {}

    def upload(self, definition: dict, name: str, x: float = 0, y: float = 0) -> dict:
        """The definition as a new process group under the root: its entity."""
        files = {"file": (f"{name}.json", json.dumps(definition).encode(), "application/json")}
        data = {"groupName": name, "positionX": str(x), "positionY": str(y), "clientId": self.client_id,
                "disconnectedNodeAcknowledged": "false"}
        return self.call("POST", f"/process-groups/{self.root()}/process-groups/upload", files=files, data=data)

    def group(self, gid: str) -> dict:
        return self.call("GET", f"/process-groups/{gid}")

    def find_group(self, name: str) -> Optional[dict]:
        got = self.call("GET", f"/process-groups/{self.root()}/process-groups")
        for g in got.get("processGroups") or []:
            if (g.get("component") or {}).get("name") == name:
                return g
        return None

    def download(self, gid: str) -> dict:
        """The group's flow definition as NiFi exports it (sensitive values are never in it)."""
        return json.loads(self.call("GET", f"/process-groups/{gid}/download", raw=True) or b"{}")

    def set_parameters(self, context_id: str, values: dict[str, str], sensitive: bool = True,
                       timeout: float = 300) -> None:
        """Sets parameters of a context (NiFi stops and restarts what references them) and waits for it."""
        if not values:
            return
        ctx = self.call("GET", f"/parameter-contexts/{context_id}")
        body = {"revision": ctx["revision"], "id": context_id, "component": {
            "id": context_id, "parameters": [{"parameter": {"name": k, "sensitive": sensitive, "value": v}}
                                             for k, v in sorted(values.items())]}}
        req = self.call("POST", f"/parameter-contexts/{context_id}/update-requests", body)
        rid = req["request"]["requestId"]
        end = time.time() + timeout
        try:
            while True:
                st = self.call("GET", f"/parameter-contexts/{context_id}/update-requests/{rid}")["request"]
                if st.get("complete"):
                    if st.get("failureReason"):
                        raise NiFiError(f"NiFi did not take the parameters: {st['failureReason'][:300]}", 409)
                    return
                if time.time() > end:
                    raise NiFiError("NiFi did not take the parameters in time", 504)
                time.sleep(1)
        finally:
            self.call("DELETE", f"/parameter-contexts/{context_id}/update-requests/{rid}", ok=(200, 404),
                      params={"clientId": self.client_id})

    def services(self, gid: str, state: str) -> None:
        """Enables (``ENABLED``) or disables (``DISABLED``) every controller service of the group, and waits."""
        self.call("PUT", f"/flow/process-groups/{gid}/controller-services",
                  {"id": gid, "state": state, "disconnectedNodeAcknowledged": False})
        end = time.time() + 120
        while time.time() < end:
            got = self.call("GET", f"/flow/process-groups/{gid}/controller-services",
                            params={"includeAncestorGroups": "false", "includeDescendantGroups": "true"})
            states = [(s.get("component") or {}).get("state") for s in got.get("controllerServices") or []]
            if all(s == state for s in states):
                return
            if state == "ENABLED" and any(
                    (s.get("component") or {}).get("validationStatus") == "INVALID"
                    for s in got.get("controllerServices") or []):
                bad = [f"{(s.get('component') or {}).get('name')}: {'; '.join((s.get('component') or {}).get('validationErrors') or [])}"
                       for s in got.get("controllerServices") or []
                       if (s.get("component") or {}).get("validationStatus") == "INVALID"]
                raise NiFiError(f"controller services are not valid: {' | '.join(bad)[:800]}", 409)
            time.sleep(1)
        raise NiFiError(f"NiFi's controller services did not become {state.lower()} in time", 504)

    def schedule(self, gid: str, state: str) -> None:
        """Starts (``RUNNING``) or stops (``STOPPED``) every component of the group."""
        self.call("PUT", f"/flow/process-groups/{gid}", {"id": gid, "state": state,
                                                         "disconnectedNodeAcknowledged": False})

    def invalid(self, gid: str) -> list[str]:
        """Processors that are not valid, with why."""
        got = self.call("GET", f"/process-groups/{gid}/processors", params={"includeDescendantGroups": "true"})
        out = []
        for p in got.get("processors") or []:
            c = p.get("component") or {}
            if c.get("validationStatus") == "INVALID":
                out.append(f"{c.get('name')}: {'; '.join(c.get('validationErrors') or [])}")
        return out

    def drop_queues(self, gid: str) -> None:
        """Empties every queue of the group (a group with queued FlowFiles cannot be removed)."""
        got = self.call("GET", f"/process-groups/{gid}/connections")
        for conn in got.get("connections") or []:
            req = self.call("POST", f"/flowfile-queues/{conn['id']}/drop-requests")
            rid = (req.get("dropRequest") or {}).get("id")
            end = time.time() + 60
            while rid and time.time() < end:
                st = self.call("GET", f"/flowfile-queues/{conn['id']}/drop-requests/{rid}").get("dropRequest") or {}
                if st.get("finished"):
                    break
                time.sleep(0.5)
            if rid:
                self.call("DELETE", f"/flowfile-queues/{conn['id']}/drop-requests/{rid}", ok=(200, 404))
        for g in self.call("GET", f"/process-groups/{gid}/process-groups").get("processGroups") or []:
            self.drop_queues(g["id"])

    def remove(self, gid: str) -> None:
        """Stops the group, disables its services, empties its queues and removes it, with its parameter context."""
        try:
            entity = self.group(gid)
        except NiFiError as e:
            if e.status == 404:
                return
            raise
        ctx = ((entity.get("component") or {}).get("parameterContext") or {}).get("id")
        self.schedule(gid, "STOPPED")
        self.services(gid, "DISABLED")
        end = time.time() + 60
        while time.time() < end:                      # threads still running in stopped processors
            st = self.status(gid)
            if not st.get("activeThreadCount"):
                break
            time.sleep(1)
        self.drop_queues(gid)
        entity = self.group(gid)
        self.call("DELETE", f"/process-groups/{gid}", params={"version": entity["revision"]["version"],
                                                              "clientId": self.client_id}, ok=(200, 404))
        if ctx:
            try:
                c = self.call("GET", f"/parameter-contexts/{ctx}")
                self.call("DELETE", f"/parameter-contexts/{ctx}", params={"version": c["revision"]["version"],
                                                                          "clientId": self.client_id}, ok=(200, 404))
            except NiFiError as e:
                if e.status != 404:
                    raise

    # ---- what it does ----------------------------------------------------------------------------------------------
    def status(self, gid: str) -> dict:
        """The group's aggregate status (NiFi's five-minute counters), recursively."""
        got = self.call("GET", f"/flow/process-groups/{gid}/status", params={"recursive": "true"})
        return ((got.get("processGroupStatus") or {}).get("aggregateSnapshot")) or {}

    def bulletins(self, after: Optional[int] = None, limit: int = 500) -> list[dict]:
        params: dict[str, Any] = {"limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        got = self.call("GET", "/flow/bulletin-board", params=params)
        return (got.get("bulletinBoard") or {}).get("bulletins") or []
