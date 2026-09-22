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


def assert_connected_to(conn: psycopg.Connection, expected_dbname: str) -> None:
    """Chặn thao tác nếu `conn` không thực sự trỏ tới `expected_dbname`.

    Dùng làm lưới an toàn trước khi TRUNCATE trong test: nếu logic dựng URL
    kết nối ở trên có sai sót (ví dụ dùng nhầm biến, nhầm hàm connect), hàm
    này báo lỗi rõ ràng thay vì âm thầm thao tác sai database.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        actual = cur.fetchone()[0]
    if actual != expected_dbname:
        raise RuntimeError(
            f"Kết nối đang trỏ tới database '{actual}', không phải"
            f" '{expected_dbname}' như mong đợi — từ chối thao tác để tránh"
            " xoá nhầm dữ liệu thật."
        )


def assert_not_connected_to(conn: psycopg.Connection, forbidden_dbname: str) -> None:
    """Chặn thao tác nếu `conn` đang trỏ tới `forbidden_dbname`.

    Lưới an toàn thứ hai, độc lập với `assert_connected_to`: nếu
    TEST_DATABASE_URL bị cấu hình nhầm trùng với DATABASE_URL (database
    dev/production), `assert_connected_to` sẽ không phát hiện ra vì cả giá
    trị mong đợi lẫn giá trị thực tế đều bị lệch theo cùng một cách — hàm
    này bắt đúng trường hợp đó bằng cách so sánh với database bị cấm một
    cách độc lập, thay vì âm thầm cho phép TRUNCATE dữ liệu thật.
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
