import os
from urllib.parse import urlsplit

import psycopg
import pytest

from pipeline import db

TEST_DB_NAME = "travel_test"


def _test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or db.database_url(TEST_DB_NAME)


def _dbname(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _ensure_database_exists(url: str) -> None:
    dbname = _dbname(url)
    admin_conn = psycopg.connect(db.with_dbname(url, "postgres"), autocommit=True)
    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin_conn.close()


@pytest.fixture(scope="session")
def _test_database() -> str:
    """Tạo (nếu chưa có) và migrate database test một lần cho cả phiên chạy.

    Test không bao giờ chạm vào database dev/production dùng bởi CLI thật —
    xem quyết định trong task-6-report.md.
    """
    url = _test_database_url()
    _ensure_database_exists(url)
    conn = psycopg.connect(url, autocommit=True)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    return url


@pytest.fixture
def db_conn(_test_database):
    url = _test_database
    conn = psycopg.connect(url, autocommit=True)
    try:
        db.assert_is_test_database(conn)
        db.assert_not_connected_to(conn, _dbname(db.database_url()))
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE places, place_external_ids, raw_documents, place_chunks,"
                " place_images, reviews, weather_cache, climate_normals RESTART IDENTITY CASCADE"
            )
    except Exception:
        conn.close()
        raise
    yield conn
    conn.close()
