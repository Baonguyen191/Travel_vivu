"""Quy tắc thời tiết dùng chung cho bộ lập lịch và bộ đánh giá.

Bộ lập lịch áp các quy tắc này lên *dự báo*; bộ đánh giá áp đúng các quy tắc
này lên *thời tiết quan trắc*. Chỉ có một định nghĩa "mưa", "mưa lớn", "bão",
nên chênh lệch giữa hai bên là do dự báo sai, không phải do hai bộ ngưỡng khác
nhau.
"""

from planner.models import HourlyWeather, Place

# Nhãn nguy hiểm, trùng với `places.unsafe_conditions` (config/weather_defaults.yml).
BAO = "bao"  # bão, dông, gió mạnh
MUA_LON = "mua_lon"
SONG_LON = "song_lon"  # biển động
HAZARDS = (BAO, MUA_LON, SONG_LON)

# Ngưỡng theo giờ. Mưa to theo WMO: >= 7.6 mm/h.
RAIN_MM_PER_HOUR = 0.5  # từ mức này trở lên coi là đang mưa
HEAVY_RAIN_MM_PER_HOUR = 7.5
# Ngưỡng theo ngày (dự báo 4–10 ngày). Mưa rất to theo quy chuẩn khí tượng
# Việt Nam: > 50 mm/24h. Không chia đều 50 mm cho 24 giờ vì mưa dồn vào vài giờ.
RAIN_MM_PER_DAY = 5.0
HEAVY_RAIN_MM_PER_DAY = 50.0
STORM_WIND_KMH = 50.0  # gió cấp 7 trở lên
ROUGH_SEA_WIND_KMH = 40.0  # xấp xỉ biển động khi chưa có dữ liệu sóng
THUNDERSTORM_CODES = {95, 96, 99}  # WMO
HEAVY_RAIN_CODES = {65, 82}  # mưa to, mưa rào rất to


def _is_daily(w: HourlyWeather) -> bool:
    return w.period_hours >= 24


def is_raining(w: HourlyWeather) -> bool:
    threshold = RAIN_MM_PER_DAY if _is_daily(w) else RAIN_MM_PER_HOUR * w.period_hours
    return w.precipitation_mm >= threshold


def hazards(w: HourlyWeather) -> set[str]:
    """Các điều kiện nguy hiểm đang xảy ra (hoặc được dự báo) trong khoảng `w`."""
    found = set()
    if w.wind_speed_kmh >= STORM_WIND_KMH or w.weather_code in THUNDERSTORM_CODES:
        found.add(BAO)
    heavy = HEAVY_RAIN_MM_PER_DAY if _is_daily(w) else HEAVY_RAIN_MM_PER_HOUR * w.period_hours
    if w.precipitation_mm >= heavy or w.weather_code in HEAVY_RAIN_CODES:
        found.add(MUA_LON)
    if w.wind_speed_kmh >= ROUGH_SEA_WIND_KMH:
        found.add(SONG_LON)
    return found


def unsafe_reason(place: Place, w: HourlyWeather) -> str | None:
    """Lý do không được đến `place` trong khoảng `w`, None nếu an toàn.

    Ràng buộc cứng chỉ dựa trên `unsafe_conditions` của place (CLAUDE.md): mưa
    lớn không cấm vào bảo tàng, nhưng cấm ra phá Tam Giang.
    """
    hit = hazards(w) & set(place.unsafe_conditions)
    if not hit:
        return None
    labels = {BAO: "bão/dông", MUA_LON: "mưa lớn", SONG_LON: "biển động"}
    return ", ".join(labels[h] for h in sorted(hit))


def rain_score(w: HourlyWeather) -> float:
    """0..1: mức mưa. Dùng lượng mưa; xác suất mưa (nếu có) chỉ nâng thêm."""
    if _is_daily(w):
        amount = min(w.precipitation_mm / 30.0, 1.0)
    else:
        amount = min(w.precipitation_mm / (5.0 * w.period_hours), 1.0)
    prob = (w.rain_probability or 0.0) / 100.0
    return max(amount, 0.7 * prob)


def heat_score(w: HourlyWeather) -> float:
    return min(max((w.temperature_c - 32.0) / 6.0, 0.0), 1.0)


def wind_score(w: HourlyWeather) -> float:
    return min(max((w.wind_speed_kmh - 20.0) / 30.0, 0.0), 1.0)


def weather_cost(place: Place, w: HourlyWeather) -> float:
    """Chi phí mềm 0..1 khi ở `place` trong khoảng `w`.

    = phần ngoài trời × (độ nhạy mưa × mức mưa + độ nhạy nóng × mức nóng
      + độ nhạy gió × mức gió), cắt về [0, 1].
    """
    sens = place.weather_sensitivity or {}
    exposure = (
        sens.get("rain", 0.5) * rain_score(w)
        + sens.get("heat", 0.5) * heat_score(w)
        + sens.get("wind", 0.2) * wind_score(w)
    )
    return min((1.0 - place.indoor_ratio) * exposure, 1.0)


def travel_factor(w: HourlyWeather | None) -> float:
    """Hệ số nhân thời gian di chuyển do thời tiết."""
    if w is None:
        return 1.0
    found = hazards(w)
    if BAO in found:
        return 1.8
    if MUA_LON in found:
        return 1.4
    if is_raining(w):
        return 1.2
    return 1.0
