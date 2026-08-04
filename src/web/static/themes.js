/* themes + the track visualization.
 *
 * a theme is a palette (css vars, scoped by [data-theme] on <html>) plus an
 * optional canvas painter for the pomodoro track. ember keeps the plain css
 * .fill bar; wave and grove paint into #track-viz every frame.
 *
 * the animation clock (t) only advances while a pomodoro is running, so
 * pausing freezes the wave mid-oscillation and the vine mid-sway — the
 * picture *is* the state. app.js feeds progress via TrackViz.set() and asks
 * TrackViz.icons() for the theme's emoji set.
 */

(function () {
  // inside the portfolio's focus.exe window: the titlebar is our chrome
  if (window.self !== window.top) document.body.classList.add("embed");

  const THEMES = {
    ember: { emoji: "🔥", label: "ember — warm hearth",
             runIcon: "🔥", idleIcon: "🍅", dayIcon: "🔥", paint: null },
    wave:  { emoji: "🌊", label: "wave — oscilloscope",
             runIcon: "🌊", idleIcon: "🍅", dayIcon: "🌀", paint: paintWave },
    grove: { emoji: "🌿", label: "grove — growing things",
             runIcon: "🌱", idleIcon: "🍅", dayIcon: "🌿", paint: paintGrove },
  };
  const STORE_KEY = "focus-theme";

  const canvas = document.getElementById("track-viz");
  const ctx = canvas ? canvas.getContext("2d") : null;
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const prog = { frac: 0, running: false, idle: true, complete: false };
  let current = null;
  // storage can throw entirely in privacy-restricted iframes, not just on write
  try { current = localStorage.getItem(STORE_KEY); } catch (e) { /* embed */ }
  if (!THEMES[current]) current = "ember";
  let pal = {};          // palette snapshot, re-read on theme change
  let t = 0;             // animation clock (seconds); frozen while paused
  let last = null;

  function readPalette() {
    const s = getComputedStyle(document.documentElement);
    const v = (name) => s.getPropertyValue(name).trim();
    pal = {
      accent: v("--accent"), soft: v("--accent-soft"), glow: v("--accent-glow"),
      gold: v("--gold"), sage: v("--sage"), rust: v("--rust"),
      dim: v("--text-dim"), faint: v("--text-faint"), card: v("--bg-card"),
    };
  }

  function apply(name) {
    current = name;
    if (name === "ember") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = name;
    try { localStorage.setItem(STORE_KEY, name); } catch (e) { /* private mode */ }
    document.querySelectorAll(".theme-btn").forEach((b) =>
      b.classList.toggle("on", b.dataset.theme === name));
    readPalette();
    resize();
  }

  function resize() {
    if (!canvas || !canvas.offsetParent) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // --- the wave painter --------------------------------------------------
  // a sine chord oscillates across the whole track, but only the stretch the
  // pomodoro has reached is revealed; ahead of the frontier lies a faint
  // guide line, so the hidden wave has somewhere to arrive.

  function waveY(x, mid, amp) {
    return mid
      + Math.sin(x * 0.045 - t * 2.4) * amp * 0.58
      + Math.sin(x * 0.013 + t * 0.7) * amp * 0.42;
  }

  function paintWave(w, h) {
    const mid = h / 2;
    const amp = h * 0.30;
    const edge = prog.idle ? 0 : prog.frac * w;

    // the road ahead: a whisper of a baseline
    ctx.strokeStyle = pal.faint;
    ctx.globalAlpha = 0.35;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(edge, mid);
    ctx.lineTo(w, mid);
    ctx.stroke();
    ctx.globalAlpha = 1;
    if (edge <= 0) return;

    ctx.globalAlpha = prog.running ? 1 : 0.45;

    // the revealed wave
    const grad = ctx.createLinearGradient(0, 0, edge, 0);
    grad.addColorStop(0, pal.soft);
    grad.addColorStop(1, pal.accent);
    ctx.strokeStyle = grad;
    ctx.lineWidth = 2.2;
    ctx.lineJoin = "round";
    ctx.shadowColor = pal.glow;
    ctx.shadowBlur = prog.complete ? 10 + Math.sin(t * 5) * 5 : 7;
    ctx.beginPath();
    for (let x = 0; x <= edge; x += 2) {
      const y = waveY(x, mid, amp);
      x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.shadowBlur = 0;

    // the frontier: a bright bead riding the leading edge
    const by = waveY(edge, mid, amp);
    const halo = ctx.createRadialGradient(edge, by, 0, edge, by, 8);
    halo.addColorStop(0, pal.soft);
    halo.addColorStop(1, "transparent");
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(edge, by, 8, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = pal.accent;
    ctx.beginPath();
    ctx.arc(edge, by, 2.6, 0, Math.PI * 2);
    ctx.fill();

    ctx.globalAlpha = 1;
  }

  // --- the grove painter -------------------------------------------------
  // a vine grows along the track, unfurling a leaf at each waypoint as the
  // frontier passes it; faint seeds mark the leaves still to come, and a
  // flower blooms when the pomodoro completes.

  const LEAF_GAP = 40;      // px between leaves
  const LEAF_GROW = 26;     // px of frontier travel it takes a leaf to unfurl

  function stemY(x, mid, h) {
    return mid + Math.sin(x * 0.02 + 1.3) * h * 0.12
               + Math.sin(t * 1.4 + x * 0.05) * 0.8;
  }

  function leaf(x, y, angle, len, color, alpha) {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(angle);
    ctx.globalAlpha *= alpha;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.quadraticCurveTo(len * 0.45, -len * 0.55, len, -len * 0.1);
    ctx.quadraticCurveTo(len * 0.5, len * 0.12, 0, 0);
    ctx.fill();
    ctx.restore();
  }

  function flower(x, y, r, pulse) {
    for (let i = 0; i < 5; i++) {
      const a = (i / 5) * Math.PI * 2 + t * 0.4;
      ctx.fillStyle = pal.gold;
      ctx.beginPath();
      ctx.arc(x + Math.cos(a) * r * pulse, y + Math.sin(a) * r * pulse,
              r * 0.62, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.fillStyle = pal.soft;
    ctx.beginPath();
    ctx.arc(x, y, r * 0.5, 0, Math.PI * 2);
    ctx.fill();
  }

  function paintGrove(w, h) {
    const mid = h / 2;
    const edge = prog.idle ? 0 : prog.frac * w;

    // seeds not yet reached: the promise of the rest of the pomodoro
    ctx.fillStyle = pal.faint;
    ctx.globalAlpha = 0.4;
    for (let lx = LEAF_GAP; lx < w - 12; lx += LEAF_GAP) {
      if (lx <= edge) continue;
      ctx.beginPath();
      ctx.arc(lx, stemY(lx, mid, h), 1.4, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (edge <= 0) return;

    ctx.globalAlpha = prog.running ? 1 : 0.5;

    // the vine itself
    const grad = ctx.createLinearGradient(0, 0, Math.max(edge, 1), 0);
    grad.addColorStop(0, pal.sage);
    grad.addColorStop(1, pal.accent);
    ctx.strokeStyle = grad;
    ctx.lineWidth = 2.4;
    ctx.lineCap = "round";
    ctx.beginPath();
    for (let x = 0; x <= edge; x += 2) {
      const y = stemY(x, mid, h);
      x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();

    // leaves unfurl as the frontier passes their waypoint
    let side = -1;
    for (let lx = LEAF_GAP; lx < w - 12; lx += LEAF_GAP) {
      side = -side;
      if (lx > edge) continue;
      const grow = Math.min(1, (edge - lx) / LEAF_GROW);
      const ease = 1 - Math.pow(1 - grow, 3);
      const y = stemY(lx, mid, h);
      const sway = Math.sin(t * 1.8 + lx) * 0.1;
      const len = (6 + h * 0.24) * ease;
      const base = side < 0 ? -2.2 : 2.2;
      leaf(lx, y, base + sway, len, side < 0 ? pal.accent : pal.sage, 0.9);
    }

    // the growing tip: a bright sprout — or, at the top of the pom, a bloom
    const ty = stemY(edge, mid, h);
    if (prog.complete) {
      flower(edge - 2, ty, 5.5, 1 + Math.sin(t * 5) * 0.12);
    } else {
      leaf(edge, ty, -2.0 + Math.sin(t * 2.2) * 0.15, 6, pal.soft, 0.95);
      leaf(edge, ty, 2.4 + Math.sin(t * 2.2) * 0.15, 5, pal.soft, 0.95);
      ctx.fillStyle = pal.accent;
      ctx.beginPath();
      ctx.arc(edge, ty, 2, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.globalAlpha = 1;
  }

  // --- the frame loop ----------------------------------------------------

  function drawOnce() {
    const theme = THEMES[current];
    if (!ctx || !theme.paint) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    // re-sync the backing store whenever layout moved under us (first paint,
    // container resize, dpr change)
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(w * dpr)) resize();
    ctx.clearRect(0, 0, w, h);
    theme.paint(w, h);
  }

  function frame(now) {
    requestAnimationFrame(frame);
    if (document.hidden) { last = now; return; }
    const dt = last === null ? 0 : Math.min((now - last) / 1000, 0.1);
    last = now;
    if (prog.running && !reduceMotion) t += dt;
    drawOnce();
  }

  // --- boot --------------------------------------------------------------

  const picker = document.getElementById("theme-picker");
  if (picker) {
    for (const [name, theme] of Object.entries(THEMES)) {
      const b = document.createElement("button");
      b.className = "theme-btn";
      b.dataset.theme = name;
      b.textContent = theme.emoji;
      b.title = theme.label;
      b.setAttribute("aria-label", theme.label);
      b.addEventListener("click", () => apply(name));
      picker.appendChild(b);
    }
  }

  apply(current);
  window.addEventListener("resize", resize);
  requestAnimationFrame(frame);

  window.TrackViz = {
    set(next) { Object.assign(prog, next); drawOnce(); },
    icons() { return THEMES[current]; },
    // manual clock: lets tests (and rAF-less headless browsers) advance the
    // animation deterministically
    step(dt) { t += dt || 0; drawOnce(); },
  };
})();
