"""Leader-elected monitor scheduler.

Exactly one replica acts as leader (Postgres advisory lock); standbys retry the lock
every ``tick_seconds``. The leader evaluates due monitors on a bounded thread pool and
computes each monitor's next run from its cron expression or interval.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Optional

from croniter import croniter

from cairn.common import metrics
from cairn.common.db import advisory_lock
from cairn.common.proposals import now_ms
from cairn.quality.engine import QualityEngine
from cairn.quality.models import Monitor

logger = logging.getLogger(__name__)

LEADER_LOCK_KEY = 0x0D4E_0001


def next_run_ms(monitor: Monitor, after_ms: int) -> int:
    sched = monitor.schedule
    if sched.cron:
        base = datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)
        return int(croniter(sched.cron, base).get_next(datetime).timestamp() * 1000)
    return after_ms + int(sched.interval_seconds or 3600) * 1000


class Scheduler:
    def __init__(self, engine: QualityEngine, tick_seconds: float = 15.0, max_workers: int = 8,
                 partition: int = 0, partitions: int = 1):
        self.engine = engine
        self.tick_seconds = tick_seconds
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cairn-monitor")
        self.partition = partition
        self.partitions = partitions
        self._stop = threading.Event()

    auto_interval = float(os.environ.get("CAIRN_AUTO_MONITOR_INTERVAL_SECONDS", "600"))

    def sweep_auto_monitors(self) -> Optional[dict]:
        """Keep auto monitors in line with the rules (one leader does it)."""
        from cairn.quality.auto import load_rules, sweep

        rules = load_rules()
        if not rules.rules or self.engine.graph is None:
            return None
        try:
            return sweep(self.engine.store, self.engine, self.engine.graph, rules, connections=self.engine.connections)
        except Exception:
            logger.exception("auto-monitor sweep failed")
            return None

    def stop(self, *_args) -> None:
        self._stop.set()

    def run_once(self, now: Optional[int] = None) -> int:
        """Evaluate every due monitor once; returns the number evaluated."""
        now = now if now is not None else now_ms()
        due = self.engine.store.due_monitors(now, self.partition, self.partitions)
        futures = []
        for monitor in due:
            scheduled = monitor.next_run_at_ms or now
            # Claim the slot before running, so a crash does not cause a hot loop.
            self.engine.store.set_next_run(monitor.id, next_run_ms(monitor, max(now, scheduled)))
            futures.append(self.pool.submit(self.engine.evaluate, monitor, now, scheduled))
        wait(futures)
        for f in futures:
            if f.exception():  # evaluate() already guards; this is belt and braces
                logger.error("evaluation crashed: %s", f.exception())
        return len(due)

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        store_engine = self.engine.store.engine
        while not self._stop.is_set():
            with advisory_lock(store_engine, LEADER_LOCK_KEY + self.partition) as leader:
                if not leader:
                    metrics.worker_cycle("scheduler", True)   # a healthy standby is not stalled
                    logger.debug("standby: another scheduler holds the leader lock")
                    self._stop.wait(self.tick_seconds)
                    continue
                logger.info("acquired scheduler leadership (partition %s/%s)", self.partition, self.partitions)
                for monitor in self.engine.store.list_monitors():
                    self.engine.sync_monitor(monitor)
                last_flush = last_sweep = 0.0
                while not self._stop.is_set():
                    started = time.monotonic()
                    try:
                        if self.partition == 0 and started - last_sweep > self.auto_interval:
                            self.sweep_auto_monitors()
                            last_sweep = started
                        self.run_once()
                        if started - last_flush > 300:
                            self.engine.flush_unemitted()
                            last_flush = started
                        metrics.worker_cycle("scheduler", True)
                    except Exception:
                        metrics.worker_cycle("scheduler", False)
                        logger.exception("scheduler tick failed")
                    self._stop.wait(max(0.0, self.tick_seconds - (time.monotonic() - started)))
        self.pool.shutdown(wait=True)
