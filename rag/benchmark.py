"""Benchmark truy hồi RAG trên bộ câu hỏi gán nhãn tay (eval/rag_queries.yml).

Các retriever so sánh (retriever đứng đầu `--retrievers` được so cặp với
từng retriever còn lại):

- hybrid: dense + bm25_multi ghép bằng RRF (rag/hybrid.py). Retriever agent dùng.
- hybrid_fold: dense + bm25_fold, bản hybrid trước khi có analyzer multi.
- dense: model embedding + pgvector (rag/retriever.py).
- bm25 / bm25_fold / bm25_multi: BM25 với ba analyzer (rag/lexical.py): giữ
  dấu, bỏ dấu, và kết hợp giữ dấu + bỏ dấu + bigram bỏ dấu.
- random: thứ tự ngẫu nhiên có seed. Mức sàn, cho biết chỉ số "may mắn" là
  bao nhiêu với cỡ kho chunk hiện tại.

hybrid so với dense và bm25_multi là ablation: cho thấy mỗi nhánh đóng góp gì.
Mặc định mọi retriever loại chunk của category không phải điểm đến
(NON_DESTINATION_CATEGORIES); `--all-places` tìm trên toàn kho để đo tác động
của việc lọc.

Nhãn gán ở mức địa danh (QID Wikidata), không ở mức chunk: chunk sinh lại mỗi
khi bài Wikipedia đổi, còn QID thì cố định. Câu dạng `fact` có thêm
`answer_keywords`; chunk thuộc địa danh đúng và chứa đủ các từ khoá đó được
coi là chunk chứa câu trả lời.

Câu hỏi mà mọi địa danh đúng đều không có chunk nào trong DB bị loại khỏi
số liệu tổng hợp và liệt kê riêng: đó là lỗ hổng dữ liệu, không phải lỗi
truy hồi, và gộp chung sẽ làm hai loại lỗi lẫn vào nhau.

Chạy:
    python -m rag.benchmark
    python -m rag.benchmark --retrievers hybrid,dense --k 1,3,5,10
"""

import argparse
from collections import Counter
import csv
from dataclasses import dataclass, field
from datetime import date
import math
from pathlib import Path
import random
import re
import time
from typing import Protocol, Sequence

import psycopg
import yaml

from pipeline.embed import BaseEmbedder, get_embedder
from rag.hybrid import HybridRetriever
from rag.lexical import BM25Retriever, load_corpus
from rag.retriever import NON_DESTINATION_CATEGORIES, RAGChunk, RAGRetriever

DEFAULT_QUERIES_PATH = "eval/rag_queries.yml"
DEFAULT_OUT_DIR = "data/qa"
DEFAULT_RETRIEVERS = (
    "hybrid", "hybrid_fold", "dense", "bm25", "bm25_fold", "bm25_multi", "random",
)
QUERY_TYPES = {"fact", "descriptive", "paraphrase", "multi", "no_diacritics"}
_QID = re.compile(r"^Q\d+$")


# ---------------------------------------------------------------------------
# Bộ câu hỏi
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldQuery:
    id: str
    query: str
    type: str
    relevant_qids: tuple[str, ...]
    answer_keywords: tuple[str, ...] = ()
    category: str | None = None
    reviewed: bool = False


def load_queries(path: str | Path = DEFAULT_QUERIES_PATH) -> list[GoldQuery]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    queries: list[GoldQuery] = []
    seen: set[str] = set()
    for item in raw:
        qid = str(item.get("id", "")).strip()
        where = f"câu '{qid or '?'}' trong {path}"
        if not qid or qid in seen:
            raise ValueError(f"{where}: id rỗng hoặc trùng")
        seen.add(qid)
        text = str(item.get("query", "")).strip()
        if not text:
            raise ValueError(f"{where}: query rỗng")
        qtype = item.get("type")
        if qtype not in QUERY_TYPES:
            raise ValueError(f"{where}: type '{qtype}' không thuộc {sorted(QUERY_TYPES)}")
        relevant = tuple(r["qid"] for r in item.get("relevant") or [])
        if not relevant or not all(_QID.match(r) for r in relevant):
            raise ValueError(f"{where}: cần ít nhất một relevant có qid dạng Q123")
        queries.append(
            GoldQuery(
                id=qid,
                query=text,
                type=qtype,
                relevant_qids=relevant,
                answer_keywords=tuple(item.get("answer_keywords") or ()),
                category=item.get("category"),
                reviewed=bool(item.get("reviewed", False)),
            )
        )
    return queries


