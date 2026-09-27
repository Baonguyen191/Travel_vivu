"""Phần xử lý ngôn ngữ của chatbot demo, không phụ thuộc Streamlit.

Dự án chưa dùng LLM API (CLAUDE.md), nên bước 1 "trích ràng buộc" và bước 3
"diễn giải" ở đây làm bằng luật đơn giản: nhận ý định theo từ khoá, bắt ngày,
số ngày, phương tiện và tên địa danh (không phân biệt dấu). Phần đúng/sai vẫn
do các tool thật: RAG hybrid, bộ lập lịch OR-Tools, Open-Meteo, Google Maps.
Khi có LLM, thay các hàm `parse_*`/`detect_intent` bằng LLM tool-calling.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from rag.lexical import fold_diacritics

# Địa danh demo: QID Wikidata -> các cách gọi (đã bỏ dấu). Giới hạn ở các điểm
# chính để tránh khớp nhầm tên quán ăn hay tên phường.
LANDMARKS: dict[str, list[str]] = {
    "Q10769129": ["hoang thanh", "dai noi"],
    "Q7023311": ["ngo mon"],
    "Q877185": ["kinh thanh"],
    "Q975568": ["chua thien mu", "thien mu", "linh mu"],
    "Q7481171": ["lang tu duc", "khiem lang"],
    "Q7818621": ["lang khai dinh", "ung lang"],
    "Q7481070": ["lang minh mang", "hieu lang"],
    "Q16511573": ["lang thieu tri", "xuong lang"],
    "Q7818615": ["lang gia long", "thien tho lang"],
    "Q5929149": ["bao tang co vat", "bao tang my thuat cung dinh", "bao tang cung dinh"],
    "Q97273263": ["cho dong ba"],
    "Q10752407": ["cau truong tien"],
    "Q10840433": ["dan nam giao", "nam giao"],
    "Q10751137": ["cung an dinh"],
    "Q10748576": ["chua tu hieu", "tu hieu"],
    "Q10833485": ["van mieu"],
    "Q10752425": ["cau ngoi thanh toan", "thanh toan"],
    "Q1515971": ["song huong"],
}
# Khi người dùng không nêu điểm nào: các điểm nổi bật, theo thứ tự ưu tiên.
DEFAULT_TRIP = ["Q10769129", "Q975568", "Q7481171", "Q7818621", "Q5929149",
                "Q7481070", "Q97273263", "Q10840433"]

PLAN_WORDS = ("lich trinh", "lap lich", "len lich", "xep lich", "ke hoach", "di choi", "tham quan",
              "tour", "nen di dau", "di dau")
INFO_WORDS = ("gio mo cua", "mo cua", "may gio", "dong cua", "gia ve", "ve vao", "bao nhieu tien",
              "trang phuc", "mac gi")
WEATHER_WORDS = ("thoi tiet", "co mua", "mua khong", "nang khong", "du bao", "nhiet do")
WEEKDAYS = {"thu hai": 0, "thu ba": 1, "thu tu": 2, "thu nam": 3, "thu sau": 4, "thu bay": 5, "chu nhat": 6}
NUMBER_WORDS = {"mot": 1, "hai": 2, "ba": 3, "bon": 4, "nam": 5}


def fold(text: str) -> str:
    return re.sub(r"\s+", " ", fold_diacritics(text)).strip()


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def detect_intent(text: str) -> str:
    """plan | info | weather | ask."""
    t = fold(text)
    if any(_has(t, w) for w in PLAN_WORDS) or re.search(r"\b\d+\s*ngay\b", t):
        return "plan"
    if any(_has(t, w) for w in INFO_WORDS) and match_landmarks(text):
        return "info"
    if any(_has(t, w) for w in WEATHER_WORDS):
        return "weather"
    return "ask"


def match_landmarks(text: str) -> list[str]:
    """QID các địa danh được nhắc tới, theo thứ tự xuất hiện trong câu."""
    t = fold(text)
    found: dict[str, int] = {}
    taken: list[tuple[int, int]] = []
    aliases = sorted(((a, q) for q, names in LANDMARKS.items() for a in names), key=lambda x: -len(x[0]))
    for alias, qid in aliases:
        for m in re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", t):
            if any(s < m.end() and m.start() < e for s, e in taken):
                continue  # nằm trong một cách gọi dài hơn đã khớp
            taken.append((m.start(), m.end()))
            found.setdefault(qid, m.start())
    return sorted(found, key=found.get)


def parse_date(text: str, today: date) -> date | None:
    t = fold(text)
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?\b", t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            return date(year, month, day)
        except ValueError:
            return None
    if _has(t, "hom nay"):
        return today
    if _has(t, "ngay mai") or _has(t, "mai"):
        return today + timedelta(days=1)
    if _has(t, "ngay kia") or _has(t, "ngay mot") or _has(t, "mot"):
        return today + timedelta(days=2)
    for name, weekday in WEEKDAYS.items():
        if _has(t, name):
            return today + timedelta(days=(weekday - today.weekday()) % 7)
    return None


def parse_days(text: str) -> int | None:
    t = fold(text)
    m = re.search(r"\b(\d+)\s*ngay\b", t)
    if m:
        return max(1, min(int(m.group(1)), 7))
    for word, n in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\s+ngay\b", t):
            return n
    return None


def parse_mode(text: str) -> str | None:
    t = fold(text)
    if any(_has(t, w) for w in ("o to", "oto", "xe hoi", "taxi", "grab car")):
        return "car"
    if _has(t, "di bo"):
        return "walking"
    if any(_has(t, w) for w in ("xe may", "xe dap dien", "grab bike")):
        return "motorbike"
    return None


@dataclass
class TripRequest:
    start: date
    days: int
    qids: list[str]
    mode: str
    assumptions: list[str] = field(default_factory=list)  # những gì bot tự giả định, nói lại với người dùng


def parse_trip(text: str, today: date, default_mode: str = "motorbike") -> TripRequest:
    assumptions = []
    start = parse_date(text, today)
    if start is None:
        start = today + timedelta(days=1)
        assumptions.append(f"chưa rõ ngày đi, lấy ngày mai ({start:%d/%m/%Y})")
    days = parse_days(text)
    if days is None:
        days = 1
        assumptions.append("chưa rõ số ngày, lấy 1 ngày")
    qids = match_landmarks(text)
    if not qids:
        qids = list(DEFAULT_TRIP)
        assumptions.append("chưa nêu địa điểm, dùng các điểm nổi bật của Huế")
    mode = parse_mode(text)
    if mode is None:
        mode = default_mode
    return TripRequest(start, days, qids, mode, assumptions)


# ---------------------------------------------------------------------------
# Diễn giải kết quả tool thành câu trả lời (thay cho bước 3 của LLM)
# ---------------------------------------------------------------------------


def best_sentences(query: str, content: str, limit: int = 3, context: str = "") -> str:
    """Các câu trong chunk trùng nhiều từ với câu hỏi nhất, giữ thứ tự gốc.

    Từ trong `context` (tên địa danh) không tính điểm: câu nào trong chunk cũng
    nói về địa danh đó, nên tên không giúp chọn câu trả lời.
    """
    q = set(fold(query).split()) - set(fold(context).split())
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", content) if len(s.strip()) > 20]
    if not sentences:
        return content[:400]
    scored = sorted(range(len(sentences)), key=lambda i: -len(q & set(fold(sentences[i]).split())))
    keep = sorted(scored[:limit])
    return " ".join(sentences[i] for i in keep)


def format_opening_hours(hours: dict | None, raw: str | None) -> str:
    if raw:
        return raw
    if not hours:
        return ""
    names = {"mon": "T2", "tue": "T3", "wed": "T4", "thu": "T5", "fri": "T6", "sat": "T7", "sun": "CN"}
    return "; ".join(f"{names[d]} " + ", ".join(f"{a}-{b}" for a, b in spans) for d, spans in hours.items())
