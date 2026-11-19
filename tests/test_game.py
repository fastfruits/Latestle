import json
from datetime import date, datetime, timedelta, timezone

import pytest

from sqlalchemy import create_engine

import app as app_module
from latestle import db, game, news, puzzles
from latestle.news import Headline, build_rounds, proper_nouns, redact
from latestle.puzzles import check_guess, load_puzzle, normalize, today_utc

YESTERDAY = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def h(title, summary="", source="BBC News"):
    return Headline(source=source, title=title, summary=summary, url="https://example.com/" + title[:10],
                    published=YESTERDAY)


HEADLINES = [
    h("Putin's Crimean Bridge partly collapses as Zelensky hails attack by Ukraine",
      "Ukraine says the attack on the bridge in Russian-annexed Crimea took weeks to plan."),
    h("Mother describes sheltering during Russia strikes on Kyiv bridge", source="NPR"),
    h("Jensen Huang says he would be honoured to return as Nvidia CEO",
      "Huang says he has had an outpouring of calls from investors.", source="The Guardian"),
    h("Huang agrees to testify to Congress about his exit from Nvidia", source="NPR"),
    h("Five EU states announce plans for return hubs in Rwanda", source="The Guardian"),
    h("Government backs welfare shake-up to prevent lost generation"),
    h("Relocation of Cape Town baboons to sanctuary ignites heated protests", source="The Guardian"),
    h("Trump says he plans to discuss Ukraine during his Asia trip", source="NPR"),
    h("Trump arrives in Tokyo ahead of summit with Japanese leaders"),
    h("Iran launches a third round of strikes on Bahrain, host of US Navy fleet", source="NPR"),
    h("Wildfires force thousands to evacuate across southern Australia overnight"),
]


@pytest.fixture
def fake_news(tmp_path, monkeypatch):
    """No network in tests: feeds return HEADLINES, puzzles go to a temp dir."""
    monkeypatch.setattr(news, "fetch_all", lambda feeds=None: list(HEADLINES))
    monkeypatch.setattr(news, "from_day", lambda hs, day: hs)
    monkeypatch.setattr(puzzles, "PUZZLE_DIR", tmp_path)
    puzzles._failed_at.clear()
    return tmp_path


class Clock:
    """Stands in for the server clock so tests can skip ahead in time."""

    def __init__(self):
        self.ms = 1_800_000_000_000

    def __call__(self):
        return self.ms

    def advance(self, seconds):
        self.ms += int(seconds * 1000)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(game, "now_ms", c)
    return c


@pytest.fixture
def fresh_db():
    db.use(create_engine("sqlite://"))


@pytest.fixture
def new_player(fake_news, fresh_db, clock):
    """Each call returns a browser with its own anonymous player cookie."""
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client


@pytest.fixture
def client(new_player):
    return new_player()


# ---------- picking answers ----------

def test_proper_nouns():
    words = {w.text: w for w in proper_nouns(
        "Putin's bridge hit as Zelensky hails attack. Ukraine says Cape Town and Jensen Huang agree")}
    assert words["Putin"].starts_sentence
    assert not words["Zelensky"].starts_sentence and words["Zelensky"].standalone
    assert words["Ukraine"].starts_sentence  # after a full stop
    assert not words["Cape"].standalone and not words["Huang"].standalone


def test_redact_whole_words_only():
    assert redact("Iran and Iranian forces; Iran's navy", "Iran") == "[REDACTED] and Iranian forces; [REDACTED]'s navy"


def test_build_rounds_prefers_widely_covered_names_and_distinct_stories():
    rounds = build_rounds(HEADLINES)
    answers = [r["answer"] for r in rounds]
    assert len(rounds) == 5
    # The two names covered by two outlets each come first.
    assert set(answers[:2]) == {"Nvidia", "Ukraine"}
    # Fragments of multi-word names never become answers.
    assert "Cape" not in answers and "Town" not in answers
    # One round per story: no headline is reused, none mentions an earlier answer.
    titles = [r["original"] for r in rounds]
    assert len(set(titles)) == len(titles)
    for i, r in enumerate(rounds):
        for earlier in answers[:i]:
            assert earlier not in r["original"]
    for r in rounds:
        assert "[REDACTED]" in r["headline"]
        assert r["answer"] not in r["headline"]
        assert r["answer"] not in r["hint"]


def test_sentence_start_only_words_are_not_answers():
    answers = {r["answer"] for r in build_rounds(HEADLINES)}
    assert "Government" not in answers and "Relocation" not in answers


