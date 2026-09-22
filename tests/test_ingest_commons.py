import functools
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

from pipeline.http import Fetcher
from pipeline.ingest import commons
from pipeline.ingest.commons import parse_imageinfo

CFG = SimpleNamespace(name="Huế")


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


def _p18_url(filename: str) -> str:
    return _api_url({"action": "query", "titles": f"File:{filename}", "prop": "imageinfo",
                      "iiprop": "url|extmetadata", "format": "json"})


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

    httpx_mock.add_response(
        url=_p18_url("A B.jpg"), content=_image_body("https://upload.wikimedia.org/a.jpg")
    )

    try:
        written = commons.run(db_conn, CFG)
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
        written = commons.run(db_conn, CFG)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert written == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT place_id, image_url, route FROM place_images")
        rows = cur.fetchall()
    assert rows == [(place_id, "https://upload.wikimedia.org/b.jpg", "category")]


def test_run_skips_place_without_structured_tags_and_makes_no_request(
    httpx_mock, monkeypatch, db_conn
):
    """Không còn route tìm theo tên: một địa điểm không có image_url lẫn
    commons_category không sinh ảnh nào, và không gửi request nào tới
    Commons API. Không có response nào được đăng ký trong httpx_mock — nếu
    code lỡ gọi API, httpx_mock sẽ ném lỗi khiến test thất bại."""
    raw_root = _setup(monkeypatch, [
        {"external_ids": {"wikidata": "Q3"}, "tags": {}},
    ])
    _insert_place(db_conn, "Địa danh không rõ", "Q3")

    try:
        written = commons.run(db_conn, CFG)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert written == 0
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM place_images")
        assert cur.fetchone() == (0,)


def test_run_skips_place_on_http_error_and_continues(httpx_mock, monkeypatch, db_conn):
    raw_root = _setup(monkeypatch, [
        {"external_ids": {"wikidata": "Q4"},
         "tags": {"image_url": "http://commons.wikimedia.org/wiki/Special:FilePath/loi.jpg"}},
        {"external_ids": {"wikidata": "Q5"},
         "tags": {"image_url": "http://commons.wikimedia.org/wiki/Special:FilePath/on.jpg"}},
    ])
    _insert_place(db_conn, "Chỗ lỗi", "Q4")
    place_ok = _insert_place(db_conn, "Chỗ ổn", "Q5")

    httpx_mock.add_response(url=_p18_url("loi.jpg"), status_code=503)
    httpx_mock.add_response(
        url=_p18_url("on.jpg"), content=_image_body("https://upload.wikimedia.org/d.jpg")
    )

    try:
        written = commons.run(db_conn, CFG)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert written == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT place_id, image_url FROM place_images")
        rows = cur.fetchall()
    assert rows == [(place_ok, "https://upload.wikimedia.org/d.jpg")]


def test_second_run_inserts_nothing_new(httpx_mock, monkeypatch, db_conn):
    raw_root = _setup(monkeypatch, [
        {"external_ids": {"wikidata": "Q6"},
         "tags": {"image_url": "http://commons.wikimedia.org/wiki/Special:FilePath/lap.jpg"}},
    ])
    _insert_place(db_conn, "Chỗ lặp", "Q6")

    httpx_mock.add_response(
        url=_p18_url("lap.jpg"), content=_image_body("https://upload.wikimedia.org/e.jpg")
    )
    httpx_mock.add_response(
        url=_p18_url("lap.jpg"), content=_image_body("https://upload.wikimedia.org/e.jpg")
    )

    try:
        first = commons.run(db_conn, CFG, force=True)
        second = commons.run(db_conn, CFG, force=True)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert first == 1
    assert second == 0
