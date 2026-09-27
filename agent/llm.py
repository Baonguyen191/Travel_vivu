"""Client LLM (OpenAI) dùng cho agent và nhận diện ảnh.

Key đọc theo thứ tự: biến môi trường, rồi file `.env` ở gốc
repo (chỉ để tiện chạy demo; pipeline dữ liệu vẫn không tự nạp .env). Không
bao giờ in key ra log.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "gpt-5.4-mini"
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


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMClient:
    """Bọc Chat Completions. Dòng GPT-5 chỉ nhận function tools trên Chat
    Completions khi `reasoning_effort="none"`, nên đặt mặc định như vậy."""

    api_key: str
    model: str = field(default_factory=lambda: os.environ.get("OPENAI_MODEL", DEFAULT_MODEL))
    usage: Usage = field(default_factory=Usage)
    _client: object = None

    def __post_init__(self):
        from openai import OpenAI

        self._client = OpenAI(api_key=self.api_key, timeout=60.0)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False):
        kwargs: dict = {"model": self.model, "messages": messages}
        if self.model.startswith("gpt-5"):
            kwargs["reasoning_effort"] = "none"
        if tools:
            kwargs["tools"] = tools
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(**kwargs)
        self.usage.calls += 1
        if response.usage:
            self.usage.input_tokens += response.usage.prompt_tokens
            self.usage.output_tokens += response.usage.completion_tokens
        return response.choices[0].message
