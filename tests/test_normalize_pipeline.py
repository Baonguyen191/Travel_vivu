import json

from pipeline.config import CityConfig
from pipeline.normalize import pipeline as normalize_pipeline
from pipeline.normalize.pipeline import load_staged

CFG = CityConfig(
    name="hue-test",
    bbox=(107.0, 16.0, 108.0, 17.0),
    core_center=(16.46, 107.59),
    core_radius_km=50.0,
)


def _write_staged(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records), encoding="utf-8")


def test_load_staged_reads_records_back_into_place_records(tmp_path):
    path = tmp_path / "staged.json"
    _write_staged(path, [
        {"name": "Chùa A", "external_ids": {"wikidata": "Q1"},
         "lat": 16.46, "lon": 107.59, "wikidata_classes": ["Q24398318"]},
    ])

    result = load_staged(str(path))

    assert len(result) == 1
    assert result[0].name == "Chùa A"
    assert result[0].external_ids == {"wikidata": "Q1"}
    assert result[0].lat == 16.46
    assert result[0].lon == 107.59


def test_run_drops_administrative_units_and_counts_them(tmp_path, monkeypatch):
    wikidata_path = tmp_path / "wikidata.json"
    osm_path = tmp_path / "osm.json"
    review_path = tmp_path / "qa" / "merge_review.csv"

    _write_staged(wikidata_path, [
        # Q687188 map tới don_vi_hanh_chinh (config/categories.yml) — phải bị bỏ.
        {"name": "Phường Vĩ Dạ", "external_ids": {"wikidata": "QP1"},
         "lat": 16.46, "lon": 107.59, "wikidata_classes": ["Q687188"]},
        # Q24398318 map tới chua — phải sống sót.
        {"name": "Chùa Thiên Mụ", "external_ids": {"wikidata": "QC1"},
         "lat": 16.46, "lon": 107.59, "wikidata_classes": ["Q24398318"]},
    ])
    _write_staged(osm_path, [])

    monkeypatch.setattr(normalize_pipeline, "WIKIDATA_STAGED_PATH", str(wikidata_path))
    monkeypatch.setattr(normalize_pipeline, "OSM_STAGED_PATH", str(osm_path))
    monkeypatch.setattr(normalize_pipeline, "MERGE_REVIEW_PATH", str(review_path))

    places, review, dropped = normalize_pipeline.run(None, CFG)

    assert dropped == 1
    assert [p.name for p in places] == ["Chùa Thiên Mụ"]
    assert all(p.category != "don_vi_hanh_chinh" for p in places)


def test_run_writes_review_csv_header_even_when_qa_dir_is_missing(tmp_path, monkeypatch):
    wikidata_path = tmp_path / "wikidata.json"
    osm_path = tmp_path / "osm.json"
    review_path = tmp_path / "qa_out" / "merge_review.csv"

    _write_staged(wikidata_path, [])
    _write_staged(osm_path, [])

    assert not review_path.parent.exists()

    monkeypatch.setattr(normalize_pipeline, "WIKIDATA_STAGED_PATH", str(wikidata_path))
    monkeypatch.setattr(normalize_pipeline, "OSM_STAGED_PATH", str(osm_path))
    monkeypatch.setattr(normalize_pipeline, "MERGE_REVIEW_PATH", str(review_path))

    normalize_pipeline.run(None, CFG)

    assert review_path.exists()
    header = review_path.read_text(encoding="utf-8").splitlines()[0]
    assert header == "wikidata_id,osm_id,wikidata_name,osm_name,distance_m,reason"
