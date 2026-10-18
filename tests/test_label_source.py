import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from label_source import (  # noqa: E402
    GROUP_FACTS, GROUP_FOOD, GROUP_SIGHTS, STAGED, fact_keys, load_places, place_key,
)


def _write(path: Path, items: list[dict]) -> Path:
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def test_place_key_prefers_wikidata_then_osm():
    assert place_key({"external_ids": {"wikidata": "Q1"}}) == "wikidata:Q1"
    assert place_key({"external_ids": {"osm": "node/5"}, "tags": {"wikidata": "Q2"}}) == "wikidata:Q2"
    assert place_key({"external_ids": {"osm": "way/7"}, "tags": {}}) == "osm:way/7"
    assert place_key({"external_ids": {}}) is None


def test_load_places_groups_dedups_and_orders(tmp_path):
    wd = _write(tmp_path / "wd.json", [
        {"name": "Lăng Tự Đức", "external_ids": {"wikidata": "Q7481171"}, "lat": 16.4330, "lon": 107.5646},
        {"name": "Đàn Nam Giao", "external_ids": {"wikidata": "Q10840433"}, "lat": 16.44, "lon": 107.58},
    ])
    osm = _write(tmp_path / "osm.json", [
        # Trùng tên với mục Wikidata, cách ~20 m → bỏ.
        {"name": "Lăng Tự Đức", "external_ids": {"osm": "node/1"}, "lat": 16.4332, "lon": 107.5646,
         "tags": {"historic": "tomb"}},
        # Trùng tên nhưng ở xa → giữ.
        {"name": "Lăng Tự Đức", "external_ids": {"osm": "node/2"}, "lat": 16.50, "lon": 107.60,
         "tags": {"historic": "tomb"}},
        {"name": "Cafe A", "external_ids": {"osm": "node/3"}, "lat": 16.46, "lon": 107.59,
         "tags": {"amenity": "cafe"}},
        {"name": "", "external_ids": {"osm": "node/4"}, "lat": 16.46, "lon": 107.59, "tags": {}},
    ])

    places = load_places((wd, osm), facts={"wikidata:Q7481171"})

    assert [(p.key, p.group) for p in places] == [
        ("wikidata:Q7481171", GROUP_FACTS),
        ("wikidata:Q10840433", GROUP_SIGHTS),
        ("osm:node/2", GROUP_SIGHTS),
        ("osm:node/3", GROUP_FOOD),
    ]


@pytest.mark.skipif(not all(p.exists() for p in STAGED), reason="data/staged chưa có")
def test_real_staged_keys_cover_place_facts():
    """Mọi khóa trong config/place_facts.csv phải có trong dữ liệu staged thật."""
    keys = {p.key for p in load_places()}
    assert fact_keys() <= keys
