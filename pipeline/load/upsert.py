import json

from pipeline.models import PlaceRecord

COLUMNS = """
  name, name_en, category, location, opening_hours, opening_hours_raw,
  ticket_price, dress_code, avg_visit_minutes, indoor_ratio,
  weather_sensitivity, best_time_of_day, unsafe_conditions, label_source,
  website, source_url, updated_at
"""


def _find_place_id(cur, external_ids: dict[str, str]) -> int | None:
    for source, external_id in external_ids.items():
        cur.execute(
            "SELECT place_id FROM place_external_ids WHERE source = %s AND external_id = %s",
            (source, external_id),
        )
        row = cur.fetchone()
        if row:
            return row[0]
    return None


def upsert_places(conn, places: list[PlaceRecord]) -> tuple[int, int]:
    """Thêm mới/cập nhật `places` theo một transaction duy nhất.

    `conn` mở ở chế độ autocommit (xem `pipeline/db.py`), nên nếu không có
    `conn.transaction()` bọc quanh toàn bộ vòng lặp, mỗi INSERT/UPDATE/liên
    kết external_id sẽ tự commit ngay khi thực thi. Một lỗi ở giữa batch khi
    đó để lại các bản ghi trước đó đã ghi xuống DB còn phần còn lại thì
    không — dữ liệu ở trạng thái dở dang cho tới khi ai đó chạy lại `load`.
    Bọc trong `conn.transaction()` đảm bảo cả batch hoặc thành công trọn
    vẹn, hoặc rollback toàn bộ.
    """
    inserted = updated = 0
    with conn.transaction(), conn.cursor() as cur:
        for place in places:
            values = (
                place.name, place.name_en, place.category,
                place.lon, place.lat,
                json.dumps(place.opening_hours) if place.opening_hours else None,
                place.opening_hours_raw,
                json.dumps(place.ticket_price) if place.ticket_price else None,
                place.dress_code, place.avg_visit_minutes, place.indoor_ratio,
                json.dumps(place.weather_sensitivity) if place.weather_sensitivity else None,
                place.best_time_of_day, place.unsafe_conditions, place.label_source,
                place.website, place.source_url,
            )
            place_id = _find_place_id(cur, place.external_ids)
            if place_id is None:
                cur.execute(
                    f"INSERT INTO places ({COLUMNS}) VALUES"
                    " (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,"
                    "  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())"
                    " RETURNING id",
                    values,
                )
                place_id = cur.fetchone()[0]
                inserted += 1
            else:
                cur.execute(
                    "UPDATE places SET name = %s, name_en = %s, category = %s,"
                    " location = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,"
                    " opening_hours = %s, opening_hours_raw = %s, ticket_price = %s,"
                    " dress_code = %s, avg_visit_minutes = %s, indoor_ratio = %s,"
                    " weather_sensitivity = %s, best_time_of_day = %s,"
                    " unsafe_conditions = %s, label_source = %s, website = %s,"
                    " source_url = %s, updated_at = now() WHERE id = %s",
                    values + (place_id,),
                )
                updated += 1

            for source, external_id in place.external_ids.items():
                cur.execute(
                    "INSERT INTO place_external_ids (place_id, source, external_id)"
                    " VALUES (%s, %s, %s) ON CONFLICT (source, external_id) DO NOTHING",
                    (place_id, source, external_id),
                )
    return inserted, updated
