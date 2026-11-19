"""Build (or preview) a day's Latestle puzzle from the previous day's news.

The server does this on its own the first time anyone loads a day, so you
only need this to check what a day will look like, or to pre-build it:

    python scripts/generate_puzzle.py                 # preview today's puzzle
    python scripts/generate_puzzle.py --save          # write puzzles/<today>.json
    python scripts/generate_puzzle.py --date 2026-11-18 --save --force
    python scripts/generate_puzzle.py --category science   # one category
    python scripts/generate_puzzle.py --category all --save

After saving, you can hand-edit the JSON (swap a headline, fix an answer) and
the server will use your version.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latestle import news  # noqa: E402
from latestle.puzzles import _write_atomic, puzzle_path, today_utc  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=date.fromisoformat, default=today_utc(), help="puzzle date (default: today, UTC)")
    parser.add_argument("--save", action="store_true", help="write the puzzle file")
    parser.add_argument("--force", action="store_true", help="overwrite an existing puzzle file")
    parser.add_argument("--category", default=news.TOP, choices=[*news.CATEGORIES, "all"],
                        help="which edition to build (default: top, the main one)")
    args = parser.parse_args()

    categories = list(news.CATEGORIES) if args.category == "all" else [args.category]
    ok = [build(args, category) for category in categories]
    if not args.save:
        print("(Preview only. Add --save to write it.)")
    if not all(ok):
        sys.exit(1)


def build(args, category: str) -> bool:
    name = news.CATEGORIES[category].name
    data = news.generate(args.date, category)
    if not data:
        print(f"{name}: not enough headlines to build a puzzle. Are the feeds reachable?\n", file=sys.stderr)
        return False

    print(f"{name} puzzle for {args.date} (news from {data['news_date']}):\n")
    for i, r in enumerate(data["rounds"], 1):
        print(f"{i}. [{r['answer']}] {r['headline']}")
        print(f"   {r['source']}: {r['url']}\n")

    if args.save:
        path = puzzle_path(args.date, category)
        if path.exists() and not args.force:
            print(f"{path} already exists. Use --force to overwrite it.\n", file=sys.stderr)
            return False
        _write_atomic(path, data)
        print(f"Saved {path}\n")
    return True


if __name__ == "__main__":
    main()