# ---------------------------------------------------------------------------
# Chỉ số. `rels[i]` là True khi chunk ở hạng i+1 thuộc một địa danh đúng.
# ---------------------------------------------------------------------------


def hit_at_k(rels: Sequence[bool], k: int) -> float:
    """1 nếu top-k có ít nhất một chunk đúng."""
    return float(any(rels[:k]))


def precision_at_k(rels: Sequence[bool], k: int) -> float:
    """Tỷ lệ chunk đúng trong k chunk đưa vào prompt; phần còn lại là nhiễu."""
    return sum(rels[:k]) / k


def reciprocal_rank(rels: Sequence[bool], k: int) -> float:
    """1/hạng của chunk đúng đầu tiên trong top-k, 0 nếu không có."""
    for rank, rel in enumerate(rels[:k], 1):
        if rel:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(rels: Sequence[bool], n_relevant: int, k: int) -> float:
    """nDCG nhị phân. `n_relevant` là số chunk đúng có trong kho.

    Mọi chunk của một địa danh đúng đều được tính là đúng, nên DCG lý tưởng
    là min(k, n_relevant) chunk đúng xếp liền nhau ở đầu.
    """
    dcg = sum(1.0 / math.log2(rank + 1) for rank, rel in enumerate(rels[:k], 1) if rel)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(k, n_relevant) + 1))
    return dcg / ideal if ideal > 0 else 0.0


def recall_at_k(ranked_places: Sequence[int], relevant: set[int], k: int) -> float:
    """Tỷ lệ địa danh đúng xuất hiện trong top-k. Có nghĩa với câu `multi`."""
    if not relevant:
        return 0.0
    return len(relevant & set(ranked_places[:k])) / len(relevant)


def paired_bootstrap(
    a: Sequence[float], b: Sequence[float], n_resamples: int = 10_000, seed: int = 0
) -> tuple[float, float, float]:
    """(chênh lệch trung bình a - b, cận dưới 95%, cận trên 95%).

    Lấy mẫu lại theo câu hỏi, giữ cặp (a_i, b_i) đi cùng nhau. Khoảng tin
    cậy không chứa 0 thì chênh lệch đáng tin với cỡ bộ câu hỏi hiện tại.
    """
    if len(a) != len(b) or not a:
        raise ValueError("a và b phải cùng độ dài và không rỗng")
    diffs = [x - y for x, y in zip(a, b)]
    n = len(diffs)
    rng = random.Random(seed)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_resamples))
    lo = means[int(0.025 * n_resamples)]
    hi = means[int(0.975 * n_resamples) - 1]
    return sum(diffs) / n, lo, hi


def percentile(values: Sequence[float], p: float) -> float:
    """Percentile theo nearest-rank, `p` trong (0, 1]."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


# ---------------------------------------------------------------------------
# Retriever baseline
# ---------------------------------------------------------------------------


class Retriever(Protocol):
    name: str

    def search(self, query: str, top_k: int = 5, category: str | None = None) -> list[RAGChunk]: ...


class RandomRetriever:
    name = "random"

    def __init__(self, corpus: Sequence[RAGChunk], seed: int = 0):
        self.corpus = list(corpus)
        self.seed = seed

    def search(self, query: str, top_k: int = 5, category: str | None = None) -> list[RAGChunk]:
        pool = [c for c in self.corpus if not category or c.category == category]
        rng = random.Random(f"{self.seed}:{query}")
        return rng.sample(pool, min(top_k, len(pool)))


# ---------------------------------------------------------------------------
# Đọc DB
# ---------------------------------------------------------------------------


def resolve_qids(conn: psycopg.Connection, qids: Sequence[str]) -> dict[str, int]:
    """QID Wikidata -> place_id. QID không có trong DB thì vắng mặt."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT external_id, place_id FROM place_external_ids"
            " WHERE source = 'wikidata' AND external_id = ANY(%s)",
            (list(qids),),
        )
        return dict(cur.fetchall())


# ---------------------------------------------------------------------------
# Chạy benchmark
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    query: GoldQuery
    retriever: str
    chunks: list[RAGChunk]
    latency_ms: float
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class Judgements:
    """Nhãn đã quy về place_id, cùng các câu bị loại vì thiếu dữ liệu."""

    relevant: dict[str, set[int]]
    chunk_counts: dict[int, int]
    missing_qids: dict[str, list[str]]
    no_chunks: list[str]


