"""Nhập dataset Zalo AI Challenge 2018 — Landmark Identification (103 lớp, nhãn là số).

Đọc ảnh thẳng từ file zip, không giải nén. Nhãn lấy từ file JSON đi kèm nếu có,
không thì lấy tên thư mục cha của ảnh trong zip.

    # 1. Trang ảnh mẫu từng lớp để xem tay lớp nào là địa danh nào ở Huế
    python scripts/zalo_landmark.py sheet path/to/train_val2018.zip [--json train_val2018.json]
    #    → data/zalo_landmark/contact_sheet.html
    #    → config/zalo_class_map.csv (mẫu; điền cột place_key cho lớp thuộc Huế)

    # 2. Tìm ảnh gần trùng trong các lớp đã map (để lọc trước khi chia train/dev)
    python scripts/zalo_landmark.py dedup path/to/train_val2018.zip [--json ...]
    #    → data/zalo_landmark/near_duplicates.csv

Giấy phép dữ liệu cuộc thi không công bố: chỉ dùng để train, không phát hành lại ảnh.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import json
import random
import sys
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from label_source import ROOT  # noqa: E402

OUT_DIR = ROOT / "data" / "zalo_landmark"
CLASS_MAP_PATH = ROOT / "config" / "zalo_class_map.csv"
CLASS_MAP_FIELDS = ["class_id", "n_images", "place_key", "note"]
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
FILE_KEYS = ("file_name", "filename", "image", "image_id", "id", "path")
LABEL_KEYS = ("category", "category_id", "label", "class", "class_id", "landmark_id")


def _label_from_entry(entry: dict) -> tuple[str, str] | None:
    file = next((str(entry[k]) for k in FILE_KEYS if k in entry), None)
    label = next((str(entry[k]) for k in LABEL_KEYS if k in entry), None)
    return (file, label) if file is not None and label is not None else None


def labels_from_json(data) -> dict[str, str]:
    """{tên file (không đuôi) → lớp}. Nhận dict {file: lớp}, list bản ghi, hoặc {"images": [...]}."""
    if isinstance(data, dict) and isinstance(data.get("images"), list):
        data = data["images"]
    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict):
        pairs = [(str(k), str(v)) for k, v in data.items() if not isinstance(v, (dict, list))]
    elif isinstance(data, list):
        pairs = [p for e in data if isinstance(e, dict) and (p := _label_from_entry(e))]
    if not pairs:
        raise ValueError("Không nhận ra cấu trúc JSON nhãn; cần trường tên file và trường lớp.")
    return {PurePosixPath(f).stem: label for f, label in pairs}


def _class_sort(label: str):
    return (0, int(label), "") if label.isdigit() else (1, 0, label)


def index_zip(zf: zipfile.ZipFile, labels: dict[str, str] | None) -> dict[str, list[str]]:
    """{lớp → [tên thành viên zip]}, sắp theo tên để lặp lại được."""
    by_class: dict[str, list[str]] = defaultdict(list)
    for name in zf.namelist():
        path = PurePosixPath(name)
        if path.suffix.lower() not in IMAGE_SUFFIXES or path.name.startswith("._"):
            continue
        label = labels.get(path.stem) if labels is not None else (path.parent.name or None)
        if label is not None:
            by_class[label].append(name)
    return {k: sorted(v) for k, v in sorted(by_class.items(), key=lambda kv: _class_sort(kv[0]))}


def _open(zf: zipfile.ZipFile, name: str) -> Image.Image:
    with zf.open(name) as fh:
        img = Image.open(io.BytesIO(fh.read()))
        img.load()
    return img.convert("RGB")


def dhash(img: Image.Image, size: int = 8) -> int:
    """Difference hash 64 bit: so sánh độ sáng các điểm kề nhau trên ảnh xám 9×8."""
    gray = np.asarray(img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS), dtype=np.int16)
    value = 0
    for bit in (gray[:, 1:] > gray[:, :-1]).flatten():
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _thumb_uri(img: Image.Image, px: int = 160) -> str:
    img = img.copy()
    img.thumbnail((px, px))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def write_contact_sheet(zf: zipfile.ZipFile, by_class: dict[str, list[str]], per_class: int, out: Path) -> None:
    rng = random.Random(0)
    parts = ["<!doctype html><meta charset=utf-8><title>Zalo landmark — ảnh mẫu</title>",
             "<style>body{font-family:sans-serif;margin:16px}section{margin-bottom:24px}"
             "img{height:120px;margin:2px;object-fit:cover}</style>"]
    for label, names in by_class.items():
        imgs = []
        for name in rng.sample(names, min(per_class, len(names))):
            try:
                imgs.append(f'<img src="{_thumb_uri(_open(zf, name))}" title="{html.escape(name)}">')
            except OSError:
                continue
        parts.append(f"<section><h2>Lớp {html.escape(label)} — {len(names)} ảnh</h2>{''.join(imgs)}</section>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")


def write_class_map_template(by_class: dict[str, list[str]], path: Path) -> bool:
    """Ghi mẫu bảng map lớp → place_key. Không ghi đè bảng đã có (có thể đã điền tay)."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CLASS_MAP_FIELDS)
        writer.writeheader()
        for label, names in by_class.items():
            writer.writerow({"class_id": label, "n_images": len(names), "place_key": "", "note": ""})
    return True


