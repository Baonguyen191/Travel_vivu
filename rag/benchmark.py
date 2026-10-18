"""Benchmark RAG: chất lượng truy hồi, độ trễ, và sinh câu trả lời (TTFT, tokens/s).

    # Truy hồi: BM25 (3 analyzer), dense và hybrid cho từng embedder, random
    python -m rag.benchmark retrieval --embedders bge-m3 e5-base e5-large

    # Sinh: retriever đã chọn + LLM qua Ollama
    python -m rag.benchmark generation --embedder bge-m3 --mode hybrid --models qwen3.5:4b qwen3.5:9b

Kết quả ghi vào `data/qa/rag/<ngày>/`. Notebook `notebooks/eval_rag.ipynb` gọi cùng các hàm này.
Bộ câu hỏi `eval/rag_queries.yml`: mặc định chỉ chấm câu `reviewed: true` (đã duyệt bằng
`scripts/review_rag_queries.py`); `--all` chấm cả câu chưa duyệt, kèm cảnh báo.
"""

from __future__ import annotations

import argparse
import gc
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd
import yaml

from rag import DEFAULT_EMBEDDER, DEFAULT_LLM, DEFAULT_RETRIEVER_MODE, ROOT
from rag.corpus import Chunk, corpus_hash, load_chunks
from rag.dense import EMBEDDERS, BuildStats, build_index
from rag.generate import REFUSAL_MARKER, build_messages, check_models, make_client, stream_answer, warm_up
from rag.metrics import (
    bootstrap_ci, hit_at_k, keyword_recall, ndcg_at_k, paired_bootstrap_diff, percentile,
    place_prf_at_k, precision_at_k, reciprocal_rank,
)
from rag.retrievers import BM25Retriever, DenseRetriever, HybridRetriever, RandomRetriever, Retriever

QUERIES_PATH = ROOT / "eval" / "rag_queries.yml"
OUT_ROOT = ROOT / "data" / "qa" / "rag"
K_VALUES = (1, 3, 5, 10)
TOP_K = 10
CONTEXT_K = 5
MAIN_METRICS = ("hit@1", "hit@5", "mrr@10", "ndcg@5", "p@5", "place_precision@5", "place_recall@5",
                "place_f1@5", "answer_hit@5")
_CITATION = re.compile(r"\[\d+\]")


@dataclass
class Query:
    id: str
    query: str
    type: str
    relevant: set[str]
    answer_keywords: list[str] = field(default_factory=list)
    reviewed: bool = False


def load_queries(path: Path = QUERIES_PATH, reviewed_only: bool = False) -> list[Query]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = raw["queries"] if isinstance(raw, dict) else raw
    queries = [Query(q["id"], q["query"], q["type"], {f"wikidata:{r['qid']}" for r in q["relevant"]},
                     list(q.get("answer_keywords") or []), bool(q.get("reviewed"))) for q in items]
    return [q for q in queries if q.reviewed] if reviewed_only else queries


# ---------------------------------------------------------------- truy hồi

def build_retrievers(chunks: Sequence[Chunk], embedder_keys: Sequence[str]) -> tuple[dict[str, Retriever], list[BuildStats]]:
    texts = [c.embedding_text for c in chunks]
    retrievers: dict[str, Retriever] = {}
    for analyzer in ("plain", "fold", "multi"):
        r = BM25Retriever(texts, analyzer)
        retrievers[r.name] = r
    stats = []
    key = corpus_hash(list(chunks))
    for emb_key in embedder_keys:
        embedder, matrix, s = build_index(EMBEDDERS[emb_key], texts, key)
        stats.append(s)
        dense = DenseRetriever(embedder, matrix)
        hybrid = HybridRetriever(dense, retrievers["bm25_multi"])
        retrievers[dense.name] = dense
        retrievers[hybrid.name] = hybrid
    rnd = RandomRetriever(len(chunks))
    retrievers[rnd.name] = rnd
    return retrievers, stats


def free_gpu_cache() -> None:
    """Trả VRAM cho Ollama trước khi chạy LLM 9B. Bên gọi phải bỏ tham chiếu tới embedder trước."""
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def score_ranking(ranking: Sequence[int], chunks: Sequence[Chunk], q: Query, n_relevant: int) -> dict:
    flags = [chunks[i].place_key in q.relevant for i in ranking]
    places = [chunks[i].place_key for i in ranking]
    row = {f"hit@{k}": hit_at_k(flags, k) for k in K_VALUES}
    row["mrr@10"] = reciprocal_rank(flags, 10)
    row["ndcg@5"] = ndcg_at_k(flags, 5, n_relevant)
    row["p@5"] = precision_at_k(flags, 5)
    p, r, f1 = place_prf_at_k(places, q.relevant, 5)
    row.update({"place_precision@5": p, "place_recall@5": r, "place_f1@5": f1})
    for k in K_VALUES:
        row[f"place_recall@{k}"] = place_prf_at_k(places, q.relevant, k)[1]
    if q.answer_keywords:
        row["answer_hit@5"] = float(any(
            flags[i] and keyword_recall(chunks[ranking[i]].text, q.answer_keywords) == 1.0
            for i in range(min(5, len(ranking)))))
    else:
        row["answer_hit@5"] = float("nan")
    return row


