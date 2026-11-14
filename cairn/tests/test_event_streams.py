"""Events out: routes to Kafka, EventBridge and Pub/Sub deliver one JSON event per notification,
with the type and entity where each system expects them; bad targets are refused up front."""

import base64
import json

import httpx
import pytest

from cairn.notify.hub import Event, NotificationHub, Route
from cairn.notify.streams import EventBridgeSender, KafkaSender, PubSubSender

DS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,a.b.orders,PROD)"


class Producer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def produce(self, topic, key, value, headers, on_delivery):
        self.sent.append((topic, key, json.loads(value), headers))
        self._cb = on_delivery

    def flush(self, timeout):
        self._cb("broker down" if self.fail else None, None)
        return 0


class Events:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):
        self.entries += Entries
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "ev-1"}]}


def test_routes_deliver_to_streams(engine, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "gtok")
    published = []

    def pubsub(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer gtok"
        published.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"messageIds": ["m1"]})

    hub = NotificationHub(engine, "https://cairn", "s")
    producer, events = Producer(), Events()
    hub.channels.update(kafka=KafkaSender(producer), eventbridge=EventBridgeSender(events),
                        pubsub=PubSubSender(httpx.Client(transport=httpx.MockTransport(pubsub))))
    hub.put_routes([Route(id="to-kafka", channel="kafka", target="cairn.events", event_types=["quality.incident.raised"]),
                    Route(id="to-eb", channel="eventbridge", target="data-platform"),
                    Route(id="to-pubsub", channel="pubsub", target="projects/acme-data/topics/cairn-events")])
    hub.publish(Event(type="quality.incident.raised", title="Orders volume dropped", body="41% fewer rows",
                      actor="urn:li:corpuser:cairn-quality", entity_urn=DS, payload={"monitor_id": "m1"}))
    hub.publish(Event(type="access.granted", title="Access granted", body="7 days", recipients=[]))
    assert hub.dispatch_pending()["sent"] == 5
    [(topic, key, value, headers)] = producer.sent                      # only the incident matches the Kafka route
    assert topic == "cairn.events" and key == DS.encode() and headers == [("cairn-event-type", b"quality.incident.raised")]
    assert value["type"] == "quality.incident.raised" and value["details"] == {"monitor_id": "m1"}
    assert value["actor"] == "urn:li:corpuser:cairn-quality" and value["url"].endswith(DS) and value["source"] == "cairn"
    assert [e["DetailType"] for e in events.entries] == ["quality.incident.raised", "access.granted"]
    assert events.entries[0]["EventBusName"] == "data-platform" and events.entries[0]["Resources"] == [DS]
    path, body = published[0]
    assert path == "/v1/projects/acme-data/topics/cairn-events:publish"
    msg = body["messages"][0]
    assert msg["attributes"] == {"type": "quality.incident.raised", "entity_urn": DS}
    assert json.loads(base64.b64decode(msg["data"]))["title"] == "Orders volume dropped"


def test_failures_retry_and_bad_targets(engine):
    hub = NotificationHub(engine, "https://cairn", "s")
    hub.channels["kafka"] = KafkaSender(Producer(fail=True))
    hub.put_routes([Route(id="to-kafka", channel="kafka", target="cairn.events")])
    hub.publish(Event(type="access.granted", title="t", body="b"))
    assert hub.dispatch_pending()["failed"] == 1 and hub.dispatch_pending()["failed"] == 1   # still pending
    for bad in (Route(id="bad-x", channel="kafka", target="bad topic!"),
                Route(id="bad-y", channel="pubsub", target="cairn-events"),
                Route(id="bad-z", channel="eventbridge", target="no spaces allowed")):
        with pytest.raises(ValueError):
            hub.put_routes([bad])
