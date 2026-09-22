import hashlib
import json
import re

from pipeline.http import Fetcher

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


def _titles_by_place(conn) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.name FROM places p"
            " JOIN place_external_ids e ON e.place_id = p.id"
            " WHERE e.source = 'wikidata'"
        )
        return cur.fetchall()


def run(conn, cfg, force: bool = False) -> int:
    fetcher = Fetcher(conn, "wikipedia", min_interval=1.0)
    written = 0
    for place_id, title in _titles_by_place(conn):
        res = fetcher.fetch(
            API_URL,
            params={"action": "query", "prop": "extracts", "explaintext": "1",
                    "redirects": "1", "format": "json", "titles": title},
            force=force,
        )
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
