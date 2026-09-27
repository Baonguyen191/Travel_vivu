import pytest

from pipeline.embed import DeterministicEmbedder, chunk_embedding_text, run_embed_chunks
from rag.hybrid import HybridRetriever, reciprocal_rank_fusion
from rag.lexical import BM25Retriever, analyze, fold_diacritics, load_corpus, tokenize
from rag.retriever import RAGChunk, RAGRetriever


def _chunk(chunk_id, place_id, name, content, category="khac"):
    return RAGChunk(chunk_id, place_id, name, category, content, None, 0.0)


CORPUS = [
    _chunk(1, 1, "Chùa Thiên Mụ", "Chùa được xây năm 1601 bên sông Hương.", "chua"),
    _chunk(2, 2, "Lăng Tự Đức", "Khiêm Lăng có hồ Lưu Khiêm.", "lang_tam"),
    _chunk(3, 3, "Cầu Trường Tiền", "Cầu sắt sáu nhịp bắc qua sông Hương.", "diem_tham_quan"),
]


def test_fold_diacritics_handles_d_and_tones():
    assert fold_diacritics("Lăng Tự Đức, Chùa Thiên Mụ") == "lang tu duc, chua thien mu"


def test_tokenize_fold_is_optional():
    assert tokenize("Đại Nội") == ["đại", "nội"]
    assert tokenize("Đại Nội", fold=True) == ["dai", "noi"]


def test_bm25_fold_matches_query_without_diacritics():
    assert BM25Retriever(CORPUS).search("lang tu duc", top_k=3) == []

    results = BM25Retriever(CORPUS, analyzer="fold").search("lang tu duc", top_k=3)
    assert results[0].place_name == "Lăng Tự Đức"


def test_analyze_multi_emits_accented_folded_and_bigram_terms():
    assert analyze("Tự Đức", "multi") == ["tự", "đức", "~tu", "~duc", "~tu_duc"]
    with pytest.raises(ValueError):
        analyze("x", "bogus")


NOISY = CORPUS + [
    # Chunk nhiễu: bỏ dấu thì "lăng nhăng", "tự do", "đạo đức" khớp đủ cả ba âm
    # tiết của "lang tu duc", nhưng không có cặp liền nhau "tu duc".
    _chunk(4, 4, "Đế quốc Việt Nam",
           "Phe nọ đảng kia lăng nhăng, bàn chuyện tự do và đạo đức, lăng nhăng mãi"
           " về tự do, về đạo đức.", "boi_canh"),
]


def _margin(analyzer: str) -> float:
    """Điểm chunk đúng chia điểm chunk nhiễu cho câu "lang tu duc"."""
    scores = {r.place_name: r.similarity
              for r in BM25Retriever(NOISY, analyzer=analyzer).search("lang tu duc", top_k=4)}
    return scores["Lăng Tự Đức"] / scores["Đế quốc Việt Nam"]


def test_bm25_multi_bigrams_separate_phrase_from_folded_collisions():
    # fold: chunk nhiễu điểm gần bằng chunk đúng; multi: bigram "tu duc" tách hẳn ra.
    assert _margin("fold") < 1.5
    assert _margin("multi") > 2.0


def test_bm25_multi_still_matches_accented_query():
    results = BM25Retriever(NOISY, analyzer="multi").search("Lăng Tự Đức", top_k=1)
    assert results[0].place_name == "Lăng Tự Đức"


def test_bm25_place_id_filter():
    results = BM25Retriever(CORPUS, analyzer="fold").search("song huong", top_k=3, place_id=3)
    assert [r.chunk_id for r in results] == [3]


def test_rrf_rewards_agreement_between_rankings():
    a, b, c = CORPUS
    fused = reciprocal_rank_fusion([[a, b], [b, c]], rrf_k=60)

    assert [r.chunk_id for r in fused] == [2, 1, 3]
    assert fused[0].similarity == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1].similarity == pytest.approx(1 / 61)


def test_rrf_empty_ranking_keeps_other():
    a, b, _ = CORPUS
    assert [r.chunk_id for r in reciprocal_rank_fusion([[a, b], []])] == [1, 2]


