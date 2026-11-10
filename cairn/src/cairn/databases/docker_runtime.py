"""Managed PostgreSQL instances on Docker: one PostgreSQL container and a backup sidecar per instance.

For small and development deployments (Kubernetes deployments use CloudNativePG, ``cairn.databases.cnpg``). Cairn
talks to the Docker Engine API on its socket (``CAIRN_PG_DOCKER_SOCKET``) and makes, per instance ``<name>``:

* the volumes ``cairn-pg-<name>-data`` (the data directory) and ``cairn-pg-<name>-spool`` (base backups on their way
  to the store);
* the container ``cairn-pg-<name>`` from the image configured for the major version (``CAIRN_PG_DOCKER_IMAGES``),
  its port published on ``CAIRN_PG_DOCKER_BIND``, with continuous WAL archiving (``archive_command`` posts each WAL
  file to the sidecar and succeeds only once it is in the store), ``pg_stat_statements`` and TLS (a certificate Cairn
  makes for the instance). The superuser's password is copied in as a file, used once by initdb and then emptied;
* the container ``cairn-pg-<name>-agent`` (``CAIRN_PG_DOCKER_AGENT_IMAGE``, a plain Python image) running
  ``cairn.databases.backup_agent`` with the store's credentials as a file, the data volume mounted read-only (it
  reads the WAL files PostgreSQL asks it to archive);
* the network ``cairn-pg-<name>-net`` the two share (and nothing else): the sidecar is reached only from there.

Base backups are ``pg_basebackup`` (tar, compressed, with the WAL needed to make it consistent) run in the instance
container, uploaded by the sidecar. A restore downloads a base backup into a new instance's data directory (one-shot
containers), and PostgreSQL replays the archived WAL up to the target, fetched through the new instance's sidecar.
Docker instances have no standbys and no pooler: high availability needs the Kubernetes runtime.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import struct
import tarfile
import time
from importlib import resources
from typing import Any, Optional

logger = logging.getLogger(__name__)
AGENT_PORT = 8008
PGDATA = "/var/lib/postgresql/data/pgdata"
DEFAULT_IMAGES = {"16": "postgres:16-alpine"}
AGENT_IMAGE = "python:3.12-slim"



def archive_sh(agent: str) -> str:
    return f"""#!/bin/sh
# cairn-archive <path> <file>: the sidecar reads the file from pg_wal; this succeeds only once it is in the store.
exec wget -Y off -q -T 600 -O /dev/null "http://{agent}:{AGENT_PORT}/archive/$2"
"""


def restore_sh(agent: str) -> str:
    return f"""#!/bin/sh
# cairn-restore <file> <path>: fetches one archived WAL file through the sidecar. Not there: exit 1 (recovery ends
# there). The store failing: exit 255, which stops recovery rather than ending it early.
i=0
until wget -Y off -q -T 5 -O /dev/null "http://{agent}:{AGENT_PORT}/ready" 2>/dev/null; do
  i=$((i+1)); [ "$i" -ge 120 ] && exit 255; sleep 1
