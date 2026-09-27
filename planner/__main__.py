"""Chạy thử bộ lập lịch từ dòng lệnh.

    python -m planner --qids Q10769129,Q975568,Q7481171 --start 2026-10-01 --days 1
    python -m planner --qids ... --start ... --routes   # thêm tuyến Google cho từng chặng

Thời gian đi dùng Google Routes khi có biến môi trường GOOGLE_MAPS_API_KEY
(dự án không tự nạp .env), không thì ước lượng.
"""

import argparse
from datetime import date, datetime, time, timedelta

from maps.client import GoogleMapsClient
from planner.models import UserConstraint
from planner.places import load_places_by_qid
from planner.service import format_plan, plan_trip

DEFAULT_HOTEL = (16.4637, 107.5909)


def main() -> int:
    parser = argparse.ArgumentParser(prog="planner")
    parser.add_argument("--qids", required=True, help="QID Wikidata, cách nhau bằng dấu phẩy, ưu tiên giảm dần")
    parser.add_argument("--start", required=True, help="Ngày đi YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--hotel", default=f"{DEFAULT_HOTEL[0]},{DEFAULT_HOTEL[1]}")
    parser.add_argument("--mode", default="motorbike", choices=["motorbike", "car", "walking"])
    parser.add_argument("--max-places", type=int, default=5)
    parser.add_argument("--routes", action="store_true", help="Gọi computeRoutes cho từng chặng (tính phí)")
    parser.add_argument("--two-wheeler", action="store_true",
                        help="Dùng TWO_WHEELER của Routes API cho xe máy (SKU Enterprise, đắt hơn DRIVE)")
    args = parser.parse_args()

    from pipeline import db

    start = date.fromisoformat(args.start)
    hotel = tuple(float(x) for x in args.hotel.split(","))
    constraint = UserConstraint(
        start_datetime=datetime.combine(start, time(8)),
        end_datetime=datetime.combine(start + timedelta(days=args.days - 1), time(18)),
        start_location=hotel, end_location=hotel, max_places_per_day=args.max_places,
        transport_mode=args.mode,
    )
    client = GoogleMapsClient(motorbike_mode="TWO_WHEELER" if args.two_wheeler else "DRIVE")
    with db.connect() as conn:
        places = load_places_by_qid(conn, [q.strip() for q in args.qids.split(",") if q.strip()])
        # Thứ tự nhập = ưu tiên: điểm đầu 1.0, điểm cuối ~0.5.
        priorities = {p.id: 1.0 - rank / (2 * len(places)) for rank, p in enumerate(places)}
        plan = plan_trip(conn, [p.id for p in places], constraint, maps_client=client,
                         with_routes=args.routes, priorities=priorities)
    print(format_plan(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