class _StubDense:
    """Nhánh dense giả: luôn trả thứ tự cố định, ghi lại tham số được gọi."""

    def __init__(self, ranking):
        self.ranking = ranking
        self.embedder = DeterministicEmbedder()
        self.calls = []

    def search(self, query, top_k=5, category=None, place_id=None):
        self.calls.append((top_k, category, place_id))
        return [c for c in self.ranking if not category or c.category == category][:top_k]


def test_hybrid_recovers_when_dense_misses_no_diacritics_query():
    # Dense xếp Lăng Tự Đức cuối (như bge-m3 với câu không dấu); BM25 bỏ dấu kéo lên.
    dense = _StubDense([CORPUS[0], CORPUS[2], CORPUS[1]])
    hybrid = HybridRetriever(dense, BM25Retriever(CORPUS, analyzer="fold"), depth=10)

    results = hybrid.search("lang tu duc", top_k=2)

    assert results[0].place_name == "Lăng Tự Đức"
    assert hybrid.name == "hybrid:deterministic+bm25_fold"


def test_hybrid_passes_filters_and_depth_to_both_branches():
    dense = _StubDense(CORPUS)
    hybrid = HybridRetriever(dense, BM25Retriever(CORPUS, analyzer="fold"), depth=10)

    results = hybrid.search("song huong", top_k=3, category="chua")

    assert dense.calls == [(10, "chua", None)]
    assert {r.category for r in results} == {"chua"}


def test_hybrid_empty_query():
    hybrid = HybridRetriever(_StubDense(CORPUS), BM25Retriever(CORPUS, analyzer="fold"))
    assert hybrid.search("  ") == []


@pytest.mark.integration
def test_hybrid_from_connection(db_conn):
    with db_conn.cursor() as cur:
        for name, content in [("Lăng Tự Đức", "Khiêm Lăng có hồ Lưu Khiêm."),
                              ("Chùa Thiên Mụ", "Chùa xây năm 1601.")]:
            cur.execute("INSERT INTO places (name) VALUES (%s) RETURNING id", (name,))
            place_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO place_chunks (place_id, content, content_hash) VALUES (%s, %s, %s)",
                (place_id, content, name),
            )
    run_embed_chunks(db_conn, embedder=DeterministicEmbedder())
    hybrid = HybridRetriever.from_connection(db_conn, embedder=DeterministicEmbedder())
    assert hybrid.lexical.name == "bm25_multi"

    # Câu trùng hệt văn bản đã embed: cả hai nhánh cùng xếp Lăng Tự Đức đầu.
    exact = chunk_embedding_text("Lăng Tự Đức", "Khiêm Lăng có hồ Lưu Khiêm.")
    assert hybrid.search(exact, top_k=2)[0].place_name == "Lăng Tự Đức"
    assert hybrid.search("chua thien mu", top_k=1)[0].place_name == "Chùa Thiên Mụ"


@pytest.mark.integration
def test_non_destination_categories_are_excluded_by_default(db_conn):
    with db_conn.cursor() as cur:
        for name, category in [("Lăng Tự Đức", "lang_tam"), ("Huế", "boi_canh"), ("Ga Huế", "ha_tang")]:
            cur.execute("INSERT INTO places (name, category) VALUES (%s, %s) RETURNING id", (name, category))
            place_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO place_chunks (place_id, content, content_hash) VALUES (%s, 'Lăng ở Huế.', %s)",
                (place_id, name),
            )
    run_embed_chunks(db_conn, embedder=DeterministicEmbedder())

    assert {c.place_name for c in load_corpus(db_conn)} == {"Lăng Tự Đức"}
    assert len(load_corpus(db_conn, exclude_categories=())) == 3

    dense = RAGRetriever(db_conn, embedder=DeterministicEmbedder())
    assert {c.place_name for c in dense.search("Huế", top_k=5)} == {"Lăng Tự Đức"}
    everything = RAGRetriever(db_conn, embedder=DeterministicEmbedder(), exclude_categories=())
    assert len(everything.search("Huế", top_k=5)) == 3
