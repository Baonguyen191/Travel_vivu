import json
from pathlib import Path

from pipeline.config import CityConfig
from pipeline.http import Fetcher
from pipeline.ingest.overpass import OVERPASS_ENDPOINTS, OVERPASS_URL, fetch_overpass
from pipeline.models import PlaceRecord

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


# Bí danh nội bộ: giữ tên cũ để không phải sửa lại các chỗ khác trong module
# này/test đã quen gọi `_fetch_overpass`. Cơ chế xoay vòng mirror thật sự
# sống ở pipeline.ingest.overpass, dùng chung với boundary.py.
_fetch_overpass = fetch_overpass


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