def run_retrieval(retrievers: dict[str, Retriever], chunks: Sequence[Chunk], queries: Sequence[Query],
                  k: int = TOP_K) -> pd.DataFrame:
    """Một dòng mỗi (retriever, câu hỏi). Câu không có chunk nào của địa danh đúng: `evaluable=False`."""
    n_relevant = {q.id: sum(c.place_key in q.relevant for c in chunks) for q in queries}
    rows = []
    for name, retriever in retrievers.items():
        retriever.search("khởi động", k)  # nạp lazy / CUDA kernel, không tính vào độ trễ
        for q in queries:
            start = time.perf_counter()
            ranking = retriever.search(q.query, k)
            latency_ms = (time.perf_counter() - start) * 1000
            row = {"retriever": name, "id": q.id, "type": q.type, "reviewed": q.reviewed,
                   "evaluable": n_relevant[q.id] > 0, "latency_ms": latency_ms,
                   "top_chunks": " | ".join(f"{chunks[i].title}#{chunks[i].chunk_id}" for i in ranking[:3])}
            row.update(score_ranking(ranking, chunks, q, n_relevant[q.id]))
            rows.append(row)
    return pd.DataFrame(rows)


def summarize_retrieval(df: pd.DataFrame, metrics: Sequence[str] = MAIN_METRICS) -> pd.DataFrame:
    """Trung bình + CI 95% mỗi chỉ số, p50/p95 độ trễ; xếp theo MRR giảm dần."""
    df = df[df["evaluable"]]
    rows = []
    for name, g in df.groupby("retriever", sort=False):
        row = {"retriever": name, "n": len(g)}
        for m in metrics:
            mean, lo, hi = bootstrap_ci(g[m].tolist())
            row[m] = mean
            row[f"{m}_ci"] = f"[{lo:.3f}, {hi:.3f}]"
        row["latency_p50_ms"] = percentile(g["latency_ms"].tolist(), 50)
        row["latency_p95_ms"] = percentile(g["latency_ms"].tolist(), 95)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("mrr@10", ascending=False).reset_index(drop=True)


def by_type(df: pd.DataFrame, metric: str = "hit@5") -> pd.DataFrame:
    df = df[df["evaluable"]]
    return df.pivot_table(index="retriever", columns="type", values=metric, aggfunc="mean")


def compare(df: pd.DataFrame, a: str, b: str, metric: str) -> tuple[float, float, float]:
    """Chênh lệch `metric` giữa retriever a và b trên cùng câu hỏi, kèm CI 95%."""
    df = df[df["evaluable"]]
    left = df[df["retriever"] == a].set_index("id")[metric]
    right = df[df["retriever"] == b].set_index("id")[metric]
    ids = [i for i in left.index if i in right.index and not (np.isnan(left[i]) or np.isnan(right[i]))]
    return paired_bootstrap_diff(left[ids].tolist(), right[ids].tolist())


# ---------------------------------------------------------------- sinh câu trả lời

def run_generation(retriever: Retriever, chunks: Sequence[Chunk], queries: Sequence[Query],
                   models: Sequence[str], client=None, k: int = CONTEXT_K) -> tuple[pd.DataFrame, dict[str, float]]:
    """Một dòng mỗi (model, câu hỏi) và {model → thời gian nạp lần đầu (s)}."""
    client = client or make_client()
    check_models(client, models)
    contexts = {}
    for q in queries:
        start = time.perf_counter()
        ranking = retriever.search(q.query, k)
        contexts[q.id] = (ranking, (time.perf_counter() - start) * 1000)
    rows, cold_start = [], {}
    for model in models:
        cold_start[model] = warm_up(client, model)
        for q in queries:
            ranking, retrieval_ms = contexts[q.id]
            result = stream_answer(client, model, build_messages(q.query, [chunks[i] for i in ranking]))
            rows.append({
                "model": model, "id": q.id, "type": q.type, "reviewed": q.reviewed,
                "retrieval_ms": retrieval_ms,
                "ttft_ms": result.ttft_s * 1000,
                "e2e_ttft_ms": retrieval_ms + result.ttft_s * 1000,
                "total_ms": result.total_s * 1000,
                "e2e_total_ms": retrieval_ms + result.total_s * 1000,
                "output_tokens": result.output_tokens,
                "tokens_exact": result.tokens_exact,
                "tokens_per_s": result.tokens_per_s,
                "keyword_recall": keyword_recall(result.answer, q.answer_keywords),
                "context_has_answer": any(chunks[i].place_key in q.relevant for i in ranking),
                "cited": bool(_CITATION.search(result.answer)),
                "refused": REFUSAL_MARKER in result.answer.casefold(),
                "answer": result.answer,
            })
    return pd.DataFrame(rows), cold_start


