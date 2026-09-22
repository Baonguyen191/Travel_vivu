import argparse

from pipeline import db


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    args = parser.parse_args()

    if args.command == "migrate":
        conn = db.connect()
        applied = db.run_migrations(conn)
        print(f"Đã chạy {len(applied)} migration: {', '.join(applied) or 'không có'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
