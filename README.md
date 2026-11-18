# Latestle

A daily speed game about yesterday's news.

Every day you get 5 real headlines from the day before, each with one name (a person, place, country or company) blacked out. You have 20 seconds per headline to fill in the blank. Faster answers score more. At the end you get a recap of every story with a link to the full article, and a result you can share:

## How a day's puzzle is made

The first time anyone loads a day, the server builds that day's puzzle from the previous day's headlines and saves it to `puzzles/YYYY-MM-DD.json`, so every player gets the same rounds.

1. **Fetch** headlines from public RSS feeds (BBC News, NPR, The Guardian), keeping those published the day before. If that day is thin, it widens to the surrounding days.
2. **Find names.** A word counts as a name if it appears capitalised mid-sentence and on its own somewhere ("Trump", "Nvidia"). This skips ordinary words that just start a headline and fragments of longer names like "Cape" in "Cape Town".
3. **Rank by coverage.** Names reported by the most outlets, then in the most stories, come first. Those are the stories most people will have heard of, which keeps it guessable.
4. **One round per story.** A headline is skipped if it mentions an earlier answer, so the day doesn't end up as five rounds about the same event.

If the feeds are unreachable, the game uses headlines from `puzzles/samples.json` and shows a "Sample headlines" badge. It tries the feeds again after 10 minutes.

To preview or pre-build a day:

```bash
python scripts/generate_puzzle.py                  # preview today
python scripts/generate_puzzle.py --save           # write puzzles/<today>.json
```

You can hand-edit a saved puzzle file and the server will use your version. Generated puzzle files are git-ignored. Use `git add -f` if you want to commit one.

## Scoring

- A correct answer scores 100 points plus a speed bonus of up to 200, which shrinks as the timer runs down. A perfect day is 1,500.
- You can guess as many times as you like within the 20 seconds. Small typos count for names of 5 letters or more.
- You always see the first letter and the length of the name. At 10 seconds, the article's summary appears as an extra hint (with the name blacked out).
- Reloading doesn't pause the clock. A round keeps counting from when it started.

Progress and stats (games played, best score, average, streak, recent scores) are kept in the browser's `localStorage`. There are no accounts yet.

## Code layout

| Piece | Where |
| --- | --- |
| Flask server and JSON API | `app.py` |
| Fetching headlines and picking answers | `latestle/news.py` |
| Loading or generating puzzles, checking guesses | `latestle/puzzles.py` |
| Page, styles, game logic | `templates/index.html`, `static/` |
| Fallback headlines | `puzzles/samples.json` |

### API

- `GET /api/puzzle?date=YYYY-MM-DD` returns the rounds with answers hidden: each has the redacted headline, the answer's length and first letter, and a hint.
- `POST /api/guess` with `{"date", "round", "guess"}` returns `correct` and `close`, plus the full story when the guess is correct.
- `POST /api/reveal` with `{"date", "round"}` returns the full story when time runs out or the player passes.

The browser sends its local date so the puzzle changes at the player's own midnight. The server only accepts today ± 1 day.

## Deploying

The `Procfile` runs `gunicorn app:app`, which works on Render, Railway, Heroku and similar hosts. On hosts with a temporary filesystem, a day's puzzle is rebuilt if the server restarts. Usually that gives the same rounds, but not always if the feeds changed in between. Saving puzzles to a database or storage bucket would fix that.

## Ideas for what's next

- Use an LLM to pick the hidden word and write cleaner hints, instead of the capitalised-word heuristic.
- A leaderboard. That needs the server to time rounds itself, because timing in the browser is easy to fake.
- Multiple-choice mode for an even easier game.
- A puzzle archive for past days.
