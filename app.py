"""Latestle: a daily speed game about yesterday's news."""

from __future__ import annotations

import hmac
import os
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from authlib.integrations.flask_client import OAuth, OAuthError
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from latestle import accounts, db, game, news
from latestle.puzzles import load_puzzle, prune_puzzles, today_utc

load_dotenv()  # settings from a local .env file, if there is one

# Vercel sets this on its servers: always https, behind its proxy, read-only disk.
ON_VERCEL = bool(os.environ.get("VERCEL"))

# Static files live in public/static: Vercel serves public/ from its CDN (it
# ignores Flask's own static folder), and Flask serves the same files locally.
app = Flask(__name__, static_folder="public/static", static_url_path="/static")

PLAYER_COOKIE = "latestle_player"
PLAYER_ID = re.compile(r"^[0-9a-f]{32}$")
TWO_YEARS = 60 * 60 * 24 * 730
GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"


# ---------- Config ----------

def _secret_key() -> str:
    """SECRET_KEY signs the session cookie that says who's signed in. In
    development, make one and keep it in instance/ so restarts don't sign
    everyone out."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    if ON_VERCEL:
        raise RuntimeError("Set SECRET_KEY in the Vercel project's environment variables.")
    path = Path(app.instance_path) / "secret_key"
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_NAME="latestle_session",
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ON_VERCEL or os.environ.get("SECURE_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID"),
    GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET"),
)

if ON_VERCEL or os.environ.get("BEHIND_PROXY") == "1":
    # Hosts like Vercel and Render sit behind a proxy; trust its headers so
    # the Google redirect URL comes out as https://your-domain/...
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

oauth = OAuth(app)
oauth.register("google", server_metadata_url=GOOGLE_DISCOVERY, client_kwargs={"scope": "openid email profile"})


def google_enabled() -> bool:
    return bool(app.config.get("GOOGLE_CLIENT_ID") and app.config.get("GOOGLE_CLIENT_SECRET"))


class BadRequest(Exception):
    pass


@app.errorhandler(BadRequest)
@app.errorhandler(game.GameError)
def bad_request(err):
    return jsonify(error=str(err)), 400


# ---------- Players and accounts ----------

@app.before_request
def identify_player():
    """Work out whose progress this request is about.

    Signed in: the account's player. Otherwise an anonymous player ID from a
    cookie, minted on the first visit. A cookie ID that belongs to an account
    only works while signed in to that account.
    """
    g.user, g.new_player = None, False
    if not request.path.startswith(("/api/", "/auth/")) or request.path.startswith("/api/cron/"):
        return
    with db.engine().begin() as conn:
        user_id = session.get("user_id")
        if user_id:
            g.user = accounts.get_user(conn, user_id)
            if g.user:
                g.player_id = g.user["player_id"]
                return
            session.pop("user_id", None)  # account no longer exists

        pid = request.cookies.get(PLAYER_COOKIE, "")
        if PLAYER_ID.match(pid) and not accounts.owner_of(conn, pid):
            g.player_id = pid
        else:
            fresh_anonymous_player()


def fresh_anonymous_player() -> None:
    g.player_id, g.new_player = accounts.new_id(), True


@app.after_request
def remember_player(response):
    if g.get("new_player"):
        response.set_cookie(
            PLAYER_COOKIE, g.player_id, max_age=TWO_YEARS,
            httponly=True, samesite="Lax", secure=request.is_secure,
        )
    return response


def safe_next(url: str | None) -> str:
    """Only redirect back to pages on this site."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return "/"


@app.get("/auth/google")
def google_login():
    if not google_enabled():
        abort(404)
    session["next"] = safe_next(request.args.get("next"))
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri, prompt="select_account")


@app.get("/auth/google/callback")
def google_callback():
    if not google_enabled():
        abort(404)
    next_url = safe_next(session.pop("next", None))
    try:
        token = oauth.google.authorize_access_token()
    except OAuthError:
        return redirect(next_url)  # they cancelled, or Google said no
    info = token.get("userinfo") or oauth.google.userinfo(token=token)

    with db.engine().begin() as conn:
        user = accounts.sign_in(conn, g.player_id, info)

    session.clear()  # new session on sign-in
    session["user_id"] = user["id"]
    session.permanent = True
    # The old anonymous ID now belongs to the account; give the browser a
    # fresh one for whenever they sign out.
    fresh_anonymous_player()
    return redirect(next_url)


@app.post("/auth/logout")
def logout():
    session.clear()
    fresh_anonymous_player()
    return jsonify(ok=True)


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
    state["account"] = {"google_enabled": google_enabled(), "user": accounts.public(g.user)}
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


@app.route("/api/cron/daily", methods=["GET", "POST"])
def daily_cron():
    """Vercel calls this once a day, just after midnight UTC (see vercel.json):
    build the day's puzzles before the first player arrives, and delete ones
    too old to play. Vercel sends CRON_SECRET as a Bearer token."""
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        abort(404)
    sent = request.headers.get("Authorization", "").encode()
    if not hmac.compare_digest(sent, f"Bearer {secret}".encode()):
        abort(401)

    day = today_utc()

    def build(category):
        return category, not load_puzzle(day, category).sample

    with ThreadPoolExecutor(max_workers=4) as pool:
        built = dict(pool.map(build, news.CATEGORIES))
    return jsonify(date=day.isoformat(), built=built, removed=prune_puzzles(day))


if __name__ == "__main__":
    app.run(debug=True, port=5001)
