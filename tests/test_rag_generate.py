from types import SimpleNamespace

import pytest

from rag.benchmark import Query, run_generation, summarize_generation
from rag.corpus import Chunk
from rag.generate import build_messages, stream_answer


def _event(content=None, usage=None):
    choices = [] if content is None else [SimpleNamespace(delta=SimpleNamespace(content=content))]
    return SimpleNamespace(choices=choices, usage=usage)


class FakeClient:
    """Giả lập stream OpenAI: hai mẩu nội dung rồi một sự kiện usage."""

    def __init__(self, text="Chùa xây năm 1601 [1].", with_usage=True, models=("m1", "m2")):
        self.text, self.with_usage, self.calls = text, with_usage, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=lambda: [SimpleNamespace(id=m) for m in models])

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        half = len(self.text) // 2
        events = [_event(""), _event(self.text[:half]), _event(self.text[half:])]
        if self.with_usage:
            events.append(_event(usage=SimpleNamespace(completion_tokens=7)))
        return iter(events)


CHUNKS = [Chunk("1-0", "wikidata:Q1", "Chùa Thiên Mụ", "Lịch sử", "Chùa xây năm 1601.")]


def test_build_messages_numbers_context():
    msgs = build_messages("Chùa xây năm nào?", CHUNKS)
    assert msgs[0]["role"] == "system"
    assert "[1] Chùa Thiên Mụ — Lịch sử" in msgs[1]["content"]


def test_stream_answer_measures_and_disables_thinking():
    client = FakeClient()
    result = stream_answer(client, "qwen3.5:4b", build_messages("q", CHUNKS))
    assert result.answer == "Chùa xây năm 1601 [1]."
    assert result.output_tokens == 7 and result.tokens_exact
    assert 0 <= result.ttft_s <= result.total_s
    call = client.calls[0]
    assert call["stream"] and call["reasoning_effort"] == "none"
    assert call["stream_options"] == {"include_usage": True}


def test_stream_answer_falls_back_to_piece_count_without_usage():
    result = stream_answer(FakeClient(with_usage=False), "m", [])
    assert result.output_tokens == 2 and not result.tokens_exact


class FirstChunk:
    name = "fake"

    def search(self, query, k):
        return [0]


def test_run_generation_rows_and_summary():
    queries = [Query("q1", "Chùa Thiên Mụ xây năm nào?", "fact", {"wikidata:Q1"}, ["1601"])]
    df, cold = run_generation(FirstChunk(), CHUNKS, queries, ["m1", "m2"], client=FakeClient())
    assert list(df["model"]) == ["m1", "m2"]
    row = df.iloc[0]
    assert row["keyword_recall"] == 1.0 and row["cited"] and not row["refused"]
    assert row["e2e_ttft_ms"] == pytest.approx(row["retrieval_ms"] + row["ttft_ms"])
    summary = summarize_generation(df, cold)
    assert set(summary.columns) >= {"ttft_ms_p50", "ttft_ms_p95", "e2e_ttft_ms_p50", "tokens_per_s_mean",
                                    "keyword_recall", "citation_rate", "cold_start_s"}


def test_run_generation_fails_fast_on_missing_model():
    queries = [Query("q1", "x", "fact", {"wikidata:Q1"})]
    with pytest.raises(RuntimeError, match="ollama pull"):
        run_generation(FirstChunk(), CHUNKS, queries, ["m9"], client=FakeClient())
