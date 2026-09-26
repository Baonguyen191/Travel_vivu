"""Truy hồi chunk tri thức địa danh theo độ tương đồng vector (pgvector)."""

from dataclasses import dataclass
from typing import Sequence

import psycopg

from pipeline.embed import BaseEmbedder, check_dimension, get_embedder, to_vector_literal


# Category không phải điểm đến (config/categories.yml). Bài Wikipedia về thành
# phố Huế, triều đại, trận đánh, bệnh viện... chen vào top-k và đẩy chunk của
# điểm tham quan xuống, nên mặc định loại khỏi truy hồi. Truyền
# `exclude_categories=()` để tìm trên toàn kho (ablation trong benchmark).
NON_DESTINATION_CATEGORIES = ("boi_canh", "ha_tang", "don_vi_hanh_chinh")


@dataclass
class RAGChunk:
    chunk_id: int
    place_id: int
    place_name: str
    category: str
    content: str
    source_url: str | None
    similarity: float


# `MATERIALIZED` buộc Postgres tính khoảng cách cho mọi chunk thoả điều kiện
# rồi mới sắp xếp, tức tìm chính xác thay vì đi qua index HNSW. Với vài nghìn
# chunk của Huế, quét hết vẫn chỉ mất vài mili giây. Đổi lại: kết quả không
# phụ thuộc recall của ANN (benchmark đo đúng model, không đo index), và các
# filter category/place_id/embedding_model không làm hụt top_k như khi HNSW
# lọc sau. Khi dữ liệu lớn hơn nhiều, bỏ MATERIALIZED và bật
# `hnsw.iterative_scan` (pgvector >= 0.8).
_SEARCH_SQL = """
    WITH scored AS MATERIALIZED (
        SELECT c.id, p.id AS place_id, p.name, p.category, c.content,
               c.source_url, c.embedding <=> %(query)s::vector AS distance
        FROM place_chunks c
        JOIN places p ON p.id = c.place_id
        WHERE c.embedding IS NOT NULL AND c.embedding_model = %(model)s
              AND NOT (p.category = ANY(%(excluded)s))
              {filters}
    )
    SELECT id, place_id, name, category, content, source_url, 1 - distance
    FROM scored
    WHERE 1 - distance >= %(min_similarity)s
    ORDER BY distance, id
    LIMIT %(top_k)s
"""


class RAGRetriever:
    def __init__(self, conn: psycopg.Connection, embedder: BaseEmbedder | None = None,
                 exclude_categories: Sequence[str] = NON_DESTINATION_CATEGORIES):
        self.conn = conn
        self.embedder = embedder or get_embedder()
        self.exclude_categories = tuple(exclude_categories)
        check_dimension(self.embedder)

    @property
    def name(self) -> str:
        return f"dense:{self.embedder.model_name}"

    def index_status(self) -> tuple[int, int]:
        """(tổng số chunk, số chunk có embedding của model đang dùng)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE embedding IS NOT NULL"
                " AND embedding_model = %s) FROM place_chunks",
                (self.embedder.model_name,),
            )
            total, embedded = cur.fetchone()
        return total, embedded

    def search(
        self,
        query: str,
        top_k: int = 5,
        category: str | None = None,
        place_id: int | None = None,
        min_similarity: float = -1.0,
    ) -> list[RAGChunk]:
        """Top-k chunk theo cosine similarity, giảm dần.

        Chỉ tìm trên chunk do cùng model với query sinh embedding.
        `min_similarity` lọc trong SQL, trước LIMIT.
        """
        if not query.strip():
            return []

        params: dict[str, object] = {
            "query": to_vector_literal(self.embedder.embed_texts([query])[0]),
            "model": self.embedder.model_name,
            "min_similarity": min_similarity,
            "top_k": top_k,
            "excluded": list(self.exclude_categories),
        }
        filters = []
        if category:
            filters.append("AND p.category = %(category)s")
            params["category"] = category
        if place_id is not None:
            filters.append("AND p.id = %(place_id)s")
            params["place_id"] = place_id

        with self.conn.cursor() as cur:
            cur.execute(_SEARCH_SQL.format(filters=" ".join(filters)), params)
            rows = cur.fetchall()

        return [
            RAGChunk(
                chunk_id=chunk_id,
                place_id=p_id,
                place_name=p_name,
                category=cat,
                content=content,
                source_url=source_url,
                similarity=float(sim),
            )
            for chunk_id, p_id, p_name, cat, content, source_url, sim in rows
        ]

    def build_rag_context(self, chunks: Sequence[RAGChunk]) -> str:
        """Ghép các chunk thành đoạn ngữ cảnh đưa vào prompt LLM."""
        if not chunks:
            return "Không tìm thấy thông tin tri thức phù hợp trong CSDL."

        lines = ["--- TRI THỨC ĐỊA DANH HUẾ (RAG CONTEXT) ---"]
        for idx, chunk in enumerate(chunks, 1):
            lines.append(f"\n[{idx}] Địa danh: {chunk.place_name} ({chunk.category})")
            lines.append(f"Nội dung: {chunk.content}")
            if chunk.source_url:
                lines.append(f"Nguồn: {chunk.source_url}")
        lines.append("-------------------------------------------")
        return "\n".join(lines)
