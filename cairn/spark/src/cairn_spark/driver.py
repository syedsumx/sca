"""``cairn-spark-driver``: the entrypoint of a driver pod for JVM applications in cluster mode.

A JVM program cannot sign in from inside the way ``lakehouse_session()`` does, and settings given at
submit time are stored in a ConfigMap. So ``cairn-spark submit --deploy-mode cluster --class …`` makes
this the driver container's command: it signs in with the pod's projected workload token, opens a
lakehouse session, merges its settings into a private copy of the driver's properties file (0600, in
the pod's own filesystem) and starts Spark's own entrypoint with that file. Nothing secret is ever
written to the cluster's API. Any other role (executors) passes straight through.
"""

from __future__ import annotations

import os
import sys
import tempfile

ENTRYPOINT = os.environ.get("CAIRN_SPARK_ENTRYPOINT", "/opt/entrypoint.sh")


def merged_args(args: list[str], conf: dict) -> list[str]:
    """Spark's driver arguments with ``--properties-file`` pointing at a private copy that adds
    ``conf`` (the session's settings win over the submitted ones)."""
    args = list(args)
    original = ""
    if "--properties-file" in args:
        i = args.index("--properties-file")
        original = args[i + 1]
        del args[i:i + 2]
    fd, path = tempfile.mkstemp(prefix="cairn-driver-", suffix=".properties")
    with os.fdopen(fd, "w") as f:
        if original and os.path.exists(original):
            with open(original) as g:
                f.write(g.read().rstrip("\n") + "\n")
        for k, v in conf.items():
            f.write(f"{k}={v}\n")
    os.chmod(path, 0o600)
    return ["--properties-file", path, *args]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "driver":
        os.execv(ENTRYPOINT, [ENTRYPOINT, *args])
    from cairn_spark import CairnSparkError, session_conf

    try:
        conf, user = session_conf(os.environ.get("CAIRN_SPARK_APP", "cairn"))
    except CairnSparkError as e:
        print(f"cairn-spark-driver: {e}", file=sys.stderr)
        return 2
    print(f"cairn-spark-driver: the driver runs as {user}", file=sys.stderr, flush=True)
    new = ["driver", *merged_args(args[1:], conf)]
    os.execv(ENTRYPOINT, [ENTRYPOINT, *new])
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
