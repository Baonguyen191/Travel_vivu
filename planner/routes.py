"""Gắn tuyến đường Google vào lịch đã giải xong, né vùng ngập khi mưa lớn.

Chạy sau solver, chỉ cho các chặng thật sự đi (vài chặng mỗi ngày), nên gọi
`computeRoutes` ít hơn nhiều so với lấy tuyến cho cả ma trận. Kết quả dùng để
vẽ tuyến trên bản đồ (encoded polyline) và báo người dùng khi phải đổi tuyến.
"""

from datetime import datetime, timedelta

from maps.client import GoogleMapsClient, RouteOption, RoutesError, decode_polyline
from maps.flood import FloodZone, zones_on_path
from planner import rules
from planner.models import Place, ScheduleResult, UserConstraint
from planner.weather import LOCAL_TZ, GridWeather


def _choose(options: list[RouteOption], zones: list[FloodZone], avoid: bool) -> tuple[RouteOption, str | None]:
    """Tuyến đầu tiên không qua vùng ngập nếu cần né; kèm ghi chú cho người dùng."""
    main = options[0]
    if not avoid or not zones:
        return main, None
    main_hits = zones_on_path(decode_polyline(main.polyline), zones)
    if not main_hits:
        return main, None
    for alt in options[1:]:
        if not zones_on_path(decode_polyline(alt.polyline), zones):
            extra = alt.minutes - main.minutes
            return alt, (f"Tuyến ngắn nhất qua vùng dễ ngập ({', '.join(z.name for z in main_hits)});"
                         f" chọn tuyến khác, lâu hơn {extra:+d} phút.")
    return main, ("Mọi tuyến Google đề xuất đều qua vùng dễ ngập ("
                  + ", ".join(z.name for z in main_hits) + "); cân nhắc đợi ngớt mưa.")


def _stormy_at(weather: GridWeather | None, point: tuple[float, float], when: datetime) -> bool:
    if weather is None:
        return False
    w = weather.at(point[0], point[1], when)
    return w is not None and bool(rules.hazards(w) & {rules.MUA_LON, rules.BAO})


def attach_routes(result: ScheduleResult, places: dict, constraint: UserConstraint,
                  client: GoogleMapsClient, weather: GridWeather | None = None,
                  zones: list[FloodZone] | tuple[FloodZone, ...] = ()) -> int:
    """Điền `route_polyline`/`route_note` cho từng chặng. Trả về số chặng đã lấy được tuyến.

    Chặng nào dự báo mưa lớn/bão lúc khởi hành thì xin thêm tuyến thay thế và
    chọn tuyến không qua vùng ngập. Lỗi Google ở chặng nào thì chặng đó để
    trống và ghi chú, không dừng cả lịch.
    """
    zones = list(zones)
    done = 0
    for itinerary in result.itineraries:
        prev_point, prev_leave = constraint.start_location, None
        legs = []
        for v in itinerary.visits:
            place: Place = places[v.place_id]
            depart = prev_leave or (v.arrival_time - timedelta(minutes=v.travel_minutes_from_prev))
            legs.append((v, prev_point, (place.lat, place.lon), depart))
            prev_point, prev_leave = (place.lat, place.lon), v.departure_time
        if itinerary.visits:
            legs.append((None, prev_point, constraint.end_location, prev_leave))

        for visit, origin, destination, depart in legs:
            avoid = _stormy_at(weather, destination if visit else origin, depart)
            try:
                options = client.compute_routes(origin, destination, depart.replace(tzinfo=LOCAL_TZ),
                                                constraint.transport_mode, alternatives=avoid and bool(zones))
            except RoutesError as exc:
                if visit is not None:
                    visit.route_note = f"Không lấy được tuyến Google: {exc}"
                continue
            if not options:
                continue
            chosen, note = _choose(options, zones, avoid)
            done += 1
            if visit is None:
                itinerary.return_route_polyline = chosen.polyline
                if note:
                    itinerary.notes.append("Chặng về: " + note)
            else:
                visit.route_polyline = chosen.polyline
                visit.route_note = note
    return done

