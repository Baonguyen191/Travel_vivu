"""Nạp địa điểm từ bảng `places` thành `Place` cho bộ lập lịch."""

from typing import Sequence

import psycopg

from planner.models import DEFAULT_VISIT_MINUTES, Place

# Category là điểm đến có thể xếp vào lịch (xem config/categories.yml). Không
# gồm boi_canh, ha_tang, don_vi_hanh_chinh, khach_san.
DESTINATION_CATEGORIES = (
    "di_tich", "lang_tam", "chua", "bao_tang", "diem_tham_quan", "diem_ngam_canh",
    "cong_vien", "bai_bien", "song", "nha_hang", "quan_ca_phe", "khac",
)

_SELECT = """
    SELECT p.id, p.name, p.category, ST_Y(p.location::geometry), ST_X(p.location::geometry),
           p.avg_visit_minutes, p.indoor_ratio, p.weather_sensitivity, p.unsafe_conditions,
           p.opening_hours, p.ticket_price
    FROM places p
"""


def _ticket_vnd(ticket_price) -> int | None:
    if isinstance(ticket_price, dict) and isinstance(ticket_price.get("vnd"), (int, float)):
        return int(ticket_price["vnd"])
    return None


def _to_place(row) -> Place:
    (pid, name, category, lat, lon, visit, indoor, sensitivity, unsafe, hours, ticket) = row
    return Place(
        id=pid,
        name=name,
        lat=lat,
        lon=lon,
        category=category,
        avg_visit_minutes=visit or DEFAULT_VISIT_MINUTES,
        indoor_ratio=indoor if indoor is not None else 0.5,
        weather_sensitivity=dict(sensitivity or {}),
        unsafe_conditions=list(unsafe or []),
        opening_hours=hours,
        ticket_price_vnd=_ticket_vnd(ticket),
    )


def load_places(conn: psycopg.Connection, ids: Sequence[int] | None = None,
                categories: Sequence[str] = DESTINATION_CATEGORIES) -> list[Place]:
    """Địa điểm có toạ độ. `ids` giữ nguyên thứ tự người gọi truyền vào."""
    with conn.cursor() as cur:
        if ids is not None:
            cur.execute(_SELECT + " WHERE p.id = ANY(%s) AND p.location IS NOT NULL", (list(ids),))
            by_id = {row[0]: _to_place(row) for row in cur.fetchall()}
            return [by_id[i] for i in ids if i in by_id]
        cur.execute(_SELECT + " WHERE p.category = ANY(%s) AND p.location IS NOT NULL ORDER BY p.id",
                    (list(categories),))
        return [_to_place(row) for row in cur.fetchall()]


def load_places_by_qid(conn: psycopg.Connection, qids: Sequence[str]) -> list[Place]:
    """Theo QID Wikidata (ổn định qua các lần nạp lại), giữ thứ tự `qids`."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT external_id, place_id FROM place_external_ids"
            " WHERE source = 'wikidata' AND external_id = ANY(%s)",
            (list(qids),),
        )
        place_ids = dict(cur.fetchall())
    missing = [q for q in qids if q not in place_ids]
    if missing:
        raise ValueError(f"QID không có trong DB: {', '.join(missing)}")
    return load_places(conn, ids=[place_ids[q] for q in qids])
