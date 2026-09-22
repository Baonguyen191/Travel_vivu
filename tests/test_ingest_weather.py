import functools
import json
import shutil
import tempfile
from pathlib import Path

import httpx

from pipeline.config import CityConfig
from pipeline.http import Fetcher
from pipeline.ingest import weather
from pipeline.ingest.weather import grid_points, parse_forecast, parse_normals

CFG = CityConfig("Huế", (16.335, 107.435, 16.605, 107.725), (16.4698, 107.5796), 15.0)

# Bbox thu hẹp còn đúng hai điểm lưới ((16.5, 107.6) và (16.6, 107.6)) để test
# hành vi bỏ-qua-và-tiếp-tục của run() mà không cần gọi mạng thật.
TWO_CELL_CFG = CityConfig("Huế", (16.5, 107.6, 16.6, 107.6), (16.55, 107.6), 5.0)


def _forecast_url(lat: float, lon: float) -> str:
    return str(httpx.URL(
        weather.FORECAST_URL,
        params={"latitude": lat, "longitude": lon, "hourly": weather.HOURLY,
                "forecast_days": 16, "timezone": "Asia/Bangkok"},
    ))


def _archive_url(lat: float, lon: float) -> str:
    return str(httpx.URL(
        weather.ARCHIVE_URL,
        params={"latitude": lat, "longitude": lon, "start_date": "1991-01-01",
                "end_date": "2020-12-31", "daily": weather.DAILY,
                "timezone": "Asia/Bangkok"},
    ))


def test_grid_points_are_rounded_to_step():
    points = grid_points(CFG, step=0.1)
    assert points
    assert all(round(lat * 10) == lat * 10 for lat, _ in points)
    assert all(16.3 <= lat <= 16.7 for lat, _ in points)


def test_parse_forecast_rows():
    payload = {"hourly": {
        "time": ["2026-09-22T00:00", "2026-09-22T01:00"],
        "temperature_2m": [27.0, 26.5],
        "precipitation_probability": [30, 40],
        "precipitation": [0.0, 1.2],
        "wind_speed_10m": [9.0, 11.0],
        "uv_index": [0.0, 0.0],
        "weather_code": [2, 61],
    }}
    rows = parse_forecast(payload)
    assert len(rows) == 2
    assert rows[1]["precip_mm"] == 1.2
    assert rows[1]["weather_code"] == 61


def test_parse_normals_aggregates_by_month():
    payload = {"daily": {
        "time": ["2020-01-01", "2020-01-02", "2020-02-01"],
        "temperature_2m_mean": [20.0, 22.0, 24.0],
        "precipitation_sum": [0.0, 10.0, 5.0],
        "wind_speed_10m_max": [10.0, 12.0, 8.0],
    }}
    rows = {r["month"]: r for r in parse_normals(payload)}
    assert rows[1]["temp_avg"] == 21.0
    assert rows[1]["precip_mm_avg"] == 5.0
    assert rows[1]["rain_days"] == 0.5
    assert rows[2]["wind_avg"] == 8.0


def test_run_skips_grid_cell_on_http_error_and_continues(httpx_mock, monkeypatch, db_conn):
    # Cô lập I/O khỏi cache thật: raw_root riêng, min_interval=0 để test chạy
    # nhanh (Fetcher thật dùng min_interval=1.0 khi ingest thật qua CLI).
    raw_root = tempfile.mkdtemp(prefix="weather_raw_")
    monkeypatch.setattr(
        weather,
        "Fetcher",
        functools.partial(Fetcher, raw_root=raw_root, min_interval=0, user_agent="test-ua"),
    )

    forecast_payload = json.dumps({"hourly": {
        "time": ["2026-09-22T00:00"],
        "temperature_2m": [27.0],
        "precipitation_probability": [30],
        "precipitation": [0.0],
        "wind_speed_10m": [9.0],
        "uv_index": [0.0],
        "weather_code": [2],
    }}).encode("utf-8")
    archive_payload = json.dumps({"daily": {
        "time": ["2020-01-01"],
        "temperature_2m_mean": [21.0],
        "precipitation_sum": [0.0],
        "wind_speed_10m_max": [10.0],
    }}).encode("utf-8")

    httpx_mock.add_response(url=_forecast_url(16.5, 107.6), content=forecast_payload)
    httpx_mock.add_response(url=_archive_url(16.5, 107.6), content=archive_payload)
    httpx_mock.add_response(url=_forecast_url(16.6, 107.6), status_code=503)

    try:
        written = weather.run(db_conn, TWO_CELL_CFG)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert written == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM weather_cache")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM climate_normals")
        assert cur.fetchone()[0] == 1
