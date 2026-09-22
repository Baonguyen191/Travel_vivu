import pytest

from pipeline import db


@pytest.fixture
def db_conn():
    conn = db.connect()
    db.run_migrations(conn)
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE places, place_external_ids, raw_documents, place_chunks,"
            " place_images, reviews, weather_cache, climate_normals RESTART IDENTITY CASCADE"
        )
    yield conn
    conn.close()
