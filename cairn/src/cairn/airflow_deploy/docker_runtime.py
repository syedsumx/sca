"""Managed Airflow deployments on Docker: for development and single hosts (Kubernetes deployments use the official
chart, ``cairn.airflow_deploy.k8s_runtime``).

Cairn talks to the Docker Engine API on its socket (``CAIRN_AIRFLOW_DOCKER_SOCKET``) and makes, per deployment
``<name>``, on a network of its own (``cairn-af-<name>-net``):

* ``cairn-af-<name>-db``: the metadata database (PostgreSQL, ``CAIRN_AIRFLOW_DOCKER_DB_IMAGE``), its password a file
  derived from Cairn's key, reached only on the deployment's network;
* ``cairn-af-<name>-api-server`` (published on ``CAIRN_AIRFLOW_DOCKER_BIND``, a port of its own from
  ``CAIRN_AIRFLOW_DOCKER_PORTS``), ``-scheduler`` (LocalExecutor: tasks run beside it), ``-dag-processor`` and
  ``-triggerer``, from the image of the deployment's Airflow version (``CAIRN_AIRFLOW_DOCKER_IMAGES``) or the
  team image last deployed. Their settings, secrets included, are a file in each container (``/opt/cairn/airflow.env``,
  readable by Airflow's user only), never the container's environment or command line;
* the volumes ``-db``, ``-dags`` (DAG releases; ``current`` is a link swapped atomically by a DAG-only deploy, so
  nothing restarts) and ``-logs`` (task logs, read by the API server).

Every container reaches Cairn (and Trino) at the addresses the operator gives (``CAIRN_AIRFLOW_CAIRN_URL``);
``host.docker.internal`` is the host.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
from typing import Any, Optional

from cairn.airflow_deploy import bundle as bundles
from cairn.databases.docker_runtime import DockerRuntime as _Engine
from cairn.databases.docker_runtime import RuntimeError_, tar_of

logger = logging.getLogger(__name__)
LABEL = "cairn.io/airflow-deployment"
COMPONENT = "cairn.io/airflow-component"
COMPONENTS = ("api-server", "scheduler", "dag-processor", "triggerer")
DAGS = "/opt/airflow/dags"
AIRFLOW_UID = 50000
DEFAULT_IMAGES = {"3.3.2": "cairn/airflow:0.1.0"}
DB_IMAGE = "postgres:16-alpine"

INIT = """set -e
set -a; . /opt/cairn/airflow.env; set +a
airflow db migrate >/tmp/migrate.log 2>&1 || { tail -40 /tmp/migrate.log; exit 1; }
if [ "${CAIRN_SKIP_OPS_USER:-}" != "1" ]; then
  airflow users list -o json 2>/dev/null > /tmp/users.json || echo "[]" > /tmp/users.json
  if ! grep -q '"username": "cairn-ops"' /tmp/users.json; then
    airflow users create --username cairn-ops --firstname Cairn --lastname Operations --email cairn-ops@cairn.invalid \
      --role Op --use-random-password >/tmp/user.log 2>&1 || { tail -40 /tmp/user.log; exit 1; }
    airflow users list -o json 2>/tmp/user.log > /tmp/users.json || { tail -40 /tmp/user.log; exit 1; }
  fi
  python - <<'PY'
import json
t = open("/tmp/users.json").read()
u = [x for x in json.loads(t[t.index("["):]) if x["username"] == "cairn-ops"]
print("CAIRN_OPS_USER_ID=%s" % u[0]["id"])
PY
fi
"""

CHECK = """set -e
set -a; . /opt/cairn/airflow.env; set +a
export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=sqlite:////tmp/check.db AIRFLOW__CORE__LOAD_EXAMPLES=false
python - <<'EOF'
import json
try:
    from airflow.dag_processing.dagbag import DagBag
except ImportError:
    from airflow.models.dagbag import DagBag
