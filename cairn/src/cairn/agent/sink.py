"""``cairn-encrypted`` ingestion sink: buffers MCPs and ships them to the gateway encrypted.

Registered through the ``datahub.ingestion.sink.plugins`` entry point, so recipes run
by the agent use::

    sink:
      type: cairn-encrypted
      config:
        run_id: run_123   # injected by the agent worker

The worker registers a :class:`RunChannel` for the run before starting the pipeline;
the sink looks it up by ``run_id``. This keeps key material out of the recipe.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from typing import Any, Optional

from cairn.agent.client import GatewayClient, GatewayHTTPError, is_transient, with_backoff

logger = logging.getLogger(__name__)

_channels: dict[str, "RunChannel"] = {}
_channels_lock = threading.Lock()


class RunCancelled(RuntimeError):
    """The run was cancelled in Cairn; the pipeline stops at its next record."""


class SpoolFull(RuntimeError):
    pass


class RunChannel:
    """Batches MCPs for one run and ships them encrypted, in sequence order.

    While the gateway is unreachable, sealed batches are spooled to ``spool_dir`` (they are already
    encrypted for the gateway, so nothing readable lands on disk) and the pipeline keeps going;
    :meth:`drain` sends them in order once the gateway answers again.
    """

    def __init__(self, client: GatewayClient, run_id: str, max_items: int = 500, max_bytes: int = 1 << 20,
                 cancelled: Optional[threading.Event] = None, spool_dir: Optional[str] = None,
                 spool_max_bytes: int = 512 << 20, send_attempts: int = 3):
        self.client = client
        self.run_id = run_id
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.cancelled = cancelled or threading.Event()
        self.spool_dir = os.path.join(spool_dir, run_id) if spool_dir else None
        self.spool_max_bytes = spool_max_bytes
        self.send_attempts = send_attempts
        self.seq = -1
        self.sent = 0
        self.spooled_total = 0
        self._pending: list[int] = []     # spooled sequence numbers not yet accepted, in order
        self._spool_bytes = 0
        self._buffer: list[dict] = []
        self._size = 0
        self._lock = threading.Lock()

    def add(self, mcp: dict) -> None:
        if self.cancelled.is_set():
            raise RunCancelled(f"run {self.run_id} was cancelled")
        size = len(json.dumps(mcp))
        with self._lock:
            if self._buffer and (len(self._buffer) >= self.max_items or self._size + size > self.max_bytes):
                self._flush_locked()
            self._buffer.append(mcp)
            self._size += size

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    @property
    def pending(self) -> int:
        return len(self._pending)

    def _send(self, seq: int, envelope: dict) -> None:
        try:
            with_backoff(lambda: self.client.send_batch(self.run_id, seq, envelope), attempts=self.send_attempts,
                         base=0.5)
        except GatewayHTTPError as e:
            if e.status == 409 and "CANCELLED" in str(e):
                self.cancelled.set()
                raise RunCancelled(f"run {self.run_id} was cancelled") from None
            raise

    def _spool(self, seq: int, envelope: dict) -> None:
        assert self.spool_dir is not None
        data = json.dumps(envelope).encode()
        if self._spool_bytes + len(data) > self.spool_max_bytes:
            raise SpoolFull(f"gateway unreachable and the spool for run {self.run_id} is full "
                            f"({self.spool_max_bytes >> 20} MiB)")
        os.makedirs(self.spool_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.spool_dir, f"{seq:08d}.json")
        with open(path + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(path + ".tmp", path)
        self._pending.append(seq)
        self._spool_bytes += len(data)
        self.spooled_total += 1

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        seq = self.seq + 1
        batch = self._buffer
        envelope = self.client.seal_batch(self.run_id, seq, batch)
        if self._pending and not self._drain_locked():
            self._spool(seq, envelope)                       # keep order behind earlier spooled batches
        else:
            try:
                self._send(seq, envelope)
            except Exception as e:
                if self.spool_dir is None or not is_transient(e):
                    raise
                logger.warning("gateway unreachable, spooling batch %d of run %s: %s", seq, self.run_id, e)
                self._spool(seq, envelope)
        self.seq = seq
        self.sent += len(batch)
        self._buffer, self._size = [], 0

    def _drain_locked(self) -> bool:
        while self._pending:
            seq = self._pending[0]
            path = os.path.join(self.spool_dir or "", f"{seq:08d}.json")
            with open(path, "rb") as fh:
                data = fh.read()
            try:
                self._send(seq, json.loads(data))
            except GatewayHTTPError as e:
                if e.status == 409 and "not greater than last accepted" in str(e):
                    pass                                     # accepted before the connection dropped
                elif is_transient(e):
                    return False
                else:
                    raise
            except Exception as e:
                if is_transient(e):
                    return False
                raise
            os.remove(path)
            self._pending.pop(0)
            self._spool_bytes -= len(data)
        return True

    def drain(self) -> bool:
        """Send spooled batches; True once none are left."""
        with self._lock:
            return self._drain_locked()

    def wait_drained(self, timeout: float, interval: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while not self.drain():
            if time.monotonic() >= deadline or self.cancelled.is_set():
                return False
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        return True

    def discard_spool(self) -> None:
        if self.spool_dir and os.path.isdir(self.spool_dir):
            shutil.rmtree(self.spool_dir, ignore_errors=True)
        self._pending.clear()
        self._spool_bytes = 0


def register_channel(channel: RunChannel) -> None:
    with _channels_lock:
        _channels[channel.run_id] = channel


def unregister_channel(run_id: str) -> Optional[RunChannel]:
    with _channels_lock:
        return _channels.pop(run_id, None)


def get_channel(run_id: str) -> RunChannel:
    with _channels_lock:
        if run_id not in _channels:
            raise RuntimeError(f"no gateway channel registered for run {run_id}; "
                               "the cairn-encrypted sink can only be used by the Cairn agent worker")
        return _channels[run_id]


def record_to_mcp_dicts(record: Any) -> list[dict]:
    """Normalize MCE / MCP / MCPW records into Rest.li-format MCP dicts."""
    from datahub.emitter.mcp_builder import mcps_from_mce
    from datahub.emitter.serialization_helper import pre_json_transform
    from datahub.metadata.schema_classes import MetadataChangeEventClass

    if isinstance(record, MetadataChangeEventClass):
        return [pre_json_transform(m.to_obj()) for m in mcps_from_mce(record)]
    return [pre_json_transform(record.to_obj())]


try:
    from datahub.configuration.common import ConfigModel
    from datahub.ingestion.api.sink import Sink, SinkReport

    class EncryptedGatewaySinkConfig(ConfigModel):
        run_id: str

    class EncryptedGatewaySink(Sink[EncryptedGatewaySinkConfig, SinkReport]):
        def __post_init__(self) -> None:
            self.channel = get_channel(self.config.run_id)

        def write_record_async(self, record_envelope, write_callback) -> None:
            try:
                for mcp in record_to_mcp_dicts(record_envelope.record):
                    self.channel.add(mcp)
            except (RunCancelled, SpoolFull):
                raise                                    # stop the pipeline, not just this record
            except Exception as e:
                self.report.report_failure({"error": str(e)})
                if write_callback:
                    write_callback.on_failure(record_envelope, e, {"error": str(e)})
                return
            self.report.report_record_written(record_envelope)
            if write_callback:
                write_callback.on_success(record_envelope, {})

        def flush(self) -> None:
            self.channel.flush()

        def close(self) -> None:
            super().close()
            self.channel.flush()

except ImportError:  # pragma: no cover - acryl-datahub not installed (control-plane image)
    EncryptedGatewaySink = None  # type: ignore[assignment,misc]
