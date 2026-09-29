"""Kiểm tra một lịch trình có khả thi không, độc lập với cách lịch được tạo ra.

Dùng chung cho lịch của bộ lập lịch và lịch do LLM tự viết (baseline trong
CLAUDE.md), nên chỉ dựa vào dữ liệu có cấu trúc trong DB: giờ mở cửa, thời
gian tham quan trung bình, giá vé, toạ độ.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from planner.matrix import TravelMatrix
from planner.models import Place, ScheduleResult, UserConstraint
from planner.solver import trip_days

TOLERANCE_MINUTES = 5
MIN_VISIT_RATIO = 0.5  # ở dưới một nửa thời gian tham quan trung bình thì coi là không thực tế


@dataclass
class Violation:
    kind: str  # unknown_place | duplicate | outside_trip | day_window | closed | opening_hours
    #            | travel | visit_too_short | max_places | budget
    day: date | None
    place_id: int | str | None
    detail: str


@dataclass
class FeasibilityReport:
    violations: list[Violation] = field(default_factory=list)
    visits: int = 0
    unverified_hours: int = 0  # lượt đi tới điểm chưa có giờ mở cửa trong DB

    @property
    def feasible(self) -> bool:
        return not self.violations

    def count(self, kind: str) -> int:
        return sum(v.kind == kind for v in self.violations)


def _minute(t: datetime) -> int:
    return t.hour * 60 + t.minute


def check_feasibility(result: ScheduleResult, places: dict[int | str, Place],
                      constraint: UserConstraint) -> FeasibilityReport:
    report = FeasibilityReport()
    windows = {day: (lo, hi) for day, lo, hi in trip_days(constraint)}
    ids = list(places)
    locations = [constraint.start_location, constraint.end_location] + [(p.lat, p.lon) for p in places.values()]
    matrix = TravelMatrix(locations, constraint.transport_mode)
    node = {pid: i + 2 for i, pid in enumerate(ids)}
    tol = TOLERANCE_MINUTES

    seen = Counter(v.place_id for v in result.visits)
    for pid, n in seen.items():
        if n > 1:
            report.violations.append(Violation("duplicate", None, pid, f"đi {n} lần"))

    # Gộp theo ngày: lịch do LLM viết có khi tách một ngày thành nhiều mục cùng ngày,
    # kiểm riêng từng mục sẽ lọt vi phạm số điểm/ngày và thời gian đi giữa hai mục.
    by_day: dict[date, list] = {}
    for itinerary in result.itineraries:
        by_day.setdefault(itinerary.date, []).extend(itinerary.visits)

    spent = 0
    for day, day_visits in by_day.items():
        visits = sorted(day_visits, key=lambda v: v.arrival_time)
        report.visits += len(visits)
        if not visits:
            continue
        if day not in windows:
            report.violations.append(Violation("outside_trip", day, None, "ngày nằm ngoài chuyến đi"))
            continue
        day_lo, day_hi = windows[day]
        if len(visits) > constraint.max_places_per_day:
            report.violations.append(Violation(
                "max_places", day, None, f"{len(visits)} điểm > {constraint.max_places_per_day}"))

        prev_node, prev_leave = 0, day_lo
        for v in visits:
            place = places.get(v.place_id)
            if place is None:
                report.violations.append(Violation("unknown_place", day, v.place_id, "không có trong DB"))
                continue
            arrive, leave = _minute(v.arrival_time), _minute(v.departure_time)
            if v.departure_time.date() != day:
                leave += (v.departure_time.date() - day).days * 24 * 60
            need = matrix.minutes(prev_node, node[v.place_id])
            if arrive + tol < prev_leave + need:
                report.violations.append(Violation(
                    "travel", day, v.place_id,
                    f"đến {v.arrival_time:%H:%M} nhưng sớm nhất {_hhmm(prev_leave + need)}"
                    f" (đi mất {need} phút)"))
            if arrive < day_lo - tol or leave > day_hi + tol:
                report.violations.append(Violation(
                    "day_window", day, v.place_id, f"{v.arrival_time:%H:%M}-{v.departure_time:%H:%M}"
                    f" ngoài khung {_hhmm(day_lo)}-{_hhmm(day_hi)}"))
            if leave - arrive < MIN_VISIT_RATIO * place.avg_visit_minutes:
                report.violations.append(Violation(
                    "visit_too_short", day, v.place_id,
                    f"{leave - arrive} phút, trung bình cần {place.avg_visit_minutes}"))
            opening = place.opening_windows(day)
            if opening is None:
                report.unverified_hours += 1
            elif not opening:
                report.violations.append(Violation("closed", day, v.place_id, "đóng cửa cả ngày"))
            elif not any(o - tol <= arrive and leave <= c + tol for o, c in opening):
                spans = ", ".join(f"{_hhmm(o)}-{_hhmm(c)}" for o, c in opening)
                report.violations.append(Violation(
                    "opening_hours", day, v.place_id,
                    f"{v.arrival_time:%H:%M}-{v.departure_time:%H:%M} ngoài giờ mở cửa {spans}"))
            spent += place.ticket_price_vnd or 0
            prev_node, prev_leave = node[v.place_id], leave

        back = matrix.minutes(prev_node, 1)
        if prev_leave + back > day_hi + tol:
            report.violations.append(Violation(
                "day_window", day, None, f"về tới nơi lúc {_hhmm(prev_leave + back)}, muộn hơn {_hhmm(day_hi)}"))

    if constraint.budget_vnd is not None and spent > constraint.budget_vnd:
        report.violations.append(Violation(
            "budget", None, None, f"vé {spent:,} đ > ngân sách {constraint.budget_vnd:,} đ"))
    return report


def _hhmm(minute: int) -> str:
    return (datetime.combine(date.min, time()) + timedelta(minutes=minute)).strftime("%H:%M")
