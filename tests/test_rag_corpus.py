import json

from rag.corpus import Chunk, build_chunks, chunk_text, corpus_hash, split_sections

ARTICLE = """Lăng Tự Đức là lăng của vua Tự Đức.
Lăng nằm ở làng Dương Xuân Thượng.

== Lịch sử ==
Lăng được xây từ năm 1864 đến năm 1867.

=== Loạn Chày Vôi ===
Binh lính nổi dậy vì phải làm việc nặng nhọc.

== Tham khảo ==
Sách A.

=== Nguồn phụ ===
Sách B.

== Kiến trúc ==
Có hồ Lưu Khiêm."""


def test_split_sections_skips_reference_sections_and_children():
    names = [name for name, _ in split_sections(ARTICLE)]
    assert names == ["", "Lịch sử", "Lịch sử > Loạn Chày Vôi", "Kiến trúc"]
    assert all("Sách" not in body for _, body in split_sections(ARTICLE))


def test_chunk_text_respects_max_chars_and_merges_short_tail():
    long_para = "Câu một dài. " * 40  # ~520 ký tự
    text = f"{long_para}\n{long_para}\n{long_para}\nĐuôi."
    chunks = chunk_text(text, max_chars=600, min_chars=50)
    assert all(len(body) <= 600 + 50 for _, body in chunks)
    assert chunks[-1][1].endswith("Đuôi.")
    assert len(chunks) == 3


def test_chunk_text_merges_tiny_section_into_previous_chunk():
    text = "Mở đầu " * 40 + "\n== Ghi nhận ==\nNgắn."
    chunks = chunk_text(text, max_chars=1200, min_chars=200)
    assert len(chunks) == 1
    assert chunks[0][1].endswith("Ghi nhận: Ngắn.")


def test_build_chunks_maps_place_key_through_redirect(tmp_path):
    raw = tmp_path / "wikipedia" / "2026-10-07"
    raw.mkdir(parents=True)
    (raw / "page1").write_text(json.dumps({"query": {
        "redirects": [{"from": "Khiêm Lăng", "to": "Lăng Tự Đức"}],
        "pages": {"1": {"pageid": 1, "title": "Lăng Tự Đức", "extract": ARTICLE}},
    }}), encoding="utf-8")
    staged = tmp_path / "wikidata.json"
    staged.write_text(json.dumps([
        {"tags": {"vi_title": "Khiêm Lăng"}, "external_ids": {"wikidata": "Q7481171"}},
    ]), encoding="utf-8")

    chunks = build_chunks(raw.parent, staged)

    assert {c.place_key for c in chunks} == {"wikidata:Q7481171"}
    assert chunks[0].chunk_id == "1-0"
    assert chunks[0].embedding_text.startswith("Lăng Tự Đức: ")


def test_corpus_hash_changes_with_content():
    a = [Chunk("1-0", None, "A", "", "x")]
    b = [Chunk("1-0", None, "A", "", "y")]
    assert corpus_hash(a) != corpus_hash(b)
    assert corpus_hash(a) == corpus_hash(list(a))
