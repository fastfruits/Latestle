"""Where player progress lives.

Five tables:
- puzzles: each day's puzzle per news category, as JSON. Only the last few
  days are kept (enough for every time zone); older ones are deleted.
- players: one row per player ID. Anonymous visitors get one in a cookie;
  an account owns exactly one.
- users: Google accounts, each pointing at the player ID that holds its
  progress.
- round_plays: one row per round a player has started (per day and news
  category), with server-side start time, status, time taken and points.
- results: one row per player per finished puzzle (day + category), used
  for stats and for "you beat X% of players".

SQLite by default. Set DATABASE_URL to use Postgres in production. Plain
postgres:// and postgresql:// URLs (what Neon, Supabase and Vercel hand out)
are switched to the psycopg 3 driver.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.engine import Engine

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "instance" / "latestle.db"

metadata = MetaData()

puzzles = Table(
    "puzzles",
    metadata,
    Column("puzzle_date", String(10), primary_key=True),  # YYYY-MM-DD
    Column("category", String(20), primary_key=True),
    Column("data", Text, nullable=False),  # JSON: news_date and rounds
    Column("created_at", BigInteger, nullable=False),  # epoch ms
)

players = Table(
    "players",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("created_at", BigInteger, nullable=False),  # epoch ms
)

users = Table(
    "users",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("google_sub", String(255), nullable=False, unique=True),  # Google's stable account ID
    Column("email", String(320)),
    Column("name", String(255)),
    Column("picture", Text),
    Column("player_id", String(32), ForeignKey("players.id"), nullable=False, unique=True),
    Column("created_at", BigInteger, nullable=False),
    Column("last_login_at", BigInteger, nullable=False),
)

round_plays = Table(
    "round_plays",
    metadata,
    Column("player_id", String(32), ForeignKey("players.id"), primary_key=True),
    Column("puzzle_date", String(10), primary_key=True),  # player's local date, YYYY-MM-DD
    Column("category", String(20), primary_key=True),  # "top", "science", ...
    Column("round", Integer, primary_key=True),
    Column("started_at", BigInteger, nullable=False),  # epoch ms, server clock
    Column("status", String(10), nullable=False),  # playing | correct | missed
    Column("time_ms", Integer),
    Column("points", Integer, nullable=False, default=0),
    Column("guesses", Text, nullable=False, default="[]"),  # JSON list of wrong guesses
)

results = Table(
    "results",
    metadata,
    Column("player_id", String(32), ForeignKey("players.id"), primary_key=True),
    Column("puzzle_date", String(10), primary_key=True),
    Column("category", String(20), primary_key=True),
    Column("score", Integer, nullable=False),
    Column("correct", Integer, nullable=False),
    Column("finished_at", BigInteger, nullable=False),
    Index("results_by_day", "puzzle_date", "category", "score"),
)

_engine: Engine | None = None


def database_url() -> str:
    # Vercel's Postgres integrations may set POSTGRES_URL instead.
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    if not url:
        if os.environ.get("VERCEL"):
            raise RuntimeError("Set DATABASE_URL: Vercel's disk is read-only, so SQLite won't work there.")
        DEFAULT_PATH.parent.mkdir(exist_ok=True)
        url = f"sqlite:///{DEFAULT_PATH}"
    return url


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(url)
    return create_engine(
        url,
        pool_size=2,
        max_overflow=3,
        # Hosted Postgres (Neon) drops idle connections when it sleeps.
        pool_pre_ping=True,
        # Works with connection poolers like Neon's "-pooler" address.
        connect_args={"prepare_threshold": None},
    )


def engine() -> Engine:
    """The shared engine, creating the tables on first use."""
    global _engine
    if _engine is None:
        use(make_engine(database_url()))
    return _engine


def use(new_engine: Engine) -> None:
    """Point the app at a database (tests use this for a throwaway one)."""
    global _engine
    _engine = new_engine
    metadata.create_all(_engine)
