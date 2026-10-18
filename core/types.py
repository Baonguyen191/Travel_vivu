"""Kiểu dữ liệu chung giữa các module (docs/modules.md, mục "Giao diện công khai").

Quy ước:
- Mọi kiểu là dataclass bất biến, kiểm **hình dạng** khi tạo (khoảng giá trị, từ vựng, giờ đóng sau
  giờ mở...). Kiểm **ngữ nghĩa** (đến khi đã đóng cửa, đi không kịp...) là việc của `planner_core`,
  nên một lịch sai do LLM sinh ra vẫn biểu diễn được để chấm.
- Thời điểm luôn có múi giờ (`core.config.VN_TZ`); giờ trong ngày dạng `time`, JSON là "HH:MM".
- `to_dict()` cho JSON; `Kiểu.from_dict(d)` dựng lại và kiểm hợp lệ. Lưu/đọc file: `core.io`.
"""

from __future__ import annotations

import collections.abc as cabc
import math
import re
import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Mapping

# ----------------------------------------------------------------------------- từ vựng chung

WEATHER_CONDITIONS = frozenset({"mua_lon", "bao", "nang_gat", "gio_lon", "song_lon"})
TIMES_OF_DAY = frozenset({"sang_som", "sang", "trua", "chieu", "chieu_muon", "hoang_hon", "toi", "bat_ky"})
LABEL_SOURCES = frozenset({"manual", "place_facts", "osm", "wikidata", "default"})
TRAVEL_MODES = frozenset({"walk", "bicycle", "motorbike", "car"})
MATRIX_SOURCES = frozenset({"osrm", "google", "estimate", "stub"})
WEATHER_SOURCES = frozenset({"forecast", "forecast_calibrated", "era5", "climatology", "stub"})
SCHEDULE_SOURCES = frozenset({"llm", "ortools", "repaired", "baseline", "random", "manual"})
PACES = frozenset({"cham", "vua", "nhanh"})
HOTEL_KEY = "hotel"
ALL_MONTHS = frozenset(range(1, 13))
ALL_WEEKDAYS = frozenset(range(7))  # 0 = thứ Hai ... 6 = Chủ nhật (như date.weekday())

_PLACE_KEY = re.compile(r"^(wikidata:Q\d+|osm:(node|way|relation)/\d+|place:\d+)$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def _finite(x: float, name: str) -> None:
    _check(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x), f"{name} không hợp lệ: {x!r}")


def _set(obj: Any, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def is_place_key(key: str) -> bool:
    return isinstance(key, str) and bool(_PLACE_KEY.match(key))


def fmt_time(t: time) -> str:
    return t.strftime("%H:%M")


def parse_time(s: str) -> time:
    m = _HHMM.match(s) if isinstance(s, str) else None
    _check(m is not None, f"giờ phải dạng HH:MM: {s!r}")
    return time(int(m.group(1)), int(m.group(2)))


def parse_datetime(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    _check(dt.tzinfo is not None, f"thời điểm thiếu múi giờ: {s!r}")
    return dt


def minutes_of(t: time) -> int:
    return t.hour * 60 + t.minute


# ----------------------------------------------------------------------------- (de)serialize chung

def to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        out = {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj) if f.init}
        extra = getattr(obj, "_json_extra", None)
        if extra:
            out.update(to_jsonable(extra()))
        return out
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, time):
        return fmt_time(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(to_jsonable(v) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, cabc.Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    return obj


def _decode(tp: Any, value: Any, where: str) -> Any:
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if tp is Any:
        return value
    if origin in (typing.Union, types.UnionType):
        if value is None:
            _check(type(None) in args, f"{where}: không được null")
            return None
        (inner,) = [a for a in args if a is not type(None)]
        return _decode(inner, value, where)
    if isinstance(tp, type) and is_dataclass(tp):
        _check(isinstance(value, cabc.Mapping), f"{where}: cần object cho {tp.__name__}")
        return _from_mapping(tp, value, where)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if tp is datetime:
        return parse_datetime(value)
    if tp is date:
        return date.fromisoformat(value)
    if tp is time:
        return parse_time(value)
    if origin in (frozenset, set):
        _check(isinstance(value, list), f"{where}: cần danh sách")
        return frozenset(_decode(args[0], v, where) for v in value)
    if origin is tuple:
        _check(isinstance(value, list), f"{where}: cần danh sách")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], v, f"{where}[{i}]") for i, v in enumerate(value))
        _check(len(value) == len(args), f"{where}: cần {len(args)} phần tử")
        return tuple(_decode(a, v, where) for a, v in zip(args, value))
    if origin in (dict, cabc.Mapping) or tp is dict:
        _check(isinstance(value, cabc.Mapping), f"{where}: cần object")
        val_t = args[1] if args else Any
        return {str(k): _decode(val_t, v, f"{where}.{k}") for k, v in value.items()}
    if tp is float:
        _check(isinstance(value, (int, float)) and not isinstance(value, bool), f"{where}: cần số")
        return float(value)
    if tp is int:
        _check(isinstance(value, int) and not isinstance(value, bool), f"{where}: cần số nguyên")
        return value
    if tp is bool:
        _check(isinstance(value, bool), f"{where}: cần true/false")
        return value
    if tp is str:
        _check(isinstance(value, str), f"{where}: cần chuỗi")
        return value
    raise TypeError(f"{where}: chưa hỗ trợ kiểu {tp!r}")


