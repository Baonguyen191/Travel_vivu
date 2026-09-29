"""Integration tests của planner với PostgreSQL (nạp place, thực nghiệm)."""

import json
from datetime import date, datetime, time, timedelta

import pytest
from psycopg.types.json import Jsonb

from planner.experiment import ExperimentConfig, Scenario, run
from planner.places import load_places, load_places_by_qid
from planner.weather import load_cached_forecast


def _insert(conn, name, qid, lat, lon, category="lang_tam", **fields):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO places (name, category, location, avg_visit_minutes, indoor_ratio,"
            " weather_sensitivity, unsafe_conditions, opening_hours, ticket_price)"
            " VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (name, category, lon, lat, fields.get("visit", 60), fields.get("indoor", 0.25),
             Jsonb(fields.get("sens", {"rain": 0.8, "heat": 0.7, "wind": 0.2})),
             fields.get("unsafe", ["mua_lon"]),
             Jsonb(fields["hours"]) if "hours" in fields else None,
             Jsonb(fields["ticket"]) if "ticket" in fields else None),
        )
        pid = cur.fetchone()[0]
        cur.execute("INSERT INTO place_external_ids (place_id, source, external_id) VALUES (%s, 'wikidata', %s)",
                    (pid, qid))
    return pid


@pytest.mark.integration
def test_load_places_reads_db_formats(db_conn):
    _insert(db_conn, "Hoàng thành Huế", "Q10769129", 16.4694, 107.5778, category="di_tich", visit=180,
            hours={"sat": [["07:30", "17:30"]]}, ticket={"vnd": 200000})
    _insert(db_conn, "Lăng Tự Đức", "Q7481171", 16.4325, 107.566)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO places (name, category) VALUES ('Huế', 'boi_canh')")

    [hoang_thanh, lang] = load_places_by_qid(db_conn, ["Q10769129", "Q7481171"])
    assert hoang_thanh.lat == pytest.approx(16.4694) and hoang_thanh.lon == pytest.approx(107.5778)
    assert hoang_thanh.avg_visit_minutes == 180 and hoang_thanh.ticket_price_vnd == 200000
    assert hoang_thanh.opening_windows(date(2026, 10, 10)) == [(450, 1050)]
    assert lang.weather_sensitivity == {"rain": 0.8, "heat": 0.7, "wind": 0.2}
    assert lang.unsafe_conditions == ["mua_lon"] and lang.opening_hours is None
    assert {p.name for p in load_places(db_conn)} == {"Hoàng thành Huế", "Lăng Tự Đức"}
    with pytest.raises(ValueError, match="Q404"):
        load_places_by_qid(db_conn, ["Q404"])


@pytest.mark.integration
def test_load_cached_forecast_respects_age(db_conn):
    now = datetime(2026, 10, 10, 7)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO weather_cache (lat_grid, lon_grid, forecast_time, precip_mm, fetched_at)"
            " VALUES (16.5, 107.6, '2026-10-10 09:00+07', 4.0, '2026-10-10 05:00+07')")
    [rec] = load_cached_forecast(db_conn, (16.5, 107.6), now)
    assert rec.time == datetime(2026, 10, 10, 9) and rec.precipitation_mm == 4.0
    assert load_cached_forecast(db_conn, (16.5, 107.6), now + timedelta(hours=10)) is None


def _payload(start: date, end: date, suffix: str, rain_hours: set[int]) -> dict:
    hours = []
    t = datetime.combine(start, time())
    while t.date() <= end:
        hours.append(t)
        t += timedelta(hours=1)
    return {"hourly": {
        "time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours],
        f"precipitation{suffix}": [6.0 if h.hour in rain_hours else 0.0 for h in hours],
    }}


@pytest.mark.integration
def test_experiment_run_end_to_end(db_conn, tmp_path):
    _insert(db_conn, "Lăng Tự Đức", "Q7481171", 16.4325, 107.566, visit=90)
    _insert(db_conn, "Bảo tàng", "Q5929149", 16.4713, 107.5819, category="bao_tang", indoor=0.95, unsafe=[],
            sens={"rain": 0.1, "heat": 0.1, "wind": 0.0})

    def fetch(url, params, fresh):
        start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
        if "previous-runs" in url:
            return _payload(start, end, "_previous_day1", rain_hours={8, 9, 10, 11})
        return _payload(start, end, "", rain_hours={8, 9, 10, 11})  # dự báo đúng

    cfg = ExperimentConfig(
        places=["Q7481171", "Q5929149"], hotel=(16.4637, 107.5909), transport_mode="motorbike",
        day_start=time(8), day_end=time(18), max_places_per_day=2, lead_days=1,
        periods=[(date(2025, 10, 1), date(2025, 10, 3))], scenarios=[Scenario("1 ngày", 1, 1)])
    report, rows = run(db_conn, cfg, time_limit_s=0.3, fetch_json=fetch, out_dir=str(tmp_path))

    assert len(rows) == 3 * 3
    rain = {v: sum(r["outdoor_rain_hours"] for r in rows if r["variant"] == v) for v in ("aware", "baseline")}
    assert rain["aware"] < rain["baseline"]
    assert all(r["feasible"] for r in rows)
    assert "aware − baseline" in report
    assert list(tmp_path.glob("planner_experiment_*.csv"))
    json.dumps(rows)  # hàng CSV chỉ chứa kiểu đơn giản


