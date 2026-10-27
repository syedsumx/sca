"""Run logs streamed from the agent to Cairn while a run is in progress.

A :class:`RunLogShipper` captures log records emitted by one run's thread, removes every secret
value the agent resolved for that run, and uploads the lines encrypted every few seconds. Lines
are kept if an upload fails and sent with the next one; past ``max_buffered`` the oldest are
dropped and the drop is reported in the log itself.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional

from cairn.agent.client import GatewayClient, GatewayHTTPError

logger = logging.getLogger(__name__)
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class RunLogShipper(logging.Handler):
    def __init__(self, client: GatewayClient, run_id: str, secrets: Iterable[str] = (),
                 interval: float = 2.0, max_buffered: int = 5000, level: int = logging.INFO):
        super().__init__(level)
        self.client = client
        self.run_id = run_id
        # Longest first, so a secret that contains another is removed whole.
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)
        self.interval = interval
        self.max_buffered = max_buffered
        self.setFormatter(logging.Formatter(FORMAT))
        self.seq = -1
        self.shipped = 0
        self._threads: set[int] = set()
        self._buffer: list[str] = []
        self._dropped = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._timer: Optional[threading.Thread] = None

    # ---- capture -------------------------------------------------------------------------
    def watch_current_thread(self) -> None:
        self._threads.add(threading.get_ident())

    def scrub(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread in self._threads and not record.name.startswith("cairn.agent.runlog")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.scrub(self.format(record))[:2000]
        except Exception:  # noqa: BLE001 - never break the run over a log line
            return
        self.line(line)

    def line(self, text: str) -> None:
        with self._lock:
            self._buffer.append(self.scrub(text))
            if len(self._buffer) > self.max_buffered:
                over = len(self._buffer) - self.max_buffered
                del self._buffer[:over]
                self._dropped += over

    # ---- shipping ------------------------------------------------------------------------
    def ship(self) -> bool:
        with self._lock:
            if self._dropped:
                self._buffer.insert(0, f"... {self._dropped} earlier log lines were dropped (gateway unreachable)")
                self._dropped = 0
            lines, self._buffer = self._buffer, []
        if not lines:
            return True
        seq = self.seq + 1
        try:
            self.client.upload_logs(self.run_id, seq, lines)
        except GatewayHTTPError as e:
            if e.status == 409 and "already received" in str(e):
                self.seq = seq                 # the previous attempt landed
                return True
            if e.status < 500:
                logger.debug("run %s logs refused: %s", self.run_id, e)
                return True                    # the run is gone; do not keep the lines
            self._requeue(lines)
            return False
        except Exception:  # noqa: BLE001 - transport errors: keep the lines for the next round
            self._requeue(lines)
            return False
        self.seq = seq
        self.shipped += len(lines)
        return True

    def _requeue(self, lines: list[str]) -> None:
        with self._lock:
            self._buffer[:0] = lines

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.ship()

    def start(self) -> "RunLogShipper":
        self.watch_current_thread()
        logging.getLogger().addHandler(self)
        self._timer = threading.Thread(target=self._loop, daemon=True, name=f"cairn-logs-{self.run_id}")
        self._timer.start()
        return self

    def stop(self) -> None:
        logging.getLogger().removeHandler(self)
        self._stop.set()
        if self._timer is not None:
            self._timer.join(timeout=self.interval + 5)
        for _ in range(3):
            if self.ship():
                break
