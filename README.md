# Latestle

A daily speed game about yesterday's news.

Every day you get 5 real headlines from the day before, each with one name (a person, place, country or company) blacked out. You have 20 seconds per headline to fill in the blank. Faster answers score more. At the end you get a recap of every story with a link to the full article, and a result you can share:

## How a day's puzzle is made

The first time anyone loads a day, the server builds that day's puzzle from the previous day's headlines and saves it to `puzzles/YYYY-MM-DD.json`, so every player gets the same rounds.

1. **Fetch** headlines from public RSS feeds (BBC News, NPR, The Guardian), keeping those published the day before. If that day is thin, it widens to the surrounding days, and then to the last few days if there still aren't five good stories.
2. **Find names.** A word counts as a name if it appears capitalised mid-sentence and on its own somewhere ("Trump", "Nvidia"). This skips ordinary words that just start a headline and fragments of longer names like "Cape" in "Cape Town". Headlines written in Title Case (Foreign Policy, The Diplomat) are ignored here, since every word is capitalised.
3. **Rank by coverage.** Names reported by the most outlets, then in the most stories, come first. Those are the stories most people will have heard of, which keeps it guessable.
4. **One round per story.** A headline is skipped if it mentions an earlier answer, so the day doesn't end up as five rounds about the same event.

### Categories

The main page (`/`) is the original mix of top stories. Each category is its own daily puzzle, built only from that category's feeds and played at `/?category=<slug>`:

| Category | Slug | Feeds |
| --- | --- | --- |
| Top stories | `top` | BBC News (top + world), NPR, The Guardian (world) |
| World | `world` | BBC World, The Guardian World, NPR World, Al Jazeera |
| USA | `usa` | BBC US & Canada, NPR National, NPR Politics, The Guardian US |
| Europe | `europe` | BBC Europe, The Guardian Europe, Euronews, Politico Europe |
| Geopolitics | `geopolitics` | Foreign Policy, The Diplomat, Al Jazeera, BBC Middle East, BBC Asia |
| Science | `science` | BBC Science, NPR Science, The Guardian Science, ScienceDaily |
| Tech | `tech` | BBC Technology, NPR Technology, The Guardian Technology, Ars Technica |
| Business | `business` | BBC Business, NPR Business, The Guardian Business |

The main puzzle is saved as `puzzles/YYYY-MM-DD.json` and the others as `puzzles/YYYY-MM-DD-<slug>.json`. To add a category, add an entry to `CATEGORIES` in `latestle/news.py`.

If the feeds are unreachable, the game uses headlines from `puzzles/samples.json` and shows a "Sample headlines" badge. It tries the feeds again after 10 minutes.

To preview or pre-build a day:

```bash
python scripts/generate_puzzle.py                  # preview today
python scripts/generate_puzzle.py --save           # write puzzles/<today>.json
python scripts/generate_puzzle.py --category science
python scripts/generate_puzzle.py --category all --save
```

You can hand-edit a saved puzzle file and the server will use your version. Generated puzzle files are git-ignored. Use `git add -f` if you want to commit one.

## Scoring

- A correct answer scores 100 points plus a speed bonus of up to 200, which shrinks as the timer runs down. A perfect day is 1,500.
- You can guess as many times as you like within the 20 seconds. Small typos count for names of 5 letters or more.
- You always see the first letter and the length of the name. At 10 seconds, the article's summary appears as an extra hint (with the name blacked out).
- The server times every round. It records when a round starts and measures each guess against that, allowing 1.5 seconds for network delay. A headline is only sent when its round starts, so nobody can read ahead with the clock stopped.
- Reloading doesn't pause the clock. The round picks up from the server's time.

## Players and saved progress

Accounts are optional. On a player's first visit the server gives them a random anonymous ID in a cookie (`latestle_player`, HttpOnly, kept for 2 years). Everything is saved on the server under that ID:

| Table | One row per | Holds |
| --- | --- | --- |
| `players` | player | the player ID and when it was created |
| `users` | Google account | Google's account ID, email, name, picture, and the player ID that holds the account's progress |
| `round_plays` | player, day, category, round | server start time, status, time taken, points, wrong guesses |
| `results` | player, day, category | final score and number correct |

From these the server works out each player's stats (played, best, average, current and best streak, last 7 scores) and how they compare with everyone else on the same puzzle ("You beat 72% of players today · 143 players · average 812"). Stats are kept separately per category.

