import json
from datetime import date, datetime, timedelta, timezone

import pytest

import app as app_module
from latestle import news, puzzles
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
    monkeypatch.setattr(news, "fetch_all", lambda: list(HEADLINES))
    monkeypatch.setattr(news, "from_day", lambda hs, day: hs)
    monkeypatch.setattr(puzzles, "PUZZLE_DIR", tmp_path)
    puzzles._failed_at.clear()
    return tmp_path


@pytest.fixture
def client(fake_news):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


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
    monkeypatch.setattr(news, "fetch_all", lambda: pytest.fail("should not refetch"))
    assert [r.answer for r in load_puzzle(date(2026, 11, 17)).rounds] == [r.answer for r in p.rounds]


def test_falls_back_to_samples_when_feeds_fail(fake_news, monkeypatch):
    monkeypatch.setattr(news, "fetch_all", lambda: [])
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


def test_puzzle_endpoint_hides_answers(client):
    data = client.get(f"/api/puzzle?date={today()}").get_json()
    assert len(data["rounds"]) == 5 and data["round_seconds"] == 20
    first = data["rounds"][0]
    assert set(first) == {"headline", "length", "first_letter", "hint"}
    answers = [r.answer for r in load_puzzle(today_utc()).rounds]
    dumped = json.dumps(data)
    for a in answers:
        assert f'"{a}"' not in dumped and f" {a} " not in dumped


def test_guess_and_reveal(client):
    answer = load_puzzle(today_utc()).rounds[0].answer
    wrong = client.post("/api/guess", json={"date": today(), "round": 0, "guess": "zzzz"}).get_json()
    assert wrong == {"correct": False, "close": False}

    right = client.post("/api/guess", json={"date": today(), "round": 0, "guess": answer.lower()}).get_json()
    assert right["correct"] and right["reveal"]["answer"] == answer and right["reveal"]["url"]

    reveal = client.post("/api/reveal", json={"date": today(), "round": 1}).get_json()["reveal"]
    assert reveal["answer"] and reveal["original"]


def test_bad_requests(client):
    future = (today_utc() + timedelta(days=3)).isoformat()
    assert client.get(f"/api/puzzle?date={future}").status_code == 400
    assert client.post("/api/guess", json={"date": today(), "round": 9, "guess": "x"}).status_code == 400
    assert client.post("/api/reveal", json={"date": today(), "round": "x"}).status_code == 400
