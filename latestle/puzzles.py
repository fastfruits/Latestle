"""Daily puzzles for Latestle and guess checking.

A day's puzzle is a handful of rounds. Each round is a headline from the
day before with one name blacked out, and the player has a few seconds to
fill it in. Answers stay on the server until a round is over.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from latestle import news

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
        return {
            "headline": self.headline,
            "length": len(self.answer),
            "first_letter": self.answer[0].upper(),
            "hint": self.hint,
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

    @property
    def number(self) -> int:
        return (self.date - LAUNCH_DATE).days + 1

    def public(self) -> dict:
        return {
            "number": self.number,
            "date": self.date.isoformat(),
            "news_date": self.news_date.isoformat(),
            "round_seconds": ROUND_SECONDS,
            "rounds": [r.public() for r in self.rounds],
            "sample": self.sample,
        }

    def round(self, index: int) -> Round:
        if not 0 <= index < len(self.rounds):
            raise IndexError(index)
        return self.rounds[index]


def _round_from(data: dict) -> Round:
    fields = Round.__dataclass_fields__
    return Round(**{k: v for k, v in data.items() if k in fields})


# ---------- Loading ----------

_lock = threading.Lock()
_failed_at: dict[date, float] = {}


def _write_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _sample_puzzle(on: date) -> Puzzle:
    pool = [_round_from(r) for r in json.loads(SAMPLES_FILE.read_text())]
    offset = ((on - LAUNCH_DATE).days * ROUNDS_PER_DAY) % len(pool)
    rounds = [pool[(offset + i) % len(pool)] for i in range(ROUNDS_PER_DAY)]
    return Puzzle(date=on, news_date=on - timedelta(days=1), rounds=rounds, sample=True)


def load_puzzle(on: date, generate: bool = True) -> Puzzle:
    """The puzzle for a day.

    1. puzzles/YYYY-MM-DD.json if it exists (hand-written or generated earlier).
    2. Otherwise build one from the previous day's headlines and save it, so
       every player gets the same rounds.
    3. If the feeds fail, fall back to the sample pool.
    """
    path = PUZZLE_DIR / f"{on.isoformat()}.json"

    if not path.exists() and generate:
        recently_failed = time.time() - _failed_at.get(on, 0) < RETRY_AFTER_SECONDS
        if not recently_failed:
            with _lock:
                if not path.exists():  # another request may have just made it
                    data = news.generate(on)
                    if data:
                        _write_atomic(path, data)
                    else:
                        _failed_at[on] = time.time()

    if path.exists():
        data = json.loads(path.read_text())
        return Puzzle(
            date=on,
            news_date=date.fromisoformat(data["news_date"]),
            rounds=[_round_from(r) for r in data["rounds"]],
        )
    return _sample_puzzle(on)


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
