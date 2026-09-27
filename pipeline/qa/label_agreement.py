from dataclasses import replace
from datetime import date
from pathlib import Path

from pipeline.ingest.osm import STAGED_PATH as OSM_STAGED_PATH
from pipeline.ingest.wikidata import STAGED_PATH as WIKIDATA_STAGED_PATH
from pipeline.normalize.category import load_category_rules, map_category
from pipeline.normalize.merge import merge_places, within_core
from pipeline.normalize.pipeline import load_staged
from pipeline.normalize.weather_labels import (
    apply_labels, load_overrides, load_weather_defaults,
)

QA_DIR = Path("data/qa")
OVERRIDES_PATH = "config/overrides.csv"
TOLERANCE = 0.2


def agreement(
    rule_values: dict[str, float], manual_values: dict[str, float],
    tolerance: float = TOLERANCE,
) -> tuple[int, int, float]:
    shared = [k for k in rule_values if k in manual_values]
    if not shared:
        return 0, 0, 0.0
    matched = sum(
        1 for k in shared if abs(rule_values[k] - manual_values[k]) <= tolerance
    )
    return matched, len(shared), matched / len(shared)


def run(conn, cfg) -> float:
    wikidata = [p for p in load_staged(WIKIDATA_STAGED_PATH) if within_core(p, cfg)]
    osm = [p for p in load_staged(OSM_STAGED_PATH) if within_core(p, cfg)]
    places, _ = merge_places(wikidata, osm)

    rules = load_category_rules()
    defaults = load_weather_defaults()
    overrides = load_overrides(OVERRIDES_PATH)

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
