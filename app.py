"""Latestle: a daily speed game about yesterday's news."""

from __future__ import annotations

from datetime import date

from flask import Flask, jsonify, render_template, request

from latestle.puzzles import check_guess, load_puzzle, today_utc

app = Flask(__name__)


class BadRequest(Exception):
    pass


@app.errorhandler(BadRequest)
def bad_request(err):
    return jsonify(error=str(err)), 400


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
    except ValueError:
        raise BadRequest("date must be YYYY-MM-DD")
    if abs((day - today_utc()).days) > 1:
        raise BadRequest("that puzzle isn't available")
    return day


def requested_round():
    puzzle = load_puzzle(requested_date())
    try:
        return puzzle.round(int(body().get("round", -1)))
    except (TypeError, ValueError, IndexError):
        raise BadRequest("no such round")


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/puzzle")
def get_puzzle():
    return jsonify(load_puzzle(requested_date()).public())


@app.post("/api/guess")
def post_guess():
    rnd = requested_round()
    result = check_guess(rnd.answer, str(body().get("guess", ""))[:100])
    payload = {"correct": result.correct, "close": result.close}
    if result.correct:
        payload["reveal"] = rnd.reveal()
    return jsonify(payload)


@app.post("/api/reveal")
def post_reveal():
    """Called when a round's timer runs out (or the player passes)."""
    return jsonify(reveal=requested_round().reveal())


if __name__ == "__main__":
    app.run(debug=True, port=5001)
