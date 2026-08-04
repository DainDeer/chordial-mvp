/* the web focus view frontend.
 *
 * the server owns the clock: /api/today returns banked seconds per task and
 * which task is running. we render that snapshot and tick it forward locally
 * (snapshot seconds + wall time since fetch), so a refresh, a laptop sleep,
 * or a second window all converge on the server's truth. every button is a
 * POST followed by a re-fetch — no client-side state to drift.
 *
 * the bar fills with progress through the CURRENT pomodoro:
 * fill = (elapsed % pomLength) / pomLength. it fills up rather than counting
 * down — finished poms are counted in the readout, not mourned by a timer.
 */

const POLL_MS = 45000;
const state = {
  data: null,          // last /api/today payload
  fetchedAt: null,     // performance.now() at fetch, for local ticking
  lastPomCount: null,  // for rollover celebration
};

const $ = (sel) => document.querySelector(sel);

// --- data -------------------------------------------------------------------

async function refresh() {
  try {
    const res = await fetch("/api/today");
    const body = await res.json();
    if (!res.ok) { showBanner(body.error || "something went wrong"); return; }
    hideBanner();
    state.data = body;
    state.fetchedAt = performance.now();
    render();
  } catch (e) {
    showBanner("can't reach chordial — is the server running?");
  }
}

async function post(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : "{}",
    });
    const data = await res.json();
    if (!res.ok) { toast(data.error || "that didn't work"); return null; }
    return data;
  } catch (e) {
    toast("can't reach chordial");
    return null;
  } finally {
    await refresh();
  }
}

// --- derived clock ------------------------------------------------------------

function activeTaskId() {
  return state.data?.focus?.active_task_id ?? null;
}

function taskSeconds(taskId) {
  const base = state.data?.focus?.seconds?.[String(taskId)] ?? 0;
  if (taskId === activeTaskId() && state.fetchedAt !== null) {
    return base + (performance.now() - state.fetchedAt) / 1000;
  }
  return base;
}

function allTasks() {
  const b = state.data?.buckets;
  return b ? [...b.overdue, ...b.today, ...b.in_progress] : [];
}

function findTask(id) {
  return allTasks().find((t) => t.id === id) || null;
}

// --- rendering ----------------------------------------------------------------

function render() {
  const d = state.data;
  if (!d) return;

  const name = d.user?.name;
  $("#greeting").textContent = name ? `hi ${name} · today` : "today";
  $("#date-line").textContent = prettyDate(d.today);

  for (const key of ["overdue", "today", "in_progress"]) {
    const section = $(`#bucket-${key}`);
    const list = section.querySelector(".tasks");
    const rows = d.buckets[key];
    list.replaceChildren(...rows.map(taskCard));
    // overdue + in_progress hide when empty; today shows its empty state
    if (key === "today") {
      section.querySelector(".empty").classList.toggle("hidden", rows.length > 0);
    } else {
      section.classList.toggle("hidden", rows.length === 0);
    }
  }

  renderDayTotal();
  renderBar();
}

function taskCard(t) {
  const li = document.createElement("li");
  li.className = "task";
  const isActive = t.id === activeTaskId();
  const secs = taskSeconds(t.id);
  if (isActive) li.classList.add("active");
  else if (secs > 0) li.classList.add("paused-with-time");

  const body = document.createElement("div");
  body.className = "body";
  const title = document.createElement("div");
  title.className = "title";
  title.textContent = t.title;
  const meta = document.createElement("div");
  meta.className = "meta";
  const chips = [];
  if (t.plan_title) chips.push(chip(t.plan_title));
  if (t.window && t.window !== "anytime") chips.push(chip(t.window));
  if (t.priority === "high") chips.push(chip("high priority", "chip-prio-high"));
  if (t.pom_estimate) chips.push(chip(`~${trimNum(t.pom_estimate)} pom${t.pom_estimate === 1 ? "" : "s"}`));
  if (t.scheduled && t.scheduled < state.data.today) chips.push(chip(`was ${prettyDate(t.scheduled)}`));
  if (secs >= 60) chips.push(chip(`${Math.floor(secs / 60)}m focused`, "chip-elapsed"));
  meta.append(...chips);
  body.append(title, meta);

  const actions = document.createElement("div");
  actions.className = "actions";
  actions.append(
    iconButton(isActive ? "⏸" : "▶", "play",
      isActive ? "pause" : (secs > 0 ? "resume" : "start"),
      () => isActive ? post("/api/focus/pause") : post("/api/focus/start", { task_id: t.id })),
    iconButton("✓", "done", "mark complete", async () => {
      const r = await post(`/api/tasks/${t.id}/status`, { status: "done" });
      if (r) toast(`"${t.title}" — done ✨`);
    }),
    iconButton("✕", "skip", "won't do", async () => {
      const r = await post(`/api/tasks/${t.id}/status`, { status: "deprioritized" });
      if (r) toast(`"${t.title}" — let go 🍂`);
    }),
  );

  li.append(body, actions);
  return li;
}

