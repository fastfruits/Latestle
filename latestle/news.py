"""Turn yesterday's headlines into a day of Latestle rounds.

Each round is one real headline with one word blacked out. To keep it
guessable, the hidden word is always a name (a person, place, country,
company...) and we favour the names that show up across the most stories,
since those are the ones most people will have heard about.
"""

from __future__ import annotations

import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape

FEEDS = [
    ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC News", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
]

ROUNDS_PER_DAY = 5
MIN_WORD_LENGTH = 4
REDACTED = "[REDACTED]"

# Capitalised words that aren't interesting answers.
STOPWORDS = {
    "the", "this", "that", "these", "those", "what", "when", "where", "which", "while", "with",
    "from", "into", "over", "after", "before", "about", "here", "there", "they", "their", "will",
    "would", "could", "should", "have", "says", "said", "how", "why", "who", "just", "more",
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "news", "live", "watch", "listen", "video", "analysis", "opinion",
    "update", "updates", "breaking", "exclusive", "report", "bbc", "npr", "guardian", "sen",
    "rep", "gov", "president", "minister", "prime", "king", "queen", "pope", "north", "south",
    "east", "west",
}


@dataclass
class Headline:
    source: str
    title: str
    summary: str
    url: str
    published: datetime | None


# ---------- Fetching ----------

def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _first_paragraph(html: str) -> str:
    """Feeds like The Guardian pack several paragraphs into the description;
    the first one is the standfirst, which is all we want."""
    for block in re.split(r"</p>|</li>|<br\s*/?>", unescape(html or ""), flags=re.I):
        text = _clean(block)
        if text:
            return text
    return ""


def fetch_feed(source: str, url: str, timeout: float = 10) -> list[Headline]:
    req = urllib.request.Request(url, headers={"User-Agent": "Latestle/0.1 (daily news game)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        root = ET.fromstring(resp.read())
    out = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        if not title:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = None
        out.append(Headline(
            source=source,
            title=title,
            summary=_first_paragraph(item.findtext("description")),
            url=(item.findtext("link") or "").strip(),
            published=published,
        ))
    return out


def fetch_all() -> list[Headline]:
    """All headlines from every feed, fetched in parallel. Dead feeds are skipped."""
    def safe(pair):
        try:
            return fetch_feed(*pair)
        except Exception as e:
            print(f"[latestle] feed failed: {pair[0]}: {e}", file=sys.stderr)
            return []

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        results = pool.map(safe, FEEDS)

    seen, unique = set(), []
    for h in (h for batch in results for h in batch):
        if h.title not in seen:  # outlets repeat stories across feeds
            seen.add(h.title)
            unique.append(h)
    return unique


def from_day(headlines: list[Headline], day: date, minimum: int = 15) -> list[Headline]:
    """Headlines published on `day` (UTC). Widens to the surrounding two days
    if that day alone is too thin to build a good puzzle."""
    def on(h, days):
        return h.published and h.published.astimezone(timezone.utc).date() in days

    picked = [h for h in headlines if on(h, {day})]
    if len(picked) < minimum:
        picked = [h for h in headlines if on(h, {day - timedelta(days=1), day, day + timedelta(days=1)})]
    return picked


# ---------- Picking answers ----------

_TOKEN = re.compile(r"\S+")
_SENTENCE_END = re.compile(r"[.:!?;]['\"’”]?$")


@dataclass
class Word:
    text: str
    starts_sentence: bool
    standalone: bool  # not glued to another capitalised word ("Cape Town")


def _is_capitalised(raw: str) -> bool:
    core = raw.strip("'\"‘’“”(")
    return bool(core) and core[0].isupper()


def _ends_phrase(raw: str) -> bool:
    """Punctuation or a possessive separates this token from the next."""
    return bool(re.search(r"[.,:;!?)'\"’”]$", raw)) or bool(re.search(r"['’]s$", raw))


def proper_nouns(text: str) -> list[Word]:
    """Capitalised words in a sentence-case headline."""
    tokens = _TOKEN.findall(text)
    found = []
    for i, raw in enumerate(tokens):
        prev = tokens[i - 1] if i else ""
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        starts_sentence = not prev or bool(_SENTENCE_END.search(prev)) or raw[0] in "'\"‘“"
        word = re.sub(r"['’]s$", "", raw.strip("'\"‘’“”.,:;!?()[]"))  # Putin's -> Putin
        if not (
            len(word) >= MIN_WORD_LENGTH
            and word.isalpha()
            and word[0].isupper()
            and word.lower() not in STOPWORDS
        ):
            continue
        glued_before = not starts_sentence and _is_capitalised(prev) and not _ends_phrase(prev)
        glued_after = bool(nxt) and _is_capitalised(nxt) and not _ends_phrase(raw)
        found.append(Word(word, starts_sentence, not (glued_before or glued_after)))
    return found


def _word_re(word: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(word)}(?![\w-])")


def redact(text: str, word: str) -> str:
    return _word_re(word).sub(REDACTED, text)


def _mentions(word: str) -> re.Pattern:
    """Matches a word and its forms: Russia, Russia's, Russian."""
    return re.compile(rf"(?<![\w-]){re.escape(word)}\w*")


def _trim(text: str, limit: int = 220) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def build_rounds(headlines: list[Headline], count: int = ROUNDS_PER_DAY) -> list[dict]:
    """Pick `count` headlines from different stories and the name to hide in each."""
    # A real name is a word that shows up capitalised mid-sentence, on its
    # own, at least once. That rules out ordinary words that start a headline
    # ("Government backs...") and fragments of longer names ("Cape Town").
    names = {
        w.text for h in headlines for w in proper_nouns(h.title)
        if w.standalone and not w.starts_sentence
    }

    # Rank names by how many outlets covered them, then by how many stories
    # mention them. Names everyone is reporting on are the easy ones.
    outlets: dict[str, set[str]] = {n: set() for n in names}
    stories = Counter()
    for h in headlines:
        text = f"{h.title} {h.summary}"
        for name in names:
            if _word_re(name).search(text):
                outlets[name].add(h.source)
                stories[name] += 1
    ranked = sorted(names, key=lambda n: (-len(outlets[n]), -stories[n], n))

    rounds, chosen = [], []
    for name in ranked:
        if len(rounds) == count:
            break
        # Skip near-duplicates of earlier answers (Russia / Russian).
        if any(name.lower().startswith(c.lower()) or c.lower().startswith(name.lower()) for c in chosen):
            continue
        for h in headlines:
            if not _word_re(name).search(h.title) or len(h.title.split()) < 6:
                continue
            # One round per story: skip anything touching an earlier answer,
            # so the day isn't five rounds about the same event.
            text = f"{h.title} {h.summary}"
            if any(_mentions(c).search(text) for c in chosen):
                continue
            summary = h.summary if h.summary and h.summary != h.title else ""
            rounds.append({
                "answer": name,
                "headline": redact(h.title, name),
                "hint": _trim(redact(summary, name)),
                "original": h.title,
                "summary": _trim(summary, 400),
                "source": h.source,
                "url": h.url,
            })
            chosen.append(name)
            break
    return rounds


def generate(puzzle_date: date) -> dict | None:
    """A puzzle for `puzzle_date` built from the previous day's news, or None
    if the feeds didn't give us enough to work with."""
    news_date = puzzle_date - timedelta(days=1)
    rounds = build_rounds(from_day(fetch_all(), news_date))
    if len(rounds) < ROUNDS_PER_DAY:
        return None
    return {
        "news_date": news_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rounds": rounds,
    }
