import csv
import io
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import zalo_landmark as zl  # noqa: E402


def _jpeg(pattern: str, shift: int = 0) -> bytes:
    x = np.linspace(0, 255, 64)
    if pattern == "horizontal":
        arr = np.tile(x, (64, 1))
    else:
        arr = np.tile(x[::-1], (64, 1)).T
    img = Image.fromarray(np.clip(arr + shift, 0, 255).astype("uint8")).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "train_val2018.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("TrainVal/0/a.jpg", _jpeg("horizontal"))
        zf.writestr("TrainVal/0/b.jpg", _jpeg("horizontal", shift=10))  # gần trùng a
        zf.writestr("TrainVal/12/c.jpg", _jpeg("vertical"))
        zf.writestr("TrainVal/12/notes.txt", "không phải ảnh")
        zf.writestr("__MACOSX/TrainVal/12/._c.jpg", b"rac")
    return path


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(zl, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(zl, "CLASS_MAP_PATH", tmp_path / "zalo_class_map.csv")
    return tmp_path


def test_labels_from_json_accepts_common_shapes():
    assert zl.labels_from_json({"a.jpg": 3}) == {"a": "3"}
    assert zl.labels_from_json([{"file_name": "x/b.jpg", "category": 7}]) == {"b": "7"}
    assert zl.labels_from_json({"images": [{"id": "c", "label": 1}]}) == {"c": "1"}
    with pytest.raises(ValueError):
        zl.labels_from_json([{"foo": 1}])


def test_index_zip_uses_folder_or_json_labels(dataset):
    with zipfile.ZipFile(dataset) as zf:
        assert zl.index_zip(zf, None) == {
            "0": ["TrainVal/0/a.jpg", "TrainVal/0/b.jpg"],
            "12": ["TrainVal/12/c.jpg"],
        }
        assert zl.index_zip(zf, {"a": "5", "c": "5"}) == {"5": ["TrainVal/0/a.jpg", "TrainVal/12/c.jpg"]}


def test_dhash_separates_near_and_far_images(dataset):
    with zipfile.ZipFile(dataset) as zf:
        a, b, c = (zl.dhash(zl._open(zf, f"TrainVal/{n}")) for n in ("0/a.jpg", "0/b.jpg", "12/c.jpg"))
    assert zl.hamming(a, b) <= 6
    assert zl.hamming(a, c) > 20


def test_sheet_writes_html_and_template_without_overwriting(dataset, paths):
    zl.main(["sheet", str(dataset)])
    html = (paths / "out" / "contact_sheet.html").read_text(encoding="utf-8")
    assert "Lớp 0 — 2 ảnh" in html and "Lớp 12 — 1 ảnh" in html
    assert html.count("<img ") == 3
    rows = list(csv.DictReader(open(paths / "zalo_class_map.csv", encoding="utf-8")))
    assert [(r["class_id"], r["n_images"], r["place_key"]) for r in rows] == [("0", "2", ""), ("12", "1", "")]

    (paths / "zalo_class_map.csv").write_text(
        "class_id,n_images,place_key,note\n0,2,wikidata:Q10769129,Đại Nội\n12,1,,\n", encoding="utf-8")
    zl.main(["sheet", str(dataset)])
    assert "wikidata:Q10769129" in (paths / "zalo_class_map.csv").read_text(encoding="utf-8")


def test_dedup_only_mapped_classes(dataset, paths):
    with pytest.raises(SystemExit):
        zl.main(["dedup", str(dataset)])
    (paths / "zalo_class_map.csv").write_text(
        "class_id,n_images,place_key,note\n0,2,wikidata:Q10769129,\n12,1,,\n", encoding="utf-8")
    zl.main(["dedup", str(dataset)])
    rows = list(csv.DictReader(open(paths / "out" / "near_duplicates.csv", encoding="utf-8")))
    assert [(r["image_a"], r["image_b"]) for r in rows] == [("TrainVal/0/a.jpg", "TrainVal/0/b.jpg")]
