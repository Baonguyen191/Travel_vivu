import httpx

from pipeline.http import Fetcher


def test_fetch_writes_raw_file_and_row(httpx_mock, tmp_path, db_conn):
    httpx_mock.add_response(url="https://example.org/a", content=b"xin chao")
    f = Fetcher(db_conn, "test", min_interval=0, raw_root=str(tmp_path),
                user_agent="travel-agent/0.1 (you@example.com)")

    res = f.fetch("https://example.org/a")

    assert res.content == b"xin chao"
    assert res.from_cache is False
    assert (tmp_path / res.storage_path).read_bytes() == b"xin chao"
    with db_conn.cursor() as cur:
        cur.execute("SELECT source, http_status FROM raw_documents")
        assert cur.fetchall() == [("test", 200)]


def test_fetch_skips_network_when_hash_unchanged(httpx_mock, tmp_path, db_conn):
    httpx_mock.add_response(url="https://example.org/b", content=b"same")
    f = Fetcher(db_conn, "test", min_interval=0, raw_root=str(tmp_path),
                user_agent="ua")
    f.fetch("https://example.org/b")

    second = f.fetch("https://example.org/b")

    assert second.from_cache is True
    assert second.content == b"same"
    assert len(httpx_mock.get_requests()) == 1


def test_fetch_sends_user_agent(httpx_mock, tmp_path, db_conn):
    httpx_mock.add_response(url="https://example.org/c", content=b"x")
    f = Fetcher(db_conn, "test", min_interval=0, raw_root=str(tmp_path),
                user_agent="travel-agent/0.1 (you@example.com)")

    f.fetch("https://example.org/c")

    req = httpx_mock.get_requests()[0]
    assert req.headers["user-agent"] == "travel-agent/0.1 (you@example.com)"


def test_fetch_raises_on_http_error(httpx_mock, tmp_path, db_conn):
    httpx_mock.add_response(url="https://example.org/d", status_code=503)
    f = Fetcher(db_conn, "test", min_interval=0, raw_root=str(tmp_path), user_agent="ua")

    try:
        f.fetch("https://example.org/d")
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("phải ném HTTPStatusError")
