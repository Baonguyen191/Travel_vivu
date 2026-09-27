"""Truy hồi lai: ghép dense (bge-m3) và BM25 bằng Reciprocal Rank Fusion.

Hai nhánh bù cho nhau (xem data/qa/rag_benchmark_2026-09-26.md): dense hiểu
câu mô tả không nêu tên nhưng trượt hết câu gõ không dấu; BM25 (analyzer
"multi", xem rag/lexical.py) khớp câu không dấu và tên riêng, nhưng không
hiểu diễn đạt khác từ.

RRF chỉ dùng thứ hạng, không dùng điểm: cosine của dense và điểm BM25 khác
thang đo, cộng thẳng thì nhánh có thang lớn hơn lấn át. Điểm của một chunk là
tổng 1 / (rrf_k + hạng) trên các nhánh có trả chunk đó.
"""

from dataclasses import replace
from typing import Sequence

import psycopg

from pipeline.embed import BaseEmbedder
from rag.lexical import BM25Retriever, load_corpus
from rag.retriever import NON_DESTINATION_CATEGORIES, RAGChunk, RAGRetriever

# Hằng số chuẩn trong bài báo gốc của RRF (Cormack và cộng sự, 2009).
DEFAULT_RRF_K = 60
# Số ứng viên lấy từ mỗi nhánh trước khi ghép.
DEFAULT_DEPTH = 50


def reciprocal_rank_fusion(rankings: Sequence[Sequence[RAGChunk]],
                           rrf_k: int = DEFAULT_RRF_K) -> list[RAGChunk]:
    """Ghép các danh sách đã xếp hạng. `similarity` của kết quả là điểm RRF."""
    scores: dict[int, float] = {}
    chunks: dict[int, RAGChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, 1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
            chunks.setdefault(chunk.chunk_id, chunk)
    ordered = sorted(scores, key=lambda cid: (-scores[cid], cid))
    return [replace(chunks[cid], similarity=scores[cid]) for cid in ordered]


class HybridRetriever:
    def __init__(self, dense: RAGRetriever, lexical: BM25Retriever,
                 rrf_k: int = DEFAULT_RRF_K, depth: int = DEFAULT_DEPTH):
        self.dense = dense
        self.lexical = lexical
        self.rrf_k = rrf_k
        self.depth = depth
        self.name = f"hybrid:{dense.embedder.model_name}+{lexical.name}"

    @classmethod
    def from_connection(cls, conn: psycopg.Connection, embedder: BaseEmbedder | None = None,
                        exclude_categories: Sequence[str] = NON_DESTINATION_CATEGORIES,
                        ) -> "HybridRetriever":
        """Nạp kho chunk cho BM25 một lần; gọi lại khi `place_chunks` đổi.

        Hai nhánh luôn dùng cùng `exclude_categories`, nếu không nhánh này
        đưa lại những chunk mà nhánh kia đã loại.
        """
        return cls(
            RAGRetriever(conn, embedder, exclude_categories),
            BM25Retriever(load_corpus(conn, exclude_categories), analyzer="multi"),
        )

    def search(self, query: str, top_k: int = 5, category: str | None = None,
               place_id: int | None = None) -> list[RAGChunk]:
        if not query.strip():
            return []
        depth = max(self.depth, top_k)
        rankings = [
            self.dense.search(query, top_k=depth, category=category, place_id=place_id),
            self.lexical.search(query, top_k=depth, category=category, place_id=place_id),
        ]
        return reciprocal_rank_fusion(rankings, self.rrf_k)[:top_k]

    def build_rag_context(self, chunks: Sequence[RAGChunk]) -> str:
        return self.dense.build_rag_context(chunks)
