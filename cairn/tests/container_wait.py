"""Waiting for a container the end-to-end tests start, and saying why when it does not come up.

The tests start Trino with ``--rm``: one that exits on start is gone at once, so polling its log until a deadline
waits for nothing and the failure said only "did not start". This stops as soon as the container is not running
and reports its state and exit code (137: killed, e.g. for memory), the end of its log, what else is running and the
machine's free memory, so a CI failure can be told apart: a slow start, a crash, or a runner out of room."""

from __future__ import annotations

import subprocess
import time


def _out(*cmd: str) -> str:
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=30)
        return (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001 - diagnostics only
        return f"({e})"


def wait_started(name: str, marker: str = "SERVER STARTED", seconds: float = 240, what: str = "Trino") -> None:
    end = time.time() + seconds
    while True:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        if marker in logs.stderr or marker in logs.stdout:
            return
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}} exit={{.State.ExitCode}} "
                                "oom={{.State.OOMKilled}}", name], capture_output=True, text=True)
        gone = state.returncode != 0 or not state.stdout.startswith("running")
        if gone or time.time() >= end:
            why = "its container stopped" if gone else f"still starting after {int(seconds)} s"
            raise AssertionError(
                f"{what} did not start ({why}; state: {(state.stdout or state.stderr).strip()})\n"
                f"--- its log (end) ---\n{(logs.stdout + logs.stderr)[-4000:]}\n"
                f"--- running containers ---\n{_out('docker', 'ps', '--format', '{{.Names}} {{.Image}} {{.Status}}')}\n"
                f"--- memory ---\n{_out('free', '-m')}")
        time.sleep(1)
