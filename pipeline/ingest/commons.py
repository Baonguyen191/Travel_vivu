import json
from pathlib import Path
from urllib.parse import unquote

import httpx
from selectolax.parser import HTMLParser

from pipeline.http import Fetcher
from pipeline.ingest.wikidata import STAGED_PATH
from pipeline.models import ImageRecord

API_URL = "https://commons.wikimedia.org/w/api.php"
MAX_IMAGES = 30

# Khách sạn, nhà hàng, quán cà phê và "khác" không phải thứ người dùng chụp
# ảnh rồi hỏi nhận diện — tên của chúng thường trùng với vô số nơi khác trên
# thế giới, nên tìm theo tên (route name_search) chỉ kéo về nhiễu cho các
# hạng mục này. Route P18/category vẫn áp dụng cho mọi địa điểm vì đó là dữ
# liệu đã được Wikidata tuyển chọn, đáng tin cậy bất kể hạng mục.
NON_RECOGNITION_CATEGORIES = {"khach_san", "nha_hang", "quan_ca_phe", "khac"}


def _plain(value: str | None) -> str | None:
    if not value:
        return None
    text = HTMLParser(value).text().strip()
    return text or None


def parse_imageinfo(payload: dict) -> list[ImageRecord]:
    images = []
    for page in payload.get("query", {}).get("pages", {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata", {})
        images.append(ImageRecord(
            place_key="",
            image_url=url,
            license=_plain(meta.get("LicenseShortName", {}).get("value")),
            author=_plain(meta.get("Artist", {}).get("value")),
        ))
    return images


def _file_title_from_url(image_url: str) -> str:
    """Suy tên `File:...` từ URL Special:FilePath (P18 của Wikidata)."""
    filename = unquote(image_url.rsplit("/", 1)[-1])
    return f"File:{filename}"


def _tags_by_qid(staged_path: str) -> dict[str, dict]:
    """Đọc `data/staged/wikidata.json`, trả về QID -> tags.

    Chỉ cần `tags.image_url` (P18) và `tags.commons_category` (P373) — hai
    nguồn có cấu trúc, ưu tiên hơn tìm kiếm theo tên.
    """
    path = Path(staged_path)
    if not path.exists():
        return {}
    records = json.loads(path.read_text(encoding="utf-8"))
    tags: dict[str, dict] = {}
    for record in records:
        qid = (record.get("external_ids") or {}).get("wikidata")
        if qid:
            tags[qid] = record.get("tags") or {}
    return tags


def _places_with_tags(conn) -> list[tuple[int, str, str, dict]]:
    tags_by_qid = _tags_by_qid(STAGED_PATH)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.name, p.category, e.external_id FROM places p"
            " JOIN place_external_ids e ON e.place_id = p.id"
            " WHERE e.source = 'wikidata'"
        )
        rows = cur.fetchall()
    return [
        (place_id, name, category, tags_by_qid.get(qid, {}))
        for place_id, name, category, qid in rows
    ]


def _fetch_for_place(fetcher: Fetcher, name: str, category: str, tags: dict,
                      city_name: str, force: bool):
    """Chọn tuyến khớp ảnh theo thứ tự ưu tiên và gọi Commons API.

    Trả về (route, FetchResult). route là None khi không có route nào áp
    dụng — không gọi API. Ưu tiên: ảnh đại diện P18 > danh mục Commons (P373)
    > tìm kiếm theo tên (nhiễu cao nhất, chỉ dùng khi không còn gì, và không
    dùng cho khách sạn/nhà hàng/quán cà phê/"khác" — xem NON_RECOGNITION_CATEGORIES).
    """
    image_url = tags.get("image_url")
    commons_category = tags.get("commons_category")
    if image_url:
        res = fetcher.fetch(
            API_URL,
            params={"action": "query", "titles": _file_title_from_url(image_url),
                    "prop": "imageinfo", "iiprop": "url|extmetadata", "format": "json"},
            force=force,
        )
        return "p18", res
    if commons_category:
        res = fetcher.fetch(
            API_URL,
            params={"action": "query", "generator": "categorymembers",
                    "gcmtitle": f"Category:{commons_category}", "gcmnamespace": "6",
                    "gcmlimit": str(MAX_IMAGES), "prop": "imageinfo",
                    "iiprop": "url|extmetadata", "format": "json"},
            force=force,
        )
        return "category", res
    if category in NON_RECOGNITION_CATEGORIES:
        return None, None
    res = fetcher.fetch(
        API_URL,
        params={"action": "query", "generator": "search",
                "gsrsearch": f"{name} {city_name} filetype:bitmap", "gsrnamespace": "6",
                "gsrlimit": str(MAX_IMAGES), "prop": "imageinfo",
                "iiprop": "url|extmetadata", "format": "json"},
        force=force,
    )
    return "name_search", res


def run(conn, cfg, force: bool = False) -> int:
    fetcher = Fetcher(conn, "commons", min_interval=1.0)
    city_name = cfg.name
    written = 0
    for place_id, name, category, tags in _places_with_tags(conn):
        try:
            route, res = _fetch_for_place(fetcher, name, category, tags, city_name, force)
        except httpx.HTTPStatusError as exc:
            print(
                f"commons: bỏ qua '{name}' (địa điểm #{place_id}) —"
                f" lỗi HTTP {exc.response.status_code}"
            )
            continue
        except httpx.TimeoutException:
            print(f"commons: bỏ qua '{name}' (địa điểm #{place_id}) — timeout")
            continue

        if route is None:
            continue

        images = parse_imageinfo(json.loads(res.content))[:MAX_IMAGES]
        for image in images:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO place_images (place_id, image_url, license, author, route)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (image_url) DO NOTHING",
                    (place_id, image.image_url, image.license, image.author, route),
                )
                written += cur.rowcount
    return written
