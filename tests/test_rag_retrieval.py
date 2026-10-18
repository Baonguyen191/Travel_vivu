import numpy as np
import pytest

from rag.benchmark import Query, compare, run_retrieval, summarize_retrieval
from rag.corpus import Chunk
from rag.lexical import BM25, analyze, fold_diacritics
from rag.retrievers import BM25Retriever, DenseRetriever, HybridRetriever, RandomRetriever, rrf_fuse

CHUNKS = [
    Chunk("1-0", "wikidata:Q1", "Chùa Thiên Mụ", "", "Chùa được xây năm 1601, có tháp Phước Duyên bảy tầng."),
    Chunk("2-0", "wikidata:Q2", "Lăng Tự Đức", "", "Lăng có hồ Lưu Khiêm và Xung Khiêm tạ."),
    Chunk("3-0", "wikidata:Q3", "Cầu Trường Tiền", "", "Cầu thép bắc qua sông Hương."),
    Chunk("4-0", None, "Huế", "", "Thành phố bên sông Hương."),
]
QUERIES = [
    Query("q1", "Chùa Thiên Mụ xây năm nào?", "fact", {"wikidata:Q1"}, ["1601"]),
    Query("q2", "lang tu duc co ho gi", "no_diacritics", {"wikidata:Q2"}),
    Query("q3", "Cầu và lăng", "multi", {"wikidata:Q2", "wikidata:Q3"}),
    Query("q4", "Địa danh không có trong kho", "fact", {"wikidata:Q99"}),
]


def test_fold_and_multi_analyzer():
    assert fold_diacritics("Lăng Tự Đức") == "lang tu duc"
    terms = analyze("Tự Đức", "multi")
    assert "tự" in terms and "~tu" in terms and "~tu_duc" in terms


def test_bm25_finds_unaccented_query_with_multi_analyzer():
    texts = [c.embedding_text for c in CHUNKS]
    scores = BM25(texts, "multi").scores("lang tu duc")
    assert int(np.argmax(scores)) == 1
    assert BM25(texts, "plain").scores("lang tu duc")[1] == 0.0


def test_rrf_fuse_rewards_agreement():
    assert rrf_fuse([[1, 2, 3], [2, 1, 3]], k=3)[2] == 3
    assert rrf_fuse([[1, 2], [2, 5]], k=1) == [2]


class FakeEmbedder:
    """Vector one-hot theo từ khoá, đủ để kiểm luồng dense/hybrid mà không cần model."""

    class spec:
        key = "fake"

    VOCAB = ["chùa", "lăng", "cầu", "sông"]

    def encode_queries(self, texts):
        return np.array([[float(w in t.casefold()) for w in self.VOCAB] for t in texts], dtype=np.float32)


def test_dense_and_hybrid_retrievers():
    emb = FakeEmbedder()
    matrix = emb.encode_queries([c.embedding_text for c in CHUNKS])
    dense = DenseRetriever(emb, matrix)
    assert dense.search("Cầu nào bắc qua sông?", 1) == [2]
    hybrid = HybridRetriever(dense, BM25Retriever([c.embedding_text for c in CHUNKS]))
    assert hybrid.name == "hybrid_fake+bm25_multi"
    assert hybrid.search("Chùa Thiên Mụ", 1) == [0]


def test_run_retrieval_scores_and_flags_unevaluable():
    retrievers = {"bm25_multi": BM25Retriever([c.embedding_text for c in CHUNKS]),
                  "random": RandomRetriever(len(CHUNKS))}
    df = run_retrieval(retrievers, CHUNKS, QUERIES, k=4)
    bm25 = df[df.retriever == "bm25_multi"].set_index("id")
    assert bm25.loc["q1", "hit@1"] == 1.0
    assert bm25.loc["q1", "answer_hit@5"] == 1.0
    assert bm25.loc["q2", "mrr@10"] == 1.0
    assert not bm25.loc["q4", "evaluable"]
    assert np.isnan(bm25.loc["q2", "answer_hit@5"])

    summary = summarize_retrieval(df)
    assert set(summary["retriever"]) == {"bm25_multi", "random"}
    assert (summary["n"] == 3).all()
    mean, lo, hi = compare(df, "bm25_multi", "bm25_multi", "mrr@10")
    assert (mean, lo, hi) == pytest.approx((0.0, 0.0, 0.0))
