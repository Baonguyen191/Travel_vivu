import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

QA_DIR = Path("data/qa")
SAMPLE_SIZE = 20
CORE_CATEGORIES = ("di_tich", "bao_tang", "lang_tam")

OPENING_HOURS_NOTE = (
    "Lưu ý về 'opening_hours': ngưỡng 5% là mức sàn để bắt lỗi hồi quy của"
    " parser, không phải mục tiêu chất lượng. OSM không mang giờ mở cửa cho"
    " phần lớn di tích/bảo tàng/lăng tẩm ở Huế — con đường thật để có dữ liệu"
    " này là nhập tay vào config/overrides.csv, việc đó chưa nằm trong phạm vi"
    " kế hoạch này."
)
MANUAL_LABEL_NOTE = (
    "Chỉ số 'Địa danh gán nhãn tay' dự kiến FAIL ở 0 cho tới khi có người điền"
    " config/overrides.csv — đây là lời nhắc về việc còn tồn đọng, không phải"
    " lỗi cần che giấu."
)
BOUNDARY_MISSING_NOTE = (
    "Chỉ số 'Tọa độ nằm trong ranh giới hành chính Huế' bị bỏ qua vì không"
    " tìm thấy file ranh giới (polygon_path trong config/city_hue.yml) —"
    " chạy `python -m pipeline.cli ingest --source boundary` để sinh file"
    " đó trước khi chỉ số này có ý nghĩa."
)


@dataclass
class Metric:
    name: str
    value: float
    threshold: float | None
    passed: bool
    informational: bool = False
    key: str | None = None


def _count(cur, sql: str, params: tuple = ()) -> int:
    cur.execute(sql, params)
    return cur.fetchone()[0]


def _load_boundary_wkt(cfg) -> str | None:
    """Đọc WKT ranh giới hành chính từ `cfg.polygon_path`, hoặc None nếu
    chưa cấu hình đường dẫn hay file chưa tồn tại (chưa chạy `boundary`)."""
    path = getattr(cfg, "polygon_path", None)
    if not path:
        return None
    file = Path(path)
    if not file.exists():
        return None
    return file.read_text(encoding="utf-8").strip()


def collect_metrics(conn, cfg) -> list[Metric]:
    with conn.cursor() as cur:
        total = _count(cur, "SELECT count(*) FROM places")

        # Trước fix này, "trong lõi Huế" nghĩa là "trong bán kính core_radius_km
        # quanh core_center" — CHÍNH XÁC cùng vị từ mà normalize/pipeline.py đã
        # dùng để lọc dữ liệu đầu vào (within_core), nên chỉ số này cấu trúc
        # luôn là ~100% bất kể chất lượng tọa độ thật sự, không đo được gì.
        # Đổi sang kiểm thật: nằm trong ranh giới hành chính Huế lấy từ
        # Overpass (`boundary`), một vị từ độc lập với bước lọc dữ liệu.
        wkt = _load_boundary_wkt(cfg)
        if wkt is None:
            coord_metric = Metric(
                "Tọa độ nằm trong ranh giới hành chính Huế", 0.0, None, True,
                informational=True, key="boundary_missing",
            )
        else:
            inside = _count(
                cur,
                "SELECT count(*) FROM places WHERE location IS NOT NULL"
                " AND ST_Within(location::geometry, ST_GeomFromText(%s, 4326))",
                (wkt,),
            )
            value = inside / total if total else 0.0
            coord_metric = Metric(
                "Tọa độ nằm trong ranh giới hành chính Huế", value, 0.98, value >= 0.98
            )

        categorized = _count(
            cur, "SELECT count(*) FROM places WHERE category <> 'khac'"
        )
        value = categorized / total if total else 0.0
        category_metric = Metric(
            "Category xác định được", value, 0.90, value >= 0.90
        )

        core_total = _count(
            cur,
            "SELECT count(*) FROM places WHERE category = ANY(%s)",
            (list(CORE_CATEGORIES),),
        )
        core_with_hours = _count(
            cur,
            "SELECT count(*) FROM places WHERE category = ANY(%s)"
            " AND opening_hours IS NOT NULL",
            (list(CORE_CATEGORIES),),
        )
        value = core_with_hours / core_total if core_total else 0.0
        hours_metric = Metric(
            "Di tích, bảo tàng, lăng tẩm có opening_hours parse được",
            value, 0.05, value >= 0.05, key="opening_hours",
        )

        wikidata_total = _count(
            cur,
            "SELECT count(DISTINCT place_id) FROM place_external_ids"
            " WHERE source = 'wikidata'",
        )
        wikidata_with_image = _count(
            cur,
            "SELECT count(DISTINCT e.place_id) FROM place_external_ids e"
            " JOIN place_images i ON i.place_id = e.place_id"
            " WHERE e.source = 'wikidata'",
        )
        value = wikidata_with_image / wikidata_total if wikidata_total else 0.0
        image_metric = Metric(
            "Place có liên kết Wikidata và có ít nhất một ảnh",
            value, 0.25, value >= 0.25,
        )

        manual = _count(
            cur, "SELECT count(*) FROM places WHERE label_source = 'manual'"
        )
        manual_metric = Metric(
            "Địa danh gán nhãn tay", manual, 100, manual >= 100, key="manual_label"
        )

        five_plus = _count(
            cur,
            "SELECT count(*) FROM places p WHERE"
            " (SELECT count(*) FROM place_images i WHERE i.place_id = p.id) >= 5",
        )
        five_plus_metric = Metric(
            "Địa danh có từ 5 ảnh trở lên", five_plus, None, True,
            informational=True,
        )

    return [
        coord_metric,
        category_metric,
        hours_metric,
        image_metric,
        manual_metric,
        five_plus_metric,
    ]


