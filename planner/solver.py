"""Bộ lập lịch VRPTW nhiều ngày thích ứng thời tiết (OR-Tools).

Mô hình:

- Mỗi ngày của chuyến đi là một "xe" xuất phát và kết thúc ở điểm người dùng
  chọn (thường là khách sạn), chạy trong khung giờ của ngày đó.
- Mỗi địa điểm được nhân thành nhiều *bản sao* theo (ngày, khung giờ đến). Ở
  tầng dự báo theo giờ, mỗi bản sao ứng với một giờ; ở các tầng khác, một bản
  sao phủ cả khoảng mở cửa trong ngày. Một disjunction buộc chọn nhiều nhất một
  bản sao cho mỗi địa điểm; bỏ cả địa điểm thì chịu phạt `visit_value × priority`.
- Ràng buộc cứng: bản sao nào có giờ tham quan trùng hiện tượng nguy hiểm nằm
  trong `unsafe_conditions` của địa điểm thì không được tạo ra. Khung giờ đến
  bảo đảm tham quan xong trước giờ đóng cửa và kịp về trước cuối ngày.
- Ràng buộc mềm: chi phí cung đi vào một bản sao = thời gian đi (đã nhân hệ số
  thời tiết) + `weather_weight × chi phí thời tiết (0..1) × số giờ tham quan`.
  Nhờ bản sao theo giờ và theo ngày, solver dời được điểm ngoài trời sang giờ
  khô hoặc ngày khô thay vì chỉ cảnh báo.

Thời gian đi (CLAUDE.md, phần chỉ đường): Cost(i, j) = thời gian đi có giao
thông × hệ số thời tiết + phạt rủi ro. Thời gian có giao thông lấy từ Google
Routes tại các mốc `traffic_hours` khi có `maps_client`, không thì ước lượng.
Phạt rủi ro áp khi đoạn đi qua vùng ngập (config/flood_zones.yml) lúc dự báo mưa
lớn hoặc bão. Cả hệ số lẫn mốc giao thông lấy theo khung giờ của điểm đến:
OR-Tools không cho thời gian cung phụ thuộc giờ khởi hành, nên đây là xấp xỉ.

`weather_aware=False` tắt mọi phần thời tiết nhưng giữ nguyên phần còn lại của
mô hình: đó là baseline "bộ lập lịch không có tính năng thời tiết" (CLAUDE.md).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from maps.client import GoogleMapsClient
from maps.flood import FloodZone, zones_on_path
from planner import rules
from planner.matrix import TravelMatrix
from planner.models import (
    DayItinerary, HourlyWeather, Place, ScheduledVisit, ScheduleResult, UserConstraint,
)
from planner.weather import DAILY, HOURLY, LOCAL_TZ, NONE, GridWeather, hours_between

MINUTES_PER_DAY = 24 * 60
_STATUS = {
    0: "NOT_SOLVED", 1: "SUCCESS", 2: "PARTIAL_SUCCESS", 3: "FAIL", 4: "FAIL_TIMEOUT",
    5: "INVALID", 6: "INFEASIBLE", 7: "OPTIMAL",
}


@dataclass
class SolverOptions:
    weather_aware: bool = True
    # Chi phí của một giờ ở nơi hoàn toàn ngoài trời trong thời tiết xấu nhất,
    # quy ra phút di chuyển. 120 = chấp nhận đi thêm tối đa 2 tiếng để tránh.
    weather_weight: int = 120
    # Phạt bỏ một địa điểm priority 1.0. Lớn hơn nhiều mọi chi phí khác, nên
    # solver ưu tiên đi được nhiều điểm nhất rồi mới tối ưu thời tiết.
    visit_value: int = 10_000
    time_limit_s: float = 5.0
    # Mốc giờ lấy ma trận giao thông Google (cao điểm sáng, trưa, cao điểm chiều).
    # Mỗi mốc mỗi ngày tốn n² phần tử tính phí.
    traffic_hours: tuple[int, ...] = (8, 12, 17)
    flood_zones: tuple[FloodZone, ...] = ()
    flood_penalty_minutes: int = 30


@dataclass
class _Copy:
    place: int
    day: int
    earliest: int  # phút tính từ 00:00 của ngày đầu chuyến đi
    latest: int
    weather_cost: int = 0
    factor: float = 1.0  # hệ số thời gian đi tới bản sao này
    arrival_ref: datetime | None = None  # giờ đến đại diện, để chọn mốc giao thông
    stormy: bool = False  # dự báo mưa lớn/bão lúc đến


@dataclass
class _Build:
    copies: list[_Copy] = field(default_factory=list)
    # place -> các khung bị loại vì thời tiết ("10/10 08h: mưa lớn lúc 09h")
    weather_blocked: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    closed: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    unknown_hours: set[int] = field(default_factory=set)


def trip_days(c: UserConstraint) -> list[tuple[date, int, int]]:
    """(ngày, phút bắt đầu, phút kết thúc) trong ngày cho từng ngày của chuyến đi."""
    first, last = c.start_datetime.date(), c.end_datetime.date()
    days = []
    day = first
    while day <= last:
        start, end = _minutes(c.day_start), _minutes(c.day_end)
        if day == first:
            start = max(start, _minutes(c.start_datetime.time()))
        if day == last:
            end = min(end, _minutes(c.end_datetime.time()))
        days.append((day, start, max(start, end)))
        day += timedelta(days=1)
    return days


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _at(day: date, minute: int) -> datetime:
    return datetime.combine(day, time()) + timedelta(minutes=minute)


class WeatherAwareScheduleOptimizer:
    def __init__(self, options: SolverOptions | None = None, maps_client=None):
        """`maps_client`: nguồn ma trận thời gian đi (maps.osrm.OsrmClient hoặc
        maps.client.GoogleMapsClient); None thì ước lượng."""
        self.options = options or SolverOptions()
        self.maps_client = maps_client
        self.last_matrix_source = "estimate"

    # -- dựng bản sao ------------------------------------------------------

    @staticmethod
    def _slot_weather(place: Place, day: date, tier: str, weather: GridWeather,
                      lo: int, hi: int) -> tuple[str | None, float, float, bool]:
        """(lý do nguy hiểm, chi phí thời tiết 0..1, hệ số đi, mưa lớn/bão lúc đến)."""
        if tier == DAILY:
            w = weather.at(place.lat, place.lon, _at(day, 12 * 60))
            if w is None:
                return None, 0.0, 1.0, False
            return (rules.unsafe_reason(place, w), rules.weather_cost(place, w), rules.travel_factor(w),
                    _stormy(w))

        visit = place.avg_visit_minutes
        # Nguy hiểm: xét mọi giờ có thể đang ở đó, từ lúc đến sớm nhất tới lúc rời muộn nhất.
        for t, _ in hours_between(_at(day, lo), _at(day, hi + visit)):
            w = weather.at(place.lat, place.lon, t)
            if w is not None and (reason := rules.unsafe_reason(place, w)):
                return f"{reason} lúc {t:%H}h", 0.0, 1.0, False
        # Chi phí: trung bình theo giờ trong lúc tham quan, nếu đến giữa khung.
        mid = _at(day, (lo + hi) // 2)
        weighted = total = 0.0
        for t, overlap in hours_between(mid, mid + timedelta(minutes=visit)):
            w = weather.at(place.lat, place.lon, t)
            if w is not None:
                weighted += rules.weather_cost(place, w) * overlap
                total += overlap
        arrival_w = weather.at(place.lat, place.lon, mid)
        return (None, (weighted / total if total else 0.0), rules.travel_factor(arrival_w),
                arrival_w is not None and _stormy(arrival_w))

    def _build_copies(self, places: list[Place], days: list[tuple[date, int, int]],
                      weather: GridWeather | None) -> _Build:
        opt = self.options
        build = _Build()
        for p_idx, place in enumerate(places):
            visit = place.avg_visit_minutes
            for d_idx, (day, day_lo, day_hi) in enumerate(days):
                windows = place.opening_windows(day)
                if windows is None:
                    build.unknown_hours.add(p_idx)
                    windows = [(0, MINUTES_PER_DAY)]
                if not windows:
                    build.closed[p_idx].append(f"{day:%d/%m} đóng cửa")
                    continue
                tier = weather.tier(day) if (opt.weather_aware and weather is not None) else NONE
                for open_min, close_min in windows:
                    # Đến trong [lo, hi] thì tham quan xong trước khi đóng cửa và trước cuối ngày.
                    lo = max(open_min, day_lo)
                    hi = min(close_min, day_hi) - visit
                    if hi < lo:
                        continue
                    slots = ([(max(lo, h * 60), min(hi, h * 60 + 59)) for h in range(lo // 60, hi // 60 + 1)]
                             if tier == HOURLY else [(lo, hi)])
                    for s_lo, s_hi in slots:
                        reason, cost, factor, stormy = None, 0.0, 1.0, False
                        if tier in (HOURLY, DAILY):
                            reason, cost, factor, stormy = self._slot_weather(
                                place, day, tier, weather, s_lo, s_hi)
                        if reason:
                            build.weather_blocked[p_idx].append(f"{day:%d/%m} {s_lo // 60:02d}h ({reason})"
                                                                if tier == HOURLY else f"{day:%d/%m} ({reason})")
                            continue
                        offset = d_idx * MINUTES_PER_DAY
                        build.copies.append(_Copy(
                            place=p_idx, day=d_idx, earliest=offset + s_lo, latest=offset + s_hi,
                            weather_cost=round(opt.weather_weight * cost * visit / 60), factor=factor,
                            arrival_ref=_at(day, (s_lo + s_hi) // 2), stormy=stormy,
                        ))
        return build

    # -- giải --------------------------------------------------------------

    def optimize(self, places: list[Place], constraint: UserConstraint,
                 weather: GridWeather | None = None) -> ScheduleResult:
        opt = self.options
        days = trip_days(constraint)
        trip_start = datetime.combine(days[0][0], time())
        build = self._build_copies(places, days, weather)
        copies = build.copies

        # Nút 0: điểm xuất phát, 1: điểm kết thúc, 2..: bản sao địa điểm.
        locations = [constraint.start_location, constraint.end_location] + [(p.lat, p.lon) for p in places]
        departure_times = [
            datetime.combine(day, time(h), tzinfo=LOCAL_TZ)
            for day, lo, hi in days for h in opt.traffic_hours if lo <= h * 60 <= hi
        ] or [datetime.combine(day, time(), tzinfo=LOCAL_TZ) + timedelta(minutes=lo) for day, lo, _ in days]
        matrix = TravelMatrix(locations, constraint.transport_mode, maps_client=self.maps_client,
                              departure_times=departure_times)
        self.last_matrix_source = matrix.source
        loc = [0, 1] + [2 + c.place for c in copies]
        n = len(loc)
        service = [0, 0] + [places[c.place].avg_visit_minutes for c in copies]
        factor_in = [1.0, 1.0] + [c.factor for c in copies]
        weather_in = [0, 0] + [c.weather_cost for c in copies]
        when = [None, None] + [c.arrival_ref for c in copies]
        stormy = [False, False] + [c.stormy for c in copies]

        def leg_ref(i: int, j: int) -> int:
            """Nút đại diện cho giờ và thời tiết của cung i -> j: điểm đến, hoặc điểm vừa rời nếu về khách sạn."""
            return i if j == 1 else j

        def travel(i: int, j: int) -> int:
            k = leg_ref(i, j)
            at = when[k] + timedelta(minutes=service[k]) if (j == 1 and when[k]) else when[k]
            return matrix.minutes(loc[i], loc[j], factor_in[k], at=at)

        flood_hits: dict[tuple[int, int], list[FloodZone]] = {}

        def flooded(i: int, j: int) -> list[FloodZone]:
            if not opt.flood_zones or not stormy[leg_ref(i, j)]:
                return []
            key = (loc[i], loc[j])
            if key not in flood_hits:
                flood_hits[key] = zones_on_path([locations[loc[i]], locations[loc[j]]], opt.flood_zones)
            return flood_hits[key]

        time_matrix = [[0] * n for _ in range(n)]
        cost_matrix = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i != j:
                    t = travel(i, j)
                    time_matrix[i][j] = service[i] + t
                    risk = opt.flood_penalty_minutes if flooded(i, j) else 0
                    cost_matrix[i][j] = t + weather_in[j] + risk

        num_days = len(days)
        manager = pywrapcp.RoutingIndexManager(n, num_days, [0] * num_days, [1] * num_days)
        routing = pywrapcp.RoutingModel(manager)
        routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitMatrix(cost_matrix))

        routing.AddDimension(routing.RegisterTransitMatrix(time_matrix), MINUTES_PER_DAY,
                             num_days * MINUTES_PER_DAY, False, "Time")
        time_dim = routing.GetDimensionOrDie("Time")
        for d, (_, lo, hi) in enumerate(days):
            offset = d * MINUTES_PER_DAY
            time_dim.CumulVar(routing.Start(d)).SetRange(offset + lo, offset + hi)
            time_dim.CumulVar(routing.End(d)).SetRange(offset + lo, offset + hi)

        by_place: dict[int, list[int]] = defaultdict(list)
        for k, c in enumerate(copies):
            index = manager.NodeToIndex(k + 2)
            time_dim.CumulVar(index).SetRange(c.earliest, c.latest)
            routing.VehicleVar(index).SetValues([-1, c.day])
            by_place[c.place].append(index)
        for p_idx, indices in by_place.items():
            routing.AddDisjunction(indices, max(1, int(opt.visit_value * places[p_idx].priority)), 1)

        count = routing.RegisterUnaryTransitVector([0, 0] + [1] * len(copies))
        routing.AddDimensionWithVehicleCapacity(count, 0, [constraint.max_places_per_day] * num_days,
                                                True, "Count")
        if constraint.budget_vnd is not None:
            prices = routing.RegisterUnaryTransitVector(
                [0, 0] + [places[c.place].ticket_price_vnd or 0 for c in copies])
            routing.AddDimensionWithVehicleCapacity(prices, 0, [constraint.budget_vnd] * num_days,
                                                    True, "Budget")
            budget_dim = routing.GetDimensionOrDie("Budget")
            solver = routing.solver()
            solver.Add(solver.Sum([budget_dim.CumulVar(routing.End(d)) for d in range(num_days)])
                       <= constraint.budget_vnd)

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.FromMilliseconds(int(opt.time_limit_s * 1000))
        solution = routing.SolveWithParameters(params)

        result = ScheduleResult(solver_status=_STATUS.get(routing.status(), str(routing.status())))
        if solution:
            result.objective = solution.ObjectiveValue()
        visited: set[int] = set()
        for d, (day, _, _) in enumerate(days):
            tier = weather.tier(day) if (opt.weather_aware and weather is not None) else NONE
            itinerary = DayItinerary(date=day, weather_tier=tier)
            if opt.weather_aware:
                itinerary.notes.extend(weather.notes(day) if weather is not None else
                                       [f"{day:%d/%m}: không có dữ liệu thời tiết; lịch chưa tính thời tiết."])
            if d == 0:
                itinerary.notes.extend(matrix.notes)
            route = [manager.IndexToNode(i) for i in self._route(routing, solution, d)] if solution else []
            prev = 0
            for node in route:
                copy = copies[node - 2]
                place = places[copy.place]
                visited.add(copy.place)
                arrival = trip_start + timedelta(minutes=solution.Min(time_dim.CumulVar(manager.NodeToIndex(node))))
                departure = arrival + timedelta(minutes=place.avg_visit_minutes)
                itinerary.visits.append(ScheduledVisit(
                    place_id=place.id, place_name=place.name, arrival_time=arrival, departure_time=departure,
                    travel_minutes_from_prev=travel(prev, node),
                    travel_distance_km=round(matrix.distance_km[loc[prev]][loc[node]], 2),
                    indoor_ratio=place.indoor_ratio,
                    weather_note=(_weather_note(place, weather, arrival, departure)
                                  if opt.weather_aware and weather is not None else None),
                    google_maps_url=GoogleMapsClient.generate_deep_link(
                        locations[loc[prev]], locations[loc[node]], transport_mode=constraint.transport_mode,
                        navigate=True),
                    travel_advice=_travel_advice(constraint.transport_mode, stormy[node],
                                                 flooded(prev, node), locations[loc[prev]], locations[loc[node]]),
                ))
                prev = node
            if route:
                itinerary.navigation_urls = GoogleMapsClient.day_links(
                    [constraint.start_location] + [locations[loc[node]] for node in route]
                    + [constraint.end_location], constraint.transport_mode)
                unknown = [places[copies[node - 2].place].name for node in route
                           if copies[node - 2].place in build.unknown_hours]
                if unknown:
                    itinerary.notes.append("Chưa có giờ mở cửa trong CSDL, nên kiểm tra trước: "
                                           + ", ".join(unknown) + ".")
            result.itineraries.append(itinerary)

        for p_idx, place in enumerate(places):
            if build.weather_blocked.get(p_idx):
                result.weather_adaptations.append(
                    f"{place.name}: tránh " + "; ".join(build.weather_blocked[p_idx]))
            if p_idx in visited:
                continue
            if p_idx in by_place:
                result.unvisited[place.id] = "không xếp vừa lịch (thời gian, số điểm mỗi ngày hoặc ngân sách)"
            elif build.weather_blocked.get(p_idx):
                result.unvisited[place.id] = "thời tiết nguy hiểm ở mọi khung giờ có thể đi"
            elif build.closed.get(p_idx):
                result.unvisited[place.id] = "đóng cửa: " + ", ".join(build.closed[p_idx])
            else:
                result.unvisited[place.id] = "khung giờ mở cửa quá ngắn so với thời gian tham quan"
        return result

    @staticmethod
    def _route(routing, solution, vehicle: int) -> list[int]:
        """Các index nút thăm (bỏ điểm đầu/cuối) trên lộ trình của một ngày."""
        out = []
        index = solution.Value(routing.NextVar(routing.Start(vehicle)))
        while not routing.IsEnd(index):
            out.append(index)
            index = solution.Value(routing.NextVar(index))
        return out


def _stormy(w: HourlyWeather) -> bool:
    return bool(rules.hazards(w) & {rules.MUA_LON, rules.BAO})


def _travel_advice(mode: str, stormy: bool, zones: list[FloodZone], origin, destination) -> str | None:
    """Lời khuyên cho chặng đi: đổi phương tiện khi mưa lớn/bão, cảnh báo vùng ngập."""
    parts = []
    if zones:
        parts.append("Đoạn đường có thể đi qua vùng dễ ngập: " + ", ".join(z.name for z in zones)
                     + "; nên hỏi đường tránh hoặc đợi ngớt mưa.")
    if stormy and mode == "motorbike":
        parts.append("Dự báo mưa lớn/bão lúc di chuyển: nên đi taxi hoặc ô tô chặng này ("
                     + GoogleMapsClient.generate_deep_link(origin, destination, transport_mode="car",
                                                           navigate=True) + ").")
    return " ".join(parts) or None


def _weather_note(place: Place, weather: GridWeather, arrival: datetime, departure: datetime) -> str | None:
    """Lời nhắc thời tiết cho người dùng; không nhắc với điểm gần như trong nhà."""
    if place.indoor_ratio >= 0.8:
        return None
    samples: list[HourlyWeather] = [w for t, _ in hours_between(arrival, departure)
                                    if (w := weather.at(place.lat, place.lon, t)) is not None]
    rainy = [w for w in samples if rules.is_raining(w)]
    if rainy:
        amount = max(w.precipitation_mm for w in rainy)
        if rainy[0].period_hours >= 24:
            return f"Dự báo ngày có mưa (~{amount:.0f} mm); mang áo mưa."
        return f"Dự báo mưa tới {amount:.1f} mm/h trong lúc tham quan; mang áo mưa."
    if any(rules.heat_score(w) >= 0.5 for w in samples):
        return "Dự báo nắng nóng; mang nước và mũ."
    return None
