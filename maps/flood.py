"""Vùng dễ ngập: phạt / né tuyến đi qua khi có mưa lớn.

Dữ liệu vùng ngập nằm ở config/flood_zones.yml và phải có nguồn. Hiện OSM
không có đoạn đường nào ở Huế gắn `flood_prone=yes` (kiểm tra qua Overpass
ngày 2026-09-27), nên file cấu hình để trống cho tới khi có nguồn tin cậy
(bản đồ ngập của thành phố, báo cáo phòng chống thiên tai). Không có vùng nào
thì mọi hàm ở đây trả về "không rủi ro".
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import yaml

DEFAULT_PATH = "config/flood_zones.yml"
LatLng = tuple[float, float]


@dataclass(frozen=True)
class FloodZone:
    name: str
    lat: float
    lon: float
    radius_m: float
    source: str


def load_flood_zones(path: str | Path = DEFAULT_PATH) -> list[FloodZone]:
    path = Path(path)
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    zones = []
    for item in raw:
        if not item.get("source"):
            raise ValueError(f"Vùng ngập '{item.get('name')}' thiếu `source` trong {path}")
        zones.append(FloodZone(item["name"], float(item["lat"]), float(item["lon"]),
                               float(item["radius_m"]), item["source"]))
    return zones


def _xy(p: LatLng, ref_lat: float) -> tuple[float, float]:
    """Chiếu phẳng (mét) quanh vĩ độ tham chiếu; đủ chính xác trong phạm vi một thành phố."""
    return (math.radians(p[1]) * 6371000 * math.cos(math.radians(ref_lat)), math.radians(p[0]) * 6371000)


def _segment_distance_m(a: LatLng, b: LatLng, c: LatLng) -> float:
    ax, ay = _xy(a, c[0])
    bx, by = _xy(b, c[0])
    cx, cy = _xy(c, c[0])
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / length2))
    return math.hypot(ax + t * dx - cx, ay + t * dy - cy)


def zones_on_path(path: Sequence[LatLng], zones: Iterable[FloodZone]) -> list[FloodZone]:
    """Các vùng mà đường gấp khúc `path` đi qua (kể cả chỉ một điểm)."""
    hit = []
    for zone in zones:
        centre = (zone.lat, zone.lon)
        segments = zip(path, path[1:]) if len(path) > 1 else [(path[0], path[0])]
        if any(_segment_distance_m(a, b, centre) <= zone.radius_m for a, b in segments):
            hit.append(zone)
    return hit
