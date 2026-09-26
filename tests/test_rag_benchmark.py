import math

import pytest

from rag.benchmark import (
    GoldQuery,
    RandomRetriever,
    build_judgements,
    evaluate,
    hit_at_k,
    load_queries,
    ndcg_at_k,
    paired_bootstrap,
    percentile,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    render_report,
    score_query,
)
from rag.lexical import BM25Retriever, tokenize
from rag.retriever import RAGChunk


def _chunk(chunk_id, place_id, name, content, category="khac"):
    return RAGChunk(chunk_id, place_id, name, category, content, None, 0.0)


CORPUS = [
    _chunk(1, 1, "Chùa Thiên Mụ", "Chùa được xây năm 1601 bên sông Hương.", "chua"),
    _chunk(2, 1, "Chùa Thiên Mụ", "Tháp Phước Duyên cao bảy tầng.", "chua"),
    _chunk(3, 2, "Lăng Tự Đức", "Khiêm Lăng có hồ Lưu Khiêm.", "lang_tam"),
    _chunk(4, 3, "Chợ Đông Ba", "Chợ lớn ở trung tâm Huế.", "cho"),
]


def test_hit_precision_rr():
    rels = [False, True, False, True]
    assert hit_at_k(rels, 1) == 0.0
    assert hit_at_k(rels, 2) == 1.0
    assert precision_at_k(rels, 4) == 0.5
    assert reciprocal_rank(rels, 4) == 0.5
    assert reciprocal_rank(rels, 1) == 0.0


def test_precision_divides_by_k_even_when_fewer_results():
    assert precision_at_k([True], 5) == 0.2


def test_ndcg_perfect_and_partial():
    assert ndcg_at_k([True, True, False], n_relevant=2, k=3) == pytest.approx(1.0)
    expected = (1 / math.log2(3)) / (1 + 1 / math.log2(3))
    assert ndcg_at_k([False, True, False], n_relevant=2, k=3) == pytest.approx(expected)
    assert ndcg_at_k([False], n_relevant=0, k=3) == 0.0


def test_recall_counts_distinct_places():
    assert recall_at_k([1, 1, 2, 5], {1, 2, 3, 4}, 3) == 0.5
    assert recall_at_k([1], set(), 3) == 0.0


def test_percentile_nearest_rank():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(values, 0.5) == 50
    assert percentile(values, 0.95) == 100


def test_paired_bootstrap_detects_consistent_gain_and_is_reproducible():
    a = [1.0] * 20
    b = [0.0] * 10 + [1.0] * 10
    diff, lo, hi = paired_bootstrap(a, b, n_resamples=2000)
    assert diff == pytest.approx(0.5)
    assert 0 < lo <= diff <= hi
    assert paired_bootstrap(a, b, n_resamples=2000) == (diff, lo, hi)


def test_paired_bootstrap_ci_contains_zero_when_equal():
    _, lo, hi = paired_bootstrap([1.0, 0.0] * 10, [0.0, 1.0] * 10, n_resamples=2000)
    assert lo <= 0 <= hi


def test_tokenize_keeps_vietnamese_syllables():
    assert tokenize("Lăng Tự Đức, 1864!") == ["lăng", "tự", "đức", "1864"]


def test_bm25_ranks_matching_chunk_first_and_drops_non_matching():
    bm25 = BM25Retriever(CORPUS)
    results = bm25.search("hồ Lưu Khiêm", top_k=4)
    assert results[0].chunk_id == 3
    assert results[0].similarity > 0
    assert all(r.similarity > 0 for r in results)
    assert bm25.search("xyz không khớp gì", top_k=4) == []


def test_bm25_category_filter():
    results = BM25Retriever(CORPUS).search("Huế sông Hương chợ", top_k=4, category="chua")
    assert {r.category for r in results} == {"chua"}


def test_random_retriever_is_seeded():
    r = RandomRetriever(CORPUS, seed=1)
    assert [c.chunk_id for c in r.search("a", top_k=3)] == [c.chunk_id for c in r.search("a", top_k=3)]
    assert len(r.search("a", top_k=10)) == len(CORPUS)


def test_score_query_answer_keywords_and_multi_recall():
    fact = GoldQuery("f", "Chùa xây năm nào", "fact", ("Q1",), answer_keywords=("1601",))
    chunks = [CORPUS[1], CORPUS[0]]  # chunk đúng địa danh, nhưng chỉ chunk thứ 2 có năm
    m = score_query(fact, chunks, relevant={1}, n_relevant=2, ks=(1, 5))
    assert m["hit@1"] == 1.0
    assert m["answer_hit@1"] == 0.0
    assert m["answer_hit@5"] == 1.0
    assert m["answer_mrr"] == 0.5
    assert "recall@5" not in m

    multi = GoldQuery("m", "chùa và lăng", "multi", ("Q1", "Q2"))
    m = score_query(multi, [CORPUS[0], CORPUS[1]], relevant={1, 2}, n_relevant=3, ks=(5,))
    assert m["recall@5"] == 0.5


