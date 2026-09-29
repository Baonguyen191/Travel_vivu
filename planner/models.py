"""Kiểu dữ liệu của bộ lập lịch thích ứng thời tiết.

Mọi trường lái thuật toán giữ đúng định dạng của bảng `places` (xem
db/migrations/002_core.sql và config/weather_defaults.yml), để nạp thẳng từ DB
mà không cần bước dịch nhãn:

- `weather_sensitivity`: {"rain": 0..1, "heat": 0..1, "wind": 0..1}
- `unsafe_conditions`: nhãn tiếng Việt không dấu, xem `planner.rules.HAZARDS`
- `opening_hours`: {"mon": [["07:30", "17:30"]], ...}; ngày vắng mặt là đóng
  cửa, None là chưa biết giờ mở cửa.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_VISIT_MINUTES = 60


def _to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass
class Place:
    id: int | str
    name: str
    lat: float
    lon: float
    category: str = "khac"
    avg_visit_minutes: int = DEFAULT_VISIT_MINUTES
    indoor_ratio: float = 0.5  # 0 = ngoài trời hoàn toàn, 1 = trong nhà hoàn toàn
    weather_sensitivity: dict[str, float] = field(default_factory=dict)
    unsafe_conditions: list[str] = field(default_factory=list)
    opening_hours: dict[str, list[list[str]]] | None = None
    ticket_price_vnd: int | None = None
    priority: float = 1.0  # 0..1, mức muốn đi của người dùng

    def opening_windows(self, day: date) -> list[tuple[int, int]] | None:
        """Các khoảng mở cửa trong ngày, tính bằng phút từ 00:00.

        None khi chưa biết giờ mở cửa (bộ lập lịch coi là mở suốt ngày và ghi
        chú điều đó); danh sách rỗng khi đóng cửa cả ngày.
        """
        if self.opening_hours is None:
            return None
        intervals = self.opening_hours.get(WEEKDAYS[day.weekday()], [])
        return [(_to_minutes(start), _to_minutes(end)) for start, end in intervals]


@dataclass
class HourlyWeather:
    """Thời tiết của một khoảng thời gian bắt đầu tại `time`.

    `period_hours` = 1 với dự báo theo giờ; = 24 với bản ghi gộp theo ngày, khi
    đó `precipitation_mm` là tổng cả ngày và các trường khác là giá trị lớn
    nhất trong ngày. `rain_probability` có thể None: dữ liệu quan trắc và dự
    báo lưu trữ của Open-Meteo không có trường này. Mọi ngưỡng "mưa", "bão"
    nằm ở planner.rules, không nằm ở đây.
    """

    time: datetime
    precipitation_mm: float = 0.0
    rain_probability: float | None = None  # 0..100
    temperature_c: float = 27.0
    wind_speed_kmh: float = 0.0
    weather_code: int | None = None
    period_hours: int = 1


@dataclass
class UserConstraint:
    """Ràng buộc do LLM trích từ yêu cầu người dùng (bước 1 của quy trình)."""

    start_datetime: datetime  # thời điểm sớm nhất có thể bắt đầu đi
    end_datetime: datetime  # thời điểm muộn nhất phải về tới điểm kết thúc
    start_location: tuple[float, float]  # (lat, lon), thường là khách sạn
    end_location: tuple[float, float]
    day_start: time = time(8, 0)
    day_end: time = time(18, 0)
    max_places_per_day: int = 6
    transport_mode: str = "motorbike"  # motorbike | car | walking
    budget_vnd: int | None = None


@dataclass
class ScheduledVisit:
    place_id: int | str
    place_name: str
    arrival_time: datetime
    departure_time: datetime
    travel_minutes_from_prev: int
    travel_distance_km: float
    indoor_ratio: float
    weather_note: str | None = None
    google_maps_url: str | None = None  # dẫn đường từ điểm trước tới điểm này
    # Khuyên đổi phương tiện cho chặng này (vd. mưa lớn khi đi xe máy).
    travel_advice: str | None = None
    # Tuyến Google cho chặng tới điểm này (encoded polyline) và ghi chú chọn tuyến;
    # chỉ có khi gọi planner.routes.attach_routes.
    route_polyline: str | None = None
    route_note: str | None = None

    @property
    def visit_minutes(self) -> int:
        return int((self.departure_time - self.arrival_time).total_seconds() // 60)


@dataclass
class DayItinerary:
    date: date
    visits: list[ScheduledVisit] = field(default_factory=list)
    weather_tier: str = "none"  # hourly | daily | climate | none, xem planner.weather
    notes: list[str] = field(default_factory=list)
    # Dẫn đường cả ngày; nhiều hơn một link khi vượt giới hạn waypoint của Maps URLs.
    navigation_urls: list[str] = field(default_factory=list)
    return_route_polyline: str | None = None  # chặng từ điểm cuối về khách sạn

    @property
    def navigation_url(self) -> str | None:
        return self.navigation_urls[0] if self.navigation_urls else None

    @property
    def total_travel_minutes(self) -> int:
        return sum(v.travel_minutes_from_prev for v in self.visits)


@dataclass
class ScheduleResult:
    itineraries: list[DayItinerary] = field(default_factory=list)
    # place_id -> lý do không xếp được
    unvisited: dict[int | str, str] = field(default_factory=dict)
    # Những quyết định thời tiết người dùng cần biết (khung giờ bị tránh vì bão...).
    weather_adaptations: list[str] = field(default_factory=list)
    solver_status: str = ""
    objective: int | None = None

    @property
    def visits(self) -> list[ScheduledVisit]:
        return [v for day in self.itineraries for v in day.visits]
