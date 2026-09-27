"""Tool của agent: nơi thuật toán và dữ liệu làm phần đúng/sai (CLAUDE.md).

Mỗi tool trả về JSON gọn cho LLM đọc, đồng thời ghi kết quả đầy đủ vào
`ctx.artifacts` để giao diện vẽ bảng, bản đồ, biểu đồ. Chế độ không LLM của
demo gọi thẳng các hàm `run_*` này.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from agent.nlu import DEFAULT_TRIP, best_sentences, fold, format_opening_hours, match_landmarks
from planner import rules
from planner.experiment import evaluate_variant, load_weather
from planner.models import Place, UserConstraint
from planner.places import DESTINATION_CATEGORIES, load_places, load_places_by_qid
from maps.osrm import OsrmClient
from planner.routes import attach_routes
from planner.service import plan_trip
from planner.solver import SolverOptions, WeatherAwareScheduleOptimizer, trip_days
from planner.weather import HOURLY, default_fetch_json, tier_for_lead, trip_weather

TIER_LABEL = {"hourly": "dự báo theo giờ", "daily": "dự báo theo ngày", "climate": "khí hậu nhiều năm",
              "none": "không có dữ liệu thời tiết"}


@dataclass
class AgentContext:
    conn: object
    retriever: object
    hotel: tuple[float, float]
    today: date
    max_places_per_day: int = 4
    default_mode: str = "motorbike"
    compare_baseline: bool = True
    maps_client: object = None
    with_routes: bool = False
    artifacts: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tìm địa điểm theo tên
# ---------------------------------------------------------------------------


def resolve_places(conn, names: list[str]) -> tuple[list[Place], list[str]]:
    """Tên do người dùng/LLM nêu -> Place trong DB. Trả về (tìm được, không tìm được)."""
    found: list[Place] = []
    missing: list[str] = []
    catalog = None
    for name in names:
        qids = match_landmarks(name)
        if qids:
            found += [p for p in load_places_by_qid(conn, qids[:1]) if p.id not in {f.id for f in found}]
            continue
        if catalog is None:
            catalog = load_places(conn, categories=DESTINATION_CATEGORIES)
        target = fold(name)
        candidates = [p for p in catalog if target and (target in fold(p.name) or fold(p.name) in target)]
        if candidates:
            best = min(candidates, key=lambda p: abs(len(fold(p.name)) - len(target)))
            if best.id not in {f.id for f in found}:
                found.append(best)
        else:
            missing.append(name)
    return found, missing


# ---------------------------------------------------------------------------
# Các hàm làm việc thật
# ---------------------------------------------------------------------------


def run_search_knowledge(ctx: AgentContext, query: str, place_name: str | None = None) -> dict:
    chunks = ctx.retriever.search(query, top_k=10)
    # Bỏ chunk chỉ có tiêu đề mục ("== Lăng lân cận =="): lỗi chia chunk của pipeline Wikipedia.
    chunks = [c for c in chunks if len(c.content) >= 80 and not c.content.lstrip().startswith("==")] or chunks
    named = {p.id for p in resolve_places(ctx.conn, [place_name])[0]} if place_name else set()
    if not named:
        named = {p.id for p in resolve_places(ctx.conn, [query])[0]} if match_landmarks(query) else set()
    chunks = sorted(chunks, key=lambda c: c.place_id not in named)[:5]
    ctx.artifacts["chunks"] = chunks
    return {"results": [
        {"ref": i + 1, "place": c.place_name, "content": c.content[:900], "source_url": c.source_url}
        for i, c in enumerate(chunks)
    ]}


def extractive_answer(query: str, chunks) -> tuple[str, list[str]]:
    """Câu trả lời không LLM: các câu liên quan nhất của đoạn đầu, kèm nguồn."""
    top = chunks[0]
    sources = list(dict.fromkeys(c.source_url for c in chunks[:3] if c.source_url))
    return f"**{top.place_name}**: {best_sentences(query, top.content, context=top.place_name)}", sources


def run_place_facts(ctx: AgentContext, place_names: list[str]) -> dict:
    places, missing = resolve_places(ctx.conn, place_names)
    facts = []
    with ctx.conn.cursor() as cur:
        for p in places:
            cur.execute("SELECT opening_hours_raw, ticket_price, dress_code, updated_at, source_url"
                        " FROM places WHERE id = %s", (p.id,))
            raw, ticket, dress, updated, source = cur.fetchone()
            vnd = ticket.get("vnd") if isinstance(ticket, dict) else None
            facts.append({
                "place": p.name,
                "opening_hours": format_opening_hours(p.opening_hours, raw) or None,
                "ticket_price_vnd": vnd,
                "dress_code": dress,
                "updated_at": updated.date().isoformat() if updated else None,
                "source_url": source,
            })
    return {"places": facts, "not_found": missing,
            "note": "Trường null nghĩa là CSDL chưa có dữ liệu; không được đoán."}


def _hour_rows(weather, point, day: date, observed=None) -> list[dict]:
    rows = []
    for h in range(6, 21):
        t = datetime.combine(day, time(h))
        w = weather.at(*point, t) if weather is not None else None
        o = observed.observed_at(*point, t) if observed is not None else None
        if w is None and o is None:
            continue
        row = {"hour": h}
        if w is not None:
            row.update({"forecast_precip_mm": round(w.precipitation_mm, 1), "temp_c": round(w.temperature_c),
                        "wind_kmh": round(w.wind_speed_kmh), "hazards": sorted(rules.hazards(w))})
        if o is not None:
            row["observed_precip_mm"] = round(o.precipitation_mm, 1)
        rows.append(row)
    return rows


def run_weather(ctx: AgentContext, day: date) -> dict:
    point = ctx.hotel
    if day < ctx.today:
        forecast, observed = load_weather(default_fetch_json(ctx.conn), [point], [(day, day)], 1)
        forecast.set_tier(day, HOURLY)
        rows = _hour_rows(forecast, point, day, observed)
        ctx.artifacts["weather"] = rows
        return {"date": day.isoformat(), "kind": "ngày đã qua: dự báo phát hành trước 1 ngày và thời tiết thật ERA5",
                "hours": rows}
    weather = trip_weather([point], [day], ctx.today, conn=ctx.conn)
    tier = weather.tier(day)
    rows = _hour_rows(weather, point, day) if tier == "hourly" else []
    ctx.artifacts["weather"] = rows
    result = {"date": day.isoformat(), "tier": TIER_LABEL[tier], "lead_days": (day - ctx.today).days,
              "hours": rows, "notes": weather.notes(day), "retrieved_at": datetime.now().isoformat(timespec="minutes")}
    if tier == "daily":
        w = weather.at(*point, datetime.combine(day, time(12)))
        result["daily"] = {"precip_mm": round(w.precipitation_mm), "max_wind_kmh": round(w.wind_speed_kmh),
                           "max_temp_c": round(w.temperature_c), "hazards": sorted(rules.hazards(w))}
    return result


def _summarize(result, places_by_id) -> dict:
    return {
        "days": [{
            "date": d.date.isoformat(),
            "weather": TIER_LABEL.get(d.weather_tier, d.weather_tier),
            "visits": [{"arrive": f"{v.arrival_time:%H:%M}", "leave": f"{v.departure_time:%H:%M}",
                        "place": v.place_name, "travel_minutes": v.travel_minutes_from_prev,
                        "note": " ".join(x for x in (v.weather_note, v.travel_advice) if x) or None}
                       for v in d.visits],
            "notes": d.notes,
        } for d in result.itineraries],
        "not_scheduled": [{"place": places_by_id[pid].name, "reason": why} for pid, why in result.unvisited.items()],
        "weather_adaptations": result.weather_adaptations,
    }


def run_plan(ctx: AgentContext, start: date, days: int, places: list[Place], mode: str,
             max_places: int | None = None) -> dict:
    """Lập lịch (thuật toán), ghi artifacts để vẽ; trả về bản tóm tắt cho LLM."""
    places_by_id = {p.id: p for p in places}
    priorities = {p.id: 1.0 - rank / (2 * len(places)) for rank, p in enumerate(places)}
    for p in places:
        p.priority = priorities[p.id]
    constraint = UserConstraint(
        start_datetime=datetime.combine(start, time(8)),
        end_datetime=datetime.combine(start + timedelta(days=days - 1), time(18)),
        start_location=ctx.hotel, end_location=ctx.hotel,
        max_places_per_day=max_places or ctx.max_places_per_day, transport_mode=mode)
    plans, outcome, frame_rows = {}, None, None

    if start < ctx.today:
        trip = [d for d, _, _ in trip_days(constraint)]
        forecast, observed = load_weather(default_fetch_json(ctx.conn), [ctx.hotel] + [(p.lat, p.lon) for p in places],
                                          [(trip[0], trip[-1])], 1)
        for d in trip:
            forecast.set_tier(d, tier_for_lead(1))
        # Ngày đã qua: Google không nhận giờ quá khứ; OSRM không phụ thuộc giờ nên dùng được.
        routing = ctx.maps_client if isinstance(ctx.maps_client, OsrmClient) else None
        aware = WeatherAwareScheduleOptimizer(SolverOptions(time_limit_s=3), maps_client=routing)
        plans["Thích ứng thời tiết"] = aware.optimize(places, constraint, forecast)
        if ctx.compare_baseline:
            plans["Không tính thời tiết"] = WeatherAwareScheduleOptimizer(
                SolverOptions(weather_aware=False, time_limit_s=2), maps_client=routing).optimize(places, constraint)
        if routing is not None and ctx.with_routes:
            for result in plans.values():
                attach_routes(result, places_by_id, constraint, routing, forecast)
        outcome = {name: evaluate_variant(r, places_by_id, constraint, observed) for name, r in plans.items()}
        frame_rows = _hour_rows(forecast, ctx.hotel, trip[0], observed)
        source = "đường thật (OSRM)" if aware.last_matrix_source == "osrm" else "ước lượng từ khoảng cách"
    else:
        plan = plan_trip(ctx.conn, [p.id for p in places], constraint, today=ctx.today, maps_client=ctx.maps_client,
                         with_routes=ctx.with_routes, priorities=priorities, options=SolverOptions(time_limit_s=3))
        plans["Thích ứng thời tiết"] = plan.result
        if ctx.compare_baseline:
            plans["Không tính thời tiết"] = WeatherAwareScheduleOptimizer(
                SolverOptions(weather_aware=False, time_limit_s=2), maps_client=ctx.maps_client
            ).optimize(places, constraint)
            if ctx.with_routes and ctx.maps_client is not None and ctx.maps_client.is_enabled:
                attach_routes(plans["Không tính thời tiết"], places_by_id, constraint, ctx.maps_client)
        if plan.result.itineraries and plan.result.itineraries[0].weather_tier == "hourly":
            frame_rows = _hour_rows(trip_weather([ctx.hotel], [start], ctx.today, conn=ctx.conn), ctx.hotel, start)
        source = {"google": "Google Routes có giao thông", "osrm": "đường thật (OSRM), không tính giao thông"}.get(
            plan.travel_time_source, "ước lượng từ khoảng cách")

    ctx.artifacts.update({"plans": plans, "places": places_by_id, "hotel": ctx.hotel, "outcome": outcome,
                          "weather": frame_rows})
    summary = {"start_date": start.isoformat(), "days": days, "transport_mode": mode,
               "travel_time_source": source, "weather_aware_plan": _summarize(plans["Thích ứng thời tiết"], places_by_id)}
    if "Không tính thời tiết" in plans:
        base = plans["Không tính thời tiết"]
        summary["baseline_without_weather"] = [
            f"{v.arrival_time:%d/%m %H:%M} {v.place_name}" for v in base.visits]
    if outcome:
        summary["measured_on_real_weather"] = {
            name: {"outdoor_hours_in_rain": m["outdoor_rain_hours"], "visits_during_heavy_rain": m["hazard_visits"]}
            for name, m in outcome.items()}
    return summary


# ---------------------------------------------------------------------------
# Khai báo cho LLM
# ---------------------------------------------------------------------------

TOOLS = [
    {"type": "function", "function": {
        "name": "search_knowledge",
        "description": "Tìm trong kho tri thức (Wikipedia tiếng Việt) về địa danh Huế: lịch sử, kiến trúc, mô tả."
                       " Dùng cho MỌI câu hỏi mô tả/lịch sử; không trả lời từ trí nhớ.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["query", "place_name"],
                       "properties": {
                           "query": {"type": "string", "description": "Câu truy vấn tiếng Việt"},
                           "place_name": {"type": ["string", "null"], "description": "Tên địa danh chính nếu có"}}},
        "strict": True}},
    {"type": "function", "function": {
        "name": "get_place_facts",
        "description": "Giờ mở cửa, giá vé, quy định trang phục từ CSDL (kèm ngày cập nhật). Bắt buộc dùng cho các"
                       " thông tin này; null nghĩa là CSDL chưa có.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["place_names"],
                       "properties": {"place_names": {"type": "array", "items": {"type": "string"}}}},
        "strict": True}},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Thời tiết Huế theo giờ cho một ngày. Ngày tương lai: dự báo Open-Meteo. Ngày đã qua: dự báo"
                       " phát hành trước 1 ngày và thời tiết thật (ERA5).",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["date"],
                       "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}}},
        "strict": True}},
    {"type": "function", "function": {
        "name": "plan_trip",
        "description": "Lập lịch tham quan bằng bộ tối ưu OR-Tools thích ứng thời tiết. Thuật toán chọn điểm, giờ và"
                       " thứ tự; KHÔNG tự sắp lịch. Ngày đã qua: lập trên dự báo cũ và chấm bằng thời tiết thật.",
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["start_date", "days", "place_names", "transport_mode", "max_places_per_day"],
                       "properties": {
                           "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                           "days": {"type": "integer", "description": "1 đến 7"},
                           "place_names": {"type": "array", "items": {"type": "string"},
                                           "description": "Theo thứ tự ưu tiên; rỗng = để hệ thống chọn điểm nổi bật"},
                           "transport_mode": {"type": ["string", "null"], "enum": ["motorbike", "car", "walking", None],
                                              "description": "Chỉ điền khi người dùng nêu phương tiện; không thì null"},
                           "max_places_per_day": {"type": ["integer", "null"]}}},
        "strict": True}},
]


def execute_tool(ctx: AgentContext, name: str, arguments: str) -> dict:
    args = json.loads(arguments or "{}")
    if name == "search_knowledge":
        return run_search_knowledge(ctx, args["query"], args.get("place_name"))
    if name == "get_place_facts":
        return run_place_facts(ctx, args["place_names"])
    if name == "get_weather":
        return run_weather(ctx, date.fromisoformat(args["date"]))
    if name == "plan_trip":
        names = args.get("place_names") or []
        places, missing = resolve_places(ctx.conn, names)
        if not names or not places:
            places = load_places_by_qid(ctx.conn, DEFAULT_TRIP)
        days = max(1, min(int(args.get("days") or 1), 7))
        summary = run_plan(ctx, date.fromisoformat(args["start_date"]), days, places,
                           args.get("transport_mode") or ctx.default_mode, args.get("max_places_per_day"))
        if missing:
            summary["places_not_found_in_database"] = missing
        return summary
    return {"error": f"tool không tồn tại: {name}"}
