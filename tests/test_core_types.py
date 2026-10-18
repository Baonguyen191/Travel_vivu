import json
import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from core import fixtures as fx
from core.config import VN_TZ
from core.types import (
    ALL_TYPES, Answer, FeasibilityReport, HOTEL_KEY, OpeningHours, OpeningRule, Passage, Place,
    Preferences, Recognition, Schedule, TravelMatrix, Violation, ViolationKind, Visit, WeatherHour,
    parse_time,
)


def _roundtrip(obj):
    return type(obj).from_dict(json.loads(json.dumps(obj.to_dict(), ensure_ascii=False)))


# --------------------------------------------------------------------------- giờ mở cửa

def test_seasonal_opening_hours_pick_first_matching_rule():
    hours = fx.DAI_NOI.opening_hours
    assert hours.intervals_on(date(2025, 7, 1)) == ((time(6, 30), time(17, 30)),)
    assert hours.intervals_on(date(2025, 12, 1)) == ((time(7, 0), time(17, 0)),)
    assert hours.covers(date(2025, 7, 1), time(6, 30), time(8, 0))
    assert not hours.covers(date(2025, 12, 1), time(6, 30), time(8, 0))
    assert not hours.covers(date(2025, 12, 1), time(16, 30), time(17, 1))


def test_weekday_rule_and_closed_days():
    hours = OpeningHours((OpeningRule(((time(8, 0), time(11, 0)), (time(13, 0), time(17, 0))), weekdays=range(0, 5)),))
    monday, sunday = date(2025, 10, 13), date(2025, 10, 19)
    assert hours.covers(monday, time(13, 0), time(14, 0))
    assert not hours.covers(monday, time(10, 30), time(13, 30))  # vắt qua giờ nghỉ trưa
    assert hours.intervals_on(sunday) == ()
    assert OpeningHours(always_open=True).covers(sunday, time(22, 0), time(23, 0))


@pytest.mark.parametrize("make", [
    lambda: OpeningRule(((time(10, 0), time(9, 0)),)),
    lambda: OpeningRule(((time(8, 0), time(12, 0)), (time(11, 0), time(13, 0)))),
    lambda: OpeningRule(((time(8, 0), time(9, 0)),), months={13}),
    lambda: OpeningRule(()),
    lambda: OpeningHours(),
    lambda: OpeningHours(rules=(OpeningRule(((time(8, 0), time(9, 0)),)),), always_open=True),
])
def test_invalid_opening_hours(make):
    with pytest.raises(ValueError):
        make()


def test_parse_time_is_strict():
    assert parse_time("06:30") == time(6, 30)
    for bad in ("6:30", "24:00", "07:60", "0730", 730):
        with pytest.raises(ValueError):
            parse_time(bad)


# --------------------------------------------------------------------------- địa điểm

@pytest.mark.parametrize("change", [
    {"key": "Q123"}, {"key": "osm:point/1"}, {"name": "  "}, {"lat": 91.0}, {"lon": math.nan},
    {"avg_visit_minutes": 0}, {"avg_visit_minutes": 30.5}, {"indoor_ratio": 1.2},
    {"unsafe_conditions": {"mua_phun"}}, {"best_time_of_day": {"nua_dem"}},
    {"ticket_price_vnd": (50_000, 10_000)}, {"label_source": "doan"},
])
def test_invalid_place(change):
    with pytest.raises(ValueError):
        replace(fx.DAI_NOI, **change)


def test_place_coerces_iterables_and_accepts_osm_keys():
    p = replace(fx.THIEN_MU, key="osm:way/123", unsafe_conditions=["bao"], ticket_price_vnd=[0, 50_000])
    assert p.unsafe_conditions == frozenset({"bao"}) and p.ticket_price_vnd == (0, 50_000)


# --------------------------------------------------------------------------- thời tiết, ma trận

def test_weather_hour_validation_and_lead_time():
    h = fx.weather_day(date(2025, 10, 15))[0]
    assert h.lead_hours == 12.0
    with pytest.raises(ValueError):
        replace(h, time=datetime(2025, 10, 15, 6))                 # thiếu múi giờ
    with pytest.raises(ValueError):
        replace(h, rain_prob=1.5)
    with pytest.raises(ValueError):
        replace(h, issued_at=h.time + timedelta(hours=1))           # phát hành sau giờ dự báo
    with pytest.raises(ValueError):
        replace(h, conditions={"tuyet"})
    with pytest.raises(ValueError):
        replace(h, source="radio")


