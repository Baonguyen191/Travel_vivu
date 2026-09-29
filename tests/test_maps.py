"""Tests cho tích hợp Google Maps (Routes API, Maps URLs, vùng ngập). Không gọi mạng."""

import json
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from maps.client import (
    MATRIX_BLOCK, ROUTE_MATRIX_URL, ROUTES_URL, GoogleMapsClient, decode_polyline, estimate_matrix,
)
from maps.flood import FloodZone, load_flood_zones, zones_on_path
from planner.matrix import TravelMatrix
from planner.models import HourlyWeather, Place, UserConstraint
from planner.routes import attach_routes
from planner.solver import SolverOptions, WeatherAwareScheduleOptimizer
from planner.weather import HOURLY, LOCAL_TZ, GridWeather

HOTEL = (16.4637, 107.5909)
HOANG_THANH = (16.4694, 107.5778)
THIEN_MU = (16.4536, 107.5448)
FUTURE = datetime.now(LOCAL_TZ) + timedelta(days=2)


class RoutesMock:
    """Giả lập Routes API: ghi lại request, trả ma trận theo khoảng cách chỉ số."""

    def __init__(self, status: int = 200, routes: list[dict] | None = None, not_found: set | None = None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.routes = routes or []
        self.not_found = not_found or set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "API key not valid"}})
        body = json.loads(request.content)
        if str(request.url) == ROUTE_MATRIX_URL:
            return httpx.Response(200, json=[
                {"originIndex": i, "destinationIndex": j,
                 "condition": "ROUTE_NOT_FOUND" if (i, j) in self.not_found else "ROUTE_EXISTS",
                 "distanceMeters": 1000 * (i + j + 1), "duration": f"{600 * (i + j + 1)}s",
                 "staticDuration": f"{300 * (i + j + 1)}s"}
                for i in range(len(body["origins"])) for j in range(len(body["destinations"]))
            ])
        return httpx.Response(200, json={"routes": self.routes})

    def body(self, k: int = -1) -> dict:
        return json.loads(self.requests[k].content)

    def client(self, **kw) -> GoogleMapsClient:
        return GoogleMapsClient(api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(self)), **kw)


# -- client: ma trận -------------------------------------------------------------