def summarize_generation(df: pd.DataFrame, cold_start: dict[str, float] | None = None) -> pd.DataFrame:
    rows = []
    for model, g in df.groupby("model", sort=False):
        row = {"model": model, "n": len(g)}
        for col in ("ttft_ms", "e2e_ttft_ms", "total_ms", "e2e_total_ms"):
            row[f"{col}_p50"] = percentile(g[col].tolist(), 50)
            row[f"{col}_p95"] = percentile(g[col].tolist(), 95)
        row["tokens_per_s_mean"], lo, hi = bootstrap_ci(g["tokens_per_s"].tolist())
        row["tokens_per_s_ci"] = f"[{lo:.1f}, {hi:.1f}]"
        kr, lo, hi = bootstrap_ci(g["keyword_recall"].tolist())
        row["keyword_recall"], row["keyword_recall_ci"] = kr, f"[{lo:.3f}, {hi:.3f}]"
        row["citation_rate"] = g["cited"].mean()
        row["refusal_rate"] = g["refused"].mean()
        row["tokens_exact"] = bool(g["tokens_exact"].all())
        if cold_start:
            row["cold_start_s"] = cold_start.get(model, float("nan"))
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- CLI

def _out_dir() -> Path:
    out = OUT_ROOT / date.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _select_queries(include_unreviewed: bool) -> list[Query]:
    """Mặc định chỉ chấm câu đã kiểm duyệt; `--all` chấm cả câu chưa duyệt (kèm cảnh báo)."""
    queries = load_queries()
    n_unreviewed = sum(not q.reviewed for q in queries)
    if include_unreviewed:
        if n_unreviewed:
            print(f"CẢNH BÁO: {n_unreviewed}/{len(queries)} câu hỏi chưa kiểm duyệt (reviewed: false). "
                  "Chưa dùng số liệu này trong báo cáo.")
        return queries
    print(f"Chấm {len(queries) - n_unreviewed} câu đã kiểm duyệt (bỏ {n_unreviewed} câu chưa duyệt / bị đánh sai).")
    return [q for q in queries if q.reviewed]


def cmd_retrieval(args) -> None:
    chunks, queries = load_chunks(), _select_queries(args.all)
    retrievers, stats = build_retrievers(chunks, args.embedders)
    df = run_retrieval(retrievers, chunks, queries)
    out = _out_dir()
    df.to_csv(out / "retrieval_per_query.csv", index=False, encoding="utf-8")
    summary = summarize_retrieval(df)
    summary.to_csv(out / "retrieval_summary.csv", index=False, encoding="utf-8")
    pd.DataFrame([{**s.__dict__, "chunks_per_second": s.chunks_per_second} for s in stats]).to_csv(
        out / "index_build.csv", index=False, encoding="utf-8")
    cols = ["retriever", "n", "hit@1", "hit@5", "mrr@10", "ndcg@5", "place_f1@5", "latency_p50_ms", "latency_p95_ms"]
    print(summary[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print(f"→ {out}")


def cmd_generation(args) -> None:
    chunks, queries = load_chunks(), _select_queries(args.all)
    texts = [c.embedding_text for c in chunks]
    lexical = BM25Retriever(texts, "multi")
    if args.mode == "bm25":
        retriever: Retriever = lexical
    else:
        embedder, matrix, _ = build_index(EMBEDDERS[args.embedder], texts, corpus_hash(chunks))
        if args.embed_device:
            embedder.to(args.embed_device)
            free_gpu_cache()
        dense = DenseRetriever(embedder, matrix)
        retriever = dense if args.mode == "dense" else HybridRetriever(dense, lexical)
    df, cold = run_generation(retriever, chunks, queries, args.models)
    df["retriever"] = retriever.name
    df["embed_device"] = args.embed_device or "default"
    out = _out_dir()
    # Hậu tố riêng để không ghi đè kết quả của notebook hoặc lần chạy khác trong ngày.
    tag = "_".join([retriever.name, *args.models, args.embed_device or "default"]).replace(":", "-")
    df.to_csv(out / f"generation_per_query__{tag}.csv", index=False, encoding="utf-8")
    summary = summarize_generation(df, cold)
    summary.to_csv(out / f"generation_summary__{tag}.csv", index=False, encoding="utf-8")
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    print(f"→ {out}")


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ret = sub.add_parser("retrieval")
    ret.add_argument("--embedders", nargs="*", default=list(EMBEDDERS), choices=list(EMBEDDERS))
    ret.set_defaults(func=cmd_retrieval)
    gen = sub.add_parser("generation")
    gen.add_argument("--embedder", default=DEFAULT_EMBEDDER, choices=list(EMBEDDERS))
    gen.add_argument("--mode", default=DEFAULT_RETRIEVER_MODE, choices=["hybrid", "dense", "bm25"])
    gen.add_argument("--models", nargs="+", default=[DEFAULT_LLM])
    gen.add_argument("--embed-device", default=None, choices=["cuda", "cpu"],
                     help="cpu: nhường toàn bộ VRAM cho LLM khi so sánh model lớn")
    gen.set_defaults(func=cmd_generation)
    for p in (ret, gen):
        p.add_argument("--all", action="store_true", help="chấm cả câu chưa kiểm duyệt")
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
