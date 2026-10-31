"""Event notifications: configuration, the events written for each kind of change, filters, signed
webhook delivery, retries while the target is down, and no event lost when the store crashes."""

import hashlib
import hmac
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from botocore.exceptions import ClientError

SECRET = "event-hook-secret-0123456789"


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


class Hook:
    """A webhook receiver that records events (and checks their signatures)."""

    def __init__(self, port=0):
        self.events, self.bad = [], 0
        hook = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                want = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
                if self.headers.get("X-Cairn-Signature") != want:
                    hook.bad += 1
                hook.events.extend(json.loads(body)["Records"])
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", port), H)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def names(self):
        return [(e["eventName"], e["s3"]["object"]["key"]) for e in self.events]

    def wait(self, n, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.events) < n:
            time.sleep(0.1)
        return len(self.events) >= n


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def targets_file(tmp_path, port):
    p = tmp_path / f"targets-{port}.json"
    p.write_text(json.dumps([{"type": "webhook", "id": "hook", "url": f"http://127.0.0.1:{port}/events", "secret": SECRET}]))
    return str(p)


def notify(s3, bucket, events, prefix="", suffix=""):
    rule = {"Id": "r1", "QueueArn": "arn:cairn:store:::hook", "Events": events}
    rules = [{"Name": "prefix", "Value": prefix}] if prefix else []
    rules += [{"Name": "suffix", "Value": suffix}] if suffix else []
    if rules:
        rule["Filter"] = {"Key": {"FilterRules": rules}}
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration={"QueueConfigurations": [rule]})


