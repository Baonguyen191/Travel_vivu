"""Embedding dense bằng sentence-transformers, cache vector ra `data/rag/emb/`.

Các model đã được chốt cho benchmark (17/10/2026): bge-m3, multilingual-e5-base,
multilingual-e5-large. Thêm model mới vào `EMBEDDERS` chỉ sau khi người làm đồ án chốt.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from rag import RAG_DIR

logger = logging.getLogger(__name__)

EMB_DIR = RAG_DIR / "emb"
# Chunk dài nhất ~1.4k ký tự, tức vài trăm token; để mặc định 8192 của bge-m3 chỉ làm chậm.
MAX_SEQ_LENGTH = 512


@dataclass(frozen=True)
class EmbedderSpec:
    key: str
    hf_name: str
    query_prefix: str = ""
    passage_prefix: str = ""


EMBEDDERS = {
    "bge-m3": EmbedderSpec("bge-m3", "BAAI/bge-m3"),
    # Họ E5 được train với tiền tố "query: " / "passage: "; thiếu tiền tố thì chất lượng giảm.
    "e5-base": EmbedderSpec("e5-base", "intfloat/multilingual-e5-base", "query: ", "passage: "),
    "e5-large": EmbedderSpec("e5-large", "intfloat/multilingual-e5-large", "query: ", "passage: "),
}


@dataclass
class BuildStats:
    model: str
    n_chunks: int
    dim: int
    seconds: float
    device: str
    peak_vram_mb: float | None
    from_cache: bool

    @property
    def chunks_per_second(self) -> float:
        return self.n_chunks / self.seconds if self.seconds else float("inf")


def _device() -> str:
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


class Embedder:
    """Bọc SentenceTransformer: chuẩn hoá L2, thêm tiền tố theo model."""

    def __init__(self, spec: EmbedderSpec, device: str | None = None):
        from sentence_transformers import SentenceTransformer

        self.spec = spec
        self.device = device or _device()
        try:
            self.model = SentenceTransformer(spec.hf_name, device=self.device, local_files_only=True)
        except OSError:
            logger.info("Chưa có %s trong cache Hugging Face, đang tải...", spec.hf_name)
            self.model = SentenceTransformer(spec.hf_name, device=self.device)
        if self.device == "cuda":
            self.model.half()
        self.model.max_seq_length = MAX_SEQ_LENGTH

    def to(self, device: str) -> "Embedder":
        """Chuyển model sang thiết bị khác, vd đưa về CPU để nhường VRAM cho LLM."""
        if device == "cpu":
            self.model.float()
        self.model.to(device)
        self.device = device
        return self

    def encode_passages(self, texts: Sequence[str], batch_size: int = 32) -> np.ndarray:
        return self._encode([self.spec.passage_prefix + t for t in texts], batch_size)

    def encode_queries(self, texts: Sequence[str], batch_size: int = 32) -> np.ndarray:
        return self._encode([self.spec.query_prefix + t for t in texts], batch_size)

    def _encode(self, texts: list[str], batch_size: int) -> np.ndarray:
        vectors = self.model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                    convert_to_numpy=True, show_progress_bar=False)
        return vectors.astype(np.float32)


def build_index(spec: EmbedderSpec, texts: Sequence[str], corpus_key: str,
                emb_dir: Path = EMB_DIR, embedder: Embedder | None = None) -> tuple[Embedder, np.ndarray, BuildStats]:
    """Nạp embedder và ma trận vector của kho; đọc cache nếu cùng model + cùng kho.

    `peak_vram_mb` = VRAM đỉnh của riêng model này (trọng số + lúc encode), đã trừ phần
    các model nạp trước đó còn giữ.
    """
    cuda = (embedder.device if embedder else _device()) == "cuda"
    base = 0
    if cuda:
        import torch
        base = torch.cuda.memory_allocated()
        torch.cuda.reset_peak_memory_stats()
    embedder = embedder or Embedder(spec)
    cache = emb_dir / f"{spec.key}-{corpus_key}.npy"
    if cache.exists():
        matrix = np.load(cache)
        return embedder, matrix, BuildStats(spec.key, len(texts), matrix.shape[1], 0.0,
                                            embedder.device, None, True)
    start = time.perf_counter()
    matrix = embedder.encode_passages(texts)
    seconds = time.perf_counter() - start
    peak = None
    if cuda:
        import torch
        peak = (torch.cuda.max_memory_allocated() - base) / 2**20
    emb_dir.mkdir(parents=True, exist_ok=True)
    np.save(cache, matrix)
    return embedder, matrix, BuildStats(spec.key, len(texts), matrix.shape[1], seconds,
                                        embedder.device, peak, False)
