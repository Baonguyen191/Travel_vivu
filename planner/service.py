"""Điểm vào cho agent: lập lịch một chuyến đi từ DB, thời tiết và Google Maps."""

from dataclasses import dataclass
from datetime import date

from maps.routing import choose_routing
from maps.flood import load_flood_zones
from planner.models import ScheduleResult, UserConstraint
from planner.places import load_places
from planner.routes import attach_routes
from planner.solver import SolverOptions, WeatherAwareScheduleOptimizer, trip_days
from planner.weather import trip_weather


@dataclass
class TripPlan:
    result: ScheduleResult
    travel_time_source: str  # "google" | "osrm" | "estimate"
    billed_elements: int  # phần tử Google Routes đã tính phí trong lần lập lịch này
    routes_attached: int


def plan_trip(conn, place_ids: list[int], constraint: UserConstraint, today: date | None = None,
              maps_client=None, with_routes: bool = False,
              options: SolverOptions | None = None, priorities: dict[int, float] | None = None,
              fetch_json=None) -> TripPlan:
    """Lập lịch cho các `place_ids`; `priorities` (0..1) là mức muốn đi do LLM trích ra.

    Không có `maps_client` thì chọn tự động (maps/routing.py): OSRM nếu đang chạy,
    rồi Google nếu có key, không thì ước lượng; lịch ghi chú nguồn đã dùng.
    `with_routes=True` lấy thêm tuyến từng chặng (polyline) để vẽ bản đồ.
    """
    today = today or date.today()
    maps_client = maps_client or choose_routing()
    places = load_places(conn, ids=place_ids)
    for place in places:
        place.priority = (priorities or {}).get(place.id, place.priority)
    zones = tuple(load_flood_zones())
    options = options or SolverOptions()
    options.flood_zones = options.flood_zones or zones

    days = [d for d, _, _ in trip_days(constraint)]
    locations = [constraint.start_location, constraint.end_location] + [(p.lat, p.lon) for p in places]
    weather = trip_weather(locations, days, today, conn=conn, fetch_json=fetch_json)

    optimizer = WeatherAwareScheduleOptimizer(options, maps_client=maps_client)
    result = optimizer.optimize(places, constraint, weather)
    attached = 0
    if with_routes and maps_client is not None and maps_client.is_enabled:
        attached = attach_routes(result, {p.id: p for p in places}, constraint, maps_client, weather, zones)
    source = optimizer.last_matrix_source
    return TripPlan(result, source, getattr(maps_client, "billed_elements", 0), attached)


def format_plan(plan: TripPlan) -> str:
    """Bản văn bản thô của lịch, để kiểm tra bằng mắt (agent dùng LLM để diễn giải)."""
    lines = [f"Thời gian đi: {plan.travel_time_source} (đã tính phí {plan.billed_elements} phần tử Google)."]
    for day in plan.result.itineraries:
        lines.append(f"\n== {day.date:%d/%m/%Y} (thời tiết: {day.weather_tier})")
        lines += [f"  ! {n}" for n in day.notes]
        for v in day.visits:
            lines.append(f"  {v.arrival_time:%H:%M}-{v.departure_time:%H:%M}  {v.place_name}"
                         f"  (đi {v.travel_minutes_from_prev} phút, {v.travel_distance_km:.1f} km)")
            for extra in (v.weather_note, v.travel_advice, v.route_note):
                if extra:
                    lines.append(f"      - {extra}")
        lines += [f"  Dẫn đường: {url}" for url in day.navigation_urls]
    if plan.result.weather_adaptations:
        lines.append("\nĐiều chỉnh theo thời tiết:")
        lines += [f"  - {a}" for a in plan.result.weather_adaptations]
    if plan.result.unvisited:
        lines.append("\nKhông xếp được:")
        lines += [f"  - {pid}: {why}" for pid, why in plan.result.unvisited.items()]
    return "\n".join(lines)
