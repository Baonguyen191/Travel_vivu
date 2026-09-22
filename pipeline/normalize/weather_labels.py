import csv
import sys
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


def _warn(place_key: str, column: str, value: str) -> None:
    print(
        f"Cảnh báo: bỏ qua giá trị không hợp lệ ở '{place_key}', cột '{column}': {value!r}",
        file=sys.stderr,
    )


def _parse_field(row: dict, key: str, place_key: str, column: str, convert):
    raw = row.get(key)
    if not raw:
        return None
    try:
        return convert(raw)
    except ValueError:
        _warn(place_key, column, raw)
        return None


def load_overrides(path: str = "config/overrides.csv") -> dict[str, dict]:
    result: dict[str, dict] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("place_key") or "").strip()
            if not key:
                continue
            entry: dict = {}

            indoor_ratio = _parse_field(row, "indoor_ratio", key, "indoor_ratio", float)
            if indoor_ratio is not None:
                entry["indoor_ratio"] = indoor_ratio

            avg_visit_minutes = _parse_field(
                row, "avg_visit_minutes", key, "avg_visit_minutes", int
            )
            if avg_visit_minutes is not None:
                entry["avg_visit_minutes"] = avg_visit_minutes

            if row.get("best_time_of_day"):
                entry["best_time_of_day"] = row["best_time_of_day"].split("|")
            if row.get("unsafe_conditions"):
                entry["unsafe_conditions"] = row["unsafe_conditions"].split("|")

            ticket_price_vnd = _parse_field(
                row, "ticket_price_vnd", key, "ticket_price_vnd", int
            )
            if ticket_price_vnd is not None:
                entry["ticket_price"] = {"vnd": ticket_price_vnd}

            if row.get("dress_code"):
                entry["dress_code"] = row["dress_code"]

            if not entry:
                print(
                    f"Cảnh báo: bỏ qua toàn bộ dòng '{key}' vì không có trường hợp lệ nào",
                    file=sys.stderr,
                )
                continue

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
            copied = {k: (list(v) if isinstance(v, list) else v) for k, v in entry.items()}
            updated = replace(updated, label_source="manual", **copied)
            break
    return updated
