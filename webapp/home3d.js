/* Animated Home layer: pseudo-3D blockchain network background (canvas 2D), live "Users Online" counter,
   AI agent scanning widget and quick source buttons. Self-contained: it never edits the other modules'
   state. It reads the existing Home DOM (#ag-title, #ag-dot, #find-sources, #find-go) and drives the
   existing Find Jobs handler by toggling the existing source chips and clicking #find-go.
   All text is set with textContent. No external libraries. */
(() => {
  const W = window.W3 || (window.W3 = {});
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const mk = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  const home = $("#view-home");
  const root = $("#h3d-root");
  if (!home || !root) return;

  const reduceMQ = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  const H = (window.H3D = { frames: 0, running: false, quality: 1, nodes: 0, presence: null });

  /* ======================================================== header block (DOM) */
  const head = mk("div", "h3d-head");
  // presence
  const pres = mk("div", "h3d-glass h3d-presence");
  const pRow = mk("div", "h3d-prow");
  const pDot = mk("span", "h3d-live"); pDot.setAttribute("aria-hidden", "true");
  const pTxt = mk("div", "h3d-ptxt");
  const pNum = mk("b", "h3d-num", "…");
  pTxt.append(mk("span", "h3d-emoji", "🟢 "), pNum, mk("span", "h3d-plabel", " Users Online"));
  pRow.append(pDot, pTxt);
  const pSub = mk("div", "h3d-psub hidden");
  pres.append(pRow, pSub);
  pres.setAttribute("aria-live", "polite");

  // AI agent scanner
  const scan = mk("div", "h3d-glass h3d-scan");
  const radar = mk("div", "h3d-radar"); radar.setAttribute("aria-hidden", "true");
  radar.append(mk("i", "h3d-ring r1"), mk("i", "h3d-ring r2"), mk("i", "h3d-sweep"), mk("i", "h3d-blip b1"), mk("i", "h3d-blip b2"), mk("i", "h3d-blip b3"));
  const sBody = mk("div", "h3d-sbody");
  const sTop = mk("div", "h3d-stop");
  const sTitle = mk("b", "h3d-stitle", "AI Agent");
  const sTag = mk("span", "h3d-tag", "demo · idle");
  sTop.append(sTitle, sTag);
  const sPhase = mk("div", "h3d-phase", "Searching…");
  const steps = mk("div", "h3d-steps");
  const PH = ["Searching", "Analyzing", "Finding Jobs"];
  const stepEls = PH.map((p) => { const s = mk("span", "h3d-step", p); steps.append(s); return s; });
  const bar = mk("div", "h3d-bar"); const barFill = mk("i"); bar.append(barFill);
  sBody.append(sTop, sPhase, steps, bar);
  scan.append(radar, sBody);

  // source buttons
  const srcs = mk("div", "h3d-glass h3d-srcs");
  const srcRow = mk("div", "h3d-srow");
  const bG = mk("button", "h3d-src g"); bG.append(mk("span", "h3d-ico", "G"), mk("span", null, "Search Google"));
  const bX = mk("button", "h3d-src x"); bX.append(mk("span", "h3d-ico", "𝕏"), mk("span", null, "Search X"));
  bG.type = bX.type = "button";
  srcRow.append(bG, bX);
  const tgChip = mk("button", "h3d-chip admin-only", "✈️ Include Telegram");
  tgChip.type = "button"; tgChip.setAttribute("aria-pressed", "false");
  const srcFoot = mk("div", "h3d-sfoot");
  srcFoot.append(tgChip, mk("span", "h3d-hint", "Uses the custom search below if filled"));
  srcs.append(srcRow, srcFoot);

  // admin presence settings
  const adm = mk("details", "h3d-glass h3d-admin admin-only");
  adm.append(mk("summary", null, "⚙️ Online counter settings (admin)"));
  const mSel = mk("select", "h3d-in"); mSel.id = "h3d-mode";
  [["custom", "Custom label"], ["real", "Real count (last 5 min)"]].forEach(([v, t]) => { const o = mk("option", null, t); o.value = v; mSel.append(o); });
  const dIn = mk("input", "h3d-in"); dIn.type = "text"; dIn.id = "h3d-display"; dIn.maxLength = 24; dIn.placeholder = "e.g. 25,000+";
  const aInfo = mk("p", "muted small h3d-ainfo", "");
  const aSave = mk("button", "btn secondary", "Save counter settings"); aSave.type = "button"; aSave.id = "h3d-save";
  const l1 = mk("label", "label", "Mode"); l1.htmlFor = "h3d-mode";
  const l2 = mk("label", "label", "Label shown in custom mode"); l2.htmlFor = "h3d-display";
  adm.append(l1, mSel, l2, dIn, aInfo, aSave);

  head.append(pres, scan, srcs, adm);
  root.append(head);

  /* ======================================================== presence data + count-up */
  let shown = 0, animT = 0;
  function parseDisplay(s) {
    const m = /^(\D*?)(\d[\d,]*(?:\.\d+)?)(.*)$/.exec(String(s || ""));
    if (!m) return null;
    const num = parseFloat(m[2].replace(/,/g, ""));
    if (!isFinite(num)) return null;
    return { pre: m[1], num, dec: (m[2].split(".")[1] || "").length, commas: m[2].includes(",") || num >= 1000, post: m[3] };
  }
  function fmt(p, v) {
    const s = p.dec ? v.toFixed(p.dec) : String(Math.round(v));
    return p.pre + (p.commas ? Number(s).toLocaleString("en-US", { minimumFractionDigits: p.dec, maximumFractionDigits: p.dec }) : s) + p.post;
  }
  function setCount(display) {
    const p = parseDisplay(display);
    cancelAnimationFrame(animT);
    if (!p) { pNum.textContent = display; return; }
    const from = shown, to = p.num;
    if (reduceMQ.matches || from === to) { shown = to; pNum.textContent = fmt(p, to); return; }
    const t0 = performance.now(), dur = from === 0 ? 1400 : 700;
    pres.classList.add("bump");
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      shown = from + (to - from) * e;
      pNum.textContent = fmt(p, shown);
      if (k < 1) animT = requestAnimationFrame(step); else { shown = to; pres.classList.remove("bump"); }
    };
    animT = requestAnimationFrame(step);
  }
  async function loadPresence() {
    if (!W.api) return;
    try {
      const d = await W.api("/api/presence");
      H.presence = d;
      setCount(d.display);
      const isAdm = typeof d.online_now === "number" && d.mode === "custom";
      pSub.classList.toggle("hidden", !isAdm);
      if (isAdm) pSub.textContent = `Admin view · real: ${d.online_now} online now · ${d.active_today} active today`;
    } catch (_) { /* offline / auth gate handled by app.js */ }
  }
  let pollT = null;
  function startPoll() { if (pollT) return; loadPresence(); pollT = setInterval(() => { if (visible()) loadPresence(); }, 30000); }
  function stopPoll() { clearInterval(pollT); pollT = null; }

  async function loadAdmin() {
    if (!document.body.classList.contains("is-admin") || !W.api) return;
    try {
      const d = await W.api("/api/admin/presence-settings");
      mSel.value = d.mode; dIn.value = d.display;
      aInfo.textContent = `Real right now: ${d.online_now} online (last 5 min) · ${d.active_today} active today (UTC).`;
    } catch (_) {}
  }
  adm.addEventListener("toggle", () => { if (adm.open) loadAdmin(); });
  aSave.onclick = async () => {
    if (aSave.disabled) return; aSave.disabled = true;
    try {
      await W.api("/api/admin/presence-settings", { method: "PUT", body: JSON.stringify({ mode: mSel.value, display: dIn.value.trim() }) });
      W.toast && W.toast("Counter updated ✅");
      await Promise.all([loadAdmin(), loadPresence()]);
    } catch (e) { if (e.message !== "auth") W.toast && W.toast(e.message || "Could not save"); }
    finally { aSave.disabled = false; }
  };

  /* ======================================================== AI agent widget (mirrors the real status) */
  let phase = 0, phaseT = null, agentMode = "idle";
  function readAgent() {
    const t = ($("#ag-title") || {}).textContent || "";
    const dot = ($("#ag-dot") || {}).className || "";
    if (/stopped/i.test(t)) return "stopped";
    if (/Searching/i.test(t) || /\bwait\b/.test(dot)) return "running";
    return "idle";
  }
  function renderAgent() {
    agentMode = readAgent();
    scan.dataset.mode = agentMode;
    sTag.textContent = agentMode === "running" ? "● live · agent running" : agentMode === "stopped" ? "agent stopped" : "demo · idle";
    if (agentMode === "stopped") { sPhase.textContent = "Paused"; stepEls.forEach((s) => s.classList.remove("on", "done")); }
  }
  function tickPhase() {
    if (agentMode === "stopped") return;
    phase = (phase + 1) % PH.length;
    sPhase.textContent = PH[phase] + "…";
    stepEls.forEach((s, i) => { s.classList.toggle("on", i === phase); s.classList.toggle("done", i < phase); });
    barFill.style.width = `${Math.round(((phase + 1) / PH.length) * 100)}%`;
  }
  function startPhases() {
    if (phaseT) return;
    const ms = () => (agentMode === "running" ? 1500 : 2600);
    const loop = () => { tickPhase(); phaseT = setTimeout(loop, ms()); };
    phaseT = setTimeout(loop, 400);
  }
  function stopPhases() { clearTimeout(phaseT); phaseT = null; }
  const agTitle = $("#ag-title"), agDot = $("#ag-dot");
  const mo = new MutationObserver(renderAgent);
  if (agTitle) mo.observe(agTitle, { childList: true, characterData: true, subtree: true });
  if (agDot) mo.observe(agDot, { attributes: true, attributeFilter: ["class"] });
  renderAgent(); tickPhase();

  /* ======================================================== source buttons -> existing Find Jobs */
  let tgOn = false;
  tgChip.onclick = () => { tgOn = !tgOn; tgChip.classList.toggle("on", tgOn); tgChip.setAttribute("aria-pressed", String(tgOn)); };
  function runSearch(src) {
    const go = $("#find-go");
    const chips = $$("#find-sources .chip");
    if (!go || !chips.length) return;
    if (go.disabled) { W.toast && W.toast("A search is already starting…"); return; }
    const before = chips.map((c) => c.classList.contains("on"));
    const want = new Set([src]);
    if (tgOn && document.body.classList.contains("is-admin")) want.add("telegram");
    chips.forEach((c) => c.classList.toggle("on", want.has(c.dataset.s)));
    // agent.js reads the chips synchronously inside its click handler, so restoring right after is safe
    try { go.click(); } finally { chips.forEach((c, i) => c.classList.toggle("on", before[i])); }
    scan.classList.add("kick"); setTimeout(() => scan.classList.remove("kick"), 900);
  }
  bG.onclick = () => runSearch("google");
  bX.onclick = () => runSearch("x");

  /* ======================================================== canvas background */
  const cv = mk("canvas", "h3d-bg"); cv.setAttribute("aria-hidden", "true");
  document.body.insertBefore(cv, document.body.firstChild);
  const ctx = cv.getContext("2d", { alpha: false });
  let Wd = 0, Ht = 0, dpr = 1, nodes = [], edges = [], cubes = [], pulses = [], parts = [], bgImg = null, glow = null, glowG = null;
  let rot = 0, tilt = 0.32, raf = 0, last = 0, slowFrames = 0, ftAvg = 16;

  function sprite(r, inner, outer) {
    const c = document.createElement("canvas"); c.width = c.height = r * 2;
    const g = c.getContext("2d"), gr = g.createRadialGradient(r, r, 0, r, r, r);
    gr.addColorStop(0, inner); gr.addColorStop(0.25, outer); gr.addColorStop(1, "rgba(0,0,0,0)");
    g.fillStyle = gr; g.fillRect(0, 0, r * 2, r * 2); return c;
  }
  function buildBg() {
    const c = document.createElement("canvas"); c.width = Math.max(1, Wd * dpr); c.height = Math.max(1, Ht * dpr);
    const g = c.getContext("2d");
    g.fillStyle = "#050b1f"; g.fillRect(0, 0, c.width, c.height);
    let gr = g.createRadialGradient(c.width * 0.5, c.height * 0.3, 0, c.width * 0.5, c.height * 0.3, Math.max(c.width, c.height) * 0.75);
    gr.addColorStop(0, "rgba(30,90,200,.28)"); gr.addColorStop(0.5, "rgba(10,40,110,.12)"); gr.addColorStop(1, "rgba(0,0,0,0)");
    g.fillStyle = gr; g.fillRect(0, 0, c.width, c.height);
    gr = g.createRadialGradient(c.width * 0.9, c.height * 0.85, 0, c.width * 0.9, c.height * 0.85, c.width * 0.8);
    gr.addColorStop(0, "rgba(34,211,238,.10)"); gr.addColorStop(1, "rgba(0,0,0,0)");
    g.fillStyle = gr; g.fillRect(0, 0, c.width, c.height);
    // faint perspective grid at the bottom
    g.strokeStyle = "rgba(61,139,255,.07)"; g.lineWidth = dpr;
    const hy = c.height * 0.72;
    for (let i = -12; i <= 12; i++) { g.beginPath(); g.moveTo(c.width / 2 + i * c.width * 0.02, hy); g.lineTo(c.width / 2 + i * c.width * 0.22, c.height); g.stroke(); }
    for (let k = 1; k < 9; k++) { const y = hy + (c.height - hy) * Math.pow(k / 9, 1.8); g.beginPath(); g.moveTo(0, y); g.lineTo(c.width, y); g.stroke(); }
    bgImg = c;
  }
  function build() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    Wd = window.innerWidth; Ht = window.innerHeight;
    cv.width = Math.round(Wd * dpr); cv.height = Math.round(Ht * dpr);
    cv.style.width = Wd + "px"; cv.style.height = Ht + "px";
    const rawDpr = window.devicePixelRatio || 1;
    const area = Wd * Ht;
    let n = area / 7000 * (rawDpr >= 3 ? 0.7 : rawDpr >= 2 ? 0.85 : 1) * H.quality;
    n = Math.round(Math.max(28, Math.min(120, n)));
    H.nodes = n;
    const R = Math.min(Wd, Ht) * 0.62;
    nodes = [];
    for (let i = 0; i < n; i++) {
      // fibonacci-ish shell with jitter: even spread, organic look
      const y = 1 - (i / (n - 1)) * 2, rr = Math.sqrt(1 - y * y), th = i * 2.399963 + Math.random() * 0.4;
      const s = R * (0.55 + Math.random() * 0.5);
      nodes.push({ x: Math.cos(th) * rr * s * 1.15, y: y * s * 0.85, z: Math.sin(th) * rr * s, sx: 0, sy: 0, sc: 0, d: 0,
                   ph: Math.random() * 6.28, block: i % 9 === 0 });
    }
    edges = []; const seen = new Set();
    for (let i = 0; i < n; i++) {
      const a = nodes[i];
      const near = nodes.map((b, j) => [j, (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2]).filter((v) => v[0] !== i)
        .sort((p, q) => p[1] - q[1]).slice(0, 3);
      near.forEach(([j]) => { const k = i < j ? i + "-" + j : j + "-" + i; if (!seen.has(k)) { seen.add(k); edges.push([Math.min(i, j), Math.max(i, j)]); } });
    }
    cubes = nodes.filter((p) => p.block);
    pulses = [];
    const pc = Math.round(Math.max(30, Math.min(110, area / 6000)) * H.quality);
    parts = Array.from({ length: pc }, () => newPart(true));
    glow = sprite(Math.round(24 * dpr), "rgba(200,245,255,1)", "rgba(34,211,238,.55)");
    glowG = sprite(Math.round(24 * dpr), "rgba(255,255,255,1)", "rgba(61,139,255,.6)");
    buildBg();
  }
  function newPart(any) {
    return { x: Math.random() * Wd, y: any ? Math.random() * Ht : Ht + 10, v: 0.15 + Math.random() * 0.55,
             dx: (Math.random() - 0.5) * 0.25, r: Math.random() < 0.15 ? 1.6 : 0.9, a: 0.25 + Math.random() * 0.55 };
  }
  function project(t) {
    const cy = Math.cos(rot), sy = Math.sin(rot), cx = Math.cos(tilt), sx = Math.sin(tilt);
    const f = Math.min(Wd, Ht) * 1.25, ox = Wd / 2, oy = Ht * 0.46;
    for (const p of nodes) {
      const x1 = p.x * cy + p.z * sy, z1 = -p.x * sy + p.z * cy;
      const y1 = p.y * cx - z1 * sx, z2 = p.y * sx + z1 * cx;
      const sc = f / (f + z2 + f * 0.55);
      p.sx = ox + x1 * sc; p.sy = oy + y1 * sc; p.sc = sc; p.d = Math.max(0, Math.min(1, (sc - 0.45) / 0.75));
    }
  }
  function drawCube(p, t) {
    const s = 7 * p.sc * dpr, a = t * 0.0011 + p.ph, b = t * 0.0007 + p.ph;
    const ca = Math.cos(a), sa = Math.sin(a), cb = Math.cos(b), sb = Math.sin(b);
    const V = [[-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1],[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]].map(([x, y, z]) => {
      const x1 = x * ca - z * sa, z1 = x * sa + z * ca, y1 = y * cb - z1 * sb;
      return [p.sx * dpr + x1 * s, p.sy * dpr + y1 * s];
    });
    ctx.strokeStyle = `rgba(110,200,255,${0.35 + p.d * 0.55})`; ctx.lineWidth = Math.max(1, dpr * 0.9);
    ctx.fillStyle = `rgba(34,140,255,${0.06 + p.d * 0.12})`;
    ctx.beginPath(); ctx.moveTo(V[4][0], V[4][1]); [5, 6, 7].forEach((i) => ctx.lineTo(V[i][0], V[i][1])); ctx.closePath(); ctx.fill();
    ctx.beginPath();
    [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]].forEach(([i, j]) => { ctx.moveTo(V[i][0], V[i][1]); ctx.lineTo(V[j][0], V[j][1]); });
    ctx.stroke();
  }
  function frame(t) {
    const dt = last ? Math.min(50, t - last) : 16; last = t;
    const slow = reduceMQ.matches;
    rot += dt * (slow ? 0 : 0.00012); tilt = 0.32 + Math.sin(t * 0.00013) * (slow ? 0 : 0.08);
    project(t);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(bgImg, 0, 0);
    // particles (flowing upward, blue/cyan)
    for (const q of parts) {
      if (!slow) { q.y -= q.v * dt * 0.06; q.x += q.dx * dt * 0.06; }
      if (q.y < -10 || q.x < -10 || q.x > Wd + 10) Object.assign(q, newPart(false));
      ctx.fillStyle = q.r > 1 ? `rgba(120,230,255,${q.a})` : `rgba(80,150,255,${q.a})`;
      const r = q.r * dpr; ctx.fillRect(q.x * dpr - r, q.y * dpr - r, r * 2, r * 2);
    }
    // edges
    ctx.lineWidth = Math.max(1, dpr * 0.7);
    for (let i = 0; i < 4; i++) {        // 4 depth buckets = 4 strokes instead of one per edge
      ctx.strokeStyle = `rgba(61,160,255,${0.06 + i * 0.07})`;
      ctx.beginPath();
      for (const [a, b] of edges) {
        const A = nodes[a], B = nodes[b], dd = (A.d + B.d) / 2;
        if (Math.min(3, Math.floor(dd * 4)) !== i) continue;
        ctx.moveTo(A.sx * dpr, A.sy * dpr); ctx.lineTo(B.sx * dpr, B.sy * dpr);
      }
      ctx.stroke();
    }
    // transaction pulses
    if (!slow && pulses.length < 4 + edges.length / 12 && Math.random() < dt / 140) {
      pulses.push({ e: edges[(Math.random() * edges.length) | 0], t: 0, v: 0.0006 + Math.random() * 0.0008, rev: Math.random() < 0.5 });
    }
    ctx.globalCompositeOperation = "lighter";
    for (let i = pulses.length - 1; i >= 0; i--) {
      const P = pulses[i]; P.t += P.v * dt;
      if (P.t >= 1) { pulses.splice(i, 1); continue; }
      const A = nodes[P.e[P.rev ? 1 : 0]], B = nodes[P.e[P.rev ? 0 : 1]], k = P.t;
      const x = (A.sx + (B.sx - A.sx) * k) * dpr, y = (A.sy + (B.sy - A.sy) * k) * dpr;
      const tx = (A.sx + (B.sx - A.sx) * Math.max(0, k - 0.18)) * dpr, ty = (A.sy + (B.sy - A.sy) * Math.max(0, k - 0.18)) * dpr;
      const g = ctx.createLinearGradient(tx, ty, x, y); g.addColorStop(0, "rgba(34,211,238,0)"); g.addColorStop(1, "rgba(120,240,255,.9)");
      ctx.strokeStyle = g; ctx.lineWidth = 2 * dpr; ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(x, y); ctx.stroke();
      const s = 18 * dpr * (0.6 + A.sc * 0.5); ctx.drawImage(glow, x - s / 2, y - s / 2, s, s);
    }
    // nodes (glow sprites, additive)
    for (const p of nodes) {
      const tw = 0.75 + 0.25 * Math.sin(t * 0.002 + p.ph);
      const s = (p.block ? 26 : 16) * dpr * p.sc * (0.6 + p.d * 0.6) * tw;
      ctx.globalAlpha = 0.35 + p.d * 0.65;
      ctx.drawImage(p.block ? glowG : glow, p.sx * dpr - s / 2, p.sy * dpr - s / 2, s, s);
    }
    ctx.globalAlpha = 1; ctx.globalCompositeOperation = "source-over";
    for (const p of cubes) drawCube(p, slow ? 0 : t);
    H.frames++;
    // adaptive quality: if frames are slow for ~2s, rebuild with fewer nodes/particles
    ftAvg = ftAvg * 0.95 + dt * 0.05;
    if (ftAvg > 24) { if (++slowFrames > 120 && H.quality > 0.5) { H.quality = Math.max(0.5, H.quality - 0.2); slowFrames = 0; ftAvg = 16; build(); } }
    else slowFrames = 0;
  }
  function loop(t) { raf = 0; if (!visible()) return stop(); frame(t); if (!reduceMQ.matches) raf = requestAnimationFrame(loop); }
  function start() {
    if (H.running) return; H.running = true; last = 0;
    if (!bgImg) build();
    if (reduceMQ.matches) { frame(performance.now()); return; }   // one static frame
    raf = requestAnimationFrame(loop);
  }
  function stop() { H.running = false; if (raf) cancelAnimationFrame(raf); raf = 0; }

  /* ======================================================== visibility wiring */
  const visible = () => home.classList.contains("active") && !document.hidden;
  function sync() {
    const on = home.classList.contains("active");
    document.body.classList.toggle("h3d-on", on);
    if (visible()) { start(); startPoll(); startPhases(); renderAgent(); }
    else { stop(); stopPoll(); stopPhases(); }
  }
  new MutationObserver(sync).observe(home, { attributes: true, attributeFilter: ["class"] });
  document.addEventListener("visibilitychange", sync);
  let rt = 0;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { build(); if (H.running && reduceMQ.matches) frame(performance.now()); }, 150); });
  if (reduceMQ.addEventListener) reduceMQ.addEventListener("change", () => { stop(); sync(); });
  sync();
})();
