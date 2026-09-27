"""Sinh embedding cho `place_chunks` phục vụ RAG.

Chuỗi đưa vào model là "<tên địa danh>: <nội dung chunk>" (xem
`chunk_embedding_text`): đoạn Wikipedia thường viết "Lăng được xây năm..."
mà không nhắc lại tên, nên thiếu tên thì query theo tên địa danh tìm kém.

Model thật là BAAI/bge-m3 (đa ngôn ngữ, 1024 chiều, không cần tiền tố
instruction cho query). `DeterministicEmbedder` chỉ dùng trong test: vector
của nó là giá trị băm, không mang ngữ nghĩa, nên không bao giờ được dùng làm
phương án dự phòng khi thiếu model thật.
"""

from abc import ABC, abstractmethod
import hashlib
import logging
import math
import os
from typing import Sequence

import psycopg

logger = logging.getLogger(__name__)

# Khớp cột `place_chunks.embedding VECTOR(1024)` trong db/migrations/002_core.sql.
EMBEDDING_DIM = 1024
DEFAULT_MODEL_NAME = "BAAI/bge-m3"
DETERMINISTIC_MODEL_NAME = "deterministic"
# Chunk dài nhất ~1200 ký tự (pipeline/ingest/wikipedia.py), tức vài trăm
# token; mặc định 8192 của bge-m3 chỉ làm chậm trên CPU.
MAX_SEQ_LENGTH = 512


class BaseEmbedder(ABC):
    """Sinh vector embedding đã chuẩn hoá L2 cho một dãy văn bản."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Tên model, ghi vào `place_chunks.embedding_model`."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Số chiều của vector sinh ra."""

    @abstractmethod
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Trả về một vector cho mỗi văn bản, cùng thứ tự."""


class SentenceTransformerEmbedder(BaseEmbedder):
    def __init__(self, model_name: str = DEFAULT_MODEL_NAME):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as err:
            raise RuntimeError(
                "Thiếu sentence-transformers. Cài bằng `pip install -e .[embed]`."
            ) from err

        self._model_name = model_name
        # Đọc cache Hugging Face trước: không mạng vẫn chạy, và tránh ~20
        # request kiểm tra phiên bản mỗi lần load. Chỉ tải khi chưa có trong
        # cache (bge-m3 ~2.3 GB, cache ở ~/.cache/huggingface, đổi bằng HF_HOME).
        try:
            self.model = SentenceTransformer(model_name, local_files_only=True)
        except OSError:
            logger.info("Chưa có '%s' trong cache, đang tải từ Hugging Face...", model_name)
            self.model = SentenceTransformer(model_name)
        self.model.max_seq_length = MAX_SEQ_LENGTH
        # sentence-transformers >= 5 đổi tên get_sentence_embedding_dimension.
        get_dim = getattr(self.model, "get_embedding_dimension", None) or (
            self.model.get_sentence_embedding_dimension
        )
        self._dimension = get_dim()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings = self.model.encode(
            list(texts),
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return embeddings.tolist()


class DeterministicEmbedder(BaseEmbedder):
    """Vector giả lập từ SHA-256 của văn bản, chỉ dùng trong test.

    Hai văn bản khác nhau dù một ký tự cho ra hai vector gần như vuông góc,
    nên độ tương đồng chỉ cao khi query trùng hệt văn bản đã embed.
    """

    def __init__(self, dim: int = EMBEDDING_DIM):
        self._dim = dim

    @property
    def model_name(self) -> str:
        return DETERMINISTIC_MODEL_NAME

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            vec = []
            for i in range(self._dim):
                h = hashlib.sha256(f"{text}:{i}".encode("utf-8")).hexdigest()
                vec.append((int(h[:8], 16) / 0xFFFFFFFF) * 2.0 - 1.0)
            norm = math.sqrt(sum(v * v for v in vec))
            results.append([v / norm for v in vec] if norm > 0 else vec)
        return results


def get_embedder(model_name: str | None = None) -> BaseEmbedder:
    """Embedder dùng chung cho `embed` và retriever.

    Cả hai phải đọc cùng một nguồn cấu hình (`EMBEDDING_MODEL`), nếu không
    chunk và query bị embed bằng hai model khác nhau.
    """
    name = model_name or os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL_NAME)
    if name == DETERMINISTIC_MODEL_NAME:
        return DeterministicEmbedder()
    return SentenceTransformerEmbedder(name)


def check_dimension(embedder: BaseEmbedder) -> None:
    if embedder.dimension != EMBEDDING_DIM:
        raise ValueError(
            f"Model '{embedder.model_name}' sinh vector {embedder.dimension} chiều,"
            f" nhưng place_chunks.embedding là VECTOR({EMBEDDING_DIM})."
        )


def chunk_embedding_text(place_name: str, content: str) -> str:
    return f"{place_name}: {content}"


def to_vector_literal(vec: Sequence[float]) -> str:
    """Chuỗi dạng '[0.1,0.2,...]' để truyền vào `%s::vector`."""
    return "[" + ",".join(f"{v:.8g}" for v in vec) + "]"


def run_embed_chunks(
    conn: psycopg.Connection,
    embedder: BaseEmbedder | None = None,
    batch_size: int = 32,
    force: bool = False,
) -> int:
    """Sinh embedding cho các chunk chưa có vector của model hiện tại.

    Chunk cần sinh: chưa có embedding, hoặc embedding do model khác sinh.
    `force=True` sinh lại toàn bộ. Mỗi batch ghi trong một transaction riêng,
    nên dừng giữa chừng thì lượt sau chạy tiếp phần còn lại.

    Trả về số chunk đã ghi embedding.
    """
    embedder = embedder or get_embedder()
    check_dimension(embedder)

    sql = (
        "SELECT c.id, p.name, c.content FROM place_chunks c"
        " JOIN places p ON p.id = c.place_id"
    )
    params: tuple = ()
    if not force:
        sql += " WHERE c.embedding IS NULL OR c.embedding_model IS DISTINCT FROM %s"
        params = (embedder.model_name,)
    # Xếp theo độ dài để mỗi batch gồm các chunk dài gần bằng nhau: model pad
    # cả batch theo chunk dài nhất, trộn chunk 100 và 2000 ký tự thì phí tính toán.
    sql += " ORDER BY length(c.content), c.id"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    if not rows:
        logger.info("Không có chunk nào cần sinh embedding.")
        return 0

    logger.info("Sinh embedding cho %d chunk bằng '%s'.", len(rows), embedder.model_name)
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        vectors = embedder.embed_texts(
            [chunk_embedding_text(name, content) for _, name, content in batch]
        )
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"Embedder trả {len(vectors)} vector cho {len(batch)} văn bản."
            )
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "UPDATE place_chunks SET embedding = %s::vector, embedding_model = %s"
                " WHERE id = %s",
                [
                    (to_vector_literal(vec), embedder.model_name, chunk_id)
                    for (chunk_id, _, _), vec in zip(batch, vectors)
                ],
            )
        total += len(batch)
        logger.info("Đã embed %d / %d chunk", total, len(rows))

    return total