def test_title_case_headlines_dont_create_names():
    assert news.is_title_case("It Is Time For Timor-Leste to Acknowledge the Victims")
    assert not news.is_title_case("Jensen Huang says he would be honoured to return as Nvidia CEO")
    title_case = h("It Is Time For Leaders to Acknowledge the Victims of the Massacre", source="The Diplomat")
    assert "Acknowledge" not in {r["answer"] for r in build_rounds(HEADLINES + [title_case])}


def test_category_puzzles_are_saved_separately(fake_news):
    load_puzzle(date(2026, 11, 17))
    p = load_puzzle(date(2026, 11, 17), "science")
    assert p.category == "science" and p.meta()["category_name"] == "Science"
    assert (fake_news / "2026-11-17.json").exists()
    assert (fake_news / "2026-11-17-science.json").exists()
    with pytest.raises(KeyError):
        load_puzzle(date(2026, 11, 17), "nope")


# ---------- matching ----------

def test_normalize():
    assert normalize("  The Musée's! ") == "musee"


@pytest.mark.parametrize("guess", ["Nvidia", "nvidia", "NVIDIA", "Nvidia's", "Nvidea"])
def test_correct_guesses(guess):
    assert check_guess("Nvidia", guess).correct


@pytest.mark.parametrize("guess", ["Intel", "", "  "])
def test_wrong_guesses(guess):
    assert not check_guess("Nvidia", guess).correct


def test_typos():
    assert check_guess("Nvidia", "Nvdiia").correct  # swapped letters
    assert not check_guess("Nvidia", "Nvdiae").correct
    assert check_guess("Zelensky", "Zelenskyy").correct
    assert check_guess("Nvidia", "Nvidiaaa").close


def test_short_answers_need_exact_match():
    assert not check_guess("Iran", "Iraq").correct
    assert check_guess("Iran", "iran").correct


# ---------- loading ----------

def test_generates_once_and_saves(fake_news, monkeypatch):
    p = load_puzzle(date(2026, 11, 17))
    assert not p.sample and len(p.rounds) == 5 and p.number == 1
    assert (fake_news / "2026-11-17.json").exists()

    # Second load reads the file instead of fetching again.
    monkeypatch.setattr(news, "fetch_all", lambda *a: pytest.fail("should not refetch"))
    assert [r.answer for r in load_puzzle(date(2026, 11, 17)).rounds] == [r.answer for r in p.rounds]


def test_falls_back_to_samples_when_feeds_fail(fake_news, monkeypatch):
    monkeypatch.setattr(news, "fetch_all", lambda *a: [])
    p = load_puzzle(date(2026, 11, 18))
    assert p.sample and len(p.rounds) == 5


def test_samples_are_valid():
    samples = json.loads(puzzles.SAMPLES_FILE.read_text())
    assert len(samples) >= 5
    for s in samples:
        assert s["headline"].count("[REDACTED]") >= 1
        assert s["headline"].replace("[REDACTED]", s["answer"]) == s["original"]
        assert s["answer"].lower() not in s["hint"].lower()
        assert s["url"].startswith("https://")


# ---------- API ----------

def today():
    return today_utc().isoformat()


def answers():
    return [r.answer for r in load_puzzle(today_utc()).rounds]


def call(client, action, round_, **extra):
    res = client.post(f"/api/round/{action}", json={"date": today(), "round": round_, **extra})
    return res.status_code, res.get_json()


def play_day(client, clock, seconds_each, correct=True):
    for i, answer in enumerate(answers()):
        call(client, "start", i)
        clock.advance(seconds_each)
        if correct:
            call(client, "guess", i, guess=answer)
        else:
            call(client, "end", i)


def test_new_visitors_get_an_anonymous_cookie(client):
    res = client.get(f"/api/state?date={today()}")
    cookie = res.headers["Set-Cookie"]
    assert cookie.startswith("latestle_player=") and "HttpOnly" in cookie
    # Already has one: not set again.
    assert "Set-Cookie" not in client.get(f"/api/state?date={today()}").headers


def test_state_has_no_headlines_before_rounds_start(client):
    state = client.get(f"/api/state?date={today()}").get_json()
    assert state["puzzle"]["total_rounds"] == 5 and state["puzzle"]["round_seconds"] == 20
    assert all(r == {"index": i, "status": "pending"} for i, r in enumerate(state["rounds"]))
    assert "result" not in state


def test_start_reveals_headline_but_not_answer_or_hint(client):
    _, view = call(client, "start", 0)
    assert view["status"] == "playing" and "[REDACTED]" in view["headline"]
    assert view["hint"] is None and "reveal" not in view
    assert answers()[0] not in json.dumps(view)


def test_rounds_must_be_played_in_order(client):
    status, body = call(client, "start", 1)
    assert status == 400 and "earlier" in body["error"]


