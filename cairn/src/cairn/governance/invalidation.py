"""MCL-driven cache invalidation for every process that hosts a PDP.

Cairn Core publishes a MetadataChangeLog event for each aspect write. When one of the aspects the
PDP reads changes (ownership, tags, terms, schema labels, domains, group membership, ...), the
entity's cached attributes are dropped at once, and with them every cached decision that used them
(decision cache keys carry the resolver's per-URN version). A changed native policy triggers an
immediate re-import instead of waiting for the periodic sync.

Each process reads the topic with its own consumer group from the latest offset: every replica must
see every event, and history is irrelevant to a cache that starts empty.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from typing import Any, Callable, Optional

from cairn.governance.attributes import AttributeResolver

logger = logging.getLogger(__name__)

MCL_TOPIC = "MetadataChangeLog_Versioned_v1"
# Aspects AttributeResolver reads: a change to one of them changes subject or resource attributes.
ATTRIBUTE_ASPECTS = frozenset({
    "ownership", "globalTags", "glossaryTerms", "editableSchemaMetadata", "schemaMetadata", "domains",
    "container", "dataPlatformInstance", "structuredProperties", "dataProducts",
    "corpUserInfo", "groupMembership", "nativeGroupMembership", "roleMembership",
})
# Membership held on the other side of a relationship: the affected entities are listed in the aspect.
MEMBERSHIP_ASPECTS = {"dataProductProperties": ("assets", "destinationUrn")}
POLICY_ASPECT = "dataHubPolicyInfo"


class MclInvalidator:
    def __init__(self, resolver: AttributeResolver, on_policy_change: Callable[[], Any],
                 on_change: Optional[Callable[[str], Any]] = None):
        self.resolver = resolver
        self.on_policy_change = on_policy_change
        self.on_change = on_change
        self.handled = 0

    def handle(self, event: dict) -> bool:
        """Apply one decoded MCL event; True when it changed what the PDP knows."""
        aspect = event.get("aspectName")
        urn = event.get("entityUrn")
        if not aspect or not urn:
            return False
        if aspect == POLICY_ASPECT:
            self.on_policy_change()
        elif aspect in ATTRIBUTE_ASPECTS:
            self.resolver.invalidate(urn)
        elif aspect in MEMBERSHIP_ASPECTS:
            self.resolver.invalidate(urn)
            for member in _members(event, *MEMBERSHIP_ASPECTS[aspect]):
                self.resolver.invalidate(member)
        else:
            return False
        self.handled += 1
        if self.on_change:
            self.on_change(urn)
        return True


def _members(event: dict, list_key: str, urn_key: str) -> set[str]:
    """Entities listed in the new and the previous aspect value (a removal must invalidate too)."""
    import json

    out: set[str] = set()
    for which in ("aspect", "previousAspectValue"):
        blob = event.get(which) or {}
        raw = blob.get("value") if isinstance(blob, dict) else None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        try:
            value = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            continue
        for item in value.get(list_key) or []:
            if isinstance(item, dict) and item.get(urn_key):
                out.add(item[urn_key])
    return out


def start_consumer(invalidator: MclInvalidator, bootstrap: str, schema_registry_url: str,
                   stop: threading.Event, topic: str = MCL_TOPIC) -> Optional[threading.Thread]:
    """Consume MCL events in a daemon thread. Returns None when Kafka support is unavailable."""
    try:
        from confluent_kafka import Consumer
        from confluent_kafka.schema_registry import SchemaRegistryClient
        from confluent_kafka.schema_registry.avro import AvroDeserializer
        from confluent_kafka.serialization import MessageField, SerializationContext
    except ImportError:
        logger.warning("confluent-kafka not installed: MCL cache invalidation disabled")
        return None

    group = f"cairn-pdp-cache-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

    def run() -> None:
        consumer = None
        while not stop.is_set():
            try:
                if consumer is None:
                    deserialize = AvroDeserializer(SchemaRegistryClient({"url": schema_registry_url.rstrip("/")}))
                    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": group,
                                         "auto.offset.reset": "latest", "enable.auto.commit": False,
                                         "session.timeout.ms": 30000})
                    consumer.subscribe([topic])
                    logger.info("MCL cache invalidation: consuming %s as %s", topic, group)
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    logger.debug("MCL consumer: %s", msg.error())
                    continue
                event = deserialize(msg.value(), SerializationContext(msg.topic(), MessageField.VALUE))
                if isinstance(event, dict):
                    invalidator.handle(event)
            except Exception as exc:   # broker or schema registry not reachable yet
                logger.warning("MCL cache invalidation: %s; reconnecting in 5 s", exc)
                if consumer is not None:
                    try:
                        consumer.close()
                    except Exception:
                        pass
                    consumer = None
                stop.wait(5)
        if consumer is not None:
            consumer.close()

    t = threading.Thread(target=run, daemon=True, name="mcl_invalidator")
    t.start()
    return t
