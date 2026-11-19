"""Google accounts, and how they connect to player progress.

Progress always belongs to a player ID (see game.py). An account simply owns
one player ID, so everything else works the same signed in or not:

- First sign-in: the anonymous player you've been using becomes the
  account's player, so the games you already played come with you.
- Signing in on another device: that device's anonymous games are merged
  into the account. If the same puzzle was played on both, the account's
  copy wins.
- Signing out: the browser gets a brand-new anonymous ID, so a shared
  computer can't keep reaching your account.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, delete, select, update
from sqlalchemy.engine import Connection

from latestle import db
from latestle.game import ensure_player, now_ms


def new_id() -> str:
    return uuid.uuid4().hex


def get_user(conn: Connection, user_id: str) -> dict | None:
    row = conn.execute(select(db.users).where(db.users.c.id == user_id)).mappings().first()
    return dict(row) if row else None


def owner_of(conn: Connection, player_id: str) -> str | None:
    """The account a player ID belongs to, if any."""
    return conn.execute(select(db.users.c.id).where(db.users.c.player_id == player_id)).scalar()


def public(user: dict | None) -> dict | None:
    """What the page needs to show who's signed in."""
    if not user:
        return None
    return {"name": user["name"], "email": user["email"], "picture": user["picture"]}


def merge_players(conn: Connection, from_id: str, into_id: str) -> int:
    """Move `from_id`'s puzzles over to `into_id`, skipping any puzzle (day +
    category) that `into_id` already has. Returns how many puzzles moved."""
    plays, results = db.round_plays.c, db.results.c

    def puzzles(table_c, player_id):
        return set(conn.execute(
            select(table_c.puzzle_date, table_c.category).where(table_c.player_id == player_id).distinct()
        ).all())

    theirs = puzzles(plays, from_id) | puzzles(results, from_id)
    taken = puzzles(plays, into_id) | puzzles(results, into_id)
    moving = theirs - taken

    for day, category in moving:
        for table, c in ((db.round_plays, plays), (db.results, results)):
            conn.execute(
                update(table)
                .where(and_(c.player_id == from_id, c.puzzle_date == day, c.category == category))
                .values(player_id=into_id)
            )
    # Whatever's left clashed with the account's own games; drop it.
    conn.execute(delete(db.round_plays).where(plays.player_id == from_id))
    conn.execute(delete(db.results).where(results.player_id == from_id))
    conn.execute(delete(db.players).where(db.players.c.id == from_id))
    return len(moving)


def sign_in(conn: Connection, current_player_id: str, info: dict) -> dict:
    """Find or create the account for a Google sign-in and connect this
    browser's anonymous progress to it. `info` is Google's userinfo."""
    u = db.users.c
    profile = {"email": info.get("email"), "name": info.get("name"), "picture": info.get("picture")}
    user = conn.execute(select(db.users).where(u.google_sub == info["sub"])).mappings().first()

    # A player ID that already belongs to some account can't be adopted or
    # merged (e.g. a stale cookie on a shared computer).
    anonymous = current_player_id if current_player_id and not owner_of(conn, current_player_id) else None

    if user is None:
        player_id = anonymous or new_id()
        ensure_player(conn, player_id)
        user_id = new_id()
        conn.execute(db.users.insert().values(
            id=user_id, google_sub=info["sub"], player_id=player_id,
            created_at=now_ms(), last_login_at=now_ms(), **profile,
        ))
        return get_user(conn, user_id)

    if anonymous and anonymous != user["player_id"]:
        merge_players(conn, anonymous, user["player_id"])
    conn.execute(update(db.users).where(u.id == user["id"]).values(last_login_at=now_ms(), **profile))
    return get_user(conn, user["id"])
