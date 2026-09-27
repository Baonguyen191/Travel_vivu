import csv
import json
from datetime import date
from pathlib import Path

from pipeline.ingest.osm import STAGED_PATH as OSM_STAGED_PATH
from pipeline.ingest.wikidata import STAGED_PATH as WIKIDATA_STAGED_PATH
from pipeline.models import PlaceRecord
from pipeline.normalize.category import load_category_rules, map_category
from pipeline.normalize.merge import merge_places, within_core
from pipeline.normalize.opening_hours import parse_opening_hours
from pipeline.normalize.weather_labels import (
    apply_labels, load_overrides, load_weather_defaults, warn_unmatched_overrides,
)

MERGE_REVIEW_PATH = "data/qa/merge_review.csv"
ADMIN_CATEGORY = "don_vi_hanh_chinh"


def load_staged(path: str) -> list[PlaceRecord]:
    file = Path(path)
    if not file.exists():
        return []
    return [PlaceRecord(**row) for row in json.loads(file.read_text(encoding="utf-8"))]


def _dated_review_path(base: str) -> Path:
    """Chèn ngày hôm nay vào tên file trước phần mở rộng.

    Trước fix này, `run()` ghi đè `MERGE_REVIEW_PATH` mỗi lần chạy — nếu ai
    đó đang điền cột 'dung_khong'/'ghi_chu' tay vào file đó (đúng quy trình
    "cặp chờ xem tay" của mục QA), một lần `load` chạy song song sẽ xoá sạch
    tiến độ dở dang của họ. Ghi theo ngày, cùng quy ước `coverage_<ngày>.md`
    / `sample_<ngày>.csv` đã dùng, để các lần chạy trong ngày khác nhau
    không đè lên nhau và lịch sử review vẫn còn lại.
    """
    path = Path(base)
    return path.with_name(f"{path.stem}_{date.today().isoformat()}{path.suffix}")


def run(conn, cfg) -> tuple[list[PlaceRecord], list[dict], int]:
    wikidata = [p for p in load_staged(WIKIDATA_STAGED_PATH) if within_core(p, cfg)]
    osm = [p for p in load_staged(OSM_STAGED_PATH) if within_core(p, cfg)]
    places, review = merge_places(wikidata, osm)

    rules = load_category_rules()
    defaults = load_weather_defaults()
    overrides = load_overrides()

    result = []
    dropped = 0
    matched_override_keys: set[str] = set()
    for place in places:
        place.category = map_category(place.tags, place.wikidata_classes, rules)
        if place.category == ADMIN_CATEGORY:
            dropped += 1
            continue
        place.opening_hours = parse_opening_hours(place.opening_hours_raw)
        result.append(apply_labels(place, defaults, overrides, matched_override_keys))

    if dropped:
        print(f"normalize: bỏ qua {dropped} đơn vị hành chính")
    warn_unmatched_overrides(overrides, matched_override_keys)

    review_path = _dated_review_path(MERGE_REVIEW_PATH)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["wikidata_id", "osm_id", "wikidata_name", "osm_name", "distance_m", "reason"]
    with open(review_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(review)
    return result, review, dropped
