"""CLI nhanh cho module RAG.

    python -m rag build                           # dựng lại data/rag/chunks.jsonl
    python -m rag search "Chùa Thiên Mụ xây năm nào?" [-k 5] [--place wikidata:Q975568]
    python -m rag ask "Chùa Thiên Mụ xây năm nào?" [-k 5] [--place ...]
    python -m rag ask "..." --json                # in Answer dạng JSON

Benchmark: `python -m rag.benchmark retrieval|generation`; số liệu: notebooks/eval_rag.ipynb.
"""

from __future__ import annotations

import argparse
import json
import sys

from rag import api, corpus


def _print_passages(passages) -> None:
    for i, p in enumerate(passages, start=1):
        print(f"[{i}] {p.title} — {p.section or 'Mở đầu'}  ({p.chunk_id}, {p.place_key}, score={p.score:.4f})")
        print(f"    {p.text[:240].replace(chr(10), ' ')}{'…' if len(p.text) > 240 else ''}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m rag", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="dựng kho chunk từ data/raw/wikipedia")
    for name in ("search", "ask"):
        p = sub.add_parser(name)
        p.add_argument("query")
        p.add_argument("-k", type=int, default=api.DEFAULT_K)
        p.add_argument("--place", default=None, help="chỉ lấy đoạn của một địa danh, vd wikidata:Q975568")
        p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "build":
        corpus.main()
        return 0
    try:
        if args.cmd == "search":
            passages = api.retrieve(args.query, args.k, args.place)
            if args.json:
                print(json.dumps([p.to_dict() for p in passages], ensure_ascii=False, indent=2))
            else:
                _print_passages(passages) if passages else print("Không có đoạn nào.")
        else:
            reply = api.answer(args.query, args.k, args.place)
            if args.json:
                print(json.dumps(reply.to_dict(), ensure_ascii=False, indent=2))
            else:
                print(reply.text, "\n")
                print(f"Model {reply.model} · " + " · ".join(f"{k} {v:.0f}" for k, v in reply.timings.items())
                      + (" · TỪ CHỐI" if reply.refused else ""))
                print("Trích dẫn:")
                _print_passages(reply.citations) if reply.citations else print("  (không có)")
    except (ValueError, api.LLMUnavailableError) as err:
        print(f"Lỗi: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
