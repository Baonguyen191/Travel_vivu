import json

from pipeline.config import CityConfig
from pipeline.qa import label_agreement
from pipeline.qa.label_agreement import agreement

CFG = CityConfig(
    name="hue-test",
    bbox=(107.0, 16.0, 108.0, 17.0),
    core_center=(16.46, 107.59),
    core_radius_km=50.0,
)


def test_agreement_counts_within_tolerance():
    rule = {"wikidata:Q1": 0.30, "wikidata:Q2": 0.90, "wikidata:Q3": 0.05}
    manual = {"wikidata:Q1": 0.25, "wikidata:Q2": 0.40, "wikidata:Q3": 0.10}
    matched, total, ratio = agreement(rule, manual, tolerance=0.2)
    assert (matched, total) == (2, 3)
    assert round(ratio, 2) == 0.67


def test_agreement_ignores_keys_without_manual_label():
    rule = {"wikidata:Q1": 0.3, "wikidata:Q9": 0.5}
    manual = {"wikidata:Q1": 0.3}
    assert agreement(rule, manual)[1] == 1


def test_agreement_with_no_overlap_returns_zero_total():
    assert agreement({"a": 0.1}, {"b": 0.2}) == (0, 0, 0.0)


def _write_staged(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records), encoding="utf-8")


def _write_overrides(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "place_key,indoor_ratio\n"
    body = "".join(f"{key},{value}\n" for key, value in rows)
    path.write_text(header + body, encoding="utf-8")


def test_run_reports_ratio_from_staged_and_overrides(tmp_path, monkeypatch):
    wikidata_path = tmp_path / "wikidata.json"
    osm_path = tmp_path / "osm.json"
    overrides_path = tmp_path / "overrides.csv"
    qa_dir = tmp_path / "qa"

    _write_staged(wikidata_path, [
        # bao_tang mặc định indoor_ratio 0.95; nhãn tay 0.9 -> lệch 0.05, khớp.
        {"name": "Bảo tàng A", "external_ids": {"wikidata": "P1"},
         "lat": 16.46, "lon": 107.59, "wikidata_classes": ["Q33506"]},
        # chua mặc định indoor_ratio 0.5; nhãn tay 0.1 -> lệch 0.4, không khớp.
        {"name": "Chùa B", "external_ids": {"wikidata": "P2"},
         "lat": 16.46, "lon": 107.59, "wikidata_classes": ["Q24398318"]},
        # Không có nhãn tay trong overrides -> không được tính vào mẫu số.
        {"name": "Địa điểm C", "external_ids": {"wikidata": "P3"},
         "lat": 16.46, "lon": 107.59, "wikidata_classes": []},
    ])
    _write_staged(osm_path, [])
    _write_overrides(overrides_path, [
        ("wikidata:P1", 0.9),
        ("wikidata:P2", 0.1),
        # Nhãn tay cho một địa danh không có trong staged data -> bị bỏ qua.
        ("wikidata:P4", 0.5),
    ])

    monkeypatch.setattr(label_agreement, "WIKIDATA_STAGED_PATH", str(wikidata_path))
    monkeypatch.setattr(label_agreement, "OSM_STAGED_PATH", str(osm_path))
    monkeypatch.setattr(label_agreement, "OVERRIDES_PATH", str(overrides_path))
    monkeypatch.setattr(label_agreement, "QA_DIR", qa_dir)

    ratio = label_agreement.run(None, CFG)

    # Chỉ P1 và P2 có cả nhãn rule lẫn nhãn tay -> mẫu số 2; trong đó chỉ P1
    # khớp trong sai số ±0.2 -> tử số 1. 1/2 = 0.5, đúng bằng phép tính tay.
    assert ratio == 0.5

    reports = list(qa_dir.glob("label_agreement_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Số địa danh có nhãn tay: 2" in text
    assert "Rule đúng trong sai số ±0.2: 1" in text
    assert "Tỷ lệ khớp: 50.0%" in text