def _from_mapping(cls: type, data: cabc.Mapping, where: str = "") -> Any:
    hints = typing.get_type_hints(cls)
    init_fields = [f.name for f in fields(cls) if f.init]
    computed = set(getattr(cls, "_json_computed", ()))
    unknown = set(data) - set(init_fields) - computed
    _check(not unknown, f"{where or cls.__name__}: trường lạ {sorted(unknown)}")
    kwargs = {name: _decode(hints[name], data[name], f"{where or cls.__name__}.{name}")
              for name in init_fields if name in data}
    return cls(**kwargs)


class _Serializable:
    def to_dict(self) -> dict:
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, data: cabc.Mapping):
        return _from_mapping(cls, data)


# ----------------------------------------------------------------------------- địa điểm

@dataclass(frozen=True)
class OpeningRule(_Serializable):
    """Khung giờ mở cửa áp dụng cho các tháng / thứ nhất định (vd giờ hè, giờ đông)."""

    intervals: tuple[tuple[time, time], ...]
    months: frozenset[int] = ALL_MONTHS
    weekdays: frozenset[int] = ALL_WEEKDAYS

    def __post_init__(self) -> None:
        _set(self, "months", frozenset(self.months))
        _set(self, "weekdays", frozenset(self.weekdays))
        _set(self, "intervals", tuple(tuple(iv) for iv in self.intervals))
        _check(bool(self.months) and self.months <= ALL_MONTHS, f"tháng không hợp lệ: {sorted(self.months)}")
        _check(bool(self.weekdays) and self.weekdays <= ALL_WEEKDAYS, f"thứ không hợp lệ: {sorted(self.weekdays)}")
        _check(bool(self.intervals), "OpeningRule cần ít nhất một khoảng giờ")
        prev_close = None
        for open_, close in sorted(self.intervals):
            _check(open_ < close, f"giờ đóng phải sau giờ mở: {fmt_time(open_)}-{fmt_time(close)}")
            _check(prev_close is None or open_ >= prev_close, "các khoảng giờ chồng nhau")
            prev_close = close
        _set(self, "intervals", tuple(sorted(self.intervals)))

    def applies(self, day: date) -> bool:
        return day.month in self.months and day.weekday() in self.weekdays