def test_restarting_a_round_does_not_reset_the_clock(client, clock):
    call(client, "start", 0)
    clock.advance(8)
    _, view = call(client, "start", 0)
    assert view["elapsed_ms"] == 8000


def test_hint_only_after_ten_seconds(client, clock):
    call(client, "start", 0)
    clock.advance(5)
    assert call(client, "hint", 0)[1]["hint"] is None
    clock.advance(5)
    assert call(client, "hint", 0)[1]["hint"]


def test_server_times_and_scores_correct_guess(client, clock):
    call(client, "start", 0)
    clock.advance(5)
    _, wrong = call(client, "guess", 0, guess="zzzz")
    assert not wrong["correct"] and wrong["round"]["guesses"] == ["zzzz"]
    clock.advance(1)
    _, right = call(client, "guess", 0, guess=answers()[0].lower())
    assert right["correct"]
    assert right["round"]["time_ms"] == 6000
    assert right["round"]["points"] == game.points_for(6000) == 240
    assert right["round"]["reveal"]["answer"] == answers()[0]


def test_late_guess_does_not_count(client, clock):
    call(client, "start", 0)
    clock.advance(25)
    _, res = call(client, "guess", 0, guess=answers()[0])
    assert not res["correct"] and res["round"]["status"] == "missed" and res["round"]["time_ms"] == 20000


def test_guess_just_after_buzzer_counts_for_minimum_points(client, clock):
    call(client, "start", 0)
    clock.advance(20.5)  # within the network grace period
    _, res = call(client, "guess", 0, guess=answers()[0])
    assert res["correct"] and res["round"]["points"] == 100


def test_passing_reveals_answer(client, clock):
    call(client, "start", 0)
    clock.advance(3)
    _, view = call(client, "end", 0)
    assert view["status"] == "missed" and view["time_ms"] == 3000 and view["reveal"]["url"]


