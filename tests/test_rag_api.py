from types import SimpleNamespace

import pytest

from core.types import Answer, Passage
from rag import api
from rag.__main__ import main as cli_main
from rag.corpus import Chunk

CHUNKS = [
    Chunk("1-0", "wikidata:Q1", "Chùa Thiên Mụ", "", "Chùa khởi lập năm 1601."),
    Chunk("1-1", "wikidata:Q1", "Chùa Thiên Mụ", "Kiến trúc", "Tháp Phước Duyên gồm 7 tầng."),
    Chunk("2-0", "wikidata:Q2", "Cầu Trường Tiền", "", "Cầu dài 402,60 m."),
]


class FakeRetriever:
    name = "fake"

    def __init__(self, order=(1, 0, 2)):
        self.order = order

    def search_scored(self, query, k):
        return [(i, 1.0 / (r + 1)) for r, i in enumerate(self.order)][:k]


class FakeClient:
    def __init__(self, text):
        self.text, self.calls = text, 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        return iter([
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=self.text))], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(completion_tokens=9)),
        ])


def _service(text="Tháp có 7 tầng [1], chùa lập năm 1601 [2][1][9].", order=(1, 0, 2)):
    return api.RAGService(CHUNKS, FakeRetriever(order), model="m", client=FakeClient(text))


def test_retrieve_returns_core_passages_with_scores():
    out = _service().retrieve("tháp mấy tầng", k=2)
    assert all(isinstance(p, Passage) for p in out)
    assert [(p.chunk_id, p.score) for p in out] == [("1-1", 1.0), ("1-0", 0.5)]


def test_retrieve_place_filter_and_validation():
    svc = _service()
    assert [p.chunk_id for p in svc.retrieve("cầu", k=5, place_key="wikidata:Q2")] == ["2-0"]
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            svc.retrieve(bad)
    for k in (0, api.MAX_K + 1):
        with pytest.raises(ValueError):
            svc.retrieve("x", k=k)


def test_answer_maps_citations_in_order_and_ignores_invalid_numbers():
    svc = _service()
    reply = svc.answer("tháp mấy tầng và chùa lập năm nào", k=3)
    assert isinstance(reply, Answer)
    assert [p.chunk_id for p in reply.citations] == ["1-1", "1-0"]
    assert [p.chunk_id for p in reply.context] == ["1-1", "1-0", "2-0"]
    assert not reply.refused and reply.output_tokens == 9
    assert set(reply.timings) == {"retrieval_ms", "ttft_ms", "total_ms"}


def test_answer_detects_refusal():
    reply = _service(text="Tư liệu hiện có không đề cập.").answer("giá vé?")
    assert reply.refused and reply.citations == ()


def test_answer_without_context_skips_llm():
    svc = _service()
    reply = svc.answer("x", place_key="wikidata:Q999")
    assert reply.refused and reply.context == () and svc._client.calls == 0


def test_answer_wraps_openai_errors():
    import openai

    class Broken:
        chat = SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: (_ for _ in ()).throw(openai.APIConnectionError(request=None))))

    svc = api.RAGService(CHUNKS, FakeRetriever(), model="m", client=Broken())
    with pytest.raises(api.LLMUnavailableError):
        svc.answer("x")


def test_module_functions_and_cli_use_injected_service(capsys):
    api.set_service(_service())
    try:
        assert api.retrieve("x", k=1)[0].chunk_id == "1-1"
        assert api.answer("x").citations[0].chunk_id == "1-1"
        assert cli_main(["search", "x", "-k", "1"]) == 0
        assert "Tháp Phước Duyên" in capsys.readouterr().out
        assert cli_main(["ask", "x", "--json"]) == 0
        assert '"refused": false' in capsys.readouterr().out
        assert cli_main(["search", ""]) == 2
    finally:
        api.set_service(None)
