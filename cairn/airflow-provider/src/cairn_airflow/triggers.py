"""Run a DAG when its input tables change, wherever they were written (Trino, Spark, dbt, Airflow).

Cairn records every dataset a pipeline finishes writing; this trigger watches that feed for some
datasets (URNs, or lakehouse tables as ``catalog.schema.table``) and fires an Airflow asset event when
any changes. Use it with Airflow 3's event-driven scheduling::

    from airflow.sdk import DAG, Asset, AssetWatcher
    from cairn_airflow.triggers import CairnDatasetChangedTrigger

    orders = Asset("lakehouse.analytics.orders", watchers=[AssetWatcher(
        name="orders_changed", trigger=CairnDatasetChangedTrigger(["lakehouse.analytics.orders"]))])

    with DAG("refresh_reports", schedule=[orders]):
        ...

The connection's Cairn user must be allowed to see the datasets. The trigger starts watching from the
moment it starts (it does not replay older changes), and polls every ``poll_interval`` seconds.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Optional, Sequence

from airflow.triggers.base import BaseEventTrigger, TriggerEvent

from cairn_airflow.hooks import CairnHook


class CairnDatasetChangedTrigger(BaseEventTrigger):
    def __init__(self, datasets: Sequence[str], cairn_conn_id: str = "cairn_default", poll_interval: float = 60.0,
                 cursor: Optional[int] = None):
        super().__init__()
        if not datasets:
            raise ValueError("name the datasets to watch")
        self.datasets, self.cairn_conn_id = list(datasets), cairn_conn_id
        self.poll_interval, self.cursor = max(5.0, float(poll_interval)), cursor

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return ("cairn_airflow.triggers.CairnDatasetChangedTrigger",
                {"datasets": self.datasets, "cairn_conn_id": self.cairn_conn_id,
                 "poll_interval": self.poll_interval, "cursor": self.cursor})

    def _poll(self, cursor: Optional[int]) -> dict:
        return CairnHook(self.cairn_conn_id).dataset_changes(self.datasets, cursor)

    async def run(self) -> AsyncIterator[TriggerEvent]:
        cursor = self.cursor
        while True:
            try:
                got = await asyncio.to_thread(self._poll, cursor)
            except Exception as e:  # noqa: BLE001 - Cairn unreachable for a moment: try again, never fire
                self.log.warning("Cairn dataset changes unavailable: %s", e)
                await asyncio.sleep(self.poll_interval)
                continue
            if cursor is None:
                cursor = got["cursor"]  # watch from now
            elif got["changes"]:
                cursor = got["cursor"]
                yield TriggerEvent({"changes": got["changes"], "cursor": cursor,
                                    "datasets": sorted({c["urn"] for c in got["changes"]})})
            await asyncio.sleep(self.poll_interval)
