import argparse
import importlib

from pipeline import db

INGEST_MODULES = {
    "boundary": "pipeline.ingest.boundary",
    "wikidata": "pipeline.ingest.wikidata",
    "osm": "pipeline.ingest.osm",
    "wikipedia": "pipeline.ingest.wikipedia",
    "commons": "pipeline.ingest.commons",
    "weather": "pipeline.ingest.weather",
}

# Thứ tự chạy của lệnh `all`. boundary/wikidata/osm phải chạy trước load
# (chúng cấp nguyên liệu cho bước normalize mà load chạy bên trong nó).
# wikipedia và commons cần places đã có id nên chạy sau load. embed sinh
# vector cho các chunk mà wikipedia vừa ghi nên chạy ngay sau nó. weather độc
# lập với các place, nên chạy sau cùng. `normalize` không nằm trong danh
# sách này: `load` đã tự chạy đúng một lượt normalize rồi upsert, đưa cả
# hai vào `all` sẽ chạy normalize hai lần cho cùng một dữ liệu. `normalize`
# vẫn còn là lệnh đứng riêng cho ai muốn xem báo cáo gộp/nhãn mà không ghi
# vào DB.
PIPELINE_ORDER = [
    "migrate",
    "boundary",
    "wikidata",
    "osm",
    "load",
    "wikipedia",
    "embed",
    "commons",
    "weather",
    "qa",
]


def _run_step(step: str, conn, force: bool = False) -> int:
    if step == "download-model":
        from pipeline.embed import check_dimension, get_embedder

        embedder = get_embedder()
        check_dimension(embedder)
        print(f"Model '{embedder.model_name}' sẵn sàng ({embedder.dimension} chiều)")
        return 0

    if step == "migrate":
        applied = db.run_migrations(conn)
        print(f"Đã chạy {len(applied)} migration: {', '.join(applied) or 'không có'}")
        return 0

    from pipeline.config import load_city

    cfg = load_city()

    if step == "qa":
        from pipeline.qa import coverage

        coverage.run(conn, cfg)
        return 0

    if step == "label-agreement":
        from pipeline.qa import label_agreement

        label_agreement.run(conn, cfg)
        return 0

    if step in {"normalize", "load"}:
        from pipeline.load.upsert import upsert_places
        from pipeline.normalize import pipeline as normalize_pipeline

        places, review, _dropped = normalize_pipeline.run(conn, cfg)
        print(f"normalize: {len(places)} địa điểm, {len(review)} cặp chờ xem tay")
        if step == "load":
            inserted, updated = upsert_places(conn, places)
            print(f"load: thêm {inserted}, cập nhật {updated}")
        return 0

    if step == "embed":
        from pipeline.embed import run_embed_chunks

        count = run_embed_chunks(conn, force=force)
        print(f"embed: {count} chunk")
        return 0

    if step not in INGEST_MODULES:
        valid = ", ".join(sorted(INGEST_MODULES))
        print(f"Nguồn không hợp lệ: '{step}'. Các nguồn hợp lệ: {valid}")
        return 1

    module = importlib.import_module(INGEST_MODULES[step])
    count = module.run(conn, cfg, force=force)
    print(f"{step}: {count} bản ghi")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "normalize", "load", "qa", "label-agreement", "all", "download-model"):
        sub.add_parser(name)
    embed_parser = sub.add_parser("embed")
    embed_parser.add_argument("--force", action="store_true")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.command == "ingest" and args.source not in INGEST_MODULES:
        valid = ", ".join(sorted(INGEST_MODULES))
        print(f"Nguồn không hợp lệ: '{args.source}'. Các nguồn hợp lệ: {valid}")
        return 1

    if args.command == "download-model":
        return _run_step(args.command, conn=None)

    conn = db.connect()
    if args.command == "all":
        for step in PIPELINE_ORDER:
            status = _run_step(step, conn)
            if status != 0:
                return status
        return 0
    if args.command == "ingest":
        return _run_step(args.source, conn, force=args.force)
    return _run_step(args.command, conn, force=getattr(args, "force", False))


if __name__ == "__main__":
    raise SystemExit(main())
