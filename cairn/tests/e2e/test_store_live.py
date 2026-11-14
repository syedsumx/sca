"""Cairn Store in the running stack (Compose service cairn-store)."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1", reason="set CAIRN_E2E=1 to run against a live stack")


def test_cairn_store_serves_s3_in_the_stack():
    """Cairn Store runs with the stack: healthy, refuses anonymous requests, and round-trips an object
    with the stack's credentials, including a conditional create."""
    boto3 = pytest.importorskip("boto3")
    from botocore.config import Config
    from botocore.exceptions import ClientError

    endpoint = os.environ.get("CAIRN_STORE_URL", "http://localhost:8333")
    assert httpx.get(f"{endpoint}/-/ready", timeout=10).status_code == 200
    assert httpx.get(f"{endpoint}/lakehouse", timeout=10).status_code == 403
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                      aws_access_key_id=os.environ.get("CAIRN_STORE_ACCESS_KEY", "cairn-store"),
                      aws_secret_access_key=os.environ.get("CAIRN_STORE_SECRET_KEY", "cairn-store-dev-secret"),
                      config=Config(s3={"addressing_style": "path"}))
    assert "lakehouse" in [b["Name"] for b in s3.list_buckets()["Buckets"]]
    key = f"e2e/{uuid.uuid4().hex}.json"
    s3.put_object(Bucket="lakehouse", Key=key, Body=b'{"ok": true}', IfNoneMatch="*")
    with pytest.raises(ClientError):
        s3.put_object(Bucket="lakehouse", Key=key, Body=b"{}", IfNoneMatch="*")
    assert s3.get_object(Bucket="lakehouse", Key=key)["Body"].read() == b'{"ok": true}'
    s3.delete_object(Bucket="lakehouse", Key=key)
