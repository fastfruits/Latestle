"use strict";

// ---------- Helpers ----------

const $ = (id) => document.getElementById(id);
const REDACTED = "[REDACTED]";
const FAST_MS = 7000; // a 🟩 instead of a 🟨
const HINT_AT_MS = 10000; // show the extra hint after this long

function localDateISO(d = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* private mode etc. – the game still works, it just won't persist */
    }
  },
};

async function api(path, payload) {
  const res = await fetch(path, payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(message, ms = 1500) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), ms);
}

function shake(el) {
  el.classList.remove("shake");
  void el.offsetWidth; // restart the animation
  el.classList.add("shake");
}

const fmtSeconds = (ms) => (ms / 1000).toFixed(1);
const fmtPoints = (n) => n.toLocaleString();

// ---------- State ----------

const today = localDateISO();
const GAME_KEY = `latestle:v2:game:${today}`;
const STATS_KEY = "latestle:v2:stats";

let puzzle = null; // public puzzle from the server
let game = null; // this player's progress today, saved to localStorage
let busy = false;
let frame = null;

function newGame() {
  return {
    phase: "intro", // intro | playing | between | done
    current: 0,
    rounds: puzzle.rounds.map(() => ({
      status: "pending", // pending | playing | correct | missed
      startedAt: null,
      timeMs: null,
      points: 0,
      guesses: [],
      reveal: null,
    })),
  };
}

const save = () => store.set(GAME_KEY, game);
const roundMs = () => puzzle.round_seconds * 1000;
const totalScore = () => game.rounds.reduce((sum, r) => sum + r.points, 0);

function pointsFor(timeMs) {
  // 100 for getting it, plus up to 200 for speed.
  const left = Math.max(0, 1 - timeMs / roundMs());
  return 100 + Math.round(200 * left);
}

// ---------- Rendering helpers ----------

/** A headline as DOM nodes, with the blank drawn as letter cells or the answer. */
function headlineNodes(text, { cells = null, answer = null, status = null } = {}) {
  const frag = document.createDocumentFragment();
  text.split(REDACTED).forEach((part, i) => {
    if (i > 0) {
      if (answer) {
        const span = document.createElement("span");
        span.className = `answer answer-${status}`;
        span.textContent = answer;
        frag.append(span);
      } else {
        frag.append(cells.cloneNode(true));
      }
    }
    frag.append(part);
  });
  return frag;
}

/** Letter cells for the blank, showing what the player has typed so far. */
function buildCells(roundInfo, typed) {
  const wrap = document.createElement("span");
  wrap.className = "slot";
  wrap.setAttribute("aria-label", `${roundInfo.length} letters, starts with ${roundInfo.first_letter}`);
  const letters = typed.replace(/[^\p{L}\p{N}]/gu, "").toUpperCase();
  for (let i = 0; i < roundInfo.length; i++) {
    const cell = document.createElement("span");
    cell.className = "cell";
    if (letters[i]) {
      cell.textContent = letters[i];
      cell.classList.add("filled");
    } else if (i === 0) {
      cell.textContent = roundInfo.first_letter;
      cell.classList.add("hint");
    }
    wrap.append(cell);
  }
  return wrap;
}

function squareFor(r) {
  if (r.status === "correct") return r.timeMs <= FAST_MS ? "fast" : "slow";
  if (r.status === "missed") return "miss";
  return "";
}

// ---------- Screens ----------

function show(section) {
  for (const id of ["intro", "play", "summary"]) $(id).hidden = id !== section;
}

function renderIntro() {
  show("intro");
  const newsDate = new Date(`${puzzle.news_date}T12:00:00`);
  $("news-date").textContent = newsDate.toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric",
  });
  $("rule-rounds").textContent = puzzle.rounds.length;
  $("rule-seconds").textContent = puzzle.round_seconds;
  $("sample-badge").hidden = !puzzle.sample;
  $("start-btn").focus();
}

