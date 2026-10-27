"""The in-VPC agent daemon.

Lifecycle: enrol once (one-time token) → loop {heartbeat, poll, run task} with an
outbound-only connection to the gateway. Each task runs a Cairn Core ingestion pipeline
in a worker thread with secrets resolved locally and the ``cairn-encrypted`` sink.
"""

from __future__ import annotations

import copy
import logging
import os
import signal
import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel, Field

from cairn.agent.client import AgentState, GatewayClient, GatewayHTTPError, with_backoff
from cairn.agent.runlog import RunLogShipper
from cairn.agent.secrets import SecretNotFound, SecretResolver
from cairn.agent.sink import RunCancelled, RunChannel, SpoolFull, register_channel, unregister_channel

logger = logging.getLogger(__name__)


class AgentConfig(BaseModel):
    gateway_url: str
    state_dir: str = "/var/lib/cairn-agent"
    enrollment_token: Optional[str] = None
    max_parallel_tasks: int = Field(default=1, ge=1, le=16)
    poll_interval_seconds: float = 5.0
    heartbeat_interval_seconds: float = 15.0
    secrets: list[dict] = Field(default_factory=lambda: [{"type": "env"}])
    tls_ca_bundle: Optional[str] = None
    batch_max_items: int = 500
    batch_max_bytes: int = 1 << 20
    spool_max_bytes: int = 512 << 20       # sealed batches kept on disk while the gateway is unreachable
    log_interval_seconds: float = 2.0

    @classmethod
    def load(cls, path: Optional[str]) -> "AgentConfig":
        data: dict[str, Any] = {}
        if path and os.path.exists(path):
            with open(path) as fh:
                data = yaml.safe_load(fh) or {}
        env = {
            "gateway_url": os.environ.get("CAIRN_AGENT_GATEWAY_URL"),
            "state_dir": os.environ.get("CAIRN_AGENT_STATE_DIR"),
            "enrollment_token": os.environ.get("CAIRN_AGENT_ENROLLMENT_TOKEN"),
        }
        data.update({k: v for k, v in env.items() if v})
        return cls.model_validate(data)


PipelineRunner = Callable[[dict], dict]


def default_pipeline_runner(recipe: dict) -> dict:
    """Run a Cairn Core ingestion pipeline in-process and return its report."""
    from datahub.ingestion.run.pipeline import Pipeline

    pipeline = Pipeline.create(recipe)
    pipeline.run()
    pipeline.raise_from_status(raise_warnings=False)
    return {"source": pipeline.source.get_report().as_obj(), "sink": pipeline.sink.get_report().as_obj()}


