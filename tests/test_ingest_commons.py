import functools
import json
import shutil
import tempfile
from pathlib import Path

import httpx

from pipeline.http import Fetcher
from pipeline.ingest import commons
from pipeline.ingest.commons import parse_imageinfo


def test_parse_imageinfo_reads_url_license_author():
    payload = {"query": {"pages": {"1": {"title": "File:A.jpg", "imageinfo": [{
        "url": "https://upload.wikimedia.org/a.jpg",
        "extmetadata": {
            "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "Artist": {"value": "<a href='x'>Nguyễn Văn A</a>"},
        },
    }]}}}}
    [image] = parse_imageinfo(payload)
    assert image.image_url == "https://upload.wikimedia.org/a.jpg"
    assert image.license == "CC BY-SA 4.0"
    assert image.author == "Nguyễn Văn A"


def test_parse_imageinfo_skips_pages_without_imageinfo():
    assert parse_imageinfo({"query": {"pages": {"1": {"title": "File:B.jpg"}}}}) == []


def test_parse_imageinfo_handles_missing_metadata():
    payload = {"query": {"pages": {"1": {"imageinfo": [
        {"url": "https://upload.wikimedia.org/c.jpg", "extmetadata": {}}
    ]}}}}
    [image] = parse_imageinfo(payload)
    assert image.license is None
    assert image.author is None


def _api_url(params: dict) -> str:
    return str(httpx.URL(commons.API_URL, params=params))


def _image_body(url: str, license_: str = "CC BY-SA 4.0", author: str = "Ai đó") -> bytes:
    return json.dumps({"query": {"pages": {"1": {"title": "File:x.jpg", "imageinfo": [{
        "url": url,
        "extmetadata": {
            "LicenseShortName": {"value": license_},
            "Artist": {"value": author},
        },
    }]}}}}).encode("utf-8")


def _setup(monkeypatch, staged_records: list[dict]) -> str:
    """Trỏ commons.STAGED_PATH và commons.Fetcher vào một thư mục tạm, cô lập
    khỏi cache/DB thật. Trả về raw_root để bên gọi tự dọn dẹp."""
    raw_root = tempfile.mkdtemp(prefix="commons_raw_")
    staged_path = Path(raw_root) / "wikidata.json"
    staged_path.write_text(json.dumps(staged_records), encoding="utf-8")
    monkeypatch.setattr(commons, "STAGED_PATH", str(staged_path))
    monkeypatch.setattr(
        commons,
        "Fetcher",
        functools.partial(Fetcher, raw_root=raw_root, min_interval=0, user_agent="test-ua"),
    )
    return raw_root


def _insert_place(db_conn, name: str, qid: str) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO places (name) VALUES (%s) RETURNING id", (name,))
        place_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO place_external_ids (place_id, source, external_id)"
            " VALUES (%s, 'wikidata', %s)",
            (place_id, qid),
        )
    return place_id


def test_run_prefers_p18_image_when_present(httpx_mock, monkeypatch, db_conn):
    raw_root = _setup(monkeypatch, [
        {"external_ids": {"wikidata": "Q1"},
         "tags": {"image_url": "http://commons.wikimedia.org/wiki/Special:FilePath/A%20B.jpg",
                   "commons_category": "Nên bị bỏ qua"}},
    ])
    place_id = _insert_place(db_conn, "Chỗ P18", "Q1")

    url = _api_url({"action": "query", "titles": "File:A B.jpg", "prop": "imageinfo",
                     "iiprop": "url|extmetadata", "format": "json"})
    httpx_mock.add_response(url=url, content=_image_body("https://upload.wikimedia.org/a.jpg"))

    try:
        written = commons.run(db_conn, cfg=None)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert written == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT place_id, image_url, route FROM place_images")
        rows = cur.fetchall()
    assert rows == [(place_id, "https://upload.wikimedia.org/a.jpg", "p18")]


