import functools
import json
import shutil
import tempfile
from pathlib import Path

import httpx

from pipeline.http import Fetcher
from pipeline.ingest import wikipedia
from pipeline.ingest.wikipedia import _vi_titles_by_qid, chunk_text


def _api_url(title: str) -> str:
    return str(httpx.URL(
        wikipedia.API_URL,
        params={"action": "query", "prop": "extracts", "explaintext": "1",
                "redirects": "1", "format": "json", "titles": title},
    ))


def test_chunk_text_keeps_paragraphs_together():
    text = "Đoạn một.\n\nĐoạn hai dài hơn một chút.\n\nĐoạn ba."
    chunks = chunk_text(text, max_chars=40)
    assert all(len(c) <= 40 for c in chunks)
    assert "Đoạn một." in chunks[0]


def test_chunk_text_splits_long_paragraph_on_sentence_boundary():
    text = "Câu một rất dài. " * 20
    chunks = chunk_text(text, max_chars=100)
    assert len(chunks) > 1
    assert all(c.endswith(".") for c in chunks)


def test_chunk_text_drops_empty_input():
    assert chunk_text("   ") == []


def test_vi_titles_by_qid_indexes_only_records_with_vi_title(tmp_path):
    staged = tmp_path / "wikidata.json"
    staged.write_text(
        json.dumps([
            {"external_ids": {"wikidata": "Q1"}, "tags": {"vi_title": "Chùa Thiên Mụ"}},
            {"external_ids": {"wikidata": "Q2"}, "tags": {}},
            {"external_ids": {"wikidata": "Q3"}, "tags": {"vi_title": "Đại Nội"}},
        ]),
        encoding="utf-8",
    )

    titles = _vi_titles_by_qid(str(staged))

    assert titles == {"Q1": "Chùa Thiên Mụ", "Q3": "Đại Nội"}


def test_vi_titles_by_qid_returns_empty_when_file_missing(tmp_path):
    assert _vi_titles_by_qid(str(tmp_path / "khong_ton_tai.json")) == {}


def test_run_skips_place_on_http_error_and_continues(httpx_mock, monkeypatch, db_conn):
    # Isole tất cả I/O khỏi cache/DB thật: raw_root riêng và một STAGED_PATH
    # trỏ tới file không tồn tại, để tên tra cứu luôn rơi về places.name.
    # Dùng thư mục tạm nông (mkdtemp thẳng dưới thư mục temp hệ thống) thay vì
    # tmp_path lồng sâu của pytest: tên file mà Fetcher ghi là toàn bộ chuỗi
    # query đã url-encode của Wikipedia API (dài, nhiều byte tiếng Việt), nên
    # cộng với đường dẫn lồng sâu của pytest sẽ vượt giới hạn MAX_PATH 260 ký
    # tự trên Windows.
    raw_root = tempfile.mkdtemp(prefix="wpt_raw_")
    monkeypatch.setattr(wikipedia, "STAGED_PATH", str(Path(raw_root) / "khong_ton_tai.json"))
    monkeypatch.setattr(
        wikipedia,
        "Fetcher",
        functools.partial(Fetcher, raw_root=raw_root, min_interval=0, user_agent="test-ua"),
    )

    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO places (name) VALUES ('Chỗ lỗi') RETURNING id")
        place_error = cur.fetchone()[0]
        cur.execute("INSERT INTO places (name) VALUES ('Chỗ ổn') RETURNING id")
        place_ok = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO place_external_ids (place_id, source, external_id)"
            " VALUES (%s, 'wikidata', 'Q1'), (%s, 'wikidata', 'Q2')",
            (place_error, place_ok),
        )

    httpx_mock.add_response(url=_api_url("Chỗ lỗi"), status_code=503)
    body = json.dumps(
        {"query": {"pages": {"1": {"title": "Chỗ ổn", "extract": "Đây là một đoạn văn hợp lệ."}}}}
    ).encode("utf-8")
    httpx_mock.add_response(url=_api_url("Chỗ ổn"), content=body)

    try:
        written = wikipedia.run(db_conn, cfg=None)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert written == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT place_id, content FROM place_chunks")
        rows = cur.fetchall()
    assert rows == [(place_ok, "Đây là một đoạn văn hợp lệ.")]
