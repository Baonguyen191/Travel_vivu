import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import review_rag_queries as rv  # noqa: E402
from rag.corpus import Chunk  # noqa: E402

YML = """# chú thích đầu file, phải giữ nguyên
- id: q001
  query: Chùa Thiên Mụ xây năm nào?
  type: fact
  relevant:
    - {qid: Q1, name: Chùa Thiên Mụ}
  answer_keywords: ["1601"]
  source_chunk: "1-1"
  reviewed: false

- id: q002
  query: Cầu và chùa
  type: multi
  relevant:
    - {qid: Q1, name: Chùa Thiên Mụ}
    - {qid: Q2, name: Cầu Trường Tiền}
"""

CHUNKS = [
    Chunk("1-0", "wikidata:Q1", "Chùa Thiên Mụ", "", "Chùa trên đồi Hà Khê."),
    Chunk("1-1", "wikidata:Q1", "Chùa Thiên Mụ", "Lịch sử", "Chùa khởi lập năm 1601."),
    Chunk("2-0", "wikidata:Q2", "Cầu Trường Tiền", "", "Cầu dài 402,60 m."),
]


@pytest.fixture
def files(tmp_path, monkeypatch):
    q, r = tmp_path / "q.yml", tmp_path / "r.jsonl"
    q.write_text(YML, encoding="utf-8")
    monkeypatch.setattr(rv, "QUERIES_PATH", q)
    monkeypatch.setattr(rv, "REVIEWS_PATH", r)
    return q, r


def test_set_reviewed_edits_only_target_block_and_keeps_comments():
    out = rv.set_reviewed(YML, "q001", True)
    assert out.startswith("# chú thích đầu file")
    data = yaml.safe_load(out)
    assert data[0]["reviewed"] is True
    assert "reviewed" not in data[1]
    out2 = rv.set_reviewed(out, "q002", True)
    assert yaml.safe_load(out2)[1]["reviewed"] is True
    assert yaml.safe_load(rv.set_reviewed(out2, "q002", False))[1]["reviewed"] is False
    with pytest.raises(KeyError):
        rv.set_reviewed(YML, "q999", True)


def test_apply_label_writes_log_and_yaml(files):
    q, r = files
    rv.apply_label("q001", "T", queries_path=q, reviews_path=r)
    rv.apply_label("q001", "F", "từ khoá sai", queries_path=q, reviews_path=r)
    assert yaml.safe_load(q.read_text(encoding="utf-8"))[0]["reviewed"] is False
    logs = [json.loads(line) for line in r.read_text(encoding="utf-8").splitlines()]
    assert [x["label"] for x in logs] == ["T", "F"]
    assert rv.latest_reviews(r)["q001"]["note"] == "từ khoá sai"
    with pytest.raises(ValueError):
        rv.apply_label("q001", "X", queries_path=q, reviews_path=r)


def test_set_answer_keywords_replaces_inserts_and_removes():
    out = rv.set_answer_keywords(YML, "q001", ["1601", "chúa Nguyễn Hoàng"])
    data = yaml.safe_load(out)
    assert data[0]["answer_keywords"] == ["1601", "chúa Nguyễn Hoàng"]
    out = rv.set_answer_keywords(out, "q002", ["402,60 m"])
    data = yaml.safe_load(out)
    assert data[1]["answer_keywords"] == ["402,60 m"] and data[1]["type"] == "multi"
    assert "answer_keywords" not in yaml.safe_load(rv.set_answer_keywords(out, "q001", []))[0]
    assert out.startswith("# chú thích đầu file")


def test_apply_answer_reports_keywords_not_in_chunks(files):
    q, _ = files
    result = rv.apply_answer("q001", [" 1601 ", "", "1601", "năm 1700"], queries_path=q, chunks=CHUNKS)
    assert result == {"keywords": ["1601", "năm 1700"], "missing": ["năm 1700"]}
    assert yaml.safe_load(q.read_text(encoding="utf-8"))[0]["answer_keywords"] == ["1601", "năm 1700"]
    with pytest.raises(ValueError):
        rv.apply_answer("q001", [str(i) for i in range(11)], queries_path=q, chunks=CHUNKS)


def test_build_items_puts_source_chunk_first_and_covers_each_place(files):
    q, _ = files
    items = rv.build_items(q, CHUNKS)
    first, multi = items
    assert first["evidence"][0]["chunk_id"] == "1-1" and first["evidence"][0]["is_source"]
    assert {e["chunk_id"] for e in multi["evidence"]} >= {"2-0"}
    assert {e["title"] for e in multi["evidence"]} == {"Chùa Thiên Mụ", "Cầu Trường Tiền"}
    assert all(r["has_chunks"] for r in multi["relevant"])
