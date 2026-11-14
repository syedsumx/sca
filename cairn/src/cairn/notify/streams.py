"""Events out to customer pipelines: a notification route can send every matching Cairn event to a
Kafka topic, an Amazon EventBridge bus or a Google Cloud Pub/Sub topic, besides webhooks.

Each delivery is one JSON event (``event()``): its id, type, title, body, entity, link, who caused it
and its details. Kafka messages are keyed by the entity (so one asset's events stay in order) and carry
the type as a header; EventBridge entries use ``Source: cairn`` and the type as ``DetailType``; Pub/Sub
messages carry the type and entity as attributes.

Connections come from the environment: ``CAIRN_EVENTS_KAFKA_BOOTSTRAP`` (and, if needed,
``CAIRN_EVENTS_KAFKA_SECURITY_PROTOCOL``, ``…_SASL_MECHANISM``, ``…_SASL_USERNAME``, ``…_SASL_PASSWORD``);
EventBridge uses the AWS credential chain (``AWS_REGION``); Pub/Sub uses the service's Google identity
(metadata server) or ``GOOGLE_OAUTH_ACCESS_TOKEN``.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx


def event(row: dict) -> dict:
    payload = row.get("payload") or {}
    return {"id": row["id"], "type": row["type"], "title": row["title"], "body": row["body"],
            "entity_urn": row.get("entity_urn"), "url": payload.get("url"), "actor": payload.get("actor"),
            "details": {k: v for k, v in payload.items() if k not in ("url", "actor", "actions")},
            "time": datetime.fromtimestamp((row.get("created_at_ms") or time.time() * 1000) / 1000,
                                           tz=timezone.utc).isoformat(), "source": "cairn"}


class KafkaSender:
    def __init__(self, producer=None, bootstrap: Optional[str] = None):
        self._producer = producer
        self.bootstrap = bootstrap or os.environ.get("CAIRN_EVENTS_KAFKA_BOOTSTRAP")

    def producer(self):
        if self._producer is None:
            if not self.bootstrap:
                raise RuntimeError("CAIRN_EVENTS_KAFKA_BOOTSTRAP is not set")
            from confluent_kafka import Producer

            conf = {"bootstrap.servers": self.bootstrap, "client.id": "cairn-events", "acks": "all",
                    "enable.idempotence": True}
            for key, env in (("security.protocol", "SECURITY_PROTOCOL"), ("sasl.mechanism", "SASL_MECHANISM"),
                             ("sasl.username", "SASL_USERNAME"), ("sasl.password", "SASL_PASSWORD")):
                if os.environ.get(f"CAIRN_EVENTS_KAFKA_{env}"):
                    conf[key] = os.environ[f"CAIRN_EVENTS_KAFKA_{env}"]
            self._producer = Producer(conf)
        return self._producer

    def send(self, row: dict, hub=None) -> str:
        topic = row["target"]
        errors: list[Any] = []
        p = self.producer()
        p.produce(topic, key=(row.get("entity_urn") or row["type"]).encode(), value=json.dumps(event(row)).encode(),
                  headers=[("cairn-event-type", row["type"].encode())],
                  on_delivery=lambda err, msg: errors.append(err) if err else None)
        left = p.flush(15)
        if errors or left:
            raise RuntimeError(f"kafka delivery to {topic} failed: {errors[0] if errors else 'timed out'}")
        return f"kafka:{topic}"


class EventBridgeSender:
    def __init__(self, client=None):
        self._client = client

    def send(self, row: dict, hub=None) -> str:
        if self._client is None:
            import boto3

            self._client = boto3.client("events")
        out = self._client.put_events(Entries=[{
            "Source": "cairn", "DetailType": row["type"], "Detail": json.dumps(event(row)),
            "EventBusName": row["target"], **({"Resources": [row["entity_urn"]]} if row.get("entity_urn") else {})}])
        if out.get("FailedEntryCount"):
            entry = (out.get("Entries") or [{}])[0]
            raise RuntimeError(f"eventbridge refused the event: {entry.get('ErrorCode')} {entry.get('ErrorMessage')}")
        return f"eventbridge:{(out.get('Entries') or [{}])[0].get('EventId', '')}"


class PubSubSender:
    METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

    def __init__(self, http: Optional[httpx.Client] = None):
        self.http = http or httpx.Client(timeout=15)
        self._token: tuple[float, str] = (0.0, "")

    def _bearer(self) -> str:
        if os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN"):
            return os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"]
        if self._token[0] > time.time() + 60:
            return self._token[1]
        r = self.http.get(self.METADATA, headers={"Metadata-Flavor": "Google"})
        r.raise_for_status()
        body = r.json()
        self._token = (time.time() + int(body.get("expires_in", 300)), body["access_token"])
        return self._token[1]

    def send(self, row: dict, hub=None) -> str:
        topic = row["target"]
        if not topic.startswith("projects/") or "/topics/" not in topic:
            raise RuntimeError("a Pub/Sub target is projects/<project>/topics/<topic>")
        msg = {"data": base64.b64encode(json.dumps(event(row)).encode()).decode(),
               "attributes": {"type": row["type"], **({"entity_urn": row["entity_urn"]} if row.get("entity_urn") else {})}}
        r = self.http.post(f"https://pubsub.googleapis.com/v1/{topic}:publish", json={"messages": [msg]},
                           headers={"Authorization": f"Bearer {self._bearer()}"})
        if r.status_code >= 300:
            raise RuntimeError(f"pubsub returned {r.status_code}: {r.text[:200]}")
        return f"pubsub:{(r.json().get('messageIds') or [''])[0]}"
