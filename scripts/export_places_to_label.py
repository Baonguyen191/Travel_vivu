"""Script xuất danh sách địa điểm cần gán nhãn ra file CSV để xem/sửa bằng Excel.

Nguồn: data/staged/*.json (khóa `place_key` thật, xem scripts/label_source.py).

Cách dùng:
    python scripts/export_places_to_label.py
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from label_source import ROOT, load_places

OUTPUT_PATH = ROOT / "data" / "staged" / "places_to_label_template.csv"
OVERRIDES_PATH = ROOT / "config" / "overrides.csv"

FIELDNAMES = [
    "place_key", "name", "group", "kind", "lat", "lon", "is_labeled_in_overrides",
    "indoor_ratio", "avg_visit_minutes", "best_time_of_day", "unsafe_conditions",
    "ticket_price_vnd", "dress_code", "note",
]


def load_overrides() -> dict[str, dict]:
    if not OVERRIDES_PATH.exists():
        return {}
    with open(OVERRIDES_PATH, encoding="utf-8", newline="") as fh:
        return {row["place_key"].strip(): row for row in csv.DictReader(fh) if row.get("place_key")}


def main() -> None:
    overrides = load_overrides()
    places = load_places()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig để Excel đọc đúng tiếng Việt.
    with open(OUTPUT_PATH, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for p in places:
            label = overrides.get(p.key, {})
            writer.writerow({
                "place_key": p.key, "name": p.name, "group": p.group, "kind": p.kind,
                "lat": p.lat, "lon": p.lon,
                "is_labeled_in_overrides": "YES" if label else "NO",
                **{f: label.get(f, "") for f in FIELDNAMES[7:]},
            })
    print(f"Đã xuất {len(places)} địa điểm vào {OUTPUT_PATH}")
    orphans = sorted(set(overrides) - {p.key for p in places})
    if orphans:
        print(f"Cảnh báo: nhãn không khớp địa điểm nào: {', '.join(orphans)}")


if __name__ == "__main__":
    main()
