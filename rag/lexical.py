"""Truy hồi theo từ khoá (BM25) trên kho chunk nạp sẵn vào bộ nhớ.

Kho của Huế chỉ vài nghìn chunk, nên chấm điểm toàn bộ trong Python mất vài
mili giây, không cần full-text search của Postgres (không có cấu hình tiếng
Việt) hay thư viện ngoài.
"""

import math
import re
import unicodedata
from collections import Counter
from dataclasses import replace
from typing import Sequence

import psycopg

from pipeline.embed import chunk_embedding_text
from rag.retriever import NON_DESTINATION_CATEGORIES, RAGChunk

_TOKEN = re.compile(r"\w+")
ANALYZERS = ("plain", "fold", "multi")


def fold_diacritics(text: str) -> str:
    """Bỏ dấu tiếng Việt: "Lăng Tự Đức" -> "lang tu duc".

    "đ" không tách được bằng NFD nên thay riêng.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("đ", "d")


def tokenize(text: str, fold: bool = False) -> list[str]:
    """Tách theo âm tiết. Tiếng Việt viết cách từng âm tiết nên đủ cho BM25.

    `fold=True` bỏ dấu cả hai phía, để câu gõ không dấu vẫn khớp. Đổi lại,
    các âm tiết khác dấu bị gộp ("lăng", "làng" -> "lang").
    """
    text = fold_diacritics(text) if fold else text.casefold()
    return _TOKEN.findall(text)


def analyze(text: str, analyzer: str) -> list[str]:
    """Chuỗi term cho BM25.

    - plain: âm tiết giữ dấu. Chính xác nhưng trượt câu gõ không dấu.
    - fold: âm tiết bỏ dấu. Khớp câu không dấu, nhưng gộp nhầm âm tiết khác
      dấu ("lăng nhăng" khớp "lang tu duc").
    - multi: cả ba loại term cùng lúc: âm tiết giữ dấu, âm tiết bỏ dấu (tiền tố
      "~") và cặp âm tiết liền nhau bỏ dấu (tiền tố "~", nối bằng "_"). Câu có
      dấu được cộng điểm ở term giữ dấu, nên khớp đúng dấu xếp trên khớp nhầm.
      Câu không dấu nhờ bigram: "tu duc" chỉ khớp chỗ có "Tự Đức" liền nhau,
      không khớp "lăng nhăng". Từ tiếng Việt phần lớn gồm hai âm tiết nên
      bigram gần với từ.
    """
    if analyzer not in ANALYZERS:
        raise ValueError(f"analyzer '{analyzer}' không thuộc {ANALYZERS}")
    if analyzer == "plain":
        return tokenize(text)
    folded = tokenize(text, fold=True)
    if analyzer == "fold":
        return folded
    return (
        tokenize(text)
        + [f"~{t}" for t in folded]
        + [f"~{a}_{b}" for a, b in zip(folded, folded[1:])]
    )


def load_corpus(conn: psycopg.Connection,
                exclude_categories: Sequence[str] = NON_DESTINATION_CATEGORIES) -> list[RAGChunk]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, p.id, p.name, p.category, c.content, c.source_url"
            " FROM place_chunks c JOIN places p ON p.id = c.place_id"
            " WHERE NOT (p.category = ANY(%s)) ORDER BY c.id",
            (list(exclude_categories),),
        )
        return [RAGChunk(*row, similarity=0.0) for row in cur.fetchall()]


class BM25Retriever:
    def __init__(self, corpus: Sequence[RAGChunk], analyzer: str = "plain",
                 k1: float = 1.5, b: float = 0.75):
        self.corpus = list(corpus)
        self.analyzer = analyzer
        self.name = "bm25" if analyzer == "plain" else f"bm25_{analyzer}"
        self.k1, self.b = k1, b
        docs = [analyze(chunk_embedding_text(c.place_name, c.content), analyzer)
                for c in self.corpus]
        self.tfs = [Counter(d) for d in docs]
        self.lengths = [len(d) for d in docs]
        self.avgdl = sum(self.lengths) / len(docs) if docs else 0.0
        df = Counter(term for d in docs for term in set(d))
        n = len(docs)
        self.idf = {t: math.log((n - f + 0.5) / (f + 0.5) + 1.0) for t, f in df.items()}

    def _score(self, idx: int, terms: Sequence[str]) -> float:
        tf, length = self.tfs[idx], self.lengths[idx]
        score = 0.0
        for term in terms:
            f = tf.get(term, 0)
            if f:
                norm = f + self.k1 * (1 - self.b + self.b * length / self.avgdl)
                score += self.idf[term] * f * (self.k1 + 1) / norm
        return score

    def search(self, query: str, top_k: int = 5, category: str | None = None,
               place_id: int | None = None) -> list[RAGChunk]:
        """Top-k chunk có điểm BM25 > 0, giảm dần. Không khớp từ nào thì rỗng."""
        terms = analyze(query, self.analyzer)
        scored = []
        for idx, chunk in enumerate(self.corpus):
            if category and chunk.category != category:
                continue
            if place_id is not None and chunk.place_id != place_id:
                continue
            score = self._score(idx, terms)
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda sc: (-sc[0], sc[1].chunk_id))
        return [replace(chunk, similarity=score) for score, chunk in scored[:top_k]]
