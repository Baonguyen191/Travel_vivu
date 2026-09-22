import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

DEFAULT_DATABASE_URL = "postgresql://travel:travel@localhost:5433/travel"


def with_dbname(url: str, dbname: str) -> str:
    """Trả về `url` với thành phần tên database được thay bằng `dbname`."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def database_url(dbname: str | None = None) -> str:
    """DATABASE_URL đang cấu hình qua biến môi trường.

    Truyền `dbname` để lấy cùng URL đó nhưng trỏ tới một database khác trên
    cùng server (ví dụ database test) mà không phải parse thủ công.
    """
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    return url if dbname is None else with_dbname(url, dbname)


def connect(dbname: str | None = None) -> psycopg.Connection:
    return psycopg.connect(database_url(dbname), autocommit=True)


def assert_is_test_database(conn: psycopg.Connection) -> None:
    """Chặn thao tác nếu database hiện tại không mang dấu hiệu database test.

    Đây là lưới an toàn thật sự (dương tính, không tautological): kiểm tra
    một thuộc tính cố định của `current_database()` — có hậu tố `_test` —
    hoàn toàn độc lập với việc URL kết nối được dựng thế nào. Một guard so
    sánh `current_database()` với tên suy ra từ chính URL vừa dùng để kết
    nối sẽ luôn đúng theo cấu trúc (libpq luôn kết nối đúng dbname trong
    DSN), nên không bắt được bất kỳ cấu hình sai nào; kiểm tra hậu tố tên
    thì có.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        actual = cur.fetchone()[0]
    if not actual.endswith("_test"):
        raise RuntimeError(
            f"Từ chối TRUNCATE: database '{actual}' không có hậu tố '_test',"
            " nên không được coi là database test. Kiểm tra lại"
            " TEST_DATABASE_URL trước khi chạy test."
        )


def assert_not_connected_to(conn: psycopg.Connection, forbidden_dbname: str) -> None:
    """Chặn thao tác nếu `conn` đang trỏ tới `forbidden_dbname`.

    Lưới an toàn thứ hai, dùng cùng `assert_is_test_database`: bắt trường
    hợp database dev/production (theo DATABASE_URL) vô tình được đổi tên
    thành một cái gì đó có hậu tố `_test` — khi đó hậu tố đúng nhưng đây
    vẫn là database thật, nên phải so sánh trực tiếp với tên database
    DATABASE_URL trỏ tới, độc lập với TEST_DATABASE_URL.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        actual = cur.fetchone()[0]
    if actual == forbidden_dbname:
        raise RuntimeError(
            f"Kết nối đang trỏ tới database '{actual}', trùng với database"
            " dev/production (DATABASE_URL) — từ chối thao tác. Kiểm tra lại"
            " TEST_DATABASE_URL để tránh xoá nhầm dữ liệu thật."
        )


def run_migrations(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT filename FROM schema_migrations")
        done = {row[0] for row in cur.fetchall()}

    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
        applied.append(path.name)
    return applied
