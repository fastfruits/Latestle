"""A player's day of Latestle, timed and scored on the server.

The browser shows a countdown, but the server decides how long a round took:
it records when each round starts and measures guesses against that. A
round's headline is only sent once the round starts, so nobody can read
ahead with the clock stopped.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

from sqlalchemy import and_, case, func, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from latestle import db
from latestle.puzzles import LAUNCH_DATE, ROUND_SECONDS, Puzzle, check_guess

ROUND_MS = ROUND_SECONDS * 1000
HINT_AT_MS = 10_000
# Extra time allowed for the network: a guess sent at 19.9s that arrives at
# 20.4s still counts (scored as if it took the full 20s).
GRACE_MS = 1_500
MAX_GUESSES_STORED = 30
DONE = ("correct", "missed")


class GameError(Exception):
    """Something the player can't do right now (400 to the client)."""


def now_ms() -> int:
    return int(time.time() * 1000)


def points_for(time_ms: int) -> int:
    """100 for getting it, plus up to 200 for speed."""
    left = max(0.0, 1 - time_ms / ROUND_MS)
    return 100 + round(200 * left)


# ---------- Loading a player's day ----------

def ensure_player(conn: Connection, player_id: str) -> None:
    exists = conn.execute(select(db.players.c.id).where(db.players.c.id == player_id)).first()
    if not exists:
        try:
            with conn.begin_nested():
                conn.execute(insert(db.players).values(id=player_id, created_at=now_ms()))
        except IntegrityError:
            pass  # created by a request running at the same time


def _plays(conn: Connection, player_id: str, day: date, puzzle: Puzzle) -> dict[int, dict]:
    """This player's rounds for the day's puzzle, keyed by round number. Rounds
    whose time ran out while the player was away are closed off as missed."""
    rows = conn.execute(
        select(db.round_plays).where(and_(
            db.round_plays.c.player_id == player_id,
            db.round_plays.c.puzzle_date == day.isoformat(),
            db.round_plays.c.category == puzzle.category,
        ))
    ).mappings().all()
    plays = {r["round"]: dict(r) for r in rows}
    for play in plays.values():
        if play["status"] == "playing" and now_ms() - play["started_at"] > ROUND_MS + GRACE_MS:
            _finish_round(conn, play, "missed", ROUND_MS)
    return plays


def _same_round(play: dict):
    c = db.round_plays.c
    return and_(
        c.player_id == play["player_id"],
        c.puzzle_date == play["puzzle_date"],
        c.category == play["category"],
        c.round == play["round"],
    )


def _finish_round(conn: Connection, play: dict, status: str, time_ms: int) -> None:
    play.update(status=status, time_ms=time_ms, points=points_for(time_ms) if status == "correct" else 0)
    conn.execute(
        update(db.round_plays)
        .where(_same_round(play))
        .values(status=play["status"], time_ms=play["time_ms"], points=play["points"], guesses=play["guesses"])
    )


def _record_result_if_done(conn: Connection, player_id: str, day: date, puzzle: Puzzle, plays: dict) -> None:
    if len(plays) < len(puzzle.rounds) or any(p["status"] not in DONE for p in plays.values()):
        return
    try:
        with conn.begin_nested():
            conn.execute(insert(db.results).values(
                player_id=player_id,
                puzzle_date=day.isoformat(),
                category=puzzle.category,
                score=sum(p["points"] for p in plays.values()),
                correct=sum(p["status"] == "correct" for p in plays.values()),
                finished_at=now_ms(),
            ))
    except IntegrityError:
        pass  # already recorded


# ---------- What the browser sees ----------

def round_view(puzzle: Puzzle, index: int, play: dict | None) -> dict:
    if play is None:
        return {"index": index, "status": "pending"}
    rnd = puzzle.rounds[index]
    view = {
        "index": index,
        "status": play["status"],
        "guesses": json.loads(play["guesses"]),
        **rnd.public(),
    }
    if play["status"] == "playing":
        elapsed = now_ms() - play["started_at"]
        view["elapsed_ms"] = elapsed
        view["hint"] = rnd.hint if elapsed >= HINT_AT_MS else None
    else:
        view.update(time_ms=play["time_ms"], points=play["points"], hint=rnd.hint, reveal=rnd.reveal())
    return view


def day_state(conn: Connection, player_id: str, day: date, puzzle: Puzzle) -> dict:
    plays = _plays(conn, player_id, day, puzzle)
    _record_result_if_done(conn, player_id, day, puzzle, plays)
    state = {
        "puzzle": puzzle.meta(),
        "rounds": [round_view(puzzle, i, plays.get(i)) for i in range(len(puzzle.rounds))],
    }
    finished = all(r["status"] in DONE for r in state["rounds"])
    if finished:
        state["result"] = day_result(conn, player_id, day, puzzle.category)
    return state


# ---------- Playing ----------

def _get_play(conn, player_id, day, puzzle, index) -> tuple[dict, dict]:
    if not 0 <= index < len(puzzle.rounds):
        raise GameError("no such round")
    plays = _plays(conn, player_id, day, puzzle)
    if index not in plays:
        raise GameError("that round hasn't started")
    return plays, plays[index]


