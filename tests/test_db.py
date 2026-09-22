import pytest
from pipeline import db

pytestmark = pytest.mark.integration


def test_run_migrations_is_idempotent():
    conn = db.connect()
    first = db.run_migrations(conn)
    assert "002_core.sql" in first
    second = db.run_migrations(conn)
    assert second == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM places")
        assert cur.fetchone()[0] >= 0
