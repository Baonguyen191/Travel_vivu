"""Chỉ số truy hồi và thống kê dùng trong benchmark RAG.

Quy ước liên quan:
- Một chunk "liên quan" khi `place_key` của nó thuộc tập địa danh đúng của câu hỏi.
- Hit@k, MRR@k, nDCG@k, P@k tính ở mức chunk.
- Precision / Recall / F1 @k tính ở mức địa danh: tập địa danh (bỏ trùng) xuất hiện trong
  top-k so với tập địa danh đúng. Đây là con số đọc được cho câu hỏi nhiều địa danh (`multi`).
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def hit_at_k(relevant_flags: Sequence[bool], k: int) -> float:
    return float(any(relevant_flags[:k]))


def reciprocal_rank(relevant_flags: Sequence[bool], k: int = 10) -> float:
    for rank, rel in enumerate(relevant_flags[:k], start=1):
        if rel:
            return 1.0 / rank
    return 0.0


def precision_at_k(relevant_flags: Sequence[bool], k: int) -> float:
    return sum(relevant_flags[:k]) / k


def ndcg_at_k(relevant_flags: Sequence[bool], k: int, n_relevant_total: int) -> float:
    """nDCG nhị phân; DCG lý tưởng xếp min(k, tổng số chunk liên quan) chunk đúng lên đầu."""
    dcg = sum(1.0 / math.log2(rank + 1) for rank, rel in enumerate(relevant_flags[:k], start=1) if rel)
    ideal_n = min(k, n_relevant_total)
    if ideal_n == 0:
        return 0.0
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_n + 1))
    return dcg / idcg


def place_prf_at_k(retrieved_places: Sequence[str | None], relevant: set[str], k: int) -> tuple[float, float, float]:
    """(precision, recall, F1) ở mức địa danh trong top-k."""
    got = {p for p in retrieved_places[:k] if p is not None}
    if not got or not relevant:
        return 0.0, 0.0, 0.0
    tp = len(got & relevant)
    precision, recall = tp / len(got), tp / len(relevant)
    f1 = 2 * precision * recall / (precision + recall) if tp else 0.0
    return precision, recall, f1


def keyword_recall(text: str, keywords: Sequence[str]) -> float:
    """Tỷ lệ từ khoá đáp án có trong văn bản (không phân biệt hoa thường)."""
    if not keywords:
        return float("nan")
    low = text.casefold()
    return sum(kw.casefold() in low for kw in keywords) / len(keywords)


def percentile(values: Sequence[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=float), q)) if len(values) else float("nan")


def bootstrap_ci(values: Sequence[float], n_boot: int = 2000, alpha: float = 0.05,
                 seed: int = 0) -> tuple[float, float, float]:
    """(trung bình, cận dưới, cận trên) bằng bootstrap percentile, lấy mẫu lại theo câu hỏi."""
    arr = np.asarray([v for v in values if not math.isnan(v)], dtype=float)
    if arr.size == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = arr[rng.integers(0, arr.size, size=(n_boot, arr.size))].mean(axis=1)
    return float(arr.mean()), float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def paired_bootstrap_diff(a: Sequence[float], b: Sequence[float], n_boot: int = 2000,
                          alpha: float = 0.05, seed: int = 0) -> tuple[float, float, float]:
    """Chênh lệch trung bình a − b trên cùng câu hỏi, kèm CI 95%. CI không chứa 0 ⇒ khác biệt có ý nghĩa."""
    if len(a) != len(b):
        raise ValueError("a và b phải cùng số câu hỏi, cùng thứ tự")
    return bootstrap_ci([x - y for x, y in zip(a, b)], n_boot, alpha, seed)