@dataclass(frozen=True)
class OpeningHours(_Serializable):
    """Giờ mở cửa. Quy tắc đầu tiên khớp ngày được dùng; không quy tắc nào khớp = đóng cửa.

    `always_open` cho nơi công cộng (cầu, công viên). Place không có OpeningHours = chưa biết giờ.
    """

    rules: tuple[OpeningRule, ...] = ()
    always_open: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        _set(self, "rules", tuple(self.rules))
        if self.always_open:
            _check(not self.rules, "always_open không đi kèm quy tắc giờ")
        else:
            _check(bool(self.rules), "OpeningHours cần quy tắc giờ hoặc always_open")

    @classmethod
    def daily(cls, open_: str, close: str, months=ALL_MONTHS, note: str = "") -> "OpeningHours":
        return cls((OpeningRule(((parse_time(open_), parse_time(close)),), frozenset(months)),), note=note)

    def intervals_on(self, day: date) -> tuple[tuple[time, time], ...]:
        if self.always_open:
            return ((time(0, 0), time(23, 59)),)
        for rule in self.rules:
            if rule.applies(day):
                return rule.intervals
        return ()

    def covers(self, day: date, start: time, end: time) -> bool:
        """Cả khoảng [start, end] nằm trong một khoảng mở cửa của ngày đó."""
        return any(o <= start and end <= c for o, c in self.intervals_on(day))


@dataclass(frozen=True)
class Place(_Serializable):
    """Một địa điểm ứng viên (key `wikidata:Q…` / `osm:…`), kèm thuộc tính dùng khi lập lịch."""

    key: str
    name: str
    lat: float
    lon: float
    category: str
    avg_visit_minutes: int
    indoor_ratio: float
    opening_hours: OpeningHours | None = None
    unsafe_conditions: frozenset[str] = frozenset()
    best_time_of_day: frozenset[str] = frozenset()
    ticket_price_vnd: tuple[int, int] | None = None  # (thấp nhất, cao nhất); giá cố định thì bằng nhau
    label_source: str = "default"

    def __post_init__(self) -> None:
        _set(self, "unsafe_conditions", frozenset(self.unsafe_conditions))
        _set(self, "best_time_of_day", frozenset(self.best_time_of_day))
        if self.ticket_price_vnd is not None:
            _set(self, "ticket_price_vnd", tuple(self.ticket_price_vnd))
        _check(is_place_key(self.key), f"place key không hợp lệ: {self.key!r}")
        _check(bool(self.name.strip()), f"{self.key}: tên rỗng")
        _finite(self.lat, "lat"), _finite(self.lon, "lon")
        _check(-90 <= self.lat <= 90 and -180 <= self.lon <= 180, f"{self.key}: tọa độ ngoài khoảng")
        _check(bool(self.category), f"{self.key}: thiếu category")
        _check(isinstance(self.avg_visit_minutes, int) and 5 <= self.avg_visit_minutes <= 600,
               f"{self.key}: avg_visit_minutes phải trong [5, 600]")
        _finite(self.indoor_ratio, "indoor_ratio")
        _check(0 <= self.indoor_ratio <= 1, f"{self.key}: indoor_ratio phải trong [0, 1]")
        _check(self.unsafe_conditions <= WEATHER_CONDITIONS,
               f"{self.key}: điều kiện lạ {sorted(self.unsafe_conditions - WEATHER_CONDITIONS)}")
        _check(self.best_time_of_day <= TIMES_OF_DAY,
               f"{self.key}: khung giờ lạ {sorted(self.best_time_of_day - TIMES_OF_DAY)}")
        if self.ticket_price_vnd is not None:
            lo, hi = self.ticket_price_vnd
            _check(0 <= lo <= hi, f"{self.key}: giá vé phải 0 ≤ thấp ≤ cao")
        _check(self.label_source in LABEL_SOURCES, f"{self.key}: label_source lạ {self.label_source!r}")


@dataclass(frozen=True)
class Location(_Serializable):
    """Điểm xuất phát/kết thúc mỗi ngày (khách sạn)."""

    name: str
    lat: float
    lon: float

    def __post_init__(self) -> None:
        _finite(self.lat, "lat"), _finite(self.lon, "lon")
        _check(-90 <= self.lat <= 90 and -180 <= self.lon <= 180, f"{self.name}: tọa độ ngoài khoảng")


# ----------------------------------------------------------------------------- thời tiết, di chuyển

