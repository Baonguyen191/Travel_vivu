import pytest
from pipeline import db

pytestmark = pytest.mark.integration

EXPECTED_TABLES = [
    "places",
    "place_external_ids",
    "raw_documents",
    "place_chunks",
    "place_images",
    "reviews",
    "weather_cache",
    "climate_normals",
]


def test_run_migrations_is_idempotent():
    conn = db.connect()
    db.run_migrations(conn)  # đưa DB về trạng thái đã áp dụng (fresh hoặc không)
    second = db.run_migrations(conn)
    assert second == []

    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (EXPECTED_TABLES,),
        )
        found = {row[0] for row in cur.fetchall()}
        assert found == set(EXPECTED_TABLES)

    migration_files = {path.name for path in db.MIGRATIONS_DIR.glob("*.sql")}
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations")
        assert cur.fetchone()[0] == len(migration_files)


def test_assert_connected_to_rejects_wrong_database():
    conn = db.connect()  # database dev thật ("travel"), không phải test
    try:
        with pytest.raises(RuntimeError):
            db.assert_connected_to(conn, "travel_test")
    finally:
        conn.close()


def test_assert_connected_to_accepts_matching_database():
    conn = db.connect()
    try:
        db.assert_connected_to(conn, "travel")
    finally:
        conn.close()


def test_with_dbname_swaps_only_the_path():
    url = db.with_dbname("postgresql://u:p@host:5433/travel?sslmode=disable", "travel_test")
    assert url == "postgresql://u:p@host:5433/travel_test?sslmode=disable"


def test_assert_not_connected_to_rejects_forbidden_database():
    # Mô phỏng đúng tình huống nguy hiểm: TEST_DATABASE_URL bị cấu hình nhầm
    # trùng với DATABASE_URL. assert_connected_to không bắt được lỗi này vì
    # cả giá trị mong đợi lẫn thực tế đều lệch theo cùng một cách; hàm này
    # phải bắt được.
    conn = db.connect()  # database dev thật ("travel")
    try:
        with pytest.raises(RuntimeError):
            db.assert_not_connected_to(conn, "travel")
    finally:
        conn.close()


def test_assert_not_connected_to_accepts_other_database():
    conn = db.connect()
    try:
        db.assert_not_connected_to(conn, "travel_test")
    finally:
        conn.close()
