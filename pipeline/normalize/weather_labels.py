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