Without an account, clearing cookies or switching devices starts a fresh player.

### Signing in with Google

Players can sign in with Google (the person button at the top right) so their progress follows them to any device. An account owns one player ID, so the game works the same signed in or not:

- **First sign-in:** the anonymous player on that browser becomes the account's player, so games already played come along.
- **Signing in on another device:** that device's anonymous games are merged into the account. If the same puzzle was played on both, the account's copy is kept.
- **Signing out:** the browser gets a brand-new anonymous ID, so a shared computer can't reach the account afterwards. An old anonymous ID that now belongs to an account only works while signed in.

Who's signed in is kept in a signed session cookie (`latestle_session`, 1 year). The only Google data stored is the account ID, email, name and picture.

**Setting it up**

1. In the [Google Cloud console](https://console.cloud.google.com/apis/credentials), create a project. Set up the OAuth consent screen (app name, support email; the `openid`, `email` and `profile` scopes need no review).
2. Create credentials → **OAuth client ID** → **Web application**. Under *Authorised redirect URIs* add `http://localhost:5001/auth/google/callback`, plus `https://<your-domain>/auth/google/callback` once deployed.
3. Copy `.env.example` to `.env` and fill in `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. The app loads `.env` automatically, and `.env` is git-ignored.
4. Restart the app. The sign-in button appears once both values are set. Without them the game runs as before, anonymous only.

While the consent screen is in *Testing* mode, only Google accounts you add as test users can sign in. Publish it when you launch.

By default the database is SQLite at `instance/latestle.db` (git-ignored, created automatically). For production, set `DATABASE_URL` to a Postgres URL and `pip install "psycopg[binary]"`. There are no migrations yet: if the table layout changes during development, delete `instance/latestle.db` and it will be recreated.

## Code layout

| Piece | Where |
| --- | --- |
| Flask server, player cookie, Google sign-in routes, JSON API | `app.py` |
| Accounts: sign-in, linking and merging progress | `latestle/accounts.py` |
| Fetching headlines and picking answers | `latestle/news.py` |
| Loading or generating puzzles, checking guesses | `latestle/puzzles.py` |
| Database tables | `latestle/db.py` |
| Server-timed rounds, scoring, results and stats | `latestle/game.py` |
| Page, styles, game logic | `templates/index.html`, `static/` |
| Fallback headlines | `puzzles/samples.json` |

### API

Every call takes `date` (the player's local `YYYY-MM-DD`) and an optional `category` (default `top`). Round calls also take `round` (0–4).

- `GET /api/state` returns the puzzle info, the list of categories, and this player's rounds so far. Each round is `pending`, `playing` (with its headline and elapsed time), or finished (with points and the full story). Once all rounds are done it also includes `result`, the player's score and how it compares.
- `POST /api/round/start` starts the clock on a round and returns its headline, the answer's length and first letter. Rounds must be played in order. Starting one again doesn't reset its clock.
- `POST /api/round/hint` returns the hint once 10 seconds have passed.
- `POST /api/round/guess` with `guess` returns `correct` and `close`, plus the updated round. A correct guess includes the time, points and full story.
- `POST /api/round/end` passes, or ends the round when the countdown hits zero.
- `GET /api/stats` returns this player's stats for the category.

The puzzle changes at the player's own midnight, because the browser sends its local date. The server only accepts today ± 1 day.

## Deploying

The `Procfile` runs `gunicorn app:app`, which works on Render, Railway, Heroku and similar hosts. Set these there (see `.env.example`):

- `DATABASE_URL`: use Postgres. A SQLite file on a temporary filesystem would lose every player's progress on restart.
- `SECRET_KEY`: a long random value. Changing it signs everyone out.
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, with the production redirect URI added in Google's console.
- `SECURE_COOKIES=1` (served over https) and `BEHIND_PROXY=1` (so the redirect to Google uses your real https address).
 Likewise, a day's puzzle is rebuilt if the server restarts. Usually that gives the same rounds, but not always if the feeds changed in between. Saving puzzles to a database or storage bucket would fix that.

## Ideas for what's next

- Use an LLM to pick the hidden word and write cleaner hints, instead of the capitalised-word heuristic.
- More sign-in options (Apple, email magic link), and a way to delete your account.
- A daily leaderboard. Rounds are already timed on the server, so scores can be trusted.
- Multiple-choice mode for an even easier game.
- A puzzle archive for past days.