import airflow
b = DagBag("/opt/cairn/check")
print("CAIRN_DAGBAG=" + json.dumps({"airflow": airflow.__version__, "dags": sorted(b.dag_ids),
                                     "import_errors": {k.replace("/opt/cairn/check/", ""): str(v)[-2000:]
                                                       for k, v in b.import_errors.items()}}))
EOF
"""


def names(dep: str) -> dict[str, str]:
    base = f"cairn-af-{dep}"
    out = {c: f"{base}-{c}" for c in COMPONENTS}
    out.update({"db": f"{base}-db", "net": f"{base}-net", "dags": f"{base}-dags", "logs": f"{base}-logs",
                "dbdata": f"{base}-db", "init": f"{base}-init", "check": f"{base}-check"})
    return out


def env_file(env: dict[str, str]) -> bytes:
    return ("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in sorted(env.items()))).encode()


class DockerRuntime:
    name = "docker"
    executors = ("LocalExecutor",)
    default_executor = "LocalExecutor"

    def __init__(self, socket: str = "/var/run/docker.sock", bind: str = "127.0.0.1", host: str = "127.0.0.1",
                 images: Optional[dict] = None, db_image: str = DB_IMAGE, ports: tuple[int, int] = (18080, 18999),
                 start_timeout: float = 300, extra_hosts: Optional[list[str]] = None):
        self.engine = _Engine(socket=socket)
        self.bind, self.host, self.db_image, self.ports = bind, host, db_image, ports
        self.images = {str(k): v for k, v in (images or DEFAULT_IMAGES).items()}
        self.start_timeout = start_timeout
        self.extra_hosts = extra_hosts if extra_hosts is not None else ["host.docker.internal:host-gateway"]

    # ---- the Engine API (the managed Postgres runtime's helpers) ---------------------------------------------
    def _call(self, *a, **kw):
        return self.engine._call(*a, **kw)

    def _inspect(self, c: str) -> Optional[dict]:
        return self.engine._inspect(c)

    def _remove(self, c: str) -> None:
        self.engine._remove(c)

    def exec(self, c: str, cmd: list[str], user: str = "airflow", timeout: float = 600) -> tuple[int, str]:
        return self.engine.exec(c, cmd, user=user, timeout=timeout)

    def _logs(self, c: str, tail: int = 200, limit: int = 200_000) -> str:
        import struct

        try:
            raw = self._call("GET", f"/containers/{c}/logs", params={"stdout": "true", "stderr": "true",
                                                                     "tail": str(tail)}, raw=True)
        except RuntimeError_:
            return ""
        out, i = [], 0
        while i + 8 <= len(raw):                       # the multiplexed stream: 8-byte headers
            size = struct.unpack(">I", raw[i + 4:i + 8])[0]
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        return b"".join(out).decode(errors="replace")[-limit:]

    def image_for(self, version: str) -> str:
        if str(version) not in self.images:
            raise RuntimeError_(f"Airflow {version} is not offered here (CAIRN_AIRFLOW_DOCKER_IMAGES: "
                                f"{', '.join(sorted(self.images))})", 400)
        return self.images[str(version)]

    def pull(self, image: str) -> None:
        self.engine._image(image)

    def _labels(self, dep: dict, component: str) -> dict:
        return {LABEL: dep["name"], COMPONENT: component}

    def _network(self, dep: dict) -> None:
        self._call("POST", "/networks/create", json={"Name": names(dep["name"])["net"], "Driver": "bridge",
                                                      "CheckDuplicate": True, "Labels": {LABEL: dep["name"]}},
                   ok=(200, 201, 409))

    def _volume(self, name: str, dep: dict) -> None:
        self._call("POST", "/volumes/create", json={"Name": name, "Labels": {LABEL: dep["name"]}})

    def _host_config(self, dep: dict, mounts: list[dict], res: Optional[dict] = None, **extra) -> dict:
        hc = {"Mounts": mounts, "NetworkMode": names(dep["name"])["net"], "RestartPolicy": {"Name": "unless-stopped"},
              "ExtraHosts": list(self.extra_hosts)}
        if res:
            hc["NanoCpus"] = int(float(res["cpu"]) * 1e9)
            hc["Memory"] = int(res["memory_mb"]) * 1024 * 1024
        hc.update(extra)
        return hc

    def _files(self, dep: dict) -> dict:
        """What each Airflow container gets as files: its settings, the CA, the sign-in config, its identity."""
        files = {"opt/cairn/airflow.env": (env_file(dep["env"]), 0o400, AIRFLOW_UID)}
        for path, (content, mode) in (dep.get("files") or {}).items():
            files[path.lstrip("/")] = (content if isinstance(content, bytes) else content.encode(), mode, AIRFLOW_UID)
        return files

    def _mounts(self, dep: dict, component: str) -> list[dict]:
        n = names(dep["name"])
        m = [{"Type": "volume", "Source": n["logs"], "Target": "/opt/airflow/logs"}]
        if component in ("scheduler", "dag-processor", "triggerer"):
            m.append({"Type": "volume", "Source": n["dags"], "Target": DAGS})
        return m

    def _run_once(self, dep: dict, cname: str, image: str, script: str, files: dict, mounts: list[dict],
                  timeout: float = 900) -> str:
        self._remove(cname)
        body = {"Image": image, "Cmd": ["bash", "-c", script], "Env": ["CONNECTION_CHECK_MAX_COUNT=0"],
                "Labels": self._labels(dep, "job"), "HostConfig": {**self._host_config(dep, mounts),
                                                                  "RestartPolicy": {"Name": "no"}}}
        self._call("POST", "/containers/create", params={"name": cname}, json=body)
        try:
            self.engine._put(cname, files)
            self._call("POST", f"/containers/{cname}/start")
            w = self._call("POST", f"/containers/{cname}/wait", timeout=timeout)
            logs = self._logs(cname, 200)
            if int(w.get("StatusCode", 1)) != 0:
                raise RuntimeError_(f"{cname} failed: {logs[-1500:]}")
            return logs
        finally:
            self._remove(cname)

    # ---- the metadata database -------------------------------------------------------------------------------
    def _db(self, dep: dict) -> None:
        n = names(dep["name"])
        if self._inspect(n["db"]) is None:
            self.pull(self.db_image)
            self._volume(n["dbdata"], dep)
            self._call("POST", "/containers/create", params={"name": n["db"]}, json={
                "Image": self.db_image, "Labels": self._labels(dep, "db"),
                "Env": ["POSTGRES_USER=airflow", "POSTGRES_DB=airflow", "POSTGRES_PASSWORD_FILE=/etc/cairn/db-password",
                        "PGDATA=/var/lib/postgresql/data/pgdata"],
                "HostConfig": self._host_config(dep, [{"Type": "volume", "Source": n["dbdata"],
                                                       "Target": "/var/lib/postgresql/data"}],
                                                {"cpu": 1, "memory_mb": 1024}, ExtraHosts=[])})
            self.engine._put(n["db"], {"etc/cairn/db-password": (dep["db_password"].encode(), 0o444, 0)})
        st = ((self._inspect(n["db"]) or {}).get("State") or {})
        if not st.get("Running"):
            self._call("POST", f"/containers/{n['db']}/start")
        end = time.time() + 120
        while time.time() < end:
            rc, _ = self.exec(n["db"], ["pg_isready", "-h", "127.0.0.1", "-U", "airflow", "-d", "airflow"],
                              user="postgres", timeout=30)
            if rc == 0:
                rc, _ = self.exec(n["db"], ["psql", "-h", "127.0.0.1", "-U", "airflow", "-d", "airflow", "-tAc",
                                            "SELECT 1"], user="postgres", timeout=30)
                if rc in (0, 2):         # 2: a password is asked over TCP: the server is the real one
                    return
            time.sleep(1)
        raise RuntimeError_(f"{n['db']} was not ready: {self._logs(n['db'])[-600:]}", 504)

    def db_url(self, dep: dict) -> str:
        from urllib.parse import quote

        return f"postgresql+psycopg2://airflow:{quote(dep['db_password'], safe='')}@{names(dep['name'])['db']}:5432/airflow"

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def _taken_ports(self) -> set[int]:
        out: set[int] = set()
        for c in self._call("GET", "/containers/json", params={"all": "true",
                                                               "filters": json.dumps({"label": [LABEL]})}):
            info = self._inspect(c["Id"]) or {}
            for b in ((info.get("HostConfig") or {}).get("PortBindings") or {}).get("8080/tcp") or []:
                if str(b.get("HostPort") or "").isdigit():
                    out.add(int(b["HostPort"]))
        return out

    def allocate_port(self) -> int:
        taken = self._taken_ports()
        for p in range(self.ports[0], self.ports[1] + 1):
            if p not in taken:
                return p
        raise RuntimeError_(f"no free port in {self.ports[0]}-{self.ports[1]} (CAIRN_AIRFLOW_DOCKER_PORTS)", 503)

    def public_url(self, dep: dict) -> str:
        return f"http://{self.host}:{dep['port']}"

    def api_url(self, dep: dict) -> str:
        return f"http://{self.host}:{dep['port']}"

    def internal_api_url(self, dep: dict) -> str:
        return f"http://{names(dep['name'])['api-server']}:8080"

    def init(self, dep: dict, image: str) -> Optional[int]:
        """``airflow db migrate`` (and Cairn's operations user) with ``image``: the user's id."""
        out = self._run_once(dep, names(dep["name"])["init"], image, INIT, self._files(dep), self._mounts(dep, "init"))
        m = re.search(r"CAIRN_OPS_USER_ID=(\d+)", out)
        return int(m.group(1)) if m else None

    def _create_component(self, dep: dict, component: str, image: str) -> None:
        n = names(dep["name"])
        cname = n[component]
        res = dep["spec"]["resources"][component.replace("-", "_")]
        cmd = "set -a; . /opt/cairn/airflow.env; set +a; exec airflow " + component
        body: dict[str, Any] = {"Image": image, "Cmd": ["bash", "-c", cmd], "Labels": self._labels(dep, component),
                                "Env": ["CONNECTION_CHECK_MAX_COUNT=0"], "StopTimeout": 30,
                                "HostConfig": self._host_config(dep, self._mounts(dep, component), res)}
        if component == "api-server":
            body["ExposedPorts"] = {"8080/tcp": {}}
            body["HostConfig"]["PortBindings"] = {"8080/tcp": [{"HostIp": self.bind, "HostPort": str(dep["port"])}]}
        self._remove(cname)
        self._call("POST", "/containers/create", params={"name": cname}, json=body)
        self.engine._put(cname, self._files(dep))

    def _prepare_dags(self, dep: dict, image: str) -> None:
        """The DAG volume's layout (releases/, current) before anything reads it."""
        n = names(dep["name"])
        self._run_once(dep, n["init"] + "-dags", image,
                       f"mkdir -p {DAGS}/releases/empty && [ -e {DAGS}/current ] || ln -sfn releases/empty {DAGS}/current",
                       {}, [{"Type": "volume", "Source": n["dags"], "Target": DAGS}])

    def create(self, dep: dict, image: str) -> dict:
        n = names(dep["name"])
        self.pull(image)
        self._network(dep)
        for v in (n["dags"], n["logs"]):
            self._volume(v, dep)
        try:
            self._db(dep)
            self._prepare_dags(dep, image)
            ops = self.init(dep, image)
            for c in COMPONENTS:
                self._create_component(dep, c, image)
                self._call("POST", f"/containers/{n[c]}/start")
        except Exception:
            self.delete(dep)
            raise
        return {"image": image, "ops_user": ops}

    def recreate(self, dep: dict, image: str, migrate: bool = False) -> dict:
        """The components again from ``image`` with the deployment's settings now (the database and DAGs stay)."""
        n = names(dep["name"])
        self.pull(image)
        self._db(dep)
        ops = None
        for c in COMPONENTS:
            self._call("POST", f"/containers/{n[c]}/stop", params={"t": "30"}, ok=(204, 304, 404))
        if migrate:
            ops = self.init(dep, image)
        for c in COMPONENTS:
            self._create_component(dep, c, image)
            self._call("POST", f"/containers/{n[c]}/start")
        return {"image": image, "ops_user": ops}

    def wait_ready(self, dep: dict, timeout: Optional[float] = None) -> dict:
        """Until Airflow says its scheduler, DAG processor and database are healthy."""
        import httpx

        end = time.time() + (timeout or self.start_timeout)
        last: Any = None
        while time.time() < end:
            for c in COMPONENTS:
                st = ((self._inspect(names(dep["name"])[c]) or {}).get("State") or {})
                if st.get("Status") in ("exited", "dead"):
                    raise RuntimeError_(f"{c} stopped: {self._logs(names(dep['name'])[c], 60)[-1500:]}")
            try:
                h = httpx.get(f"{self.api_url(dep)}/api/v2/monitor/health", timeout=5, trust_env=False).json()
                last = h
                if all((h.get(k) or {}).get("status") == "healthy" for k in ("metadatabase", "scheduler",
                                                                             "dag_processor", "triggerer")):
                    return h
            except (httpx.HTTPError, ValueError) as e:
                last = str(e)
            time.sleep(2)
        raise RuntimeError_(f"{dep['name']} was not healthy after {int(timeout or self.start_timeout)} s: {last}", 504)

    def stop(self, dep: dict) -> None:
        """Hibernation: every container stopped (nothing runs, the volumes stay)."""
        n = names(dep["name"])
        for c in (*COMPONENTS, "db"):
            self._call("POST", f"/containers/{n[c]}/stop", params={"t": "30"}, ok=(204, 304, 404))

    def start(self, dep: dict) -> None:
        n = names(dep["name"])
        self._db(dep)
        for c in COMPONENTS:
            self._call("POST", f"/containers/{n[c]}/start", ok=(204, 304))

    def put_files(self, dep: dict) -> None:
        """The deployment's settings and files, as of now, into every component (running or stopped)."""
        n = names(dep["name"])
        for c in COMPONENTS:
            if self._inspect(n[c]) is not None:
                self.engine._put(n[c], self._files(dep))

    def refresh_files(self, dep: dict, files: dict) -> None:
        """Puts files into the running components (the workload token, renewed)."""
        n = names(dep["name"])
        for c in COMPONENTS:
            st = ((self._inspect(n[c]) or {}).get("State") or {})
            if st.get("Running"):
                self.engine._put(n[c], {p.lstrip("/"): (v if isinstance(v, bytes) else v.encode(), mode, AIRFLOW_UID)
                                        for p, (v, mode) in files.items()})

    def delete(self, dep: dict) -> None:
        n = names(dep["name"])
        for c in (*COMPONENTS, "db", "init", "check"):
            self._remove(n[c])
        for v in (n["dags"], n["logs"], n["dbdata"]):
            self._call("DELETE", f"/volumes/{v}", params={"force": "true"}, ok=(204, 404, 409))
        self._call("DELETE", f"/networks/{n['net']}", ok=(204, 404, 409))

    # ---- DAGs ------------------------------------------------------------------------------------------------
    def sync_dags(self, dep: dict, release: str, data: bytes, keep: int = 5) -> None:
        """A DAG release into the running deployment, without restarting it: unpacked beside the others, then the
        ``current`` link swapped in one rename."""
        if not re.match(r"^[a-z0-9-]{1,64}$", release):
            raise RuntimeError_("a release id is lowercase letters, digits and '-'", 400)
        c = names(dep["name"])["dag-processor"]
        st = ((self._inspect(c) or {}).get("State") or {})
        if not st.get("Running"):
            raise RuntimeError_(f"{dep['name']} is not running", 409)
        self._call("PUT", f"/containers/{c}/archive", params={"path": DAGS},
                   content=bundles.relocate(data, f"releases/{release}", AIRFLOW_UID),
                   headers={"Content-Type": "application/x-tar"})
        swap = (f"cd {DAGS} && ln -sfn releases/{release} .cairn-next && mv -Tf .cairn-next current && "
                f"cur=$(readlink current) && ls -1t releases | grep -v '^empty$' | tail -n +{keep + 1} | "
                f"while read r; do [ \"releases/$r\" = \"$cur\" ] || rm -rf \"releases/$r\"; done")
        rc, out = self.exec(c, ["sh", "-c", swap], timeout=120)
        if rc != 0:
            raise RuntimeError_(f"the DAG release could not be switched: {out[-400:]}")

    def current_release(self, dep: dict) -> Optional[str]:
        c = names(dep["name"])["dag-processor"]
        rc, out = self.exec(c, ["readlink", f"{DAGS}/current"], timeout=30)
        return out.strip().removeprefix("releases/") if rc == 0 else None

    def check_dags(self, dep: dict, image: str, data: Optional[bytes]) -> dict:
        """Parses a DAG release on ``image`` (a one-shot container, no database): the DAGs found and import errors."""
        self.pull(image)
        files = self._files(dep)
        if data:
            files.update({k: v for k, v in _untar_files(bundles.relocate(data, "opt/cairn/check", AIRFLOW_UID)).items()})
        else:
            files["opt/cairn/check/.keep"] = (b"", 0o644, AIRFLOW_UID)
        out = self._run_once(dep, names(dep["name"])["check"], image, CHECK, files, [], timeout=600)
        m = re.search(r"CAIRN_DAGBAG=(\{.*\})", out)
        if not m:
            raise RuntimeError_(f"the DAG check gave no result: {out[-800:]}")
        return json.loads(m.group(1))

    def image_version(self, dep: dict, image: str) -> str:
        """The Airflow version an image runs."""
        self.pull(image)
        out = self._run_once(dep, names(dep["name"])["check"], image,
                             "python -c 'import airflow; print(\"CAIRN_AIRFLOW_VERSION=\" + airflow.__version__)'",
                             {}, [], timeout=300)
        m = re.search(r"CAIRN_AIRFLOW_VERSION=([0-9][0-9a-z.+-]*)", out)
        if not m:
            raise RuntimeError_(f"{image}: not an Airflow image ({out[-300:]})", 400)
        return m.group(1)

    # ---- state and logs ---------------------------------------------------------------------------------------
    def state(self, dep: dict) -> dict:
        n = names(dep["name"])
        comps = []
        for c in (*COMPONENTS, "db"):
            info = self._inspect(n[c]) or {}
            st = info.get("State") or {}
            comps.append({"component": c, "status": st.get("Status") or "missing", "running": bool(st.get("Running")),
                          "restarts": info.get("RestartCount", 0), "started_at": st.get("StartedAt"),
                          "image": (info.get("Config") or {}).get("Image")})
        return {"runtime": "docker", "components": comps, "running": sum(1 for c in comps if c["running"])}

    def logs(self, dep: dict, component: str, tail: int = 200) -> str:
        if component not in (*COMPONENTS, "db"):
            raise RuntimeError_(f"component is one of {', '.join((*COMPONENTS, 'db'))}", 400)
        return self._logs(names(dep["name"])[component], max(1, min(int(tail), 5000)))


def _untar_files(data: bytes) -> dict:
    import io
    import tarfile

    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as t:
        for m in t:
            if m.isfile():
                f = t.extractfile(m)
                out[m.name] = (f.read() if f else b"", 0o644, AIRFLOW_UID)
    return out


__all__ = ["DockerRuntime", "RuntimeError_", "names", "tar_of"]
