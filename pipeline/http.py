import hashlib
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx


@dataclass
class FetchResult:
    content: bytes
    storage_path: str
    from_cache: bool
    status: int


def _default_user_agent() -> str:
    email = os.environ.get("CONTACT_EMAIL")
    if not email:
        raise RuntimeError(
            "Thiếu CONTACT_EMAIL. Wikimedia yêu cầu User-Agent có email liên hệ."
        )
    return f"travel-agent-thesis/0.1 ({email})"


class Fetcher:
    def __init__(self, conn, source: str, min_interval: float = 1.0,
                 raw_root: str = "data/raw", user_agent: str | None = None):
        self.conn = conn
        self.source = source
        self.min_interval = min_interval
        self.raw_root = Path(raw_root)
        self.user_agent = user_agent or _default_user_agent()
        self._last_call = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent}, timeout=60.0, follow_redirects=True
        )

    def _wait(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.monotonic()

    def _cached(self, key: str) -> tuple[str, str, int] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT storage_path, content_hash, http_status FROM raw_documents"
                " WHERE source_url = %s AND source = %s"
                " ORDER BY fetched_at DESC LIMIT 1",
                (key, self.source),
            )
            return cur.fetchone()

    def fetch(self, url: str, *, params: dict | None = None, method: str = "GET",
              data: str | None = None, force: bool = False) -> FetchResult:
        url_key = url if not params else str(httpx.URL(url, params=params))
        key = f"{method} {url_key}"
        if data is not None:
            key += f" sha256:{hashlib.sha256(data.encode('utf-8')).hexdigest()}"
        if not force:
            hit = self._cached(key)
            if hit:
                path = self.raw_root / hit[0]
                if path.exists():
                    return FetchResult(path.read_bytes(), hit[0], True, hit[2])

        self._wait()
        response = self._client.request(method, url, params=params, content=data)
        response.raise_for_status()

        content = response.content
        digest = hashlib.sha256(content).hexdigest()
        name = quote(key, safe="")[:150]
        rel = Path(self.source) / date.today().isoformat() / f"{name}.{digest[:8]}"
        (self.raw_root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.raw_root / rel).write_bytes(content)

        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO raw_documents"
                " (source, source_url, content_hash, storage_path, http_status)"
                " VALUES (%s, %s, %s, %s, %s)",
                (self.source, key, digest, str(rel), response.status_code),
            )
        return FetchResult(content, str(rel), False, response.status_code)
