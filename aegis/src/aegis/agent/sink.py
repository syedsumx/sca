"""``aegis-encrypted`` ingestion sink: buffers MCPs and ships them to the gateway encrypted.

Registered through the ``datahub.ingestion.sink.plugins`` entry point, so recipes run
by the agent use::

    sink:
      type: aegis-encrypted
      config:
        run_id: run_123   # injected by the agent worker

The worker registers a :class:`RunChannel` for the run before starting the pipeline;
the sink looks it up by ``run_id``. This keeps key material out of the recipe.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Optional

from aegis.agent.client import GatewayClient, with_backoff

_channels: dict[str, "RunChannel"] = {}
_channels_lock = threading.Lock()


class RunChannel:
    def __init__(self, client: GatewayClient, run_id: str, max_items: int = 500, max_bytes: int = 1 << 20):
        self.client = client
        self.run_id = run_id
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.seq = -1
        self.sent = 0
        self._buffer: list[dict] = []
        self._size = 0
        self._lock = threading.Lock()

    def add(self, mcp: dict) -> None:
        size = len(json.dumps(mcp))
        with self._lock:
            if self._buffer and (len(self._buffer) >= self.max_items or self._size + size > self.max_bytes):
                self._flush_locked()
            self._buffer.append(mcp)
            self._size += size

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        seq = self.seq + 1
        batch = self._buffer
        with_backoff(lambda: self.client.upload_batch(self.run_id, seq, batch))
        self.seq = seq
        self.sent += len(batch)
        self._buffer, self._size = [], 0


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
                               "the aegis-encrypted sink can only be used by the Aegis agent worker")
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
