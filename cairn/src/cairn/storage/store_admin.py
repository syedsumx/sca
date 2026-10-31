"""A client for Cairn Store's admin API (``/-/admin/v1``), signing requests with SigV4.

The control plane holds the store's admin key; users never see it. Admin calls are made on their
behalf after Cairn's own authorization (see ``store_api``)."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import httpx

from cairn.common.audit import verify_entries


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, body: bytes, access_key: str, secret_key: str, region: str = "us-east-1",
                  now: Optional[dt.datetime] = None) -> dict[str, str]:
    """AWS Signature Version 4 headers (service "s3", signed payload) for one request."""
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    u = urlsplit(url)
    host = u.netloc
    payload = hashlib.sha256(body).hexdigest()
    pairs = sorted((quote(k, safe="-_.~"), quote(v, safe="-_.~"))
                   for k, _, v in (p.partition("=") for p in u.query.split("&") if p))
    canonical_query = "&".join(f"{k}={v}" for k, v in pairs)
    headers = {"host": host, "x-amz-content-sha256": payload, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, quote(u.path or "/", safe="/-_.~"), canonical_query,
                           "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload])
    scope = f"{date}/{region}/s3/aws4_request"
    to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    k = _sign(_sign(_sign(_sign(f"AWS4{secret_key}".encode(), date), region), "s3"), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"x-amz-content-sha256": payload, "x-amz-date": amz_date,
            "authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"}


class StoreAdminError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class StoreAdmin:
    def __init__(self, url: str, access_key: str, secret_key: str, region: str = "us-east-1", timeout: float = 10.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self.url, self.ak, self.sk, self.region = url.rstrip("/"), access_key, secret_key, region
        self.http = httpx.Client(timeout=timeout, transport=transport)

    def _call(self, method: str, path: str, body: Any = None, query: str = "") -> Any:
        url = f"{self.url}/-/admin/v1/{path}" + (f"?{query}" if query else "")
        data = json.dumps(body).encode() if body is not None else b""
        headers = sigv4_headers(method, url, data, self.ak, self.sk, self.region)
        if data:
            headers["content-type"] = "application/json"
        try:
            r = self.http.request(method, url, content=data, headers=headers)
        except httpx.HTTPError as e:
            raise StoreAdminError(502, f"Cairn Store is not reachable: {e}") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise StoreAdminError(r.status_code, msg)
        if "ndjson" in r.headers.get("content-type", ""):
            return [json.loads(line) for line in r.text.splitlines() if line.strip()]
        return r.json()

    def stats(self) -> dict:
        return self._call("GET", "stats")

    def buckets(self) -> list[dict]:
        return self._call("GET", "buckets")["buckets"]

    def bucket(self, name: str) -> dict:
        return self._call("GET", f"buckets/{quote(name, safe='')}")

    def set_quota(self, name: str, max_bytes: Optional[int], max_objects: Optional[int]) -> dict:
        body = {k: v for k, v in {"max_bytes": max_bytes, "max_objects": max_objects}.items() if v is not None}
        return self._call("PUT", f"buckets/{quote(name, safe='')}/quota", body)

    def delete_quota(self, name: str) -> dict:
        return self._call("DELETE", f"buckets/{quote(name, safe='')}/quota")

    def audit(self, after: int = 0, limit: int = 1000) -> list[dict]:
        return self._call("GET", "audit", query=f"after={after}&limit={limit}&format=jsonl")

    def verify_audit(self) -> dict:
        """Fetches the whole trail and checks its hash chain with Cairn's verifier, from the start."""
        entries, after = [], 0
        while True:
            page = self.audit(after=after, limit=10000)
            entries += page
            if len(page) < 10000:
                break
            after = page[-1]["seq"]
        result = verify_entries(entries)
        if result.get("valid") and entries and result.get("starts_after") != "0" * 64:
            return {**result, "valid": False, "reason": "the trail does not start at the beginning of the chain"}
        return result
