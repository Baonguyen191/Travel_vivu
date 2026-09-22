# Pipeline dữ liệu Huế — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dựng tầng dữ liệu cho Travel Agent phạm vi thành phố Huế: từ máy trống, `docker compose up -d` rồi `python -m pipeline.cli all` cho ra PostgreSQL chứa ~100 địa danh và 300–600 quán ăn kèm báo cáo chất lượng.

**Architecture:** ETL ba tầng tuần tự — `ingest` (gọi mạng, ghi bản thô xuống `data/raw/`), `normalize` (thuần Python, không I/O mạng), `load` (upsert vào PostgreSQL), rồi `qa` (báo cáo + mẫu kiểm duyệt). Mỗi nguồn dữ liệu là một module riêng có cùng chữ ký `run(conn, cfg, force=False) -> int`. Không orchestrator, chạy bằng CLI.

**Tech Stack:** Python 3.14, `httpx`, `selectolax`, `trafilatura`, `psycopg` v3, `PyYAML`, `pytest`, `pytest-httpx`; PostgreSQL 17 + PostGIS 3.5 + pgvector trong Docker.

**Spec:** [docs/superpowers/specs/2026-09-22-data-pipeline-hue-design.md](../specs/2026-09-22-data-pipeline-hue-design.md)

## Global Constraints

- **Không API key.** Chỉ Wikidata, Wikipedia, Wikivoyage, OSM Overpass, Wikimedia Commons, Open-Meteo. Không Google Places, không Foursquare, không LLM API. Một task cần key là task sai.
- **Python 3.14.6, Windows.** Không dùng Scrapy. Nếu `psycopg[binary]` chưa có wheel cho cp314, cài `psycopg[c]` hoặc `psycopg` thuần kèm libpq từ Docker Desktop — ghi lại cách đã dùng vào `README.md`.
- **Ghi bản thô trước khi parse.** Mọi phản hồi mạng lưu xuống `data/raw/<source>/<YYYY-MM-DD>/` và có một dòng trong `raw_documents`.
- **Rate limit:** Overpass mỗi lần một request, chờ xong mới gọi tiếp. Các domain khác tối đa 1 request/giây. User-Agent đọc từ biến môi trường `CONTACT_EMAIL`, không hardcode.
- **Idempotent:** mọi lệnh CLI chạy lại nhiều lần không sinh dữ liệu trùng. `ingest` bỏ qua URL có `content_hash` trùng trừ khi có `--force`.
- **Không đoán bừa:** dữ liệu không parse được thì để `NULL` và giữ bản raw, không suy diễn.
- **Test không gọi mạng.** Mock HTTP bằng `pytest_httpx`.
- **Tiếng Việt** cho tên category, `best_time_of_day`, `unsafe_conditions` — dùng dạng không dấu, snake_case: `di_tich`, `sang_som`, `mua_lon`.

---

## File Structure

| File | Trách nhiệm |
|---|---|
| `docker-compose.yml` | Một service `db` |
| `db/Dockerfile` | PostGIS 3.5 + pgvector |
| `db/migrations/*.sql` | DDL, chạy tuần tự theo tên |
| `pipeline/db.py` | Kết nối, chạy migration |
| `pipeline/config.py` | Đọc `config/*.yml` thành dataclass |
| `pipeline/models.py` | `PlaceRecord`, `ImageRecord`, `ChunkRecord` |
| `pipeline/http.py` | `Fetcher`: rate limit, lưu raw, dedupe theo hash |
| `pipeline/ingest/*.py` | Mỗi nguồn một module, cùng chữ ký `run()` |
| `pipeline/normalize/*.py` | Hàm thuần, không I/O |
| `pipeline/load/upsert.py` | Upsert theo `place_external_ids` |
| `pipeline/qa/coverage.py` | Báo cáo ngưỡng + mẫu kiểm duyệt |
| `pipeline/cli.py` | Điều phối lệnh |
| `config/*.yml`, `config/overrides.csv` | Cấu hình người sửa bằng tay |

---

## Task 1: Hạ tầng Docker, migration, kết nối DB