def test_events_for_every_kind_of_change(custom_store, tmp_path):
    hook = Hook()
    st = custom_store({"CAIRN_STORE_EVENT_TARGETS_FILE": targets_file(tmp_path, hook.port)})
    s3 = st.client()
    s3.create_bucket(Bucket="evs")
    notify(s3, "evs", ["s3:ObjectCreated:*", "s3:ObjectRemoved:*", "s3:ObjectTagging:*"])
    got = s3.get_bucket_notification_configuration(Bucket="evs")["QueueConfigurations"][0]
    assert got["QueueArn"] == "arn:cairn:store:::hook" and got["Id"] == "r1"
    s3.put_object(Bucket="evs", Key="a.csv", Body=b"abc")
    s3.copy_object(Bucket="evs", Key="b.csv", CopySource={"Bucket": "evs", "Key": "a.csv"})
    up = s3.create_multipart_upload(Bucket="evs", Key="m.bin")
    e = s3.upload_part(Bucket="evs", Key="m.bin", UploadId=up["UploadId"], PartNumber=1, Body=b"m")["ETag"]
    s3.complete_multipart_upload(Bucket="evs", Key="m.bin", UploadId=up["UploadId"],
                                 MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": e}]})
    s3.put_object_tagging(Bucket="evs", Key="a.csv", Tagging={"TagSet": [{"Key": "k", "Value": "v"}]})
    s3.delete_object(Bucket="evs", Key="b.csv")
    s3.delete_object(Bucket="evs", Key="never-existed")  # nothing removed: no event
    assert hook.wait(5)
    time.sleep(0.5)
    assert hook.names() == [("ObjectCreated:Put", "a.csv"), ("ObjectCreated:Copy", "b.csv"),
                            ("ObjectCreated:CompleteMultipartUpload", "m.bin"), ("ObjectTagging:Put", "a.csv"),
                            ("ObjectRemoved:Delete", "b.csv")]
    first = hook.events[0]
    assert first["s3"]["bucket"]["name"] == "evs" and first["s3"]["object"]["size"] == 3
    assert first["s3"]["object"]["eTag"] == hashlib.md5(b"abc").hexdigest() and first["s3"]["configurationId"] == "r1"
    assert first["userIdentity"]["principalId"] and first["requestParameters"]["sourceIPAddress"] == "127.0.0.1"
    seqs = [int(e["s3"]["object"]["sequencer"], 16) for e in hook.events]
    assert seqs == sorted(seqs)
    assert hook.bad == 0  # every body was signed with the target's secret
    hook.stop()


def test_filters_and_versioned_deletes(custom_store, tmp_path):
    hook = Hook()
    st = custom_store({"CAIRN_STORE_EVENT_TARGETS_FILE": targets_file(tmp_path, hook.port)})
    s3 = st.client()
    s3.create_bucket(Bucket="flt")
    s3.put_bucket_versioning(Bucket="flt", VersioningConfiguration={"Status": "Enabled"})
    notify(s3, "flt", ["s3:ObjectCreated:Put", "s3:ObjectRemoved:DeleteMarkerCreated"], prefix="raw/", suffix=".parquet")
    s3.put_object(Bucket="flt", Key="raw/a.parquet", Body=b"x")
    s3.put_object(Bucket="flt", Key="raw/a.csv", Body=b"x")  # suffix does not match
    s3.put_object(Bucket="flt", Key="other/a.parquet", Body=b"x")  # prefix does not match
    s3.delete_object(Bucket="flt", Key="raw/a.parquet")
    assert hook.wait(2)
    time.sleep(0.5)
    assert hook.names() == [("ObjectCreated:Put", "raw/a.parquet"), ("ObjectRemoved:DeleteMarkerCreated", "raw/a.parquet")]
    assert hook.events[0]["s3"]["object"]["versionId"]
    hook.stop()


def test_lifecycle_expirations_are_events(custom_store, tmp_path):
    hook = Hook()
    st = custom_store({"CAIRN_STORE_EVENT_TARGETS_FILE": targets_file(tmp_path, hook.port),
                       "CAIRN_STORE_LIFECYCLE_INTERVAL": "1", "CAIRN_STORE_LIFECYCLE_DAY_SECONDS": "1"})
    s3 = st.client()
    s3.create_bucket(Bucket="lcy")
    notify(s3, "lcy", ["s3:LifecycleExpiration:*"])
    s3.put_object(Bucket="lcy", Key="tmp/x", Body=b"x")
    s3.put_bucket_lifecycle_configuration(Bucket="lcy", LifecycleConfiguration={"Rules": [
        {"ID": "e", "Status": "Enabled", "Filter": {"Prefix": "tmp/"}, "Expiration": {"Days": 1}}]})
    assert hook.wait(1)
    assert hook.names() == [("LifecycleExpiration:Delete", "tmp/x")]
    hook.stop()


def test_invalid_notification_configurations(custom_store, tmp_path):
    st = custom_store({"CAIRN_STORE_EVENT_TARGETS_FILE": targets_file(tmp_path, free_port())})
    s3 = st.client()
    s3.create_bucket(Bucket="bad")
    for cfg in (
        {"QueueConfigurations": [{"QueueArn": "arn:cairn:store:::nope", "Events": ["s3:ObjectCreated:*"]}]},
        {"QueueConfigurations": [{"QueueArn": "arn:cairn:store:::hook", "Events": ["s3:ObjectRestore:Completed"]}]},
    ):
        with pytest.raises(ClientError) as ei:
            s3.put_bucket_notification_configuration(Bucket="bad", NotificationConfiguration=cfg)
        assert code(ei) == "InvalidArgument"
    s3.put_bucket_notification_configuration(Bucket="bad", NotificationConfiguration={})
    assert "QueueConfigurations" not in s3.get_bucket_notification_configuration(Bucket="bad")


def test_retries_and_no_loss_across_a_crash(fresh_store, tmp_path):
    port = free_port()
    fresh_store.extra_env["CAIRN_STORE_EVENT_TARGETS_FILE"] = targets_file(tmp_path, port)
    st = fresh_store.start()
    s3 = st.client()
    s3.create_bucket(Bucket="dur")
    notify(s3, "dur", ["s3:ObjectCreated:*"])
    for i in range(5):
        s3.put_object(Bucket="dur", Key=f"k{i}", Body=b"x")  # the target is down: events wait in the outbox
    time.sleep(1.5)
    st.stop(kill=True)  # crash with undelivered events
    st.start()
    hook = Hook(port)  # the target comes back
    assert hook.wait(5, timeout=30)
    assert [k for _, k in hook.names()] == [f"k{i}" for i in range(5)]  # in order, none lost
    hook.stop()