class AgentWorker:
    def __init__(self, config: AgentConfig, runner: PipelineRunner = default_pipeline_runner,
                 client: Optional[GatewayClient] = None):
        self.config = config
        self.state = AgentState(config.state_dir)
        self.client = client or GatewayClient(config.gateway_url, self.state,
                                              verify=config.tls_ca_bundle or True)
        self.secrets = SecretResolver.from_config(config.secrets)
        self.runner = runner
        self.pool = ThreadPoolExecutor(max_workers=config.max_parallel_tasks, thread_name_prefix="cairn-task")
        self.running: dict[str, Future] = {}
        self.cancels: dict[str, threading.Event] = {}
        self.spool_dir = os.path.join(config.state_dir, "spool")
        self._stop = threading.Event()

    def ensure_enrolled(self) -> None:
        if self.state.enrolled:
            return
        if not self.config.enrollment_token:
            raise SystemExit("agent is not enrolled and no CAIRN_AGENT_ENROLLMENT_TOKEN was provided")
        body = with_backoff(lambda: self.client.enroll(self.config.enrollment_token))  # type: ignore[arg-type]
        logger.info("enrolled as %s in pool %s", body["agent_id"], body["pool"])

    def prepare_recipe(self, task: dict, seen: Optional[set] = None) -> dict:
        recipe = copy.deepcopy(task["recipe"])
        recipe = self.secrets.resolve(recipe, seen)
        recipe["sink"] = {"type": "cairn-encrypted", "config": {"run_id": task["run_id"]}}
        recipe.setdefault("pipeline_name", f"cairn-{task['task_id']}")
        recipe["run_id"] = task["run_id"]
        return recipe

    def run_task(self, task: dict) -> str:
        run_id, task_id = task["run_id"], task["task_id"]
        cancelled = self.cancels.setdefault(task_id, threading.Event())
        channel = RunChannel(self.client, run_id, self.config.batch_max_items, self.config.batch_max_bytes,
                             cancelled=cancelled, spool_dir=self.spool_dir,
                             spool_max_bytes=self.config.spool_max_bytes)
        register_channel(channel)
        status, report = "SUCCESS", {}
        secrets: set = set()
        shipper = RunLogShipper(self.client, run_id, interval=self.config.log_interval_seconds)
        try:
            recipe = self.prepare_recipe(task, secrets)
            shipper.secrets = sorted({v for v in secrets if len(v) >= 4}, key=len, reverse=True)
            shipper.start()
            shipper.line(f"run {run_id} started on the agent ({recipe.get('source', {}).get('type', '?')} source)")
            if cancelled.is_set():
                raise RunCancelled(f"run {run_id} was cancelled")
            report = self.runner(recipe) or {}
            channel.flush()
        except SecretNotFound as e:
            status, report = "FAILURE", {"error": f"secret {e.args[0]!r} not found in any configured backend"}
        except Exception as e:
            status = "CANCELLED" if cancelled.is_set() or isinstance(e, RunCancelled) else "FAILURE"
            if status == "FAILURE":
                report = {**report, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-8000:]}
                if not isinstance(e, SpoolFull):
                    try:
                        channel.flush()
                    except Exception:
                        pass
        finally:
            unregister_channel(run_id)
        if status == "SUCCESS" and channel.pending:
            # Batches are spooled: the run only succeeds once the gateway has every one of them.
            lease = float(task.get("lease_seconds") or 300)
            shipper.line(f"waiting for the gateway to accept {channel.pending} spooled batch(es)")
            if not channel.wait_drained(timeout=lease):
                shipper.stop()
                channel.discard_spool()
                self.cancels.pop(task_id, None)
                logger.error("run %s: gateway unreachable for %.0fs; giving the task back", run_id, lease)
                return "ABANDONED"
        report["mcps_sent"] = channel.sent
        if channel.spooled_total:
            report["batches_spooled"] = channel.spooled_total
        shipper.line(f"run {run_id} finished: {status} ({channel.sent} metadata changes sent)")
        shipper.stop()
        channel.discard_spool()
        self.cancels.pop(task_id, None)
        try:
            with_backoff(lambda: self.client.complete(run_id, status, _scrub(report, secrets), channel.seq))
        except GatewayHTTPError as e:
            if e.status != 409:
                raise
            logger.info("run %s was closed by the gateway (%s)", run_id, e)
        logger.info("run %s finished: %s (%d MCPs)", run_id, status, channel.sent)
        return status

    def stop(self, *_args) -> None:
        self._stop.set()

    def tick(self) -> None:
        for tid in [t for t, f in self.running.items() if f.done()]:
            self.running.pop(tid)
        if len(self.running) < self.config.max_parallel_tasks:
            task = self.client.poll()
            if task:
                logger.info("leased task %s (run %s)", task["task_id"], task["run_id"])
                self.running[task["task_id"]] = self.pool.submit(self.run_task, task)

    def handle_heartbeat(self, reply: Optional[dict]) -> None:
        for task_id in (reply or {}).get("cancel_task_ids") or []:
            if task_id in self.running:
                logger.info("task %s was cancelled in Cairn; stopping it", task_id)
                self.cancels.setdefault(task_id, threading.Event()).set()

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.handle_heartbeat(self.client.heartbeat(list(self.running)))
            except Exception as e:
                logger.warning("heartbeat failed: %s", e)
            self._stop.wait(self.config.heartbeat_interval_seconds)

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.ensure_enrolled()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="cairn-heartbeat").start()
        backoff = self.config.poll_interval_seconds
        while not self._stop.is_set():
            try:
                self.tick()
                backoff = self.config.poll_interval_seconds
            except GatewayHTTPError as e:
                if e.status == 401:
                    logger.error("gateway rejected agent credentials (revoked?): %s", e)
                    raise SystemExit(2)
                logger.warning("poll failed: %s", e)
                backoff = min(60.0, backoff * 2)
            except Exception as e:
                logger.warning("poll failed: %s", e)
                backoff = min(60.0, backoff * 2)
            self._stop.wait(backoff)
        self.pool.shutdown(wait=True)


def _scrub(report: dict, secrets: Optional[set] = None) -> dict:
    """Reports go to the central plane: drop anything that looks like resolved config, and every
    secret value used for the run wherever it appears (error messages, tracebacks)."""
    import json

    report = dict(report)
    for key in ("recipe", "config", "source_config"):
        report.pop(key, None)
    values = sorted({s for s in secrets or () if len(s) >= 4}, key=len, reverse=True)
    if not values:
        return report
    text = json.dumps(report, default=str)
    for v in values:
        text = text.replace(json.dumps(v)[1:-1], "***")
    return json.loads(text)


def main(config_path: Optional[str]) -> None:
    logging.basicConfig(level=os.environ.get("CAIRN_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    AgentWorker(AgentConfig.load(config_path)).run_forever()
