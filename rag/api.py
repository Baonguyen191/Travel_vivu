"""Giao diện công khai của module RAG (docs/modules.md). Module khác chỉ gọi các hàm ở đây.

    from rag import api
    passages = api.retrieve("Chùa Thiên Mụ xây năm nào?")          # list[core.types.Passage]
    reply = api.answer("Chùa Thiên Mụ xây năm nào?")               # core.types.Answer

Cấu hình mặc định (chốt 18/10/2026, `rag/__init__.py`): hybrid bge-m3 + BM25, LLM qwen3.5:4b qua
Ollama. Lần gọi đầu nạp kho chunk + embedder (vài giây); các lần sau dùng lại. `set_service()` cho
phép module khác/test thay bằng service tự dựng.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Sequence

from core.types import Answer, Passage
from rag import DEFAULT_EMBEDDER, DEFAULT_LLM, DEFAULT_RETRIEVER_MODE
from rag.corpus import Chunk, corpus_hash, load_chunks
from rag.generate import REFUSAL_MARKER, build_messages, make_client, stream_answer

DEFAULT_K = 5
MAX_K = 20
# Khi lọc theo địa danh, lấy rộng hơn rồi lọc để vẫn đủ k đoạn.
FILTER_DEPTH = 200
_CITATION = re.compile(r"\[(\d+)\]")
NO_CONTEXT_TEXT = "Tư liệu hiện có không đề cập."


class LLMUnavailableError(RuntimeError):
    """Không gọi được server LLM (Ollama tắt, sai model...)."""


class RAGService:
    def __init__(self, chunks: Sequence[Chunk], retriever, model: str = DEFAULT_LLM, client=None):
        self.chunks = list(chunks)
        self._index = {c.chunk_id: i for i, c in enumerate(self.chunks)}
        self.retriever = retriever
        self.model = model
        self._client = client

    @classmethod
    def from_defaults(cls, embedder: str = DEFAULT_EMBEDDER, mode: str = DEFAULT_RETRIEVER_MODE,
                      model: str = DEFAULT_LLM) -> "RAGService":
        from rag.dense import EMBEDDERS, build_index
        from rag.retrievers import BM25Retriever, DenseRetriever, HybridRetriever

        chunks = load_chunks()
        texts = [c.embedding_text for c in chunks]
        lexical = BM25Retriever(texts, "multi")
        if mode == "bm25":
            return cls(chunks, lexical, model)
        emb, matrix, _ = build_index(EMBEDDERS[embedder], texts, corpus_hash(chunks))
        dense = DenseRetriever(emb, matrix)
        retriever = dense if mode == "dense" else HybridRetriever(dense, lexical)
        # Một truy vấn khởi động: khởi tạo CUDA/kernel ngay lúc dựng service, không dồn vào câu hỏi đầu.
        retriever.search_scored("khởi động", 1)
        return cls(chunks, retriever, model)

    def warm_up_llm(self) -> float:
        """Nạp sẵn LLM vào VRAM (Ollama nạp lười); trả về số giây. App gọi lúc khởi động."""
        from rag.generate import warm_up
        return warm_up(self.client, self.model)

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def _passage(self, idx: int, score: float) -> Passage:
        c = self.chunks[idx]
        return Passage(c.chunk_id, c.place_key, c.title, c.section, c.text, score)

    def retrieve(self, query: str, k: int = DEFAULT_K, place_key: str | None = None) -> list[Passage]:
        query = (query or "").strip()
        if not query:
            raise ValueError("câu hỏi rỗng")
        if not 1 <= k <= MAX_K:
            raise ValueError(f"k phải trong [1, {MAX_K}]")
        if place_key is None:
            return [self._passage(i, s) for i, s in self.retriever.search_scored(query, k)]
        hits = self.retriever.search_scored(query, min(FILTER_DEPTH, len(self.chunks)))
        return [self._passage(i, s) for i, s in hits if self.chunks[i].place_key == place_key][:k]

    def answer(self, query: str, k: int = DEFAULT_K, place_key: str | None = None) -> Answer:
        start = time.perf_counter()
        context = self.retrieve(query, k, place_key)
        retrieval_ms = (time.perf_counter() - start) * 1000
        if not context:
            return Answer(NO_CONTEXT_TEXT, (), (), self.model, True, {"retrieval_ms": retrieval_ms})
        messages = build_messages(query, [self.chunks[self._index[p.chunk_id]] for p in context])
        try:
            result = stream_answer(self.client, self.model, messages)
        except Exception as err:  # lỗi mạng / server của SDK OpenAI: báo rõ cho bên gọi
            if type(err).__module__.split(".")[0] == "openai":
                raise LLMUnavailableError(f"Không gọi được LLM '{self.model}': {err}") from err
            raise
        cited = []
        for m in _CITATION.finditer(result.answer):
            n = int(m.group(1))
            if 1 <= n <= len(context) and context[n - 1] not in cited:
                cited.append(context[n - 1])
        timings = {"retrieval_ms": retrieval_ms, "ttft_ms": result.ttft_s * 1000, "total_ms": result.total_s * 1000}
        return Answer(result.answer, tuple(cited), tuple(context), self.model,
                      REFUSAL_MARKER in result.answer.casefold(), timings,
                      result.output_tokens if result.tokens_exact else None)


_service: RAGService | None = None
_lock = threading.Lock()


def get_service() -> RAGService:
    global _service
    with _lock:
        if _service is None:
            _service = RAGService.from_defaults()
        return _service


def set_service(service: RAGService | None) -> None:
    """Thay service mặc định (test, hoặc app muốn cấu hình khác). None: quay về mặc định."""
    global _service
    with _lock:
        _service = service


def retrieve(query: str, k: int = DEFAULT_K, place_key: str | None = None) -> list[Passage]:
    """Top-k đoạn tư liệu cho câu hỏi; `place_key` (vd `wikidata:Q975568`) giới hạn trong một địa danh."""
    return get_service().retrieve(query, k, place_key)


def answer(query: str, k: int = DEFAULT_K, place_key: str | None = None) -> Answer:
    """Trả lời dựa trên top-k đoạn tư liệu, kèm trích dẫn và thời gian. Lỗi LLM: LLMUnavailableError."""
    return get_service().answer(query, k, place_key)
