"""Nguồn thời tiết cho bộ lập lịch, theo ô lưới 0.1 độ.

Độ chi tiết phụ thuộc khoảng cách từ hôm nay tới ngày đi (CLAUDE.md):

- hourly (0–3 ngày): dự báo theo giờ, xếp lịch theo khung giờ.
- daily (4–10 ngày): gộp dự báo giờ thành một bản ghi cả ngày; chỉ đủ để chọn
  ngày nào đi điểm ngoài trời, không đủ tin để chọn giờ.
- climate (> 10 ngày): chỉ có khí hậu nhiều năm (`climate_normals`); không ràng
  buộc gì, chỉ ghi cảnh báo xu hướng.
- none: không có dữ liệu. Bộ lập lịch KHÔNG coi là trời đẹp mà ghi rõ lịch
  chưa được tối ưu theo thời tiết.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

import httpx

from pipeline.ingest.weather import grid_key
from planner.models import HourlyWeather

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
TIMEZONE = "Asia/Ho_Chi_Minh"
LOCAL_TZ = timezone(timedelta(hours=7))
HOURLY_VARS = ("temperature_2m", "precipitation", "precipitation_probability",
               "wind_speed_10m", "weather_code")

HOURLY, DAILY, CLIMATE, NONE = "hourly", "daily", "climate", "none"
HOURLY_MAX_LEAD = 3
DAILY_MAX_LEAD = 10
FORECAST_DAYS = 16  # giới hạn của Open-Meteo
CACHE_MAX_AGE = timedelta(hours=6)

# (url, params, fresh) -> payload JSON. `fresh=True` cho dự báo (URL không đổi
# giữa các lần gọi nhưng dữ liệu đổi), False cho dữ liệu lưu trữ bất biến.
FetchJson = Callable[[str, dict, bool], dict]
Grid = tuple[float, float]


def tier_for_lead(lead_days: int) -> str:
    if lead_days < 0:
        return NONE
    if lead_days <= HOURLY_MAX_LEAD:
        return HOURLY
    if lead_days <= DAILY_MAX_LEAD:
        return DAILY
    return CLIMATE


@dataclass
class ClimateNormal:
    month: int
    precip_mm_per_day: float | None
    rain_day_fraction: float | None
    temp_avg: float | None


def aggregate_daily(records: Iterable[HourlyWeather]) -> dict[date, HourlyWeather]:
    """Gộp bản ghi theo giờ thành một bản ghi mỗi ngày (period_hours=24)."""
    by_day: dict[date, list[HourlyWeather]] = defaultdict(list)
    for r in records:
        by_day[r.time.date()].append(r)
    daily = {}
    for day, rs in by_day.items():
        probs = [r.rain_probability for r in rs if r.rain_probability is not None]
        codes = [r.weather_code for r in rs if r.weather_code is not None]
        daily[day] = HourlyWeather(
            time=datetime.combine(day, datetime.min.time()),
            precipitation_mm=sum(r.precipitation_mm for r in rs),
            rain_probability=max(probs) if probs else None,
            temperature_c=max(r.temperature_c for r in rs),
            wind_speed_kmh=max(r.wind_speed_kmh for r in rs),
            weather_code=max(codes) if codes else None,  # mã WMO lớn hơn = hiện tượng nặng hơn
            period_hours=24,
        )
    return daily


class GridWeather:
    """Thời tiết theo ô lưới, kèm tầng độ chi tiết cho từng ngày.

    Toạ độ không có ô lưới riêng dùng ô gần nhất có dữ liệu.
    """

    def __init__(self):
        self._hourly: dict[Grid, dict[datetime, HourlyWeather]] = defaultdict(dict)
        self._daily: dict[Grid, dict[date, HourlyWeather]] = {}
        self._climate: dict[Grid, dict[int, ClimateNormal]] = defaultdict(dict)
        self._tiers: dict[date, str] = {}
        self._notes: dict[date, list[str]] = defaultdict(list)

    @classmethod
    def uniform(cls, records: Iterable[HourlyWeather], tier: str = HOURLY,
                days: Iterable[date] | None = None) -> "GridWeather":
        """Một chuỗi thời tiết áp cho mọi toạ độ (test, thực nghiệm một ô lưới)."""
        records = list(records)
        w = cls()
        w.add_hourly((0.0, 0.0), records)
        for day in days if days is not None else {r.time.date() for r in records}:
            w.set_tier(day, tier)
        return w

    def add_hourly(self, grid: Grid, records: Iterable[HourlyWeather]) -> None:
        for r in records:
            self._hourly[grid][r.time.replace(minute=0, second=0, microsecond=0)] = r
        self._daily.pop(grid, None)

    def add_climate(self, grid: Grid, normals: Iterable[ClimateNormal]) -> None:
        for n in normals:
            self._climate[grid][n.month] = n

    def set_tier(self, day: date, tier: str, note: str | None = None) -> None:
        self._tiers[day] = tier
        if note:
            self._notes[day].append(note)

    def tier(self, day: date) -> str:
        return self._tiers.get(day, NONE)

    def notes(self, day: date) -> list[str]:
        return list(self._notes[day])

    def _nearest(self, table: dict, lat: float, lon: float):
        if not table:
            return None
        key = grid_key(lat, lon)
        if key in table:
            return table[key]
        best = min(table, key=lambda g: (g[0] - lat) ** 2 + (g[1] - lon) ** 2)
        return table[best]

    def at(self, lat: float, lon: float, when: datetime) -> HourlyWeather | None:
        """Thời tiết dùng để ra quyết định tại `when`; None ở tầng climate/none."""
        tier = self.tier(when.date())
        if tier == HOURLY:
            hours = self._nearest(self._hourly, lat, lon)
            return hours.get(when.replace(minute=0, second=0, microsecond=0)) if hours else None
        if tier == DAILY:
            hours_by_grid = self._hourly
            if not hours_by_grid:
                return None
            key = grid_key(lat, lon)
            if key not in hours_by_grid:
                key = min(hours_by_grid, key=lambda g: (g[0] - lat) ** 2 + (g[1] - lon) ** 2)
            if key not in self._daily:
                self._daily[key] = aggregate_daily(hours_by_grid[key].values())
            return self._daily[key].get(when.date())
        return None

    def observed_at(self, lat: float, lon: float, when: datetime) -> HourlyWeather | None:
        """Bản ghi theo giờ bất kể tầng; dùng cho thời tiết quan trắc khi đánh giá."""
        hours = self._nearest(self._hourly, lat, lon)
        return hours.get(when.replace(minute=0, second=0, microsecond=0)) if hours else None

    def climate(self, lat: float, lon: float, month: int) -> ClimateNormal | None:
        table = self._nearest(self._climate, lat, lon)
        return table.get(month) if table else None


# ---------------------------------------------------------------------------
# Đọc payload Open-Meteo
# ---------------------------------------------------------------------------


def _num(values: list | None, i: int, default=None):
    if values is None or i >= len(values) or values[i] is None:
        return default
    return values[i]


def parse_hourly(payload: dict, suffix: str = "") -> list[HourlyWeather]:
    """Bản ghi theo giờ từ payload Open-Meteo. `suffix` như "_previous_day2".

    Giờ trả về là giờ địa phương không kèm múi giờ (request dùng TIMEZONE).
    Giờ thiếu lượng mưa bị bỏ qua thay vì coi là khô.
    """
    h = payload.get("hourly") or {}
    get = lambda name: h.get(f"{name}{suffix}")  # noqa: E731
    records = []
    for i, stamp in enumerate(h.get("time", [])):
        precip = _num(get("precipitation"), i)
        if precip is None:
            continue
        code = _num(get("weather_code"), i)
        records.append(HourlyWeather(
            time=datetime.fromisoformat(stamp),
            precipitation_mm=float(precip),
            rain_probability=_num(get("precipitation_probability"), i),
            temperature_c=float(_num(get("temperature_2m"), i, 27.0)),
            wind_speed_kmh=float(_num(get("wind_speed_10m"), i, 0.0)),
            weather_code=int(code) if code is not None else None,
        ))
    return records


def default_fetch_json(conn=None) -> FetchJson:
    """Gọi Open-Meteo; có `conn` và CONTACT_EMAIL thì qua Fetcher của pipeline
    (lưu bản thô vào data/raw, giới hạn tốc độ, cache dữ liệu lưu trữ)."""
    if conn is not None:
        try:
            from pipeline.http import Fetcher

            fetcher = Fetcher(conn, "open_meteo_planner", min_interval=1.0)
        except RuntimeError:
            fetcher = None
        if fetcher is not None:
            return lambda url, params, fresh: json.loads(
                fetcher.fetch(url, params=params, force=fresh).content)

    client = httpx.Client(timeout=60.0)

    def fetch(url: str, params: dict, _fresh: bool) -> dict:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    return fetch


def fetch_forecast(fetch_json: FetchJson, grid: Grid) -> list[HourlyWeather]:
    payload = fetch_json(FORECAST_URL, {
        "latitude": grid[0], "longitude": grid[1], "hourly": ",".join(HOURLY_VARS),
        "forecast_days": FORECAST_DAYS, "timezone": TIMEZONE,
    }, True)
    return parse_hourly(payload)


def fetch_previous_runs(fetch_json: FetchJson, grid: Grid, start: date, end: date,
                        lead_days: int) -> list[HourlyWeather]:
    """Dự báo đã phát hành `lead_days` ngày trước mỗi giờ (Open-Meteo Previous Runs).

    Đây là "dự báo người dùng thật sự nhìn thấy" khi lập lịch trước chuyến đi.
    API không lưu xác suất mưa cho các lượt chạy cũ nên trường đó là None.
    """
    suffix = f"_previous_day{lead_days}"
    payload = fetch_json(PREVIOUS_RUNS_URL, {
        "latitude": grid[0], "longitude": grid[1],
        "hourly": ",".join(f"{v}{suffix}" for v in HOURLY_VARS),
        "start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": TIMEZONE,
    }, False)
    return parse_hourly(payload, suffix)


def fetch_observed(fetch_json: FetchJson, grid: Grid, start: date, end: date) -> list[HourlyWeather]:
    """Thời tiết đã xảy ra theo tái phân tích ERA5.

    Chỉ định `models=era5`: model mặc định của archive API trộn cả dự báo cho
    những ngày gần, khi đó "quan trắc" trùng hệt dự báo và phép đánh giá bị
    vòng lặp.
    """
    payload = fetch_json(ARCHIVE_URL, {
        "latitude": grid[0], "longitude": grid[1],
        "hourly": "temperature_2m,precipitation,wind_speed_10m,weather_code",
        "models": "era5", "start_date": start.isoformat(), "end_date": end.isoformat(),
        "timezone": TIMEZONE,
    }, False)
    return parse_hourly(payload)


# ---------------------------------------------------------------------------
# Đọc DB
# ---------------------------------------------------------------------------


def load_cached_forecast(conn, grid: Grid, now: datetime,
                         max_age: timedelta = CACHE_MAX_AGE) -> list[HourlyWeather] | None:
    """Dự báo theo giờ trong `weather_cache` nếu đủ mới, None nếu cũ hoặc thiếu."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT forecast_time, temperature, precip_prob, precip_mm, wind_speed, weather_code,"
            " fetched_at FROM weather_cache WHERE lat_grid = %s AND lon_grid = %s"
            " AND forecast_time >= %s ORDER BY forecast_time",
            (grid[0], grid[1], now.replace(tzinfo=LOCAL_TZ) - timedelta(hours=1)),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    oldest_fetch = min(r[6] for r in rows)
    if now.replace(tzinfo=LOCAL_TZ) - oldest_fetch > max_age:
        return None
    return [
        HourlyWeather(
            time=t.astimezone(LOCAL_TZ).replace(tzinfo=None),
            temperature_c=temp if temp is not None else 27.0,
            rain_probability=prob,
            precipitation_mm=mm if mm is not None else 0.0,
            wind_speed_kmh=wind if wind is not None else 0.0,
            weather_code=code,
        )
        for t, temp, prob, mm, wind, code, _ in rows
        if mm is not None
    ]


