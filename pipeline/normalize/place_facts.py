"""Giờ mở cửa và giá vé nhập từ nguồn web vào `config/place_facts.csv`.

Tách khỏi `config/overrides.csv` vì file đó là nhãn tay cho nhóm trường thời
tiết (đặt `label_source = 'manual'`, chỉ số QA gán nhãn đọc trường này).

- `opening_hours`: cú pháp OSM, parse bằng cùng parser với dữ liệu OSM. Khi các
  nguồn khác nhau (giờ hè/đông, trang này trang kia) ghi khung rộng nhất để bộ
  kiểm tra không báo vi phạm oan; khung hẹp ghi ở `note`.
- `ticket_price_vnd`: một số (`150000`) hoặc khoảng (`0-50000`). Khoảng lưu
  `vnd` = cận dưới (lập lịch và kiểm ngân sách dùng cận dưới cho đỡ khắt khe),
  `vnd_max` = cận trên.
- `source_url` (nhiều link cách nhau bằng dấu cách), `checked_on`: lưu cùng giá
  vé để agent trích nguồn đúng cho giờ mở cửa và giá vé.

Ô trống nghĩa là chưa có nguồn: giữ nguyên giá trị đang có, không đoán.
"""

import csv
import sys
from dataclasses import replace

from pipeline.normalize.opening_hours import parse_opening_hours

FACTS_PATH = "config/place_facts.csv"


def _warn(key: str, message: str) -> None:
    print(f"Cảnh báo: config/place_facts.csv '{key}': {message}", file=sys.stderr)


def parse_ticket(raw: str) -> dict:
    """'150000' -> {'vnd': 150000}; '0-50000' -> {'vnd': 0, 'vnd_max': 50000}."""
    low, _, high = raw.replace(" ", "").replace(".", "").partition("-")
    lo = int(low)
    if not high:
        return {"vnd": lo}
    hi = int(high)
    if hi < lo:
        raise ValueError(f"khoảng giá ngược: {raw}")
    return {"vnd": lo, "vnd_max": hi}


def load_place_facts(path: str = FACTS_PATH) -> dict[str, dict]:
    result: dict[str, dict] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("place_key") or "").strip()
            if not key:
                continue
            entry: dict = {}
            hours = (row.get("opening_hours") or "").strip()
            if hours:
                parsed = parse_opening_hours(hours)
                if parsed is None:
                    _warn(key, f"bỏ qua giờ mở cửa không parse được: {hours!r}")
                else:
                    entry["opening_hours_raw"] = hours
                    entry["opening_hours"] = parsed
            price = (row.get("ticket_price_vnd") or "").strip()
            if price:
                try:
                    ticket = parse_ticket(price)
                except ValueError:
                    _warn(key, f"bỏ qua giá vé không hợp lệ: {price!r}")
                else:
                    sources = (row.get("source_url") or "").split()
                    if sources:
                        ticket["source_url"] = sources
                    if row.get("checked_on"):
                        ticket["checked_on"] = row["checked_on"].strip()
                    entry["ticket_price"] = ticket
            if entry and not (row.get("source_url") or "").strip():
                _warn(key, "thiếu source_url")
            if entry:
                result[key] = entry
    return result


def apply_place_facts(place, facts: dict, matched_keys: set | None = None):
    for source, external_id in place.external_ids.items():
        key = f"{source}:{external_id}"
        entry = facts.get(key)
        if entry:
            if matched_keys is not None:
                matched_keys.add(key)
            return replace(place, **entry)
    return place
