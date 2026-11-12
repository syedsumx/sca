"""The updater (L160): applies a planned update to the Helm release Cairn runs as, in its own Kubernetes Job (started
from the chart's ``<release>-updater`` CronJob template, never on a schedule of its own), so it outlives the pods
it replaces. ``cairn updates apply`` runs it for ``CAIRN_UPDATE_PLAN``.

For each step of the plan:

1. **Verify again.** The feed is checked now; the step's release must still be on the channel with the same chart
   and images (a release withdrawn since it was planned stops the update).
2. **Pre-flight.** The release is ``deployed`` at the version the plan starts from; Kubernetes and Helm are at least
   what the release ``requires``; with ``require_backup``, a backup step is configured.
3. **Back up** (``CAIRN_UPDATER_BACKUP_COMMAND``, e.g. a database snapshot; its last output line is the backup's id).
4. **Fetch the chart** and check its sha256 against the manifest; **render** it with the release's values and the
   manifest's image digests (``global.cairn.digests``) and check every Cairn image is exactly the signed digest.
5. **Upgrade** (``helm upgrade --reset-then-reuse-values --wait``), then **health**: the API answers with the
   step's version.
6. **On failure**, put the install back the safe way. If the database is still at the migration it was at, ``helm
   rollback``. If it moved on (migrations are forward only), restore the backup first
   (``CAIRN_UPDATER_RESTORE_COMMAND``, given ``CAIRN_UPDATE_BACKUP_ID``) and then roll back; with no restore
   configured, stop and say so: old code is never run on a newer schema.

Commands run without a shell except the operator's own backup and restore commands; their output goes to the
plan's log, truncated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import yaml

from cairn.release.manifest import version_key

logger = logging.getLogger(__name__)


class StepFailed(Exception):
    """A step failed; ``changed`` says whether the install was changed (the upgrade had begun)."""

    def __init__(self, message: str, changed: bool = False):
        super().__init__(message)
        self.changed = changed


def images_in(rendered: str) -> list[str]:
    out = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("containers", "initContainers") and isinstance(v, list):
                    out.extend(c.get("image") for c in v if isinstance(c, dict) and c.get("image"))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    for doc in yaml.safe_load_all(rendered):
        walk(doc)
    return out


def digests_of(images: dict[str, str]) -> dict[str, str]:
    """{component: "repo@sha256:…"} → {image name (the repository's last part): "sha256:…"}."""
    out = {}
    for ref in images.values():
        repo, digest = ref.split("@", 1)
        out[repo.rsplit("/", 1)[-1].split(":")[0]] = digest
    return out


def check_images(rendered: str, digests: dict[str, str]) -> None:
    """Every rendered image named like one the manifest signs is exactly that digest, and each is used."""
    seen = set()
    for img in images_in(rendered):
        repo = img.split("@", 1)[0]
        name = repo.rsplit("/", 1)[-1].split(":")[0]
        if name in digests:
            if not img.endswith("@" + digests[name]):
                raise StepFailed(f"the chart renders {img}, not the signed {name}@{digests[name]}")
            seen.add(name)
    if not seen:
        raise StepFailed("the chart renders none of the release's signed images")


class Runner:
    def __init__(self, release: str, namespace: str, health_url: str, feed=None, report=None,
                 head: Optional[Callable[[], Optional[str]]] = None, helm: str = "helm", kubectl: str = "kubectl",
                 backup_command: str = "", restore_command: str = "", timeout_s: int = 900,
                 require_backup: bool = False, env: Optional[dict] = None, plain_http: bool = False,
                 health_get: Optional[Callable[[], tuple[int, dict]]] = None):
        self.release, self.ns, self.health_url = release, namespace, health_url
        self.feed, self.report = feed, report or (lambda step, msg: logger.info("%s: %s", step, msg))
        self.head = head or (lambda: None)
        self.helm, self.kubectl = helm, kubectl
        self.backup_command, self.restore_command = backup_command, restore_command
        self.timeout_s, self.require_backup = timeout_s, require_backup
        self.env = env
        self.plain_http = plain_http            # a registry on this machine over http (tests, a local mirror)
        self.health_get = health_get            # how to ask for the health answer (default: GET health_url)

    # ---- commands -----------------------------------------------------------------------------------------
    def _run(self, args: list[str], timeout: Optional[int] = None, check: bool = True) -> str:
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout or self.timeout_s, env=self.env)
        except subprocess.TimeoutExpired:
            raise StepFailed(f"{args[0]} {args[1]} took longer than {timeout or self.timeout_s}s")
        if check and r.returncode != 0:
            raise StepFailed(f"{' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[-1500:]}")
        return r.stdout

    def _hook(self, what: str, command: str, extra_env: Optional[dict] = None) -> str:
        env = {**(self.env or os.environ), **(extra_env or {})}
        try:
            r = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, timeout=self.timeout_s,
                               env=env)
        except subprocess.TimeoutExpired:
            raise StepFailed(f"the {what} took longer than {self.timeout_s}s")
        out = (r.stdout or "").strip()
        self.report(what, out[-1500:] or "(no output)")
        if r.returncode != 0:
            raise StepFailed(f"the {what} failed ({r.returncode}): {(r.stderr or '').strip()[-1500:]}")
        return out.splitlines()[-1] if out else ""

    def _helm(self, *args: str, timeout: Optional[int] = None, check: bool = True) -> str:
        return self._run([self.helm, *args, "--namespace", self.ns], timeout, check)

    # ---- the release --------------------------------------------------------------------------------------
    def status(self) -> dict:
        return json.loads(self._helm("status", self.release, "-o", "json", timeout=120))

    def revision(self) -> int:
        return int(self.status()["version"])

    def app_version(self) -> str:
        hist = json.loads(self._helm("history", self.release, "-o", "json", "--max", "1", timeout=120))
        return str(hist[-1].get("app_version") or "")

    # ---- one step -----------------------------------------------------------------------------------------
    def preflight(self, step: dict, from_version: str) -> int:
        st = self.status()
        status = (st.get("info") or {}).get("status")
        if status != "deployed":
            raise StepFailed(f"the release {self.release} is {status}, not deployed: settle it first (helm history)")
        running = self.app_version()
        if running and running != from_version:
            raise StepFailed(f"the release runs {running}, the plan starts from {from_version}: plan again")
        req = step.get("requires") or {}
        if req.get("kubernetes"):
            v = json.loads(self._run([self.kubectl, "version", "-o", "json"], 60))["serverVersion"]
            have = f"{v['major']}.{re.sub(r'[^0-9]', '', v['minor'])}.0"
            if version_key(have) < version_key(_semver(req["kubernetes"])):
                raise StepFailed(f"{step['version']} needs Kubernetes {req['kubernetes']}; the cluster runs {have}")
        if req.get("helm"):
            out = self._run([self.helm, "version", "--template", "{{.Version}}"], 60).strip().lstrip("v")
            have = out.split("+")[0]
            if version_key(_semver(have)) < version_key(_semver(req["helm"])):
                raise StepFailed(f"{step['version']} needs Helm {req['helm']}; the updater has {have}")
        if self.require_backup and not self.backup_command:
            raise StepFailed("this install's policy requires a backup before an update, and no backup step is "
                             "configured (cairn.updates.agent.backupCommand)")
        return int(st["version"])

    def fetch_chart(self, step: dict, into: Path) -> Path:
        chart = step["chart"]
        url = chart["url"]
        if url.startswith("oci://"):
            extra = []
            if self.plain_http:
                host = url[len("oci://"):].split("/", 1)[0].split(":")[0]
                if host not in ("127.0.0.1", "localhost"):
                    raise StepFailed("plain http is for a registry on this machine only")
                extra = ["--plain-http"]
            self._run([self.helm, "pull", url, "--version", chart["version"], "--destination", str(into), *extra], 300)
        else:
            self._run([self.helm, "pull", url, "--destination", str(into)], 300)
        tgz = sorted(into.glob("*.tgz"))
        if len(tgz) != 1:
            raise StepFailed("the chart could not be fetched")
        got = "sha256:" + hashlib.sha256(tgz[0].read_bytes()).hexdigest()
        if got != chart["digest"]:
            raise StepFailed(f"the chart fetched is {got}, the release signed {chart['digest']}: refused")
        return tgz[0]

    def overrides(self, step: dict) -> list[str]:
        out = []
        for name, digest in sorted(digests_of(step["images"]).items()):
            if not re.match(r"^[a-z0-9][a-z0-9._-]*$", name):
                raise StepFailed(f"image name {name!r} cannot be set")
            out += ["--set", f"global.cairn.digests.{name}={digest}"]
        return out

    def render(self, chart: Path, step: dict, workdir: Path) -> str:
        values = workdir / "values.yaml"
        values.write_text(self._helm("get", "values", self.release, "-o", "yaml", timeout=120))
        return self._helm("template", self.release, str(chart), "-f", str(values), *self.overrides(step),
                          timeout=300)

    def health(self, version: str) -> None:
        import httpx

        deadline, last = time.time() + min(self.timeout_s, 600), "no answer"
        while time.time() < deadline:
            try:
                if self.health_get is not None:
                    code, body = self.health_get()
                else:
                    r = httpx.get(self.health_url, timeout=10)
                    code, body = r.status_code, (r.json() if r.status_code == 200 else {})
                if code == 200 and body.get("version") == version and body.get("status", "ok") == "ok":
                    return
                last = f"{code} {body.get('version')}"
            except Exception as e:      # noqa: BLE001 - keep asking until the deadline
                last = type(e).__name__
            time.sleep(3)
        raise StepFailed(f"{self.health_url} did not answer with version {version} ({last})", changed=True)

    def step(self, step: dict, from_version: str) -> None:
        if self.feed is not None:
            state = self.feed.check(None)
            if state.get("error"):
                raise StepFailed(f"the release feed does not verify now: {state['error']}")
            now = {s["version"]: s for s in state.get("steps") or []} | {
                a["version"]: a for a in state.get("available") or []}
            cur = now.get(step["version"])
            if cur is None or cur.get("chart") != step["chart"] or cur.get("images") != step["images"]:
                raise StepFailed(f"{step['version']} is no longer offered as planned (withdrawn or changed): stopped")
        prev = self.preflight(step, from_version)
        self.report("preflight", f"release {self.release} revision {prev} at {from_version}; checks passed")
        head_before = self.head()
        backup_id = ""
        if self.backup_command:
            backup_id = self._hook("backup", self.backup_command)
            self.report("backup", f"backup {backup_id or '(no id)'} taken")
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            chart = self.fetch_chart(step, work)
            check_images(self.render(chart, step, work), digests_of(step["images"]))
            self.report("verify", f"chart {step['chart']['digest'][:19]}… and {len(step['images'])} images match "
                                  "the signed release")
            try:
                self._helm("upgrade", self.release, str(chart), "--reset-then-reuse-values", *self.overrides(step),
                           "--wait", "--timeout", f"{self.timeout_s}s", timeout=self.timeout_s + 60)
                self.report("upgrade", f"helm upgraded {self.release} to {step['version']}")
                self.health(step["version"])
            except StepFailed as e:
                self.recover(prev, head_before, backup_id, str(e))
                raise StepFailed(str(e), changed=True)
        self.report("health", f"{step['version']} is up")

    def recover(self, prev: int, head_before: Optional[str], backup_id: str, why: str) -> None:
        head_after = self.head()
        self.report("recover", f"the update failed ({why}); database at {head_after}, it was at {head_before}")
        if head_after != head_before:
            if not self.restore_command:
                raise RolledBackError(f"{why}. The database moved from migration {head_before} to {head_after}, and "
                                      "no restore step is configured, so the release was left as it is rather than "
                                      f"run old code on a newer schema: restore backup {backup_id or '(none taken)'}"
                                      f", then `helm rollback {self.release} {prev}`", rolled_back=False)
            self._hook("restore", self.restore_command, {"CAIRN_UPDATE_BACKUP_ID": backup_id})
            self.report("restore", f"backup {backup_id} restored")
        self._helm("rollback", self.release, str(prev), "--wait", "--timeout", f"{self.timeout_s}s",
                   timeout=self.timeout_s + 60)
        self.report("rollback", f"rolled back to revision {prev}")
        raise RolledBackError(why, rolled_back=True)

    def apply(self, plan: dict) -> tuple[str, Optional[str]]:
        """Apply each step of ``plan`` in turn: ("succeeded", None), ("rolled_back", why) or ("failed", why)."""
        at = plan["from"]
        for step in plan["steps"]:
            self.report("step", f"{at} -> {step['version']}")
            try:
                self.step(step, at)
            except RolledBackError as e:
                return ("rolled_back" if e.rolled_back else "failed"), str(e)
            except StepFailed as e:
                return "failed", (str(e) if not e.changed else f"{e} (the release may be changed: check helm history)")
            at = step["version"]
        return "succeeded", None


