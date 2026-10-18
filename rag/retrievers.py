"""Các retriever cùng giao diện:

- `search(query, k) -> list[int]`: chỉ số chunk, xếp hạng giảm dần (benchmark dùng).
- `search_scored(query, k) -> list[tuple[int, float]]`: kèm điểm của retriever đó (`rag.api` dùng).
  Thang điểm khác nhau theo loại: BM25 ≥ 0, dense là cosine ∈ [−1, 1], hybrid là điểm RRF.
"""

from __future__ import annotations

import random
from typing import Protocol, Sequence

import numpy as np

from rag.dense import Embedder
from rag.lexical import BM25

RRF_K = 60


class Retriever(Protocol):
    name: str

    def search(self, query: str, k: int) -> list[int]: ...

    def search_scored(self, query: str, k: int) -> list[tuple[int, float]]: ...


def _top_k_scored(scores: Sequence[float] | np.ndarray, k: int) -> list[tuple[int, float]]:
    scores = np.asarray(scores)
    k = min(k, len(scores))
    if k <= 0:
        return []
    idx = np.argpartition(-scores, k - 1)[:k]
    # Hoà điểm: chỉ số nhỏ trước, để kết quả lặp lại được.
    return [(i, float(scores[i])) for i in sorted(idx.tolist(), key=lambda i: (-scores[i], i))]


def _top_k(scores: Sequence[float] | np.ndarray, k: int) -> list[int]:
    return [i for i, _ in _top_k_scored(scores, k)]


class BM25Retriever:
    def __init__(self, texts: Sequence[str], analyzer: str = "multi"):
        self.name = f"bm25_{analyzer}"
        self.index = BM25(texts, analyzer)

    def search_scored(self, query: str, k: int) -> list[tuple[int, float]]:
        return _top_k_scored(self.index.scores(query), k)

    def search(self, query: str, k: int) -> list[int]:
        return [i for i, _ in self.search_scored(query, k)]


class DenseRetriever:
    def __init__(self, embedder: Embedder, matrix: np.ndarray):
        self.name = f"dense_{embedder.spec.key}"
        self.embedder, self.matrix = embedder, matrix

    def search_scored(self, query: str, k: int) -> list[tuple[int, float]]:
        q = self.embedder.encode_queries([query])[0]
        return _top_k_scored(self.matrix @ q, k)

    def search(self, query: str, k: int) -> list[int]:
        return [i for i, _ in self.search_scored(query, k)]


def rrf_fuse_scored(rankings: Sequence[Sequence[int]], k: int, rrf_k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: điểm = Σ 1 / (rrf_k + hạng)."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (rrf_k + rank)
    return [(i, scores[i]) for i in sorted(scores, key=lambda i: (-scores[i], i))[:k]]


def rrf_fuse(rankings: Sequence[Sequence[int]], k: int, rrf_k: int = RRF_K) -> list[int]:
    return [i for i, _ in rrf_fuse_scored(rankings, k, rrf_k)]


class HybridRetriever:
    """Dense + BM25 gộp bằng RRF; mỗi nhánh lấy `depth` ứng viên."""

    def __init__(self, dense: DenseRetriever, lexical: BM25Retriever, depth: int = 50):
        self.name = f"hybrid_{dense.embedder.spec.key}+{lexical.name}"
        self.dense, self.lexical, self.depth = dense, lexical, depth

    def search_scored(self, query: str, k: int) -> list[tuple[int, float]]:
        return rrf_fuse_scored([self.dense.search(query, self.depth), self.lexical.search(query, self.depth)], k)

    def search(self, query: str, k: int) -> list[int]:
        return [i for i, _ in self.search_scored(query, k)]


class RandomRetriever:
    """Mốc dưới: thứ tự ngẫu nhiên cố định theo câu hỏi."""

    def __init__(self, n: int, seed: int = 0):
        self.name, self.n, self.seed = "random", n, seed

    def search_scored(self, query: str, k: int) -> list[tuple[int, float]]:
        rng = random.Random(f"{self.seed}:{query}")
        return [(i, 0.0) for i in rng.sample(range(self.n), min(k, self.n))]

    def search(self, query: str, k: int) -> list[int]:
        return [i for i, _ in self.search_scored(query, k)]
