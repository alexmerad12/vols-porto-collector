"""The private Supabase database, through PostgREST, with the service-role key.

SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY come from the environment (GitHub secrets on the
runner, a gitignored .env file on a laptop). Writes go through the database's own functions
(record_prices, record_search), each one transaction.
"""

from __future__ import annotations

import os
import ssl
import time
from typing import Any

import httpx
import truststore

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}

# The operating system's certificate store, not certifi's bundle: Alex's PC runs an antivirus
# that re-signs HTTPS, so certifi alone fails there with CERTIFICATE_VERIFY_FAILED.
TLS = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, url: str, key: str, timeout: float = 30.0):
        self.client = httpx.Client(
            base_url=f"{url.rstrip('/')}/rest/v1",
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=timeout,
            verify=TLS,
        )

    @classmethod
    def from_env(cls) -> "Store":
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise StoreError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
        return cls(url, key)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        # A gateway timeout is not proof the statement did not run, so only idempotent-safe calls
        # are retried: every write here is either an upsert-like function or a log row.
        last: Exception | None = None
        for attempt in range(4):
            try:
                res = self.client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last = exc
            else:
                if res.status_code not in RETRYABLE:
                    if res.is_error:
                        raise StoreError(f"{method} {path}: HTTP {res.status_code} {res.text[:500]}")
                    return res
                last = StoreError(f"{method} {path}: HTTP {res.status_code} {res.text[:200]}")
            time.sleep(2 * (attempt + 1))
        raise StoreError(f"{method} {path} failed after retries: {last}")

    def rpc(self, fn: str, **params: Any) -> Any:
        return self._request("POST", f"/rpc/{fn}", json=params).json()

    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        res = self._request("POST", f"/{table}", json=row, headers={"Prefer": "return=representation"})
        return res.json()[0]

    def update(self, table: str, row_id: int, fields: dict[str, Any]) -> None:
        self._request("PATCH", f"/{table}?id=eq.{row_id}", json=fields)

    def select(self, table: str, query: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/{table}?{query}").json()
