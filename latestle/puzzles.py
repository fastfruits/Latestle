"""Daily puzzles for Latestle and guess checking.

A day's puzzle is a handful of rounds. Each round is a headline from the
day before with one name blacked out, and the player has a few seconds to
fill it in. Answers stay on the server until a round is over.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy import and_, delete, select
from sqlalchemy.exc import IntegrityError

from latestle import db, news

PUZZLE_DIR = Path(__file__).resolve().parent.parent / "puzzles"
SAMPLES_FILE = PUZZLE_DIR / "samples.json"

# Puzzle #1 is played on this date; the number goes up by one each day.
LAUNCH_DATE = date(2026, 11, 17)

ROUND_SECONDS = 20
ROUNDS_PER_DAY = news.ROUNDS_PER_DAY

# Guesses within this many typos still count; a little further is "close".
def allowed_typos(answer: str) -> int:
    if len(answer) <= 4:
        return 0  # otherwise "Iraq" would count for "Iran"
    return 1 if len(answer) <= 7 else 2


CLOSE_THRESHOLD = 0.70

# If the news feeds are down, don't retry on every request.
RETRY_AFTER_SECONDS = 10 * 60


@dataclass
class Round:
    answer: str
    headline: str  # with [REDACTED] where the answer goes
    original: str
    hint: str = ""  # article summary, answer redacted
    summary: str = ""  # article summary, unredacted, for the recap
    source: str = ""
    url: str = ""

    def public(self) -> dict:
        """What the player sees while the round is running (the hint is
        handed out separately, after 10 seconds)."""
        return {
            "headline": self.headline,
            "length": len(self.answer),
            "first_letter": self.answer[0].upper(),
        }

    def reveal(self) -> dict:
        return {
            "answer": self.answer,
            "original": self.original,
            "summary": self.summary,
            "source": self.source,
            "url": self.url,
        }


@dataclass
class Puzzle:
    date: date
    news_date: date
    rounds: list[Round]
    sample: bool = False
    category: str = news.TOP

    @property
    def number(self) -> int:
        return (self.date - LAUNCH_DATE).days + 1

    def meta(self) -> dict:
        """About the day's puzzle, without any headlines (those are sent one
        at a time as each round starts)."""
        return {
            "number": self.number,
            "category": self.category,
            "category_name": news.CATEGORIES[self.category].name,
            "date": self.date.isoformat(),
            "news_date": self.news_date.isoformat(),
            "round_seconds": ROUND_SECONDS,
            "total_rounds": len(self.rounds),
            "sample": self.sample,
        }

    def round(self, index: int) -> Round:
        if not 0 <= index < len(self.rounds):
            raise IndexError(index)
        return self.rounds[index]


def _round_from(data: dict) -> Round:
    fields = Round.__dataclass_fields__
    return Round(**{k: v for k, v in data.items() if k in fields})


# ---------- Loading and saving ----------

# Puzzles are dated by the player's local day, which is never more than one
# day either side of today in UTC. Older puzzles can't be played any more, so
# they're deleted whenever a new one is saved.
KEEP_DAYS_BEFORE_TODAY = 1

_locks: dict[tuple[str, date], threading.Lock] = {}
_locks_guard = threading.Lock()
_failed_at: dict[tuple[str, date], float] = {}


def _lock_for(key: tuple[str, date]) -> threading.Lock:
    """One lock per category and day, so building Science doesn't make
    everyone loading Top stories wait."""
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _where(on: date, category: str):
    c = db.puzzles.c
    return and_(c.puzzle_date == on.isoformat(), c.category == category)


def stored_puzzle(on: date, category: str = news.TOP) -> dict | None:
    """The saved puzzle data for a day and category, if there is one."""
    with db.engine().connect() as conn:
        raw = conn.execute(select(db.puzzles.c.data).where(_where(on, category))).scalar()
    return json.loads(raw) if raw else None


def save_puzzle(on: date, category: str, data: dict, replace: bool = False) -> bool:
    """Save a day's puzzle and delete ones too old to play.

    The first puzzle saved for a day is the one everyone plays: if another
    server got there first this returns False and leaves theirs alone, unless
    `replace` is set (for swapping in a hand-edited puzzle).
    """
    row = {
        "puzzle_date": on.isoformat(),
        "category": category,
        "data": json.dumps(data, ensure_ascii=False),
        "created_at": int(time.time() * 1000),
    }
    try:
        with db.engine().begin() as conn:
            if replace:
                conn.execute(delete(db.puzzles).where(_where(on, category)))
            conn.execute(db.puzzles.insert().values(**row))
    except IntegrityError:
        return False
    prune_puzzles(keep=on)
    return True


def prune_puzzles(today: date | None = None, keep: date | None = None) -> int:
    """Delete puzzles from before yesterday (UTC). `keep` protects a day that
    was just saved, e.g. an old date built by hand with the script."""
    cutoff = (today or today_utc()) - timedelta(days=KEEP_DAYS_BEFORE_TODAY)
    if keep:
        cutoff = min(cutoff, keep)
    with db.engine().begin() as conn:
        return conn.execute(delete(db.puzzles).where(db.puzzles.c.puzzle_date < cutoff.isoformat())).rowcount


def _sample_puzzle(on: date, category: str = news.TOP) -> Puzzle:
    pool = [_round_from(r) for r in json.loads(SAMPLES_FILE.read_text())]
    offset = ((on - LAUNCH_DATE).days * ROUNDS_PER_DAY) % len(pool)
    rounds = [pool[(offset + i) % len(pool)] for i in range(ROUNDS_PER_DAY)]
    return Puzzle(date=on, news_date=on - timedelta(days=1), rounds=rounds, sample=True, category=category)


def load_puzzle(on: date, category: str = news.TOP, generate: bool = True) -> Puzzle:
    """The puzzle for a day in one category.

    1. The saved puzzle if there is one (generated earlier, or put there by
       scripts/generate_puzzle.py).
    2. Otherwise build one from the previous day's headlines and save it, so
       every player gets the same rounds.
    3. If the feeds fail, fall back to the sample pool (not saved, so the
       next try after RETRY_AFTER_SECONDS can still build the real one).
    """
    if category not in news.CATEGORIES:
        raise KeyError(category)
    key = (category, on)
    data = stored_puzzle(on, category)

    if data is None and generate:
        recently_failed = time.time() - _failed_at.get(key, 0) < RETRY_AFTER_SECONDS
        if not recently_failed:
            with _lock_for(key):
                data = stored_puzzle(on, category)  # another request may have just made it
                if data is None:
                    built = news.generate(on, category)
                    if built:
                        # If another server saved one first, play theirs.
                        data = built if save_puzzle(on, category, built) else stored_puzzle(on, category)
                    else:
                        _failed_at[key] = time.time()

    if data:
        return Puzzle(
            date=on,
            news_date=date.fromisoformat(data["news_date"]),
            rounds=[_round_from(r) for r in data["rounds"]],
            category=category,
        )
    return _sample_puzzle(on, category)


# ---------- Guess checking ----------

def normalize(text: str) -> str:
    """Lowercase, strip accents, punctuation, possessives and a leading article."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"['’]s\b", "", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"^(the|a|an) ", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class GuessResult:
    correct: bool
    close: bool


def edit_distance(a: str, b: str) -> int:
    """Typos between two strings: insertions, deletions, substitutions and
    swapped neighbouring letters each count as one."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def check_guess(answer: str, guess: str) -> GuessResult:
    g, a = normalize(guess), normalize(answer)
    if not g:
        return GuessResult(False, False)
    typos = edit_distance(g, a)
    if typos <= allowed_typos(a):
        return GuessResult(True, False)
    close = typos <= allowed_typos(a) + 1 or SequenceMatcher(None, g, a).ratio() >= CLOSE_THRESHOLD
    return GuessResult(False, close)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
