import argparse

from pipeline import db

INGEST_MODULES = {"boundary": "pipeline.ingest.boundary"}


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--force", action="store_true")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
