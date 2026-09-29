"""Tests cho bộ lập lịch thích ứng thời tiết. Không gọi mạng."""

from datetime import date, datetime, time, timedelta

import pytest

from planner import rules
from planner.evaluator import weather_outcome
from planner.feasibility import check_feasibility
from planner.llm_baseline import llm_prompt, schedule_from_json
from planner.matrix import TravelMatrix, haversine_distance_km
from planner.models import DayItinerary, HourlyWeather, Place, ScheduledVisit, ScheduleResult, UserConstraint
from planner.solver import SolverOptions, WeatherAwareScheduleOptimizer
from planner.weather import (
    CLIMATE, DAILY, HOURLY, NONE, GridWeather, aggregate_daily, parse_hourly, tier_for_lead, trip_weather,
)

DAY = date(2026, 10, 10)  # thứ bảy
HOTEL = (16.4637, 107.5909)
FAST = SolverOptions(time_limit_s=0.5)
BLIND = SolverOptions(weather_aware=False, time_limit_s=0.5)
OUTDOOR_SENS = {"rain": 0.8, "heat": 0.7, "wind": 0.2}


def place(pid, name, lat, lon, **kw) -> Place:
    kw.setdefault("weather_sensitivity", OUTDOOR_SENS)
    return Place(id=pid, name=name, lat=lat, lon=lon, **kw)


LANG = place("lang", "Lăng Tự Đức", 16.4325, 107.5660, avg_visit_minutes=90, indoor_ratio=0.25,
             unsafe_conditions=["mua_lon"])
CHUA = place("chua", "Chùa Thiên Mụ", 16.4536, 107.5448, avg_visit_minutes=45, indoor_ratio=0.3,
             unsafe_conditions=["mua_lon"])
BAO_TANG = place("bt", "Bảo tàng Cổ vật", 16.4713, 107.5819, avg_visit_minutes=60, indoor_ratio=0.95,
                 weather_sensitivity={"rain": 0.1, "heat": 0.1, "wind": 0.0})
BIEN = place("bien", "Biển Thuận An", 16.5600, 107.6400, avg_visit_minutes=120, indoor_ratio=0.0,
             unsafe_conditions=["bao", "song_lon", "mua_lon"])


def constraint(days=1, **kw) -> UserConstraint:
    return UserConstraint(
        start_datetime=datetime.combine(DAY, time(8)),
        end_datetime=datetime.combine(DAY + timedelta(days=days - 1), time(18)),
        start_location=HOTEL, end_location=HOTEL, **kw)


def hourly(fn, days=1) -> list[HourlyWeather]:
    return [fn(datetime.combine(DAY + timedelta(days=d), time(h))) for d in range(days) for h in range(24)]


def weather(fn, days=1, tier=HOURLY) -> GridWeather:
    return GridWeather.uniform(hourly(fn, days), tier)


DRY = lambda t: HourlyWeather(t)  # noqa: E731


# -- rules -------------------------------------------------------------------


def test_hazards_use_hourly_and_daily_thresholds():
    t = datetime.combine(DAY, time(9))
    assert rules.hazards(HourlyWeather(t, precipitation_mm=8)) == {"mua_lon"}
    assert rules.hazards(HourlyWeather(t, precipitation_mm=3)) == set()
    assert rules.hazards(HourlyWeather(t, wind_speed_kmh=55)) == {"bao", "song_lon"}
    assert rules.hazards(HourlyWeather(t, weather_code=95)) == {"bao"}
    # 30 mm cả ngày là mưa, chưa phải mưa rất to; 60 mm/ngày thì có.
    assert rules.hazards(HourlyWeather(t, precipitation_mm=30, period_hours=24)) == set()
    assert rules.is_raining(HourlyWeather(t, precipitation_mm=30, period_hours=24))
    assert rules.hazards(HourlyWeather(t, precipitation_mm=60, period_hours=24)) == {"mua_lon"}


