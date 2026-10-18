"""BM25 trong bộ nhớ, không thư viện ngoài (kho Huế chỉ ~1k chunk, chấm toàn bộ mất vài ms).

Ba bộ phân tích term:
- plain: âm tiết giữ dấu. Chính xác nhưng trượt câu gõ không dấu.
- fold: âm tiết bỏ dấu. Khớp câu không dấu, nhưng gộp nhầm âm tiết khác dấu ("lăng"/"làng").
- multi: âm tiết giữ dấu + âm tiết bỏ dấu (tiền tố "~") + cặp âm tiết liền nhau bỏ dấu.
  Câu có dấu được cộng điểm ở term giữ dấu; câu không dấu nhờ bigram ("tu duc" chỉ khớp
  chỗ "Tự Đức" liền nhau). Từ tiếng Việt phần lớn gồm hai âm tiết nên bigram gần với từ.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from typing import Sequence

_TOKEN = re.compile(r"\w+")
ANALYZERS = ("plain", "fold", "multi")


def fold_diacritics(text: str) -> str:
    """"Lăng Tự Đức" → "lang tu duc". "đ" không tách được bằng NFD nên thay riêng."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").replace("đ", "d")


def tokenize(text: str, fold: bool = False) -> list[str]:
    return _TOKEN.findall(fold_diacritics(text) if fold else text.casefold())


def analyze(text: str, analyzer: str) -> list[str]:
    if analyzer == "plain":
        return tokenize(text)
    if analyzer == "fold":
        return tokenize(text, fold=True)
    if analyzer == "multi":
        folded = tokenize(text, fold=True)
        bigrams = [f"~{a}_{b}" for a, b in zip(folded, folded[1:])]
        return tokenize(text) + [f"~{t}" for t in folded] + bigrams
    raise ValueError(f"analyzer '{analyzer}' không thuộc {ANALYZERS}")


class BM25:
    def __init__(self, documents: Sequence[str], analyzer: str = "multi", k1: float = 1.5, b: float = 0.75):
        self.analyzer, self.k1, self.b = analyzer, k1, b
        self._tf = [Counter(analyze(doc, analyzer)) for doc in documents]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg_len = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(documents)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = [t for t in analyze(query, self.analyzer) if t in self._idf]
        result = []
        for tf, length in zip(self._tf, self._len):
            norm = self.k1 * (1 - self.b + self.b * length / self._avg_len) if self._avg_len else self.k1
            s = 0.0
            for t in terms:
                f = tf.get(t)
                if f:
                    s += self._idf[t] * f * (self.k1 + 1) / (f + norm)
            result.append(s)
        return result
