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
