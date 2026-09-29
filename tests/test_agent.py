"""Tests cho agent LLM, tool và nhận diện ảnh. LLM được giả lập, không gọi API."""

import io
import json
from dataclasses import dataclass, field
from datetime import date

import pytest
from PIL import Image
from psycopg.types.json import Jsonb

from agent.agent import SYSTEM_PROMPT, run_agent
from agent.llm import llm_from_env, load_openai_key
from agent.tools import TOOLS, AgentContext, execute_tool, resolve_places
from rag.lexical import BM25Retriever, load_corpus
from recognition.landmark import read_gps, recognize


# -- LLM giả lập ------------------------------------------------------------------


@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Fn
    type: str = "function"


@dataclass
class _Message:
    content: str | None = None
    tool_calls: list = None


@dataclass
class ScriptedLLM:
    """Trả lần lượt các message đã soạn; ghi lại messages nhận được."""

    script: list
    model: str = "fake"
    seen: list = field(default_factory=list)

    def chat(self, messages, tools=None, json_mode=False):
        self.seen.append({"messages": [dict(m) for m in messages], "tools": tools, "json_mode": json_mode})
        return self.script.pop(0)


def call(name: str, **args) -> _Message:
    return _Message(tool_calls=[_ToolCall(id=f"c-{name}", function=_Fn(name, json.dumps(args)))])


# -- khung agent -----------------------------------------------------------------


def test_agent_runs_tools_then_answers(monkeypatch):
    executed = []

    def fake_execute(ctx, name, arguments):
        executed.append((name, json.loads(arguments)))
        return {"results": [{"ref": 1, "content": "Chùa khởi lập năm 1601.", "source_url": "u"}]}

    monkeypatch.setattr("agent.agent.execute_tool", fake_execute)
    llm = ScriptedLLM([call("search_knowledge", query="Thiên Mụ xây năm nào", place_name="Chùa Thiên Mụ"),
                       _Message(content="Năm 1601 [1](u).")])
    ctx = AgentContext(conn=None, retriever=None, hotel=(16.46, 107.59), today=date(2026, 9, 27))

    reply = run_agent("Chùa Thiên Mụ xây năm nào?", [{"role": "user", "text": "chào"}], ctx, llm)

    assert reply.text == "Năm 1601 [1](u)."
    assert [c.name for c in reply.tool_calls] == ["search_knowledge"]
    assert executed == [("search_knowledge", {"query": "Thiên Mụ xây năm nào", "place_name": "Chùa Thiên Mụ"})]
    first = llm.seen[0]["messages"]
    assert "2026-09-27" in first[0]["content"] and first[1] == {"role": "user", "content": "chào"}
    second = llm.seen[1]["messages"]
    assert second[-1]["role"] == "tool" and "1601" in second[-1]["content"]


def test_agent_reports_tool_errors_to_llm(monkeypatch):
    def boom(ctx, name, arguments):
        raise RuntimeError("DB down")

    monkeypatch.setattr("agent.agent.execute_tool", boom)
    llm = ScriptedLLM([call("get_weather", date="2026-09-28"), _Message(content="Không lấy được thời tiết.")])
    reply = run_agent("mai mưa không", [], AgentContext(None, None, (0, 0), date(2026, 9, 27)), llm)
    assert "RuntimeError: DB down" in llm.seen[1]["messages"][-1]["content"]
    assert reply.text == "Không lấy được thời tiết."


def test_agent_answers_right_after_plan_trip(monkeypatch):
    monkeypatch.setattr("agent.agent.execute_tool", lambda ctx, name, arguments: {"days": []})
    llm = ScriptedLLM([call("plan_trip", start_date="2026-09-28", days=1, place_names=[], transport_mode=None,
                            max_places_per_day=None),
                       _Message(content="Lịch của bạn...")])
    reply = run_agent("mai đi đâu", [], AgentContext(None, None, (0, 0), date(2026, 9, 27)), llm)
    assert reply.text == "Lịch của bạn..."
    assert llm.seen[0]["tools"] and llm.seen[1]["tools"] is None


