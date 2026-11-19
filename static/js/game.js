"use strict";

// The server is the source of truth: it times every round, scores it, and
// keeps this player's progress (tied to an anonymous cookie). The browser
// just draws the countdown and asks the server what happened.

// ---------- Helpers ----------

const $ = (id) => document.getElementById(id);
const REDACTED = "[REDACTED]";
const FAST_MS = 7000; // a 🟩 instead of a 🟨
const HINT_AT_MS = 10000; // the server releases the hint after this long

function localDateISO(d = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

const prefs = {
  get(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* fine without it */ }
  },
};

async function api(path, payload) {
  const res = await fetch(path, payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ date: today, category, ...payload }),
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
const isDone = (r) => r.status === "correct" || r.status === "missed";

// ---------- State ----------

const today = localDateISO();
// Which edition: the main "top" puzzle, or a category like ?category=science.
const category = new URLSearchParams(location.search).get("category") || "top";
const query = `date=${today}&category=${encodeURIComponent(category)}`;

let meta = null; // { number, category, category_name, news_date, round_seconds, total_rounds, sample }
let categories = []; // [{ slug, name }] for the edition picker
let account = { google_enabled: false, user: null }; // who's signed in, if anyone
let rounds = []; // one entry per round, as the server describes it
let result = null; // { score, players, average, beat_pct } once the day is done
let current = 0;
let startedAt = 0; // local clock time the current round started
let busy = false;
let hintRequested = false;
let frame = null;

const roundMs = () => meta.round_seconds * 1000;
const totalScore = () => rounds.reduce((sum, r) => sum + (r.points || 0), 0);

function setRound(view) {
  rounds[view.index] = view;
  if (view.status === "playing") {
    // Use the server's elapsed time, so the countdown matches its clock.
    startedAt = Date.now() - view.elapsed_ms;
  }
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
function buildCells(r, typed) {
  const wrap = document.createElement("span");
  wrap.className = "slot";
  wrap.setAttribute("aria-label", `${r.length} letters, starts with ${r.first_letter}`);
  const letters = typed.replace(/[^\p{L}\p{N}]/gu, "").toUpperCase();
  for (let i = 0; i < r.length; i++) {
    const cell = document.createElement("span");
    cell.className = "cell";
    if (letters[i]) {
      cell.textContent = letters[i];
      cell.classList.add("filled");
    } else if (i === 0) {
      cell.textContent = r.first_letter;
      cell.classList.add("hint");
    }
    wrap.append(cell);
  }
  return wrap;
}

function squareFor(r) {
  if (r.status === "correct") return r.time_ms <= FAST_MS ? "fast" : "slow";
  if (r.status === "missed") return "miss";
  return "";
}

// ---------- Screens ----------

function show(section) {
  for (const id of ["intro", "play", "summary"]) $(id).hidden = id !== section;
  // Switching editions or signing in mid-round would just burn the clock,
  // so hide those during a round.
  $("editions").hidden = section === "play";
  $("account-btn").hidden = section === "play" || !account.google_enabled;
}

// ---------- Account ----------

/** A profile picture, or the first letter of the name if there isn't one. */
function avatarNodes(user) {
  if (user.picture) {
    const img = document.createElement("img");
    img.src = user.picture;
    img.alt = "";
    img.referrerPolicy = "no-referrer"; // Google's image host refuses some referrers
    return [img];
  }
  return [(user.name || user.email || "?").trim()[0].toUpperCase()];
}

function renderAccount() {
  const user = account.user;
  const btn = $("account-btn");
  btn.hidden = !account.google_enabled || !$("play").hidden;
  btn.setAttribute("aria-label", user ? `Account: ${user.name || user.email}` : "Sign in");
  if (user) {
    btn.replaceChildren(...avatarNodes(user));
  } else {
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M12 12a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9Zm0 2c-4.4 0-8 2.2-8 5v2h16v-2c0-2.8-3.6-5-8-5Z"/></svg>';
  }

  $("account-signed-out").hidden = !!user;
  $("account-signed-in").hidden = !user;
  if (user) {
    $("account-avatar").replaceChildren(...avatarNodes(user));
    $("account-name").textContent = user.name || "";
    $("account-email").textContent = user.email || "";
  }
  // Come back to this same page (and category) after signing in.
  $("google-btn").href = `/auth/google?next=${encodeURIComponent(location.pathname + location.search)}`;
  $("signin-nudge").hidden = !account.google_enabled || !!user;
}

async function logout() {
  try {
    await fetch("/auth/logout", { method: "POST" });
  } finally {
    location.reload();
  }
}

function renderEditions() {
  const nav = $("editions");
  nav.replaceChildren();
  categories.forEach((c) => {
    const a = document.createElement("a");
    a.className = "edition";
    a.textContent = c.name;
    a.href = c.slug === "top" ? "/" : `/?category=${encodeURIComponent(c.slug)}`;
    if (c.slug === meta.category) a.setAttribute("aria-current", "page");
    nav.append(a);
  });
}

function renderIntro() {
  show("intro");
  $("intro-kicker").textContent = meta.category === "top"
    ? "Yesterday's news"
    : `Yesterday's news · ${meta.category_name}`;
  const newsDate = new Date(`${meta.news_date}T12:00:00`);
  $("news-date").textContent = newsDate.toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric",
  });
  $("rule-rounds").textContent = meta.total_rounds;
  $("rule-seconds").textContent = meta.round_seconds;
  $("sample-badge").hidden = !meta.sample;
  $("start-btn").focus();
}