def test_build_judgements_separates_data_gaps():
    queries = [
        GoldQuery("ok", "chùa", "descriptive", ("Q1",)),
        GoldQuery("nochunk", "hồ", "descriptive", ("Q9",)),
        GoldQuery("absent", "x", "descriptive", ("Q404",)),
    ]
    j = build_judgements(queries, {"Q1": 1, "Q9": 9}, CORPUS)
    assert j.relevant == {"ok": {1}}
    assert set(j.no_chunks) == {"nochunk", "absent"}
    assert j.missing_qids == {"absent": ["Q404"]}


def test_evaluate_and_report_end_to_end():
    queries = [
        GoldQuery("q1", "Khiêm Lăng hồ Lưu Khiêm", "fact", ("Q2",), answer_keywords=("Khiêm Lăng",)),
        GoldQuery("q2", "tháp Phước Duyên", "paraphrase", ("Q1",)),
    ]
    j = build_judgements(queries, {"Q1": 1, "Q2": 2}, CORPUS)
    results = {
        r.name: evaluate(r, queries, j, ks=(1, 3))
        for r in (BM25Retriever(CORPUS), RandomRetriever(CORPUS))
    }
    assert [r.metrics["hit@1"] for r in results["bm25"]] == [1.0, 1.0]

    report = render_report(queries, j, results, (1, 3), 3, "Kho: 4 chunk.", "eval/x.yml")
    assert "| bm25 |" in report
    assert "chưa kiểm duyệt" in report
    assert "bm25 − random" in report


def test_load_queries_validates(tmp_path):
    path = tmp_path / "q.yml"
    path.write_text(
        "- {id: a, query: x, type: fact, relevant: [{qid: Q1}], answer_keywords: ['1601']}\n",
        encoding="utf-8",
    )
    [q] = load_queries(path)
    assert q.relevant_qids == ("Q1",)
    assert q.answer_keywords == ("1601",)
    assert q.reviewed is False

    path.write_text("- {id: a, query: x, type: bogus, relevant: [{qid: Q1}]}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="type"):
        load_queries(path)

    path.write_text("- {id: a, query: x, type: fact, relevant: [{qid: abc}]}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="qid"):
        load_queries(path)


def test_seed_query_file_is_valid():
    queries = load_queries("eval/rag_queries.yml")
    assert len(queries) >= 30


@pytest.mark.integration
def test_run_end_to_end_on_database(db_conn, tmp_path):
    from pipeline.embed import DeterministicEmbedder, run_embed_chunks
    from rag import benchmark

    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO places (name, category) VALUES ('Chùa Thiên Mụ', 'chua') RETURNING id")
        place_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO place_external_ids (place_id, source, external_id) VALUES (%s, 'wikidata', 'Q975568')",
            (place_id,),
        )
        cur.execute(
            "INSERT INTO place_chunks (place_id, content, content_hash) VALUES (%s, 'Chùa xây năm 1601.', 'h')",
            (place_id,),
        )
    run_embed_chunks(db_conn, embedder=DeterministicEmbedder())
    queries = tmp_path / "q.yml"
    queries.write_text(
        "- {id: a, query: chùa xây năm nào, type: fact, relevant: [{qid: Q975568}],"
        " answer_keywords: ['1601']}\n"
        "- {id: b, query: lăng, type: fact, relevant: [{qid: Q7481171}]}\n",
        encoding="utf-8",
    )

    report, results = benchmark.run(
        db_conn, queries_path=str(queries), ks=(1, 5), main_k=5,
        embedder=DeterministicEmbedder(), out_dir=str(tmp_path),
    )

    assert set(results) == {
        "hybrid:deterministic+bm25_multi", "hybrid:deterministic+bm25_fold",
        "dense:deterministic", "bm25", "bm25_fold", "bm25_multi", "random",
    }
    # Kho chỉ có một chunk, của đúng địa danh: mọi retriever đều trúng.
    assert all(r.metrics["hit@5"] == 1.0 for rs in results.values() for r in rs)
    assert "`b`: QID không có trong DB: Q7481171" in report
    assert list(tmp_path.glob("rag_benchmark_*.md")) and list(tmp_path.glob("rag_benchmark_*.csv"))
