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


def test_parse_forecast_appends_utc_offset_suffix_derived_from_response():
    # Open-Meteo trả "hourly.time" theo giờ địa phương (không có offset) cộng
    # với "utc_offset_seconds" ở cấp cao nhất. Phải tự gắn offset vào chuỗi
    # trước khi đưa vào TIMESTAMPTZ, không được dựa vào timezone của session
    # DB hay hardcode "+07:00" — suy ra hậu tố từ chính utc_offset_seconds để
    # đúng với mọi thành phố/múi giờ.
    payload = {"utc_offset_seconds": 25200, "hourly": {
        "time": ["2026-09-22T00:00", "2026-09-22T01:00"],
        "temperature_2m": [27.0, 26.5],
        "precipitation_probability": [30, 40],
        "precipitation": [0.0, 1.2],
        "wind_speed_10m": [9.0, 11.0],
        "uv_index": [0.0, 0.0],
        "weather_code": [2, 61],
    }}
    rows = parse_forecast(payload)
    assert rows[0]["forecast_time"] == "2026-09-22T00:00+07:00"
    assert rows[1]["forecast_time"] == "2026-09-22T01:00+07:00"


def test_forecast_time_round_trips_local_offset_to_correct_instant(db_conn):
    # Test cấp database: chuỗi có offset explicit phải được Postgres đọc
    # thành đúng thời điểm UTC bất kể timezone của session DB là gì (dev/test
    # DB đều chạy session timezone UTC — SHOW timezone trả 'Etc/UTC').
    payload = {"utc_offset_seconds": 25200, "hourly": {
        "time": ["2026-09-22T00:00"],
        "temperature_2m": [27.0],
        "precipitation_probability": [30],
        "precipitation": [0.0],
        "wind_speed_10m": [9.0],
        "uv_index": [0.0],
        "weather_code": [2],
    }}
    row = parse_forecast(payload)[0]
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO weather_cache (lat_grid, lon_grid, forecast_time,"
            " temperature, precip_prob, precip_mm, wind_speed, uv_index, weather_code)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (16.5, 107.6, row["forecast_time"], row["temperature"], row["precip_prob"],
             row["precip_mm"], row["wind_speed"], row["uv_index"], row["weather_code"]),
        )
        # 2026-09-22T00:00+07:00 và 2026-09-21T17:00:00+00:00 là cùng một
        # thời điểm — so sánh bằng '=' của timestamptz là so sánh instant,
        # không phải so sánh chuỗi hiển thị.
        cur.execute(
            "SELECT forecast_time = '2026-09-21T17:00:00+00:00'::timestamptz"
            " FROM weather_cache WHERE lat_grid = 16.5::real AND lon_grid = 107.6::real"
        )
        assert cur.fetchone()[0] is True


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


def test_parse_normals_skips_null_days_when_aggregating():
    # Open-Meteo có thể trả null cho một ngày thiếu dữ liệu trong 30 năm lưu
    # trữ. parse_normals phải bỏ qua giá trị null khi tính trung bình (chia
    # cho số ngày có dữ liệu thật, không phải tổng số ngày), và một ngày mưa
    # bị null không được tính là ngày mưa.
    payload = {"daily": {
        "time": ["2020-06-01", "2020-06-02", "2020-06-03"],
        "temperature_2m_mean": [None, 22.0, 24.0],
        "precipitation_sum": [5.0, None, 0.5],
        "wind_speed_10m_max": [10.0, 12.0, None],
    }}
    rows = {r["month"]: r for r in parse_normals(payload)}
    row = rows[6]
    assert row["temp_avg"] == 23.0
    assert row["precip_mm_avg"] == 2.75
    assert row["rain_days"] == 0.5
    assert row["wind_avg"] == 11.0


def test_parse_normals_stores_null_when_a_variable_has_no_usable_values():
    payload = {"daily": {
        "time": ["2020-07-01"],
        "temperature_2m_mean": [None],
        "precipitation_sum": [None],
        "wind_speed_10m_max": [None],
    }}
    rows = {r["month"]: r for r in parse_normals(payload)}
    row = rows[7]
    assert row["temp_avg"] is None
    assert row["precip_mm_avg"] is None
    assert row["rain_days"] is None
    assert row["wind_avg"] is None


def test_run_guards_forecast_and_archive_fetch_independently(httpx_mock, monkeypatch, db_conn):
    # Trước fix này, forecast và archive dùng chung một try/except: forecast
    # lỗi thì archive của CHÍNH ô lưới đó cũng bị bỏ qua (dù chưa hề gọi), và
    # archive lỗi thì forecast đã fetch thành công bị vứt đi. Test này dựng
    # tình huống forecast lỗi ở ô (16.6, 107.6) nhưng archive của đúng ô đó
    # vẫn thành công, và xác nhận dữ liệu khí hậu của ô đó vẫn được ghi.
    raw_root = tempfile.mkdtemp(prefix="weather_raw_")
    monkeypatch.setattr(weather, "MIN_INTERVAL", 0)
    monkeypatch.setattr(
        weather,
        "Fetcher",
        functools.partial(Fetcher, raw_root=raw_root, user_agent="test-ua"),
    )

    forecast_payload = json.dumps({"utc_offset_seconds": 25200, "hourly": {
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
    httpx_mock.add_response(url=_archive_url(16.6, 107.6), content=archive_payload)

    try:
        written = weather.run(db_conn, TWO_CELL_CFG)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    # 1 dòng forecast (chỉ ô 16.5) + 2 dòng khí hậu (cả hai ô, kể cả ô có
    # forecast lỗi) = 3.
    assert written == 3
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM weather_cache")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM climate_normals")
        assert cur.fetchone()[0] == 2