function renderRound() {
  show("play");
  const i = game.current;
  const r = game.rounds[i];
  const info = puzzle.rounds[i];
  const over = r.status === "correct" || r.status === "missed";

  $("round-label").textContent = `Headline ${i + 1} of ${puzzle.rounds.length}`;
  $("score").textContent = fmtPoints(totalScore());
  $("headline-source").textContent = over && r.reveal?.source ? r.reveal.source : "";

  const headline = $("headline");
  if (over) {
    headline.replaceChildren(headlineNodes(info.headline, { answer: r.reveal?.answer ?? "?", status: r.status }));
  } else {
    headline.replaceChildren(headlineNodes(info.headline, { cells: buildCells(info, $("guess-input").value) }));
  }

  $("tried").textContent = r.guesses.length ? `Tried: ${r.guesses.join(", ")}` : "";
  $("guess-form").hidden = over;
  $("round-result").hidden = !over;
  $("headline-card").classList.toggle("is-correct", r.status === "correct");
  $("headline-card").classList.toggle("is-missed", r.status === "missed");

  if (over) {
    $("round-verdict").textContent = r.status === "correct"
      ? `+${r.points} points · ${fmtSeconds(r.timeMs)}s`
      : r.timeMs >= roundMs() ? "Time's up" : "Passed";
    $("round-verdict").classList.toggle("good", r.status === "correct");
    // Fill the answer into the hint too, if it was showing.
    const hint = $("headline-hint");
    if (!hint.hidden && info.hint && r.reveal) {
      const label = document.createElement("strong");
      label.textContent = "Hint: ";
      hint.replaceChildren(label, headlineNodes(info.hint, { answer: r.reveal.answer, status: r.status }));
    }
    const last = i === puzzle.rounds.length - 1;
    $("next-btn").textContent = last ? "See your results" : "Next headline";
    $("next-btn").focus();
    stopTimer();
    setTimer(r.status === "correct" ? roundMs() - r.timeMs : 0);
  }
}

function setTimer(remaining) {
  const frac = Math.max(0, remaining / roundMs());
  $("timer-fill").style.transform = `scaleX(${frac})`;
  $("timer-fill").classList.toggle("low", remaining <= 5000);
  $("timer-text").textContent = fmtSeconds(Math.max(0, remaining));
}

function showHint(visible) {
  const hint = puzzle.rounds[game.current].hint;
  const el = $("headline-hint");
  el.hidden = !(visible && hint);
  if (!el.hidden && !el.childNodes.length) {
    const label = document.createElement("strong");
    label.textContent = "Hint: ";
    el.replaceChildren(label, headlineNodes(hint, { cells: buildCells(puzzle.rounds[game.current], "") }));
  }
}

function renderSummary() {
  show("summary");
  stopTimer();
  $("final-score").textContent = fmtPoints(totalScore());

  const squares = $("final-squares");
  squares.replaceChildren();
  game.rounds.forEach((r) => {
    const sq = document.createElement("span");
    sq.className = `sq sq-${squareFor(r)}`;
    squares.append(sq);
  });

  const list = $("stories");
  list.replaceChildren();
  game.rounds.forEach((r, i) => {
    const info = puzzle.rounds[i];
    const rev = r.reveal || {};
    const li = document.createElement("li");
    li.className = "story";

    const meta = document.createElement("p");
    meta.className = "story-meta";
    const sq = document.createElement("span");
    sq.className = `sq sq-${squareFor(r)}`;
    const metaText = document.createElement("span");
    metaText.textContent = r.status === "correct"
      ? `${fmtSeconds(r.timeMs)}s · +${r.points} pts`
      : "Missed · 0 pts";
    meta.append(sq, metaText);

    const h = document.createElement("h3");
    h.append(headlineNodes(info.headline, { answer: rev.answer ?? "?", status: r.status }));

    li.append(meta, h);

    if (rev.summary) {
      const p = document.createElement("p");
      p.className = "story-summary";
      p.textContent = rev.summary;
      li.append(p);
    }
    if (rev.url) {
      const a = document.createElement("a");
      a.className = "story-link";
      a.href = rev.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = `Read the full story${rev.source ? ` at ${rev.source}` : ""} →`;
      li.append(a);
    }
    list.append(li);
  });
  startCountdown();
}

function render() {
  if (game.phase === "intro") renderIntro();
  else if (game.phase === "done") renderSummary();
  else renderRound();
}

// ---------- Timer ----------

function stopTimer() {
  cancelAnimationFrame(frame);
  frame = null;
}