def test_unsafe_reason_only_for_tagged_conditions():
    storm = HourlyWeather(datetime.combine(DAY, time(9)), precipitation_mm=12, wind_speed_kmh=60)
    assert rules.unsafe_reason(BAO_TANG, storm) is None
    assert rules.unsafe_reason(LANG, storm) == "mưa lớn"
    assert rules.unsafe_reason(BIEN, storm) == "bão/dông, mưa lớn, biển động"


def test_weather_cost_uses_numeric_sensitivity_and_indoor_ratio():
    rain = HourlyWeather(datetime.combine(DAY, time(9)), precipitation_mm=5)
    sensitive = place("a", "a", 0, 0, indoor_ratio=0.0, weather_sensitivity={"rain": 0.8})
    tolerant = place("b", "b", 0, 0, indoor_ratio=0.0, weather_sensitivity={"rain": 0.1})
    indoor = place("c", "c", 0, 0, indoor_ratio=1.0, weather_sensitivity={"rain": 0.8})
    assert rules.weather_cost(sensitive, rain) > rules.weather_cost(tolerant, rain) > 0
    assert rules.weather_cost(indoor, rain) == 0.0
    assert rules.weather_cost(sensitive, HourlyWeather(rain.time)) == 0.0


def test_travel_factor():
    t = datetime.combine(DAY, time(9))
    assert rules.travel_factor(None) == 1.0
    assert rules.travel_factor(HourlyWeather(t)) == 1.0
    assert rules.travel_factor(HourlyWeather(t, precipitation_mm=1)) == 1.2
    assert rules.travel_factor(HourlyWeather(t, precipitation_mm=9)) == 1.4
    assert rules.travel_factor(HourlyWeather(t, wind_speed_kmh=60)) == 1.8


# -- models, matrix ------------------------------------------------------------


def test_opening_windows():
    p = place("x", "x", 0, 0, opening_hours={"sat": [["07:30", "11:00"], ["13:30", "17:00"]]})
    assert p.opening_windows(DAY) == [(450, 660), (810, 1020)]
    assert p.opening_windows(DAY + timedelta(days=1)) == []  # chủ nhật vắng mặt = đóng cửa
    assert place("y", "y", 0, 0).opening_windows(DAY) is None


def test_travel_matrix_estimate():
    m = TravelMatrix([HOTEL, HOTEL, (LANG.lat, LANG.lon)])
    assert m.base_minutes[0][1] == 0
    km = haversine_distance_km(*HOTEL, LANG.lat, LANG.lon)
    assert m.distance_km[0][2] == pytest.approx(km * 1.3)
    assert m.minutes(0, 2, 1.5) > m.minutes(0, 2)


# -- weather -------------------------------------------------------------------


def test_parse_hourly_skips_missing_precipitation_and_reads_suffix():
    payload = {"hourly": {
        "time": ["2025-10-27T08:00", "2025-10-27T09:00"],
        "precipitation_previous_day1": [2.5, None],
        "temperature_2m_previous_day1": [25.0, 25.0],
        "wind_speed_10m_previous_day1": [10.0, 12.0],
        "weather_code_previous_day1": [61, 3],
        "precipitation_probability_previous_day1": [None, None],
    }}
    [rec] = parse_hourly(payload, "_previous_day1")
    assert rec.time == datetime(2025, 10, 27, 8) and rec.precipitation_mm == 2.5
    assert rec.rain_probability is None and rec.weather_code == 61


def test_aggregate_daily():
    records = hourly(lambda t: HourlyWeather(t, precipitation_mm=1.0 if t.hour < 6 else 0.0, wind_speed_kmh=t.hour))
    [daily] = aggregate_daily(records).values()
    assert daily.period_hours == 24 and daily.precipitation_mm == 6.0 and daily.wind_speed_kmh == 23


