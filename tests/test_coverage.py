from pipeline.config import CityConfig
from pipeline.load.upsert import upsert_places
from pipeline.models import PlaceRecord
from pipeline.qa.coverage import (
    BOUNDARY_MISSING_NOTE,
    MANUAL_LABEL_NOTE,
    OPENING_HOURS_NOTE,
    Metric,
    collect_metrics,
    render_report,
)

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


def test_collect_metrics_coordinate_inside_administrative_boundary(db_conn, tmp_path):
    # Trước fix này, "trong lõi Huế" nghĩa là "trong core_radius_km quanh
    # core_center" — CHÍNH XÁC vị từ mà normalize/pipeline.py (within_core)
    # đã dùng để lọc dữ liệu trước khi nạp vào DB, nên chỉ số này luôn ra
    # ~100% một cách cấu trúc, không đo được gì thật. Test này kiểm bằng một
    # ranh giới hành chính thật (ST_Within trên polygon), độc lập với bước
    # lọc dữ liệu.
    wkt_path = tmp_path / "boundary.wkt"
    wkt_path.write_text(
        "POLYGON((107.53 16.42, 107.63 16.42, 107.63 16.52, 107.53 16.52,"
        " 107.53 16.42))",
        encoding="utf-8",
    )
    cfg = CityConfig("Huế", (16.335, 107.435, 16.605, 107.725), (16.4698, 107.5796),
                      15.0, polygon_path=str(wkt_path))
    upsert_places(db_conn, [
        # Bên trong hình vuông ranh giới ở trên.
        PlaceRecord("Trong ranh giới", {"osm": "node/10"}, 16.47, 107.58),
        # ~50 km về phía bắc — rõ ràng ngoài ranh giới.
        PlaceRecord("Ngoài ranh giới", {"osm": "node/11"}, 16.9194, 107.5796),
    ])
    by_name = {m.name: m for m in collect_metrics(db_conn, cfg)}
    metric = by_name["Tọa độ nằm trong ranh giới hành chính Huế"]
    assert metric.value == 0.5
    assert metric.passed is False
    assert metric.informational is False


def test_collect_metrics_skips_boundary_metric_when_file_missing(db_conn):
    cfg = CityConfig("Huế", (16.335, 107.435, 16.605, 107.725), (16.4698, 107.5796),
                      15.0, polygon_path="data/generated/khong_ton_tai_thuc.wkt")
    upsert_places(db_conn, [PlaceRecord("A", {"osm": "node/1"}, 16.47, 107.58)])
    by_name = {m.name: m for m in collect_metrics(db_conn, cfg)}
    metric = by_name["Tọa độ nằm trong ranh giới hành chính Huế"]
    assert metric.informational is True
    text = render_report([metric], merge_review_rows=0)
    assert BOUNDARY_MISSING_NOTE in text


def test_collect_metrics_skips_boundary_metric_when_polygon_path_unset(db_conn):
    upsert_places(db_conn, [PlaceRecord("A", {"osm": "node/1"}, 16.47, 107.58)])
    by_name = {m.name: m for m in collect_metrics(db_conn, CFG)}
    metric = by_name["Tọa độ nằm trong ranh giới hành chính Huế"]
    assert metric.informational is True


def test_collect_metrics_opening_hours_denominator_is_three_categories(db_conn):
    hours = {"mon": [["07:00", "17:00"]]}
    upsert_places(db_conn, [
        PlaceRecord("Di tích có giờ", {"osm": "node/20"}, 16.47, 107.58,
                     category="di_tich", opening_hours=hours),
        PlaceRecord("Di tích chưa parse", {"osm": "node/21"}, 16.47, 107.58,
                     category="di_tich"),
        PlaceRecord("Bảo tàng có giờ", {"osm": "node/22"}, 16.47, 107.58,
                     category="bao_tang", opening_hours=hours),
        PlaceRecord("Lăng tẩm chưa parse", {"osm": "node/23"}, 16.47, 107.58,
                     category="lang_tam"),
        # Ngoài 3 category — có giờ mở cửa nhưng KHÔNG được tính vào mẫu số
        # lẫn tử số; nếu vô tình lọt vào sẽ đổi kết quả 0.5 thành 0.6 (3/5).
        PlaceRecord("Chùa có giờ", {"osm": "node/24"}, 16.47, 107.58,
                     category="chua", opening_hours=hours),
    ])
    by_name = {m.name: m for m in collect_metrics(db_conn, CFG)}
    metric = by_name["Di tích, bảo tàng, lăng tẩm có opening_hours parse được"]
    assert metric.value == 0.5
    assert metric.passed is True


def test_render_report_includes_required_notes():
    metrics = [
        Metric("Tên chỉ số bất kỳ", 0.08, 0.05, True, key="opening_hours"),
        Metric("Tên chỉ số bất kỳ khác", 0, 100, False, key="manual_label"),
    ]
    text = render_report(metrics, merge_review_rows=0)
    assert OPENING_HOURS_NOTE in text
    assert MANUAL_LABEL_NOTE in text


def test_render_report_omits_notes_without_matching_key():
    metrics = [Metric("Chỉ số không liên quan", 1.0, 0.98, True)]
    text = render_report(metrics, merge_review_rows=0)
    assert OPENING_HOURS_NOTE not in text
    assert MANUAL_LABEL_NOTE not in text