def test_finished_day_compares_players_and_tracks_stats(new_player, clock):
    alice, bob = new_player(), new_player()
    play_day(alice, clock, seconds_each=2)
    a = alice.get(f"/api/state?date={today()}").get_json()["result"]
    assert a["players"] == 1 and a["beat_pct"] is None and a["score"] == 5 * game.points_for(2000)

    play_day(bob, clock, seconds_each=1, correct=False)
    b = bob.get(f"/api/state?date={today()}").get_json()["result"]
    assert b == {"score": 0, "players": 2, "average": a["score"] // 2, "beat_pct": 0}
    assert alice.get(f"/api/state?date={today()}").get_json()["result"]["beat_pct"] == 100

    stats = alice.get(f"/api/stats?date={today()}").get_json()
    assert stats["played"] == 1 and stats["best"] == a["score"] and stats["streak"] == 1
    assert stats["recent"][0]["score"] == a["score"]


def test_streaks(fresh_db, clock):
    with db.engine().begin() as conn:
        game.ensure_player(conn, "p")
        for d, score in [("2026-11-10", 100), ("2026-11-12", 200), ("2026-11-13", 300), ("2026-11-14", 400)]:
            conn.execute(db.results.insert().values(
                player_id="p", puzzle_date=d, category="top", score=score, correct=1, finished_at=0))
        conn.execute(db.results.insert().values(
            player_id="p", puzzle_date="2026-11-15", category="science", score=999, correct=5, finished_at=0))
        s = game.player_stats(conn, "p", date(2026, 11, 15), "top")
        assert (s["streak"], s["best_streak"], s["played"], s["average"]) == (3, 3, 4, 250)
        assert game.player_stats(conn, "p", date(2026, 11, 16), "top")["streak"] == 0
        assert game.player_stats(conn, "p", date(2026, 11, 15), "science")["best"] == 999


def test_bad_requests(client):
    future = (today_utc() + timedelta(days=3)).isoformat()
    assert client.get(f"/api/state?date={future}").status_code == 400
    assert call(client, "start", 9)[0] == 400
    assert call(client, "guess", 0, guess="x")[0] == 400  # not started
    assert client.post("/api/round/end", json={"date": today(), "round": "x"}).status_code == 400


def test_each_category_is_its_own_game(client, clock):
    call(client, "start", 0)
    clock.advance(2)
    call(client, "end", 0)
    sci = client.get(f"/api/state?date={today()}&category=science").get_json()
    assert sci["puzzle"]["category"] == "science"
    assert all(r["status"] == "pending" for r in sci["rounds"])  # top's progress doesn't leak in
    _, view = call(client, "start", 0, category="science")
    assert view["status"] == "playing"
    top = client.get(f"/api/state?date={today()}").get_json()
    assert top["rounds"][0]["status"] == "missed"
    assert {"slug": "science", "name": "Science"} in top["categories"]
    assert client.get(f"/api/state?date={today()}&category=nope").status_code == 400


# ---------- Google accounts ----------

from latestle import accounts  # noqa: E402


@pytest.fixture
def google(monkeypatch):
    """Pretend Google sign-in is configured, and answer the callback with
    whichever Google account the test picks."""
    app_module.app.config.update(GOOGLE_CLIENT_ID="test-id", GOOGLE_CLIENT_SECRET="test-secret")
    who = {}
    monkeypatch.setattr(app_module.oauth.google, "authorize_access_token", lambda **kw: {"userinfo": dict(who)})
    yield who
    app_module.app.config.update(GOOGLE_CLIENT_ID=None, GOOGLE_CLIENT_SECRET=None)


def sign_in(client, google, sub="google-123", name="Ada Lovelace"):
    google.update(sub=sub, name=name, email=f"{sub}@example.com", picture=None)
    with client.session_transaction() as s:
        s["next"] = "/?category=science"
    return client.get("/auth/google/callback")


def state(client, category="top"):
    return client.get(f"/api/state?date={today()}&category={category}").get_json()


def test_sign_in_disabled_without_google_settings(client):
    assert state(client)["account"] == {"google_enabled": False, "user": None}
    assert client.get("/auth/google").status_code == 404


def test_first_sign_in_keeps_anonymous_progress(client, clock, google):
    call(client, "start", 0)
    call(client, "end", 0)
    res = sign_in(client, google)
    assert res.status_code == 302 and res.headers["Location"] == "/?category=science"
    assert "latestle_player=" in res.headers.get("Set-Cookie", "")  # fresh ID for after sign-out

    s = state(client)
    assert s["account"]["user"]["name"] == "Ada Lovelace"
    assert s["rounds"][0]["status"] == "missed"  # came with them


def test_sign_out_starts_a_fresh_anonymous_player(client, clock, google):
    call(client, "start", 0)
    sign_in(client, google)
    assert client.post("/auth/logout").get_json() == {"ok": True}
    s = state(client)
    assert s["account"]["user"] is None
    assert s["rounds"][0]["status"] == "pending"


def test_account_cookie_id_only_works_when_signed_in(client, clock, google):
    sign_in(client, google)
    call(client, "start", 0)
    with db.engine().begin() as conn:
        account_player = conn.execute(db.users.select()).mappings().one()["player_id"]
    client.post("/auth/logout")
    client.set_cookie("latestle_player", account_player)  # e.g. a stale cookie on a shared computer
    assert state(client)["rounds"][0]["status"] == "pending"


def test_signing_in_on_another_device_merges_progress(new_player, clock, google):
    laptop, phone = new_player(), new_player()
    sign_in(laptop, google)
    call(laptop, "start", 0)
    clock.advance(2)
    call(laptop, "guess", 0, guess=answers()[0])

    # The phone played the same top puzzle differently, plus a science one.
    call(phone, "start", 0)
    call(phone, "end", 0)
    call(phone, "start", 0, category="science")
    sign_in(phone, google)

    top, sci = state(phone), state(phone, "science")
    assert top["rounds"][0]["status"] == "correct"  # the account's copy wins
    assert sci["rounds"][0]["status"] == "playing"  # moved over from the phone
    assert state(laptop, "science")["rounds"][0]["status"] == "playing"


def test_merge_players_unit(fresh_db, clock):
    with db.engine().begin() as conn:
        for pid in ("anon", "acct"):
            game.ensure_player(conn, pid)
        row = dict(puzzle_date="2026-11-18", score=100, correct=1, finished_at=0)
        conn.execute(db.results.insert().values(player_id="anon", category="top", **row))
        conn.execute(db.results.insert().values(player_id="anon", category="tech", **row))
        conn.execute(db.results.insert().values(player_id="acct", category="top", **{**row, "score": 900}))
        assert accounts.merge_players(conn, "anon", "acct") == 1
        scores = dict(conn.execute(db.results.select().with_only_columns(
            db.results.c.category, db.results.c.score)).all())
        assert scores == {"top": 900, "tech": 100}
        assert conn.execute(db.players.select().where(db.players.c.id == "anon")).first() is None


@pytest.mark.parametrize("url,expected", [
    ("/?category=tech", "/?category=tech"), ("//evil.example", "/"), ("https://evil.example", "/"),
    ("/\\evil.example", "/"), (None, "/"),
])
def test_safe_next(url, expected):
    assert app_module.safe_next(url) == expected
