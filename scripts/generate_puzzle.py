"""Build (or preview) a day's Latestle puzzle from the previous day's news.

The server does this on its own the first time anyone loads a day (and a
daily cron job does it on Vercel), so you only need this to check what a day
will look like, or to swap in a hand-edited puzzle:

    python scripts/generate_puzzle.py                 # preview today's puzzle
    python scripts/generate_puzzle.py --save          # save it to the database
    python scripts/generate_puzzle.py --date 2026-11-18 --save --force
    python scripts/generate_puzzle.py --category science   # one category
    python scripts/generate_puzzle.py --category all --save

To hand-edit a puzzle (swap a headline, fix an answer):

    python scripts/generate_puzzle.py --json > puzzle.json
    # ...edit puzzle.json...
    python scripts/generate_puzzle.py --from-file puzzle.json --force

Puzzles are saved to the database in DATABASE_URL (from .env, if there is
one), or the local SQLite database if that's not set.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from latestle import news  # noqa: E402
from latestle.puzzles import save_puzzle, stored_puzzle, today_utc  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=date.fromisoformat, default=today_utc(), help="puzzle date (default: today, UTC)")
    parser.add_argument("--save", action="store_true", help="save the puzzle to the database")
    parser.add_argument("--force", action="store_true", help="replace a puzzle that's already saved")
    parser.add_argument("--category", default=news.TOP, choices=[*news.CATEGORIES, "all"],
                        help="which edition to build (default: top, the main one)")
    parser.add_argument("--json", action="store_true", help="print the puzzle as JSON, for hand-editing")
    parser.add_argument("--from-file", type=Path, metavar="FILE",
                        help="save this (hand-edited) JSON puzzle instead of building one")
    args = parser.parse_args()

    if args.from_file:
        if args.category == "all":
            parser.error("--from-file needs a single --category")
        data = json.loads(args.from_file.read_text())
        ok = save(args, args.category, data)
        sys.exit(0 if ok else 1)

    categories = list(news.CATEGORIES) if args.category == "all" else [args.category]
    ok = [build(args, category) for category in categories]
    if not args.save and not args.json:
        print("(Preview only. Add --save to save it.)")
    if not all(ok):
        sys.exit(1)


def build(args, category: str) -> bool:
    name = news.CATEGORIES[category].name
    data = news.generate(args.date, category)
    if not data:
        print(f"{name}: not enough headlines to build a puzzle. Are the feeds reachable?\n", file=sys.stderr)
        return False

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"{name} puzzle for {args.date} (news from {data['news_date']}):\n")
        for i, r in enumerate(data["rounds"], 1):
            print(f"{i}. [{r['answer']}] {r['headline']}")
            print(f"   {r['source']}: {r['url']}\n")

    return save(args, category, data) if args.save else True


def save(args, category: str, data: dict) -> bool:
    name = news.CATEGORIES[category].name
    if stored_puzzle(args.date, category) and not args.force:
        print(f"{name} for {args.date} is already saved. Use --force to replace it.\n", file=sys.stderr)
        return False
    save_puzzle(args.date, category, data, replace=args.force)
    print(f"Saved {name} for {args.date}\n", file=sys.stderr)
    return True


if __name__ == "__main__":
    main()
