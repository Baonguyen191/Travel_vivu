"""Lưu/đọc kiểu của `core.types` ra JSON, kèm tên kiểu và phiên bản schema.

Mỗi bản ghi là một "phong bì": {"schema": "Problem", "version": 1, "data": {...}}. Đọc lại luôn
kiểm hợp lệ qua `from_dict`, nên file sai (thiếu trường, sai khoảng giá trị, sai phiên bản) bị
phát hiện ngay khi nạp, không lan sang module khác.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from core.types import ALL_TYPES

SCHEMA_VERSION = 1


class SchemaError(ValueError):
    """Dữ liệu không đúng schema của core."""


def _type_name(expected: type | str | None) -> str | None:
    if expected is None:
        return None
    return expected if isinstance(expected, str) else expected.__name__


def envelope(obj: Any) -> dict:
    name = type(obj).__name__
    if name not in ALL_TYPES:
        raise SchemaError(f"không phải kiểu của core.types: {name}")
    return {"schema": name, "version": SCHEMA_VERSION, "data": obj.to_dict()}


def from_envelope(env: Any, expected: type | str | None = None) -> Any:
    if not isinstance(env, dict) or {"schema", "version", "data"} - set(env):
        raise SchemaError("cần object có đủ schema, version, data")
    name, want = env["schema"], _type_name(expected)
    cls = ALL_TYPES.get(name)
    if cls is None:
        raise SchemaError(f"schema lạ: {name!r}")
    if want and name != want:
        raise SchemaError(f"cần {want}, nhận {name}")
    if env["version"] != SCHEMA_VERSION:
        raise SchemaError(f"{name}: phiên bản {env['version']} khác {SCHEMA_VERSION}")
    try:
        return cls.from_dict(env["data"])
    except (ValueError, TypeError, KeyError) as err:
        raise SchemaError(f"{name}: {err}") from err


def dumps(obj: Any, indent: int | None = None) -> str:
    return json.dumps(envelope(obj), ensure_ascii=False, indent=indent)


def loads(text: str, expected: type | str | None = None) -> Any:
    try:
        env = json.loads(text)
    except json.JSONDecodeError as err:
        raise SchemaError(f"JSON hỏng: {err}") from err
    return from_envelope(env, expected)


def save(obj: Any, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(obj, indent=2) + "\n", encoding="utf-8")


def load(path: Path, expected: type | str | None = None) -> Any:
    return loads(Path(path).read_text(encoding="utf-8"), expected)


def save_jsonl(objs: Iterable[Any], path: Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for obj in objs:
            fh.write(dumps(obj) + "\n")
            n += 1
    return n


def iter_jsonl(path: Path, expected: type | str | None = None) -> Iterator[Any]:
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield loads(line, expected)
            except SchemaError as err:
                raise SchemaError(f"{Path(path).name} dòng {lineno}: {err}") from err


def load_jsonl(path: Path, expected: type | str | None = None) -> list[Any]:
    return list(iter_jsonl(path, expected))
