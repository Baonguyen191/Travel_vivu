import re

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


def _parse_intervals(spec: str) -> list[list[str]] | None:
    intervals = []
    for part in spec.split(","):
        match = TIME_RANGE_RE.match(part.strip())
        if not match:
            return None
        intervals.append([match.group(1), match.group(2)])
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
    matched_any_rule = False
    for parts in clause_parts:
        if len(parts) == 1:
            # Không có phần ngày: quy tắc chỉ gồm dải giờ, áp dụng mọi ngày.
            intervals = _parse_intervals(parts[0])
            if intervals is None:
                return None
            for d in DAYS:
                result[d] = intervals
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
        for i in days:
            result[DAYS[i]] = intervals
        matched_any_rule = True
    if not matched_any_rule:
        return None
    return result