def test_tier_for_lead():
    assert [tier_for_lead(x) for x in (-1, 0, 3, 4, 10, 11)] == [NONE, HOURLY, HOURLY, DAILY, DAILY, CLIMATE]


def test_grid_weather_nearest_cell_and_tiers():
    w = GridWeather()
    w.add_hourly((16.5, 107.6), hourly(lambda t: HourlyWeather(t, precipitation_mm=2)))
    w.set_tier(DAY, HOURLY)
    at_nine = datetime.combine(DAY, time(9))
    assert w.at(16.43, 107.56, at_nine).precipitation_mm == 2  # ô gần nhất
    w.set_tier(DAY, DAILY)
    assert w.at(16.43, 107.56, at_nine).period_hours == 24
    w.set_tier(DAY, CLIMATE)
    assert w.at(16.43, 107.56, at_nine) is None
    assert w.observed_at(16.43, 107.56, at_nine).precipitation_mm == 2


def _forecast_payload(start: date, days: int) -> dict:
    times = [(datetime.combine(start, time()) + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M")
             for h in range(days * 24)]
    return {"hourly": {"time": times, "precipitation": [0.0] * len(times)}}


def test_trip_weather_assigns_tiers_by_lead():
    today = DAY - timedelta(days=2)
    fetch = lambda url, params, fresh: _forecast_payload(today, 16)  # noqa: E731
    days = [DAY, DAY + timedelta(days=5), DAY + timedelta(days=20)]
    w = trip_weather([HOTEL], days, today, fetch_json=fetch)
    assert [w.tier(d) for d in days] == [HOURLY, DAILY, CLIMATE]
    assert "chưa có dự báo" in w.notes(days[2])[0]


def test_trip_weather_failure_is_never_silent_sunshine():
    import httpx

    def boom(url, params, fresh):
        raise httpx.ConnectError("offline")

    w = trip_weather([HOTEL], [DAY], DAY, fetch_json=boom)
    assert w.tier(DAY) == NONE
    assert "không lấy được dự báo" in w.notes(DAY)[0]


# -- solver --------------------------------------------------------------------


def test_outdoor_places_move_to_dry_hours():
    morning_rain = weather(lambda t: HourlyWeather(t, precipitation_mm=6 if t.hour < 12 else 0))
    places = [LANG, CHUA, BAO_TANG]

    aware = WeatherAwareScheduleOptimizer(FAST).optimize(places, constraint(), morning_rain)
    by_id = {v.place_id: v for v in aware.visits}
    assert set(by_id) == {"lang", "chua", "bt"}
    assert by_id["lang"].arrival_time.hour >= 12 and by_id["chua"].arrival_time.hour >= 12

    blind = WeatherAwareScheduleOptimizer(BLIND).optimize(places, constraint(), morning_rain)
    assert min(v.arrival_time for v in blind.visits).hour < 12


def test_hazard_hours_are_hard_constraints():
    storm = weather(lambda t: HourlyWeather(t, wind_speed_kmh=70 if 8 <= t.hour < 13 else 10))
    result = WeatherAwareScheduleOptimizer(FAST).optimize([BIEN, BAO_TANG], constraint(), storm)
    [beach] = [v for v in result.visits if v.place_id == "bien"]
    assert beach.arrival_time.hour >= 13
    assert any("Biển Thuận An: tránh" in a for a in result.weather_adaptations)


def test_place_dangerous_all_day_is_dropped_with_reason():
    storm = weather(lambda t: HourlyWeather(t, wind_speed_kmh=70))
    result = WeatherAwareScheduleOptimizer(FAST).optimize([BIEN, BAO_TANG], constraint(), storm)
    assert [v.place_id for v in result.visits] == ["bt"]
    assert result.unvisited["bien"] == "thời tiết nguy hiểm ở mọi khung giờ có thể đi"


def test_multi_day_moves_outdoor_place_to_dry_day():
    day1_storm = weather(lambda t: HourlyWeather(t, precipitation_mm=9 if t.date() == DAY else 0), days=2)
    result = WeatherAwareScheduleOptimizer(FAST).optimize([LANG, BAO_TANG], constraint(days=2), day1_storm)
    [lang] = [v for v in result.visits if v.place_id == "lang"]
    assert lang.arrival_time.date() == DAY + timedelta(days=1)


def test_daily_tier_picks_the_drier_day():
    wet_first = weather(lambda t: HourlyWeather(t, precipitation_mm=1.5 if t.date() == DAY else 0), days=2,
                        tier=DAILY)
    result = WeatherAwareScheduleOptimizer(FAST).optimize([LANG], constraint(days=2, max_places_per_day=1),
                                                          wet_first)
    assert result.visits[0].arrival_time.date() == DAY + timedelta(days=1)
    assert result.itineraries[0].weather_tier == DAILY


def test_opening_hours_and_closed_days():
    afternoon = place("pm", "Chỉ mở chiều", 16.4713, 107.5819, avg_visit_minutes=60, indoor_ratio=0.9,
                      opening_hours={"sat": [["14:00", "16:00"]]})
    closes_early = place("early", "Đóng sớm", 16.4690, 107.5780, avg_visit_minutes=120, indoor_ratio=0.9,
                         opening_hours={"sat": [["07:30", "11:00"]]})
    sunday_only = place("sun", "Chỉ mở chủ nhật", 16.46, 107.58, opening_hours={"sun": [["08:00", "17:00"]]})
    result = WeatherAwareScheduleOptimizer(FAST).optimize([afternoon, closes_early, sunday_only], constraint(),
                                                          weather(DRY))
    by_id = {v.place_id: v for v in result.visits}
    assert by_id["pm"].arrival_time.time() >= time(14) and by_id["pm"].departure_time.time() <= time(16)
    assert by_id["early"].departure_time.time() <= time(11)
    assert result.unvisited["sun"].startswith("đóng cửa")


def test_max_places_enforced_inside_solver():
    many = [place(f"p{i}", f"Điểm {i}", 16.46 + i * 0.003, 107.58, avg_visit_minutes=30, indoor_ratio=0.5)
            for i in range(6)]
    result = WeatherAwareScheduleOptimizer(FAST).optimize(many, constraint(max_places_per_day=2), weather(DRY))
    assert len(result.visits) == 2
    assert len(result.unvisited) == 4


def test_budget_constraint():
    cheap = place("cheap", "Rẻ", 16.465, 107.585, ticket_price_vnd=50_000)
    pricey = place("pricey", "Đắt", 16.466, 107.586, ticket_price_vnd=200_000)
    result = WeatherAwareScheduleOptimizer(FAST).optimize([cheap, pricey], constraint(budget_vnd=100_000),
                                                          weather(DRY))
    assert [v.place_id for v in result.visits] == ["cheap"]


def test_missing_weather_is_reported_not_assumed_sunny():
    result = WeatherAwareScheduleOptimizer(FAST).optimize([LANG], constraint(), None)
    day = result.itineraries[0]
    assert day.weather_tier == NONE
    assert "không có dữ liệu thời tiết" in day.notes[0]


def test_solver_output_passes_feasibility_and_has_navigation_links():
    places = [LANG, CHUA, BAO_TANG, BIEN]
    c = constraint()
    result = WeatherAwareScheduleOptimizer(FAST).optimize(
        places, c, weather(lambda t: HourlyWeather(t, precipitation_mm=3 if t.hour in (9, 10) else 0)))
    report = check_feasibility(result, {p.id: p for p in places}, c)
    assert report.feasible, report.violations
    day = result.itineraries[0]
    assert day.navigation_url.startswith("https://www.google.com/maps/dir/?api=1")
    assert all(v.google_maps_url for v in day.visits)


# -- feasibility, evaluator, LLM baseline ---------------------------------------


def _visit(p: Place, start: str, end: str, day: date = DAY) -> ScheduledVisit:
    return ScheduledVisit(p.id, p.name, datetime.combine(day, time.fromisoformat(start)),
                          datetime.combine(day, time.fromisoformat(end)), 0, 0.0, p.indoor_ratio)


def test_feasibility_catches_each_violation_kind():
    museum = place("m", "Bảo tàng", 16.4713, 107.5819, avg_visit_minutes=60, ticket_price_vnd=80_000,
                   opening_hours={"sat": [["08:00", "11:00"]]})
    far = place("f", "Xa", 16.3878, 107.5678, avg_visit_minutes=60)
    places = {p.id: p for p in (museum, far)}
    bad = ScheduleResult(itineraries=[DayItinerary(DAY, [
        _visit(museum, "10:30", "11:30"),  # quá giờ đóng cửa
        _visit(far, "11:35", "11:50"),  # không kịp đi, ở quá ngắn
        _visit(museum, "17:30", "18:30"),  # lặp lại, về muộn, quá 2 điểm
    ])])
    report = check_feasibility(bad, places, constraint(max_places_per_day=2, budget_vnd=100_000))
    kinds = {v.kind for v in report.violations}
    assert {"opening_hours", "travel", "visit_too_short", "duplicate", "day_window", "max_places",
            "budget"} <= kinds
    assert not report.feasible


def test_feasibility_merges_itineraries_of_same_date():
    # LLM có khi tách một ngày thành hai mục cùng ngày; phải kiểm như một ngày.
    split = ScheduleResult(itineraries=[
        DayItinerary(DAY, [_visit(LANG, "08:30", "10:00")]),
        DayItinerary(DAY, [_visit(CHUA, "10:05", "11:00"), _visit(BAO_TANG, "11:30", "12:30")]),
    ])
    report = check_feasibility(split, {p.id: p for p in (LANG, CHUA, BAO_TANG)}, constraint(max_places_per_day=2))
    assert report.count("max_places") == 1
    assert report.count("travel") == 1  # lăng → chùa không kịp 5 phút


def test_feasibility_counts_unverified_hours():
    result = ScheduleResult(itineraries=[DayItinerary(DAY, [_visit(LANG, "08:30", "10:00")])])
    report = check_feasibility(result, {LANG.id: LANG}, constraint())
    assert report.feasible and report.unverified_hours == 1


def test_weather_outcome_on_observed_weather():
    observed = weather(lambda t: HourlyWeather(t, precipitation_mm=9 if t.hour == 9 else 0))
    result = ScheduleResult(itineraries=[DayItinerary(DAY, [
        _visit(LANG, "08:30", "10:00"),  # 1 giờ mưa lớn × 0.75 ngoài trời
        _visit(BAO_TANG, "10:30", "11:30"),
    ])])
    out = weather_outcome(result, {p.id: p for p in (LANG, BAO_TANG)}, observed)
    assert out.visits == 2
    assert out.outdoor_rain_hours == pytest.approx(0.75)
    assert out.hazard_visits == 1 and "Lăng Tự Đức" in out.hazard_details[0]


def test_llm_prompt_and_json_round_trip():
    places = {p.id: p for p in (LANG, BAO_TANG)}
    prompt = llm_prompt(list(places.values()), constraint(), weather(DRY))
    assert "place_id=lang" in prompt and '"days"' in prompt
    result = schedule_from_json(
        '{"days": [{"date": "2026-10-10", "visits": [{"place_id": "lang", "arrival": "08:30",'
        ' "departure": "10:00"}, {"place_id": "ghost", "arrival": "11:00", "departure": "12:00"}]}]}', places)
    assert [v.place_id for v in result.visits] == ["lang", "ghost"]
    report = check_feasibility(result, places, constraint())
    assert report.count("unknown_place") == 1
