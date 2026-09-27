from urllib.parse import urlsplit

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


def test_with_dbname_swaps_only_the_path():
    url = db.with_dbname("postgresql://u:p@host:5433/travel?sslmode=disable", "travel_test")
    assert url == "postgresql://u:p@host:5433/travel_test?sslmode=disable"


def test_assert_is_test_database_rejects_database_without_test_suffix():
    # Database dev thật ("travel") không có hậu tố "_test" — phải bị từ chối
    # dù nó có được kết nối "đúng" theo URL hay không. Nếu bỏ điều kiện hậu
    # tố này đi, test sẽ fail vì không còn raise nữa.
    conn = db.connect()  # database dev thật ("travel")
    try:
        with pytest.raises(RuntimeError):
            db.assert_is_test_database(conn)
    finally:
        conn.close()


def test_assert_not_connected_to_rejects_database_named_like_production():
    # Mô phỏng trường hợp database dev/production bị đổi tên thành một cái
    # gì đó có hậu tố "_test": assert_is_test_database sẽ không bắt được vì
    # hậu tố đúng, nhưng assert_not_connected_to vẫn phải từ chối vì tên
    # trùng với database mà DATABASE_URL trỏ tới. Nếu bỏ điều kiện này đi,
    # test sẽ fail vì không còn raise nữa.
    conn = db.connect()  # database dev thật ("travel")
    try:
        with pytest.raises(RuntimeError):
            db.assert_not_connected_to(conn, "travel")
    finally:
        conn.close()


def test_guards_accept_the_real_travel_test_database(db_conn):
    # db_conn tự nó chỉ yield được nếu cả hai guard đã pass khi fixture
    # thiết lập; gọi lại trực tiếp ở đây để khẳng định rõ ràng là database
    # test thật ("travel_test") được cả hai điều kiện chấp nhận.
    db.assert_is_test_database(db_conn)
    production_dbname = urlsplit(db.database_url()).path.lstrip("/")
    db.assert_not_connected_to(db_conn, production_dbname)
