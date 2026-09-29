"""Tests cho client OSRM (mock HTTP theo định dạng API OSRM v5/v6)."""

from datetime import date, datetime, time

import httpx
import pytest

from maps.client import RoutesError
from maps.osrm import OsrmClient
from maps.routing import choose_routing
from planner.matrix import BUFFER_MINUTES, TravelMatrix
from planner.models import Place, UserConstraint
from planner.solver import SolverOptions, WeatherAwareScheduleOptimizer

HOTEL = (16.4637, 107.5909)
THIEN_MU = (16.4536, 107.5448)
DAI_NOI = (16.4694, 107.5778)


class OsrmMock:
    def __init__(self, fail: bool = False):
        self.requests: list[httpx.Request] = []
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.fail:
            return httpx.Response(400, json={"code": "InvalidQuery", "message": "Query string malformed"})
        if path.startswith("/nearest"):
            return httpx.Response(200, json={"code": "Ok", "waypoints": []})
        if path.startswith("/table"):
            sources = request.url.params["sources"].split(";")
            dests = request.url.params["destinations"].split(";")
            return httpx.Response(200, json={
                "code": "Ok",
                "durations": [[None if (s, d) == ("0", "2") and len(dests) > 1 else 600.0 * (int(s) + 1)
                               for d in dests] for s in sources],
                "distances": [[5000.0 * (int(s) + 1) for d in dests] for s in sources],
            })
        return httpx.Response(200, json={"code": "Ok", "routes": [
            {"distance": 6100.0, "duration": 900.0, "geometry": "_p~iF~ps|U"},
            {"distance": 7000.0, "duration": 1000.0, "geometry": "abc"},
        ]})

    def client(self) -> OsrmClient:
        return OsrmClient("http://osrm.test", http_client=httpx.Client(transport=httpx.MockTransport(self)))


def test_is_enabled_pings_once():
    mock = OsrmMock()
    client = mock.client()
    assert client.is_enabled and client.is_enabled
    assert len(mock.requests) == 1


def test_unreachable_server_is_disabled_with_note():
    def refuse(request):
        raise httpx.ConnectError("refused")

    client = OsrmClient("http://osrm.test", http_client=httpx.Client(transport=httpx.MockTransport(refuse)))
    assert not client.is_enabled
    assert "up -d osrm" in client.disabled_note


def test_route_matrix_request_and_units():
    mock = OsrmMock()
    result = mock.client().route_matrix([HOTEL, DAI_NOI], [THIEN_MU], transport_mode="motorbike")
    request = mock.requests[0]
    assert request.url.path.startswith("/table/v1/driving/107.590900,16.463700;107.577800,16.469400;")
    assert request.url.params["annotations"] == "duration,distance"
    assert result.source == "osrm" and result.distance_km == [[5.0], [10.0]]
    # max(thời gian OSRM, quãng đường / 25 km/h): 5 km -> max(10, 12) phút; 10 km -> max(20, 24)
    assert result.minutes == [[12 + BUFFER_MINUTES], [24 + BUFFER_MINUTES]]
    assert result.minutes == result.static_minutes  # không có giao thông
    assert "OSRM" in result.notes[0]


def test_route_matrix_ignores_departure_time_and_caches():
    mock = OsrmMock()
    client = mock.client()
    client.route_matrix([HOTEL], [THIEN_MU], datetime(2030, 1, 1, 8))
    client.route_matrix([HOTEL], [THIEN_MU], datetime(2030, 1, 1, 17))
    assert len(mock.requests) == 1


def test_walking_uses_distance_and_null_cells_keep_estimate():
    mock = OsrmMock()
    result = mock.client().route_matrix([HOTEL], [DAI_NOI, THIEN_MU], transport_mode="walking")
    assert result.minutes[0][0] == 67 + BUFFER_MINUTES  # 5 km / 4.5 km/h
    # ô durations = null (không có đường) giữ ước lượng
    assert result.distance_km[0][1] != 5.0


def test_server_error_falls_back_to_estimate():
    result = OsrmMock(fail=True).client().route_matrix([HOTEL], [THIEN_MU])
    assert result.source == "estimate"
    assert "malformed" in result.notes[0]


def test_compute_routes():
    mock = OsrmMock()
    options = mock.client().compute_routes(HOTEL, THIEN_MU, alternatives=True)
    assert mock.requests[0].url.params["alternatives"] == "true"
    assert [(o.minutes, round(o.distance_km, 1)) for o in options] == [(15 + BUFFER_MINUTES, 6.1),
                                                                       (17 + BUFFER_MINUTES, 7.0)]
    assert options[0].polyline == "_p~iF~ps|U"
    with pytest.raises(RoutesError):
        OsrmMock(fail=True).client().compute_routes(HOTEL, THIEN_MU)


def test_travel_matrix_and_solver_use_osrm():
    client = OsrmMock().client()
    m = TravelMatrix([HOTEL, THIEN_MU], maps_client=client,
                     departure_times=[datetime(2030, 1, 7, 8), datetime(2030, 1, 7, 12)])
    assert m.source == "osrm" and m.distance_km[0][1] == 5.0

    day = date(2030, 1, 7)
    c = UserConstraint(datetime.combine(day, time(8)), datetime.combine(day, time(18)), HOTEL, HOTEL)
    optimizer = WeatherAwareScheduleOptimizer(SolverOptions(time_limit_s=0.3), maps_client=client)
    result = optimizer.optimize([Place("tm", "Chùa Thiên Mụ", *THIEN_MU)], c)
    assert optimizer.last_matrix_source == "osrm"
    assert result.visits[0].travel_distance_km == 5.0


def test_choose_routing(monkeypatch):
    monkeypatch.setattr(OsrmClient, "is_enabled", property(lambda self: True))
    assert isinstance(choose_routing(), OsrmClient)
    monkeypatch.setattr(OsrmClient, "is_enabled", property(lambda self: False))
    assert choose_routing(google_key="") is None
    assert choose_routing("google", google_key="k").is_enabled
    assert choose_routing("estimate") is None
