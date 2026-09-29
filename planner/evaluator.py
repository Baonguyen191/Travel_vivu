"""Đo hệ quả thời tiết của một lịch trình trên thời tiết *quan trắc*.

Lịch được lập trên dự báo; ở đây chấm nó trên thời tiết đã thật sự xảy ra
(ERA5, xem planner.weather.fetch_observed). Dùng đúng các quy tắc của
planner.rules, nên chênh lệch giữa bộ lập lịch có và không có thời tiết đến từ
quyết định lập lịch và sai số dự báo, không từ hai bộ ngưỡng khác nhau.

Hai chỉ số theo CLAUDE.md:
- outdoor_rain_hours: tổng giờ ở ngoài trời trong lúc mưa, tức Σ (số giờ trùng
  mưa quan trắc × tỷ lệ ngoài trời của điểm).
- hazard_visits: số điểm đến trùng hiện tượng nguy hiểm nằm trong
  `unsafe_conditions` của điểm đó, tức hoạt động lẽ ra phải huỷ.
"""

from dataclasses import dataclass, field

from planner import rules
from planner.models import Place, ScheduleResult
from planner.weather import GridWeather, hours_between


@dataclass
class WeatherOutcome:
    visits: int = 0
    outdoor_rain_hours: float = 0.0
    hazard_visits: int = 0
    hazard_details: list[str] = field(default_factory=list)
    unobserved_hours: float = 0.0  # giờ tham quan không có dữ liệu quan trắc


def weather_outcome(result: ScheduleResult, places: dict[int | str, Place],
                    observed: GridWeather) -> WeatherOutcome:
    out = WeatherOutcome()
    for v in result.visits:
        place = places.get(v.place_id)
        if place is None:
            continue
        out.visits += 1
        outdoor = 1.0 - place.indoor_ratio
        hazard = None
        for t, overlap in hours_between(v.arrival_time, v.departure_time):
            w = observed.observed_at(place.lat, place.lon, t)
            if w is None:
                out.unobserved_hours += overlap
                continue
            if rules.is_raining(w):
                out.outdoor_rain_hours += overlap * outdoor
            hazard = hazard or rules.unsafe_reason(place, w)
        if hazard:
            out.hazard_visits += 1
            out.hazard_details.append(f"{place.name} {v.arrival_time:%d/%m %H:%M}: {hazard}")
    out.outdoor_rain_hours = round(out.outdoor_rain_hours, 3)
    return out
