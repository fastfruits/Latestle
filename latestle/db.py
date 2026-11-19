"""Where player progress lives.

Four tables:
- players: one row per player ID. Anonymous visitors get one in a cookie;
  an account owns exactly one.
- users: Google accounts, each pointing at the player ID that holds its
  progress.
- round_plays: one row per round a player has started (per day and news
  category), with server-side start time, status, time taken and points.
- results: one row per player per finished puzzle (day + category), used
  for stats and for "you beat X% of players".

SQLite by default. Set DATABASE_URL to use Postgres in production, e.g.
postgresql+psycopg://user:pass@host/dbname (and `pip install psycopg[binary]`).
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
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):  # Heroku-style URLs
        url = "postgresql://" + url[len("postgres://"):]
    if not url:
        DEFAULT_PATH.parent.mkdir(exist_ok=True)
        url = f"sqlite:///{DEFAULT_PATH}"
    return url


def engine() -> Engine:
    """The shared engine, creating the tables on first use."""
    global _engine
    if _engine is None:
        use(create_engine(database_url()))
    return _engine


def use(new_engine: Engine) -> None:
    """Point the app at a database (tests use this for a throwaway one)."""
    global _engine
    _engine = new_engine
    metadata.create_all(_engine)
