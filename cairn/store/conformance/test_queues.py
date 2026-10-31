"""Message queues under /-/queues/v1: topics, publishing (single and batch), consumer groups that read in order
and commit offsets (at least once), long polling, retention by count and age, durability across restarts,
access rules and the audit trail."""

import json
import threading
import time

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, READER, REGION, SCOPED


def call(st, method, path, body=None, key=ADMIN, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    req = AWSRequest(method=method, url=f"{st.endpoint}/-/queues/v1/{path}", data=data, headers=headers or {})
    S3SigV4Auth(Credentials(*key), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=30)
    return r.status_code, (r.json() if r.content else None)


@pytest.fixture
def q(custom_store):
    return custom_store({})


def test_publish_consume_commit(q):
    assert call(q, "PUT", "topics/orders", {"retention_seconds": 0})[0] == 200
    hdrs = {"content-type": "application/json", "x-cairn-key": "c-1"}
    code, r = call(q, "POST", "topics/orders/messages", raw=b'{"id": 1}', headers=hdrs)
    assert code == 201 and r["seq"] == 1
    code, r = call(q, "POST", "topics/orders/messages?batch", {"messages": [{"value": f"order {i}", "key": f"c-{i}"} for i in range(2, 6)]})
    assert code == 201 and (r["first"], r["last"]) == (2, 5)
    call(q, "POST", "topics/orders/messages?batch", {"messages": [{"value_base64": "AAEC"}]})
    # A group reads from the start, in order, and only moves on when it commits.
    _, r = call(q, "GET", "topics/orders/messages?group=billing&max=3")
    assert [m["seq"] for m in r["messages"]] == [1, 2, 3]
    first = r["messages"][0]
    assert first["value"] == '{"id": 1}' and first["key"] == "c-1" and first["content_type"] == "application/json"
    _, again = call(q, "GET", "topics/orders/messages?group=billing&max=3")
    assert [m["seq"] for m in again["messages"]] == [1, 2, 3]  # not committed: delivered again
    assert call(q, "POST", "topics/orders/groups/billing", {"seq": 3})[1]["offset"] == 3
    _, r = call(q, "GET", "topics/orders/messages?group=billing&max=10")
    assert [m["seq"] for m in r["messages"]] == [4, 5, 6] and r["messages"][2]["value_base64"] == "AAEC"
    # Commits never move back, nor past the last message; other groups are independent.
    assert call(q, "POST", "topics/orders/groups/billing", {"seq": 1})[1]["offset"] == 3
    assert call(q, "POST", "topics/orders/groups/billing", {"seq": 99})[0] == 400
    _, r = call(q, "GET", "topics/orders/messages?group=audit&max=2")
    assert [m["seq"] for m in r["messages"]] == [1, 2]
    _, r = call(q, "GET", "topics/orders/messages?after=4")
    assert [m["seq"] for m in r["messages"]] == [5, 6]
    _, t = call(q, "GET", "topics/orders")
    assert (t["first"], t["next"], t["messages"]) == (1, 7, 6)
    assert t["groups"]["billing"] == {"offset": 3, "lag": 3}
    # Durable: a restart keeps messages, sequence numbers and offsets.
    q.stop()
    q.start()
    _, r = call(q, "GET", "topics/orders/messages?group=billing")
    assert [m["seq"] for m in r["messages"]] == [4, 5, 6]
    assert call(q, "POST", "topics/orders/messages", raw=b"after restart")[1]["seq"] == 7


def test_long_poll(q):
    call(q, "PUT", "topics/events", {})
    got = {}

    def consumer():
        t = time.time()
        got["r"] = call(q, "GET", "topics/events/messages?group=g&wait=10")[1]
        got["took"] = time.time() - t

    th = threading.Thread(target=consumer)
    th.start()
    time.sleep(0.5)
    call(q, "POST", "topics/events/messages", raw=b"wake up")
    th.join()
    assert [m["value"] for m in got["r"]["messages"]] == ["wake up"] and got["took"] < 5
    # Nothing new: the poll ends after the wait with no messages.
    t = time.time()
    call(q, "POST", "topics/events/groups/g", {"seq": 1})
    _, r = call(q, "GET", "topics/events/messages?group=g&wait=1")
    assert r["messages"] == [] and 0.9 < time.time() - t < 5


def test_retention(custom_store):
    q = custom_store({"CAIRN_STORE_QUEUE_RETENTION_INTERVAL": "1"})
    call(q, "PUT", "topics/capped", {"max_messages": 5})
    call(q, "PUT", "topics/aging", {"retention_seconds": 1})
    call(q, "POST", "topics/capped/messages?batch", {"messages": [{"value": str(i)} for i in range(12)]})
    call(q, "POST", "topics/aging/messages", raw=b"old")
    deadline = time.time() + 15
    while time.time() < deadline:
        capped, aging = call(q, "GET", "topics/capped")[1], call(q, "GET", "topics/aging")[1]
        if capped["messages"] == 5 and aging["messages"] == 0:
            break
        time.sleep(0.3)
    assert (capped["first"], capped["messages"]) == (8, 5) and aging["messages"] == 0
    _, r = call(q, "GET", "topics/capped/messages?group=late")  # a new group starts at the oldest kept
    assert [m["seq"] for m in r["messages"]] == [8, 9, 10, 11, 12]


def test_access_and_audit(q):
    assert call(q, "PUT", "topics/lake-feed", {})[0] == 200
    assert call(q, "PUT", "topics/private", {})[0] == 200
    assert call(q, "PUT", "topics/Bad_Name", {})[0] == 400
    assert call(q, "PUT", "topics/lake-x", {}, key=SCOPED)[0] == 403  # only admin keys manage topics
    # Bucket patterns apply to topics: the lake-* key may use lake-feed, not private.
    assert call(q, "POST", "topics/lake-feed/messages", raw=b"x", key=SCOPED)[0] == 201
    assert call(q, "POST", "topics/private/messages", raw=b"x", key=SCOPED)[0] == 403
    assert call(q, "GET", "topics/lake-feed/messages?group=s", key=SCOPED)[0] == 200
    # Read-only keys consume and commit, never publish.
    assert call(q, "POST", "topics/private/messages", raw=b"x", key=READER)[0] == 403
    assert call(q, "GET", "topics/private/messages?after=0", key=READER)[0] == 200
    assert [t["name"] for t in call(q, "GET", "topics", key=SCOPED)[1]["topics"]] == ["lake-feed"]
    assert call(q, "POST", "topics/nope/messages", raw=b"x")[0] == 404
    assert call(q, "GET", "topics/lake-feed/messages")[0] == 400  # a group or a position
    assert call(q, "POST", "topics/lake-feed/messages", raw=b"x" * (1 << 20 + 1))[0] in (400, 413)
    unsigned = requests.post(f"{q.endpoint}/-/queues/v1/topics/lake-feed/messages", data=b"x", timeout=5)
    assert unsigned.status_code == 403
    assert call(q, "DELETE", "topics/private")[0] == 204
    assert call(q, "GET", "topics/private")[0] == 404
    from test_keys import admin

    actions = [e["action"] for e in admin(q, "GET", "audit?after=0")[1]["entries"] if e["action"].startswith("queue.")]
    assert actions == ["queue.create", "queue.create", "queue.delete"]
