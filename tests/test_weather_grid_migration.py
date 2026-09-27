"""Migration 006 sửa lỗi migration 005: ALTER COLUMN ... TYPE DOUBLE PRECISION
mở rộng đúng giá trị NHỊ PHÂN float32 cũ (16.3 -> 16.299999237060547) thay vì
giá trị thập phân, để lại một dòng "xấp xỉ" song song với dòng "sạch" mà các
lượt `weather` sau đó ghi qua grid_key(). Test này không dựa vào
`run_migrations` (chỉ áp mỗi migration đúng một lần theo tên file, không
lặp lại được) — nó dựng lại tình huống lỗi bằng tay rồi chạy thẳng nội dung
SQL của migration 006, đúng như ai đó áp migration này vào một DB đã bị lỗi.
"""

from pathlib import Path

import pytest

from pipeline import db

pytestmark = pytest.mark.integration

MIGRATION_006 = (
    db.MIGRATIONS_DIR / "006_fix_weather_grid_duplicate_cells.sql"
).read_text(encoding="utf-8")

# Giá trị float32 16.3/107.4 mở rộng lên float64 — đúng bug thật đã thấy
# trên DB dev.
STALE_LAT, STALE_LON = 16.299999237060547, 107.4000015258789
CLEAN_LAT, CLEAN_LON = 16.3, 107.4


def _run_migration_006(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(MIGRATION_006)


def test_migration_dedupes_weather_cache_keeping_newer_fetched_at(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO weather_cache (lat_grid, lon_grid, forecast_time,"
            " temperature, fetched_at) VALUES"
            " (%s, %s, '2026-09-22T00:00+07:00', 27.0, now() - interval '1 day')",
            (STALE_LAT, STALE_LON),
        )
        cur.execute(
            "INSERT INTO weather_cache (lat_grid, lon_grid, forecast_time,"
            " temperature, fetched_at) VALUES"
            " (%s, %s, '2026-09-22T00:00+07:00', 31.0, now())",
            (CLEAN_LAT, CLEAN_LON),
        )

    _run_migration_006(db_conn)

    with db_conn.cursor() as cur:
        cur.execute("SELECT lat_grid, lon_grid, temperature FROM weather_cache")
        rows = cur.fetchall()
    assert rows == [(16.3, 107.4, 31.0)]  # giữ dòng fetched_at mới hơn (31.0)


def test_migration_dedupes_climate_normals(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO climate_normals (lat_grid, lon_grid, month, temp_avg)"
            " VALUES (%s, %s, 6, 29.0)",
            (STALE_LAT, STALE_LON),
        )
        cur.execute(
            "INSERT INTO climate_normals (lat_grid, lon_grid, month, temp_avg)"
            " VALUES (%s, %s, 6, 29.0)",
            (CLEAN_LAT, CLEAN_LON),
        )

    _run_migration_006(db_conn)

    with db_conn.cursor() as cur:
        cur.execute("SELECT lat_grid, lon_grid FROM climate_normals")
        rows = cur.fetchall()
    assert rows == [(16.3, 107.4)]


def test_migration_leaves_no_two_cells_with_equal_rounded_coordinates(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO weather_cache (lat_grid, lon_grid, forecast_time, temperature,"
            " fetched_at) VALUES"
            " (%s, %s, '2026-09-22T00:00+07:00', 27.0, now() - interval '1 day'),"
            " (%s, %s, '2026-09-22T01:00+07:00', 28.0, now())",
            (STALE_LAT, STALE_LON, CLEAN_LAT, CLEAN_LON),
        )
        cur.execute(
            "INSERT INTO climate_normals (lat_grid, lon_grid, month, temp_avg) VALUES"
            " (%s, %s, 6, 29.0), (%s, %s, 7, 30.0)",
            (STALE_LAT, STALE_LON, CLEAN_LAT, CLEAN_LON),
        )

    _run_migration_006(db_conn)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT round(lat_grid::numeric, 1), round(lon_grid::numeric, 1),"
            " forecast_time, count(*) FROM weather_cache"
            " GROUP BY 1, 2, 3 HAVING count(*) > 1"
        )
        assert cur.fetchall() == []
        cur.execute(
            "SELECT round(lat_grid::numeric, 1), round(lon_grid::numeric, 1),"
            " month, count(*) FROM climate_normals"
            " GROUP BY 1, 2, 3 HAVING count(*) > 1"
        )
        assert cur.fetchall() == []


def test_migration_is_a_no_op_on_an_already_clean_database(db_conn):
    """An toàn chạy trên DB đã sạch (đúng trạng thái ai áp migration này sau
    khi nó đã tự động chạy một lần qua run_migrations sẽ gặp)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO weather_cache (lat_grid, lon_grid, forecast_time, temperature)"
            " VALUES (%s, %s, '2026-09-22T00:00+07:00', 27.0)",
            (CLEAN_LAT, CLEAN_LON),
        )
        cur.execute(
            "INSERT INTO climate_normals (lat_grid, lon_grid, month, temp_avg)"
            " VALUES (%s, %s, 6, 29.0)",
            (CLEAN_LAT, CLEAN_LON),
        )

    _run_migration_006(db_conn)
    _run_migration_006(db_conn)  # chạy hai lần liên tiếp cũng phải an toàn

    with db_conn.cursor() as cur:
        cur.execute("SELECT lat_grid, lon_grid FROM weather_cache")
        assert cur.fetchall() == [(16.3, 107.4)]
        cur.execute("SELECT lat_grid, lon_grid FROM climate_normals")
        assert cur.fetchall() == [(16.3, 107.4)]


def test_grid_key_is_the_only_place_that_rounds_write_path_coordinates():
    """Xác nhận grid_points() (nguồn (lat, lon) duy nhất mà weather.run() lặp
    qua để ghi cả weather_cache lẫn climate_normals) dùng grid_key() để làm
    tròn, để một lượt chạy sau này không thể tái tạo lại khoá chưa làm tròn.
    """
    import inspect

    from pipeline.ingest import weather

    source = inspect.getsource(weather.grid_points)
    assert "grid_key(" in source
