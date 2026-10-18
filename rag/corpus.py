"""Dựng kho chunk từ snapshot Wikipedia trong `data/raw/wikipedia/`.

Mỗi bài cắt theo mục (dòng `== Tiêu đề ==`), trong mục gom đoạn văn tới
`MAX_CHARS`. Bỏ các mục không mang nội dung (Tham khảo, Liên kết ngoài...)
cùng mọi mục con của chúng. Bài được gắn `place_key` qua `tags.vi_title`
của `data/staged/wikidata.json`.

    python -m rag.corpus        # ghi data/rag/chunks.jsonl và in thống kê
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from rag import RAG_DIR, ROOT

RAW_WIKIPEDIA = ROOT / "data" / "raw" / "wikipedia"
STAGED_WIKIDATA = ROOT / "data" / "staged" / "wikidata.json"
CHUNKS_PATH = RAG_DIR / "chunks.jsonl"

MAX_CHARS = 1200
MIN_CHARS = 200
SKIP_SECTIONS = {
    "tham khảo", "chú thích", "liên kết ngoài", "xem thêm", "đọc thêm",
    "ghi chú", "thư mục", "nguồn", "tài liệu tham khảo", "hình ảnh", "thư viện ảnh",
}
_HEADER = re.compile(r"^(=+)\s*(.*?)\s*\1$")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    place_key: str | None
    title: str
    section: str
    text: str

    @property
    def embedding_text(self) -> str:
        """Gắn tên bài vào trước: đoạn Wikipedia hay viết "Lăng được xây..." mà không nhắc tên."""
        return f"{self.title}: {self.text}"


def load_pages(raw_dir: Path = RAW_WIKIPEDIA) -> tuple[dict[str, dict], dict[str, str]]:
    """({title → page}, {tiêu đề gốc → tiêu đề sau redirect}). Snapshot mới hơn ghi đè cũ."""
    pages: dict[str, dict] = {}
    redirects: dict[str, str] = {}
    for snapshot in sorted(p for p in raw_dir.iterdir() if p.is_dir()):
        for path in sorted(snapshot.iterdir()):
            query = json.loads(path.read_text(encoding="utf-8")).get("query", {})
            for r in query.get("normalized", []) + query.get("redirects", []):
                redirects[r["from"]] = r["to"]
            for page in query.get("pages", {}).values():
                if page.get("extract"):
                    pages[page["title"]] = page
    return pages, redirects


def title_place_keys(redirects: dict[str, str], staged: Path = STAGED_WIKIDATA) -> dict[str, str]:
    """{tiêu đề bài Wikipedia → `wikidata:<QID>`}."""
    mapping: dict[str, str] = {}
    for item in json.loads(staged.read_text(encoding="utf-8")):
        title = (item.get("tags") or {}).get("vi_title")
        qid = (item.get("external_ids") or {}).get("wikidata")
        if title and qid:
            mapping[redirects.get(title, title)] = f"wikidata:{qid}"
    return mapping


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    parts, current = [], ""
    for sentence in _SENTENCE_END.split(paragraph):
        if current and len(current) + 1 + len(sentence) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    # Câu đơn dài hơn max_chars: cắt cứng.
    return [p[i:i + max_chars] for p in parts for i in range(0, len(p), max_chars)]


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(đường dẫn mục "A > B", nội dung)], bỏ mục thuộc SKIP_SECTIONS và mọi mục con của nó."""
    sections: list[tuple[str, list[str] | None]] = [("", [])]
    path: list[tuple[int, str]] = []
    skip_level: int | None = None
    for line in text.splitlines():
        match = _HEADER.match(line.strip())
        if match:
            level, name = len(match.group(1)), match.group(2)
            if skip_level is not None and level <= skip_level:
                skip_level = None
            path = [(lvl, n) for lvl, n in path if lvl < level] + [(level, name)]
            if skip_level is None and name.casefold() in SKIP_SECTIONS:
                skip_level = level
            sections.append((" > ".join(n for _, n in path), [] if skip_level is None else None))
            continue
        lines = sections[-1][1]
        if lines is not None:
            lines.append(line)
    return [(name, body) for name, lines in sections
            if lines is not None and (body := "\n".join(lines).strip())]


def chunk_text(text: str, max_chars: int = MAX_CHARS, min_chars: int = MIN_CHARS) -> list[tuple[str, str]]:
    """[(mục, đoạn ≤ max_chars)]. Đoạn cuối mục ngắn hơn min_chars được gộp vào đoạn trước cùng mục."""
    chunks: list[tuple[str, str]] = []
    for section, body in split_sections(text):
        paragraphs = [p for raw in body.split("\n") if (p := raw.strip())]
        pieces = [piece for p in paragraphs for piece in _split_long(p, max_chars)]
        start = len(chunks)
        current = ""
        for piece in pieces:
            if current and len(current) + 1 + len(piece) > max_chars:
                chunks.append((section, current))
                current = piece
            else:
                current = f"{current}\n{piece}".strip()
        if not current:
            continue
        if len(current) < min_chars and len(chunks) > start:
            chunks[-1] = (section, f"{chunks[-1][1]}\n{current}")
        elif len(current) < min_chars and chunks:
            # Cả mục quá ngắn: gộp vào chunk trước (mục khác), giữ tên mục trong nội dung.
            prev_section, prev_text = chunks[-1]
            chunks[-1] = (prev_section, f"{prev_text}\n{section}: {current}" if section else f"{prev_text}\n{current}")
        else:
            chunks.append((section, current))
    return chunks


def build_chunks(raw_dir: Path = RAW_WIKIPEDIA, staged: Path = STAGED_WIKIDATA) -> list[Chunk]:
    pages, redirects = load_pages(raw_dir)
    keys = title_place_keys(redirects, staged)
    chunks = []
    for title in sorted(pages):
        page = pages[title]
        for i, (section, text) in enumerate(chunk_text(page["extract"])):
            chunks.append(Chunk(f"{page['pageid']}-{i}", keys.get(title), title, section, text))
    return chunks


def save_chunks(chunks: list[Chunk], path: Path = CHUNKS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")


def load_chunks(path: Path = CHUNKS_PATH) -> list[Chunk]:
    if not path.exists():
        raise FileNotFoundError(f"Chưa có {path}; chạy `python -m rag.corpus` trước.")
    with open(path, encoding="utf-8") as fh:
        return [Chunk(**json.loads(line)) for line in fh if line.strip()]


def corpus_hash(chunks: list[Chunk]) -> str:
    """Băm nội dung kho, dùng làm khóa cache embedding."""
    digest = hashlib.sha256()
    for c in chunks:
        digest.update(f"{c.chunk_id}\x1f{c.embedding_text}\x1e".encode("utf-8"))
    return digest.hexdigest()[:12]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    chunks = build_chunks()
    save_chunks(chunks)
    lengths = sorted(len(c.text) for c in chunks)
    pages = {c.title for c in chunks}
    mapped = {c.title for c in chunks if c.place_key}
    print(f"{len(chunks)} chunk từ {len(pages)} bài ({len(mapped)} bài gắn được place_key) → {CHUNKS_PATH}")
    print(f"Độ dài chunk (ký tự): min {lengths[0]}, trung vị {lengths[len(lengths) // 2]}, max {lengths[-1]}")
    unmapped = sorted(pages - mapped)
    if unmapped:
        print(f"Bài không gắn place_key: {', '.join(unmapped)}")


if __name__ == "__main__":
    main()
