"""``cairn-spark``: any Spark program on the lakehouse as a Cairn identity: Scala and Java jobs, the
``spark-sql`` shell, ``spark-submit`` of Python files, on any master (local, Kubernetes, standalone).

    cairn-spark submit --master k8s://https://kubernetes.default.svc --class com.acme.Job job.jar
    cairn-spark sql -e "SELECT count(*) FROM lakehouse.sales.orders"

It opens a lakehouse session at Cairn (CAIRN_URL, CAIRN_TOKEN[_FILE] or CAIRN_WORKLOAD_TOKEN_FILE,
CAIRN_CATALOG_URI) and runs Spark with its settings in a private properties file (0600, removed
afterwards; the session credential never appears on a command line). ``--properties-file`` and
``--conf`` given by the caller still apply: the caller's ``--conf`` win.

On Kubernetes in cluster mode (``--master k8s://... --deploy-mode cluster``) it opens no session:
spark-submit would store its settings in a ConfigMap. It submits the application with no credential,
a driver pod template mounting a projected service-account token, and the driver's Cairn settings; the
driver (a Python program calling ``lakehouse_session()``) signs in itself. The driver's service account
is ``--conf spark.kubernetes.authenticate.driver.serviceAccountName=...`` or CAIRN_SPARK_SERVICE_ACCOUNT.
A JVM application's driver pod runs cairn-spark-driver, which signs in the same way before Spark
starts. See cairn_spark.kubernetes and cairn_spark.driver.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from cairn_spark import CairnSparkError, session_conf
from cairn_spark import kubernetes as k8s

SPARK_HOME = os.environ.get("SPARK_HOME", "/opt/spark")
TOOLS = {"submit": "spark-submit", "sql": "spark-sql", "shell": "spark-shell", "pyspark": "pyspark"}


def _properties(conf: dict, extra_file: str | None) -> str:
    fd, path = tempfile.mkstemp(prefix="cairn-spark-", suffix=".conf")
    with os.fdopen(fd, "w") as f:
        if extra_file:
            with open(extra_file) as g:
                f.write(g.read().rstrip("\n") + "\n")
        for k, v in conf.items():
            f.write(f"{k} {v}\n")
    os.chmod(path, 0o600)
    return path


FLAGS = {"--verbose", "-v", "--supervise", "--help", "-h", "--version", "--kill", "--status"}


def _conf_value(rest: list[str], key: str) -> str | None:
    for i, a in enumerate(rest):
        if a == "--conf" and i + 1 < len(rest) and rest[i + 1].startswith(key + "="):
            return rest[i + 1].split("=", 1)[1]
        if a.startswith("--conf=" + key + "="):
            return a.split("=", 2)[2]
    return None


def _opt(rest: list[str], name: str) -> str | None:
    for i, a in enumerate(rest):
        if a == name and i + 1 < len(rest):
            return rest[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _primary(rest: list[str]) -> str | None:
    """spark-submit's primary resource: the first argument that is not an option or an option's value."""
    i = 0
    while i < len(rest):
        a = rest[i]
        if not a.startswith("-"):
            return a
        i += 1 if (a in FLAGS or "=" in a) else 2
    return None


def _cluster_on_kubernetes(rest: list[str]) -> bool:
    master = _opt(rest, "--master") or _conf_value(rest, "spark.master") or ""
    mode = _opt(rest, "--deploy-mode") or _conf_value(rest, "spark.submit.deployMode") or "client"
    return master.startswith("k8s://") and mode == "cluster"


def _submit_cluster(rest: list[str], extra: str | None) -> int:
    main = _primary(rest) or ""
    # A JVM program: its driver pod signs in before Spark starts (cairn-spark-driver); a Python program
    # signs in itself when it calls lakehouse_session().
    jvm = bool(_opt(rest, "--class")) or not main.endswith(".py")
    sa = _conf_value(rest, "spark.kubernetes.authenticate.driver.serviceAccountName") or \
        os.environ.get("CAIRN_SPARK_SERVICE_ACCOUNT", "")
    url, catalog = os.environ.get("CAIRN_URL", ""), os.environ.get("CAIRN_CATALOG_URI", "")
    if not (sa and url and catalog):
        print("cairn-spark: cluster mode needs the driver's service account (bound to a Cairn user), CAIRN_URL and "
              "CAIRN_CATALOG_URI", file=sys.stderr)
        return 2
    import json

    fd, template = tempfile.mkstemp(prefix="cairn-spark-driver-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(k8s.driver_pod_template(os.environ.get("CAIRN_WORKLOAD_AUDIENCE", "cairn"), jvm=jvm), f)
    props = _properties(k8s.cluster_conf(url, catalog, sa, template), extra)
    print(f"cairn-spark: submitting {main} to Kubernetes; its driver signs in as service account {sa}", file=sys.stderr)
    try:
        return subprocess.call([os.path.join(SPARK_HOME, "bin", "spark-submit"), "--properties-file", props, *rest])
    finally:
        os.unlink(props)
        os.unlink(template)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in TOOLS:
        print(f"usage: cairn-spark {{{'|'.join(TOOLS)}}} [Spark arguments]", file=sys.stderr)
        return 2
    tool, rest = TOOLS[args[0]], args[1:]
    extra = None
    if "--properties-file" in rest:
        i = rest.index("--properties-file")
        extra = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    if tool == "spark-submit" and _cluster_on_kubernetes(rest):
        return _submit_cluster(rest, extra)
    name = next((rest[i + 1] for i, a in enumerate(rest[:-1]) if a == "--name"), None) or os.environ.get("CAIRN_SPARK_APP", "cairn")
    try:
        conf, user = session_conf(name)
    except CairnSparkError as e:
        print(f"cairn-spark: {e}", file=sys.stderr)
        return 2
    props = _properties(conf, extra)
    print(f"cairn-spark: running {tool} as {user}", file=sys.stderr)
    try:
        return subprocess.call([os.path.join(SPARK_HOME, "bin", tool), "--properties-file", props, *rest])
    finally:
        os.unlink(props)


if __name__ == "__main__":
    sys.exit(main())