def build_judgements(queries: Sequence[GoldQuery], qid_to_place: dict[str, int],
                     corpus: Sequence[RAGChunk]) -> Judgements:
    chunk_counts = Counter(c.place_id for c in corpus)
    relevant: dict[str, set[int]] = {}
    missing: dict[str, list[str]] = {}
    no_chunks: list[str] = []
    for q in queries:
        places = {qid_to_place[qid] for qid in q.relevant_qids if qid in qid_to_place}
        absent = [qid for qid in q.relevant_qids if qid not in qid_to_place]
        if absent:
            missing[q.id] = absent
        if sum(chunk_counts[p] for p in places) == 0:
            no_chunks.append(q.id)
        else:
            relevant[q.id] = places
    return Judgements(relevant, dict(chunk_counts), missing, no_chunks)


def score_query(q: GoldQuery, chunks: Sequence[RAGChunk], relevant: set[int],
                n_relevant: int, ks: Sequence[int]) -> dict[str, float]:
    max_k = max(ks)
    rels = [c.place_id in relevant for c in chunks]
    places = [c.place_id for c in chunks]
    metrics = {"mrr": reciprocal_rank(rels, max_k)}
    for k in ks:
        metrics[f"hit@{k}"] = hit_at_k(rels, k)
        metrics[f"precision@{k}"] = precision_at_k(rels, k)
        metrics[f"ndcg@{k}"] = ndcg_at_k(rels, n_relevant, k)
        if q.type == "multi":
            metrics[f"recall@{k}"] = recall_at_k(places, relevant, k)
    if q.answer_keywords:
        keywords = [kw.casefold() for kw in q.answer_keywords]
        answer = [
            rel and all(kw in c.content.casefold() for kw in keywords)
            for rel, c in zip(rels, chunks)
        ]
        metrics["answer_mrr"] = reciprocal_rank(answer, max_k)
        for k in ks:
            metrics[f"answer_hit@{k}"] = hit_at_k(answer, k)
    return metrics


def evaluate(retriever: Retriever, queries: Sequence[GoldQuery], judgements: Judgements,
             ks: Sequence[int] = (1, 3, 5, 10)) -> list[QueryResult]:
    results = []
    for q in queries:
        if q.id not in judgements.relevant:
            continue
        relevant = judgements.relevant[q.id]
        started = time.perf_counter()
        chunks = retriever.search(q.query, top_k=max(ks), category=q.category)
        latency_ms = (time.perf_counter() - started) * 1000
        n_relevant = sum(judgements.chunk_counts.get(p, 0) for p in relevant)
        results.append(
            QueryResult(q, retriever.name, list(chunks), latency_ms,
                        score_query(q, chunks, relevant, n_relevant, ks))
        )
    return results


def summarize(results: Sequence[QueryResult]) -> dict[str, tuple[float, int]]:
    """metric -> (trung bình, số câu có metric đó)."""
    buckets: dict[str, list[float]] = {}
    for r in results:
        for name, value in r.metrics.items():
            buckets.setdefault(name, []).append(value)
    summary = {name: (sum(v) / len(v), len(v)) for name, v in buckets.items()}
    latencies = [r.latency_ms for r in results]
    if latencies:
        summary["latency_p50_ms"] = (percentile(latencies, 0.5), len(latencies))
        summary["latency_p95_ms"] = (percentile(latencies, 0.95), len(latencies))
    return summary


# ---------------------------------------------------------------------------
# Báo cáo
# ---------------------------------------------------------------------------


def _fmt(summary: dict[str, tuple[float, int]], metric: str, digits: int = 3) -> str:
    if metric not in summary:
        return "—"
    return f"{summary[metric][0]:.{digits}f}"


