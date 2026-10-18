"""CLI cho module Core.

    python -m core schema                 # liệt kê các kiểu
    python -m core schema Problem         # các trường của một kiểu
    python -m core example Problem        # JSON mẫu (từ core.fixtures)
    python -m core check path/to/file.json [--type Problem]     # kiểm file .json / .jsonl
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import typing
from pathlib import Path

from core import fixtures, io
from core.types import ALL_TYPES

EXAMPLES = {
    "Problem": lambda: fixtures.sample_problem(),
    "Schedule": fixtures.sample_schedule,
    "Place": lambda: fixtures.DAI_NOI,
    "Location": lambda: fixtures.HOTEL,
    "TravelMatrix": fixtures.travel_matrix,
    "WeatherHour": lambda: fixtures.weather_day(fixtures.sample_problem().start_date)[7],
}


def _fmt_type(tp) -> str:
    return getattr(tp, "__name__", None) if isinstance(tp, type) else str(tp).replace("typing.", "")


def cmd_schema(name: str | None) -> int:
    if name is None:
        for n, cls in ALL_TYPES.items():
            print(f"{n:18} {(cls.__doc__ or '').strip().splitlines()[0] if cls.__doc__ else ''}")
        return 0
    cls = ALL_TYPES.get(name)
    if cls is None:
        print(f"Không có kiểu {name}. Có: {', '.join(ALL_TYPES)}", file=sys.stderr)
        return 2
    hints = typing.get_type_hints(cls)
    for f in dataclasses.fields(cls):
        if not f.init:
            continue
        has_default = f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING
        print(f"{f.name:20} {_fmt_type(hints[f.name]):45} {'(tùy chọn)' if has_default else '(bắt buộc)'}")
    return 0


def cmd_example(name: str) -> int:
    if name not in EXAMPLES:
        print(f"Chưa có ví dụ cho {name}. Có: {', '.join(EXAMPLES)}", file=sys.stderr)
        return 2
    print(io.dumps(EXAMPLES[name](), indent=2))
    return 0


def cmd_check(path: Path, type_name: str | None) -> int:
    try:
        if path.suffix == ".jsonl":
            objs = io.load_jsonl(path, type_name)
        else:
            objs = [io.load(path, type_name)]
    except (io.SchemaError, OSError) as err:
        print(f"LỖI: {err}", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    for obj in objs:
        counts[type(obj).__name__] = counts.get(type(obj).__name__, 0) + 1
    print("OK: " + ", ".join(f"{n} {k}" for k, n in counts.items()) if counts else "OK: file rỗng")
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m core", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("schema")
    p.add_argument("type", nargs="?")
    p = sub.add_parser("example")
    p.add_argument("type")
    p = sub.add_parser("check")
    p.add_argument("path", type=Path)
    p.add_argument("--type", dest="type_name")
    args = parser.parse_args(argv)
    if args.cmd == "schema":
        return cmd_schema(args.type)
    if args.cmd == "example":
        return cmd_example(args.type)
    return cmd_check(args.path, args.type_name)


if __name__ == "__main__":
    raise SystemExit(main())
