"""Sinh câu trả lời từ chunk truy hồi được, đo TTFT / tổng thời gian / tokens mỗi giây.

Gọi LLM qua API tương thích OpenAI (mặc định Ollama local). Model đã chốt cho benchmark
(17/10/2026): `qwen3.5:4b`, `qwen3.5:9b`. Gọi Ollama `/v1` với `reasoning_effort="none"`
để tắt thinking, nếu không TTFT đo cả thời gian "suy nghĩ" ẩn.

Định nghĩa thời gian (giây, đo bằng `time.perf_counter`):
- ttft: từ lúc gửi request tới khi nhận ký tự nội dung đầu tiên.
- total: từ lúc gửi request tới khi stream kết thúc.
- tokens_per_s: số token đầu ra / (total − ttft), tức tốc độ sinh sau token đầu.
TTFT đầu-cuối của hệ RAG = thời gian truy hồi + ttft (tính trong benchmark).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Sequence

from rag.corpus import Chunk

DEFAULT_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
MAX_TOKENS = 384

# Câu từ chối quy định trong SYSTEM_PROMPT; dùng để nhận biết câu trả lời từ chối (so khớp casefold).
REFUSAL_MARKER = "không đề cập"

SYSTEM_PROMPT = (
    "Bạn là trợ lý du lịch Huế. Chỉ trả lời dựa trên các đoạn tư liệu được cung cấp. "
    "Ghi số đoạn đã dùng trong ngoặc vuông, ví dụ [1]. "
    "Nếu tư liệu không có thông tin, trả lời: \"Tư liệu hiện có không đề cập.\" "
    "Trả lời ngắn gọn bằng tiếng Việt."
)


@dataclass
class GenerationResult:
    answer: str
    ttft_s: float
    total_s: float
    output_tokens: int
    tokens_exact: bool  # False: không có `usage`, số token ước lượng bằng số mẩu stream

    @property
    def tokens_per_s(self) -> float:
        gen_time = self.total_s - self.ttft_s
        return self.output_tokens / gen_time if gen_time > 0 else float("nan")


def build_messages(question: str, chunks: Sequence[Chunk]) -> list[dict]:
    context = "\n\n".join(f"[{i}] {c.title} — {c.section or 'Mở đầu'}\n{c.text}"
                          for i, c in enumerate(chunks, start=1))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Tư liệu:\n{context}\n\nCâu hỏi: {question}"},
    ]


def make_client(base_url: str = DEFAULT_BASE_URL):
    from openai import OpenAI
    return OpenAI(base_url=base_url, api_key=os.environ.get("OPENAI_API_KEY", "ollama"))


def stream_answer(client, model: str, messages: list[dict], max_tokens: int = MAX_TOKENS) -> GenerationResult:
    start = time.perf_counter()
    stream = client.chat.completions.create(
        model=model, messages=messages, stream=True, temperature=0, max_tokens=max_tokens,
        reasoning_effort="none", stream_options={"include_usage": True},
    )
    parts: list[str] = []
    ttft = None
    pieces = 0
    usage_tokens = None
    for event in stream:
        if getattr(event, "usage", None) is not None:
            usage_tokens = event.usage.completion_tokens
        for choice in event.choices or []:
            content = getattr(choice.delta, "content", None)
            if content:
                if ttft is None:
                    ttft = time.perf_counter() - start
                parts.append(content)
                pieces += 1
    total = time.perf_counter() - start
    return GenerationResult(
        answer="".join(parts),
        ttft_s=ttft if ttft is not None else total,
        total_s=total,
        output_tokens=usage_tokens if usage_tokens is not None else pieces,
        tokens_exact=usage_tokens is not None,
    )


def check_models(client, models: Sequence[str]) -> None:
    """Báo lỗi sớm nếu server không có model. `/v1/models` liệt kê được chưa chắc nạp được
    (file trọng số hỏng), nên warm_up vẫn là bước kiểm tra cuối."""
    available = {m.id for m in client.models.list()}
    missing = [m for m in models if m not in available]
    if missing:
        raise RuntimeError(f"Server LLM không có model {missing}; chạy `ollama pull <model>`. Có sẵn: {sorted(available)}")


def warm_up(client, model: str) -> float:
    """Một request ngắn để Ollama nạp model vào VRAM; trả về thời gian (≈ cold start)."""
    start = time.perf_counter()
    stream_answer(client, model, [{"role": "user", "content": "Chào"}], max_tokens=8)
    return time.perf_counter() - start
