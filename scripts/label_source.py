"""Danh sách địa điểm để gán nhãn tay, đọc từ data/staged/*.json.

Không còn danh sách địa danh viết tay: bản cũ hardcode QID không tồn tại trong
dữ liệu (vd Lăng Khải Định `Q1320092`, thật là `Q7818621`), nên nhãn ghi ra
`config/overrides.csv` không khớp với địa điểm nào. Khóa `place_key` giờ lấy
thẳng từ dữ liệu staged, cùng quy ước với pipeline: `wikidata:<QID>` nếu có,
không thì `osm:<type>/<id>`.
"""

from __future__ import annotations

import csv
import json
import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGED = (ROOT / "data" / "staged" / "wikidata.json", ROOT / "data" / "staged" / "osm.json")
PLACE_FACTS_PATH = ROOT / "config" / "place_facts.csv"

FOOD_AMENITIES = {"restaurant", "cafe", "fast_food", "bar", "pub", "food_court"}
# OSM trùng tên với một mục Wikidata trong bán kính này coi là cùng địa điểm.
DUPLICATE_RADIUS_M = 300

GROUP_FACTS = "co_nguon"      # có trong config/place_facts.csv (giờ, giá đã kiểm)
GROUP_SIGHTS = "tham_quan"
GROUP_FOOD = "an_uong"
GROUP_ORDER = {GROUP_FACTS: 0, GROUP_SIGHTS: 1, GROUP_FOOD: 2}


@dataclass(frozen=True)
class LabelPlace:
    key: str
    name: str
    lat: float
    lon: float
    group: str
    kind: str
    source_url: str


def place_key(item: dict) -> str | None:
    ext = item.get("external_ids") or {}
    qid = ext.get("wikidata") or (item.get("tags") or {}).get("wikidata")
    if qid:
        return f"wikidata:{qid}"
    if ext.get("osm"):
        return f"osm:{ext['osm']}"
    return None


def _kind(item: dict) -> str:
    tags = item.get("tags") or {}
    for tag in ("tourism", "historic", "amenity", "leisure"):
        if tag in tags:
            return f"{tag}={tags[tag]}"
    return "wikidata" if (item.get("external_ids") or {}).get("wikidata") else "khac"


def _fold(name: str) -> str:
    text = unicodedata.normalize("NFD", name.lower().replace("đ", "d"))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).strip()


def _distance_m(a: LabelPlace, b: LabelPlace) -> float:
    dlat = math.radians(b.lat - a.lat)
    dlon = math.radians(b.lon - a.lon) * math.cos(math.radians((a.lat + b.lat) / 2))
    return 6_371_000 * math.hypot(dlat, dlon)


def fact_keys(path: Path = PLACE_FACTS_PATH) -> set[str]:
    if not path.exists():
        return set()
    with open(path, encoding="utf-8", newline="") as fh:
        return {row["place_key"].strip() for row in csv.DictReader(fh) if row.get("place_key")}


def load_places(staged: tuple[Path, ...] = STAGED, facts: set[str] | None = None) -> list[LabelPlace]:
    """Mọi địa điểm staged, đã bỏ trùng; nhóm có nguồn → tham quan → ăn uống, rồi theo tên."""
    facts = fact_keys() if facts is None else facts
    by_key: dict[str, LabelPlace] = {}
    for path in staged:
        for item in json.loads(path.read_text(encoding="utf-8")):
            key = place_key(item)
            if key is None or key in by_key or not item.get("name"):
                continue
            kind = _kind(item)
            if key in facts:
                group = GROUP_FACTS
            elif kind.startswith("amenity=") and kind.split("=", 1)[1] in FOOD_AMENITIES:
                group = GROUP_FOOD
            else:
                group = GROUP_SIGHTS
            by_key[key] = LabelPlace(key, item["name"], float(item["lat"]), float(item["lon"]),
                                     group, kind, item.get("source_url") or "")

    wikidata = [p for p in by_key.values() if p.key.startswith("wikidata:")]
    places = [
        p for p in by_key.values()
        if p.key.startswith("wikidata:") or not any(
            _fold(p.name) == _fold(w.name) and _distance_m(p, w) <= DUPLICATE_RADIUS_M for w in wikidata)
    ]
    return sorted(places, key=lambda p: (GROUP_ORDER[p.group], _fold(p.name)))