def load_climate(conn, grid: Grid) -> list[ClimateNormal]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT month, precip_mm_avg, rain_days, temp_avg FROM climate_normals"
            " WHERE lat_grid = %s AND lon_grid = %s",
            grid,
        )
        return [ClimateNormal(*row) for row in cur.fetchall()]


def climate_note(day: date, normal: ClimateNormal | None, lead_days: int) -> str:
    head = f"{day:%d/%m}: cách hôm nay {lead_days} ngày, chưa có dự báo đủ tin cậy."
    if normal is None or normal.precip_mm_per_day is None:
        return head + " Không có dữ liệu khí hậu; lịch chưa tính thời tiết."
    note = (f"{head} Khí hậu tháng {normal.month}: mưa trung bình"
            f" {normal.precip_mm_per_day:.1f} mm/ngày")
    if normal.rain_day_fraction is not None:
        note += f", {normal.rain_day_fraction:.0%} số ngày có mưa"
    note += "."
    if (normal.rain_day_fraction or 0) >= 0.5:
        note += " Nên chuẩn bị phương án trong nhà và xem lại lịch khi gần ngày đi."
    return note


def trip_weather(locations: Iterable[tuple[float, float]], days: Iterable[date], today: date,
                 conn=None, fetch_json: FetchJson | None = None,
                 now: datetime | None = None) -> GridWeather:
    """Dựng thời tiết cho một chuyến đi, chọn tầng theo khoảng cách tới từng ngày.

    Dự báo lấy từ `weather_cache` nếu còn mới, không thì gọi Open-Meteo. Gọi
    thất bại thì các ngày liên quan chuyển tầng none kèm ghi chú, không bao
    giờ thay bằng thời tiết giả định.
    """
    now = now or datetime.combine(today, datetime.min.time())
    fetch_json = fetch_json or default_fetch_json(conn)
    grids = sorted({grid_key(lat, lon) for lat, lon in locations})
    days = sorted(set(days))
    weather = GridWeather()
    leads = {day: (day - today).days for day in days}
    need_forecast = any(tier_for_lead(lead) in (HOURLY, DAILY) and lead < FORECAST_DAYS
                        for lead in leads.values())

    failed: list[str] = []
    if need_forecast:
        for grid in grids:
            records = load_cached_forecast(conn, grid, now) if conn is not None else None
            if records is None:
                try:
                    records = fetch_forecast(fetch_json, grid)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    failed.append(f"{grid}: {exc}")
                    continue
            weather.add_hourly(grid, records)

    for grid in grids:
        if conn is not None:
            weather.add_climate(grid, load_climate(conn, grid))

    center = (sum(g[0] for g in grids) / len(grids), sum(g[1] for g in grids) / len(grids))
    for day, lead in leads.items():
        tier = tier_for_lead(lead)
        if tier in (HOURLY, DAILY):
            has_data = any(weather.observed_at(g[0], g[1], datetime.combine(day, datetime.min.time())
                                                + timedelta(hours=12)) for g in grids)
            if has_data:
                note = (f"{day:%d/%m}: một số ô lưới không lấy được dự báo, dùng ô gần nhất."
                        if failed else None)
                weather.set_tier(day, tier, note)
                continue
            reason = "; ".join(failed) if failed else "dự báo không phủ tới ngày này"
            weather.set_tier(day, NONE, f"{day:%d/%m}: không lấy được dự báo ({reason});"
                                        " lịch ngày này chưa tính thời tiết.")
        elif tier == CLIMATE:
            weather.set_tier(day, CLIMATE, climate_note(day, weather.climate(*center, day.month), lead))
        else:
            weather.set_tier(day, NONE, f"{day:%d/%m} đã qua; không có dự báo.")
    return weather


def hours_between(start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """Các mốc giờ phủ [start, end) và số giờ trùng với mỗi mốc."""
    out = []
    t = start.replace(minute=0, second=0, microsecond=0)
    while t < end:
        overlap = (min(end, t + timedelta(hours=1)) - max(start, t)).total_seconds() / 3600
        if overlap > 0:
            out.append((t, overlap))
        t += timedelta(hours=1)
    return out
