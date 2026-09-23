import json
from collections import defaultdict

import httpx

from pipeline.config import CityConfig
from pipeline.http import Fetcher

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = ("temperature_2m,precipitation_probability,precipitation,"
          "wind_speed_10m,uv_index,weather_code")
DAILY = "temperature_2m_mean,precipitation_sum,wind_speed_10m_max"
RAIN_DAY_MM = 1.0
MIN_INTERVAL = 1.0


def _utc_offset_suffix(utc_offset_seconds: int) -> str:
    sign = "+" if utc_offset_seconds >= 0 else "-"
    total_minutes = abs(utc_offset_seconds) // 60
    hours, minutes = divmod(total_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def grid_key(lat: float, lon: float, step: float = 0.1) -> tuple[float, float]:
    """Làm tròn một toạ độ về điểm lưới ~`step` độ — một nơi duy nhất cho quy
    ước làm tròn, dùng cả khi liệt kê lưới (`grid_points`) lẫn khi tra cache
    thời tiết theo toạ độ một địa điểm bất kỳ."""
    return (round(round(lat / step) * step, 1), round(round(lon / step) * step, 1))


def grid_points(cfg: CityConfig, step: float = 0.1) -> list[tuple[float, float]]:
    south, west, north, east = cfg.bbox
    points = []
    lat, _ = grid_key(south, west, step)
    while lat <= north + step / 2:
        _, lon = grid_key(lat, west, step)
        while lon <= east + step / 2:
            points.append((lat, lon))
            lon = round(lon + step, 1)
        lat = round(lat + step, 1)
    return points


def parse_forecast(payload: dict) -> list[dict]:
    h = payload["hourly"]
    suffix = _utc_offset_suffix(payload.get("utc_offset_seconds", 0))
    rows = []
    for i, stamp in enumerate(h["time"]):
        rows.append({
            "forecast_time": f"{stamp}{suffix}",
            "temperature": h["temperature_2m"][i],
            "precip_prob": h["precipitation_probability"][i],
            "precip_mm": h["precipitation"][i],
            "wind_speed": h["wind_speed_10m"][i],
            "uv_index": h["uv_index"][i],
            "weather_code": h["weather_code"][i],
        })
    return rows


def parse_normals(payload: dict) -> list[dict]:
    d = payload["daily"]
    buckets: dict[int, dict[str, list]] = defaultdict(
        lambda: {"temp": [], "precip": [], "wind": []}
    )
    for i, stamp in enumerate(d["time"]):
        month = int(stamp[5:7])
        buckets[month]["temp"].append(d["temperature_2m_mean"][i])
        buckets[month]["precip"].append(d["precipitation_sum"][i])
        buckets[month]["wind"].append(d["wind_speed_10m_max"][i])

    def _avg(values: list) -> float | None:
        present = [v for v in values if v is not None]
        return sum(present) / len(present) if present else None

    rows = []
    for month, values in sorted(buckets.items()):
        precip = values["precip"]
        precip_present = [v for v in precip if v is not None]
        rain_days = (
            sum(1 for v in precip_present if v >= RAIN_DAY_MM) / len(precip_present)
            if precip_present else None
        )
        rows.append({
            "month": month,
            "temp_avg": _avg(values["temp"]),
            "precip_mm_avg": _avg(precip),
            "rain_days": rain_days,
            "wind_avg": _avg(values["wind"]),
        })
    return rows


def run(conn, cfg: CityConfig, force: bool = False) -> int:
    fetcher = Fetcher(conn, "open_meteo", min_interval=MIN_INTERVAL)
    written = 0
    for lat, lon in grid_points(cfg):
        try:
            # Dự báo là dữ liệu "luôn mới" theo bản chất — URL không đổi giữa
            # các lần gọi (không có tham số ngày), nên cache theo hash request
            # của Fetcher sẽ trả lại đúng bản thô cũ mãi mãi nếu không force.
            # force=True vô điều kiện ở đây, độc lập với tham số `force` của
            # run() (tham số đó chỉ còn áp dụng cho archive, dữ liệu bất biến).
            forecast = fetcher.fetch(
                FORECAST_URL,
                params={"latitude": lat, "longitude": lon, "hourly": HOURLY,
                        "forecast_days": 16, "timezone": "Asia/Ho_Chi_Minh"},
                force=True,
            )
        except httpx.HTTPStatusError as exc:
            print(
                f"weather: bỏ qua dự báo ô lưới ({lat}, {lon}) —"
                f" lỗi HTTP {exc.response.status_code}"
            )
        except httpx.TimeoutException:
            print(f"weather: bỏ qua dự báo ô lưới ({lat}, {lon}) — timeout")
        else:
            for row in parse_forecast(json.loads(forecast.content)):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO weather_cache (lat_grid, lon_grid, forecast_time,"
                        " temperature, precip_prob, precip_mm, wind_speed, uv_index,"
                        " weather_code, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())"
                        " ON CONFLICT (lat_grid, lon_grid, forecast_time) DO UPDATE SET"
                        " temperature = EXCLUDED.temperature,"
                        " precip_prob = EXCLUDED.precip_prob,"
                        " precip_mm = EXCLUDED.precip_mm,"
                        " wind_speed = EXCLUDED.wind_speed,"
                        " uv_index = EXCLUDED.uv_index,"
                        " weather_code = EXCLUDED.weather_code, fetched_at = now()",
                        (lat, lon, row["forecast_time"], row["temperature"],
                         row["precip_prob"], row["precip_mm"], row["wind_speed"],
                         row["uv_index"], row["weather_code"]),
                    )
                    written += 1

        try:
            archive = fetcher.fetch(
                ARCHIVE_URL,
                params={"latitude": lat, "longitude": lon, "start_date": "1991-01-01",
                        "end_date": "2020-12-31", "daily": DAILY,
                        "timezone": "Asia/Ho_Chi_Minh"},
                force=force,
            )
        except httpx.HTTPStatusError as exc:
            print(
                f"weather: bỏ qua khí hậu ô lưới ({lat}, {lon}) —"
                f" lỗi HTTP {exc.response.status_code}"
            )
            continue
        except httpx.TimeoutException:
            print(f"weather: bỏ qua khí hậu ô lưới ({lat}, {lon}) — timeout")
            continue

        for row in parse_normals(json.loads(archive.content)):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO climate_normals (lat_grid, lon_grid, month, temp_avg,"
                    " precip_mm_avg, rain_days, wind_avg) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (lat_grid, lon_grid, month) DO UPDATE SET"
                    " temp_avg = EXCLUDED.temp_avg,"
                    " precip_mm_avg = EXCLUDED.precip_mm_avg,"
                    " rain_days = EXCLUDED.rain_days, wind_avg = EXCLUDED.wind_avg",
                    (lat, lon, row["month"], row["temp_avg"], row["precip_mm_avg"],
                     row["rain_days"], row["wind_avg"]),
                )
                written += 1
    return written