@pytest.mark.integration
def test_llm_collect_and_score(db_conn, tmp_path):
    from types import SimpleNamespace

    from planner.experiment import collect_llm

    lang = _insert(db_conn, "Lăng Tự Đức", "Q7481171", 16.4325, 107.566, visit=90)
    bt = _insert(db_conn, "Bảo tàng", "Q5929149", 16.4713, 107.5819, category="bao_tang", indoor=0.95, unsafe=[])

    def fetch(url, params, fresh):
        start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
        suffix = "_previous_day1" if "previous-runs" in url else ""
        return _payload(start, end, suffix, rain_hours={8, 9, 10, 11})

    answers = {  # ngày 1: lịch hợp lệ; ngày 2: không phải JSON; ngày 3: đến lăng không kịp
        "2025-10-01": [(bt, "08:30", "09:30"), (lang, "10:00", "11:30")],
        "2025-10-03": [(bt, "08:05", "09:30"), (lang, "09:31", "11:00")],
    }

    class FakeLLM:
        model, reasoning_effort = "fake", "none"

        def chat(self, messages, json_mode=False):
            prompt = messages[0]["content"]
            day = next(d for d in ("2025-10-01", "2025-10-02", "2025-10-03") if f"từ {d}" in prompt)
            if day not in answers:
                return SimpleNamespace(content="Xin lỗi, tôi không lập được lịch.")
            visits = [{"place_id": p, "arrival": a, "departure": d} for p, a, d in answers[day]]
            return SimpleNamespace(content=json.dumps({"days": [{"date": day, "visits": visits}]}))

    cfg = ExperimentConfig(
        places=["Q7481171", "Q5929149"], hotel=(16.4637, 107.5909), transport_mode="motorbike",
        day_start=time(8), day_end=time(18), max_places_per_day=2, lead_days=1,
        periods=[(date(2025, 10, 1), date(2025, 10, 3))], scenarios=[Scenario("1 ngày", 1, 1)])
    llm_dir = tmp_path / "llm"
    summary = collect_llm(db_conn, cfg, FakeLLM(), llm_dir, workers=2, fetch_json=fetch)
    assert summary == {"requested": 3, "failed": []}
    assert collect_llm(db_conn, cfg, FakeLLM(), llm_dir, fetch_json=fetch)["requested"] == 0  # đã cache

    report, rows = run(db_conn, cfg, time_limit_s=0.3, fetch_json=fetch, out_dir=None, llm_dir=str(llm_dir))
    llm = {r["start"]: r for r in rows if r["variant"] == "llm"}
    assert len(rows) == 3 * 4
    assert llm["2025-10-01"]["feasible"] == 1 and llm["2025-10-01"]["travel_minutes"] > 0
    assert llm["2025-10-02"]["solver_status"] == "llm_invalid" and llm["2025-10-02"]["feasible"] == 0
    assert "travel" in llm["2025-10-03"]["violations"]
    assert "aware − llm" in report and "`fake`" in report


@pytest.mark.integration
def test_plan_trip_end_to_end_with_fake_services(db_conn):
    import httpx

    from maps.client import GoogleMapsClient
    from planner.models import UserConstraint
    from planner.service import format_plan, plan_trip

    lang = _insert(db_conn, "Lăng Tự Đức", "Q7481171", 16.4325, 107.566, visit=90)
    bt = _insert(db_conn, "Bảo tàng", "Q5929149", 16.4713, 107.5819, category="bao_tang", indoor=0.95, unsafe=[])
    today = date(2030, 1, 5)
    trip_day = date(2030, 1, 7)

    def fetch(url, params, fresh):  # dự báo: mưa lớn buổi sáng ngày đi
        payload = _payload(today, today + timedelta(days=15), "", rain_hours=set())
        times = payload["hourly"]["time"]
        payload["hourly"]["precipitation"] = [
            9.0 if t.startswith(trip_day.isoformat()) and int(t[11:13]) < 12 else 0.0 for t in times]
        return payload

    def routes(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=[
            {"originIndex": i, "destinationIndex": j, "condition": "ROUTE_EXISTS", "distanceMeters": 3000,
             "duration": "900s", "staticDuration": "600s"}
            for i in range(len(body["origins"])) for j in range(len(body["destinations"]))])

    client = GoogleMapsClient(api_key="k", http_client=httpx.Client(transport=httpx.MockTransport(routes)))
    hotel = (16.4637, 107.5909)
    constraint = UserConstraint(datetime.combine(trip_day, time(8)), datetime.combine(trip_day, time(18)),
                                hotel, hotel)
    plan = plan_trip(db_conn, [lang, bt], constraint, today=today, maps_client=client, fetch_json=fetch)

    by_id = {v.place_id: v for v in plan.result.visits}
    assert set(by_id) == {lang, bt}
    assert by_id[lang].arrival_time.hour >= 12  # mưa lớn buổi sáng, lăng có unsafe mua_lon
    assert plan.travel_time_source == "google" and plan.billed_elements > 0
    assert all(v.travel_minutes_from_prev >= 15 for v in plan.result.visits if v.travel_distance_km > 0)
    assert "Dẫn đường:" in format_plan(plan)
