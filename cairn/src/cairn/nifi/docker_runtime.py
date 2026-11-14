"""Managed NiFi deployments on Docker: for development, single hosts and tests (Kubernetes deployments are a
StatefulSet Cairn renders, :mod:`cairn.nifi.k8s_runtime`).

Cairn talks to the Docker Engine API (the managed Postgres runtime's client, ``CAIRN_NIFI_DOCKER_SOCKET``) and makes,
per deployment ``<name>``, on a network of its own (``cairn-nifi-<name>-net``):

* ``cairn-nifi-<name>``: NiFi from the upstream image (``apache/nifi:<version>``, unmodified), as NiFi's own user
  (uid 1000), HTTPS only, published on ``CAIRN_NIFI_DOCKER_BIND`` (127.0.0.1 by default) at a port of its own from
  ``CAIRN_NIFI_DOCKER_PORTS``: only Cairn reaches it, and NiFi accepts only Cairn's single user, whose password only
  Cairn knows. Its keys are files in ``/opt/cairn`` readable by NiFi's user alone (:mod:`cairn.nifi.keys`), never
  the container's environment or command line;
* volumes for NiFi's ``conf`` (the flow), its repositories (FlowFiles, content, provenance, database) and ``state``.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from cairn.databases.docker_runtime import DockerRuntime as _Engine
from cairn.databases.docker_runtime import RuntimeError_
from cairn.nifi.keys import NIFI_HOME, NIFI_UID

LABEL = "cairn.io/nifi-deployment"
DEFAULT_IMAGES = {"2.12.0": "apache/nifi:2.12.0"}
VOLUMES = ("conf", "database_repository", "flowfile_repository", "content_repository", "provenance_repository", "state")


def names(dep: str) -> dict[str, str]:
    base = f"cairn-nifi-{dep}"
    out = {"nifi": base, "net": f"{base}-net"}
    out.update({v: f"{base}-{v.replace('_', '-')}" for v in VOLUMES})
    return out


def env(dep: dict, proxy_hosts: list[str], context_path: str) -> list[str]:
    """NiFi's settings that are not secret (the image's start script reads them)."""
    heap = int(dep["spec"]["resources"]["heap_mb"])
    return ["NIFI_WEB_HTTPS_PORT=8443", "NIFI_WEB_HTTPS_HOST=0.0.0.0",
            f"NIFI_WEB_PROXY_HOST={','.join(proxy_hosts)}", f"NIFI_WEB_PROXY_CONTEXT_PATH={context_path}",
            f"NIFI_JVM_HEAP_INIT={heap}m", f"NIFI_JVM_HEAP_MAX={heap}m"]


class DockerRuntime:
    name = "docker"

    def __init__(self, socket: str = "/var/run/docker.sock", bind: str = "127.0.0.1", host: str = "127.0.0.1",
                 images: Optional[dict] = None, ports: tuple[int, int] = (18443, 18999), start_timeout: float = 600,
                 extra_hosts: Optional[list[str]] = None):
        self.engine = _Engine(socket=socket)
        self.bind, self.host, self.ports, self.start_timeout = bind, host, ports, start_timeout
        self.images = {str(k): v for k, v in (images or DEFAULT_IMAGES).items()}
        self.extra_hosts = extra_hosts if extra_hosts is not None else ["host.docker.internal:host-gateway"]

    def _call(self, *a, **kw):
        return self.engine._call(*a, **kw)

    def _inspect(self, c: str) -> Optional[dict]:
        return self.engine._inspect(c)

    def image_for(self, version: str) -> str:
        if str(version) not in self.images:
            raise RuntimeError_(f"NiFi {version} is not offered here (CAIRN_NIFI_DOCKER_IMAGES: "
                                f"{', '.join(sorted(self.images))})", 400)
        return self.images[str(version)]

    # ---- addresses -------------------------------------------------------------------------------------------------
    def _taken_ports(self) -> set[int]:
        out: set[int] = set()
        for c in self._call("GET", "/containers/json", params={"all": "true",
                                                               "filters": json.dumps({"label": [LABEL]})}):
            info = self._inspect(c["Id"]) or {}
            for b in ((info.get("HostConfig") or {}).get("PortBindings") or {}).get("8443/tcp") or []:
                if str(b.get("HostPort") or "").isdigit():
                    out.add(int(b["HostPort"]))
        return out

    def allocate_port(self, exclude: Any = ()) -> int:
        """A free port: none a container of Cairn's binds, nor any in ``exclude`` (ports recorded for deployments
        whose container is gone or not made yet)."""
        taken = self._taken_ports() | {int(p) for p in exclude if p}
        for p in range(self.ports[0], self.ports[1] + 1):
            if p not in taken:
                return p
        raise RuntimeError_(f"no free port in {self.ports[0]}-{self.ports[1]} (CAIRN_NIFI_DOCKER_PORTS)", 503)

    def api_url(self, dep: dict) -> str:
        return f"https://{self.host}:{dep['port']}"

    def tls_hosts(self, dep: dict) -> list[str]:
        return [self.host, names(dep["name"])["nifi"]]

    def proxy_hosts(self, dep: dict) -> list[str]:
        return [f"{self.host}:{dep['port']}", f"localhost:{dep['port']}"]

    # ---- lifecycle -------------------------------------------------------------------------------------------------
    def _body(self, dep: dict, image: str) -> dict:
        n = names(dep["name"])
        res = dep["spec"]["resources"]
        mounts = [{"Type": "volume", "Source": n[v], "Target": f"{NIFI_HOME}/{v}"} for v in VOLUMES]
        return {"Image": image, "User": f"{NIFI_UID}:{NIFI_UID}", "Entrypoint": ["bash", "/opt/cairn/start.sh"],
                "Cmd": [], "WorkingDir": NIFI_HOME, "Env": env(dep, dep["proxy_hosts"], dep["context_path"]),
                "Labels": {LABEL: dep["name"]}, "ExposedPorts": {"8443/tcp": {}}, "StopTimeout": 60,
                "HostConfig": {"Mounts": mounts, "NetworkMode": n["net"], "RestartPolicy": {"Name": "unless-stopped"},
                               "ExtraHosts": list(self.extra_hosts), "NanoCpus": int(float(res["cpu"]) * 1e9),
                               "Memory": int(res["memory_mb"]) * 1024 * 1024,
                               "PortBindings": {"8443/tcp": [{"HostIp": self.bind, "HostPort": str(dep["port"])}]},
                               "SecurityOpt": ["no-new-privileges"], "CapDrop": ["ALL"]}}

    def _files(self, dep: dict) -> dict:
        return {p.lstrip("/"): (c, mode, NIFI_UID) for p, (c, mode) in dep["files"].items()}

    def create(self, dep: dict, image: str) -> dict:
        n = names(dep["name"])
        self.engine._image(image)
        self._call("POST", "/networks/create", json={"Name": n["net"], "Driver": "bridge", "CheckDuplicate": True,
                                                      "Labels": {LABEL: dep["name"]}}, ok=(200, 201, 409))
        for v in VOLUMES:
            self._call("POST", "/volumes/create", json={"Name": n[v], "Labels": {LABEL: dep["name"]}})
        try:
            self._start(dep, image)
        except Exception:
            self.delete(dep)
            raise
        return {"image": image}

    def _start(self, dep: dict, image: str) -> None:
        n = names(dep["name"])
        self.engine._remove(n["nifi"])
        self._call("POST", "/containers/create", params={"name": n["nifi"]}, json=self._body(dep, image))
        self.engine._put(n["nifi"], self._files(dep))
        self._call("POST", f"/containers/{n['nifi']}/start")

    def recreate(self, dep: dict, image: str) -> dict:
        """The container again with the deployment's settings now (scaling, an upgrade); the volumes stay."""
        self.engine._image(image)
        n = names(dep["name"])
        self._call("POST", f"/containers/{n['nifi']}/stop", params={"t": "60"}, ok=(204, 304, 404), timeout=120)
        self._start(dep, image)
        return {"image": image}

    def wait_ready(self, dep: dict, ready, timeout: Optional[float] = None) -> None:
        """Until ``ready()`` (Cairn signed in to NiFi's API) is true, or the container stopped."""
        end = time.time() + (timeout or self.start_timeout)
        last: Any = None
        while time.time() < end:
            st = ((self._inspect(names(dep["name"])["nifi"]) or {}).get("State") or {})
            if st.get("Status") in ("exited", "dead"):
                raise RuntimeError_(f"NiFi stopped: {self.logs(dep, 80)[-1500:]}")
            try:
                if ready():
                    return
            except Exception as e:  # noqa: BLE001 - not up yet
                last = e
            time.sleep(3)
        raise RuntimeError_(f"{dep['name']} was not ready after {int(timeout or self.start_timeout)} s: {last}", 504)

    def delete(self, dep: dict) -> None:
        n = names(dep["name"])
        self.engine._remove(n["nifi"])
        for v in VOLUMES:
            self._call("DELETE", f"/volumes/{n[v]}", params={"force": "true"}, ok=(204, 404, 409))
        self._call("DELETE", f"/networks/{n['net']}", ok=(204, 404, 409))

    # ---- state and logs --------------------------------------------------------------------------------------------
    def state(self, dep: dict) -> dict:
        info = self._inspect(names(dep["name"])["nifi"]) or {}
        st = info.get("State") or {}
        return {"runtime": "docker", "status": st.get("Status") or "missing", "running": bool(st.get("Running")),
                "restarts": info.get("RestartCount", 0), "started_at": st.get("StartedAt"),
                "image": (info.get("Config") or {}).get("Image")}

    def logs(self, dep: dict, tail: int = 200) -> str:
        import struct

        try:
            raw = self._call("GET", f"/containers/{names(dep['name'])['nifi']}/logs",
                             params={"stdout": "true", "stderr": "true", "tail": str(max(1, min(int(tail), 5000)))},
                             raw=True)
        except RuntimeError_:
            return ""
        out, i = [], 0
        while i + 8 <= len(raw):
            size = struct.unpack(">I", raw[i + 4:i + 8])[0]
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        return b"".join(out).decode(errors="replace")[-400_000:]
