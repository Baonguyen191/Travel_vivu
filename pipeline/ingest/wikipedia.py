import hashlib
import json
import re
from pathlib import Path

import httpx

from pipeline.http import Fetcher
from pipeline.ingest.wikidata import STAGED_PATH

API_URL = "https://vi.wikipedia.org/w/api.php"


def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    chunks: list[str] = []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        current = ""
        for sentence in re.split(r"(?<=\.)\s+", para):
            if len(current) + len(sentence) + 1 > max_chars and current:
                chunks.append(current.strip())
                current = ""
            current += sentence + " "
        if current.strip():
            chunks.append(current.strip())
    return chunks


def _vi_titles_by_qid(staged_path: str) -> dict[str, str]:
    """Đọc `data/staged/wikidata.json`, trả về QID -> tiêu đề Wikipedia tiếng Việt.

    Chỉ những bản ghi có sitelink tiếng Việt (`tags.vi_title`) mới xuất hiện
    trong dict này; những bản ghi còn lại được bỏ qua ở đây và sẽ rơi về tên
    địa điểm (`places.name`) ở `_titles_by_place`.
    """
    path = Path(staged_path)
    if not path.exists():
        return {}
    records = json.loads(path.read_text(encoding="utf-8"))
    titles: dict[str, str] = {}
    for record in records:
        vi_title = (record.get("tags") or {}).get("vi_title")
        qid = (record.get("external_ids") or {}).get("wikidata")
        if vi_title and qid:
            titles[qid] = vi_title
    return titles


def _titles_by_place(conn) -> list[tuple[int, str]]:
    vi_titles = _vi_titles_by_qid(STAGED_PATH)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.name, e.external_id FROM places p"
            " JOIN place_external_ids e ON e.place_id = p.id"
            " WHERE e.source = 'wikidata'"
        )
        rows = cur.fetchall()
    return [
        (place_id, vi_titles.get(qid, name))
        for place_id, name, qid in rows
    ]


def run(conn, cfg, force: bool = False) -> int:
    fetcher = Fetcher(conn, "wikipedia", min_interval=1.0)
    written = 0
    for place_id, title in _titles_by_place(conn):
        try:
            res = fetcher.fetch(
                API_URL,
                params={"action": "query", "prop": "extracts", "explaintext": "1",
                        "redirects": "1", "format": "json", "titles": title},
                force=force,
            )
        except httpx.HTTPStatusError as exc:
            print(
                f"wikipedia: bỏ qua '{title}' (địa điểm #{place_id}) —"
                f" lỗi HTTP {exc.response.status_code}"
            )
            continue
        except httpx.TimeoutException:
            print(f"wikipedia: bỏ qua '{title}' (địa điểm #{place_id}) — timeout")
            continue
        pages = json.loads(res.content).get("query", {}).get("pages", {})
        for page in pages.values():
            extract = page.get("extract")
            if not extract:
                continue
            url = f"https://vi.wikipedia.org/wiki/{page['title'].replace(' ', '_')}"
            for chunk in chunk_text(extract):
                digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO place_chunks (place_id, content, content_hash, source_url)"
                        " VALUES (%s, %s, %s, %s)"
                        " ON CONFLICT (place_id, content_hash) DO NOTHING",
                        (place_id, chunk, digest, url),
                    )
                    written += cur.rowcount
    return written
