"""Latestle: a daily speed game about yesterday's news."""

from __future__ import annotations

import re
import uuid
from datetime import date

from flask import Flask, g, jsonify, render_template, request

from latestle import db, game, news
from latestle.puzzles import load_puzzle, today_utc

app = Flask(__name__)

PLAYER_COOKIE = "latestle_player"
PLAYER_ID = re.compile(r"^[0-9a-f]{32}$")
TWO_YEARS = 60 * 60 * 24 * 730


class BadRequest(Exception):
    pass


@app.errorhandler(BadRequest)
@app.errorhandler(game.GameError)
def bad_request(err):
    return jsonify(error=str(err)), 400


# ---------- Players ----------

@app.before_request
def identify_player():
    """Every visitor gets an anonymous ID in a cookie. No sign-up needed; it
    just lets the server keep their progress and stats."""
    pid = request.cookies.get(PLAYER_COOKIE, "")
    g.new_player = not PLAYER_ID.match(pid)
    g.player_id = uuid.uuid4().hex if g.new_player else pid


@app.after_request
def remember_player(response):
    if g.get("new_player") and request.path.startswith("/api/"):
        response.set_cookie(
            PLAYER_COOKIE, g.player_id, max_age=TWO_YEARS,
            httponly=True, samesite="Lax", secure=request.is_secure,
        )
    return response


# ---------- Request helpers ----------

def body() -> dict:
    return request.get_json(silent=True) or {}


def requested_date() -> date:
    """The day the player is on, in their own time zone.

    The puzzle flips at the player's local midnight, so the browser sends its
    local date. Only today +/- one day (UTC) is allowed, which covers every
    time zone without letting anyone play ahead.
    """
    raw = request.args.get("date") or body().get("date")
    if not raw:
        return today_utc()
    try:
        day = date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise BadRequest("date must be YYYY-MM-DD")
    if abs((day - today_utc()).days) > 1:
        raise BadRequest("that puzzle isn't available")
    return day


def requested_category() -> str:
    """Which edition: "top" (the default) or a category like "science"."""
    slug = request.args.get("category") or body().get("category") or news.TOP
    if slug not in news.CATEGORIES:
        raise BadRequest("unknown category")
    return slug


def requested_puzzle():
    day = requested_date()
    return day, load_puzzle(day, requested_category())


def requested_round() -> int:
    try:
        return int(body().get("round"))
    except (TypeError, ValueError):
        raise BadRequest("round must be a number")


def round_action(action, *args):
    """Run one step of a round inside a database transaction."""
    day, puzzle = requested_puzzle()
    with db.engine().begin() as conn:
        return jsonify(action(conn, g.player_id, day, puzzle, requested_round(), *args))


# ---------- Routes ----------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def get_state():
    """The puzzle's info plus everything this player has done in it today."""
    day, puzzle = requested_puzzle()
    with db.engine().begin() as conn:
        state = game.day_state(conn, g.player_id, day, puzzle)
    state["categories"] = [{"slug": c.slug, "name": c.name} for c in news.CATEGORIES.values()]
    return jsonify(state)


@app.post("/api/round/start")
def post_start():
    return round_action(game.start_round)


@app.post("/api/round/hint")
def post_hint():
    return round_action(game.get_hint)


@app.post("/api/round/guess")
def post_guess():
    return round_action(game.submit_guess, str(body().get("guess", "")).strip()[:60])


@app.post("/api/round/end")
def post_end():
    """The player passed, or their countdown reached zero."""
    return round_action(game.end_round)


@app.get("/api/stats")
def get_stats():
    with db.engine().begin() as conn:
        return jsonify(game.player_stats(conn, g.player_id, requested_date(), requested_category()))


if __name__ == "__main__":
    app.run(debug=True, port=5001)