def test_agent_stops_after_max_steps(monkeypatch):
    monkeypatch.setattr("agent.agent.execute_tool", lambda ctx, name, arguments: {})
    llm = ScriptedLLM([call("get_weather", date="2026-09-28") for _ in range(10)])
    reply = run_agent("?", [], AgentContext(None, None, (0, 0), date(2026, 9, 27)), llm)
    assert "chưa hoàn tất" in reply.text and len(reply.tool_calls) == 5


def test_tool_schemas_are_strict_and_complete():
    for tool in TOOLS:
        fn = tool["function"]
        params = fn["parameters"]
        assert fn["strict"] is True and params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"])
    assert "KHÔNG đoán" in SYSTEM_PROMPT and "KHÔNG tự sắp lịch" in SYSTEM_PROMPT


def test_load_openai_key_prefers_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert load_openai_key() == "sk-env"
    monkeypatch.delenv("OPENAI_API_KEY")
    env = tmp_path / ".env"
    env.write_text('DATABASE_URL=x\nOPENAI_API_KEY="sk-file"\n', encoding="utf-8")
    monkeypatch.setattr("agent.llm.ENV_FILE", env)
    assert load_openai_key() == "sk-file"
    env.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    assert load_openai_key() is None


def test_llm_from_env_supports_local_and_openai(monkeypatch, tmp_path):
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    monkeypatch.setattr("agent.llm.ENV_FILE", env)
    env.write_text("", encoding="utf-8")
    assert llm_from_env() is None  # không key, không server local: chế độ luật

    # Ollama: không cần key, model và base_url đọc từ .env, tắt thinking.
    env.write_text("OPENAI_BASE_URL=http://localhost:11434/v1\nOPENAI_MODEL=qwen3.5:9b\n", encoding="utf-8")
    local = llm_from_env()
    assert local.model == "qwen3.5:9b" and local.provider == "local" and local.reasoning_effort == "none"

    # OpenAI: gpt-5 cần reasoning_effort none để dùng tools; tham số rõ ràng thắng mặc định.
    env.write_text("OPENAI_API_KEY=sk-file\n", encoding="utf-8")
    assert llm_from_env().provider == "openai" and llm_from_env().reasoning_effort == "none"
    assert llm_from_env(reasoning_effort="medium", model=None).reasoning_effort == "medium"

    # Nhà cung cấp khác (Gemini): không tự gửi reasoning_effort.
    gemini = llm_from_env(base_url="https://generativelanguage.googleapis.com/v1beta/openai/", model="gemini-x")
    assert gemini.reasoning_effort is None and gemini.provider == "generativelanguage.googleapis.com"


# -- nhận diện ảnh -----------------------------------------------------------------


def jpeg(gps: tuple[float, float] | None = None) -> bytes:
    img = Image.new("RGB", (64, 48), "white")
    exif = Image.Exif()
    if gps:
        def dms(x):
            d = int(x)
            m = int((x - d) * 60)
            return (float(d), float(m), round((x - d - m / 60) * 3600, 2))
        exif[0x8825] = {1: "N", 2: dms(gps[0]), 3: "E", 4: dms(gps[1])}
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_read_gps():
    lat, lon = read_gps(jpeg((16.4690, 107.5886)))
    assert lat == pytest.approx(16.4690, abs=1e-4) and lon == pytest.approx(107.5886, abs=1e-4)
    assert read_gps(jpeg()) is None
    assert read_gps(b"not an image") is None


# -- tích hợp DB -------------------------------------------------------------------


def _place(conn, name, qid, lat, lon, category="diem_tham_quan", content=None, **extra):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO places (name, category, location, opening_hours_raw, ticket_price)"
            " VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s) RETURNING id",
            (name, category, lon, lat, extra.get("hours_raw"), Jsonb(extra["ticket"]) if "ticket" in extra else None))
        pid = cur.fetchone()[0]
        cur.execute("INSERT INTO place_external_ids (place_id, source, external_id) VALUES (%s, 'wikidata', %s)",
                    (pid, qid))
        if content:
            cur.execute("INSERT INTO place_chunks (place_id, content, content_hash) VALUES (%s, %s, %s)",
                        (pid, content, qid))
    return pid