function renderHint(r) {
  const el = $("headline-hint");
  // Done rounds only show the hint if the player got that far.
  const show = r.hint && (r.status === "playing" || r.time_ms >= HINT_AT_MS);
  el.hidden = !show;
  if (!show) return;
  const label = document.createElement("strong");
  label.textContent = "Hint: ";
  el.replaceChildren(label, isDone(r)
    ? headlineNodes(r.hint, { answer: r.reveal.answer, status: r.status })
    : headlineNodes(r.hint, { cells: buildCells(r, "") }));
}

function renderHeadline(r) {
  $("headline").replaceChildren(isDone(r)
    ? headlineNodes(r.headline, { answer: r.reveal.answer, status: r.status })
    : headlineNodes(r.headline, { cells: buildCells(r, $("guess-input").value) }));
}

function renderRound() {
  show("play");
  const r = rounds[current];
  const over = isDone(r);

  $("round-label").textContent = `Headline ${current + 1} of ${meta.total_rounds}`;
  $("score").textContent = fmtPoints(totalScore());
  $("headline-source").textContent = over ? r.reveal.source : "";
  renderHeadline(r);
  renderHint(r);

  $("tried").textContent = r.guesses.length ? `Tried: ${r.guesses.join(", ")}` : "";
  $("guess-form").hidden = over;
  $("round-result").hidden = !over;

  if (over) {
    stopTimer();
    setTimer(r.status === "correct" ? roundMs() - r.time_ms : 0);
    $("round-verdict").textContent = r.status === "correct"
      ? `+${r.points} points · ${fmtSeconds(r.time_ms)}s`
      : r.time_ms >= roundMs() ? "Time's up" : "Passed";
    $("round-verdict").classList.toggle("good", r.status === "correct");
    const last = current === meta.total_rounds - 1;
    $("next-btn").textContent = last ? "See your results" : "Next headline";
    $("next-btn").focus();
  }
}

function setTimer(remaining) {
  const frac = Math.max(0, remaining / roundMs());
  $("timer-fill").style.transform = `scaleX(${frac})`;
  $("timer-fill").classList.toggle("low", remaining <= 5000);
  $("timer-text").textContent = fmtSeconds(Math.max(0, remaining));
}

function renderResultLine() {
  const el = $("result-line");
  if (!result) {
    el.textContent = "";
    return;
  }
  if (result.beat_pct === null) {
    el.textContent = "You're the first to finish today. Check back later to see how you compare!";
  } else {
    const players = `${fmtPoints(result.players)} players`;
    el.textContent = `You beat ${result.beat_pct}% of players today · ${players} · average ${fmtPoints(result.average)}`;
  }
}

