"""Nhận diện địa danh từ ảnh, chạy từ rẻ đến đắt (CLAUDE.md):

1. GPS trong EXIF -> PostGIS tìm địa danh trong bán kính ~200 m.
   GPS là vị trí NGƯỜI CHỤP, không phải vật được chụp: ảnh Cầu Trường Tiền chụp
   từ bờ sông có GPS cách tượng đài Phan Bội Châu 72 m. Vì vậy khi có vision
   LLM, GPS chỉ thu hẹp ứng viên (bán kính GPS_CANDIDATE_RADIUS_M) để vision
   chọn; không có LLM mới lấy luôn điểm gần nhất trong 200 m.
2. Embedding ảnh (CLIP/SigLIP) so với `place_images` — CHƯA làm: kho ảnh tham
   chiếu đã thu thập nhưng chưa sinh embedding.
3. Vision LLM dự phòng, bị giới hạn trong danh sách địa danh có trong DB để
   không trả về tên ngoài phạm vi; trả "không xác định" khi không chắc.
"""

import base64
import io
import json
from dataclasses import dataclass

from planner.models import Place
from planner.places import load_places

GPS_RADIUS_M = 200
GPS_CANDIDATE_RADIUS_M = 800
VISION_MIN_CONFIDENCE = 0.5
# Điểm tham quan có thể chụp ảnh (không gồm quán ăn, khách sạn, bối cảnh, hạ tầng).
LANDMARK_CATEGORIES = ("di_tich", "lang_tam", "chua", "bao_tang", "diem_tham_quan", "diem_ngam_canh",
                       "cong_vien", "bai_bien", "song", "khac")


@dataclass
class Recognition:
    place: Place | None
    tier: str  # "gps" | "gps+vision" | "vision" | "none"
    confidence: float | None
    detail: str


def _to_degrees(value) -> float:
    d, m, s = (float(x) for x in value)
    return d + m / 60 + s / 3600


def read_gps(image_bytes: bytes) -> tuple[float, float] | None:
    """(lat, lon) từ EXIF GPS, None nếu ảnh không có."""
    from PIL import Image

    try:
        gps = Image.open(io.BytesIO(image_bytes)).getexif().get_ifd(0x8825)
    except Exception:
        return None
    if not gps or 2 not in gps or 4 not in gps:
        return None
    lat, lon = _to_degrees(gps[2]), _to_degrees(gps[4])
    if gps.get(1) == "S":
        lat = -lat
    if gps.get(3) == "W":
        lon = -lon
    return lat, lon


def landmarks_near(conn, lat: float, lon: float, radius_m: int) -> list[tuple[Place, float]]:
    """Địa danh trong bán kính, gần nhất trước, kèm khoảng cách (m)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, ST_Distance(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS d"
            " FROM places WHERE category = ANY(%s) AND location IS NOT NULL"
            " AND ST_DWithin(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)"
            " ORDER BY d",
            (lon, lat, list(LANDMARK_CATEGORIES), lon, lat, radius_m))
        rows = cur.fetchall()
    places = {p.id: p for p in load_places(conn, ids=[r[0] for r in rows])}
    return [(places[pid], d) for pid, d in rows if pid in places]


def vision_candidates(conn) -> list[Place]:
    """Địa danh có bài tri thức: đủ nổi tiếng để có ảnh và để trả lời tiếp sau khi nhận diện."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT p.id FROM places p JOIN place_chunks c ON c.place_id = p.id"
                    " WHERE p.category = ANY(%s)", (list(LANDMARK_CATEGORIES),))
        ids = [r[0] for r in cur.fetchall()]
    return load_places(conn, ids=ids)


def _shrink(image_bytes: bytes, max_side: int = 1024) -> bytes:
    """JPEG cạnh dài tối đa `max_side`: đủ nhận diện, bớt token ảnh gửi LLM."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def recognize_with_vision(llm, image_bytes: bytes, candidates: list[Place]) -> Recognition:
    names = sorted({p.name for p in candidates})
    prompt = ("Ảnh này được chụp ở thành phố Huế, Việt Nam. Chọn đúng MỘT địa danh trong danh sách dưới đây nếu ảnh"
              " chụp địa danh đó; nếu không chắc hoặc không có trong danh sách, trả name = null.\n"
              "Danh sách:\n" + "\n".join(f"- {n}" for n in names)
              + '\n\nChỉ trả JSON: {"name": <tên đúng như danh sách hoặc null>, "confidence": <0..1>,'
                ' "reason": <một câu, đặc điểm nhìn thấy trong ảnh>}')
    data_url = f"data:image/jpeg;base64,{base64.b64encode(_shrink(image_bytes)).decode()}"
    message = llm.chat([{"role": "user", "content": [
        {"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_url}}]}], json_mode=True)
    try:
        answer = json.loads(message.content or "{}")
    except json.JSONDecodeError:
        return Recognition(None, "vision", None, "Vision LLM trả về định dạng không đọc được.")
    by_name = {p.name: p for p in candidates}
    confidence = answer.get("confidence")
    place = by_name.get(answer.get("name"))
    if place is None or (confidence is not None and confidence < VISION_MIN_CONFIDENCE):
        return Recognition(None, "vision", confidence, answer.get("reason") or "Không nhận ra địa danh trong danh sách.")
    return Recognition(place, "vision", confidence, answer.get("reason") or "")


def recognize(conn, image_bytes: bytes, llm=None) -> Recognition:
    gps = read_gps(image_bytes)
    nearby = landmarks_near(conn, *gps, GPS_CANDIDATE_RADIUS_M) if gps else []
    if llm is None:
        if nearby and nearby[0][1] <= GPS_RADIUS_M:
            place, distance = nearby[0]
            return Recognition(place, "gps", None, f"Ảnh chụp cách {place.name} {distance:.0f} m (theo GPS)."
                               " GPS là vị trí người chụp, có thể không phải vật được chụp.")
        detail = "Ảnh không có GPS gần địa danh nào" if gps else "Ảnh không có GPS"
        return Recognition(None, "none", None, detail + "; chưa bật vision LLM (tầng embedding ảnh chưa làm).")
    if nearby:
        result = recognize_with_vision(llm, image_bytes, [p for p, _ in nearby])
        if result.place is not None:
            distance = dict((p.id, d) for p, d in nearby)[result.place.id]
            result.tier = "gps+vision"
            result.detail += f" (GPS: người chụp cách {result.place.name} {distance:.0f} m)"
            return result
    return recognize_with_vision(llm, image_bytes, vision_candidates(conn))