def test_travel_matrix_validation_and_lookup():
    m = fx.travel_matrix()
    assert m.get(HOTEL_KEY, HOTEL_KEY) == 0 and m.get(fx.DAI_NOI.key, fx.TU_DUC.key) > 0
    with pytest.raises(ValueError):
        TravelMatrix(("a", "b"), ((0, 1),), "car", "stub")
    with pytest.raises(ValueError):
        TravelMatrix(("a", "b"), ((1, 1), (1, 0)), "car", "stub")
    with pytest.raises(ValueError):
        TravelMatrix(("a", "b"), ((0, -1), (1, 0)), "car", "stub")
    with pytest.raises(ValueError):
        TravelMatrix(("a", "a"), ((0, 1), (1, 0)), "car", "stub")
    with pytest.raises(ValueError):
        TravelMatrix(("a", "b"), ((0, 1), (1, 0)), "rocket", "stub")


# --------------------------------------------------------------------------- bài toán, lịch

def test_problem_validation():
    p = fx.sample_problem(days=2)
    assert p.place(fx.TU_DUC.key) is fx.TU_DUC and p.place("wikidata:Q1") is None
    assert p.date_of(2) == date(2025, 10, 16)
    with pytest.raises(ValueError, match="thiếu"):
        replace(p, travel=fx.travel_matrix(places=fx.PLACES[:2]))
    with pytest.raises(ValueError, match="trùng"):
        replace(p, places=fx.PLACES + (fx.DAI_NOI,))
    with pytest.raises(ValueError, match="ngoài ứng viên"):
        replace(p, preferences=Preferences(avoid={"wikidata:Q1"}))
    with pytest.raises(ValueError):
        replace(p, day_start=time(18, 0))
    with pytest.raises(ValueError):
        Preferences(must_visit={fx.DAI_NOI.key}, avoid={fx.DAI_NOI.key})


def test_visit_and_schedule():
    s = fx.sample_schedule()
    assert [v.place_key for v in s.by_day()[1]] == [fx.DAI_NOI.key, fx.BAO_TANG.key, fx.THIEN_MU.key]
    assert s.visits[0].minutes == 150
    with pytest.raises(ValueError):
        Visit(1, fx.DAI_NOI.key, time(10, 0), time(10, 0))
    with pytest.raises(ValueError):
        Visit(0, fx.DAI_NOI.key, time(9, 0), time(10, 0))
    with pytest.raises(ValueError):
        Schedule((), source="chatgpt")
    # Lịch chồng giờ / sai địa điểm vẫn tạo được: kiểm ngữ nghĩa là việc của planner_core.
    Schedule((Visit(1, "wikidata:Q1", time(9, 0), time(10, 0)), Visit(1, "x", time(9, 30), time(10, 30))), "llm")


def test_feasibility_report_derives_feasible():
    ok = FeasibilityReport(metrics={"visits": 3.0})
    bad = FeasibilityReport((Violation(ViolationKind.TRAVEL, "không kịp", 1), Violation(ViolationKind.TRAVEL, "x")))
    assert ok.feasible and not bad.feasible
    assert bad.counts() == {"travel": 2}
    assert bad.to_dict()["feasible"] is False
    assert FeasibilityReport.from_dict(bad.to_dict()) == bad
    with pytest.raises(ValueError):
        FeasibilityReport(metrics={"x": math.inf})


def test_recognition_rules():
    Recognition("wikidata:Q975568", 0.9, (("wikidata:Q975568", 0.9), ("wikidata:Q7481171", 0.05)), used_gps=True)
    Recognition(None, 0.2, is_ood=True)
    with pytest.raises(ValueError):
        Recognition("wikidata:Q975568", 0.9, is_ood=True)
    with pytest.raises(ValueError):
        Recognition(None, 0.5, (("wikidata:Q1", 0.1), ("wikidata:Q2", 0.4)))
    with pytest.raises(ValueError):
        Recognition(None, 1.5)