@dataclass(frozen=True)
class WeatherHour(_Serializable):
    """Thời tiết một giờ tại một ô lưới. `issued_at`: thời điểm phát hành dự báo (None với dữ liệu thực)."""

    time: datetime
    lat: float
    lon: float
    rain_prob: float
    precip_mm: float
    temp_c: float
    wind_kmh: float
    source: str
    conditions: frozenset[str] = frozenset()
    issued_at: datetime | None = None

    def __post_init__(self) -> None:
        _set(self, "conditions", frozenset(self.conditions))
        _check(self.time.tzinfo is not None, "WeatherHour.time thiếu múi giờ")
        for name in ("lat", "lon", "rain_prob", "precip_mm", "temp_c", "wind_kmh"):
            _finite(getattr(self, name), name)
        _check(0 <= self.rain_prob <= 1, f"rain_prob phải trong [0, 1]: {self.rain_prob}")
        _check(self.precip_mm >= 0 and self.wind_kmh >= 0, "lượng mưa / gió không được âm")
        _check(-30 <= self.temp_c <= 60, f"nhiệt độ bất thường: {self.temp_c}")
        _check(self.conditions <= WEATHER_CONDITIONS, f"điều kiện lạ {sorted(self.conditions - WEATHER_CONDITIONS)}")
        _check(self.source in WEATHER_SOURCES, f"nguồn thời tiết lạ {self.source!r}")
        if self.issued_at is not None:
            _check(self.issued_at.tzinfo is not None, "issued_at thiếu múi giờ")
            _check(self.issued_at <= self.time, "dự báo phát hành sau thời điểm được dự báo")

    @property
    def lead_hours(self) -> float | None:
        return None if self.issued_at is None else (self.time - self.issued_at).total_seconds() / 3600


@dataclass(frozen=True)
class TravelMatrix(_Serializable):
    """Thời gian đi (phút) giữa các điểm; `keys` gồm HOTEL_KEY và place key."""

    keys: tuple[str, ...]
    minutes: tuple[tuple[float, ...], ...]
    mode: str
    source: str
    _index: dict = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _set(self, "keys", tuple(self.keys))
        _set(self, "minutes", tuple(tuple(float(x) for x in row) for row in self.minutes))
        n = len(self.keys)
        _check(len(set(self.keys)) == n, "keys trùng nhau")
        _check(len(self.minutes) == n and all(len(r) == n for r in self.minutes), "ma trận phải vuông, khớp keys")
        for i, row in enumerate(self.minutes):
            for j, v in enumerate(row):
                _check(math.isfinite(v) and v >= 0, f"thời gian đi không hợp lệ [{self.keys[i]}→{self.keys[j]}]: {v}")
            _check(row[i] == 0, f"đường chéo phải bằng 0 tại {self.keys[i]}")
        _check(self.mode in TRAVEL_MODES, f"phương tiện lạ {self.mode!r}")
        _check(self.source in MATRIX_SOURCES, f"nguồn ma trận lạ {self.source!r}")
        _set(self, "_index", {k: i for i, k in enumerate(self.keys)})

    def get(self, a: str, b: str) -> float:
        return self.minutes[self._index[a]][self._index[b]]


# ----------------------------------------------------------------------------- bài toán và lịch

@dataclass(frozen=True)
class Preferences(_Serializable):
    """Ràng buộc từ người dùng: điểm bắt buộc / tránh, sở thích, nhịp đi, ngân sách vé."""

    must_visit: frozenset[str] = frozenset()
    avoid: frozenset[str] = frozenset()
    interests: frozenset[str] = frozenset()   # category ưa thích
    pace: str = "vua"
    budget_vnd: int | None = None

    def __post_init__(self) -> None:
        for name in ("must_visit", "avoid", "interests"):
            _set(self, name, frozenset(getattr(self, name)))
        _check(not (self.must_visit & self.avoid), f"vừa bắt buộc vừa tránh: {sorted(self.must_visit & self.avoid)}")
        _check(self.pace in PACES, f"nhịp đi lạ {self.pace!r}")
        _check(self.budget_vnd is None or self.budget_vnd >= 0, "ngân sách âm")


