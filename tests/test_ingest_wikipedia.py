from pipeline.ingest.wikipedia import chunk_text


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