function renderSummary() {
  show("summary");
  stopTimer();
  $("summary-kicker").textContent = meta.category === "top" ? "Final score" : `Final score · ${meta.category_name}`;
  $("final-score").textContent = fmtPoints(totalScore());
  renderResultLine();

  const squares = $("final-squares");
  squares.replaceChildren();
  rounds.forEach((r) => {
    const sq = document.createElement("span");
    sq.className = `sq sq-${squareFor(r)}`;
    squares.append(sq);
  });

  const list = $("stories");
  list.replaceChildren();
  rounds.forEach((r) => {
    const rev = r.reveal;
    const li = document.createElement("li");
    li.className = "story";

    const meta = document.createElement("p");
    meta.className = "story-meta";
    const sq = document.createElement("span");
    sq.className = `sq sq-${squareFor(r)}`;
    const metaText = document.createElement("span");
    metaText.textContent = r.status === "correct"
      ? `${fmtSeconds(r.time_ms)}s · +${r.points} pts`
      : "Missed · 0 pts";
    meta.append(sq, metaText);

    const h = document.createElement("h3");
    h.append(headlineNodes(r.headline, { answer: rev.answer, status: r.status }));
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

/** Pick the right screen for wherever the player is in their day. */
function render() {
  const playing = rounds.findIndex((r) => r.status === "playing");
  const doneCount = rounds.filter(isDone).length;

  if (doneCount === rounds.length) return renderSummary();
  if (playing >= 0) {
    current = playing;
    return resumeRound();
  }
  if (doneCount === 0) return renderIntro();
  // Between rounds: show the last finished one with its Next button.
  current = doneCount - 1;
  renderRound();
}

// ---------- Timer ----------

function stopTimer() {
  cancelAnimationFrame(frame);
  frame = null;
}

function tick() {
  const r = rounds[current];
  if (r.status !== "playing") return;
  const elapsed = Date.now() - startedAt;
  setTimer(roundMs() - elapsed);

  if (elapsed >= HINT_AT_MS && !r.hint && !hintRequested) fetchHint();

  if (elapsed >= roundMs()) {
    // Let an in-flight guess land first; it was sent before the buzzer.
    if (!busy) return endRound();
  }
  frame = requestAnimationFrame(tick);
}

async function fetchHint() {
  hintRequested = true;
  try {
    const view = await api("/api/round/hint", { round: current });
    if (view.hint && rounds[current].status === "playing") {
      rounds[current].hint = view.hint;
      renderHint(rounds[current]);
    } else {
      setTimeout(() => (hintRequested = false), 500); // clocks slightly apart; try again
    }
  } catch {
    hintRequested = false;
  }
}

function resumeRound() {
  hintRequested = false;
  $("guess-input").value = "";
  renderRound();
  stopTimer();
  frame = requestAnimationFrame(tick);
  $("guess-input").focus();
}

async function startRound(index) {
  if (busy) return;
  busy = true;
  try {
    current = index;
    setRound(await api("/api/round/start", { round: index }));
    if (isDone(rounds[index])) renderRound();
    else resumeRound();
  } catch (err) {
    toast(err.message);
    await resync();
  } finally {
    busy = false;
  }
}

async function endRound() {
  stopTimer();
  busy = true;
  try {
    setRound(await api("/api/round/end", { round: current }));
    renderRound();
  } catch (err) {
    toast(err.message);
    await resync();
  } finally {
    busy = false;
  }
}

async function submitGuess() {
  const r = rounds[current];
  const input = $("guess-input");
  const guess = input.value.trim();
  if (busy || r.status !== "playing" || !guess) return;

  if (r.guesses.some((g) => g.toLowerCase() === guess.toLowerCase())) {
    toast("Already tried that");
    shake(input);
    return;
  }

  busy = true;
  try {
    const res = await api("/api/round/guess", { round: current, guess });
    const hint = r.hint;
    setRound(res.round);
    if (!rounds[current].hint && hint) rounds[current].hint = hint; // keep a hint we already have

    if (res.correct) {
      renderRound();
      toast(res.round.time_ms <= FAST_MS ? "Lightning fast! ⚡" : "Got it!");
    } else if (isDone(res.round)) {
      renderRound(); // the buzzer beat the guess
    } else {
      // Only clear what was submitted; the player may already be typing again.
      if (input.value.trim() === guess) input.value = "";
      shake(input);
      if (res.close) toast("So close…");
      renderHeadline(rounds[current]);
      $("tried").textContent = `Tried: ${rounds[current].guesses.join(", ")}`;
    }
  } catch (err) {
    toast(err.message);
  } finally {
    busy = false;
    if (rounds[current].status === "playing") input.focus();
  }
}

async function nextRound() {
  if (current < meta.total_rounds - 1) return startRound(current + 1);
  await resync(); // picks up the final result and percentile
  setTimeout(openStats, 900);
}

/** Reload everything from the server and redraw. */
async function resync() {
  const state = await api(`/api/state?${query}`);
  meta = state.puzzle;
  categories = state.categories || [];
  account = state.account || account;
  renderEditions();
  renderAccount();
  rounds = state.rounds;
  result = state.result || null;
  const playing = rounds.find((r) => r.status === "playing");
  if (playing) setRound(playing);
  render();
}

// ---------- Stats ----------

async function openStats() {
  let stats;
  try {
    stats = await api(`/api/stats?${query}`);
  } catch (err) {
    return toast(err.message);
  }
  $("stat-played").textContent = stats.played;
  $("stat-best").textContent = fmtPoints(stats.best);
  $("stat-avg").textContent = fmtPoints(stats.average);
  $("stat-streak").textContent = stats.streak;
  $("stats-edition").textContent = meta?.category_name || "";

  const recent = $("recent");
  recent.replaceChildren();
  if (!stats.recent.length) recent.textContent = "Finish a day to see your scores here.";
  const max = Math.max(1, ...stats.recent.map((r) => r.score));
  stats.recent.forEach((r) => {
    const row = document.createElement("div");
    row.className = "dist-row";
    const label = document.createElement("span");
    label.textContent = `#${r.number}`;
    const bar = document.createElement("span");
    bar.className = "dist-bar";
    if (r.date === today) bar.classList.add("today");
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
  const squares = rounds.map((r) => emoji[squareFor(r)] || "⬜").join("");
  const times = rounds.filter((r) => r.status === "correct").map((r) => r.time_ms);
  const fastest = times.length ? `\n⚡ Fastest: ${fmtSeconds(Math.min(...times))}s` : "";
  const beat = result?.beat_pct != null ? `\nBeat ${result.beat_pct}% of players` : "";
  const edition = meta.category === "top" ? "" : ` ${meta.category_name}`;
  const link = meta.category === "top" ? location.origin : `${location.origin}/?category=${meta.category}`;
  return `Latestle #${meta.number}${edition} · ${fmtPoints(totalScore())} pts\n${squares}${fastest}${beat}\n${link}`;
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
  $("account-btn").addEventListener("click", () => $("account-dialog").showModal());
  $("nudge-btn").addEventListener("click", () => $("account-dialog").showModal());
  $("logout-btn").addEventListener("click", logout);
  $("share-btn").addEventListener("click", share);
  $("start-btn").addEventListener("click", () => startRound(0));
  $("next-btn").addEventListener("click", nextRound);
  $("pass-btn").addEventListener("click", () => !busy && endRound());
  $("guess-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitGuess();
  });
  // Mirror typing into the letter cells in the headline.
  $("guess-input").addEventListener("input", () => {
    if (rounds[current]?.status === "playing") renderHeadline(rounds[current]);
  });
  document.querySelectorAll("dialog").forEach((d) =>
    d.addEventListener("click", (e) => e.target === d && d.close())
  );

  // The first visit of the day may build the puzzle from the feeds.
  const loading = setTimeout(() => ($("loading").hidden = false), 300);
  try {
    await resync();
  } catch (err) {
    $("dateline").textContent = "Couldn't load today's headlines";
    toast(err.message, 4000);
    return;
  } finally {
    clearTimeout(loading);
    $("loading").hidden = true;
  }

  const longDate = new Date(`${today}T12:00:00`).toLocaleDateString(undefined, {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
  });
  $("dateline").textContent = `No. ${meta.number} · ${longDate}`;

  if (!prefs.get("latestle:seen-help")) {
    $("help-dialog").showModal();
    prefs.set("latestle:seen-help", "1");
  }
}

init();