def render_report(metrics: list[Metric], merge_review_rows: int) -> str:
    lines = [
        f"# Báo cáo chất lượng dữ liệu — {date.today().isoformat()}", "",
        "| Chỉ số | Giá trị | Ngưỡng | Kết quả |", "|---|---|---|---|",
    ]
    for m in metrics:
        is_ratio = m.threshold is not None and m.threshold <= 1
        value = f"{m.value:.0%}" if is_ratio else f"{m.value:.0f}"
        if m.informational:
            threshold = "—"
            result = "(thông tin)"
        else:
            threshold = f"{m.threshold:.0%}" if is_ratio else f"{m.threshold:.0f}"
            result = "PASS" if m.passed else "FAIL"
        lines.append(f"| {m.name} | {value} | {threshold} | {result} |")

    lines += ["", f"Cặp chờ xem tay trong merge_review.csv: {merge_review_rows}", ""]

    keys = {m.key for m in metrics if m.key is not None}
    if "opening_hours" in keys:
        lines.append(OPENING_HOURS_NOTE)
    if "manual_label" in keys:
        lines.append(MANUAL_LABEL_NOTE)
    if "boundary_missing" in keys:
        lines.append(BOUNDARY_MISSING_NOTE)

    return "\n".join(lines) + "\n"


def _write_sample(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.name, p.category, p.opening_hours_raw,"
            " p.opening_hours::text AS opening_hours_parsed, p.indoor_ratio,"
            " p.label_source, p.source_url,"
            " (SELECT string_agg(source || ':' || external_id, ' ')"
            "  FROM place_external_ids e WHERE e.place_id = p.id) AS external_ids"
            " FROM places p ORDER BY random() LIMIT %s",
            (SAMPLE_SIZE,),
        )
        rows = cur.fetchall()
        headers = [d.name for d in cur.description]

    path = QA_DIR / f"sample_{date.today().isoformat()}.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers + ["dung_khong", "ghi_chu"])
        for row in rows:
            writer.writerow(list(row) + ["", ""])


def run(conn, cfg) -> list[Metric]:
    QA_DIR.mkdir(parents=True, exist_ok=True)
    merge_path = QA_DIR / "merge_review.csv"
    merge_rows = 0
    if merge_path.exists():
        with open(merge_path, encoding="utf-8", newline="") as fh:
            merge_rows = max(0, sum(1 for _ in fh) - 1)

    metrics = collect_metrics(conn, cfg)
    report = render_report(metrics, merge_rows)
    (QA_DIR / f"coverage_{date.today().isoformat()}.md").write_text(
        report, encoding="utf-8"
    )
    _write_sample(conn)
    print(report)
    for m in metrics:
        if not m.informational and not m.passed:
            print(f"CẢNH BÁO: {m.name} dưới ngưỡng")
    return metrics
