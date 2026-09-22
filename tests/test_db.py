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