def render_report(
    queries: Sequence[GoldQuery],
    judgements: Judgements,
    results: dict[str, list[QueryResult]],
    ks: Sequence[int],
    main_k: int,
    index_note: str,
    queries_path: str,
) -> str:
    by_id = {q.id: q for q in queries}
    reviewed = sum(q.reviewed for q in queries)
    evaluated = len(judgements.relevant)
    lines = [
        f"# Benchmark truy hồi RAG — {date.today().isoformat()}",
        "",
        f"- Bộ câu hỏi: `{queries_path}`, {len(queries)} câu, {reviewed} câu đã kiểm duyệt tay.",
        f"- Được chấm: {evaluated} câu. Loại vì không có chunk nào của địa danh đúng:"
        f" {len(judgements.no_chunks)} câu.",
        f"- {index_note}",
    ]
    if reviewed < len(queries):
        lines.append(
            f"- **Cảnh báo:** {len(queries) - reviewed} câu chưa kiểm duyệt tay. Chưa dùng"
            " số liệu này trong báo cáo đồ án."
        )
    lines += ["", "## Tổng hợp", ""]

    header = ["Retriever"] + [f"Hit@{k}" for k in ks] + [
        f"MRR@{max(ks)}", f"nDCG@{main_k}", f"P@{main_k}", f"AnswerHit@{main_k}",
        "p50 ms", "p95 ms",
    ]
    lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    summaries = {name: summarize(rs) for name, rs in results.items()}
    for name, s in summaries.items():
        row = [name] + [_fmt(s, f"hit@{k}") for k in ks] + [
            _fmt(s, "mrr"), _fmt(s, f"ndcg@{main_k}"), _fmt(s, f"precision@{main_k}"),
            _fmt(s, f"answer_hit@{main_k}"),
            _fmt(s, "latency_p50_ms", 1), _fmt(s, "latency_p95_ms", 1),
        ]
        lines.append("| " + " | ".join(row) + " |")
    n_answer = next((s[f"answer_hit@{main_k}"][1] for s in summaries.values()
                     if f"answer_hit@{main_k}" in s), 0)
    lines += ["", f"AnswerHit tính trên {n_answer} câu có `answer_keywords`."]

    types = sorted({by_id[qid].type for qid in judgements.relevant})
    lines += ["", f"## Theo loại câu hỏi (Hit@{main_k} / MRR)", ""]
    lines += ["| Retriever | " + " | ".join(types) + " |", "|" + "---|" * (len(types) + 1)]
    for name, rs in results.items():
        cells = []
        for t in types:
            s = summarize([r for r in rs if r.query.type == t])
            cells.append(f"{_fmt(s, f'hit@{main_k}')} / {_fmt(s, 'mrr')} (n={s['mrr'][1]})"
                         if s else "—")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    if any(q.type == "multi" for q in queries):
        lines += ["", "## Recall@k cho câu nhiều địa danh (`multi`)", ""]
        lines += ["| Retriever | " + " | ".join(f"Recall@{k}" for k in ks) + " |",
                  "|" + "---|" * (len(ks) + 1)]
        for name, rs in results.items():
            s = summarize([r for r in rs if r.query.type == "multi"])
            lines.append(f"| {name} | " + " | ".join(_fmt(s, f"recall@{k}") for k in ks) + " |")

    names = list(results)
    if len(names) >= 2:
        lines += ["", "## So sánh cặp (bootstrap 95%, lấy mẫu lại theo câu hỏi)", ""]
        lines += ["| So sánh | Chỉ số | Chênh lệch | CI 95% |", "|---|---|---|---|"]
        first = names[0]
        for other in names[1:]:
            for metric in ("mrr", f"hit@{main_k}"):
                a = [r.metrics[metric] for r in results[first]]
                b = [r.metrics[metric] for r in results[other]]
                diff, lo, hi = paired_bootstrap(a, b)
                lines.append(f"| {first} − {other} | {metric} | {diff:+.3f} | [{lo:+.3f}, {hi:+.3f}] |")

    lines += ["", f"## Câu trượt top-{main_k} (phân tích lỗi)", ""]
    for name, rs in results.items():
        misses = [r for r in rs if r.metrics[f"hit@{main_k}"] == 0]
        lines.append(f"### {name}: {len(misses)} câu")
        for r in misses:
            got = ", ".join(dict.fromkeys(c.place_name for c in r.chunks[:3])) or "(không có kết quả)"
            lines.append(f"- `{r.query.id}` ({r.query.type}) \"{r.query.query}\" → top-3: {got}")
        lines.append("")

    if judgements.no_chunks or judgements.missing_qids:
        lines += ["## Lỗ hổng dữ liệu", ""]
        for qid in judgements.no_chunks:
            lines.append(f"- `{qid}` bị loại: không địa danh đúng nào có chunk. \"{by_id[qid].query}\"")
        for qid, absent in judgements.missing_qids.items():
            lines.append(f"- `{qid}`: QID không có trong DB: {', '.join(absent)}")
        lines.append("")
    return "\n".join(lines)


def write_csv(path: Path, results: dict[str, list[QueryResult]], ks: Sequence[int]) -> None:
    metric_names = sorted({m for rs in results.values() for r in rs for m in r.metrics})
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["query_id", "type", "retriever", "latency_ms", *metric_names,
                         f"top{max(ks)}_places"])
        for rs in results.values():
            for r in rs:
                writer.writerow([
                    r.query.id, r.query.type, r.retriever, f"{r.latency_ms:.1f}",
                    *(f"{r.metrics[m]:.4f}" if m in r.metrics else "" for m in metric_names),
                    " | ".join(c.place_name for c in r.chunks),
                ])


