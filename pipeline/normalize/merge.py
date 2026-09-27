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
    # Every (Wikidata, OSM) pair whose normalized names match, whatever the
    # distance. This is the full pool a review row can be drawn from.
    same_name_pairs: list[tuple[float, int, int]] = []  # (distance, wd_index, osm_index)
    for wi, wd in enumerate(wikidata):
        wd_key = normalize_name(wd.name)
        for oi, candidate in enumerate(osm):
            if normalize_name(candidate.name) != wd_key:
                continue
            distance = haversine_m(wd.lat, wd.lon, candidate.lat, candidate.lon)
            same_name_pairs.append((distance, wi, oi))

    # Global assignment: among same-name pairs under the radius, accept the
    # closest first and skip any pair whose Wikidata or OSM side is already
    # taken. The distance/id-based sort key depends only on record content,
    # not on input list position, so the assignment is independent of how
    # `wikidata` or `osm` were ordered or shuffled.
    def match_sort_key(pair: tuple[float, int, int]) -> tuple[float, str, str]:
        distance, wi, oi = pair
        wd_id = wikidata[wi].external_ids.get("wikidata", "")
        osm_id = osm[oi].external_ids.get("osm", "")
        return (distance, wd_id, osm_id)

    match_candidates = sorted(
        (pair for pair in same_name_pairs if pair[0] < MATCH_RADIUS_M),
        key=match_sort_key,
    )

    matched_wd: dict[int, int] = {}
    matched_osm: dict[int, int] = {}
    for _distance, wi, oi in match_candidates:
        if wi in matched_wd or oi in matched_osm:
            continue
        matched_wd[wi] = oi
        matched_osm[oi] = wi

    merged: list[PlaceRecord] = []
    for wi, wd in enumerate(wikidata):
        if wi not in matched_wd:
            merged.append(wd)
            continue
        partner = osm[matched_wd[wi]]
        merged.append(replace(
            wd,
            external_ids={**wd.external_ids, **partner.external_ids},
            opening_hours_raw=partner.opening_hours_raw or wd.opening_hours_raw,
            website=partner.website or wd.website,
            tags={**wd.tags, **partner.tags},
        ))
    merged.extend(candidate for oi, candidate in enumerate(osm) if oi not in matched_osm)
    merged.sort(key=lambda place: place.place_key)

    review: list[dict] = []
    for distance, wi, oi in same_name_pairs:
        if matched_wd.get(wi) == oi:
            continue
        wd = wikidata[wi]
        candidate = osm[oi]
        review.append({
            "wikidata_id": wd.external_ids.get("wikidata", ""),
            "osm_id": candidate.external_ids.get("osm", ""),
            "wikidata_name": wd.name,
            "osm_name": candidate.name,
            "distance_m": round(distance, 1),
            "reason": "trung_ten_nhung_xa",
        })
    review.sort(key=lambda row: (row["wikidata_id"], row["osm_id"]))

    return merged, review
