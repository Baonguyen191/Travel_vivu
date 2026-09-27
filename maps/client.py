"""Google Maps Platform: Routes API (ma trận thời gian đi, tuyến đường) và Maps URLs.

- Ma trận: `computeRouteMatrix` của Routes API. Distance Matrix API cũ đã ở
  trạng thái Legacy, Google khuyến nghị chuyển sang Compute Route Matrix.
- Tuyến đường: `computeRoutes`, lấy polyline và tuyến thay thế.
- Dẫn đường: Maps URLs (không cần key, không tính phí).

Chi phí: ma trận tính phí theo phần tử (điểm đi × điểm đến); `TRAFFIC_AWARE` tính
theo SKU Pro, chế độ `TWO_WHEELER` theo SKU Enterprise. Vì vậy mặc định xe máy
dùng DRIVE, và mỗi client có trần số phần tử (`max_billable_elements`); vượt
trần thì trả về ước lượng kèm ghi chú thay vì gọi tiếp.

Cache: chỉ trong bộ nhớ của một client, để một lần lập lịch không gọi trùng.
Không lưu kết quả ra đĩa: điều khoản Google Maps Platform hạn chế lưu trữ nội
dung trả về.
"""

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MAPS_DIR_URL = "https://www.google.com/maps/dir/"
MATRIX_BLOCK = 25  # 25 × 25 = 625 phần tử, giới hạn mỗi request (không dùng TRAFFIC_AWARE_OPTIMAL)
PLACEHOLDER_KEY = "your_google_maps_api_key_here"
# Maps URLs: tối đa 9 waypoint (3 trên trình duyệt di động).
MAX_URL_WAYPOINTS = 9

# transport_mode của planner -> travelmode của Maps URLs
URL_TRAVEL_MODE = {"motorbike": "two-wheeler", "car": "driving", "walking": "walking",
                   "bicycling": "bicycling"}

LatLng = tuple[float, float]


class RoutesError(RuntimeError):
    pass


@dataclass
class MatrixResult:
    distance_km: list[list[float]]
    minutes: list[list[int]]  # có tính giao thông nếu traffic_aware
    static_minutes: list[list[int]]  # không tính giao thông
    source: str  # "google" | "estimate"
    notes: list[str] = field(default_factory=list)

    def congestion(self, i: int, j: int) -> float:
        """Tỷ lệ thời gian có giao thông / không giao thông (1.0 = đường thông)."""
        static = self.static_minutes[i][j]
        return self.minutes[i][j] / static if static else 1.0


@dataclass
class RouteOption:
    distance_km: float
    minutes: int
    static_minutes: int
    polyline: str  # encoded polyline
    labels: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    description: str = ""


def _seconds(duration: str | None) -> int:
    """'123s' -> 123."""
    if not duration:
        return 0
    return int(float(duration.rstrip("s")))


