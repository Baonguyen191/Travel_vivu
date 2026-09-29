"""Baseline "LLM tự lập lịch" (CLAUDE.md) khi chưa có LLM API.

Quy trình: `llm_prompt` sinh prompt chứa đúng dữ liệu bộ lập lịch được thấy
(địa điểm, giờ mở cửa, thời gian tham quan, dự báo); người làm thực nghiệm dán
vào một LLM, lưu câu trả lời JSON; `schedule_from_json` đọc lại để chấm bằng
cùng bộ kiểm tra khả thi và cùng thời tiết quan trắc với bộ lập lịch.
"""

import json
from datetime import date, datetime, time
from typing import Iterable

from planner import rules
from planner.models import DayItinerary, Place, ScheduledVisit, ScheduleResult, UserConstraint
from planner.solver import trip_days
from planner.weather import GridWeather

JSON_FORMAT = """{
  "days": [
    {"date": "YYYY-MM-DD",
     "visits": [{"place_id": 123, "arrival": "HH:MM", "departure": "HH:MM"}]}
  ]
}"""


def _hours_text(place: Place) -> str:
    if place.opening_hours is None:
        return "chưa rõ"
    return "; ".join(f"{d}: " + ", ".join(f"{a}-{b}" for a, b in spans)
                     for d, spans in place.opening_hours.items()) or "đóng cửa"


def _forecast_text(weather: GridWeather | None, days: Iterable[date], lat: float, lon: float) -> str:
    if weather is None:
        return "Không có dự báo."
    lines = []
    for day in days:
        cells = []
        for hour in range(6, 21):
            w = weather.at(lat, lon, datetime.combine(day, time(hour)))
            if w is None:
                continue
            flags = "".join(sorted(rules.hazards(w)))
            cells.append(f"{hour}h {w.precipitation_mm:.1f}mm {w.wind_speed_kmh:.0f}km/h"
                         + (f" [{flags}]" if flags else ""))
        lines.append(f"- {day.isoformat()} ({weather.tier(day)}): " + ("; ".join(cells) or "không có dữ liệu"))
    return "\n".join(lines)


def llm_prompt(places: list[Place], constraint: UserConstraint, weather: GridWeather | None) -> str:
    days = [d for d, _, _ in trip_days(constraint)]
    place_lines = "\n".join(
        f"- place_id={p.id} | {p.name} | ({p.lat:.5f}, {p.lon:.5f}) | tham quan ~{p.avg_visit_minutes} phút"
        f" | tỷ lệ trong nhà {p.indoor_ratio:.2f} | nguy hiểm khi: {', '.join(p.unsafe_conditions) or 'không'}"
        f" | giờ mở cửa: {_hours_text(p)}"
        for p in places)
    return f"""Bạn là trợ lý du lịch ở Huế. Hãy lập lịch tham quan theo các ràng buộc dưới đây.

Thời gian: từ {constraint.start_datetime:%Y-%m-%d %H:%M} đến {constraint.end_datetime:%Y-%m-%d %H:%M};
mỗi ngày đi trong khung {constraint.day_start:%H:%M}-{constraint.day_end:%H:%M}, xuất phát và quay về
khách sạn tại {constraint.start_location}. Phương tiện: {constraint.transport_mode}.
Tối đa {constraint.max_places_per_day} điểm mỗi ngày. Mỗi điểm đi nhiều nhất một lần.

Địa điểm có thể đi:
{place_lines}

Dự báo thời tiết theo giờ (lượng mưa, gió; bao = bão/dông, mua_lon = mưa lớn, song_lon = biển động):
{_forecast_text(weather, days, *constraint.start_location)}

Yêu cầu: đi được nhiều điểm nhất có thể, tránh ở ngoài trời khi mưa, không đến điểm đang có hiện tượng
nguy hiểm được liệt kê cho điểm đó, đến và rời trong giờ mở cửa, đủ thời gian di chuyển giữa các điểm.

Chỉ trả về JSON đúng định dạng sau, không kèm giải thích:
{JSON_FORMAT}
"""


def schedule_from_json(data: dict | str, places: dict[int | str, Place]) -> ScheduleResult:
    """Đọc lịch dạng JSON (định dạng JSON_FORMAT). place_id lạ vẫn giữ để bộ kiểm tra báo lỗi."""
    if isinstance(data, str):
        data = json.loads(data)
    result = ScheduleResult(solver_status="llm")
    for day_data in data.get("days", []):
        day = date.fromisoformat(day_data["date"])
        itinerary = DayItinerary(date=day)
        for v in day_data.get("visits", []):
            pid = v["place_id"]
            place = places.get(pid)
            arrival = datetime.combine(day, time.fromisoformat(v["arrival"]))
            departure = datetime.combine(day, time.fromisoformat(v["departure"]))
            itinerary.visits.append(ScheduledVisit(
                place_id=pid, place_name=place.name if place else str(pid), arrival_time=arrival,
                departure_time=departure, travel_minutes_from_prev=0, travel_distance_km=0.0,
                indoor_ratio=place.indoor_ratio if place else 0.5,
            ))
        result.itineraries.append(itinerary)
    return result
