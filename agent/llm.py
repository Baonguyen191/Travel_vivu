"""Client LLM dùng cho agent, nhận diện ảnh và baseline lập lịch.

Nói chuyện qua API tương thích OpenAI Chat Completions, nên cùng một code chạy
được với OpenAI, Ollama local (`OPENAI_BASE_URL=http://localhost:11434/v1`) và
Gemini (endpoint tương thích OpenAI). Cấu hình đọc theo thứ tự: biến môi
trường, rồi file `.env` ở gốc repo (chỉ để tiện chạy demo; pipeline dữ liệu vẫn
không tự nạp .env). Không bao giờ in key ra log.
"""

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "gpt-5.4-mini"
# Ollama không kiểm tra key nhưng SDK OpenAI bắt buộc có một giá trị.
PLACEHOLDER_KEY = "not-needed"
_USAGE_LOCK = threading.Lock()
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_env_value(name: str) -> str | None:
    """Biến môi trường `name`, không có thì đọc từ file .env ở gốc repo."""
    value = os.environ.get(name)
    if value:
        return value
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    return None


def load_openai_key() -> str | None:
    return load_env_value("OPENAI_API_KEY")


def is_local(base_url: str | None) -> bool:
    return bool(base_url) and any(h in base_url for h in ("localhost", "127.0.0.1", "0.0.0.0"))


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMClient:
    """Bọc Chat Completions.

    `reasoning_effort`: None thì tự chọn. Dòng GPT-5 của OpenAI chỉ nhận function
    tools trên Chat Completions khi `"none"`; model có chế độ suy nghĩ chạy local
    (qwen3.5) cũng đặt `"none"` để trích JSON và gọi tool nhanh. Các nhà cung cấp
    khác thì không gửi tham số này trừ khi được đặt rõ."""

    api_key: str
    model: str = field(default_factory=lambda: load_env_value("OPENAI_MODEL") or DEFAULT_MODEL)
    base_url: str | None = field(default_factory=lambda: load_env_value("OPENAI_BASE_URL"))
    reasoning_effort: str | None = None
    timeout: float = 120.0
    usage: Usage = field(default_factory=Usage)
    _client: object = None

    def __post_init__(self):
        from openai import OpenAI

        if self.reasoning_effort is None and (self.model.startswith("gpt-5") or is_local(self.base_url)):
            self.reasoning_effort = "none"
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)

    @property
    def provider(self) -> str:
        if not self.base_url:
            return "openai"
        return "local" if is_local(self.base_url) else self.base_url.split("//", 1)[-1].split("/", 1)[0]

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False):
        kwargs: dict = {"model": self.model, "messages": messages}
        if self.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if tools:
            kwargs["tools"] = tools
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(**kwargs)
        with _USAGE_LOCK:  # client có thể được gọi từ nhiều luồng (planner.experiment llm-collect)
            self.usage.calls += 1
            if response.usage:
                self.usage.input_tokens += response.usage.prompt_tokens
                self.usage.output_tokens += response.usage.completion_tokens
        return response.choices[0].message


def llm_from_env(**overrides) -> LLMClient | None:
    """Client theo cấu hình môi trường/.env; None khi không có key và không trỏ tới
    server local (khi đó agent chạy chế độ luật, agent/nlu.py). Tham số None bị bỏ qua."""
    overrides = {k: v for k, v in overrides.items() if v is not None}
    base_url = overrides.pop("base_url", None) or load_env_value("OPENAI_BASE_URL")
    key = overrides.pop("api_key", None) or load_openai_key() or (PLACEHOLDER_KEY if is_local(base_url) else None)
    if not key:
        return None
    return LLMClient(key, base_url=base_url, **overrides)
