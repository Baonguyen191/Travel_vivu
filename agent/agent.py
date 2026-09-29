"""Agent tool-calling: LLM làm phần ngôn ngữ, tool làm phần đúng/sai.

Theo ranh giới trong CLAUDE.md: LLM trích ràng buộc thành tham số tool (bước
1) và diễn giải kết quả tool thành văn bản (bước 3). Lịch trình, giờ mở cửa,
giá vé, thời tiết và kiến thức về địa danh đều phải đến từ tool.
"""

import json
from dataclasses import dataclass, field

from agent.llm import LLMClient
from agent.tools import TOOLS, AgentContext, execute_tool

MAX_STEPS = 5
HISTORY_TURNS = 6

SYSTEM_PROMPT = """Bạn là trợ lý du lịch cho thành phố Huế. Hôm nay là {today}. Nơi ở của người dùng: {hotel}.

Quy tắc bắt buộc:
1. Kiến thức về địa danh (lịch sử, kiến trúc, mô tả): luôn gọi `search_knowledge`, chỉ dùng nội dung trả về,
   ghi nguồn dạng [1](url) theo `ref`. Không trả lời từ trí nhớ. Nội dung không có trong kết quả thì nói không tìm thấy.
2. Giờ mở cửa, giá vé, trang phục: luôn gọi `get_place_facts`. Trường null nghĩa là CSDL chưa có: nói rõ là chưa có,
   KHÔNG đoán, không lấy từ trí nhớ. Ghi ngày cập nhật.
3. Lịch trình: gọi `plan_trip`, trích đúng ngày (YYYY-MM-DD), số ngày, danh sách địa điểm theo thứ tự ưu tiên,
   phương tiện. KHÔNG tự sắp lịch, không đổi giờ hay thứ tự. Diễn giải lịch trả về: giờ, điểm, lời nhắc thời tiết,
   điểm không xếp được và lý do. Có `measured_on_real_weather` thì nêu số giờ ngoài trời khi mưa của hai lịch.
   Với yêu cầu lập lịch, chỉ gọi `plan_trip`; không gọi thêm tool khác trừ khi người dùng hỏi thêm.
4. Thời tiết: gọi `get_weather`, nêu số liệu kèm thời điểm. Ngày càng xa thì diễn đạt càng thận trọng.
5. Ngoài phạm vi Huế: nói rõ hệ thống chỉ hỗ trợ Huế.
Trả lời bằng tiếng Việt, ngắn gọn, dùng markdown. Không lặp lại toàn bộ bảng lịch (giao diện đã hiển thị bảng);
chỉ tóm tắt điểm chính."""


@dataclass
class ToolCall:
    name: str
    arguments: dict
    result_preview: str


@dataclass
class AgentReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)


def run_agent(question: str, history: list[dict], ctx: AgentContext, llm: LLMClient) -> AgentReply:
    """`history`: các lượt trước [{"role": "user"|"assistant", "text": ...}]."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(
        today=ctx.today.isoformat(), hotel=f"{ctx.hotel[0]:.4f}, {ctx.hotel[1]:.4f}")}]
    for turn in history[-HISTORY_TURNS:]:
        messages.append({"role": turn["role"], "content": turn["text"]})
    messages.append({"role": "user", "content": question})

    calls: list[ToolCall] = []
    for _ in range(MAX_STEPS):
        # Có lịch rồi thì chỉ còn việc diễn giải: không đưa tool nữa, tránh LLM gọi
        # thêm tra cứu cho từng điểm (tốn thời gian, token) dù prompt đã dặn.
        planned = any(c.name == "plan_trip" for c in calls)
        message = llm.chat(messages, tools=None if planned else TOOLS)
        if not message.tool_calls:
            return AgentReply(message.content or "", calls, ctx.artifacts)
        messages.append({"role": "assistant", "content": message.content or "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in message.tool_calls]})
        for tc in message.tool_calls:
            try:
                result = execute_tool(ctx, tc.function.name, tc.function.arguments)
            except Exception as exc:  # lỗi tool trả lại cho LLM để nó báo người dùng, không bịa
                result = {"error": f"{type(exc).__name__}: {exc}"}
            payload = json.dumps(result, ensure_ascii=False, default=str)
            calls.append(ToolCall(tc.function.name, json.loads(tc.function.arguments or "{}"), payload[:300]))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": payload})
    return AgentReply("Mình chưa hoàn tất được yêu cầu sau nhiều bước gọi công cụ; bạn thử hỏi cụ thể hơn nhé.",
                      calls, ctx.artifacts)