def test_disabled_without_key_returns_estimate_with_note(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    client = GoogleMapsClient()
    assert not client.is_enabled
    assert not GoogleMapsClient(api_key="your_google_maps_api_key_here").is_enabled
    result = client.route_matrix([HOTEL], [THIEN_MU])
    assert result.source == "estimate" and result.minutes[0][0] > 0
    assert "GOOGLE_MAPS_API_KEY" in result.notes[0]


def test_route_matrix_request_format_and_parsing():
    mock = RoutesMock()
    result = mock.client().route_matrix([HOTEL, HOANG_THANH], [THIEN_MU], FUTURE, "motorbike")

    request = mock.requests[0]
    assert str(request.url) == ROUTE_MATRIX_URL
    assert request.headers["X-Goog-Api-Key"] == "test-key"
    assert "staticDuration" in request.headers["X-Goog-FieldMask"]
    assert "key=" not in str(request.url)  # key nằm ở header, không lộ trong URL/log
    body = mock.body()
    assert body["travelMode"] == "DRIVE" and body["routingPreference"] == "TRAFFIC_AWARE"
    assert body["departureTime"].endswith("Z")
    assert body["origins"][0]["waypoint"]["location"]["latLng"] == {"latitude": HOTEL[0], "longitude": HOTEL[1]}

    assert result.source == "google"
    assert result.minutes == [[10], [20]] and result.static_minutes == [[5], [10]]
    assert result.distance_km == [[1.0], [2.0]]
    assert result.congestion(1, 0) == 2.0


def test_two_wheeler_mode_and_past_departure_omitted():
    mock = RoutesMock()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    mock.client(motorbike_mode="TWO_WHEELER").route_matrix([HOTEL], [THIEN_MU], past, "motorbike")
    body = mock.body()
    assert body["travelMode"] == "TWO_WHEELER"
    assert "departureTime" not in body  # Routes API chỉ nhận giờ quá khứ với TRANSIT


def test_naive_departure_time_rejected():
    with pytest.raises(ValueError, match="múi giờ"):
        RoutesMock().client().route_matrix([HOTEL], [THIEN_MU], datetime(2030, 1, 1, 8))


def test_route_not_found_keeps_estimate_for_that_cell():
    mock = RoutesMock(not_found={(0, 1)})
    result = mock.client().route_matrix([HOTEL], [THIEN_MU, HOANG_THANH], FUTURE)
    estimate = estimate_matrix([HOTEL], [HOANG_THANH], "motorbike")
    assert result.minutes[0][0] == 10
    assert result.minutes[0][1] == estimate.minutes[0][0]


def test_route_matrix_blocks_cache_and_budget():
    points = [(16.40 + i * 0.002, 107.55) for i in range(MATRIX_BLOCK + 5)]
    mock = RoutesMock()
    client = mock.client(max_billable_elements=10_000)
    client.route_matrix(points, points, FUTURE)
    sizes = [(len(mock.body(k)["origins"]), len(mock.body(k)["destinations"])) for k in range(len(mock.requests))]
    assert all(o * d <= 625 for o, d in sizes) and len(sizes) == 4

    calls = len(mock.requests)
    client.route_matrix(points[:3], points[:3], FUTURE)  # đã có trong cache của cùng mốc giờ
    assert len(mock.requests) == calls

    capped = RoutesMock().client(max_billable_elements=3)
    result = capped.route_matrix([HOTEL, HOANG_THANH], [THIEN_MU, HOTEL], FUTURE)
    assert result.source == "estimate" and "trần" in result.notes[0]


def test_http_error_falls_back_with_reason():
    result = RoutesMock(status=403).client().route_matrix([HOTEL], [THIEN_MU], FUTURE)
    assert result.source == "estimate"
    assert "API key not valid" in result.notes[0]


def test_matrix_error_in_list_shape_is_reported():
    # computeRouteMatrix trả lỗi dạng mảng [{"error": {...}}] (gặp thật khi Routes API chưa bật).
    def handler(request):
        return httpx.Response(403, json=[{"error": {"code": 403, "message": "Routes API has not been used"}}])

    client = GoogleMapsClient(api_key="k", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.route_matrix([HOTEL], [THIEN_MU], FUTURE)
    assert result.source == "estimate"
    assert "Routes API has not been used" in result.notes[0]


# -- client: tuyến đường, deep link ---------------------------------------------


def test_compute_routes_parses_options():
    mock = RoutesMock(routes=[
        {"distanceMeters": 4200, "duration": "900s", "staticDuration": "780s",
         "polyline": {"encodedPolyline": "_p~iF~ps|U"}, "routeLabels": ["DEFAULT_ROUTE"]},
        {"distanceMeters": 5000, "duration": "1080s", "polyline": {"encodedPolyline": "abc"},
         "routeLabels": ["DEFAULT_ROUTE_ALTERNATE"]},
    ])
    options = mock.client().compute_routes(HOTEL, THIEN_MU, FUTURE, alternatives=True)
    assert str(mock.requests[0].url) == ROUTES_URL
    assert mock.body()["computeAlternativeRoutes"] is True
    assert [(o.minutes, o.static_minutes) for o in options] == [(15, 13), (18, 18)]
    assert options[0].polyline == "_p~iF~ps|U" and options[1].labels == ["DEFAULT_ROUTE_ALTERNATE"]


def test_decode_polyline_reference_example():
    # Ví dụ trong tài liệu Encoded Polyline Algorithm Format của Google.
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [
        (38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_deep_link_uses_two_wheeler_and_encodes_waypoints():
    link = GoogleMapsClient.generate_deep_link(HOTEL, THIEN_MU, [HOANG_THANH, (16.47, 107.58)], navigate=True)
    assert link.startswith("https://www.google.com/maps/dir/?api=1")
    q = parse_qs(urlsplit(link).query)
    assert q["travelmode"] == ["two-wheeler"] and q["dir_action"] == ["navigate"]
    assert q["waypoints"] == ["16.4694,107.5778|16.47,107.58"]
    assert "%7C" in link


def test_day_links_split_by_waypoint_limit():
    stops = [HOTEL] + [(16.40 + i * 0.01, 107.55) for i in range(12)] + [HOTEL]
    links = GoogleMapsClient.day_links(stops, "car")
    assert len(links) == 2
    first, second = (parse_qs(urlsplit(link).query) for link in links)
    assert len(first["waypoints"][0].split("|")) == 9
    assert first["destination"] == second["origin"]
    assert second["destination"] == [f"{HOTEL[0]},{HOTEL[1]}"]


# -- vùng ngập -------------------------------------------------------------------

ZONE = FloodZone("Đường ven sông (thử)", 16.4600, 107.5700, 400, "test")


def test_zones_on_path():
    assert zones_on_path([(16.4600, 107.5600), (16.4600, 107.5800)], [ZONE]) == [ZONE]
    assert zones_on_path([(16.4800, 107.5600), (16.4800, 107.5800)], [ZONE]) == []


def test_flood_zone_config(tmp_path):
    assert load_flood_zones("config/flood_zones.yml") == []  # chưa có nguồn dữ liệu
    path = tmp_path / "z.yml"
    path.write_text("- {name: A, lat: 16.46, lon: 107.57, radius_m: 300}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source"):
        load_flood_zones(path)


# -- tích hợp planner ----------------------------------------------------------------


class _BucketClient:
    """Client giả: ma trận khác nhau theo giờ khởi hành (cao điểm 17h chậm gấp đôi)."""

    is_enabled = True

    def __init__(self):
        self.calls = []

    def route_matrix(self, origins, destinations, departure_time=None, transport_mode="motorbike"):
        from maps.client import MatrixResult

        self.calls.append(departure_time)
        slow = 2 if departure_time.hour == 17 else 1
        n = len(origins)
        mins = [[0 if i == j else 10 * slow for j in range(n)] for i in range(n)]
        return MatrixResult([[1.0] * n for _ in range(n)], mins, [[10] * n for _ in range(n)], "google")


def test_travel_matrix_picks_nearest_traffic_bucket():
    day = date(2030, 1, 7)
    buckets = [datetime.combine(day, time(h), tzinfo=LOCAL_TZ) for h in (8, 12, 17)]
    client = _BucketClient()
    m = TravelMatrix([HOTEL, THIEN_MU], maps_client=client, departure_times=buckets)
    assert m.source == "google" and len(client.calls) == 3
    assert m.minutes(0, 1, at=datetime.combine(day, time(9))) == 10
    assert m.minutes(0, 1, at=datetime.combine(day, time(16, 30))) == 20
    assert m.minutes(0, 1, 1.5, at=datetime.combine(day, time(16, 30))) == 30


def _plan(places, rain_mm, zones=(), maps_client=None):
    day = date(2030, 1, 7)
    weather = GridWeather.uniform(
        [HourlyWeather(datetime.combine(day, time(h)), precipitation_mm=rain_mm) for h in range(24)], HOURLY)
    c = UserConstraint(datetime.combine(day, time(8)), datetime.combine(day, time(18)), HOTEL, HOTEL)
    opts = SolverOptions(time_limit_s=0.5, flood_zones=tuple(zones))
    return WeatherAwareScheduleOptimizer(opts, maps_client=maps_client).optimize(places, c, weather), c, weather


MUSEUM = Place("bt", "Bảo tàng", 16.4600, 107.5550, indoor_ratio=0.95)  # cách tâm vùng ngập ~1.6 km


def test_solver_uses_google_traffic_matrix_when_enabled():
    client = _BucketClient()
    result, _, _ = _plan([MUSEUM], 0.0, maps_client=client)
    assert client.calls and all(t.tzinfo is not None for t in client.calls)
    assert result.visits[0].travel_minutes_from_prev in (10, 20)


def test_storm_leg_advises_car_and_flags_flood_zone():
    result, _, _ = _plan([MUSEUM], 9.0, zones=[ZONE])  # 9 mm/h: mưa lớn
    [visit] = result.visits
    assert "vùng dễ ngập" in visit.travel_advice
    assert "taxi hoặc ô tô" in visit.travel_advice and "travelmode=driving" in visit.travel_advice

    dry, _, _ = _plan([MUSEUM], 0.0, zones=[ZONE])
    assert dry.visits[0].travel_advice is None


def test_attach_routes_prefers_alternative_outside_flood_zone():
    def encode(points):
        out, prev_lat, prev_lon = [], 0, 0
        for lat, lon in points:
            for value, prev in ((round(lat * 1e5), prev_lat), (round(lon * 1e5), prev_lon)):
                v = value - prev
                v = ~(v << 1) if v < 0 else v << 1
                while v >= 0x20:
                    out.append(chr((0x20 | (v & 0x1F)) + 63))
                    v >>= 5
                out.append(chr(v + 63))
            prev_lat, prev_lon = round(lat * 1e5), round(lon * 1e5)
        return "".join(out)

    wet_path = encode([HOTEL, (16.4600, 107.5700), (MUSEUM.lat, MUSEUM.lon)])
    dry_path = encode([HOTEL, (16.4800, 107.5800), (16.4800, 107.5550), (MUSEUM.lat, MUSEUM.lon)])
    mock = RoutesMock(routes=[
        {"distanceMeters": 3000, "duration": "600s", "polyline": {"encodedPolyline": wet_path}},
        {"distanceMeters": 4000, "duration": "780s", "polyline": {"encodedPolyline": dry_path}},
    ])
    result, constraint, weather = _plan([MUSEUM], 9.0, zones=[ZONE])
    done = attach_routes(result, {MUSEUM.id: MUSEUM}, constraint, mock.client(), weather, [ZONE])

    [visit] = result.visits
    assert done == 2  # chặng đi và chặng về
    assert visit.route_polyline == dry_path
    assert "chọn tuyến khác, lâu hơn +3 phút" in visit.route_note
    assert mock.body(0)["computeAlternativeRoutes"] is True