# --------------------------------------------------------------------------- RAG

def _p(cid="1-0", score=0.5):
    return Passage(cid, "wikidata:Q1", "Chùa Thiên Mụ", "", "Chùa xây năm 1601.", score)


def test_passage_validation_and_json():
    p = _p()
    assert json.loads(json.dumps(p.to_dict()))["chunk_id"] == "1-0"
    with pytest.raises(ValueError):
        _p(cid="")
    with pytest.raises(ValueError):
        _p(score=math.nan)
    with pytest.raises(ValueError):
        Passage("1-0", None, "t", "", "", 0.1)


def test_answer_rejects_citation_outside_context_and_bad_timings():
    ctx = (_p("1-0"), _p("1-1"))
    a = Answer("Năm 1601 [1].", (ctx[0],), ctx, "m", False, {"ttft_ms": 12.0})
    assert a.to_dict()["citations"][0]["chunk_id"] == "1-0"
    with pytest.raises(ValueError, match="ngoài ngữ cảnh"):
        Answer("x", (_p("9-9"),), ctx, "m", False)
    with pytest.raises(ValueError, match="timings"):
        Answer("x", (), ctx, "m", False, {"ttft_ms": -1.0})


# --------------------------------------------------------------------------- JSON cho mọi kiểu

SAMPLES = {
    "OpeningRule": lambda: fx.DAI_NOI.opening_hours.rules[0],
    "OpeningHours": lambda: fx.DAI_NOI.opening_hours,
    "Place": lambda: fx.DAI_NOI,
    "Location": lambda: fx.HOTEL,
    "WeatherHour": lambda: fx.weather_day(date(2025, 10, 15))[7],
    "TravelMatrix": fx.travel_matrix,
    "Preferences": lambda: Preferences(must_visit={"wikidata:Q1"}, pace="cham", budget_vnd=300_000),
    "Problem": lambda: fx.sample_problem(days=2),
    "Visit": lambda: fx.sample_schedule().visits[0],
    "Schedule": fx.sample_schedule,
    "Violation": lambda: Violation(ViolationKind.OPENING_HOURS, "đến lúc đã đóng cửa", 2),
    "FeasibilityReport": lambda: FeasibilityReport((Violation(ViolationKind.DUPLICATE, "x"),), {"visits": 2.0}),
    "Recognition": lambda: Recognition("wikidata:Q975568", 0.8, (("wikidata:Q975568", 0.8),), used_gps=True),
    "Passage": _p,
    "Answer": lambda: Answer("Năm 1601 [1].", (_p(),), (_p(),), "qwen3.5:4b", False, {"ttft_ms": 3000.0}, 12),
}


def test_every_type_has_a_sample():
    assert set(SAMPLES) == set(ALL_TYPES)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_json_roundtrip(name):
    obj = SAMPLES[name]()
    assert _roundtrip(obj) == obj


def test_from_dict_rejects_unknown_fields_and_wrong_types():
    data = fx.DAI_NOI.to_dict()
    with pytest.raises(ValueError, match="trường lạ"):
        Place.from_dict({**data, "rating": 5})
    with pytest.raises(ValueError, match="số nguyên"):
        Place.from_dict({**data, "avg_visit_minutes": "150"})
    with pytest.raises(ValueError, match="HH:MM"):
        Visit.from_dict({"day": 1, "place_key": "x", "start": "9h", "end": "10:00"})
    times = fx.weather_day(date(2025, 10, 15))[0].to_dict()
    assert times["time"].endswith("+07:00")
    with pytest.raises(ValueError, match="múi giờ"):
        WeatherHour.from_dict({**times, "time": "2025-10-15T06:00:00"})


def test_json_uses_readable_forms():
    d = fx.sample_problem().to_dict()
    assert d["day_start"] == "08:00" and d["start_date"] == "2025-10-15"
    assert d["places"][0]["opening_hours"]["rules"][0]["intervals"] == [["06:30", "17:30"]]
    assert d["places"][0]["unsafe_conditions"] == ["bao"]
    assert d["weather"][0]["issued_at"] == datetime(2025, 10, 14, 18, 0, tzinfo=VN_TZ).isoformat()