function chip(text, cls) {
  const s = document.createElement("span");
  if (cls) s.className = cls;
  s.textContent = text;
  return s;
}

function iconButton(glyph, cls, label, onClick) {
  const b = document.createElement("button");
  b.className = `icon-btn ${cls}`;
  b.textContent = glyph;
  b.title = label;
  b.addEventListener("click", onClick);
  return b;
}

// theme-appropriate emoji (🔥 vs 🌊 vs 🌱); falls back if themes.js is absent
function themeIcons() {
  return window.TrackViz ? TrackViz.icons()
    : { runIcon: "🔥", idleIcon: "🍅", dayIcon: "🔥" };
}

function renderDayTotal() {
  const secondsMap = state.data?.focus?.seconds || {};
  // taskSeconds adds the live delta only for the active task
  const total = Object.keys(secondsMap).reduce((acc, id) => acc + taskSeconds(Number(id)), 0);
  const el = $("#day-total");
  el.textContent = total >= 60 ? `${themeIcons().dayIcon} ${Math.floor(total / 3600) ? `${Math.floor(total / 3600)}h ` : ""}${Math.floor((total % 3600) / 60)}m focused` : "";
}

function renderBar() {
  const bar = $("#pom-bar");
  const id = activeTaskId();
  const pomSecs = (state.data?.pom_minutes || 25) * 60;
  const toggle = $("#pom-toggle");

  // is there a "current" task at all? active wins; else the last task with
  // banked time stays on the bar, paused, so resume is one click away
  let current = id !== null ? findTask(id) : null;
  let running = id !== null;
  if (!current) {
    const withTime = allTasks().filter((t) => taskSeconds(t.id) > 0);
    if (withTime.length) {
      current = withTime.reduce((a, b) => taskSeconds(a.id) > taskSeconds(b.id) ? a : b);
    }
    running = false;
  }

  if (!current) {
    bar.classList.add("idle");
    bar.classList.remove("paused", "complete");
    $("#pom-task-title").textContent = "pick a task to begin";
    $("#pom-state-icon").textContent = themeIcons().idleIcon;
    $("#pom-elapsed").textContent = "–";
    $("#pom-sub").textContent = "";
    toggle.classList.add("hidden");
    $("#fill").style.width = "0%";
    if (window.TrackViz) TrackViz.set({ frac: 0, running: false, idle: true, complete: false });
    return;
  }

  const secs = taskSeconds(current.id);
  const inPom = secs % pomSecs;
  const pomsDone = Math.floor(secs / pomSecs);
  const frac = inPom / pomSecs;
  const complete = running && frac > 0.995;

  bar.classList.remove("idle");
  bar.classList.toggle("paused", !running);
  bar.classList.toggle("complete", complete);
  if (window.TrackViz) TrackViz.set({ frac, running, idle: false, complete });

  $("#pom-task-title").textContent = current.title;
  $("#pom-state-icon").textContent = running ? themeIcons().runIcon : "⏸";
  $("#pom-elapsed").textContent = mmss(inPom);
  const subBits = [`of ${state.data.pom_minutes}:00`];
  if (pomsDone > 0) subBits.push(`· ${pomsDone} pom${pomsDone === 1 ? "" : "s"} done`);
  if (current.pom_estimate) subBits.push(`· est ${trimNum(current.pom_estimate)}`);
  $("#pom-sub").textContent = subBits.join(" ");

  toggle.classList.remove("hidden");
  toggle.textContent = running ? "pause" : "resume";
  toggle.onclick = () =>
    running ? post("/api/focus/pause") : post("/api/focus/start", { task_id: current.id });

  $("#fill").style.width = `${Math.min(100, frac * 100)}%`;

  // pom rollover: celebrate once when the count ticks up while running
  if (running && state.lastPomCount !== null && pomsDone > state.lastPomCount) {
    toast(`pomodoro complete on "${current.title}" 🍅✨`);
  }
  state.lastPomCount = running ? pomsDone : null;
}

// --- little helpers ------------------------------------------------------------

function mmss(seconds) {
  const s = Math.floor(seconds);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function trimNum(n) {
  return Number.isInteger(n) ? String(n) : String(n).replace(/\.0$/, "");
}

const MONTHS = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"];
function prettyDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  const days = ["sunday","monday","tuesday","wednesday","thursday","friday","saturday"];
  return `${days[date.getDay()]}, ${MONTHS[m - 1]} ${d}`;
}

let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

function showBanner(msg) {
  $("#banner").textContent = msg;
  $("#banner").classList.remove("hidden");
}
function hideBanner() {
  $("#banner").classList.add("hidden");
}

// --- boot -----------------------------------------------------------------------

refresh();
setInterval(refresh, POLL_MS);
setInterval(() => { if (state.data) { renderBar(); renderDayTotal(); } }, 500);
// re-sync promptly when the window comes back (laptop wake, tab switch)
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
