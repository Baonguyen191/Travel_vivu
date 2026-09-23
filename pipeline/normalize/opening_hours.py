import re
from collections import defaultdict

# Quy ước giờ trong dict trả về: "24:00" nghĩa là hết ngày (nửa đêm), cùng
# quy ước OSM/Overpass đã dùng cho "24/7" — không phải "00:00" của ngày hôm
# sau. Một khoảng qua nửa đêm (ví dụ 17:00-01:30) được TÁCH thành hai đoạn:
# [start, "24:00"] ghi vào ngày hiện tại, và ["00:00", end] ghi vào ngày kế
# tiếp — end - start tính trực tiếp trên một đoạn không bao giờ âm.
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OSM_DAYS = {"Mo": 0, "Tu": 1, "We": 2, "Th": 3, "Fr": 4, "Sa": 5, "Su": 6}

TIME_RANGE_RE = re.compile(
    r"^((?:[01]\d|2[0-4]):[0-5]\d)-((?:[01]\d|2[0-4]):[0-5]\d)$"
)
DAY_TOKEN_RE = re.compile(r"^(Mo|Tu|We|Th|Fr|Sa|Su)(-(Mo|Tu|We|Th|Fr|Sa|Su))?$")


def _expand_days(spec: str) -> list[int] | None:
    indexes: list[int] = []
    for token in spec.split(","):
        match = DAY_TOKEN_RE.match(token.strip())
        if not match:
            return None
        start = OSM_DAYS[match.group(1)]
        end = OSM_DAYS[match.group(3)] if match.group(3) else start
        i = start
        while True:
            indexes.append(i)
            if i == end:
                break
            i = (i + 1) % 7
    return indexes


def _to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _split_interval(start: str, end: str) -> list[tuple[int, list[str]]] | None:
    """Tách một khoảng giờ thành các đoạn (day_offset, [start, end]).

    day_offset 0 = cùng ngày với quy tắc, 1 = tràn sang ngày kế tiếp.
    Trả về None nếu khoảng suy biến (start == end, không có ý nghĩa thời
    lượng nào) — khi đó toàn bộ chuỗi bị từ chối như một chuỗi không hỗ trợ.
    """
    if start == end:
        return None
    start_min, end_min = _to_minutes(start), _to_minutes(end)
    if end_min > start_min:
        return [(0, [start, end])]
    # end_min <= start_min: khoảng qua nửa đêm (vd 17:00-01:30, 16:00-04:00).
    segments: list[tuple[int, list[str]]] = [(0, [start, "24:00"])]
    if end != "00:00":
        # "...-00:00" là cách viết tắt phổ biến cho "đến nửa đêm" — tương
        # đương "...-24:00", không phải một khoảng thật sự tràn sang ngày
        # sau. Tách nó ra sẽ tạo một đoạn ["00:00", "00:00"] rỗng vô nghĩa
        # ở ngày kế tiếp, nên bỏ qua phần tràn trong trường hợp này.
        segments.append((1, ["00:00", end]))
    return segments


def _parse_intervals(spec: str) -> list[tuple[int, list[str]]] | None:
    intervals: list[tuple[int, list[str]]] = []
    for part in spec.split(","):
        match = TIME_RANGE_RE.match(part.strip())
        if not match:
            return None
        segments = _split_interval(match.group(1), match.group(2))
        if segments is None:
            return None
        intervals.extend(segments)
    return intervals


def parse_opening_hours(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text == "24/7":
        return {d: [["00:00", "24:00"]] for d in DAYS}

    clauses = [c.strip() for c in text.split(";")]
    clauses = [c for c in clauses if c]
    if not clauses:
        return None
    clause_parts = [c.split(None, 1) for c in clauses]

    # Quy tắc không kèm phần ngày chỉ được coi là "áp dụng cả tuần" khi nó
    # là quy tắc duy nhất trong chuỗi. Nếu chuỗi có nhiều quy tắc và một
    # quy tắc thiếu phần ngày, ý định mơ hồ (có thể là quên dấu phẩy nối
    # thêm dải giờ vào quy tắc trước) nên từ chối toàn bộ chuỗi.
    if len(clauses) > 1 and any(len(p) == 1 for p in clause_parts):
        return None

    result: dict[str, list[list[str]]] = {d: [] for d in DAYS}
    # Đoạn giờ tràn từ ngày hôm trước sang (offset 1 của _split_interval).
    # Cộng dồn riêng, không bị một clause khác GHI ĐÈ result[ngày kế tiếp]
    # — hai quy tắc chạm cùng một ngày vẫn phải giữ được phần tràn từ đêm
    # hôm trước.
    spillover: dict[str, list[list[str]]] = defaultdict(list)
    matched_any_rule = False

    def _apply(days_idx: list[int], intervals: list[tuple[int, list[str]]]) -> None:
        own = [seg for offset, seg in intervals if offset == 0]
        spill = [seg for offset, seg in intervals if offset == 1]
        for i in days_idx:
            result[DAYS[i]] = own
            if spill:
                spillover[DAYS[(i + 1) % 7]].extend(spill)

    for parts in clause_parts:
        if len(parts) == 1:
            # Không có phần ngày: quy tắc chỉ gồm dải giờ, áp dụng mọi ngày.
            intervals = _parse_intervals(parts[0])
            if intervals is None:
                return None
            _apply(range(7), intervals)
            matched_any_rule = True
            continue
        day_spec, time_spec = parts[0], parts[1].strip()
        days = _expand_days(day_spec)
        if days is None:
            return None
        if time_spec == "off":
            for i in days:
                result[DAYS[i]] = []
            matched_any_rule = True
            continue
        intervals = _parse_intervals(time_spec)
        if intervals is None:
            return None
        _apply(days, intervals)
        matched_any_rule = True
    if not matched_any_rule:
        return None
    for day, extra in spillover.items():
        result[day] = extra + result[day]
    return result