@dataclass(frozen=True)
class Problem(_Serializable):
    """Một bài toán lập lịch: ứng viên, ngày đi, khung giờ, khách sạn, thời gian đi, thời tiết."""

    problem_id: str
    places: tuple[Place, ...]
    start_date: date
    days: int
    day_start: time
    day_end: time
    hotel: Location
    travel: TravelMatrix
    weather: tuple[WeatherHour, ...] = ()
    max_places_per_day: int = 4
    preferences: Preferences = Preferences()
    request_text: str = ""
    _by_key: dict = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _set(self, "places", tuple(self.places))
        _set(self, "weather", tuple(self.weather))
        _check(bool(self.problem_id), "problem_id rỗng")
        _check(bool(self.places), "bài toán không có địa điểm")
        keys = [p.key for p in self.places]
        _check(len(set(keys)) == len(keys), "địa điểm trùng key")
        _check(1 <= self.days <= 14, "days phải trong [1, 14]")
        _check(self.day_start < self.day_end, "day_start phải trước day_end")
        _check(1 <= self.max_places_per_day <= 12, "max_places_per_day phải trong [1, 12]")
        missing = ({HOTEL_KEY} | set(keys)) - set(self.travel.keys)
        _check(not missing, f"ma trận thời gian đi thiếu {sorted(missing)}")
        outside = (self.preferences.must_visit | self.preferences.avoid) - set(keys)
        _check(not outside, f"sở thích nhắc địa điểm ngoài ứng viên: {sorted(outside)}")
        _set(self, "_by_key", {p.key: p for p in self.places})

    def place(self, key: str) -> Place | None:
        return self._by_key.get(key)

    def date_of(self, day: int) -> date:
        return date.fromordinal(self.start_date.toordinal() + day - 1)


@dataclass(frozen=True)
class Visit(_Serializable):
    """Một lượt tham quan trong lịch: ngày thứ mấy, địa điểm, giờ đến, giờ rời."""

    day: int          # 1 = ngày đầu
    place_key: str
    start: time
    end: time

    def __post_init__(self) -> None:
        _check(isinstance(self.day, int) and self.day >= 1, f"day phải ≥ 1: {self.day}")
        _check(bool(self.place_key), "place_key rỗng")
        _check(self.start < self.end, f"{self.place_key}: giờ kết thúc phải sau giờ bắt đầu")

    @property
    def minutes(self) -> int:
        return minutes_of(self.end) - minutes_of(self.start)


@dataclass(frozen=True)
class Schedule(_Serializable):
    """Lịch đề xuất. Không kiểm trùng/chồng giờ ở đây — đó là việc của planner_core.check()."""

    visits: tuple[Visit, ...]
    source: str
    explanation: str = ""

    def __post_init__(self) -> None:
        _set(self, "visits", tuple(self.visits))
        _check(self.source in SCHEDULE_SOURCES, f"nguồn lịch lạ {self.source!r}")

    def by_day(self) -> dict[int, list[Visit]]:
        out: dict[int, list[Visit]] = {}
        for v in self.visits:
            out.setdefault(v.day, []).append(v)
        return {d: sorted(vs, key=lambda v: v.start) for d, vs in sorted(out.items())}


class ViolationKind(str, Enum):
    INVALID_OUTPUT = "invalid_output"          # LLM trả sai định dạng
    UNKNOWN_PLACE = "unknown_place"
    DUPLICATE = "duplicate"
    DAY_OUT_OF_RANGE = "day_out_of_range"
    DAY_WINDOW = "day_window"
    OVERLAP = "overlap"
    TRAVEL = "travel"                          # không kịp di chuyển
    OPENING_HOURS = "opening_hours"
    UNKNOWN_HOURS = "unknown_hours"            # chưa biết giờ mở cửa (cảnh báo tùy chính sách)
    VISIT_TOO_SHORT = "visit_too_short"
    MAX_PLACES = "max_places"
    UNSAFE_WEATHER = "unsafe_weather"
    AVOIDED_PLACE = "avoided_place"
    MUST_VISIT_MISSING = "must_visit_missing"
    OVER_BUDGET = "over_budget"


@dataclass(frozen=True)
class Violation(_Serializable):
    """Một vi phạm tìm thấy khi kiểm lịch; `visit_index` trỏ vào Schedule.visits nếu gắn với một lượt."""

    kind: ViolationKind
    detail: str
    visit_index: int | None = None