def _waypoint(p: LatLng) -> dict:
    return {"waypoint": {"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}}}


def _rfc3339(t: datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("departure_time cần có múi giờ (vd. Asia/Ho_Chi_Minh, UTC+7)")
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def estimate_matrix(origins: Sequence[LatLng], destinations: Sequence[LatLng],
                    transport_mode: str) -> MatrixResult:
    """Ước lượng khi không gọi Google: đường chim bay × hệ số đường vòng / vận tốc."""
    from planner.matrix import BUFFER_MINUTES, ROAD_FACTOR, SPEED_KMH, haversine_distance_km

    speed = SPEED_KMH.get(transport_mode, SPEED_KMH["motorbike"])
    dist, mins = [], []
    for o in origins:
        d_row, m_row = [], []
        for d in destinations:
            km = haversine_distance_km(*o, *d) * ROAD_FACTOR
            m = math.ceil(km / speed * 60)
            d_row.append(km)
            m_row.append(m + BUFFER_MINUTES if m > 0 else 0)
        dist.append(d_row)
        mins.append(m_row)
    return MatrixResult(dist, mins, [row[:] for row in mins], "estimate")


class GoogleMapsClient:
    def __init__(self, api_key: str | None = None, http_client: httpx.Client | None = None,
                 motorbike_mode: str = "DRIVE", traffic_aware: bool = True,
                 max_billable_elements: int = 2000, language: str = "vi"):
        self.api_key = api_key if api_key is not None else os.getenv("GOOGLE_MAPS_API_KEY")
        self._http = http_client or httpx.Client(timeout=30.0)
        if motorbike_mode not in ("DRIVE", "TWO_WHEELER"):
            raise ValueError("motorbike_mode phải là DRIVE hoặc TWO_WHEELER")
        self.motorbike_mode = motorbike_mode
        self.traffic_aware = traffic_aware
        self.max_billable_elements = max_billable_elements
        self.billed_elements = 0
        self.language = language
        self._matrix_cache: dict[tuple, tuple[float, int, int]] = {}

    @property
    def is_enabled(self) -> bool:
        return bool(self.api_key and self.api_key != PLACEHOLDER_KEY)

    def _travel_mode(self, transport_mode: str) -> str:
        return {"motorbike": self.motorbike_mode, "car": "DRIVE", "walking": "WALK",
                "bicycling": "BICYCLE"}.get(transport_mode, "DRIVE")

    def _post(self, url: str, body: dict, field_mask: str) -> dict | list:
        resp = self._http.post(url, json=body, headers={
            "X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": field_mask})
        if resp.status_code != 200:
            try:
                body = resp.json()
                # computeRouteMatrix trả lỗi dạng mảng [{"error": {...}}], computeRoutes dạng object.
                body = body[0] if isinstance(body, list) and body else body
                message = body.get("error", {}).get("message", resp.text[:200])
            except (ValueError, AttributeError):
                message = resp.text[:200]
            raise RoutesError(f"HTTP {resp.status_code}: {message}")
        return resp.json()

    def _common(self, transport_mode: str, departure_time: datetime | None) -> dict:
        if departure_time is not None and departure_time.tzinfo is None:
            raise ValueError("departure_time cần có múi giờ (vd. Asia/Ho_Chi_Minh, UTC+7)")
        mode = self._travel_mode(transport_mode)
        body: dict = {"travelMode": mode, "languageCode": self.language, "regionCode": "vn"}
        if mode in ("DRIVE", "TWO_WHEELER"):
            body["routingPreference"] = "TRAFFIC_AWARE" if self.traffic_aware else "TRAFFIC_UNAWARE"
            # Chỉ TRANSIT được dùng giờ khởi hành trong quá khứ; quá khứ thì để mặc định (bây giờ).
            if departure_time is not None and departure_time > datetime.now(timezone.utc):
                body["departureTime"] = _rfc3339(departure_time)
        return body

    # -- ma trận -------------------------------------------------------------

    def route_matrix(self, origins: Sequence[LatLng], destinations: Sequence[LatLng],
                     departure_time: datetime | None = None,
                     transport_mode: str = "motorbike") -> MatrixResult:
        """Ma trận thời gian đi. Ô nào Google không trả được thì dùng ước lượng và ghi chú."""
        fallback = estimate_matrix(origins, destinations, transport_mode)
        if not self.is_enabled:
            fallback.notes.append("Chưa cấu hình GOOGLE_MAPS_API_KEY; thời gian đi là ước lượng.")
            return fallback

        result = MatrixResult([r[:] for r in fallback.distance_km], [r[:] for r in fallback.minutes],
                              [r[:] for r in fallback.static_minutes], "google")
        bucket = departure_time.replace(minute=0, second=0, microsecond=0) if departure_time else None
        missing: list[tuple[int, int]] = []
        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                if o == d:
                    result.distance_km[i][j], result.minutes[i][j], result.static_minutes[i][j] = 0.0, 0, 0
                    continue
                cached = self._matrix_cache.get((o, d, transport_mode, bucket))
                if cached:
                    result.distance_km[i][j], result.minutes[i][j], result.static_minutes[i][j] = cached
                else:
                    missing.append((i, j))

        need_o = sorted({i for i, _ in missing})
        need_d = sorted({j for _, j in missing})
        from_google = len(missing) < sum(1 for o in origins for d in destinations if o != d)
        for oi in range(0, len(need_o), MATRIX_BLOCK):
            for di in range(0, len(need_d), MATRIX_BLOCK):
                o_idx, d_idx = need_o[oi:oi + MATRIX_BLOCK], need_d[di:di + MATRIX_BLOCK]
                elements = len(o_idx) * len(d_idx)
                if self.billed_elements + elements > self.max_billable_elements:
                    result.notes.append(
                        f"Đã chạm trần {self.max_billable_elements} phần tử Google; phần còn lại dùng ước lượng.")
                    continue
                body = self._common(transport_mode, departure_time)
                body["origins"] = [_waypoint(origins[i]) for i in o_idx]
                body["destinations"] = [_waypoint(destinations[j]) for j in d_idx]
                try:
                    rows = self._post(ROUTE_MATRIX_URL, body, "originIndex,destinationIndex,status,"
                                      "condition,distanceMeters,duration,staticDuration")
                except (RoutesError, httpx.HTTPError) as exc:
                    logger.warning("computeRouteMatrix lỗi: %s", exc)
                    result.notes.append(f"Google Routes lỗi ({exc}); một phần thời gian đi là ước lượng.")
                    continue
                self.billed_elements += elements
                for el in rows:
                    if el.get("condition") != "ROUTE_EXISTS":
                        continue
                    i, j = o_idx[el.get("originIndex", 0)], d_idx[el.get("destinationIndex", 0)]
                    value = (el.get("distanceMeters", 0) / 1000.0,
                             math.ceil(_seconds(el.get("duration")) / 60),
                             math.ceil(_seconds(el.get("staticDuration") or el.get("duration")) / 60))
                    result.distance_km[i][j], result.minutes[i][j], result.static_minutes[i][j] = value
                    self._matrix_cache[(origins[i], destinations[j], transport_mode, bucket)] = value
                    from_google = True
        if not from_google:
            result.source = "estimate"
        return result

    # -- tuyến đường -----------------------------------------------------------

    def compute_routes(self, origin: LatLng, destination: LatLng, departure_time: datetime | None = None,
                       transport_mode: str = "motorbike", alternatives: bool = False) -> list[RouteOption]:
        """Tuyến chính (và tuyến thay thế nếu `alternatives`) kèm polyline."""
        if not self.is_enabled:
            raise RoutesError("Chưa cấu hình GOOGLE_MAPS_API_KEY")
        body = self._common(transport_mode, departure_time)
        body.update({"origin": _waypoint(origin)["waypoint"], "destination": _waypoint(destination)["waypoint"],
                     "computeAlternativeRoutes": alternatives, "polylineQuality": "OVERVIEW"})
        data = self._post(ROUTES_URL, body, "routes.distanceMeters,routes.duration,routes.staticDuration,"
                          "routes.polyline.encodedPolyline,routes.routeLabels,routes.warnings,routes.description")
        return [
            RouteOption(
                distance_km=r.get("distanceMeters", 0) / 1000.0,
                minutes=math.ceil(_seconds(r.get("duration")) / 60),
                static_minutes=math.ceil(_seconds(r.get("staticDuration") or r.get("duration")) / 60),
                polyline=r.get("polyline", {}).get("encodedPolyline", ""),
                labels=r.get("routeLabels", []), warnings=r.get("warnings", []),
                description=r.get("description", ""),
            )
            for r in data.get("routes", [])
        ]

    # -- dẫn đường ------------------------------------------------------------

    @staticmethod
    def generate_deep_link(origin: LatLng, destination: LatLng, waypoints: Sequence[LatLng] | None = None,
                           transport_mode: str = "motorbike", navigate: bool = False) -> str:
        """Link Maps URLs mở Google Maps chỉ đường.

        Quá MAX_URL_WAYPOINTS waypoint thì cắt bớt: dùng `day_links` để chia chặng.
        """
        params = {"api": "1", "origin": _coord(origin), "destination": _coord(destination),
                  "travelmode": URL_TRAVEL_MODE.get(transport_mode, "driving")}
        if waypoints:
            params["waypoints"] = "|".join(_coord(p) for p in waypoints[:MAX_URL_WAYPOINTS])
        if navigate:
            params["dir_action"] = "navigate"
        return f"{MAPS_DIR_URL}?{urlencode(params)}"

    @classmethod
    def day_links(cls, stops: Sequence[LatLng], transport_mode: str = "motorbike",
                  max_waypoints: int = MAX_URL_WAYPOINTS) -> list[str]:
        """Chia một ngày (khách sạn, các điểm, khách sạn) thành các link vừa giới hạn waypoint."""
        links, start = [], 0
        while start < len(stops) - 1:
            end = min(start + max_waypoints + 1, len(stops) - 1)
            links.append(cls.generate_deep_link(stops[start], stops[end], stops[start + 1:end], transport_mode))
            start = end
        return links


def _coord(p: LatLng) -> str:
    """Toạ độ cho URL; 6 chữ số thập phân ~ 0.1 m."""
    return ",".join(f"{v:.6f}".rstrip("0").rstrip(".") for v in p)


def decode_polyline(encoded: str) -> list[LatLng]:
    """Giải mã Encoded Polyline Algorithm Format của Google."""
    points, index, lat, lng = [], 0, 0, 0
    while index < len(encoded):
        for is_lng in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lng:
                lng += delta
            else:
                lat += delta
        points.append((lat / 1e5, lng / 1e5))
    return points