def mapped_classes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["class_id"]: r["place_key"].strip()
                for r in csv.DictReader(fh) if (r.get("place_key") or "").strip()}


def near_duplicates(hashes: list[tuple[str, str, int]], max_dist: int) -> list[tuple[str, str, str, str, int]]:
    """Mọi cặp ảnh có khoảng cách Hamming ≤ max_dist, kể cả khác lớp (khác lớp = nghi nhãn sai)."""
    pairs = []
    for i, (cls_a, name_a, h_a) in enumerate(hashes):
        for cls_b, name_b, h_b in hashes[i + 1:]:
            d = hamming(h_a, h_b)
            if d <= max_dist:
                pairs.append((cls_a, name_a, cls_b, name_b, d))
    return pairs


def _load_labels(json_path: str | None) -> dict[str, str] | None:
    if json_path is None:
        return None
    with open(json_path, encoding="utf-8") as fh:
        return labels_from_json(json.load(fh))


def cmd_sheet(args) -> None:
    with zipfile.ZipFile(args.zip) as zf:
        by_class = index_zip(zf, _load_labels(args.json))
        if not by_class:
            sys.exit("Không thấy ảnh có nhãn trong zip.")
        out = OUT_DIR / "contact_sheet.html"
        write_contact_sheet(zf, by_class, args.per_class, out)
    total = sum(len(v) for v in by_class.values())
    print(f"{len(by_class)} lớp, {total} ảnh → {out}")
    if write_class_map_template(by_class, CLASS_MAP_PATH):
        print(f"Mẫu bảng map → {CLASS_MAP_PATH}: điền place_key (vd wikidata:Q10769129) cho lớp thuộc Huế.")
    else:
        print(f"{CLASS_MAP_PATH} đã có, không ghi đè.")


def cmd_dedup(args) -> None:
    classes = mapped_classes(CLASS_MAP_PATH)
    if not classes:
        sys.exit(f"Chưa có lớp nào được map trong {CLASS_MAP_PATH}; chạy `sheet` rồi điền place_key trước.")
    hashes = []
    with zipfile.ZipFile(args.zip) as zf:
        by_class = index_zip(zf, _load_labels(args.json))
        for label in classes:
            for name in by_class.get(label, []):
                try:
                    hashes.append((label, name, dhash(_open(zf, name))))
                except OSError:
                    print(f"Bỏ qua ảnh hỏng: {name}")
    pairs = near_duplicates(hashes, args.max_dist)
    out = OUT_DIR / "near_duplicates.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["class_a", "image_a", "class_b", "image_b", "hamming"])
        writer.writerows(pairs)
    cross = sum(1 for p in pairs if p[0] != p[2])
    print(f"{len(hashes)} ảnh trong {len(classes)} lớp; {len(pairs)} cặp gần trùng ({cross} cặp khác lớp) → {out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sheet = sub.add_parser("sheet")
    sheet.add_argument("--per-class", type=int, default=12)
    sheet.set_defaults(func=cmd_sheet)
    dedup = sub.add_parser("dedup")
    dedup.add_argument("--max-dist", type=int, default=6)
    dedup.set_defaults(func=cmd_dedup)
    for p in (sheet, dedup):
        p.add_argument("zip")
        p.add_argument("--json", help="file nhãn đi kèm (vd train_val2018.json)")
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
