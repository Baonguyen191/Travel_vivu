import pytest

from pipeline.embed import DeterministicEmbedder, chunk_embedding_text, run_embed_chunks
from rag.retriever import RAGChunk, RAGRetriever


def test_rag_build_context_formatting():
    retriever = RAGRetriever(conn=None, embedder=DeterministicEmbedder())

    context = retriever.build_rag_context([
        RAGChunk(
            chunk_id=1,
            place_id=10,
            place_name="Đại Nội Huế",
            category="di_tich",
            content="Đại Nội Huế là hoàng thành thuộc quần thể di tích Cố đô Huế.",
            source_url="https://vi.wikipedia.org/wiki/Đại_Nội_Huế",
            similarity=0.92,
        )
    ])

    assert "Đại Nội Huế (di_tich)" in context
    assert "Đại Nội Huế là hoàng thành" in context
    assert "Nguồn: https://vi.wikipedia.org/wiki/Đại_Nội_Huế" in context


def test_retriever_rejects_wrong_dimension():
    with pytest.raises(ValueError):
        RAGRetriever(conn=None, embedder=DeterministicEmbedder(dim=768))


def _seed(conn) -> dict[str, int]:
    """Ba địa danh, mỗi cái một chunk, đã embed bằng DeterministicEmbedder."""
    places = {
        "Lăng Tự Đức": ("lang_tam", "Lăng có hồ Lưu Khiêm."),
        "Lăng Khải Định": ("lang_tam", "Lăng ghép sành sứ."),
        "Chùa Thiên Mụ": ("chua", "Chùa xây năm 1601."),
    }
    ids = {}
    with conn.cursor() as cur:
        for name, (category, content) in places.items():
            cur.execute(
                "INSERT INTO places (name, category) VALUES (%s, %s) RETURNING id",
                (name, category),
            )
            ids[name] = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO place_chunks (place_id, content, content_hash) VALUES (%s, %s, %s)",
                (ids[name], content, name),
            )
    run_embed_chunks(conn, embedder=DeterministicEmbedder())
    return ids


@pytest.mark.integration
def test_search_ranks_exact_text_first(db_conn):
    _seed(db_conn)
    retriever = RAGRetriever(db_conn, embedder=DeterministicEmbedder())

    # DeterministicEmbedder chỉ cho similarity cao khi query trùng hệt chuỗi đã embed.
    results = retriever.search(chunk_embedding_text("Lăng Tự Đức", "Lăng có hồ Lưu Khiêm."), top_k=3)

    assert [r.place_name for r in results][0] == "Lăng Tự Đức"
    assert results[0].similarity > 0.99
    assert len(results) == 3
    assert results[0].similarity >= results[1].similarity >= results[2].similarity


@pytest.mark.integration
def test_search_filters_category_and_min_similarity(db_conn):
    _seed(db_conn)
    retriever = RAGRetriever(db_conn, embedder=DeterministicEmbedder())
    query = chunk_embedding_text("Lăng Tự Đức", "Lăng có hồ Lưu Khiêm.")

    assert {r.category for r in retriever.search(query, top_k=5, category="lang_tam")} == {"lang_tam"}
    assert [r.place_name for r in retriever.search(query, top_k=5, min_similarity=0.9)] == ["Lăng Tự Đức"]


@pytest.mark.integration
def test_search_ignores_chunks_from_other_model(db_conn):
    _seed(db_conn)

    class _OtherModel(DeterministicEmbedder):
        @property
        def model_name(self) -> str:
            return "other-model"

    retriever = RAGRetriever(db_conn, embedder=_OtherModel())

    assert retriever.search("Lăng Tự Đức", top_k=5) == []
    assert retriever.index_status() == (3, 0)
