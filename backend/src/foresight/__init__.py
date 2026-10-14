"""Foresight: local semantic search, Q&A and class notes over your study materials."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="foresight")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_index = sub.add_parser("index", help="(re)index the corpus folder")
    p_index.add_argument("--force", action="store_true", help="re-embed every file")
    p_search = sub.add_parser("search", help="hybrid search")
    p_search.add_argument("query")
    p_search.add_argument("-k", type=int, default=8)
    p_search.add_argument("-s", "--source", action="append")
    p_ask = sub.add_parser("ask", help="answer a question with citations")
    p_ask.add_argument("question")
    p_ask.add_argument("-k", type=int, default=6)
    sub.add_parser("serve", help="run the HTTP API for the desktop app")
    args = ap.parse_args()

    from .config import Settings

    s = Settings.load()
    if args.cmd == "serve":
        from .server import run

        run(s)
        return

    from .indexer import Indexer
    from .runtime import make_runtime
    from .search import search, stream_answer
    from .store import Store

    rt = make_runtime(s)
    profile = s.load_profile()
    if rt.remote:  # scanned pages: OCR on the PC (Windows OCR is much faster)
        from . import ocr

        ocr.use_remote(rt.ocr_images)
    store = Store(s.data_dir, s.embed_dims)
    try:
        if args.cmd == "index":
            p = Indexer(s, store, rt, profile).run(force=args.force, log=print)
            mins = (p.finished - p.started) / 60
            print(f"\n{p.state}: {p.files_done} files, {p.chunks_done} chunks in {mins:.1f} min; "
                  f"{p.skipped_duplicates} duplicates skipped; total rows {store.count()}")
            for e in p.errors:
                print("  error:", e)
            if p.needs_ocr:
                print(f"  {len(p.needs_ocr)} files look scanned (need OCR):")
                for r in p.needs_ocr:
                    print("   -", r)
            return

        rt.start_embed()
        if args.cmd == "search":
            for i, h in enumerate(search(store, rt, args.query, args.k, args.source), 1):
                kw = " +kw" if h["keyword"] else ""
                print(f"\n[{i}] {h['source']} · {h['title']} · p.{h['page']}  "
                      f"(sim {h['similarity']}{kw})\n    {h['section']}\n    {h['text'][:300]}…")
        elif args.cmd == "ask":
            hits = search(store, rt, args.question, args.k, max_per_source=3)
            for chunk in stream_answer(rt, args.question, hits, profile):
                print(chunk, end="", flush=True)
            print("\n\nSources:")
            for i, h in enumerate(hits, 1):
                print(f"  [{i}] {h['title']}, p. {h['page']}")
    finally:
        rt.stop()