def test_run_lists_commons_category_when_no_p18(httpx_mock, monkeypatch, db_conn):
    raw_root = _setup(monkeypatch, [
        {"external_ids": {"wikidata": "Q2"}, "tags": {"commons_category": "Thien Mu Pagoda"}},
    ])
    place_id = _insert_place(db_conn, "Chùa Thiên Mụ", "Q2")

    url = _api_url({"action": "query", "generator": "categorymembers",
                     "gcmtitle": "Category:Thien Mu Pagoda", "gcmnamespace": "6",
                     "gcmlimit": "30", "prop": "imageinfo",
                     "iiprop": "url|extmetadata", "format": "json"})
    httpx_mock.add_response(url=url, content=_image_body("https://upload.wikimedia.org/b.jpg"))

    try:
        written = commons.run(db_conn, cfg=None)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert written == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT place_id, image_url, route FROM place_images")
        rows = cur.fetchall()
    assert rows == [(place_id, "https://upload.wikimedia.org/b.jpg", "category")]


def test_run_falls_back_to_name_search_when_neither_present(httpx_mock, monkeypatch, db_conn):
    raw_root = _setup(monkeypatch, [
        {"external_ids": {"wikidata": "Q3"}, "tags": {}},
    ])
    place_id = _insert_place(db_conn, "Địa danh không rõ", "Q3")

    url = _api_url({"action": "query", "generator": "search",
                     "gsrsearch": "Địa danh không rõ filetype:bitmap", "gsrnamespace": "6",
                     "gsrlimit": "30", "prop": "imageinfo",
                     "iiprop": "url|extmetadata", "format": "json"})
    httpx_mock.add_response(url=url, content=_image_body("https://upload.wikimedia.org/c.jpg"))

    try:
        written = commons.run(db_conn, cfg=None)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert written == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT place_id, image_url, route FROM place_images")
        rows = cur.fetchall()
    assert rows == [(place_id, "https://upload.wikimedia.org/c.jpg", "name_search")]


def test_run_skips_place_on_http_error_and_continues(httpx_mock, monkeypatch, db_conn):
    raw_root = _setup(monkeypatch, [
        {"external_ids": {"wikidata": "Q4"}, "tags": {}},
        {"external_ids": {"wikidata": "Q5"}, "tags": {}},
    ])
    place_error = _insert_place(db_conn, "Chỗ lỗi", "Q4")
    place_ok = _insert_place(db_conn, "Chỗ ổn", "Q5")

    url_error = _api_url({"action": "query", "generator": "search",
                           "gsrsearch": "Chỗ lỗi filetype:bitmap", "gsrnamespace": "6",
                           "gsrlimit": "30", "prop": "imageinfo",
                           "iiprop": "url|extmetadata", "format": "json"})
    url_ok = _api_url({"action": "query", "generator": "search",
                        "gsrsearch": "Chỗ ổn filetype:bitmap", "gsrnamespace": "6",
                        "gsrlimit": "30", "prop": "imageinfo",
                        "iiprop": "url|extmetadata", "format": "json"})
    httpx_mock.add_response(url=url_error, status_code=503)
    httpx_mock.add_response(url=url_ok, content=_image_body("https://upload.wikimedia.org/d.jpg"))

    try:
        written = commons.run(db_conn, cfg=None)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert written == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT place_id, image_url FROM place_images")
        rows = cur.fetchall()
    assert rows == [(place_ok, "https://upload.wikimedia.org/d.jpg")]


def test_second_run_inserts_nothing_new(httpx_mock, monkeypatch, db_conn):
    raw_root = _setup(monkeypatch, [
        {"external_ids": {"wikidata": "Q6"}, "tags": {}},
    ])
    _insert_place(db_conn, "Chỗ lặp", "Q6")

    url = _api_url({"action": "query", "generator": "search",
                     "gsrsearch": "Chỗ lặp filetype:bitmap", "gsrnamespace": "6",
                     "gsrlimit": "30", "prop": "imageinfo",
                     "iiprop": "url|extmetadata", "format": "json"})
    httpx_mock.add_response(url=url, content=_image_body("https://upload.wikimedia.org/e.jpg"))
    httpx_mock.add_response(url=url, content=_image_body("https://upload.wikimedia.org/e.jpg"))

    try:
        first = commons.run(db_conn, cfg=None, force=True)
        second = commons.run(db_conn, cfg=None, force=True)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert first == 1
    assert second == 0