function tick() {
  const r = game.rounds[game.current];
  const elapsed = Date.now() - r.startedAt;
  const remaining = roundMs() - elapsed;
  setTimer(remaining);
  showHint(elapsed >= HINT_AT_MS);

  if (remaining <= 0) {
    // Let an in-flight guess finish first; it was typed before the buzzer.
    if (!busy) endRound("missed");
    else frame = requestAnimationFrame(tick);
    return;
  }
  frame = requestAnimationFrame(tick);
}

function startRound() {
  const r = game.rounds[game.current];
  if (r.status === "pending") {
    r.status = "playing";
    r.startedAt = Date.now();
    game.phase = "playing";
    save();
  }
  $("guess-input").value = "";
  $("headline-hint").replaceChildren();
  $("headline-hint").hidden = true;
  renderRound();
  stopTimer();
  frame = requestAnimationFrame(tick);
  $("guess-input").focus();
}

async function endRound(status, { timeMs = null, reveal = null } = {}) {
  const r = game.rounds[game.current];
  if (r.status !== "playing") return;
  stopTimer();
  r.status = status;
  r.timeMs = timeMs ?? Math.min(Date.now() - r.startedAt, roundMs());
  r.points = status === "correct" ? pointsFor(r.timeMs) : 0;
  game.phase = "between";
  save();

  if (!reveal) {
    try {
      reveal = (await api("/api/reveal", { date: today, round: game.current })).reveal;
    } catch (err) {
      toast(err.message);
    }
  }
  r.reveal = reveal;
  save();
  renderRound();
  if (status === "correct") toast(r.timeMs <= FAST_MS ? "Lightning fast! ⚡" : "Got it!");
}

async function submitGuess() {
  const r = game.rounds[game.current];
  const input = $("guess-input");
  const guess = input.value.trim();
  if (busy || r.status !== "playing" || !guess) return;

  if (r.guesses.some((g) => g.toLowerCase() === guess.toLowerCase())) {
    toast("Already tried that");
    shake(input);
    return;
  }

  // Time is measured when the player hits Enter, not when the server replies.
  const timeMs = Date.now() - r.startedAt;
  busy = true;
  try {
    const res = await api("/api/guess", { date: today, round: game.current, guess });
    if (res.correct && timeMs <= roundMs()) {
      await endRound("correct", { timeMs, reveal: res.reveal });
      return;
    }
    r.guesses.push(guess);
    save();
    // Only clear what was submitted; the player may already be typing again.
    if (input.value.trim() === guess) input.value = "";
    shake(input);
    if (res.close) toast("So close…");
    renderRound();
  } catch (err) {
    toast(err.message);
  } finally {
    busy = false;
    if (game.rounds[game.current].status === "playing") input.focus();
  }
}

function nextRound() {
  if (game.current < puzzle.rounds.length - 1) {
    game.current += 1;
    save();
    startRound();
  } else {
    game.phase = "done";
    save();
    recordStats();
    renderSummary();
    setTimeout(openStats, 900);
  }
}

// ---------- Stats ----------

function recordStats() {
  const stats = store.get(STATS_KEY, { played: 0, total: 0, best: 0, streak: 0, maxStreak: 0, last: null, recent: [] });
  const score = totalScore();
  stats.played += 1;
  stats.total += score;
  stats.best = Math.max(stats.best, score);
  stats.streak = stats.last === puzzle.number - 1 ? stats.streak + 1 : 1;
  stats.maxStreak = Math.max(stats.maxStreak, stats.streak);
  stats.last = puzzle.number;
  stats.recent = [...stats.recent, { number: puzzle.number, score }].slice(-7);
  store.set(STATS_KEY, stats);
}

function openStats() {
  const stats = store.get(STATS_KEY, null) || { played: 0, total: 0, best: 0, streak: 0, last: null, recent: [] };
  const streakAlive = stats.last !== null && puzzle && puzzle.number - stats.last <= 1;
  $("stat-played").textContent = stats.played;
  $("stat-best").textContent = fmtPoints(stats.best);
  $("stat-avg").textContent = stats.played ? fmtPoints(Math.round(stats.total / stats.played)) : 0;
  $("stat-streak").textContent = streakAlive ? stats.streak : 0;

  const recent = $("recent");
  recent.replaceChildren();
  const max = Math.max(1, ...stats.recent.map((r) => r.score));
  if (!stats.recent.length) recent.textContent = "Play a day to see your scores here.";
  stats.recent.forEach((r) => {
    const row = document.createElement("div");
    row.className = "dist-row";
    const label = document.createElement("span");
    label.textContent = `#${r.number}`;
    const bar = document.createElement("span");
    bar.className = "dist-bar";
    if (r.number === puzzle?.number) bar.classList.add("today");
    bar.style.width = `${Math.max(12, (100 * r.score) / max)}%`;
    bar.textContent = fmtPoints(r.score);
    row.append(label, bar);
    recent.append(row);
  });
  $("stats-dialog").showModal();
}