**Files:**
- Create: `pyproject.toml`, `.env.example`, `docker-compose.yml`, `db/Dockerfile`, `db/migrations/001_extensions.sql`, `db/migrations/002_core.sql`, `pipeline/__init__.py`, `pipeline/db.py`, `pipeline/cli.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: không
- Produces: `pipeline.db.connect() -> psycopg.Connection`; `pipeline.db.run_migrations(conn) -> list[str]` trả tên các migration vừa chạy; bảng `schema_migrations(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ)`

- [ ] **Step 1: Tạo `pyproject.toml` và `.env.example`**

```toml
[project]
name = "travel-pipeline"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "httpx>=0.28",
  "psycopg[binary]>=3.2",
  "PyYAML>=6.0",
  "selectolax>=0.3.21",
  "trafilatura>=2.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-httpx>=0.35"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.env.example`:

```
DATABASE_URL=postgresql://travel:travel@localhost:5433/travel
CONTACT_EMAIL=you@example.com
```

Cổng 5433 để không đụng Postgres có sẵn trên máy.

- [ ] **Step 2: Tạo `db/Dockerfile` và `docker-compose.yml`**

```dockerfile
FROM postgis/postgis:17-3.5
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-pgvector \
 && rm -rf /var/lib/apt/lists/*
```

```yaml
services:
  db:
    build: ./db
    environment:
      POSTGRES_USER: travel
      POSTGRES_PASSWORD: travel
      POSTGRES_DB: travel
    ports: ["5433:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U travel -d travel"]
      interval: 5s
      retries: 10
volumes:
  pgdata:
```

- [ ] **Step 3: Viết migration**

`db/migrations/001_extensions.sql`:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
```

`db/migrations/002_core.sql`:

```sql
CREATE TABLE places (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  name_en TEXT,
  category TEXT NOT NULL DEFAULT 'khac',
  location GEOGRAPHY(POINT, 4326),
  opening_hours JSONB,
  opening_hours_raw TEXT,
  ticket_price JSONB,
  dress_code TEXT,
  etiquette TEXT,
  avg_visit_minutes INT,
  indoor_ratio REAL,
  weather_sensitivity JSONB,
  best_time_of_day TEXT[],
  unsafe_conditions TEXT[],
  label_source TEXT NOT NULL DEFAULT 'default',
  website TEXT,
  source_url TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX places_location_gix ON places USING GIST (location);
CREATE INDEX places_category_idx ON places (category);

CREATE TABLE place_external_ids (
  place_id INT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  PRIMARY KEY (source, external_id)
);
CREATE INDEX place_external_ids_place_idx ON place_external_ids (place_id);

CREATE TABLE raw_documents (
  id SERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  source_url TEXT NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  content_hash TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  http_status INT NOT NULL
);
CREATE INDEX raw_documents_url_idx ON raw_documents (source_url, fetched_at DESC);

CREATE TABLE place_chunks (
  id SERIAL PRIMARY KEY,
  place_id INT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source_url TEXT,
  embedding VECTOR(1024),
  UNIQUE (place_id, content_hash)
);

CREATE TABLE place_images (
  id SERIAL PRIMARY KEY,
  place_id INT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  image_url TEXT NOT NULL UNIQUE,
  license TEXT,
  author TEXT,
  embedding VECTOR(768)
);

CREATE TABLE reviews (
  id SERIAL PRIMARY KEY,
  place_id INT REFERENCES places(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  rating REAL,
  aspects JSONB,
  source TEXT,
  created_at TIMESTAMPTZ
);

CREATE TABLE weather_cache (
  lat_grid REAL, lon_grid REAL, forecast_time TIMESTAMPTZ,
  temperature REAL, precip_prob REAL, precip_mm REAL,
  wind_speed REAL, uv_index REAL, weather_code INT,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (lat_grid, lon_grid, forecast_time)
);

CREATE TABLE climate_normals (
  lat_grid REAL, lon_grid REAL, month INT,
  temp_avg REAL, precip_mm_avg REAL, rain_days REAL, wind_avg REAL,
  PRIMARY KEY (lat_grid, lon_grid, month)
);
```

Index HNSW để riêng, chạy sau khi có dữ liệu — `db/migrations/003_vector_indexes.sql`:

```sql
CREATE INDEX place_chunks_embedding_idx ON place_chunks
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX place_images_embedding_idx ON place_images
  USING hnsw (embedding vector_cosine_ops);
```

- [ ] **Step 4: Viết test cho migration runner**

`tests/test_db.py`:

```python
import pytest
from pipeline import db

pytestmark = pytest.mark.integration


def test_run_migrations_is_idempotent():
    conn = db.connect()
    first = db.run_migrations(conn)
    assert "002_core.sql" in first
    second = db.run_migrations(conn)
    assert second == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM places")
        assert cur.fetchone()[0] >= 0
```

- [ ] **Step 5: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_db.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'pipeline.db'`

- [ ] **Step 6: Viết `pipeline/db.py`**

```python
import os
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def connect() -> psycopg.Connection:
    url = os.environ.get(
        "DATABASE_URL", "postgresql://travel:travel@localhost:5433/travel"
    )
    return psycopg.connect(url, autocommit=True)


def run_migrations(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT filename FROM schema_migrations")
        done = {row[0] for row in cur.fetchall()}

    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
        applied.append(path.name)
    return applied
```

- [ ] **Step 7: Viết `pipeline/cli.py` với lệnh `migrate`**

```python
import argparse

from pipeline import db


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    args = parser.parse_args()

    if args.command == "migrate":
        conn = db.connect()
        applied = db.run_migrations(conn)
        print(f"Đã chạy {len(applied)} migration: {', '.join(applied) or 'không có'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Thêm `pipeline/__main__.py` với `from pipeline.cli import main; raise SystemExit(main())` để `python -m pipeline.cli` và `python -m pipeline` đều chạy.

- [ ] **Step 8: Dựng DB và chạy test**

Run:
```bash
docker compose up -d --build
python -m pytest tests/test_db.py -v
```
Expected: PASS. Nếu `CREATE EXTENSION vector` lỗi, kiểm tra lại `docker compose build --no-cache db`.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .env.example docker-compose.yml db pipeline tests
git commit -m "feat: dựng PostGIS + pgvector và migration runner"
```

---

## Task 2: Cấu hình và model dữ liệu

**Files:**
- Create: `pipeline/config.py`, `pipeline/models.py`, `config/city_hue.yml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: không
- Produces:
  - `CityConfig(name: str, bbox: tuple[float, float, float, float], core_center: tuple[float, float], core_radius_km: float, polygon_wkt: str | None)`, bbox theo thứ tự `(south, west, north, east)`
  - `load_city(path: str = "config/city_hue.yml") -> CityConfig`
  - `save_city(cfg: CityConfig, path: str) -> None`
  - `PlaceRecord(name, external_ids: dict[str, str], lat, lon, category="khac", name_en=None, opening_hours=None, opening_hours_raw=None, website=None, source_url=None, tags: dict[str, str] = {}, wikidata_classes: list[str] = [], avg_visit_minutes=None, indoor_ratio=None, weather_sensitivity=None, best_time_of_day=None, unsafe_conditions=None, ticket_price=None, dress_code=None, label_source="default")`
  - `ImageRecord(place_key: str, image_url: str, license: str | None, author: str | None)`
  - `ChunkRecord(place_key: str, content: str, source_url: str)`
  - `place_key` là chuỗi `"<source>:<external_id>"`, ví dụ `"wikidata:Q123"`

- [ ] **Step 1: Viết test**

`tests/test_config.py`:

```python
from pipeline.config import CityConfig, load_city, save_city


def test_load_city_reads_bbox_and_core(tmp_path):
    p = tmp_path / "city.yml"
    p.write_text(
        "name: Huế\n"
        "bbox: [16.335, 107.435, 16.605, 107.725]\n"
        "core_center: [16.4698, 107.5796]\n"
        "core_radius_km: 15\n"
        "polygon_wkt: null\n",
        encoding="utf-8",
    )
    cfg = load_city(str(p))
    assert cfg.name == "Huế"
    assert cfg.bbox == (16.335, 107.435, 16.605, 107.725)
    assert cfg.core_center == (16.4698, 107.5796)
    assert cfg.polygon_wkt is None


def test_save_city_roundtrips_polygon(tmp_path):
    p = tmp_path / "city.yml"
    cfg = CityConfig(
        name="Huế",
        bbox=(16.0, 107.0, 17.0, 108.0),
        core_center=(16.5, 107.5),
        core_radius_km=15.0,
        polygon_wkt="POLYGON((107 16, 108 16, 108 17, 107 16))",
    )
    save_city(cfg, str(p))
    assert load_city(str(p)) == cfg
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'pipeline.config'`

- [ ] **Step 3: Viết `pipeline/models.py`**

```python
from dataclasses import dataclass, field


@dataclass
class PlaceRecord:
    name: str
    external_ids: dict[str, str]
    lat: float
    lon: float
    category: str = "khac"
    name_en: str | None = None
    opening_hours: dict | None = None
    opening_hours_raw: str | None = None
    website: str | None = None
    source_url: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    wikidata_classes: list[str] = field(default_factory=list)
    avg_visit_minutes: int | None = None
    indoor_ratio: float | None = None
    weather_sensitivity: dict | None = None
    best_time_of_day: list[str] | None = None
    unsafe_conditions: list[str] | None = None
    ticket_price: dict | None = None
    dress_code: str | None = None
    label_source: str = "default"

    @property
    def place_key(self) -> str:
        source, external_id = sorted(self.external_ids.items())[0]
        return f"{source}:{external_id}"


@dataclass
class ImageRecord:
    place_key: str
    image_url: str
    license: str | None = None
    author: str | None = None


@dataclass
class ChunkRecord:
    place_key: str
    content: str
    source_url: str
```

- [ ] **Step 4: Viết `pipeline/config.py`**

```python
from dataclasses import asdict, dataclass

import yaml


@dataclass(frozen=True)
class CityConfig:
    name: str
    bbox: tuple[float, float, float, float]
    core_center: tuple[float, float]
    core_radius_km: float
    polygon_wkt: str | None = None


def load_city(path: str = "config/city_hue.yml") -> CityConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return CityConfig(
        name=raw["name"],
        bbox=tuple(raw["bbox"]),
        core_center=tuple(raw["core_center"]),
        core_radius_km=float(raw["core_radius_km"]),
        polygon_wkt=raw.get("polygon_wkt"),
    )


def save_city(cfg: CityConfig, path: str = "config/city_hue.yml") -> None:
    data = asdict(cfg)
    data["bbox"] = list(cfg.bbox)
    data["core_center"] = list(cfg.core_center)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
```

- [ ] **Step 5: Tạo `config/city_hue.yml`**

```yaml
name: Huế
# bbox quanh lõi đô thị, không phải toàn thành phố trực thuộc trung ương.
# Thứ tự: south, west, north, east
bbox: [16.335, 107.435, 16.605, 107.725]
# Kinh thành Huế
core_center: [16.4698, 107.5796]
core_radius_km: 15
polygon_wkt: null
```

- [ ] **Step 6: Chạy test**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 7: Commit**

```bash
git add pipeline/config.py pipeline/models.py config/city_hue.yml tests/test_config.py
git commit -m "feat: thêm CityConfig và model dữ liệu địa điểm"
```

---

## Task 3: Tầng HTTP có rate limit và lưu bản thô

**Files:**
- Create: `pipeline/http.py`
- Test: `tests/test_http.py`

**Interfaces:**
- Consumes: `pipeline.db.connect`
- Produces:
  - `FetchResult(content: bytes, storage_path: str, from_cache: bool, status: int)`
  - `Fetcher(conn, source: str, min_interval: float = 1.0, raw_root: str = "data/raw", user_agent: str | None = None)`
  - `Fetcher.fetch(url: str, *, params: dict | None = None, method: str = "GET", data: str | None = None, force: bool = False) -> FetchResult`

- [ ] **Step 1: Viết test**

`tests/test_http.py`:

```python
import httpx

from pipeline.http import Fetcher


def test_fetch_writes_raw_file_and_row(httpx_mock, tmp_path, db_conn):
    httpx_mock.add_response(url="https://example.org/a", content=b"xin chao")
    f = Fetcher(db_conn, "test", min_interval=0, raw_root=str(tmp_path),
                user_agent="travel-agent/0.1 (you@example.com)")

    res = f.fetch("https://example.org/a")

    assert res.content == b"xin chao"
    assert res.from_cache is False
    assert (tmp_path / res.storage_path).read_bytes() == b"xin chao"
    with db_conn.cursor() as cur:
        cur.execute("SELECT source, http_status FROM raw_documents")
        assert cur.fetchall() == [("test", 200)]


def test_fetch_skips_network_when_hash_unchanged(httpx_mock, tmp_path, db_conn):
    httpx_mock.add_response(url="https://example.org/b", content=b"same")
    f = Fetcher(db_conn, "test", min_interval=0, raw_root=str(tmp_path),
                user_agent="ua")
    f.fetch("https://example.org/b")

    second = f.fetch("https://example.org/b")

    assert second.from_cache is True
    assert second.content == b"same"
    assert len(httpx_mock.get_requests()) == 1


def test_fetch_sends_user_agent(httpx_mock, tmp_path, db_conn):
    httpx_mock.add_response(url="https://example.org/c", content=b"x")
    f = Fetcher(db_conn, "test", min_interval=0, raw_root=str(tmp_path),
                user_agent="travel-agent/0.1 (you@example.com)")

    f.fetch("https://example.org/c")

    req = httpx_mock.get_requests()[0]
    assert req.headers["user-agent"] == "travel-agent/0.1 (you@example.com)"


def test_fetch_raises_on_http_error(httpx_mock, tmp_path, db_conn):
    httpx_mock.add_response(url="https://example.org/d", status_code=503)
    f = Fetcher(db_conn, "test", min_interval=0, raw_root=str(tmp_path), user_agent="ua")

    try:
        f.fetch("https://example.org/d")
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("phải ném HTTPStatusError")
```

Thêm fixture `db_conn` vào `tests/conftest.py`:

```python
import pytest

from pipeline import db


@pytest.fixture
def db_conn():
    conn = db.connect()
    db.run_migrations(conn)
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE places, place_external_ids, raw_documents, place_chunks,"
            " place_images, reviews, weather_cache, climate_normals RESTART IDENTITY CASCADE"
        )
    yield conn
    conn.close()
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_http.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'pipeline.http'`

- [ ] **Step 3: Viết `pipeline/http.py`**

```python
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

    def _cached(self, url: str) -> tuple[str, str] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT storage_path, content_hash FROM raw_documents"
                " WHERE source_url = %s ORDER BY fetched_at DESC LIMIT 1",
                (url,),
            )
            return cur.fetchone()

    def fetch(self, url: str, *, params: dict | None = None, method: str = "GET",
              data: str | None = None, force: bool = False) -> FetchResult:
        key = url if not params else str(httpx.URL(url, params=params))
        if not force:
            hit = self._cached(key)
            if hit:
                path = self.raw_root / hit[0]
                if path.exists():
                    return FetchResult(path.read_bytes(), hit[0], True, 200)

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
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_http.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add pipeline/http.py tests/test_http.py tests/conftest.py
git commit -m "feat: thêm tầng HTTP có rate limit, lưu raw và dedupe theo hash"
```

---

## Task 4: Ingest ranh giới hành chính Huế

**Files:**
- Create: `pipeline/ingest/__init__.py`, `pipeline/ingest/boundary.py`
- Modify: `pipeline/cli.py` (thêm `ingest --source boundary`)
- Test: `tests/test_ingest_boundary.py`

**Interfaces:**
- Consumes: `Fetcher`, `CityConfig`, `save_city`
- Produces: `pipeline.ingest.boundary.run(conn, cfg: CityConfig, force: bool = False) -> int` — ghi `polygon_wkt` vào `config/city_hue.yml`, trả số ring lấy được; `pipeline.ingest.boundary.rings_to_wkt(elements: list[dict]) -> str`

Huế là thành phố trực thuộc trung ương từ 2025-01-01, nên OSM gắn `admin_level=4`. Query hỏi theo tên và chấp nhận cả `4` lẫn `6`, chọn quan hệ có `admin_level` lớn nhất (đơn vị nhỏ nhất) để tránh lấy nhầm ranh giới cấp trên.

- [ ] **Step 1: Viết test**

`tests/test_ingest_boundary.py`:

```python
from pipeline.ingest.boundary import rings_to_wkt


def test_rings_to_wkt_builds_closed_polygon():
    elements = [
        {
            "type": "relation",
            "tags": {"admin_level": "4", "name": "Thành phố Huế"},
            "members": [
                {"role": "outer", "geometry": [
                    {"lat": 16.4, "lon": 107.5},
                    {"lat": 16.5, "lon": 107.5},
                    {"lat": 16.5, "lon": 107.6},
                ]}
            ],
        }
    ]
    wkt = rings_to_wkt(elements)
    assert wkt.startswith("POLYGON((")
    assert wkt.count("107.5 16.4") == 2  # điểm đầu lặp lại ở cuối để khép vòng


def test_rings_to_wkt_prefers_smallest_admin_unit():
    elements = [
        {"type": "relation", "tags": {"admin_level": "4"},
         "members": [{"role": "outer", "geometry": [
             {"lat": 0, "lon": 0}, {"lat": 9, "lon": 0}, {"lat": 9, "lon": 9}]}]},
        {"type": "relation", "tags": {"admin_level": "6"},
         "members": [{"role": "outer", "geometry": [
             {"lat": 1, "lon": 1}, {"lat": 2, "lon": 1}, {"lat": 2, "lon": 2}]}]},
    ]
    assert "1 1" in rings_to_wkt(elements)
    assert "9 9" not in rings_to_wkt(elements)
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_ingest_boundary.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'pipeline.ingest'`

- [ ] **Step 3: Viết `pipeline/ingest/boundary.py`**

```python
import json

from pipeline.config import CityConfig, save_city
from pipeline.http import Fetcher

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

QUERY = """
[out:json][timeout:180];
relation["boundary"="administrative"]["name"="Thành phố Huế"]
        ["admin_level"~"^(4|6)$"];
out geom;
"""


def rings_to_wkt(elements: list[dict]) -> str:
    relations = [e for e in elements if e.get("type") == "relation"]
    if not relations:
        raise ValueError("Overpass không trả về relation ranh giới nào")
    chosen = max(relations, key=lambda e: int(e["tags"].get("admin_level", 0)))

    points: list[tuple[float, float]] = []
    for member in chosen.get("members", []):
        if member.get("role") != "outer":
            continue
        for node in member.get("geometry", []):
            points.append((node["lon"], node["lat"]))
    if len(points) < 3:
        raise ValueError("Ring outer có dưới 3 điểm")
    if points[0] != points[-1]:
        points.append(points[0])
    body = ", ".join(f"{lon} {lat}" for lon, lat in points)
    return f"POLYGON(({body}))"


def run(conn, cfg: CityConfig, force: bool = False) -> int:
    fetcher = Fetcher(conn, "osm_boundary", min_interval=2.0)
    res = fetcher.fetch(OVERPASS_URL, method="POST", data=QUERY, force=force)
    elements = json.loads(res.content)["elements"]
    wkt = rings_to_wkt(elements)
    save_city(
        CityConfig(cfg.name, cfg.bbox, cfg.core_center, cfg.core_radius_km, wkt),
        "config/city_hue.yml",
    )
    return 1
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_ingest_boundary.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Nối vào CLI**

Trong `pipeline/cli.py`, thay phần `sub.add_parser("migrate")` bằng:

```python
ingest = sub.add_parser("ingest")
ingest.add_argument("--source", required=True)
ingest.add_argument("--force", action="store_true")
```

và thêm nhánh xử lý:

```python
INGEST_MODULES = {"boundary": "pipeline.ingest.boundary"}

if args.command == "ingest":
    import importlib

    from pipeline.config import load_city

    module = importlib.import_module(INGEST_MODULES[args.source])
    conn = db.connect()
    count = module.run(conn, load_city(), force=args.force)
    print(f"{args.source}: {count} bản ghi")
```

- [ ] **Step 6: Chạy thật một lần**

Run: `python -m pipeline.cli ingest --source boundary`
Expected: in `boundary: 1 bản ghi`, và `config/city_hue.yml` có `polygon_wkt` dài vài nghìn ký tự. Nếu Overpass trả `429` hoặc `504`, chờ 60 giây rồi chạy lại — đây là hạn mức công cộng.

- [ ] **Step 7: Commit**

```bash
git add pipeline/ingest pipeline/cli.py config/city_hue.yml tests/test_ingest_boundary.py
git commit -m "feat: lấy ranh giới hành chính Huế từ Overpass"
```

---

## Task 5: Ingest địa danh từ Wikidata

**Files:**
- Create: `pipeline/ingest/wikidata.py`
- Modify: `pipeline/cli.py` (thêm `wikidata` vào `INGEST_MODULES`)
- Test: `tests/test_ingest_wikidata.py`

**Interfaces:**
- Consumes: `Fetcher`, `CityConfig`, `PlaceRecord`
- Produces:
  - `pipeline.ingest.wikidata.parse_bindings(bindings: list[dict]) -> list[PlaceRecord]`
  - `pipeline.ingest.wikidata.run(conn, cfg, force=False) -> int` — ghi `data/staged/wikidata.json`
  - `pipeline.ingest.wikidata.STAGED_PATH = "data/staged/wikidata.json"`

Bản thô SPARQL lưu qua `Fetcher`; kết quả đã parse lưu thành JSON ở `data/staged/` để tầng `normalize` đọc mà không cần gọi lại mạng.

- [ ] **Step 1: Viết test**

`tests/test_ingest_wikidata.py`:

```python
from pipeline.ingest.wikidata import parse_bindings


def test_parse_bindings_extracts_place():
    bindings = [
        {
            "item": {"value": "http://www.wikidata.org/entity/Q1023458"},
            "itemLabel": {"value": "Chùa Thiên Mụ"},
            "coord": {"value": "Point(107.5453 16.4539)"},
            "classes": {"value": "Q24398318|Q16970"},
            "image": {"value": "https://commons.wikimedia.org/wiki/Special:FilePath/a.jpg"},
            "commons": {"value": "Thien Mu Pagoda"},
            "viTitle": {"value": "Chùa Thiên Mụ"},
        }
    ]
    [place] = parse_bindings(bindings)
    assert place.external_ids == {"wikidata": "Q1023458"}
    assert place.name == "Chùa Thiên Mụ"
    assert (round(place.lat, 4), round(place.lon, 4)) == (16.4539, 107.5453)
    assert place.wikidata_classes == ["Q24398318", "Q16970"]
    assert place.tags["commons_category"] == "Thien Mu Pagoda"
    assert place.tags["vi_title"] == "Chùa Thiên Mụ"


def test_parse_bindings_skips_rows_without_coordinates():
    bindings = [
        {"item": {"value": "http://www.wikidata.org/entity/Q1"},
         "itemLabel": {"value": "Không tọa độ"}},
    ]
    assert parse_bindings(bindings) == []
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_ingest_wikidata.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'pipeline.ingest.wikidata'`

- [ ] **Step 3: Viết `pipeline/ingest/wikidata.py`**

```python
import json
import re
from pathlib import Path

from pipeline.config import CityConfig
from pipeline.http import Fetcher
from pipeline.models import PlaceRecord

SPARQL_URL = "https://query.wikidata.org/sparql"
STAGED_PATH = "data/staged/wikidata.json"

POINT_RE = re.compile(r"Point\(([-\d.]+) ([-\d.]+)\)")

QUERY_TEMPLATE = """
SELECT ?item ?itemLabel ?coord ?image ?commons ?viTitle
       (GROUP_CONCAT(DISTINCT ?cls; separator="|") AS ?classes)
WHERE {{
  SERVICE wikibase:box {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerWest "Point({west} {south})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerEast "Point({east} {north})"^^geo:wktLiteral .
  }}
  ?item wdt:P31 ?cls .
  OPTIONAL {{ ?item wdt:P18 ?image. }}
  OPTIONAL {{ ?item wdt:P373 ?commons. }}
  OPTIONAL {{
    ?viArticle schema:about ?item ;
               schema:isPartOf <https://vi.wikipedia.org/> ;
               schema:name ?viTitle .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "vi,en". }}
}}
GROUP BY ?item ?itemLabel ?coord ?image ?commons ?viTitle
"""


def parse_bindings(bindings: list[dict]) -> list[PlaceRecord]:
    places = []
    for row in bindings:
        coord = row.get("coord", {}).get("value", "")
        match = POINT_RE.match(coord)
        if not match:
            continue
        lon, lat = float(match.group(1)), float(match.group(2))
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        classes = [c for c in row.get("classes", {}).get("value", "").split("|") if c]

        tags: dict[str, str] = {}
        if row.get("commons"):
            tags["commons_category"] = row["commons"]["value"]
        if row.get("image"):
            tags["image_url"] = row["image"]["value"]
        if row.get("viTitle"):
            tags["vi_title"] = row["viTitle"]["value"]

        places.append(
            PlaceRecord(
                name=row["itemLabel"]["value"],
                external_ids={"wikidata": qid},
                lat=lat,
                lon=lon,
                wikidata_classes=classes,
                tags=tags,
                source_url=row["item"]["value"],
            )
        )
    return places


def run(conn, cfg: CityConfig, force: bool = False) -> int:
    south, west, north, east = cfg.bbox
    query = QUERY_TEMPLATE.format(south=south, west=west, north=north, east=east)
    fetcher = Fetcher(conn, "wikidata", min_interval=1.0)
    res = fetcher.fetch(
        SPARQL_URL, params={"query": query, "format": "json"}, force=force
    )
    bindings = json.loads(res.content)["results"]["bindings"]
    places = parse_bindings(bindings)

    Path(STAGED_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(STAGED_PATH).write_text(
        json.dumps([p.__dict__ for p in places], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return len(places)
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_ingest_wikidata.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Chạy thật**

Thêm `"wikidata": "pipeline.ingest.wikidata"` vào `INGEST_MODULES` rồi chạy:
`python -m pipeline.cli ingest --source wikidata`
Expected: 200–600 bản ghi (bbox bắt mọi thứ có tọa độ, kể cả làng xã — Task 8 lọc lại bằng category, Task 9 lọc theo bán kính lõi).

- [ ] **Step 6: Commit**

```bash
git add pipeline/ingest/wikidata.py pipeline/cli.py tests/test_ingest_wikidata.py
git commit -m "feat: lấy địa danh Huế từ Wikidata SPARQL"
```

---

## Task 6: Ingest POI từ OSM Overpass

**Files:**
- Create: `pipeline/ingest/osm.py`
- Modify: `pipeline/cli.py`
- Test: `tests/test_ingest_osm.py`

**Interfaces:**
- Consumes: `Fetcher`, `CityConfig`, `PlaceRecord`
- Produces:
  - `pipeline.ingest.osm.parse_elements(elements: list[dict]) -> list[PlaceRecord]`
  - `pipeline.ingest.osm.run(conn, cfg, force=False) -> int`
  - `pipeline.ingest.osm.STAGED_PATH = "data/staged/osm.json"`

- [ ] **Step 1: Viết test**

`tests/test_ingest_osm.py`:

```python
from pipeline.ingest.osm import parse_elements


def test_parse_elements_reads_node_and_way():
    elements = [
        {"type": "node", "id": 1, "lat": 16.47, "lon": 107.58,
         "tags": {"name": "Quán bún bò", "amenity": "restaurant",
                  "opening_hours": "Mo-Su 06:00-10:00", "website": "http://x.vn"}},
        {"type": "way", "id": 2, "center": {"lat": 16.46, "lon": 107.57},
         "tags": {"name": "Đại Nội", "historic": "castle"}},
    ]
    a, b = parse_elements(elements)
    assert a.external_ids == {"osm": "node/1"}
    assert a.opening_hours_raw == "Mo-Su 06:00-10:00"
    assert a.website == "http://x.vn"
    assert b.external_ids == {"osm": "way/2"}
    assert (b.lat, b.lon) == (16.46, 107.57)


def test_parse_elements_skips_unnamed_and_positionless():
    elements = [
        {"type": "node", "id": 3, "lat": 16.4, "lon": 107.5,
         "tags": {"amenity": "restaurant"}},
        {"type": "way", "id": 4, "tags": {"name": "Không tọa độ"}},
    ]
    assert parse_elements(elements) == []
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_ingest_osm.py -v`
Expected: FAIL với `ModuleNotFoundError`

- [ ] **Step 3: Viết `pipeline/ingest/osm.py`**

```python
import json
from pathlib import Path

from pipeline.config import CityConfig
from pipeline.http import Fetcher
from pipeline.models import PlaceRecord

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
STAGED_PATH = "data/staged/osm.json"

QUERY_TEMPLATE = """
[out:json][timeout:300];
(
  nwr["tourism"~"^(attraction|museum|viewpoint|artwork|gallery)$"]({bbox});
  nwr["historic"]({bbox});
  nwr["amenity"~"^(restaurant|cafe|fast_food)$"]({bbox});
  nwr["leisure"~"^(park|garden)$"]({bbox});
);
out center tags;
"""


def parse_elements(elements: list[dict]) -> list[PlaceRecord]:
    places = []
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        lat = el.get("lat", (el.get("center") or {}).get("lat"))
        lon = el.get("lon", (el.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        osm_id = f"{el['type']}/{el['id']}"
        places.append(
            PlaceRecord(
                name=name,
                name_en=tags.get("name:en"),
                external_ids={"osm": osm_id},
                lat=float(lat),
                lon=float(lon),
                tags=tags,
                opening_hours_raw=tags.get("opening_hours"),
                website=tags.get("website") or tags.get("contact:website"),
                source_url=f"https://www.openstreetmap.org/{osm_id}",
            )
        )
    return places


def run(conn, cfg: CityConfig, force: bool = False) -> int:
    south, west, north, east = cfg.bbox
    query = QUERY_TEMPLATE.format(bbox=f"{south},{west},{north},{east}")
    fetcher = Fetcher(conn, "osm", min_interval=2.0)
    res = fetcher.fetch(OVERPASS_URL, method="POST", data=query, force=force)
    places = parse_elements(json.loads(res.content)["elements"])

    Path(STAGED_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(STAGED_PATH).write_text(
        json.dumps([p.__dict__ for p in places], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return len(places)
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_ingest_osm.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Chạy thật**

Thêm `"osm": "pipeline.ingest.osm"` vào `INGEST_MODULES` rồi:
`python -m pipeline.cli ingest --source osm`
Expected: 500–1500 bản ghi. Ghi lại con số thật vào `README.md` — nếu quán ăn dưới 300, nới `core_radius_km` hoặc thêm `amenity=bar` vào query, và ghi lý do vào spec.

- [ ] **Step 6: Commit**

```bash
git add pipeline/ingest/osm.py pipeline/cli.py tests/test_ingest_osm.py
git commit -m "feat: lấy POI Huế từ OSM Overpass"
```

---

## Task 7: Parser `opening_hours`

**Files:**
- Create: `pipeline/normalize/__init__.py`, `pipeline/normalize/opening_hours.py`
- Test: `tests/test_opening_hours.py`

**Interfaces:**
- Consumes: không
- Produces: `parse_opening_hours(raw: str | None) -> dict | None` — trả `{"mon": [["07:00", "17:00"]], ...}`, khóa là `mon tue wed thu fri sat sun`, ngày đóng cửa là danh sách rỗng. Trả `None` khi chuỗi nằm ngoài tập con hỗ trợ.

Tập con hỗ trợ: `24/7`; danh sách quy tắc cách nhau bằng `;`; mỗi quy tắc là `<dải ngày> <dải giờ>[,<dải giờ>]` hoặc `<dải ngày> off`; dải ngày dạng `Mo`, `Mo-Fr`, `Mo,We,Fr`. Mọi thứ khác (`sunrise-sunset`, `Apr-Oct ...`, `PH`, `week ...`) trả `None`.

- [ ] **Step 1: Viết test**

`tests/test_opening_hours.py`:

```python
import pytest

from pipeline.normalize.opening_hours import parse_opening_hours

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def test_24_7():
    assert parse_opening_hours("24/7") == {d: [["00:00", "24:00"]] for d in DAYS}


def test_simple_range():
    result = parse_opening_hours("Mo-Su 07:00-17:00")
    assert result == {d: [["07:00", "17:00"]] for d in DAYS}


def test_two_intervals_in_one_day():
    result = parse_opening_hours("Mo-Fr 08:00-11:30,13:30-17:00")
    assert result["mon"] == [["08:00", "11:30"], ["13:30", "17:00"]]
    assert result["sat"] == []


def test_off_rule_overrides_earlier_rule():
    result = parse_opening_hours("Tu-Su 08:00-17:00; Mo off")
    assert result["mon"] == []
    assert result["tue"] == [["08:00", "17:00"]]


def test_day_list():
    result = parse_opening_hours("Mo,We,Fr 09:00-12:00")
    assert result["wed"] == [["09:00", "12:00"]]
    assert result["tue"] == []


def test_wraparound_day_range():
    result = parse_opening_hours("Sa-Mo 10:00-12:00")
    assert result["sat"] and result["sun"] and result["mon"]
    assert result["tue"] == []


@pytest.mark.parametrize(
    "raw",
    ["sunrise-sunset", "Apr-Oct Mo-Su 07:00-18:00", "Mo-Su 07:00-17:00; PH off",
     "week 1-53/2 Mo 10:00-12:00", "linh tinh", "", None],
)
def test_unsupported_returns_none(raw):
    assert parse_opening_hours(raw) is None
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_opening_hours.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'pipeline.normalize'`

- [ ] **Step 3: Viết `pipeline/normalize/opening_hours.py`**

```python
import re

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OSM_DAYS = {"Mo": 0, "Tu": 1, "We": 2, "Th": 3, "Fr": 4, "Sa": 5, "Su": 6}

TIME_RANGE_RE = re.compile(r"^([0-2]\d:[0-5]\d)-([0-2]\d:[0-5]\d)$")
DAY_TOKEN_RE = re.compile(r"^(Mo|Tu|We|Th|Fr|Sa|Su)(-(Mo|Tu|We|Th|Fr|Sa|Su))?$")


def _expand_days(spec: str) -> list[int] | None:
    indexes: list[int] = []
    for token in spec.split(","):
        match = DAY_TOKEN_RE.match(token.strip())
        if not match:
            return None
        start = OSM_DAYS[match.group(1)]
        end = OSM_DAYS[match.group(3)] if match.group(3) else start
        i = start
        while True:
            indexes.append(i)
            if i == end:
                break
            i = (i + 1) % 7
    return indexes


def _parse_intervals(spec: str) -> list[list[str]] | None:
    intervals = []
    for part in spec.split(","):
        match = TIME_RANGE_RE.match(part.strip())
        if not match:
            return None
        intervals.append([match.group(1), match.group(2)])
    return intervals


def parse_opening_hours(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text == "24/7":
        return {d: [["00:00", "24:00"]] for d in DAYS}

    result: dict[str, list[list[str]]] = {d: [] for d in DAYS}
    for rule in text.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        parts = rule.split(None, 1)
        if len(parts) != 2:
            return None
        day_spec, time_spec = parts[0], parts[1].strip()
        days = _expand_days(day_spec)
        if days is None:
            return None
        if time_spec == "off":
            for i in days:
                result[DAYS[i]] = []
            continue
        intervals = _parse_intervals(time_spec)
        if intervals is None:
            return None
        for i in days:
            result[DAYS[i]] = intervals
    if all(not v for v in result.values()):
        return None
    return result
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_opening_hours.py -v`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add pipeline/normalize tests/test_opening_hours.py
git commit -m "feat: parser opening_hours cho tập con cú pháp OSM"
```

---

## Task 8: Map category

**Files:**
- Create: `pipeline/normalize/category.py`, `config/categories.yml`
- Test: `tests/test_category.py`

**Interfaces:**
- Consumes: `PlaceRecord`
- Produces: `map_category(tags: dict[str, str], wikidata_classes: list[str], rules: dict) -> str`; `load_category_rules(path: str = "config/categories.yml") -> dict`

Thứ tự ưu tiên: class Wikidata trước (chính xác hơn), rồi tag OSM theo thứ tự khai báo trong YAML, cuối cùng là `khac`.

- [ ] **Step 1: Viết test**

`tests/test_category.py`:

```python
from pipeline.normalize.category import load_category_rules, map_category

RULES = {
    "wikidata": {"Q16970": "chua", "Q24398318": "chua", "Q33506": "bao_tang"},
    "osm": [
        {"match": {"amenity": "restaurant"}, "category": "nha_hang"},
        {"match": {"amenity": "cafe"}, "category": "quan_ca_phe"},
        {"match": {"tourism": "museum"}, "category": "bao_tang"},
        {"match": {"historic": "castle"}, "category": "di_tich"},
        {"match": {"historic": "*"}, "category": "di_tich"},
        {"match": {"leisure": "park"}, "category": "cong_vien"},
    ],
}


def test_wikidata_class_wins_over_osm_tag():
    assert map_category({"amenity": "restaurant"}, ["Q16970"], RULES) == "chua"


def test_osm_exact_tag():
    assert map_category({"amenity": "cafe"}, [], RULES) == "quan_ca_phe"


def test_osm_wildcard_tag():
    assert map_category({"historic": "memorial"}, [], RULES) == "di_tich"


def test_unknown_falls_back_to_khac():
    assert map_category({"shop": "bakery"}, ["Q999999"], RULES) == "khac"


def test_config_file_has_required_categories():
    rules = load_category_rules("config/categories.yml")
    categories = {r["category"] for r in rules["osm"]} | set(rules["wikidata"].values())
    assert {"di_tich", "chua", "bao_tang", "nha_hang", "quan_ca_phe"} <= categories
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_category.py -v`
Expected: FAIL với `ModuleNotFoundError`

- [ ] **Step 3: Viết `config/categories.yml`**

```yaml
wikidata:
  Q16970: chua          # nhà thờ / kiến trúc tôn giáo
  Q24398318: chua       # chùa Phật giáo
  Q33506: bao_tang
  Q751876: di_tich      # lâu đài, cung điện
  Q39614: lang_tam      # nghĩa trang, lăng mộ
  Q22698: cong_vien
  Q11707: nha_hang
  Q30022: quan_ca_phe
  Q40080: bai_bien
  Q4022: song
osm:
  - { match: { amenity: restaurant }, category: nha_hang }
  - { match: { amenity: fast_food }, category: nha_hang }
  - { match: { amenity: cafe }, category: quan_ca_phe }
  - { match: { tourism: museum }, category: bao_tang }
  - { match: { tourism: gallery }, category: bao_tang }
  - { match: { tourism: viewpoint }, category: diem_ngam_canh }
  - { match: { historic: tomb }, category: lang_tam }
  - { match: { historic: castle }, category: di_tich }
  - { match: { historic: city_gate }, category: di_tich }
  - { match: { historic: "*" }, category: di_tich }
  - { match: { amenity: place_of_worship }, category: chua }
  - { match: { leisure: park }, category: cong_vien }
  - { match: { leisure: garden }, category: cong_vien }
  - { match: { natural: beach }, category: bai_bien }
  - { match: { tourism: attraction }, category: diem_tham_quan }
```

- [ ] **Step 4: Viết `pipeline/normalize/category.py`**

```python
import yaml

DEFAULT_CATEGORY = "khac"


def load_category_rules(path: str = "config/categories.yml") -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def map_category(tags: dict, wikidata_classes: list[str], rules: dict) -> str:
    by_qid = rules.get("wikidata", {})
    for qid in wikidata_classes:
        if qid in by_qid:
            return by_qid[qid]

    for rule in rules.get("osm", []):
        for key, expected in rule["match"].items():
            value = tags.get(key)
            if value is not None and (expected == "*" or value == expected):
                return rule["category"]
    return DEFAULT_CATEGORY
```

- [ ] **Step 5: Chạy test**

Run: `python -m pytest tests/test_category.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add pipeline/normalize/category.py config/categories.yml tests/test_category.py
git commit -m "feat: map category từ class Wikidata và tag OSM"
```

---

## Task 9: Chuẩn hóa tên, lọc bán kính lõi, ghép trùng

**Files:**
- Create: `pipeline/normalize/merge.py`
- Test: `tests/test_merge.py`

**Interfaces:**
- Consumes: `PlaceRecord`
- Produces:
  - `normalize_name(name: str) -> str`
  - `haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float`
  - `within_core(place: PlaceRecord, cfg: CityConfig) -> bool`
  - `merge_places(wikidata: list[PlaceRecord], osm: list[PlaceRecord]) -> tuple[list[PlaceRecord], list[dict]]` — trả danh sách đã ghép và danh sách dòng chờ xem tay, mỗi dòng có khóa `wikidata_id, osm_id, wikidata_name, osm_name, distance_m, reason`

Quy tắc: ghép khi khoảng cách < 150m **và** tên chuẩn hóa khớp. Đúng một điều kiện thì không ghép, đẩy vào danh sách xem tay. Khi ghép: tên và `wikidata_classes` lấy từ Wikidata; `opening_hours_raw`, `website`, `tags` lấy từ OSM; `external_ids` gộp cả hai.

`normalize_name` phải xử lý `đ` riêng vì `unicodedata` không tách được chữ này.

- [ ] **Step 1: Viết test**

`tests/test_merge.py`:

```python
from pipeline.config import CityConfig
from pipeline.models import PlaceRecord
from pipeline.normalize.merge import (
    haversine_m, merge_places, normalize_name, within_core,
)

CFG = CityConfig("Huế", (16.335, 107.435, 16.605, 107.725), (16.4698, 107.5796), 15.0)


def test_normalize_name_strips_accents_and_prefix():
    assert normalize_name("Chùa Thiên Mụ") == "thien mu"
    assert normalize_name("Lăng Tự Đức") == "tu duc"
    assert normalize_name("Đại Nội") == "dai noi"


def test_normalize_name_handles_d_with_stroke():
    assert "đ" not in normalize_name("Đền Huyền Trân")
    assert normalize_name("Đền Huyền Trân") == "huyen tran"


def test_haversine_known_distance():
    d = haversine_m(16.4698, 107.5796, 16.4698, 107.5896)
    assert 1000 < d < 1120


def test_within_core_rejects_far_place():
    near = PlaceRecord("A", {"osm": "node/1"}, 16.47, 107.58)
    far = PlaceRecord("B", {"osm": "node/2"}, 16.20, 107.20)
    assert within_core(near, CFG) is True
    assert within_core(far, CFG) is False


def test_merge_combines_matching_pair():
    wd = PlaceRecord("Chùa Thiên Mụ", {"wikidata": "Q1"}, 16.4539, 107.5453,
                     wikidata_classes=["Q16970"])
    osm = PlaceRecord("Chùa Thiên Mụ", {"osm": "way/9"}, 16.4540, 107.5454,
                      opening_hours_raw="Mo-Su 07:00-17:00", website="http://x.vn")
    merged, review = merge_places([wd], [osm])
    assert review == []
    [place] = merged
    assert place.external_ids == {"wikidata": "Q1", "osm": "way/9"}
    assert place.opening_hours_raw == "Mo-Su 07:00-17:00"
    assert place.website == "http://x.vn"
    assert place.wikidata_classes == ["Q16970"]


def test_close_but_different_name_goes_to_review():
    wd = PlaceRecord("Chùa Thiên Mụ", {"wikidata": "Q1"}, 16.4539, 107.5453)
    osm = PlaceRecord("Quán cà phê Thiên Mụ View", {"osm": "node/9"}, 16.4540, 107.5454)
    merged, review = merge_places([wd], [osm])
    assert len(merged) == 2
    assert len(review) == 1
    assert review[0]["reason"] == "gan_nhung_khac_ten"
    assert review[0]["distance_m"] < 150


def test_same_name_far_apart_goes_to_review():
    wd = PlaceRecord("Chợ Đông Ba", {"wikidata": "Q1"}, 16.4700, 107.5800)
    osm = PlaceRecord("Chợ Đông Ba", {"osm": "node/9"}, 16.5200, 107.6200)
    merged, review = merge_places([wd], [osm])
    assert len(merged) == 2
    assert review[0]["reason"] == "trung_ten_nhung_xa"
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_merge.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'pipeline.normalize.merge'`

- [ ] **Step 3: Viết `pipeline/normalize/merge.py`**

```python
import math
import re
import unicodedata
from dataclasses import replace

from pipeline.config import CityConfig
from pipeline.models import PlaceRecord

MATCH_RADIUS_M = 150.0

PREFIXES = (
    "chua", "den", "lang", "mieu", "nha tho", "bao tang", "cho", "cong vien",
    "khach san", "quan", "nha hang", "cau", "cua", "dien", "phu",
)


def normalize_name(name: str) -> str:
    text = name.lower().replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for prefix in sorted(PREFIXES, key=len, reverse=True):
        if text.startswith(prefix + " "):
            text = text[len(prefix) + 1:]
            break
    return text.strip()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def within_core(place: PlaceRecord, cfg: CityConfig) -> bool:
    lat, lon = cfg.core_center
    return haversine_m(lat, lon, place.lat, place.lon) <= cfg.core_radius_km * 1000


def merge_places(
    wikidata: list[PlaceRecord], osm: list[PlaceRecord]
) -> tuple[list[PlaceRecord], list[dict]]:
    merged: list[PlaceRecord] = []
    review: list[dict] = []
    used_osm: set[int] = set()

    for wd in wikidata:
        wd_key = normalize_name(wd.name)
        pair_index = None
        for i, candidate in enumerate(osm):
            if i in used_osm:
                continue
            distance = haversine_m(wd.lat, wd.lon, candidate.lat, candidate.lon)
            same_name = normalize_name(candidate.name) == wd_key
            if distance < MATCH_RADIUS_M and same_name:
                pair_index = i
                break
            if distance < MATCH_RADIUS_M or same_name:
                review.append({
                    "wikidata_id": wd.external_ids.get("wikidata", ""),
                    "osm_id": candidate.external_ids.get("osm", ""),
                    "wikidata_name": wd.name,
                    "osm_name": candidate.name,
                    "distance_m": round(distance, 1),
                    "reason": ("gan_nhung_khac_ten" if distance < MATCH_RADIUS_M
                               else "trung_ten_nhung_xa"),
                })

        if pair_index is None:
            merged.append(wd)
            continue

        partner = osm[pair_index]
        used_osm.add(pair_index)
        merged.append(replace(
            wd,
            external_ids={**wd.external_ids, **partner.external_ids},
            opening_hours_raw=partner.opening_hours_raw or wd.opening_hours_raw,
            website=partner.website or wd.website,
            tags={**partner.tags, **wd.tags},
        ))

    merged.extend(p for i, p in enumerate(osm) if i not in used_osm)
    return merged, review
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_merge.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add pipeline/normalize/merge.py tests/test_merge.py
git commit -m "feat: chuẩn hóa tên tiếng Việt và ghép trùng Wikidata với OSM"
```

---

## Task 10: Gán nhãn thời tiết ba lớp

**Files:**
- Create: `pipeline/normalize/weather_labels.py`, `config/weather_defaults.yml`, `config/overrides.csv`
- Test: `tests/test_weather_labels.py`

**Interfaces:**
- Consumes: `PlaceRecord`
- Produces:
  - `load_weather_defaults(path: str = "config/weather_defaults.yml") -> dict`
  - `load_overrides(path: str = "config/overrides.csv") -> dict[str, dict]` — khóa là `place_key`
  - `apply_labels(place: PlaceRecord, defaults: dict, overrides: dict) -> PlaceRecord`

Ba lớp theo thứ tự: mặc định theo category → điều chỉnh `indoor_ratio` theo tag OSM → override từ CSV (đặt `label_source = "manual"`).

- [ ] **Step 1: Viết test**

`tests/test_weather_labels.py`:

```python
from pipeline.models import PlaceRecord
from pipeline.normalize.weather_labels import apply_labels, load_weather_defaults

DEFAULTS = {
    "bao_tang": {"indoor_ratio": 0.95, "avg_visit_minutes": 60,
                 "weather_sensitivity": {"rain": 0.1, "heat": 0.1, "wind": 0.0},
                 "best_time_of_day": ["bat_ky"], "unsafe_conditions": []},
    "lang_tam": {"indoor_ratio": 0.25, "avg_visit_minutes": 75,
                 "weather_sensitivity": {"rain": 0.8, "heat": 0.7, "wind": 0.2},
                 "best_time_of_day": ["sang_som"], "unsafe_conditions": ["mua_lon"]},
    "khac": {"indoor_ratio": 0.5, "avg_visit_minutes": 45,
             "weather_sensitivity": {"rain": 0.5, "heat": 0.5, "wind": 0.2},
             "best_time_of_day": ["bat_ky"], "unsafe_conditions": []},
}


def test_layer1_applies_category_defaults():
    place = PlaceRecord("Bảo tàng", {"osm": "node/1"}, 16.4, 107.5, category="bao_tang")
    out = apply_labels(place, DEFAULTS, {})
    assert out.indoor_ratio == 0.95
    assert out.avg_visit_minutes == 60
    assert out.unsafe_conditions == []
    assert out.label_source == "default"


def test_layer1_unknown_category_uses_khac():
    place = PlaceRecord("X", {"osm": "node/2"}, 16.4, 107.5, category="khong_co_that")
    assert apply_labels(place, DEFAULTS, {}).indoor_ratio == 0.5


def test_layer2_building_tag_raises_indoor_ratio():
    place = PlaceRecord("Lăng", {"osm": "node/3"}, 16.4, 107.5,
                        category="lang_tam", tags={"building": "yes"})
    assert apply_labels(place, DEFAULTS, {}).indoor_ratio == 0.9


def test_layer2_park_tag_lowers_indoor_ratio():
    place = PlaceRecord("Công viên", {"osm": "node/4"}, 16.4, 107.5,
                        category="bao_tang", tags={"leisure": "park"})
    assert apply_labels(place, DEFAULTS, {}).indoor_ratio == 0.05


def test_layer2_does_not_touch_unsafe_conditions():
    place = PlaceRecord("Lăng", {"osm": "node/5"}, 16.4, 107.5,
                        category="lang_tam", tags={"building": "yes"})
    assert apply_labels(place, DEFAULTS, {}).unsafe_conditions == ["mua_lon"]


def test_layer3_override_wins_and_marks_manual():
    place = PlaceRecord("Lăng Tự Đức", {"wikidata": "Q1"}, 16.4, 107.5,
                        category="lang_tam", tags={"building": "yes"})
    overrides = {"wikidata:Q1": {"indoor_ratio": 0.15, "avg_visit_minutes": 90,
                                 "unsafe_conditions": ["mua_lon", "bao"]}}
    out = apply_labels(place, DEFAULTS, overrides)
    assert out.indoor_ratio == 0.15
    assert out.avg_visit_minutes == 90
    assert out.unsafe_conditions == ["mua_lon", "bao"]
    assert out.label_source == "manual"


def test_defaults_file_covers_every_category_in_categories_yml():
    from pipeline.normalize.category import load_category_rules

    defaults = load_weather_defaults("config/weather_defaults.yml")
    rules = load_category_rules("config/categories.yml")
    used = {r["category"] for r in rules["osm"]} | set(rules["wikidata"].values())
    assert used | {"khac"} <= set(defaults)
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_weather_labels.py -v`
Expected: FAIL với `ModuleNotFoundError`

- [ ] **Step 3: Viết `config/weather_defaults.yml`**

Phải có đủ mọi category xuất hiện trong `config/categories.yml`, cộng `khac`.

```yaml
di_tich:
  { indoor_ratio: 0.3, avg_visit_minutes: 90,
    weather_sensitivity: { rain: 0.8, heat: 0.7, wind: 0.2 },
    best_time_of_day: [sang_som, chieu_muon], unsafe_conditions: [mua_lon] }
chua:
  { indoor_ratio: 0.5, avg_visit_minutes: 45,
    weather_sensitivity: { rain: 0.6, heat: 0.5, wind: 0.2 },
    best_time_of_day: [sang_som], unsafe_conditions: [mua_lon] }
lang_tam:
  { indoor_ratio: 0.25, avg_visit_minutes: 75,
    weather_sensitivity: { rain: 0.8, heat: 0.7, wind: 0.2 },
    best_time_of_day: [sang_som, chieu_muon], unsafe_conditions: [mua_lon] }
bao_tang:
  { indoor_ratio: 0.95, avg_visit_minutes: 60,
    weather_sensitivity: { rain: 0.1, heat: 0.1, wind: 0.0 },
    best_time_of_day: [bat_ky], unsafe_conditions: [] }
diem_tham_quan:
  { indoor_ratio: 0.4, avg_visit_minutes: 60,
    weather_sensitivity: { rain: 0.6, heat: 0.5, wind: 0.3 },
    best_time_of_day: [bat_ky], unsafe_conditions: [mua_lon] }
diem_ngam_canh:
  { indoor_ratio: 0.05, avg_visit_minutes: 30,
    weather_sensitivity: { rain: 0.9, heat: 0.6, wind: 0.5 },
    best_time_of_day: [hoang_hon], unsafe_conditions: [mua_lon, bao] }
cong_vien:
  { indoor_ratio: 0.05, avg_visit_minutes: 45,
    weather_sensitivity: { rain: 0.9, heat: 0.8, wind: 0.3 },
    best_time_of_day: [sang_som, chieu_muon], unsafe_conditions: [mua_lon, bao] }
bai_bien:
  { indoor_ratio: 0.0, avg_visit_minutes: 120,
    weather_sensitivity: { rain: 0.9, heat: 0.5, wind: 0.8 },
    best_time_of_day: [sang_som, chieu_muon], unsafe_conditions: [bao, song_lon, mua_lon] }
song:
  { indoor_ratio: 0.1, avg_visit_minutes: 60,
    weather_sensitivity: { rain: 0.7, heat: 0.5, wind: 0.8 },
    best_time_of_day: [hoang_hon], unsafe_conditions: [bao, mua_lon] }
nha_hang:
  { indoor_ratio: 0.8, avg_visit_minutes: 60,
    weather_sensitivity: { rain: 0.2, heat: 0.2, wind: 0.1 },
    best_time_of_day: [bat_ky], unsafe_conditions: [] }
quan_ca_phe:
  { indoor_ratio: 0.7, avg_visit_minutes: 45,
    weather_sensitivity: { rain: 0.3, heat: 0.2, wind: 0.1 },
    best_time_of_day: [bat_ky], unsafe_conditions: [] }
khac:
  { indoor_ratio: 0.5, avg_visit_minutes: 45,
    weather_sensitivity: { rain: 0.5, heat: 0.5, wind: 0.2 },
    best_time_of_day: [bat_ky], unsafe_conditions: [] }
```

- [ ] **Step 4: Viết `config/overrides.csv`**

Header cộng vài dòng mẫu. Cột trống nghĩa là không đè trường đó. `unsafe_conditions` và `best_time_of_day` phân tách bằng `|`.

```csv
place_key,indoor_ratio,avg_visit_minutes,best_time_of_day,unsafe_conditions,ticket_price_vnd,dress_code,note
wikidata:Q1140380,0.2,180,sang_som|chieu_muon,mua_lon,200000,,Đại Nội đi bộ nhiều sân lộ thiên
```

`place_key` lấy từ cột cùng tên trong `data/qa/sample_*.csv` hoặc từ `data/staged/*.json`.

- [ ] **Step 5: Viết `pipeline/normalize/weather_labels.py`**

```python
import csv
from dataclasses import replace

import yaml

INDOOR_TAG_RULES = [
    ({"building": "yes"}, 0.9),
    ({"indoor": "yes"}, 0.9),
    ({"covered": "yes"}, 0.6),
    ({"leisure": "park"}, 0.05),
    ({"leisure": "garden"}, 0.05),
    ({"natural": "*"}, 0.05),
]


def load_weather_defaults(path: str = "config/weather_defaults.yml") -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_overrides(path: str = "config/overrides.csv") -> dict[str, dict]:
    result: dict[str, dict] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("place_key") or "").strip()
            if not key:
                continue
            entry: dict = {}
            if row.get("indoor_ratio"):
                entry["indoor_ratio"] = float(row["indoor_ratio"])
            if row.get("avg_visit_minutes"):
                entry["avg_visit_minutes"] = int(row["avg_visit_minutes"])
            if row.get("best_time_of_day"):
                entry["best_time_of_day"] = row["best_time_of_day"].split("|")
            if row.get("unsafe_conditions"):
                entry["unsafe_conditions"] = row["unsafe_conditions"].split("|")
            if row.get("ticket_price_vnd"):
                entry["ticket_price"] = {"vnd": int(row["ticket_price_vnd"])}
            if row.get("dress_code"):
                entry["dress_code"] = row["dress_code"]
            result[key] = entry
    return result


def _indoor_from_tags(tags: dict) -> float | None:
    for match, value in INDOOR_TAG_RULES:
        for key, expected in match.items():
            tag = tags.get(key)
            if tag is not None and (expected == "*" or tag == expected):
                return value
    return None


def apply_labels(place, defaults: dict, overrides: dict):
    base = defaults.get(place.category) or defaults["khac"]
    updated = replace(
        place,
        indoor_ratio=base["indoor_ratio"],
        avg_visit_minutes=base["avg_visit_minutes"],
        weather_sensitivity=dict(base["weather_sensitivity"]),
        best_time_of_day=list(base["best_time_of_day"]),
        unsafe_conditions=list(base["unsafe_conditions"]),
        label_source="default",
    )

    from_tags = _indoor_from_tags(place.tags)
    if from_tags is not None:
        updated = replace(updated, indoor_ratio=from_tags)

    for key in place.external_ids:
        entry = overrides.get(f"{key}:{place.external_ids[key]}")
        if entry:
            updated = replace(updated, label_source="manual", **entry)
            break
    return updated
```

- [ ] **Step 6: Chạy test**

Run: `python -m pytest tests/test_weather_labels.py -v`
Expected: PASS (7 passed)

- [ ] **Step 7: Commit**

```bash
git add pipeline/normalize/weather_labels.py config/weather_defaults.yml config/overrides.csv tests/test_weather_labels.py
git commit -m "feat: gán nhãn thời tiết ba lớp cho địa điểm"
```

---

## Task 11: Upsert idempotent và lệnh `normalize`

**Files:**
- Create: `pipeline/load/__init__.py`, `pipeline/load/upsert.py`, `pipeline/normalize/pipeline.py`
- Modify: `pipeline/cli.py` (thêm lệnh `normalize` và `load`)
- Test: `tests/test_upsert.py`

**Interfaces:**
- Consumes: `PlaceRecord`, các hàm normalize ở Task 7–10
- Produces:
  - `pipeline.load.upsert.upsert_places(conn, places: list[PlaceRecord]) -> tuple[int, int]` trả `(số thêm mới, số cập nhật)`
  - `pipeline.normalize.pipeline.run(conn, cfg) -> tuple[list[PlaceRecord], list[dict]]` — đọc `data/staged/*.json`, chạy lọc lõi, ghép trùng, map category, parse `opening_hours`, gán nhãn; ghi `data/qa/merge_review.csv`

- [ ] **Step 1: Viết test**

`tests/test_upsert.py`:

```python
from pipeline.load.upsert import upsert_places
from pipeline.models import PlaceRecord


def _place(**kw):
    base = dict(name="Chùa Thiên Mụ", external_ids={"wikidata": "Q1"},
                lat=16.4539, lon=107.5453, category="chua",
                indoor_ratio=0.5, avg_visit_minutes=45,
                weather_sensitivity={"rain": 0.6, "heat": 0.5, "wind": 0.2},
                best_time_of_day=["sang_som"], unsafe_conditions=["mua_lon"])
    base.update(kw)
    return PlaceRecord(**base)


def test_upsert_inserts_then_updates(db_conn):
    inserted, updated = upsert_places(db_conn, [_place()])
    assert (inserted, updated) == (1, 0)

    inserted, updated = upsert_places(db_conn, [_place(avg_visit_minutes=60)])
    assert (inserted, updated) == (0, 1)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(avg_visit_minutes) FROM places")
        assert cur.fetchone() == (1, 60)


def test_upsert_links_all_external_ids(db_conn):
    upsert_places(db_conn, [_place(external_ids={"wikidata": "Q1", "osm": "way/9"})])
    with db_conn.cursor() as cur:
        cur.execute("SELECT source, external_id FROM place_external_ids ORDER BY source")
        assert cur.fetchall() == [("osm", "way/9"), ("wikidata", "Q1")]


def test_upsert_matches_existing_place_via_any_external_id(db_conn):
    upsert_places(db_conn, [_place(external_ids={"osm": "way/9"})])
    upsert_places(db_conn, [_place(external_ids={"wikidata": "Q1", "osm": "way/9"})])
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM places")
        assert cur.fetchone()[0] == 1


def test_upsert_writes_geography_point(db_conn):
    upsert_places(db_conn, [_place()])
    with db_conn.cursor() as cur:
        cur.execute("SELECT ST_Y(location::geometry), ST_X(location::geometry) FROM places")
        lat, lon = cur.fetchone()
        assert round(lat, 4) == 16.4539
        assert round(lon, 4) == 107.5453
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_upsert.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'pipeline.load'`

- [ ] **Step 3: Viết `pipeline/load/upsert.py`**

```python
import json

from pipeline.models import PlaceRecord

COLUMNS = """
  name, name_en, category, location, opening_hours, opening_hours_raw,
  ticket_price, dress_code, avg_visit_minutes, indoor_ratio,
  weather_sensitivity, best_time_of_day, unsafe_conditions, label_source,
  website, source_url, updated_at
"""


def _find_place_id(cur, external_ids: dict[str, str]) -> int | None:
    for source, external_id in external_ids.items():
        cur.execute(
            "SELECT place_id FROM place_external_ids WHERE source = %s AND external_id = %s",
            (source, external_id),
        )
        row = cur.fetchone()
        if row:
            return row[0]
    return None


def upsert_places(conn, places: list[PlaceRecord]) -> tuple[int, int]:
    inserted = updated = 0
    with conn.cursor() as cur:
        for place in places:
            values = (
                place.name, place.name_en, place.category,
                place.lon, place.lat,
                json.dumps(place.opening_hours) if place.opening_hours else None,
                place.opening_hours_raw,
                json.dumps(place.ticket_price) if place.ticket_price else None,
                place.dress_code, place.avg_visit_minutes, place.indoor_ratio,
                json.dumps(place.weather_sensitivity) if place.weather_sensitivity else None,
                place.best_time_of_day, place.unsafe_conditions, place.label_source,
                place.website, place.source_url,
            )
            place_id = _find_place_id(cur, place.external_ids)
            if place_id is None:
                cur.execute(
                    f"INSERT INTO places ({COLUMNS}) VALUES"
                    " (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,"
                    "  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())"
                    " RETURNING id",
                    values,
                )
                place_id = cur.fetchone()[0]
                inserted += 1
            else:
                cur.execute(
                    "UPDATE places SET name = %s, name_en = %s, category = %s,"
                    " location = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,"
                    " opening_hours = %s, opening_hours_raw = %s, ticket_price = %s,"
                    " dress_code = %s, avg_visit_minutes = %s, indoor_ratio = %s,"
                    " weather_sensitivity = %s, best_time_of_day = %s,"
                    " unsafe_conditions = %s, label_source = %s, website = %s,"
                    " source_url = %s, updated_at = now() WHERE id = %s",
                    values + (place_id,),
                )
                updated += 1

            for source, external_id in place.external_ids.items():
                cur.execute(
                    "INSERT INTO place_external_ids (place_id, source, external_id)"
                    " VALUES (%s, %s, %s) ON CONFLICT (source, external_id) DO NOTHING",
                    (place_id, source, external_id),
                )
    return inserted, updated
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_upsert.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Viết `pipeline/normalize/pipeline.py`**

```python
import csv
import json
from pathlib import Path

from pipeline.models import PlaceRecord
from pipeline.normalize.category import load_category_rules, map_category
from pipeline.normalize.merge import merge_places, within_core
from pipeline.normalize.opening_hours import parse_opening_hours
from pipeline.normalize.weather_labels import (
    apply_labels, load_overrides, load_weather_defaults,
)

MERGE_REVIEW_PATH = "data/qa/merge_review.csv"


def _load_staged(path: str) -> list[PlaceRecord]:
    file = Path(path)
    if not file.exists():
        return []
    return [PlaceRecord(**row) for row in json.loads(file.read_text(encoding="utf-8"))]


def run(conn, cfg) -> tuple[list[PlaceRecord], list[dict]]:
    wikidata = [p for p in _load_staged("data/staged/wikidata.json") if within_core(p, cfg)]
    osm = [p for p in _load_staged("data/staged/osm.json") if within_core(p, cfg)]
    places, review = merge_places(wikidata, osm)

    rules = load_category_rules()
    defaults = load_weather_defaults()
    overrides = load_overrides()

    result = []
    for place in places:
        place.category = map_category(place.tags, place.wikidata_classes, rules)
        place.opening_hours = parse_opening_hours(place.opening_hours_raw)
        result.append(apply_labels(place, defaults, overrides))

    Path(MERGE_REVIEW_PATH).parent.mkdir(parents=True, exist_ok=True)
    fields = ["wikidata_id", "osm_id", "wikidata_name", "osm_name", "distance_m", "reason"]
    with open(MERGE_REVIEW_PATH, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(review)
    return result, review
```

- [ ] **Step 6: Nối `normalize` và `load` vào CLI**

Trong `pipeline/cli.py` thêm `sub.add_parser("normalize")`, `sub.add_parser("load")` và nhánh:

```python
if args.command in {"normalize", "load"}:
    from pipeline.config import load_city
    from pipeline.load.upsert import upsert_places
    from pipeline.normalize import pipeline as normalize_pipeline

    conn = db.connect()
    cfg = load_city()
    places, review = normalize_pipeline.run(conn, cfg)
    print(f"normalize: {len(places)} địa điểm, {len(review)} cặp chờ xem tay")
    if args.command == "load":
        inserted, updated = upsert_places(conn, places)
        print(f"load: thêm {inserted}, cập nhật {updated}")
```

- [ ] **Step 7: Chạy thật và kiểm tra tính idempotent**

Run:
```bash
python -m pipeline.cli load
python -m pipeline.cli load
docker compose exec db psql -U travel -d travel -c "SELECT category, count(*) FROM places GROUP BY category ORDER BY 2 DESC;"
```
Expected: lần chạy thứ hai in `thêm 0`, tổng số dòng không đổi.

- [ ] **Step 8: Commit**

```bash
git add pipeline/load pipeline/normalize/pipeline.py pipeline/cli.py tests/test_upsert.py
git commit -m "feat: upsert idempotent và nối tầng normalize vào CLI"
```

---

## Task 12: Ingest văn bản Wikipedia và Wikivoyage thành `place_chunks`

**Files:**
- Create: `pipeline/ingest/wikipedia.py`
- Modify: `pipeline/cli.py`
- Test: `tests/test_ingest_wikipedia.py`

**Interfaces:**
- Consumes: `Fetcher`, `place_external_ids`
- Produces:
  - `chunk_text(text: str, max_chars: int = 1200) -> list[str]` — cắt theo đoạn, không cắt giữa câu
  - `pipeline.ingest.wikipedia.run(conn, cfg, force=False) -> int` trả số chunk đã ghi

Chỉ lấy bài cho địa điểm có `tags["vi_title"]` (tức có bài Wikipedia tiếng Việt). Chunk ghi vào `place_chunks` với `content_hash` để chạy lại không trùng. Cột `embedding` để `NULL`.

- [ ] **Step 1: Viết test**

`tests/test_ingest_wikipedia.py`:

```python
from pipeline.ingest.wikipedia import chunk_text


def test_chunk_text_keeps_paragraphs_together():
    text = "Đoạn một.\n\nĐoạn hai dài hơn một chút.\n\nĐoạn ba."
    chunks = chunk_text(text, max_chars=40)
    assert all(len(c) <= 40 for c in chunks)
    assert "Đoạn một." in chunks[0]


def test_chunk_text_splits_long_paragraph_on_sentence_boundary():
    text = "Câu một rất dài. " * 20
    chunks = chunk_text(text, max_chars=100)
    assert len(chunks) > 1
    assert all(c.endswith(".") for c in chunks)


def test_chunk_text_drops_empty_input():
    assert chunk_text("   ") == []
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_ingest_wikipedia.py -v`
Expected: FAIL với `ModuleNotFoundError`

- [ ] **Step 3: Viết `pipeline/ingest/wikipedia.py`**

```python
import hashlib
import json
import re

from pipeline.http import Fetcher

API_URL = "https://vi.wikipedia.org/w/api.php"


def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    chunks: list[str] = []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        current = ""
        for sentence in re.split(r"(?<=\.)\s+", para):
            if len(current) + len(sentence) + 1 > max_chars and current:
                chunks.append(current.strip())
                current = ""
            current += sentence + " "
        if current.strip():
            chunks.append(current.strip())
    return chunks


def _titles_by_place(conn) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.name FROM places p"
            " JOIN place_external_ids e ON e.place_id = p.id"
            " WHERE e.source = 'wikidata'"
        )
        return cur.fetchall()


def run(conn, cfg, force: bool = False) -> int:
    fetcher = Fetcher(conn, "wikipedia", min_interval=1.0)
    written = 0
    for place_id, title in _titles_by_place(conn):
        res = fetcher.fetch(
            API_URL,
            params={"action": "query", "prop": "extracts", "explaintext": "1",
                    "redirects": "1", "format": "json", "titles": title},
            force=force,
        )
        pages = json.loads(res.content).get("query", {}).get("pages", {})
        for page in pages.values():
            extract = page.get("extract")
            if not extract:
                continue
            url = f"https://vi.wikipedia.org/wiki/{page['title'].replace(' ', '_')}"
            for chunk in chunk_text(extract):
                digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO place_chunks (place_id, content, content_hash, source_url)"
                        " VALUES (%s, %s, %s, %s)"
                        " ON CONFLICT (place_id, content_hash) DO NOTHING",
                        (place_id, chunk, digest, url),
                    )
                    written += cur.rowcount
    return written
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_ingest_wikipedia.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Chạy thật**

Thêm `"wikipedia": "pipeline.ingest.wikipedia"` vào `INGEST_MODULES`, chạy sau `load`:
`python -m pipeline.cli ingest --source wikipedia`
Expected: vài trăm đến vài nghìn chunk. Chạy lần hai in `0 bản ghi`.

- [ ] **Step 6: Commit**

```bash
git add pipeline/ingest/wikipedia.py pipeline/cli.py tests/test_ingest_wikipedia.py
git commit -m "feat: nạp văn bản Wikipedia tiếng Việt thành place_chunks"
```

---

## Task 13: Ingest ảnh Wikimedia Commons

**Files:**
- Create: `pipeline/ingest/commons.py`
- Modify: `pipeline/cli.py`
- Test: `tests/test_ingest_commons.py`

**Interfaces:**
- Consumes: `Fetcher`, `places`, `place_external_ids`
- Produces:
  - `parse_imageinfo(payload: dict) -> list[ImageRecord]` với `place_key` để rỗng, hàm gọi điền sau
  - `pipeline.ingest.commons.run(conn, cfg, force=False) -> int` trả số ảnh ghi mới, tối đa 30 ảnh mỗi địa danh

Chỉ lưu URL, license và tác giả — không tải file ảnh về ở giai đoạn này. Tải file là việc của tính năng nhận diện.

- [ ] **Step 1: Viết test**

`tests/test_ingest_commons.py`:

```python
from pipeline.ingest.commons import parse_imageinfo


def test_parse_imageinfo_reads_url_license_author():
    payload = {"query": {"pages": {"1": {"title": "File:A.jpg", "imageinfo": [{
        "url": "https://upload.wikimedia.org/a.jpg",
        "extmetadata": {
            "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "Artist": {"value": "<a href='x'>Nguyễn Văn A</a>"},
        },
    }]}}}}
    [image] = parse_imageinfo(payload)
    assert image.image_url == "https://upload.wikimedia.org/a.jpg"
    assert image.license == "CC BY-SA 4.0"
    assert image.author == "Nguyễn Văn A"


def test_parse_imageinfo_skips_pages_without_imageinfo():
    assert parse_imageinfo({"query": {"pages": {"1": {"title": "File:B.jpg"}}}}) == []


def test_parse_imageinfo_handles_missing_metadata():
    payload = {"query": {"pages": {"1": {"imageinfo": [
        {"url": "https://upload.wikimedia.org/c.jpg", "extmetadata": {}}
    ]}}}}
    [image] = parse_imageinfo(payload)
    assert image.license is None
    assert image.author is None
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_ingest_commons.py -v`
Expected: FAIL với `ModuleNotFoundError`

- [ ] **Step 3: Viết `pipeline/ingest/commons.py`**

```python
import json

from selectolax.parser import HTMLParser

from pipeline.http import Fetcher
from pipeline.models import ImageRecord

API_URL = "https://commons.wikimedia.org/w/api.php"
MAX_IMAGES = 30


def _plain(value: str | None) -> str | None:
    if not value:
        return None
    text = HTMLParser(value).text().strip()
    return text or None


def parse_imageinfo(payload: dict) -> list[ImageRecord]:
    images = []
    for page in payload.get("query", {}).get("pages", {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata", {})
        images.append(ImageRecord(
            place_key="",
            image_url=url,
            license=_plain(meta.get("LicenseShortName", {}).get("value")),
            author=_plain(meta.get("Artist", {}).get("value")),
        ))
    return images


def _places_from_wikidata(conn) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.name FROM places p"
            " JOIN place_external_ids e ON e.place_id = p.id"
            " WHERE e.source = 'wikidata'"
        )
        return cur.fetchall()


def run(conn, cfg, force: bool = False) -> int:
    fetcher = Fetcher(conn, "commons", min_interval=1.0)
    written = 0
    for place_id, name in _places_from_wikidata(conn):
        res = fetcher.fetch(
            API_URL,
            params={"action": "query", "generator": "search",
                    "gsrsearch": f"{name} filetype:bitmap",
                    "gsrnamespace": "6", "gsrlimit": str(MAX_IMAGES),
                    "prop": "imageinfo", "iiprop": "url|extmetadata",
                    "format": "json"},
            force=force,
        )
        for image in parse_imageinfo(json.loads(res.content)):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO place_images (place_id, image_url, license, author)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT (image_url) DO NOTHING",
                    (place_id, image.image_url, image.license, image.author),
                )
                written += cur.rowcount
    return written
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_ingest_commons.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Chạy thật**

Thêm `"commons": "pipeline.ingest.commons"` rồi `python -m pipeline.cli ingest --source commons`.
Expected: vài nghìn ảnh. Tìm theo tên nên có nhiễu — Task 15 báo cáo tỷ lệ địa danh đủ 5 ảnh, và mẫu kiểm duyệt cho thấy mức nhiễu.

- [ ] **Step 6: Commit**

```bash
git add pipeline/ingest/commons.py pipeline/cli.py tests/test_ingest_commons.py
git commit -m "feat: nạp ảnh Wikimedia Commons kèm license và tác giả"
```

---

## Task 14: Ingest thời tiết và khí hậu Open-Meteo

**Files:**
- Create: `pipeline/ingest/weather.py`
- Modify: `pipeline/cli.py`
- Test: `tests/test_ingest_weather.py`

**Interfaces:**
- Consumes: `CityConfig`, `Fetcher`
- Produces:
  - `grid_points(cfg: CityConfig, step: float = 0.1) -> list[tuple[float, float]]` — tọa độ đã làm tròn về bội số của `step`
  - `parse_forecast(payload: dict) -> list[dict]` với khóa `forecast_time, temperature, precip_prob, precip_mm, wind_speed, uv_index, weather_code`
  - `parse_normals(payload: dict) -> list[dict]` với khóa `month, temp_avg, precip_mm_avg, rain_days, wind_avg`
  - `pipeline.ingest.weather.run(conn, cfg, force=False) -> int`

- [ ] **Step 1: Viết test**

`tests/test_ingest_weather.py`:

```python
from pipeline.config import CityConfig
from pipeline.ingest.weather import grid_points, parse_forecast, parse_normals

CFG = CityConfig("Huế", (16.335, 107.435, 16.605, 107.725), (16.4698, 107.5796), 15.0)


def test_grid_points_are_rounded_to_step():
    points = grid_points(CFG, step=0.1)
    assert points
    assert all(round(lat * 10) == lat * 10 for lat, _ in points)
    assert all(16.3 <= lat <= 16.7 for lat, _ in points)


def test_parse_forecast_rows():
    payload = {"hourly": {
        "time": ["2026-09-22T00:00", "2026-09-22T01:00"],
        "temperature_2m": [27.0, 26.5],
        "precipitation_probability": [30, 40],
        "precipitation": [0.0, 1.2],
        "wind_speed_10m": [9.0, 11.0],
        "uv_index": [0.0, 0.0],
        "weather_code": [2, 61],
    }}
    rows = parse_forecast(payload)
    assert len(rows) == 2
    assert rows[1]["precip_mm"] == 1.2
    assert rows[1]["weather_code"] == 61


def test_parse_normals_aggregates_by_month():
    payload = {"daily": {
        "time": ["2020-01-01", "2020-01-02", "2020-02-01"],
        "temperature_2m_mean": [20.0, 22.0, 24.0],
        "precipitation_sum": [0.0, 10.0, 5.0],
        "wind_speed_10m_max": [10.0, 12.0, 8.0],
    }}
    rows = {r["month"]: r for r in parse_normals(payload)}
    assert rows[1]["temp_avg"] == 21.0
    assert rows[1]["precip_mm_avg"] == 5.0
    assert rows[1]["rain_days"] == 0.5
    assert rows[2]["wind_avg"] == 8.0
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_ingest_weather.py -v`
Expected: FAIL với `ModuleNotFoundError`

- [ ] **Step 3: Viết `pipeline/ingest/weather.py`**

```python
import json
from collections import defaultdict

from pipeline.config import CityConfig
from pipeline.http import Fetcher

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = ("temperature_2m,precipitation_probability,precipitation,"
          "wind_speed_10m,uv_index,weather_code")
DAILY = "temperature_2m_mean,precipitation_sum,wind_speed_10m_max"
RAIN_DAY_MM = 1.0


def grid_points(cfg: CityConfig, step: float = 0.1) -> list[tuple[float, float]]:
    south, west, north, east = cfg.bbox
    points = []
    lat = round(round(south / step) * step, 1)
    while lat <= north + step / 2:
        lon = round(round(west / step) * step, 1)
        while lon <= east + step / 2:
            points.append((lat, lon))
            lon = round(lon + step, 1)
        lat = round(lat + step, 1)
    return points


def parse_forecast(payload: dict) -> list[dict]:
    h = payload["hourly"]
    rows = []
    for i, stamp in enumerate(h["time"]):
        rows.append({
            "forecast_time": stamp,
            "temperature": h["temperature_2m"][i],
            "precip_prob": h["precipitation_probability"][i],
            "precip_mm": h["precipitation"][i],
            "wind_speed": h["wind_speed_10m"][i],
            "uv_index": h["uv_index"][i],
            "weather_code": h["weather_code"][i],
        })
    return rows


def parse_normals(payload: dict) -> list[dict]:
    d = payload["daily"]
    buckets: dict[int, dict[str, list]] = defaultdict(
        lambda: {"temp": [], "precip": [], "wind": []}
    )
    for i, stamp in enumerate(d["time"]):
        month = int(stamp[5:7])
        buckets[month]["temp"].append(d["temperature_2m_mean"][i])
        buckets[month]["precip"].append(d["precipitation_sum"][i])
        buckets[month]["wind"].append(d["wind_speed_10m_max"][i])

    rows = []
    for month, values in sorted(buckets.items()):
        precip = values["precip"]
        rows.append({
            "month": month,
            "temp_avg": sum(values["temp"]) / len(values["temp"]),
            "precip_mm_avg": sum(precip) / len(precip),
            "rain_days": sum(1 for v in precip if v >= RAIN_DAY_MM) / len(precip),
            "wind_avg": sum(values["wind"]) / len(values["wind"]),
        })
    return rows


def run(conn, cfg: CityConfig, force: bool = False) -> int:
    fetcher = Fetcher(conn, "open_meteo", min_interval=1.0)
    written = 0
    for lat, lon in grid_points(cfg):
        forecast = fetcher.fetch(
            FORECAST_URL,
            params={"latitude": lat, "longitude": lon, "hourly": HOURLY,
                    "forecast_days": 16, "timezone": "Asia/Bangkok"},
            force=force,
        )
        for row in parse_forecast(json.loads(forecast.content)):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO weather_cache (lat_grid, lon_grid, forecast_time,"
                    " temperature, precip_prob, precip_mm, wind_speed, uv_index,"
                    " weather_code, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())"
                    " ON CONFLICT (lat_grid, lon_grid, forecast_time) DO UPDATE SET"
                    " temperature = EXCLUDED.temperature,"
                    " precip_prob = EXCLUDED.precip_prob,"
                    " precip_mm = EXCLUDED.precip_mm,"
                    " wind_speed = EXCLUDED.wind_speed,"
                    " uv_index = EXCLUDED.uv_index,"
                    " weather_code = EXCLUDED.weather_code, fetched_at = now()",
                    (lat, lon, row["forecast_time"], row["temperature"],
                     row["precip_prob"], row["precip_mm"], row["wind_speed"],
                     row["uv_index"], row["weather_code"]),
                )
                written += 1

        archive = fetcher.fetch(
            ARCHIVE_URL,
            params={"latitude": lat, "longitude": lon, "start_date": "1991-01-01",
                    "end_date": "2020-12-31", "daily": DAILY, "timezone": "Asia/Bangkok"},
            force=force,
        )
        for row in parse_normals(json.loads(archive.content)):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO climate_normals (lat_grid, lon_grid, month, temp_avg,"
                    " precip_mm_avg, rain_days, wind_avg) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (lat_grid, lon_grid, month) DO UPDATE SET"
                    " temp_avg = EXCLUDED.temp_avg,"
                    " precip_mm_avg = EXCLUDED.precip_mm_avg,"
                    " rain_days = EXCLUDED.rain_days, wind_avg = EXCLUDED.wind_avg",
                    (lat, lon, row["month"], row["temp_avg"], row["precip_mm_avg"],
                     row["rain_days"], row["wind_avg"]),
                )
                written += 1
    return written
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_ingest_weather.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Chạy thật**

Thêm `"weather": "pipeline.ingest.weather"` rồi `python -m pipeline.cli ingest --source weather`.
Expected: khoảng 12 điểm lưới × (384 giờ + 12 tháng). Lần chạy `--force` sau này ghi đè, không nhân dòng.

- [ ] **Step 6: Commit**

```bash
git add pipeline/ingest/weather.py pipeline/cli.py tests/test_ingest_weather.py
git commit -m "feat: nạp dự báo Open-Meteo và khí hậu trung bình theo tháng"
```

---

## Task 15: Báo cáo QA

**Files:**
- Create: `pipeline/qa/__init__.py`, `pipeline/qa/coverage.py`
- Modify: `pipeline/cli.py` (thêm lệnh `qa`)
- Test: `tests/test_coverage.py`

**Interfaces:**
- Consumes: DB đã nạp
- Produces:
  - `Metric(name: str, value: float, threshold: float, passed: bool)`
  - `collect_metrics(conn, cfg) -> list[Metric]`
  - `render_report(metrics: list[Metric], merge_review_rows: int) -> str`
  - `run(conn, cfg) -> list[Metric]` — ghi `data/qa/coverage_<YYYY-MM-DD>.md` và `data/qa/sample_<YYYY-MM-DD>.csv` (20 bản ghi ngẫu nhiên), in cảnh báo cho từng chỉ số fail

Ngưỡng lấy đúng từ spec mục 7.

- [ ] **Step 1: Viết test**

`tests/test_coverage.py`:

```python
from pipeline.config import CityConfig
from pipeline.load.upsert import upsert_places
from pipeline.models import PlaceRecord
from pipeline.qa.coverage import Metric, collect_metrics, render_report

CFG = CityConfig("Huế", (16.335, 107.435, 16.605, 107.725), (16.4698, 107.5796), 15.0)


def test_render_report_marks_failures():
    metrics = [
        Metric("Tọa độ hợp lệ", 1.0, 0.98, True),
        Metric("Category xác định được", 0.5, 0.90, False),
    ]
    text = render_report(metrics, merge_review_rows=3)
    assert "Category xác định được" in text
    assert "FAIL" in text
    assert "3" in text


def test_collect_metrics_counts_category_coverage(db_conn):
    upsert_places(db_conn, [
        PlaceRecord("A", {"osm": "node/1"}, 16.47, 107.58, category="chua"),
        PlaceRecord("B", {"osm": "node/2"}, 16.47, 107.58, category="khac"),
    ])
    by_name = {m.name: m for m in collect_metrics(db_conn, CFG)}
    assert by_name["Category xác định được"].value == 0.5
    assert by_name["Category xác định được"].passed is False
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_coverage.py -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'pipeline.qa'`

- [ ] **Step 3: Viết `pipeline/qa/coverage.py`**

```python
import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

QA_DIR = Path("data/qa")
SAMPLE_SIZE = 20


@dataclass
class Metric:
    name: str
    value: float
    threshold: float
    passed: bool


def _ratio(cur, numerator_sql: str, denominator_sql: str) -> float:
    cur.execute(f"SELECT count(*) FROM places WHERE {denominator_sql}")
    total = cur.fetchone()[0]
    if total == 0:
        return 0.0
    cur.execute(
        f"SELECT count(*) FROM places WHERE {denominator_sql} AND ({numerator_sql})"
    )
    return cur.fetchone()[0] / total


def collect_metrics(conn, cfg) -> list[Metric]:
    lat, lon = cfg.core_center
    radius_m = cfg.core_radius_km * 1000
    inside = (
        "ST_DWithin(location, ST_SetSRID(ST_MakePoint("
        f"{lon}, {lat}), 4326)::geography, {radius_m})"
    )
    checks = [
        ("Tọa độ hợp lệ trong lõi Huế", inside, "TRUE", 0.98),
        ("Giờ mở cửa parse được (di tích, bảo tàng)",
         "opening_hours IS NOT NULL",
         "category IN ('di_tich', 'bao_tang', 'lang_tam')", 0.60),
        ("Category xác định được", "category <> 'khac'", "TRUE", 0.90),
        ("Địa danh có từ 5 ảnh",
         "(SELECT count(*) FROM place_images i WHERE i.place_id = places.id) >= 5",
         "category NOT IN ('nha_hang', 'quan_ca_phe')", 0.70),
    ]

    metrics = []
    with conn.cursor() as cur:
        for name, numerator, denominator, threshold in checks:
            value = _ratio(cur, numerator, denominator)
            metrics.append(Metric(name, value, threshold, value >= threshold))

        cur.execute("SELECT count(*) FROM places WHERE label_source = 'manual'")
        manual = cur.fetchone()[0]
        metrics.append(Metric("Địa danh gán nhãn tay", manual, 100, manual >= 100))
    return metrics


def render_report(metrics: list[Metric], merge_review_rows: int) -> str:
    lines = [f"# Báo cáo chất lượng dữ liệu — {date.today().isoformat()}", "",
             "| Chỉ số | Giá trị | Ngưỡng | Kết quả |", "|---|---|---|---|"]
    for m in metrics:
        value = f"{m.value:.0%}" if m.threshold <= 1 else f"{m.value:.0f}"
        threshold = f"{m.threshold:.0%}" if m.threshold <= 1 else f"{m.threshold:.0f}"
        lines.append(
            f"| {m.name} | {value} | {threshold} | {'PASS' if m.passed else 'FAIL'} |"
        )
    lines += ["", f"Cặp chờ xem tay trong merge_review.csv: {merge_review_rows}"]
    return "\n".join(lines) + "\n"


def _write_sample(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.name, p.category, p.opening_hours_raw, p.indoor_ratio,"
            " p.label_source, p.source_url,"
            " (SELECT string_agg(source || ':' || external_id, ' ')"
            "  FROM place_external_ids e WHERE e.place_id = p.id) AS place_keys"
            " FROM places p ORDER BY random() LIMIT %s",
            (SAMPLE_SIZE,),
        )
        rows = cur.fetchall()
        headers = [d.name for d in cur.description]

    path = QA_DIR / f"sample_{date.today().isoformat()}.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers + ["dung_khong", "ghi_chu"])
        for row in rows:
            writer.writerow(list(row) + ["", ""])


def run(conn, cfg) -> list[Metric]:
    QA_DIR.mkdir(parents=True, exist_ok=True)
    merge_path = QA_DIR / "merge_review.csv"
    merge_rows = 0
    if merge_path.exists():
        with open(merge_path, encoding="utf-8", newline="") as fh:
            merge_rows = max(0, sum(1 for _ in fh) - 1)

    metrics = collect_metrics(conn, cfg)
    report = render_report(metrics, merge_rows)
    (QA_DIR / f"coverage_{date.today().isoformat()}.md").write_text(
        report, encoding="utf-8"
    )
    _write_sample(conn)
    print(report)
    for m in metrics:
        if not m.passed:
            print(f"CẢNH BÁO: {m.name} dưới ngưỡng")
    return metrics
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_coverage.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Nối vào CLI và chạy thật**

Thêm `sub.add_parser("qa")` và nhánh gọi `pipeline.qa.coverage.run(db.connect(), load_city())`.
Run: `python -m pipeline.cli qa`
Expected: in bảng chỉ số, tạo `data/qa/coverage_<ngày>.md` và `data/qa/sample_<ngày>.csv`.

- [ ] **Step 6: Commit**

```bash
git add pipeline/qa pipeline/cli.py tests/test_coverage.py
git commit -m "feat: báo cáo chất lượng dữ liệu và mẫu kiểm duyệt"
```

---

## Task 16: Lệnh `all`, README, chạy đầu cuối

**Files:**
- Modify: `pipeline/cli.py`
- Create: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: mọi module trước đó
- Produces: `pipeline.cli.PIPELINE_ORDER: list[str]` — thứ tự chạy của lệnh `all`

Thứ tự bắt buộc: `boundary`, `wikidata`, `osm` chạy trước `normalize` và `load`; `wikipedia` và `commons` cần `places` đã có `id` nên chạy sau `load`; `weather` độc lập, chạy cuối.

- [ ] **Step 1: Viết test**

`tests/test_cli.py`:

```python
from pipeline.cli import INGEST_MODULES, PIPELINE_ORDER


def test_pipeline_order_loads_places_before_text_and_images():
    assert PIPELINE_ORDER.index("load") < PIPELINE_ORDER.index("wikipedia")
    assert PIPELINE_ORDER.index("load") < PIPELINE_ORDER.index("commons")


def test_pipeline_order_ingests_sources_before_normalize():
    for source in ("boundary", "wikidata", "osm"):
        assert PIPELINE_ORDER.index(source) < PIPELINE_ORDER.index("normalize")


def test_every_ingest_step_has_a_module():
    steps = [s for s in PIPELINE_ORDER if s not in {"migrate", "normalize", "load", "qa"}]
    assert set(steps) <= set(INGEST_MODULES)
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL với `ImportError: cannot import name 'PIPELINE_ORDER'`

- [ ] **Step 3: Viết lại `pipeline/cli.py` hoàn chỉnh**

```python
import argparse
import importlib

from pipeline import db
from pipeline.config import load_city

INGEST_MODULES = {
    "boundary": "pipeline.ingest.boundary",
    "wikidata": "pipeline.ingest.wikidata",
    "osm": "pipeline.ingest.osm",
    "wikipedia": "pipeline.ingest.wikipedia",
    "commons": "pipeline.ingest.commons",
    "weather": "pipeline.ingest.weather",
}

PIPELINE_ORDER = [
    "migrate", "boundary", "wikidata", "osm", "normalize", "load",
    "wikipedia", "commons", "weather", "qa",
]


def _run_step(step: str, conn, force: bool = False) -> None:
    if step == "migrate":
        applied = db.run_migrations(conn)
        print(f"migrate: {len(applied)} migration")
        return

    cfg = load_city()
    if step == "qa":
        from pipeline.qa import coverage

        coverage.run(conn, cfg)
        return

    if step in {"normalize", "load"}:
        from pipeline.load.upsert import upsert_places
        from pipeline.normalize import pipeline as normalize_pipeline

        places, review = normalize_pipeline.run(conn, cfg)
        print(f"normalize: {len(places)} địa điểm, {len(review)} cặp chờ xem tay")
        if step == "load":
            inserted, updated = upsert_places(conn, places)
            print(f"load: thêm {inserted}, cập nhật {updated}")
        return

    module = importlib.import_module(INGEST_MODULES[step])
    print(f"{step}: {module.run(conn, cfg, force=force)} bản ghi")


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "normalize", "load", "qa", "all"):
        sub.add_parser(name)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--source", required=True, choices=sorted(INGEST_MODULES))
    ingest.add_argument("--force", action="store_true")
    args = parser.parse_args()

    conn = db.connect()
    if args.command == "all":
        for step in PIPELINE_ORDER:
            _run_step(step, conn)
    elif args.command == "ingest":
        _run_step(args.source, conn, force=args.force)
    else:
        _run_step(args.command, conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Chạy toàn bộ test**

Run: `python -m pytest -v`
Expected: PASS toàn bộ.

- [ ] **Step 5: Chạy đầu cuối trên DB trống**

Run:
```bash
docker compose down -v
docker compose up -d --build
python -m pipeline.cli all
```
Expected: chạy hết không lỗi, in báo cáo QA ở cuối. Ghi lại số địa danh, số quán ăn, số ảnh thật vào `README.md`.

- [ ] **Step 6: Viết `README.md`**

Gồm: yêu cầu môi trường; cách sao `.env.example` thành `.env` và điền `CONTACT_EMAIL`; `docker compose up -d --build`; `python -m pipeline.cli all`; bảng số liệu thật sau lần chạy đầu; cách sửa `config/overrides.csv` rồi chạy lại `python -m pipeline.cli load`; ghi chú cách cài `psycopg` nếu wheel cp314 chưa có.

- [ ] **Step 7: Commit**

```bash
git add pipeline/cli.py README.md tests/test_cli.py
git commit -m "feat: lệnh all chạy toàn bộ pipeline và README hướng dẫn chạy"
```

---

## Task 17: Đối chiếu nhãn rule-based với nhãn tay

**Files:**
- Create: `pipeline/qa/label_agreement.py`
- Modify: `pipeline/cli.py` (thêm `sub.add_parser("label-agreement")` và nhánh gọi)
- Test: `tests/test_label_agreement.py`

**Interfaces:**
- Consumes: `config/overrides.csv`, `data/staged/*.json`, các hàm normalize
- Produces: `agreement(rule_values: dict[str, float], manual_values: dict[str, float], tolerance: float = 0.2) -> tuple[int, int, float]` trả `(số khớp, tổng, tỷ lệ)`; `run(conn, cfg) -> float` ghi `data/qa/label_agreement_<ngày>.md`

Đây là số liệu spec mục 6 hứa đưa vào báo cáo: rule-based đoán đúng `indoor_ratio` trong sai số ±0.2 bao nhiêu phần trăm so với nhãn người. Task này chỉ chạy được sau khi `config/overrides.csv` đã có đủ khoảng 100 dòng.

- [ ] **Step 1: Viết test**

`tests/test_label_agreement.py`:

```python
from pipeline.qa.label_agreement import agreement


def test_agreement_counts_within_tolerance():
    rule = {"wikidata:Q1": 0.30, "wikidata:Q2": 0.90, "wikidata:Q3": 0.05}
    manual = {"wikidata:Q1": 0.25, "wikidata:Q2": 0.40, "wikidata:Q3": 0.10}
    matched, total, ratio = agreement(rule, manual, tolerance=0.2)
    assert (matched, total) == (2, 3)
    assert round(ratio, 2) == 0.67


def test_agreement_ignores_keys_without_manual_label():
    rule = {"wikidata:Q1": 0.3, "wikidata:Q9": 0.5}
    manual = {"wikidata:Q1": 0.3}
    assert agreement(rule, manual)[1] == 1


def test_agreement_with_no_overlap_returns_zero_total():
    assert agreement({"a": 0.1}, {"b": 0.2}) == (0, 0, 0.0)
```

- [ ] **Step 2: Chạy test, xác nhận fail**

Run: `python -m pytest tests/test_label_agreement.py -v`
Expected: FAIL với `ModuleNotFoundError`

- [ ] **Step 3: Viết `pipeline/qa/label_agreement.py`**

```python
from dataclasses import replace
from datetime import date
from pathlib import Path

from pipeline.normalize.category import load_category_rules, map_category
from pipeline.normalize.pipeline import _load_staged
from pipeline.normalize.merge import merge_places, within_core
from pipeline.normalize.weather_labels import (
    apply_labels, load_overrides, load_weather_defaults,
)

QA_DIR = Path("data/qa")


def agreement(rule_values: dict[str, float], manual_values: dict[str, float],
              tolerance: float = 0.2) -> tuple[int, int, float]:
    shared = [k for k in rule_values if k in manual_values]
    if not shared:
        return 0, 0, 0.0
    matched = sum(
        1 for k in shared if abs(rule_values[k] - manual_values[k]) <= tolerance
    )
    return matched, len(shared), matched / len(shared)


def run(conn, cfg) -> float:
    wikidata = [p for p in _load_staged("data/staged/wikidata.json") if within_core(p, cfg)]
    osm = [p for p in _load_staged("data/staged/osm.json") if within_core(p, cfg)]
    places, _ = merge_places(wikidata, osm)

    rules = load_category_rules()
    defaults = load_weather_defaults()
    overrides = load_overrides()

    rule_values: dict[str, float] = {}
    manual_values: dict[str, float] = {}
    for place in places:
        place = replace(
            place, category=map_category(place.tags, place.wikidata_classes, rules)
        )
        rule_only = apply_labels(place, defaults, {})
        for source, external_id in place.external_ids.items():
            key = f"{source}:{external_id}"
            if key in overrides and "indoor_ratio" in overrides[key]:
                rule_values[key] = rule_only.indoor_ratio
                manual_values[key] = overrides[key]["indoor_ratio"]

    matched, total, ratio = agreement(rule_values, manual_values)
    QA_DIR.mkdir(parents=True, exist_ok=True)
    report = (
        f"# Đối chiếu nhãn rule-based với nhãn tay — {date.today().isoformat()}\n\n"
        f"- Số địa danh có nhãn tay: {total}\n"
        f"- Rule đúng trong sai số ±0.2: {matched}\n"
        f"- Tỷ lệ khớp: {ratio:.1%}\n"
    )
    (QA_DIR / f"label_agreement_{date.today().isoformat()}.md").write_text(
        report, encoding="utf-8"
    )
    print(report)
    return ratio
```

- [ ] **Step 4: Chạy test**

Run: `python -m pytest tests/test_label_agreement.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Nối vào CLI**

Trong `pipeline/cli.py`, thêm `"label-agreement"` vào vòng lặp tạo parser:

```python
for name in ("migrate", "normalize", "load", "qa", "label-agreement", "all"):
    sub.add_parser(name)
```

và thêm nhánh vào đầu `_run_step`, ngay sau nhánh `qa`:

```python
    if step == "label-agreement":
        from pipeline.qa import label_agreement

        label_agreement.run(conn, cfg)
        return
```

`PIPELINE_ORDER` giữ nguyên — lệnh này chạy tay sau khi `config/overrides.csv` đã điền, không nằm trong `all`.

- [ ] **Step 6: Chạy thật**

Run: `python -m pipeline.cli label-agreement`
Expected: in tỷ lệ khớp. Nếu `Số địa danh có nhãn tay: 0` thì `config/overrides.csv` chưa được điền — đây là việc thủ công, không phải lỗi code.

- [ ] **Step 7: Commit**

```bash
git add pipeline/qa/label_agreement.py pipeline/cli.py tests/test_label_agreement.py
git commit -m "feat: đo mức khớp giữa nhãn rule-based và nhãn gán tay"
```

---

## Sau khi chạy xong plan

Việc thủ công còn lại, không code thay được:

1. Điền `config/overrides.csv` cho khoảng 100 địa danh chính. Nguồn: trang Trung tâm Bảo tồn Di tích Cố đô Huế cho giá vé và giờ mở cửa; ảnh vệ tinh để ước lượng `indoor_ratio`.
2. Xử lý `data/qa/merge_review.csv`: cặp nào đúng là một chỗ thì thêm dòng `osm` vào `place_external_ids` bằng SQL, hoặc hạ `MATCH_RADIUS_M` nếu nhiễu quá nhiều.
3. Đối chiếu `data/qa/sample_<ngày>.csv` với nguồn, điền cột `dung_khong`, ghi tỷ lệ đúng vào báo cáo đồ án.
4. Ngưỡng nào phải hạ so với spec thì sửa cả spec và ghi lý do — số liệu thật thắng ước lượng ban đầu.
