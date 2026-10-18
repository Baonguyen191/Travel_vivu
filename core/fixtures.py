"""Dữ liệu mẫu nhỏ ở Huế để các module test và chạy thử độc lập (quy tắc "chạy một mình" trong
docs/modules.md).

Chỉ dùng cho test và ví dụ: tọa độ, giờ mở cửa, thời tiết là giá trị xấp xỉ / tổng hợp, không dùng
làm dữ liệu thật hay đưa vào thực nghiệm.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

from core.config import VN_TZ
from core.types import (
    HOTEL_KEY, Location, OpeningHours, OpeningRule, Place, Preferences, Problem, Schedule,
    TravelMatrix, Visit, WeatherHour,
)

DAI_NOI = Place(
    "wikidata:Q10769129", "Hoàng thành Huế (Đại Nội)", 16.4698, 107.5786, "di_tich", 150, 0.2,
    OpeningHours((
        OpeningRule(((time(6, 30), time(17, 30)),), months=frozenset(range(4, 10))),
        OpeningRule(((time(7, 0), time(17, 0)),)),
    ), note="fixture: giờ hè tháng 4–9, còn lại giờ đông; mốc mùa chưa kiểm nguồn"),
    unsafe_conditions={"bao"}, best_time_of_day={"sang_som", "chieu_muon"},
    ticket_price_vnd=(200_000, 200_000), label_source="manual",
)
THIEN_MU = Place(
    "wikidata:Q975568", "Chùa Thiên Mụ", 16.4530, 107.5451, "chua", 45, 0.3,
    OpeningHours.daily("08:00", "18:00"), unsafe_conditions={"mua_lon", "bao"},
    best_time_of_day={"sang_som", "hoang_hon"}, ticket_price_vnd=(0, 0), label_source="place_facts",
)
TU_DUC = Place(
    "wikidata:Q7481171", "Lăng Tự Đức", 16.4332, 107.5646, "lang_tam", 90, 0.25,
    OpeningHours.daily("06:30", "17:30"), unsafe_conditions={"mua_lon", "bao"},
    best_time_of_day={"sang_som", "chieu_muon"}, ticket_price_vnd=(150_000, 150_000), label_source="place_facts",
)
CAU_TRUONG_TIEN = Place(
    "wikidata:Q10752407", "Cầu Trường Tiền", 16.4688, 107.5901, "diem_ngam_canh", 20, 0.0,
    OpeningHours(always_open=True), unsafe_conditions={"bao"},
    best_time_of_day={"hoang_hon", "toi"}, ticket_price_vnd=(0, 0), label_source="place_facts",
)
BAO_TANG = Place(
    "wikidata:Q5929149", "Bảo tàng Cổ vật Cung đình Huế", 16.4719, 107.5815, "bao_tang", 60, 0.95,
    OpeningHours.daily("07:00", "17:00"), best_time_of_day={"bat_ky"},
    ticket_price_vnd=(50_000, 50_000), label_source="place_facts",
)
PLACES = (DAI_NOI, THIEN_MU, TU_DUC, CAU_TRUONG_TIEN, BAO_TANG)
HOTEL = Location("Khách sạn trung tâm (fixture)", 16.4637, 107.5909)


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def travel_matrix(places=PLACES, hotel: Location = HOTEL, mode: str = "motorbike",
                  speed_kmh: float = 25.0, detour: float = 1.3, overhead_min: float = 5.0) -> TravelMatrix:
    """Ước lượng thô: đường chim bay × hệ số đường vòng / tốc độ + phút gửi xe."""
    points = [(HOTEL_KEY, (hotel.lat, hotel.lon))] + [(p.key, (p.lat, p.lon)) for p in places]
    minutes = [[0.0 if i == j else round(_km(a, b) * detour / speed_kmh * 60 + overhead_min, 1)
                for j, (_, b) in enumerate(points)] for i, (_, a) in enumerate(points)]
    return TravelMatrix(tuple(k for k, _ in points), minutes, mode, "estimate")


def weather_day(day: date, rainy_hours=range(13, 16), lat: float = 16.46, lon: float = 107.58) -> tuple[WeatherHour, ...]:
    """Dự báo giả cho một ngày 06:00–20:00: mưa lớn trong `rainy_hours`, phát hành 18:00 hôm trước."""
    issued = datetime.combine(day - timedelta(days=1), time(18, 0), VN_TZ)
    out = []
    for hour in range(6, 21):
        rainy = hour in rainy_hours
        out.append(WeatherHour(
            datetime.combine(day, time(hour, 0), VN_TZ), lat, lon,
            rain_prob=0.85 if rainy else 0.1, precip_mm=8.0 if rainy else 0.0,
            temp_c=27.0, wind_kmh=12.0, source="stub",
            conditions={"mua_lon"} if rainy else frozenset(), issued_at=issued,
        ))
    return tuple(out)


def sample_problem(days: int = 1, start: date = date(2025, 10, 15)) -> Problem:
    weather = tuple(h for d in range(days) for h in weather_day(start + timedelta(days=d)))
    return Problem(
        problem_id=f"fixture-hue-{days}d", places=PLACES, start_date=start, days=days,
        day_start=time(8, 0), day_end=time(17, 30), hotel=HOTEL, travel=travel_matrix(),
        weather=weather, max_places_per_day=4,
        preferences=Preferences(must_visit={DAI_NOI.key}, interests={"di_tich"}),
        request_text=f"Tôi ở Huế {days} ngày, nhất định đi Đại Nội, ngại đi ngoài trời lúc mưa.",
    )


def sample_schedule() -> Schedule:
    """Lịch mẫu cho test serialize; KHÔNG khẳng định là lịch khả thi."""
    return Schedule((
        Visit(1, DAI_NOI.key, time(8, 15), time(10, 45)),
        Visit(1, BAO_TANG.key, time(10, 55), time(11, 55)),
        Visit(1, THIEN_MU.key, time(16, 15), time(17, 0)),
    ), source="manual", explanation="Sáng Đại Nội và bảo tàng; tránh mưa đầu giờ chiều; chiều muộn chùa Thiên Mụ.")
