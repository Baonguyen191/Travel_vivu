"""Ma trận khoảng cách và thời gian di chuyển giữa các điểm."""

import math
from datetime import datetime
from typing import Sequence

# Đường đi thực tế dài hơn đường chim bay; ~1.3 là hệ số thường gặp ở đô thị.
ROAD_FACTOR = 1.3
SPEED_KMH = {"motorbike": 25.0, "car": 30.0, "walking": 4.5}
BUFFER_MINUTES = 3  # gửi xe, tìm lối vào


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


class TravelMatrix:
    """Khoảng cách (km) và thời gian đi khi trời tốt (phút) giữa các điểm.

    Mặc định ước lượng từ đường chim bay × ROAD_FACTOR. Có `maps_client` (OSRM
    hoặc Google, xem maps/routing.py) và `departure_times` thì lấy ma trận của
    nguồn đó tại từng mốc giờ; `minutes(..., at=t)` dùng mốc gần `t` nhất. OSRM
    không có giao thông nên mọi mốc giống nhau. Mốc nào lỗi thì rơi về ước
    lượng, lý do nằm trong `notes`.
    """

    def __init__(self, locations: Sequence[tuple[float, float]], transport_mode: str = "motorbike",
                 maps_client=None, departure_times: Sequence[datetime] = ()):
        if transport_mode not in SPEED_KMH:
            raise ValueError(f"transport_mode '{transport_mode}' không thuộc {sorted(SPEED_KMH)}")
        speed = SPEED_KMH[transport_mode]
        n = len(locations)
        self.distance_km = [[0.0] * n for _ in range(n)]
        self.base_minutes = [[0] * n for _ in range(n)]
        for i, a in enumerate(locations):
            for j, b in enumerate(locations):
                if i == j:
                    continue
                km = haversine_distance_km(*a, *b) * ROAD_FACTOR
                self.distance_km[i][j] = km
                # Hai điểm trùng toạ độ (vd. khách sạn làm cả điểm đầu và cuối) không tốn buffer.
                m = math.ceil(km / speed * 60)
                self.base_minutes[i][j] = m + BUFFER_MINUTES if m > 0 else 0

        self.source = "estimate"
        self.notes: list[str] = []
        self._traffic: list[tuple[datetime, list[list[int]]]] = []  # (mốc giờ địa phương, phút)
        if maps_client is None:
            return
        if not maps_client.is_enabled:
            self.notes.append(getattr(maps_client, "disabled_note", None)
                              or "Chưa cấu hình GOOGLE_MAPS_API_KEY; thời gian đi là ước lượng"
                                 " (đường chim bay × 1.3, không tính giao thông).")
            return
        for t in sorted(set(departure_times)):
            result = maps_client.route_matrix(locations, locations, t, transport_mode)
            self.notes.extend(n for n in result.notes if n not in self.notes)
            if result.source != "estimate":
                self._traffic.append((t.replace(tzinfo=None), result.minutes))
                self.distance_km = result.distance_km
                self.source = result.source

    def minutes(self, i: int, j: int, factor: float = 1.0, at: datetime | None = None) -> int:
        base = self.base_minutes
        if self._traffic:
            if at is None:
                base = self._traffic[0][1]
            else:
                at = at.replace(tzinfo=None)
                base = min(self._traffic, key=lambda tm: abs((tm[0] - at).total_seconds()))[1]
        return math.ceil(base[i][j] * factor)
