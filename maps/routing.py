"""Chọn nguồn thời gian đi cho bộ lập lịch.

Thứ tự tự động: OSRM tự host (miễn phí, đường thật, không giao thông) nếu đang
chạy; Google Routes (có giao thông, tính phí) nếu có key; không thì None, tức
ước lượng từ khoảng cách.
"""

from maps.client import GoogleMapsClient
from maps.osrm import OsrmClient

SOURCES = ("auto", "osrm", "google", "estimate")


def choose_routing(prefer: str = "auto", google_key: str | None = None):
    if prefer not in SOURCES:
        raise ValueError(f"prefer phải thuộc {SOURCES}")
    if prefer == "estimate":
        return None
    if prefer in ("auto", "osrm"):
        osrm = OsrmClient()
        if osrm.is_enabled or prefer == "osrm":
            return osrm
    google = GoogleMapsClient(api_key=google_key) if google_key is not None else GoogleMapsClient()
    if prefer == "google" or google.is_enabled:
        return google
    return None