def start_round(conn: Connection, player_id: str, day: date, puzzle: Puzzle, index: int) -> dict:
    """Start the clock on a round. Starting one that's already started just
    returns it as it stands: reloading never resets the timer."""
    if not 0 <= index < len(puzzle.rounds):
        raise GameError("no such round")
    ensure_player(conn, player_id)
    plays = _plays(conn, player_id, day, puzzle)
    if index not in plays:
        if any(plays.get(i, {}).get("status") not in DONE for i in range(index)):
            raise GameError("finish the earlier headlines first")
        play = {
            "player_id": player_id, "puzzle_date": day.isoformat(),
            "category": puzzle.category, "round": index,
            "started_at": now_ms(), "status": "playing", "time_ms": None, "points": 0, "guesses": "[]",
        }
        try:
            with conn.begin_nested():
                conn.execute(insert(db.round_plays).values(**play))
        except IntegrityError:
            plays = _plays(conn, player_id, day, puzzle)  # started by a double-click
        else:
            plays[index] = play
    return round_view(puzzle, index, plays[index])


def get_hint(conn: Connection, player_id: str, day: date, puzzle: Puzzle, index: int) -> dict:
    _, play = _get_play(conn, player_id, day, puzzle, index)
    view = round_view(puzzle, index, play)
    # A little slack for clocks drifting between browser and server.
    if play["status"] == "playing" and view["elapsed_ms"] >= HINT_AT_MS - 300:
        view["hint"] = puzzle.rounds[index].hint
    return view


def submit_guess(conn: Connection, player_id: str, day: date, puzzle: Puzzle, index: int, guess: str) -> dict:
    plays, play = _get_play(conn, player_id, day, puzzle, index)
    if play["status"] != "playing":
        # Time ran out (or the round was already answered).
        _record_result_if_done(conn, player_id, day, puzzle, plays)
        return {"correct": False, "close": False, "round": round_view(puzzle, index, play)}

    elapsed = now_ms() - play["started_at"]
    result = check_guess(puzzle.rounds[index].answer, guess)
    if result.correct:
        _finish_round(conn, play, "correct", min(elapsed, ROUND_MS))
        _record_result_if_done(conn, player_id, day, puzzle, plays)
    else:
        guesses = json.loads(play["guesses"])
        if guess and guess not in guesses and len(guesses) < MAX_GUESSES_STORED:
            guesses.append(guess)
            play["guesses"] = json.dumps(guesses)
            conn.execute(
                update(db.round_plays).where(_same_round(play)).values(guesses=play["guesses"])
            )
    return {"correct": result.correct, "close": result.close, "round": round_view(puzzle, index, play)}


def end_round(conn: Connection, player_id: str, day: date, puzzle: Puzzle, index: int) -> dict:
    """The player passed, or their countdown hit zero."""
    plays, play = _get_play(conn, player_id, day, puzzle, index)
    if play["status"] == "playing":
        _finish_round(conn, play, "missed", min(now_ms() - play["started_at"], ROUND_MS))
    _record_result_if_done(conn, player_id, day, puzzle, plays)
    return round_view(puzzle, index, play)


# ---------- Results and stats ----------

def day_result(conn: Connection, player_id: str, day: date, category: str) -> dict | None:
    """This player's score on a puzzle, compared with everyone who played it."""
    r = db.results.c
    same_puzzle = and_(r.puzzle_date == day.isoformat(), r.category == category)
    mine = conn.execute(select(r.score).where(and_(r.player_id == player_id, same_puzzle))).scalar()
    if mine is None:
        return None
    players, average, lower = conn.execute(
        select(
            func.count(),
            func.avg(r.score),
            func.sum(case((r.score < mine, 1), else_=0)),
        ).where(same_puzzle)
    ).one()
    others = players - 1
    return {
        "score": mine,
        "players": players,
        "average": round(average or 0),
        # Share of *other* players you beat; None when you're the only one so far.
        "beat_pct": round(100 * (lower or 0) / others) if others else None,
    }


def player_stats(conn: Connection, player_id: str, today: date, category: str) -> dict:
    """Stats for one category. Scores aren't comparable across categories
    (Science is harder than Top stories), so each keeps its own."""
    r = db.results.c
    rows = conn.execute(
        select(r.puzzle_date, r.score)
        .where(and_(r.player_id == player_id, r.category == category))
        .order_by(r.puzzle_date)
    ).all()
    days = [date.fromisoformat(d) for d, _ in rows]
    scores = [s for _, s in rows]

    # Longest run of consecutive days, and the run that's still alive
    # (ending today, or yesterday if today isn't played yet).
    best_streak = run = 0
    for i, d in enumerate(days):
        run = run + 1 if i and d - days[i - 1] == timedelta(days=1) else 1
        best_streak = max(best_streak, run)
    current = run if days and (today - days[-1]).days <= 1 else 0

    return {
        "played": len(scores),
        "best": max(scores, default=0),
        "average": round(sum(scores) / len(scores)) if scores else 0,
        "streak": current,
        "best_streak": best_streak,
        "recent": [
            {"date": d.isoformat(), "number": (d - LAUNCH_DATE).days + 1, "score": s}
            for d, s in list(zip(days, scores))[-7:]
        ],
    }
