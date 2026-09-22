import json
import time
from pathlib import Path

import httpx

from pipeline.config import CityConfig
from pipeline.http import Fetcher
from pipeline.models import PlaceRecord

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_URL = OVERPASS_ENDPOINTS[0]
STAGED_PATH = "data/staged/osm.json"

_RETRYABLE_STATUSES = {429, 502, 504}
_RETRY_WAIT_SECONDS = 60
_RETRY_PASSES = 2

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


def _fetch_overpass(fetcher: Fetcher, query: str, force: bool):
    """Thử lần lượt các endpoint Overpass, tối đa 2 lượt qua toàn bộ danh sách.

    Overpass là dịch vụ công cộng miễn phí, hay quá tải (504) hoặc bị giới
    hạn (429/502). Khi một endpoint lỗi theo kiểu tạm thời, đợi khoảng một
    phút rồi chuyển sang endpoint kế tiếp thay vì bỏ cuộc ngay.
    """
    last_error: Exception | None = None
    for lap in range(1, _RETRY_PASSES + 1):
        for url in OVERPASS_ENDPOINTS:
            print(f"osm: đang thử endpoint {url} (lượt {lap}/{_RETRY_PASSES})")
            try:
                return fetcher.fetch(url, method="POST", data=query, force=force)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in _RETRYABLE_STATUSES:
                    raise
                last_error = exc
                print(
                    f"osm: {url} trả về {status}, đợi {_RETRY_WAIT_SECONDS}s"
                    " rồi thử endpoint kế tiếp"
                )
                time.sleep(_RETRY_WAIT_SECONDS)
            except httpx.TimeoutException as exc:
                last_error = exc
                print(
                    f"osm: {url} timeout, đợi {_RETRY_WAIT_SECONDS}s"
                    " rồi thử endpoint kế tiếp"
                )
                time.sleep(_RETRY_WAIT_SECONDS)
    raise RuntimeError(
        f"Cả {len(OVERPASS_ENDPOINTS)} endpoint Overpass đều lỗi"
        f" sau {_RETRY_PASSES} lượt thử"
    ) from last_error


def run(conn, cfg: CityConfig, force: bool = False) -> int:
    south, west, north, east = cfg.bbox
    query = QUERY_TEMPLATE.format(bbox=f"{south},{west},{north},{east}")
    fetcher = Fetcher(conn, "osm", min_interval=2.0)
    res = _fetch_overpass(fetcher, query, force)
    places = parse_elements(json.loads(res.content)["elements"])

    Path(STAGED_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(STAGED_PATH).write_text(
        json.dumps([p.__dict__ for p in places], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return len(places)
