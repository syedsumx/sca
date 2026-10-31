"""Crash safety: the store is killed (SIGKILL, no shutdown) while clients write. After a restart every
write that was acknowledged is present and intact, nothing half-written is visible, and the store's
own check finds no damage."""

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from urllib3.exceptions import ProtocolError

from conftest import ADMIN


def test_kill_during_writes_loses_nothing_acknowledged(fresh_store):
    st = fresh_store.start()
    s3 = st.client()
    bucket = "crash-" + uuid.uuid4().hex[:8]
    s3.create_bucket(Bucket=bucket)
    acked: dict[str, str] = {}
    lock = threading.Lock()
    stop = threading.Event()

    def writer(n):
        c = st.client()
        i = 0
        while not stop.is_set():
            key = f"w{n}/obj-{i}"
            body = os.urandom(64 << 10) + key.encode()
            try:
                c.put_object(Bucket=bucket, Key=key, Body=body)
            except (ClientError, BotoCoreError, ProtocolError, OSError):
                return  # the store went away mid-request: this write was never acknowledged
            with lock:
                acked[key] = hashlib.md5(body).hexdigest()
            i += 1

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 60
    while len(acked) < 150 and time.monotonic() < deadline:
        time.sleep(0.01)
    st.stop(kill=True)                       # SIGKILL mid-write
    stop.set()
    for t in threads:
        t.join(10)
    assert len(acked) >= 150

    st.start()                                # restart runs recovery
    s3 = st.client()
    for key, md5 in acked.items():
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert hashlib.md5(body).hexdigest() == md5, key
    listed = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            listed.add(o["Key"])
            # anything listed but never acknowledged must still be whole: it ends with its own key
            if o["Key"] not in acked:
                assert s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().endswith(o["Key"].encode())
    assert set(acked) <= listed

    st.stop()
    out = subprocess.run([st.binary, "fsck", "-data", st.data], capture_output=True, text=True,
                         env={**os.environ, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1]})
    report = json.loads(out.stdout)
    assert out.returncode == 0 and report["objects_with_missing_blobs"] == 0, out.stdout
    assert report["blobs_referenced"] == len(listed)