class RolledBackError(StepFailed):
    def __init__(self, message: str, rolled_back: bool):
        super().__init__(message, changed=not rolled_back)
        self.rolled_back = rolled_back


def _semver(v: str) -> str:
    parts = re.findall(r"\d+", v)[:3]
    return ".".join((parts + ["0", "0", "0"])[:3])


# ---- starting the updater Job -------------------------------------------------------------------------------
class JobLauncher:
    """Starts the updater as a Job made from the chart's suspended ``<release>-updater`` CronJob (its image,
    service account, settings and secrets), with the plan's id added; Kubernetes API in-cluster by default."""

    def __init__(self, cronjob: str, namespace: str, api: str = "", token: str = "", ca: str = "", http=None):
        self.cronjob, self.ns = cronjob, namespace
        sa = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.api = api or f"https://{os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')}:" \
                          f"{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
        self._token = token or ((sa / "token").read_text().strip() if (sa / "token").exists() else "")
        if http is None:
            import httpx

            http = httpx.Client(timeout=30, verify=ca or (str(sa / "ca.crt") if (sa / "ca.crt").exists() else True))
        self.http = http

    def _call(self, method: str, path: str, body=None) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        r = self.http.request(method, self.api + path, json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r.json()

    def start(self, plan_id: str) -> str:
        cj = self._call("GET", f"/apis/batch/v1/namespaces/{self.ns}/cronjobs/{self.cronjob}")
        spec = json.loads(json.dumps(cj["spec"]["jobTemplate"]["spec"]))
        pod = spec["template"]["spec"]
        for c in pod["containers"]:
            c["env"] = [e for e in c.get("env") or [] if e.get("name") != "CAIRN_UPDATE_PLAN"] + [
                {"name": "CAIRN_UPDATE_PLAN", "value": plan_id}]
        spec["backoffLimit"] = 0                 # an update is never retried by Kubernetes: the plan decides
        spec.setdefault("ttlSecondsAfterFinished", 7 * 86400)
        suffix = plan_id.removeprefix("upd_")[:12]
        name = f"{self.cronjob[:63 - len(suffix) - 1].rstrip('-')}-{suffix}"     # a DNS label: 63, no trailing -
        self._call("POST", f"/apis/batch/v1/namespaces/{self.ns}/jobs", {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/component": "updater", "cairn.io/plan": plan_id}},
            "spec": spec})
        return name

    def state(self, name: str) -> str:
        try:
            job = self._call("GET", f"/apis/batch/v1/namespaces/{self.ns}/jobs/{name}")
        except RuntimeError as e:
            return "missing" if " 404 " in str(e) else "unknown"
        st = job.get("status") or {}
        if st.get("succeeded"):
            return "succeeded"
        if st.get("failed"):
            return "failed"
        return "running"


def launcher_from_env() -> Optional[JobLauncher]:
    cj = os.environ.get("CAIRN_UPDATER_CRONJOB", "").strip()
    if not cj:
        return None
    return JobLauncher(cj, os.environ.get("CAIRN_UPDATER_NAMESPACE") or _namespace())


def _namespace() -> str:
    p = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    return p.read_text().strip() if p.exists() else "default"