@pytest.mark.integration
def test_tools_against_database(db_conn):
    ht = _place(db_conn, "Hoàng thành Huế", "Q10769129", 16.4694, 107.5778, "di_tich",
                "Hoàng thành Huế là vòng thành thứ hai bên trong Kinh thành, xây dựng từ năm 1804 dưới triều Gia Long.",
                hours_raw="Mo-Su 07:30-17:30", ticket={"vnd": 200000})
    kd = _place(db_conn, "Lăng Khải Định", "Q7818621", 16.3986, 107.588, "lang_tam",
                "Lăng Khải Định còn gọi là Ứng Lăng, kết hợp kiến trúc Đông Tây và nghệ thuật khảm sành sứ độc đáo.")
    ctx = AgentContext(conn=db_conn, retriever=BM25Retriever(load_corpus(db_conn), analyzer="multi"),
                       hotel=(16.4637, 107.5909), today=date(2026, 9, 27), compare_baseline=False)

    places, missing = resolve_places(db_conn, ["Đại Nội", "lang khai dinh", "Tháp Eiffel"])
    assert [p.id for p in places] == [ht, kd] and missing == ["Tháp Eiffel"]

    facts = execute_tool(ctx, "get_place_facts", json.dumps({"place_names": ["Đại Nội", "Lăng Khải Định"]}))
    by_name = {f["place"]: f for f in facts["places"]}
    assert by_name["Hoàng thành Huế"]["opening_hours"] == "Mo-Su 07:30-17:30"
    assert by_name["Hoàng thành Huế"]["ticket_price_vnd"] == 200000
    assert by_name["Lăng Khải Định"]["opening_hours"] is None  # thiếu thì null, không đoán

    found = execute_tool(ctx, "search_knowledge", json.dumps({"query": "khảm sành sứ", "place_name": None}))
    assert found["results"][0]["place"] == "Lăng Khải Định"
    assert ctx.artifacts["chunks"]


@pytest.mark.integration
def test_recognize_with_gps_and_vision(db_conn):
    bridge = _place(db_conn, "Cầu Trường Tiền", "Q10752407", 16.4689, 107.5886,
                    content="Cầu Trường Tiền là cây cầu thép bắc qua sông Hương, gồm sáu nhịp vòm.")
    statue = _place(db_conn, "Tượng đài", "Q1", 16.4695, 107.5890)
    photo = jpeg((16.4696, 107.5891))  # người chụp đứng sát tượng đài, chụp về phía cầu

    no_llm = recognize(db_conn, photo)
    assert no_llm.tier == "gps" and no_llm.place.id == statue  # không có LLM: lấy điểm gần nhất

    llm = ScriptedLLM([_Message(content=json.dumps({"name": "Cầu Trường Tiền", "confidence": 0.95,
                                                     "reason": "Cầu thép nhiều nhịp vòm"}))])
    both = recognize(db_conn, photo, llm)
    assert both.tier == "gps+vision" and both.place.id == bridge
    sent = llm.seen[0]
    assert sent["json_mode"] and "Cầu Trường Tiền" in sent["messages"][0]["content"][0]["text"]

    unsure = ScriptedLLM([_Message(content=json.dumps({"name": "Cầu Trường Tiền", "confidence": 0.3}))] * 2)
    assert recognize(db_conn, jpeg(), unsure).place is None  # dưới ngưỡng tin cậy thì không kết luận

    outside = ScriptedLLM([_Message(content=json.dumps({"name": "Tháp Eiffel", "confidence": 0.99}))])
    assert recognize(db_conn, jpeg(), outside).place is None  # tên ngoài danh sách bị bỏ
