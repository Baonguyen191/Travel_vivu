"""OSRM tự host: ma trận thời gian đi và tuyến đường theo đường thật, miễn phí.

Cùng giao diện với `maps.client.GoogleMapsClient` (`is_enabled`, `route_matrix`,
`compute_routes`, `billed_elements`) để solver và `attach_routes` dùng thay thế.

Khác Google: không có giao thông, nên `minutes == static_minutes` và giờ khởi
hành không ảnh hưởng kết quả (cache bỏ qua giờ). Dữ liệu dựng bằng profile
`car` (scripts/setup_osrm.py), mà tốc độ mặc định của profile này là tốc độ
đường thông thoáng: 15,5 km ra biển Thuận An chỉ 18 phút (~60 km/h), quá nhanh
cho giao thông hỗn hợp ở Huế. Vì vậy thời gian đi = max(thời gian OSRM, quãng
đường OSRM / tốc độ trung bình của phương tiện trong planner.matrix.SPEED_KMH):
giữ quãng đường thật, không để thời gian lạc quan hơn tốc độ đô thị.
"""

import math
import os
from datetime import datetime
from typing import Sequence

import httpx

from maps.client import LatLng, MatrixResult, RouteOption, RoutesError, estimate_matrix
from planner.matrix import BUFFER_MINUTES, SPEED_KMH

DEFAULT_URL = "http://localhost:5100"
MAX_TABLE = 100  # --max-table-size mặc định của osrm-routed


def _coords(points: Sequence[LatLng]) -> str:
    return ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)


class OsrmClient:
    billed_elements = 0

    def __init__(self, base_url: str | None = None, http_client: httpx.Client | None = None):
        self.base_url = (base_url or os.environ.get("OSRM_URL") or DEFAULT_URL).rstrip("/")
        self._http = http_client or httpx.Client(timeout=30.0)
        self._enabled: bool | None = None
        self._cache: dict[tuple, tuple[float, int]] = {}

    @property
    def is_enabled(self) -> bool:
        """Server có trả lời không (kiểm tra một lần mỗi client)."""
        if self._enabled is None:
            try:
                resp = self._http.get(f"{self.base_url}/nearest/v1/driving/107.59,16.46", timeout=2.0)
                self._enabled = resp.status_code == 200 and resp.json().get("code") == "Ok"
            except (httpx.HTTPError, ValueError):
                self._enabled = False
        return self._enabled

    @property
    def disabled_note(self) -> str:
        return (f"Không kết nối được OSRM ở {self.base_url} (chạy `docker compose --profile routing up -d osrm`);"
                " thời gian đi là ước lượng.")

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = self._http.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise RoutesError(f"OSRM không trả lời: {exc}") from exc
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or data.get("code") != "Ok":
            raise RoutesError(f"OSRM {resp.status_code}: {data.get('message') or data.get('code') or resp.text[:200]}")
        return data

    def route_matrix(self, origins: Sequence[LatLng], destinations: Sequence[LatLng],
                     departure_time: datetime | None = None, transport_mode: str = "motorbike") -> MatrixResult:
        fallback = estimate_matrix(origins, destinations, transport_mode)
        result = MatrixResult([r[:] for r in fallback.distance_km], [r[:] for r in fallback.minutes],
                              [r[:] for r in fallback.static_minutes], "osrm")
        points = list(dict.fromkeys(list(origins) + list(destinations)))
        index = {p: i for i, p in enumerate(points)}
        missing = [(i, j) for i, o in enumerate(origins) for j, d in enumerate(destinations)
                   if o != d and (o, d) not in self._cache]
        if missing:
            if len(points) > MAX_TABLE:
                raise RoutesError(f"OSRM table tối đa {MAX_TABLE} điểm, nhận {len(points)}")
            try:
                data = self._get(f"/table/v1/driving/{_coords(points)}", {
                    "sources": ";".join(str(index[o]) for o in origins),
                    "destinations": ";".join(str(index[d]) for d in destinations),
                    "annotations": "duration,distance",
                })
            except RoutesError as exc:
                fallback.notes.append(f"{exc}; thời gian đi là ước lượng.")
                return fallback
            for i, o in enumerate(origins):
                for j, d in enumerate(destinations):
                    seconds, meters = data["durations"][i][j], data["distances"][i][j]
                    if seconds is not None and meters is not None:
                        self._cache[(o, d)] = (meters / 1000.0, seconds)
        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                if o == d:
                    result.distance_km[i][j], result.minutes[i][j], result.static_minutes[i][j] = 0.0, 0, 0
                elif (o, d) in self._cache:
                    km, seconds = self._cache[(o, d)]
                    minutes = self._minutes(km, seconds, transport_mode)
                    result.distance_km[i][j], result.minutes[i][j], result.static_minutes[i][j] = km, minutes, minutes
        result.notes.append("Thời gian đi theo đường thật (OSRM, bản đồ OpenStreetMap), không tính giao thông.")
        return result

    @staticmethod
    def _minutes(km: float, seconds: float, transport_mode: str) -> int:
        at_urban_speed = km / SPEED_KMH.get(transport_mode, SPEED_KMH["motorbike"]) * 60
        travel = at_urban_speed if transport_mode == "walking" else max(seconds / 60, at_urban_speed)
        m = math.ceil(travel)
        return m + BUFFER_MINUTES if m > 0 else 0

    def compute_routes(self, origin: LatLng, destination: LatLng, departure_time: datetime | None = None,
                       transport_mode: str = "motorbike", alternatives: bool = False) -> list[RouteOption]:
        data = self._get(f"/route/v1/driving/{_coords([origin, destination])}", {
            "overview": "simplified", "geometries": "polyline", "alternatives": str(alternatives).lower()})
        options = []
        for k, r in enumerate(data.get("routes", [])):
            km = r["distance"] / 1000.0
            minutes = self._minutes(km, r["duration"], transport_mode)
            options.append(RouteOption(km, minutes, minutes, r.get("geometry", ""),
                                       ["DEFAULT_ROUTE" if k == 0 else "DEFAULT_ROUTE_ALTERNATE"]))
        return options