done
out=$(wget -Y off -S -q -T 600 -O "$2.cairn" "http://{agent}:{AGENT_PORT}/wal/$1" 2>&1)
if [ $? -eq 0 ]; then mv "$2.cairn" "$2"; exit 0; fi
rm -f "$2.cairn"
case "$out" in *" 404 "*) exit 1;; esac
echo "cairn-restore: $1: $out" >&2
exit 255
"""


class RuntimeError_(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def names(instance: str) -> dict[str, str]:
    return {"pg": f"cairn-pg-{instance}", "agent": f"cairn-pg-{instance}-agent",
            "data": f"cairn-pg-{instance}-data", "spool": f"cairn-pg-{instance}-spool", "net": f"cairn-pg-{instance}-net"}


def tar_of(files: dict[str, tuple[bytes, int, int]], make_dirs: bool = True) -> bytes:
    """A tar of ``{path: (content, mode, uid)}``; directories on the way are made 0755 (``make_dirs``; otherwise
    they must exist, and keep their owner and mode)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        dirs = set()
        for path in files if make_dirs else ():
            parts = path.strip("/").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        for d in sorted(dirs):
            info = tarfile.TarInfo(d)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, int(time.time())
            t.addfile(info)
        for path, (data, mode, uid) in files.items():
            info = tarfile.TarInfo(path.strip("/"))
            info.size, info.mode, info.uid, info.gid, info.mtime = len(data), mode, uid, uid, int(time.time())
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def self_signed(cn: str) -> tuple[bytes, bytes]:
    """A certificate and key for an instance's TLS (clients use sslmode=require; verify-full needs the operator's own)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=3650)).sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def agent_source() -> bytes:
    return resources.files("cairn.databases").joinpath("backup_agent.py").read_bytes()


class DockerRuntime:
    name = "docker"
    supports_replicas = False
    supports_pooler = False

    def __init__(self, socket: str = "/var/run/docker.sock", bind: str = "127.0.0.1", host: str = "127.0.0.1",
                 images: Optional[dict] = None, agent_image: str = AGENT_IMAGE, network: str = "",
                 store: Optional[dict] = None, start_timeout: float = 120, restore_timeout: float = 900,
                 ports: tuple[int, int] = (15432, 15999)):
        self.socket, self.bind, self.host, self.network = socket, bind, host, network
        self.ports = ports
        self.images = {str(k): v for k, v in (images or DEFAULT_IMAGES).items()}
        self.agent_image, self.store = agent_image, store
        self.start_timeout, self.restore_timeout = start_timeout, restore_timeout

    # ---- the Engine API ------------------------------------------------------------------------------------------
    def _client(self, timeout: float = 60):
        import httpx

        return httpx.Client(transport=httpx.HTTPTransport(uds=self.socket), base_url="http://docker", timeout=timeout)

    def _call(self, method: str, path: str, ok=(200, 201, 204, 304), timeout: float = 60, **kw) -> Any:
        import httpx

        try:
            with self._client(timeout) as c:
                r = c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise RuntimeError_(f"docker: {method} {path}: {type(e).__name__}", 503) from e
        if r.status_code not in ok:
            raise RuntimeError_(f"docker: {method} {path}: {r.status_code} {r.text[:300]}",
                                404 if r.status_code == 404 else 409 if r.status_code == 409 else 502)
        if r.content and r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.content

    def _inspect(self, container: str) -> Optional[dict]:
        try:
            return self._call("GET", f"/containers/{container}/json")
        except RuntimeError_ as e:
            if e.status == 404:
                return None
            raise

    def _remove(self, container: str) -> None:
        self._call("DELETE", f"/containers/{container}", params={"force": "true", "v": "false"}, ok=(204, 404))

    def _put(self, container: str, files: dict[str, tuple[bytes, int, int]], make_dirs: bool = True) -> None:
        self._call("PUT", f"/containers/{container}/archive", params={"path": "/"}, content=tar_of(files, make_dirs),
                   headers={"Content-Type": "application/x-tar"})

    def exec(self, container: str, cmd: list[str], user: str = "postgres", timeout: float = 3600) -> tuple[int, str]:
        """Runs ``cmd`` in the container (no shell): its exit code and output (stdout and stderr)."""
        ex = self._call("POST", f"/containers/{container}/exec",
                        json={"Cmd": cmd, "User": user, "AttachStdout": True, "AttachStderr": True})
        raw = self._call("POST", f"/exec/{ex['Id']}/start", json={"Detach": False, "Tty": False}, timeout=timeout)
        out, i = [], 0
        while i + 8 <= len(raw):                       # the multiplexed stream: 8-byte headers
            size = struct.unpack(">I", raw[i + 4:i + 8])[0]
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        info = self._call("GET", f"/exec/{ex['Id']}/json")
        return int(info.get("ExitCode") or 0), b"".join(out).decode(errors="replace")

    def _logs(self, container: str, tail: int = 30) -> str:
        try:
            raw = self._call("GET", f"/containers/{container}/logs",
                             params={"stdout": "true", "stderr": "true", "tail": str(tail)})
        except RuntimeError_:
            return ""
        out, i = [], 0
        while i + 8 <= len(raw):
            size = struct.unpack(">I", raw[i + 4:i + 8])[0]
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        return b"".join(out).decode(errors="replace")[-2000:]

    def _volume(self, name: str, instance: str) -> None:
        self._call("POST", "/volumes/create", json={"Name": name, "Labels": {"cairn.io/pg-instance": instance}})

    def _image(self, image: str) -> None:
        """Pulls the image when it is not here yet."""
        try:
            self._call("GET", f"/images/{image}/json")
        except RuntimeError_ as e:
            if e.status != 404:
                raise
            repo, _, tag = image.partition(":")
            self._call("POST", "/images/create", params={"fromImage": repo, "tag": tag or "latest"}, timeout=900)

    def _run_once(self, name: str, image: str, cmd: list[str], mounts: list[dict], user: str = "",
                  files: Optional[dict] = None, network: str = "", timeout: float = 900, entrypoint=None) -> str:
        """A one-shot container, run to completion and removed: its output; RuntimeError_ if it failed."""
        self._remove(name)
        body: dict[str, Any] = {"Image": image, "Cmd": cmd, "User": user,
                                "HostConfig": {"Mounts": mounts, "NetworkMode": network or self.network or "bridge"},
                                "Labels": {"cairn.io/pg-job": name}}
        if entrypoint is not None:
            body["Entrypoint"] = entrypoint
        self._call("POST", "/containers/create", params={"name": name}, json=body)
        try:
            if files:
                self._put(name, files)
            self._call("POST", f"/containers/{name}/start")
            w = self._call("POST", f"/containers/{name}/wait", timeout=timeout)
            logs = self._logs(name, 50)
            if int(w.get("StatusCode", 1)) != 0:
                raise RuntimeError_(f"{name} failed: {logs[-600:]}")
            return logs
        finally:
            self._remove(name)

    # ---- an instance ---------------------------------------------------------------------------------------------
    def image_for(self, version: str, image: str = "") -> str:
        if image:
            return image
        if str(version) not in self.images:
            raise RuntimeError_(f"PostgreSQL {version} is not offered here (CAIRN_PG_DOCKER_IMAGES: "
                                f"{', '.join(sorted(self.images))})", 400)
        return self.images[str(version)]

    def _pg_command(self, archive: bool) -> list[str]:
        cmd = ["postgres", "-c", "listen_addresses=*", "-c", "wal_level=replica", "-c", "max_wal_senders=10",
               "-c", "shared_preload_libraries=pg_stat_statements", "-c", "password_encryption=scram-sha-256",
               "-c", "restore_command=/usr/local/bin/cairn-restore %f %p"]
        if archive:
            cmd += ["-c", "archive_mode=on", "-c", "archive_command=/usr/local/bin/cairn-archive %p %f",
                    "-c", "archive_timeout=60"]
        else:
            cmd += ["-c", "archive_mode=off"]
        return cmd

    def _create_pg(self, inst: dict, image: str, port: Optional[int], password: Optional[str], archive: bool = True,
                   container: str = "", data: str = "", spool: str = "", publish: bool = True) -> None:
        n = names(inst["name"])
        container, data, spool = container or n["pg"], data or n["data"], spool or n["spool"]
        spec = inst["spec"]
        env = [f"PGDATA={PGDATA}"]
        if password is not None:
            env += ["POSTGRES_PASSWORD_FILE=/etc/cairn/superuser", "POSTGRES_INITDB_ARGS=--data-checksums"]
        host = {"Mounts": [{"Type": "volume", "Source": data, "Target": "/var/lib/postgresql/data"},
                           {"Type": "volume", "Source": spool, "Target": "/spool"}],
                "NetworkMode": n["net"],
                "RestartPolicy": {"Name": "unless-stopped"},
                "NanoCpus": int(float(spec["cpu"]) * 1e9), "Memory": int(spec["memory_mb"]) * 1024 * 1024,
                "ShmSize": min(int(spec["memory_mb"]) // 4, 1024) * 1024 * 1024}
        body: dict[str, Any] = {"Image": image, "Cmd": self._pg_command(archive), "Env": env,
                                "Labels": {"cairn.io/pg-instance": inst["name"]}, "HostConfig": host,
                                "StopTimeout": 60}
        if publish:
            host["PortBindings"] = {"5432/tcp": [{"HostIp": self.bind, "HostPort": str(port or "")}]}
            body["ExposedPorts"] = {"5432/tcp": {}}
        self._call("POST", "/containers/create", params={"name": container}, json=body)
        agent = container + "-agent"
        files = {"usr/local/bin/cairn-archive": (archive_sh(agent).encode(), 0o755, 0),
                 "usr/local/bin/cairn-restore": (restore_sh(agent).encode(), 0o755, 0)}
        if password is not None:
            files["etc/cairn/superuser"] = (password.encode(), 0o444, 0)   # read once by initdb, then emptied
        self._put(container, files)

    def _taken_ports(self) -> set[int]:
        out: set[int] = set()
        for c in self._call("GET", "/containers/json", params={"all": "true",
                                                               "filters": json.dumps({"label": ["cairn.io/pg-instance"]})}):
            info = self._inspect(c["Id"]) or {}
            for b in ((info.get("HostConfig") or {}).get("PortBindings") or {}).get("5432/tcp") or []:
                if str(b.get("HostPort") or "").isdigit():
                    out.add(int(b["HostPort"]))
        return out

    def _start_pg(self, inst: dict, image: str, password: Optional[str], port: Optional[int] = None, **kw) -> None:
        """Creates and starts the instance's container on a port of its own (``port``, or the first free one in
        CAIRN_PG_DOCKER_PORTS): fixed, so it keeps it across restarts."""
        container = kw.get("container") or names(inst["name"])["pg"]
        if not kw.get("publish", True):
            self._create_pg(inst, image, None, password, **kw)
            self._call("POST", f"/containers/{container}/start")
            return
        candidates = [port] if port else [p for p in range(self.ports[0], self.ports[1] + 1)
                                          if p not in self._taken_ports()]
        for p in candidates[:50]:
            self._create_pg(inst, image, p, password, **kw)
            try:
                self._call("POST", f"/containers/{container}/start")
                return
            except RuntimeError_ as e:
                if port or not any(x in str(e) for x in ("allocated", "address already in use", "bind")):
                    raise
                self._remove(container)                 # taken by something else here: the next one
        raise RuntimeError_(f"no free port in {self.ports[0]}-{self.ports[1]} (CAIRN_PG_DOCKER_PORTS)", 503)

    def _network(self, name: str, instance: str) -> None:
        self._call("POST", "/networks/create", json={"Name": name, "Driver": "bridge", "CheckDuplicate": True,
                                                     "Labels": {"cairn.io/pg-instance": instance}}, ok=(200, 201, 409))

    def _create_agent(self, inst: dict, prefix: str, restore_prefix: str = "", container: str = "",
                      pg: str = "", spool: str = "", data: str = "", net: str = "") -> None:
        if not self.store:
            return
        n = names(inst["name"])
        container, pg, spool = container or n["agent"], pg or n["pg"], spool or n["spool"]
        data, net = data or n["data"], net or n["net"]
        # The sidecar runs as PostgreSQL's user (it reads and removes what pg_basebackup writes to the spool).
        uid = self._postgres_uid(pg)
        self.exec(pg, ["sh", "-c", "mkdir -p /spool/base && chown -R postgres:postgres /spool && chmod 700 /spool"],
                  user="root", timeout=60)
        self._remove(container)
        self._call("POST", "/containers/create", params={"name": container}, json={
            "Image": self.agent_image, "Cmd": ["python", "-I", "/agent/backup_agent.py"], "User": f"{uid}:{uid}",
            "Labels": {"cairn.io/pg-instance": inst["name"]},
            "HostConfig": {"NetworkMode": net, "RestartPolicy": {"Name": "unless-stopped"},
                           "Mounts": [{"Type": "volume", "Source": spool, "Target": "/spool"},
                                      {"Type": "volume", "Source": data, "Target": "/data", "ReadOnly": True}],
                           "Memory": 256 * 1024 * 1024}})
        if self.network:                                 # where it reaches the store, when that is not the default
            self._call("POST", f"/networks/{self.network}/connect", json={"Container": container})
        self._put(container, self._agent_files(prefix, restore_prefix, uid))
        self._call("POST", f"/containers/{container}/start")

    def _postgres_uid(self, container: str) -> int:
        rc, out = self.exec(container, ["id", "-u", "postgres"], user="root", timeout=30)
        if rc != 0 or not out.strip().isdigit():
            raise RuntimeError_(f"{container}: no postgres user ({out[-200:]})")
        return int(out.strip())

    def _agent_files(self, prefix: str, restore_prefix: str = "", uid: int = 0) -> dict:
        s = self.store or {}
        cfg = {"endpoint": s["endpoint"], "bucket": s["bucket"], "region": s.get("region") or "us-east-1",
               "prefix": prefix, "restore_prefix": restore_prefix, "port": AGENT_PORT, "spool": "/spool",
               "pg_wal": PGDATA.replace("/var/lib/postgresql/data", "/data") + "/pg_wal", "bind": "0.0.0.0"}
        cred = {"access_key": s["access_key"], "secret_key": s["secret_key"]}
        return {"agent/backup_agent.py": (agent_source(), 0o444, 0),
                "agent/config.json": (json.dumps(cfg).encode(), 0o444, 0),
                "agent/credentials": (json.dumps(cred).encode(), 0o400, uid)}

    def _wait_ready(self, container: str, timeout: float, in_recovery_ok: bool = False) -> None:
        end = time.time() + timeout
        while time.time() < end:
            info = self._inspect(container)
            if info is None:
                raise RuntimeError_(f"{container} is gone")
            st = info.get("State") or {}
            if st.get("Restarting") or (not st.get("Running") and st.get("Status") in ("exited", "dead")):
                raise RuntimeError_(f"{container} stopped: {self._logs(container)[-800:]}")
            if st.get("Running"):
                rc, out = self.exec(container, ["psql", "-h", "/var/run/postgresql", "-U", "postgres", "-tAc",
                                                "SELECT pg_is_in_recovery()"], timeout=30)
                # The image's first start is a temporary server without TCP; wait for the real one.
                if rc == 0 and (out.strip() == "f" or in_recovery_ok):
                    rc2, _ = self.exec(container, ["pg_isready", "-h", "127.0.0.1", "-p", "5432"], timeout=30)
                    if rc2 == 0:
                        return
            time.sleep(1)
        raise RuntimeError_(f"{container} was not ready after {int(timeout)} s: {self._logs(container)[-800:]}", 504)

    def _port(self, container: str) -> int:
        info = self._inspect(container) or {}
        b = ((info.get("NetworkSettings") or {}).get("Ports") or {}).get("5432/tcp") or []
        if not b:
            raise RuntimeError_(f"{container}: no published port")
        return int(b[0]["HostPort"])

    def _tls(self, container: str, cn: str) -> None:
        """A certificate for the instance, in its data directory (it travels with backups), and ssl on."""
        rc, out = self.exec(container, ["id", "-u"], timeout=30)
        uid = int(out.strip()) if rc == 0 and out.strip().isdigit() else 70
        cert, key = self_signed(cn)
        self._put(container, {f"{PGDATA.strip('/')}/cairn-server.crt": (cert, 0o644, uid),
                              f"{PGDATA.strip('/')}/cairn-server.key": (key, 0o600, uid)}, make_dirs=False)
        for stmt in ("ALTER SYSTEM SET ssl_cert_file = 'cairn-server.crt'",
                     "ALTER SYSTEM SET ssl_key_file = 'cairn-server.key'", "ALTER SYSTEM SET ssl = on",
                     "SELECT pg_reload_conf()"):
            self.psql(container, stmt)

    def psql(self, container: str, sql: str, db: str = "postgres") -> str:
        """One statement as the superuser over the local socket (inside the container: no password)."""
        rc, out = self.exec(container, ["psql", "-v", "ON_ERROR_STOP=1", "-h", "/var/run/postgresql", "-U",
                                        "postgres", "-d", db, "-tAc", sql], timeout=600)
        if rc != 0:
            raise RuntimeError_(f"{container}: {out[-400:]}")
        return out.strip()

    def create(self, inst: dict, password: str) -> dict:
        """Makes and starts a new instance; returns where it is reached."""
        n = names(inst["name"])
        image = self.image_for(inst["spec"]["version"], inst.get("image") or "")
        self._image(image)
        if self.store:
            self._image(self.agent_image)
        for v in (n["data"], n["spool"]):
            self._volume(v, inst["name"])
        self._network(n["net"], inst["name"])
        try:
            self._start_pg(inst, image, password)
            self._create_agent(inst, inst["backup_prefix"])
            self._wait_ready(n["pg"], self.start_timeout)
            # Emptied, not removed: the image reads the file at every start (and needs it only for initdb).
            self.exec(n["pg"], ["sh", "-c", ": > /etc/cairn/superuser"], user="root", timeout=30)
            self._tls(n["pg"], f"cairn-pg-{inst['name']}")
        except Exception:
            self.delete(inst, purge=True)
            raise
        return {"host": self.host, "port": self._port(n["pg"]), "image": image}

    def recreate(self, inst: dict, image: str) -> dict:
        """Stops the instance and starts it again from ``image`` on the same volumes and port (a minor upgrade, or a
        setting that needs a restart). The data directory is kept; WAL not yet archived is archived after."""
        n = names(inst["name"])
        self._image(image)
        port = inst.get("port")
        self._call("POST", f"/containers/{n['pg']}/stop", params={"t": "60"}, ok=(204, 304, 404))
        self._remove(n["agent"])
        self._remove(n["pg"])
        self._start_pg(inst, image, None, port)
        self._create_agent(inst, inst["backup_prefix"])
        self._wait_ready(n["pg"], self.start_timeout)
        return {"host": self.host, "port": self._port(n["pg"]), "image": image}

    def restart(self, inst: dict) -> None:
        n = names(inst["name"])
        self._call("POST", f"/containers/{n['pg']}/restart", params={"t": "60"})
        self._wait_running(n["pg"])
        self._wait_ready(n["pg"], self.start_timeout)

    def _wait_running(self, container: str, timeout: float = 60) -> None:
        end = time.time() + timeout
        while time.time() < end:
            st = (self._inspect(container) or {}).get("State") or {}
            if st.get("Running") and not st.get("Restarting"):
                return
            time.sleep(0.5)
        raise RuntimeError_(f"{container} did not start again: {self._logs(container)[-600:]}", 504)

    def heal(self, inst: dict) -> bool:
        """The sidecar answers from inside the instance (it may have lost the namespace when the instance restarted
        on its own); if not, it is made again. True when it had to be."""
        if not self.store:
            return False
        n = names(inst["name"])
        st = (self._inspect(n["pg"]) or {}).get("State") or {}
        if not st.get("Running"):
            return False
        rc, _ = self.exec(n["pg"], ["wget", "-Y", "off", "-q", "-T", "5", "-O", "/dev/null",
                                    f"http://{n['agent']}:{AGENT_PORT}/ready"], timeout=30)
        if rc == 0:
            return False
        logger.warning("instance %s: the backup sidecar did not answer; starting it again", inst["name"])
        self._create_agent(inst, inst["backup_prefix"])
        return True

    def resize(self, inst: dict) -> None:
        spec, n = inst["spec"], names(inst["name"])
        self._call("POST", f"/containers/{n['pg']}/update", json={
            "NanoCpus": int(float(spec["cpu"]) * 1e9), "Memory": int(spec["memory_mb"]) * 1024 * 1024,
            "MemorySwap": -1})

    def delete(self, inst: dict, purge: bool = True) -> None:
        n = names(inst["name"])
        self._remove(n["agent"])
        self._remove(n["pg"])
        if purge:
            for v in (n["data"], n["spool"]):
                self._call("DELETE", f"/volumes/{v}", params={"force": "true"}, ok=(204, 404, 409))
            self._call("DELETE", f"/networks/{n['net']}", ok=(204, 404, 409))

    def sweep(self, older_than_s: float = 7200) -> int:
        """Removes restore-check instances left behind (Cairn stopped during a check)."""
        gone = 0
        for c in self._call("GET", "/containers/json", params={"all": "true",
                                                               "filters": json.dumps({"label": ["cairn.io/pg-instance"]})}):
            name = (c.get("Labels") or {}).get("cairn.io/pg-instance", "")
            if name.startswith("verify-") and time.time() - int(c.get("Created") or 0) > older_than_s:
                self.delete({"name": name}, purge=True)
                gone += 1
        return gone

    def state(self, inst: dict) -> dict:
        n = names(inst["name"])
        pg, agent = self._inspect(n["pg"]), self._inspect(n["agent"])
        st = (pg or {}).get("State") or {}
        out = {"runtime": "docker", "running": bool(st.get("Running")), "status": st.get("Status") or "missing",
               "restarts": (pg or {}).get("RestartCount", 0),
               "image": ((pg or {}).get("Config") or {}).get("Image"),
               "backup_agent": (((agent or {}).get("State") or {}).get("Status") or "missing") if self.store else None,
               "instances": [{"name": n["pg"], "role": "primary", "ready": bool(st.get("Running"))}]}
        if not st.get("Running") and pg is not None:
            out["log"] = self._logs(n["pg"], 20)[-600:]
        return out

    def endpoint(self, inst: dict) -> tuple[str, int]:
        return self.host, int(inst["port"])

    # ---- backups -------------------------------------------------------------------------------------------------
    def backup(self, inst: dict, bid: str) -> dict:
        """A base backup (pg_basebackup in the instance, uploaded by the sidecar): its metadata."""
        n = names(inst["name"])
        rc, out = self.exec(n["pg"], ["pg_basebackup", "-h", "/var/run/postgresql", "-U", "postgres",
                                      "-D", f"/spool/base/{bid}", "-Ft", "-z", "-X", "fetch", "-c", "fast",
                                      "-l", f"cairn {bid}", "--manifest-checksums=SHA256"])
        if rc != 0:
            self.exec(n["pg"], ["rm", "-rf", f"/spool/base/{bid}"], timeout=60)
            raise RuntimeError_(f"pg_basebackup: {out[-400:]}")
        rc, out = self.exec(n["pg"], ["wget", "-Y", "off", "-q", "-T", "3600", "-O", "-", "--post-data=",
                                      f"http://{n['agent']}:{AGENT_PORT}/base/{bid}"])
        if rc != 0:
            self.exec(n["pg"], ["rm", "-rf", f"/spool/base/{bid}"], timeout=60)
            raise RuntimeError_(f"the backup could not be stored: {out[-400:]}")
        try:
            return json.loads(out[out.index("{"):])
        except ValueError:
            raise RuntimeError_(f"the backup sidecar answered: {out[-300:]}")

    def restore(self, inst: dict, base_prefix: str, source_prefix: str, recovery: dict,
                archive: bool = True, scratch: str = "") -> dict:
        """A new instance from a base backup (``<source>/base/<id>``) and the source's archived WAL, recovered to
        ``recovery`` ({"target_time": iso} | {"immediate": True} | {}), then promoted. ``scratch``: a throwaway
        instance for a restore check (not published, nothing archived). The restored superuser keeps the source's
        password until Cairn changes it."""
        n = names(scratch or inst["name"])
        image = self.image_for(inst["spec"]["version"], inst.get("image") or "")
        self._image(image)
        self._image(self.agent_image)
        for v in (n["data"], n["spool"]):
            self._volume(v, inst["name"])
        self._network(n["net"], inst["name"])
        mounts = [{"Type": "volume", "Source": n["data"], "Target": "/var/lib/postgresql/data"},
                  {"Type": "volume", "Source": n["spool"], "Target": "/spool"}]
        try:
            # 1. The sidecar's image downloads the base backup into the spool.
            self._run_once(n["pg"] + "-fetch", self.agent_image,
                           ["python", "-I", "/agent/backup_agent.py", "fetch-base", base_prefix, "/spool/restore"],
                           mounts, user="0", files=self._agent_files(source_prefix))
            # 2. PostgreSQL's image unpacks it into the data directory, as its own user, with the recovery settings.
            conf = ["restore_command = '/usr/local/bin/cairn-restore %f %p'"]
            if recovery.get("target_time"):
                conf += [f"recovery_target_time = '{recovery['target_time']}'", "recovery_target_action = 'promote'",
                         "recovery_target_inclusive = true"]
            elif recovery.get("immediate"):
                conf += ["recovery_target = 'immediate'", "recovery_target_action = 'promote'"]
            unpack = (f"set -e; mkdir -p {PGDATA}; rm -rf {PGDATA}/*; "
                      f"tar xzf /spool/restore/base.tar.gz -C {PGDATA}; "
                      f"if [ -f /spool/restore/pg_wal.tar.gz ]; then tar xzf /spool/restore/pg_wal.tar.gz -C {PGDATA}/pg_wal; fi; "
                      f"cat /etc/cairn/recovery.conf >> {PGDATA}/postgresql.auto.conf; touch {PGDATA}/recovery.signal; "
                      f"rm -f {PGDATA}/postmaster.pid; chown -R postgres:postgres {PGDATA}; chmod 700 {PGDATA}; "
                      "rm -rf /spool/restore")
            self._run_once(n["pg"] + "-unpack", image, ["-c", unpack], mounts, user="0", entrypoint=["sh"],
                           files={"etc/cairn/recovery.conf": (("\n".join(conf) + "\n").encode(), 0o444, 0)})
            # 3. The instance and its sidecar (which serves the source's WAL); recovery runs, then promotes.
            self._start_pg({**inst, "name": scratch or inst["name"]}, image, None, archive=archive,
                           container=n["pg"], data=n["data"], spool=n["spool"], publish=not scratch)
            self._create_agent({**inst, "name": scratch or inst["name"]},
                               inst.get("backup_prefix") or source_prefix, restore_prefix=source_prefix,
                               container=n["agent"], pg=n["pg"], spool=n["spool"], data=n["data"], net=n["net"])
            self._wait_ready(n["pg"], self.restore_timeout)
            # Recovery settings stay out of the data directory (a later restart must not recover again).
            self.psql(n["pg"], "ALTER SYSTEM RESET recovery_target_time")
            self.psql(n["pg"], "ALTER SYSTEM RESET recovery_target")
            self.psql(n["pg"], "ALTER SYSTEM RESET recovery_target_action")
            self.psql(n["pg"], "ALTER SYSTEM RESET recovery_target_inclusive")
            self.psql(n["pg"], "ALTER SYSTEM RESET restore_command")
            self.psql(n["pg"], "SELECT pg_reload_conf()")
        except Exception:
            self.delete({"name": scratch or inst["name"]}, purge=True)
            raise
        if scratch:
            return {"container": n["pg"]}
        return {"host": self.host, "port": self._port(n["pg"]), "image": image}

    def switch_wal(self, inst: dict) -> Optional[str]:
        """Closes the current WAL file (after a commit stamped now) and waits until it is archived: a point in time
        up to now can then be recovered."""
        n = names(inst["name"])
        self.psql(n["pg"], "SELECT txid_current()")
        seg = self.psql(n["pg"], "SELECT pg_walfile_name(pg_switch_wal())")
        end = time.time() + 120
        while time.time() < end:
            last = self.psql(n["pg"], "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")
            if last and last[:24] >= seg[:24]:
                return seg
            time.sleep(1)
        raise RuntimeError_(f"WAL {seg} was not archived within 2 minutes: check the backup sidecar", 504)