// ---------- Sharing ----------

function shareText() {
  const emoji = { fast: "🟩", slow: "🟨", miss: "🟥" };
  const squares = game.rounds.map((r) => emoji[squareFor(r)] || "⬜").join("");
  const times = game.rounds.filter((r) => r.status === "correct").map((r) => r.timeMs);
  const fastest = times.length ? `\n⚡ Fastest: ${fmtSeconds(Math.min(...times))}s` : "";
  return `Latestle #${puzzle.number} · ${fmtPoints(totalScore())} pts\n${squares}${fastest}\n${location.origin}`;
}

async function share() {
  const text = shareText();
  try {
    if (navigator.share && matchMedia("(pointer: coarse)").matches) {
      await navigator.share({ text });
      return;
    }
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard");
  } catch (err) {
    if (err?.name !== "AbortError") toast("Couldn't share – try copying manually");
  }
}

// ---------- Countdown ----------

let countdownTimer;
function startCountdown() {
  clearInterval(countdownTimer);
  const pad = (n) => String(n).padStart(2, "0");
  const tickCountdown = () => {
    const now = new Date();
    const midnight = new Date(now);
    midnight.setHours(24, 0, 0, 0);
    const secs = Math.max(0, Math.floor((midnight - now) / 1000));
    if (secs === 0) return location.reload();
    $("countdown").textContent = `${pad(Math.floor(secs / 3600))}:${pad(Math.floor((secs % 3600) / 60))}:${pad(secs % 60)}`;
  };
  tickCountdown();
  countdownTimer = setInterval(tickCountdown, 1000);
}

// ---------- Boot ----------

async function init() {
  $("help-btn").addEventListener("click", () => $("help-dialog").showModal());
  $("stats-btn").addEventListener("click", openStats);
  $("share-btn").addEventListener("click", share);
  $("start-btn").addEventListener("click", startRound);
  $("next-btn").addEventListener("click", nextRound);
  $("pass-btn").addEventListener("click", () => endRound("missed"));
  $("guess-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitGuess();
  });
  // Mirror typing into the letter cells in the headline.
  $("guess-input").addEventListener("input", () => {
    if (game?.rounds[game.current].status === "playing") {
      const info = puzzle.rounds[game.current];
      $("headline").replaceChildren(headlineNodes(info.headline, { cells: buildCells(info, $("guess-input").value) }));
    }
  });
  document.querySelectorAll("dialog").forEach((d) =>
    d.addEventListener("click", (e) => e.target === d && d.close())
  );

  // The first visit of the day may build the puzzle from the feeds.
  const loading = setTimeout(() => ($("loading").hidden = false), 300);
  try {
    puzzle = await api(`/api/puzzle?date=${today}`);
  } catch (err) {
    clearTimeout(loading);
    $("loading").hidden = true;
    $("dateline").textContent = "Couldn't load today's headlines";
    toast(err.message, 4000);
    return;
  }
  clearTimeout(loading);
  $("loading").hidden = true;

  const longDate = new Date(`${today}T12:00:00`).toLocaleDateString(undefined, {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
  });
  $("dateline").textContent = `No. ${puzzle.number} · ${longDate}`;

  game = store.get(GAME_KEY, null);
  if (!game || game.rounds?.length !== puzzle.rounds.length) {
    game = newGame();
    save();
  }

  // Reloading doesn't pause the clock: resume the round where it stands
  // (or end it, if its 20 seconds already ran out).
  if (game.phase === "playing") startRound();
  else render();

  if (!store.get("latestle:seen-help", false)) {
    $("help-dialog").showModal();
    store.set("latestle:seen-help", true);
  }
}

init();
