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


def grid_points(cfg: CityConfig, step: float = 0.1) -> list[tuple[float, float]]:
    south, west, north, east = cfg.bbox
    points = []
    lat = round(round(south / step) * step, 1)
    while lat <= north + step / 2:
        lon = round(round(west / step) * step, 1)
        while lon <= east + step / 2:
            points.append((lat, lon))
            lon = round(lon + step, 1)
        lat = round(lat + step, 1)
    return points


def parse_forecast(payload: dict) -> list[dict]:
    h = payload["hourly"]
    rows = []
    for i, stamp in enumerate(h["time"]):
        rows.append({
            "forecast_time": stamp,
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

    rows = []
    for month, values in sorted(buckets.items()):
        precip = values["precip"]
        rows.append({
            "month": month,
            "temp_avg": sum(values["temp"]) / len(values["temp"]),
            "precip_mm_avg": sum(precip) / len(precip),
            "rain_days": sum(1 for v in precip if v >= RAIN_DAY_MM) / len(precip),
            "wind_avg": sum(values["wind"]) / len(values["wind"]),
        })
    return rows


def run(conn, cfg: CityConfig, force: bool = False) -> int:
    fetcher = Fetcher(conn, "open_meteo", min_interval=1.0)
    written = 0
    for lat, lon in grid_points(cfg):
        try:
            forecast = fetcher.fetch(
                FORECAST_URL,
                params={"latitude": lat, "longitude": lon, "hourly": HOURLY,
                        "forecast_days": 16, "timezone": "Asia/Bangkok"},
                force=force,
            )
            archive = fetcher.fetch(
                ARCHIVE_URL,
                params={"latitude": lat, "longitude": lon, "start_date": "1991-01-01",
                        "end_date": "2020-12-31", "daily": DAILY, "timezone": "Asia/Bangkok"},
                force=force,
            )
        except httpx.HTTPStatusError as exc:
            print(
                f"weather: bỏ qua ô lưới ({lat}, {lon}) —"
                f" lỗi HTTP {exc.response.status_code}"
            )
            continue
        except httpx.TimeoutException:
            print(f"weather: bỏ qua ô lưới ({lat}, {lon}) — timeout")
            continue

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
