"""Smoke test cho endpoint tương thích OpenAI (Ollama / Gemini / OpenAI).

Đo: tỷ lệ gọi đúng tool, tỷ lệ JSON hợp lệ, (tùy chọn) chọn đúng địa danh từ ảnh, độ trễ.

Cấu hình đọc như agent (agent/llm.py): biến môi trường, rồi `.env` ở gốc repo;
server local không cần key và được gửi `reasoning_effort="none"` (tắt thinking).

Dùng:
    python scripts/llm_smoke_test.py --n 20                     # model theo OPENAI_MODEL
    $env:OPENAI_MODEL="qwen3.5:4b"; python scripts/llm_smoke_test.py --n 20
    python scripts/llm_smoke_test.py --n 5 --image anh_cau_truong_tien.jpg --answer "Cầu Trường Tiền"
"""
import argparse, base64, json, statistics, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.llm import llm_from_env  # noqa: E402

_llm = llm_from_env()
if _llm is None:
    raise SystemExit("Chưa cấu hình LLM: đặt OPENAI_API_KEY, hoặc OPENAI_BASE_URL tới Ollama.")
client = _llm._client
MODEL = _llm.model
EXTRA = {"reasoning_effort": _llm.reasoning_effort} if _llm.reasoning_effort else {}

TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Lấy dự báo thời tiết theo ngày cho một địa điểm ở Huế.",
        "parameters": {"type": "object", "properties": {
            "place": {"type": "string", "description": "Tên địa điểm hoặc thành phố"},
            "date": {"type": "string", "description": "Ngày dạng YYYY-MM-DD"}},
            "required": ["place", "date"]}}},
    {"type": "function", "function": {
        "name": "search_places",
        "description": "Tìm địa điểm tham quan hoặc quán ăn ở Huế theo từ khóa.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
]

TOOL_CASES = [
    ("Ngày 2026-10-02 ở Huế có mưa không?", "get_weather"),
    ("Thời tiết Đại Nội Huế ngày 2026-10-05 thế nào?", "get_weather"),
    ("Gợi ý quán bún bò gần chợ Đông Ba", "search_places"),
    ("Tìm giúp mình các lăng tẩm vua Nguyễn", "search_places"),
]

EXTRACT_PROMPT = (
    "Trích ràng buộc chuyến đi thành JSON với đúng các khóa: days (int), budget_vnd (int), "
    "interests (list[str]), avoid_outdoor_when_rain (bool). Chỉ trả JSON, không giải thích.\n"
    "Câu: 'Mình đi Huế 2 ngày, ngân sách tầm 3 triệu, thích lăng tẩm với ẩm thực, "
    "mưa thì đừng xếp chỗ ngoài trời nhé.'"
)
EXPECTED = {"days": 2, "budget_vnd": 3000000, "avoid_outdoor_when_rain": True}


def timed(**kw):
    t = time.perf_counter()
    r = client.chat.completions.create(model=MODEL, temperature=0, **EXTRA, **kw)
    return r, time.perf_counter() - t


def parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1])


def test_tools(n):
    ok, lat = 0, []
    for i in range(n):
        q, want = TOOL_CASES[i % len(TOOL_CASES)]
        r, dt = timed(messages=[{"role": "user", "content": q}], tools=TOOLS)
        lat.append(dt)
        calls = r.choices[0].message.tool_calls or []
        if calls and calls[0].function.name == want:
            try:
                json.loads(calls[0].function.arguments); ok += 1
            except json.JSONDecodeError:
                pass
    return ok / n, statistics.mean(lat)


def test_extract(n):
    ok, lat = 0, []
    for _ in range(n):
        r, dt = timed(messages=[{"role": "user", "content": EXTRACT_PROMPT}])
        lat.append(dt)
        try:
            d = parse_json(r.choices[0].message.content)
            ok += all(d.get(k) == v for k, v in EXPECTED.items())
        except Exception:
            pass
    return ok / n, statistics.mean(lat)


def test_vision(n, image, answer, candidates):
    b64 = base64.b64encode(open(image, "rb").read()).decode()
    prompt = ("Ảnh chụp ở Huế. Chọn đúng MỘT địa danh trong danh sách sau hoặc 'không xác định'. "
              f"Danh sách: {json.dumps(candidates, ensure_ascii=False)}. "
              'Trả JSON {"place": ..., "confidence": 0..1}.')
    ok, lat = 0, []
    for _ in range(n):
        r, dt = timed(messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}])
        lat.append(dt)
        try:
            ok += parse_json(r.choices[0].message.content).get("place") == answer
        except Exception:
            pass
    return ok / n, statistics.mean(lat)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--image")
    ap.add_argument("--answer")
    ap.add_argument("--candidates", default="Cầu Trường Tiền,Tượng đài Phan Bội Châu,Chợ Đông Ba,Kỳ Đài")
    a = ap.parse_args()
    res = {"model": MODEL, "base_url": str(client.base_url), **EXTRA}
    res["tool_call_acc"], res["tool_latency_s"] = test_tools(a.n)
    res["json_extract_acc"], res["extract_latency_s"] = test_extract(a.n)
    if a.image:
        res["vision_top1"], res["vision_latency_s"] = test_vision(
            a.n, a.image, a.answer, a.candidates.split(","))
    print(json.dumps(res, ensure_ascii=False, indent=2))