@dataclass(frozen=True)
class FeasibilityReport(_Serializable):
    """Kết quả kiểm một lịch. `feasible` suy ra từ việc không có vi phạm (không lưu riêng để khỏi lệch)."""

    violations: tuple[Violation, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)

    _json_computed = ("feasible",)

    def __post_init__(self) -> None:
        _set(self, "violations", tuple(self.violations))
        for k, v in self.metrics.items():
            _finite(v, f"metrics.{k}")

    @property
    def feasible(self) -> bool:
        return not self.violations

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.violations:
            out[v.kind.value] = out.get(v.kind.value, 0) + 1
        return out

    def _json_extra(self) -> dict:
        return {"feasible": self.feasible}


# ----------------------------------------------------------------------------- nhận diện ảnh

@dataclass(frozen=True)
class Recognition(_Serializable):
    """Kết quả nhận diện ảnh. `is_ood` (ảnh lạ) thì không gán địa danh."""

    place_key: str | None
    score: float
    candidates: tuple[tuple[str, float], ...] = ()
    is_ood: bool = False
    used_gps: bool = False

    def __post_init__(self) -> None:
        _set(self, "candidates", tuple(tuple(c) for c in self.candidates))
        _finite(self.score, "score")
        _check(0 <= self.score <= 1, f"score phải trong [0, 1]: {self.score}")
        _check(not (self.is_ood and self.place_key is not None), "ảnh lạ (is_ood) không được gán địa danh")
        _check(self.place_key is None or is_place_key(self.place_key), f"place key không hợp lệ: {self.place_key!r}")
        scores = [s for _, s in self.candidates]
        _check(all(math.isfinite(s) and 0 <= s <= 1 for s in scores), "điểm ứng viên phải trong [0, 1]")
        _check(scores == sorted(scores, reverse=True), "ứng viên phải xếp điểm giảm dần")


# ----------------------------------------------------------------------------- RAG

@dataclass(frozen=True)
class Passage(_Serializable):
    """Một đoạn tư liệu truy hồi được.

    `score` theo thang của retriever sinh ra nó (hybrid: điểm RRF), chỉ dùng để so trong cùng
    một lần truy hồi, không so giữa các retriever khác nhau.
    """

    chunk_id: str
    place_key: str | None
    title: str
    section: str
    text: str
    score: float

    def __post_init__(self) -> None:
        if not self.chunk_id:
            raise ValueError("chunk_id rỗng")
        if not self.text:
            raise ValueError(f"Passage {self.chunk_id} không có nội dung")
        if not math.isfinite(self.score):
            raise ValueError(f"score không hữu hạn: {self.score}")


@dataclass(frozen=True)
class Answer(_Serializable):
    """Câu trả lời của RAG.

    `citations`: các đoạn được câu trả lời trích ([n]), theo thứ tự xuất hiện; rỗng nghĩa là câu
    trả lời không dựa vào tư liệu nào (thường là từ chối). `context`: mọi đoạn đã đưa cho LLM.
    `timings` (ms): retrieval_ms, ttft_ms, total_ms; `output_tokens` nếu server trả usage.
    """

    text: str
    citations: tuple[Passage, ...]
    context: tuple[Passage, ...]
    model: str
    refused: bool
    timings: Mapping[str, float] = field(default_factory=dict)
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        _set(self, "citations", tuple(self.citations))
        _set(self, "context", tuple(self.context))
        context_ids = {p.chunk_id for p in self.context}
        stray = [p.chunk_id for p in self.citations if p.chunk_id not in context_ids]
        if stray:
            raise ValueError(f"trích dẫn ngoài ngữ cảnh: {stray}")
        bad = {k: v for k, v in self.timings.items() if not math.isfinite(v) or v < 0}
        if bad:
            raise ValueError(f"timings không hợp lệ: {bad}")


ALL_TYPES: dict[str, type] = {cls.__name__: cls for cls in (
    OpeningRule, OpeningHours, Place, Location, WeatherHour, TravelMatrix, Preferences, Problem,
    Visit, Schedule, Violation, FeasibilityReport, Recognition, Passage, Answer,
)}
