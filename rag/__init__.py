"""RAG tri thức địa danh Huế: kho chunk Wikipedia, truy hồi BM25 / dense / hybrid, sinh câu trả lời.

Luồng: `python -m rag.corpus` (dựng `data/rag/chunks.jsonl`) → `python -m rag.benchmark ...`
→ xem số liệu trong `notebooks/eval_rag.ipynb`.
"""

from core.config import DATA_DIR, ROOT  # noqa: F401  (ROOT dùng lại ở các module con)

RAG_DIR = DATA_DIR / "rag"

# Cấu hình hệ thống, chốt 18/10/2026 theo benchmark data/qa/rag/2026-10-18:
# hybrid e5-large nhỉnh hơn về MRR nhưng không khác biệt có ý nghĩa (CI chứa 0) và chậm hơn ~35 ms;
# qwen3.5:4b TTFT p50 3.3 s. Đổi chỉ khi benchmark mới cho thấy khác biệt có ý nghĩa và được chốt lại.
DEFAULT_EMBEDDER = "bge-m3"
DEFAULT_RETRIEVER_MODE = "hybrid"
DEFAULT_LLM = "qwen3.5:4b"
