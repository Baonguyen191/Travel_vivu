import argparse

from pipeline import db

INGEST_MODULES = {
    "boundary": "pipeline.ingest.boundary",
    "wikidata": "pipeline.ingest.wikidata",
    "osm": "pipeline.ingest.osm",
}


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--force", action="store_true")
    sub.add_parser("normalize")
    sub.add_parser("load")
    args = parser.parse_args()

    if args.command == "migrate":
        conn = db.connect()
        applied = db.run_migrations(conn)
        print(f"Đã chạy {len(applied)} migration: {', '.join(applied) or 'không có'}")
    elif args.command == "ingest":
        import importlib

        from pipeline.config import load_city

        if args.source not in INGEST_MODULES:
            valid = ", ".join(sorted(INGEST_MODULES))
            print(f"Nguồn không hợp lệ: '{args.source}'. Các nguồn hợp lệ: {valid}")
            return 1

        module = importlib.import_module(INGEST_MODULES[args.source])
        conn = db.connect()
        count = module.run(conn, load_city(), force=args.force)
        print(f"{args.source}: {count} bản ghi")
    elif args.command in {"normalize", "load"}:
        from pipeline.config import load_city
        from pipeline.load.upsert import upsert_places
        from pipeline.normalize import pipeline as normalize_pipeline

        conn = db.connect()
        cfg = load_city()
        places, review, _dropped = normalize_pipeline.run(conn, cfg)
        print(f"normalize: {len(places)} địa điểm, {len(review)} cặp chờ xem tay")
        if args.command == "load":
            inserted, updated = upsert_places(conn, places)
            print(f"load: thêm {inserted}, cập nhật {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
