from pipeline.config import CityConfig
from pipeline.load.upsert import upsert_places
from pipeline.models import PlaceRecord
from pipeline.qa.coverage import Metric, collect_metrics, render_report

CFG = CityConfig("Huế", (16.335, 107.435, 16.605, 107.725), (16.4698, 107.5796), 15.0)


def test_render_report_marks_failures():
    metrics = [
        Metric("Tọa độ hợp lệ", 1.0, 0.98, True),
        Metric("Category xác định được", 0.5, 0.90, False),
    ]
    text = render_report(metrics, merge_review_rows=3)
    assert "Category xác định được" in text
    assert "FAIL" in text
    assert "3" in text


def test_collect_metrics_counts_category_coverage(db_conn):
    upsert_places(db_conn, [
        PlaceRecord("A", {"osm": "node/1"}, 16.47, 107.58, category="chua"),
        PlaceRecord("B", {"osm": "node/2"}, 16.47, 107.58, category="khac"),
    ])
    by_name = {m.name: m for m in collect_metrics(db_conn, CFG)}
    assert by_name["Category xác định được"].value == 0.5
    assert by_name["Category xác định được"].passed is False


def test_collect_metrics_manual_label_count(db_conn):
    upsert_places(db_conn, [
        PlaceRecord("A", {"osm": "node/1"}, 16.47, 107.58, category="chua",
                     label_source="manual"),
        PlaceRecord("B", {"osm": "node/2"}, 16.47, 107.58, category="khac"),
    ])
    by_name = {m.name: m for m in collect_metrics(db_conn, CFG)}
    metric = by_name["Địa danh gán nhãn tay"]
    assert metric.value == 1
    assert metric.threshold == 100
    assert metric.passed is False


def test_collect_metrics_wikidata_image_coverage(db_conn):
    upsert_places(db_conn, [
        PlaceRecord("A", {"wikidata": "Q1"}, 16.47, 107.58, category="chua"),
        PlaceRecord("B", {"wikidata": "Q2"}, 16.47, 107.58, category="chua"),
        PlaceRecord("C", {"osm": "node/3"}, 16.47, 107.58, category="chua"),
    ])
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO place_images (place_id, image_url)"
            " SELECT p.id, 'https://example.org/a.jpg' FROM places p"
            " JOIN place_external_ids e ON e.place_id = p.id"
            " WHERE e.source = 'wikidata' AND e.external_id = 'Q1'"
        )
    by_name = {m.name: m for m in collect_metrics(db_conn, CFG)}
    metric = by_name["Place có liên kết Wikidata và có ít nhất một ảnh"]
    assert metric.value == 0.5
    assert metric.threshold == 0.25
    assert metric.passed is True