def run(
    conn: psycopg.Connection,
    retriever_names: Sequence[str] = DEFAULT_RETRIEVERS,
    queries_path: str = DEFAULT_QUERIES_PATH,
    ks: Sequence[int] = (1, 3, 5, 10),
    main_k: int = 5,
    model_name: str | None = None,
    out_dir: str | None = DEFAULT_OUT_DIR,
    embedder: BaseEmbedder | None = None,
    exclude_categories: Sequence[str] = NON_DESTINATION_CATEGORIES,
) -> tuple[str, dict[str, list[QueryResult]]]:
    """Chạy benchmark, ghi báo cáo .md và .csv vào `out_dir` (None: không ghi).

    Truyền `embedder` đã tải sẵn để khỏi tải lại model (notebook); nếu không,
    tạo theo `model_name` hoặc EMBEDDING_MODEL.
    """
    if main_k not in ks:
        raise ValueError(f"main_k={main_k} phải nằm trong ks={list(ks)}")
    queries = load_queries(queries_path)
    corpus = load_corpus(conn, exclude_categories)
    qid_to_place = resolve_qids(conn, sorted({qid for q in queries for qid in q.relevant_qids}))
    judgements = build_judgements(queries, qid_to_place, corpus)

    dense: RAGRetriever | None = None
    if {"dense", "hybrid"} & set(retriever_names):
        dense = RAGRetriever(conn, embedder or get_embedder(model_name), exclude_categories)
        total, embedded = dense.index_status()
        index_note = f"Kho: {total} chunk, {embedded} chunk có embedding của `{dense.embedder.model_name}`."
        if embedded < total:
            index_note += (f" **Index chưa đủ: dense chỉ thấy {embedded}/{total} chunk,"
                           " so sánh với BM25/random chưa công bằng. Chạy `python -m pipeline embed`.**")
    else:
        index_note = ""
    excluded = ", ".join(f"`{c}`" for c in exclude_categories) or "không loại gì"
    index_note += f" Loại chunk của category: {excluded}; còn {len(corpus)} chunk để tìm."

    lexical = {a: BM25Retriever(corpus, analyzer=a) for a in ("plain", "fold", "multi")}
    factories = {
        "hybrid": lambda: HybridRetriever(dense, lexical["multi"]),
        "hybrid_fold": lambda: HybridRetriever(dense, lexical["fold"]),
        "dense": lambda: dense,
        "bm25": lambda: lexical["plain"],
        "bm25_fold": lambda: lexical["fold"],
        "bm25_multi": lambda: lexical["multi"],
        "random": lambda: RandomRetriever(corpus),
    }
    unknown = [n for n in retriever_names if n not in factories]
    if unknown:
        raise ValueError(f"Retriever không hợp lệ: {unknown}. Chọn trong {', '.join(factories)}.")
    retrievers: list[Retriever] = [factories[n]() for n in retriever_names]

    results = {r.name: evaluate(r, queries, judgements, ks) for r in retrievers}
    report = render_report(queries, judgements, results, ks, main_k, index_note, queries_path)

    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"rag_benchmark_{date.today().isoformat()}"
        if not exclude_categories:
            stem += "_all_places"
        (out / f"{stem}.md").write_text(report, encoding="utf-8")
        write_csv(out / f"{stem}.csv", results, ks)
    return report, results


def main() -> int:
    parser = argparse.ArgumentParser(prog="rag.benchmark")
    parser.add_argument("--queries", default=DEFAULT_QUERIES_PATH)
    parser.add_argument("--retrievers", default=",".join(DEFAULT_RETRIEVERS))
    parser.add_argument("--k", default="1,3,5,10")
    parser.add_argument("--main-k", type=int, default=5)
    parser.add_argument("--model", default=None, help="Mặc định lấy từ EMBEDDING_MODEL")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--all-places", action="store_true",
                        help="Không loại category không phải điểm đến (ablation)")
    args = parser.parse_args()

    from pipeline import db

    with db.connect() as conn:
        report, _ = run(
            conn,
            retriever_names=[r.strip() for r in args.retrievers.split(",") if r.strip()],
            queries_path=args.queries,
            ks=sorted({int(k) for k in args.k.split(",")}),
            main_k=args.main_k,
            model_name=args.model,
            out_dir=args.out_dir,
            exclude_categories=() if args.all_places else NON_DESTINATION_CATEGORIES,
        )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
