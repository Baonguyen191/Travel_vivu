import re

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OSM_DAYS = {"Mo": 0, "Tu": 1, "We": 2, "Th": 3, "Fr": 4, "Sa": 5, "Su": 6}

TIME_RANGE_RE = re.compile(r"^([0-2]\d:[0-5]\d)-([0-2]\d:[0-5]\d)$")
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

    result: dict[str, list[list[str]]] = {d: [] for d in DAYS}
    for rule in text.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        parts = rule.split(None, 1)
        if len(parts) != 2:
            return None
        day_spec, time_spec = parts[0], parts[1].strip()
        days = _expand_days(day_spec)
        if days is None:
            return None
        if time_spec == "off":
            for i in days:
                result[DAYS[i]] = []
            continue
        intervals = _parse_intervals(time_spec)
        if intervals is None:
            return None
        for i in days:
            result[DAYS[i]] = intervals
    if all(not v for v in result.values()):
        return None
    return result
