/* Full automation controls on the owner's Home (admins only). The scheduler runs on the server, so this screen
   only shows status (polled every 5 s while visible) and sends Start / Pause / Stop / Run now.
   Every call is re-checked on the server (initData HMAC + admin id). textContent only, no secrets here. */
(() => {
  const W = window.W3 || (window.W3 = {});
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const el = (...a) => W.el(...a);
  const POLL_MS = 5000;
  let inited = false, timer = null, last = null, formDirty = false;

  function err(e) { if (e && e.message !== "auth") W.toast(e.message || "Something went wrong"); }
  const when = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso), s = Math.round((d.getTime() - Date.now()) / 1000);
    const rel = s >= 0 ? (s >= 60 ? `in ${Math.round(s / 60)} min` : "in <1 min") : (-s >= 60 ? `${Math.round(-s / 60)} min ago` : "just now");
    return `${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} (${rel})`;
  };

  W.initAutomation = async () => {
    if (inited) return; inited = true;
    $("#au-start").onclick = (e) => act(e.target, "start");
    $("#au-pause").onclick = (e) => act(e.target, "pause");
    $("#au-stop").onclick = (e) => act(e.target, "stop");
    $("#au-run").onclick = (e) => act(e.target, "run");
    $("#au-save").onclick = (e) => busy(e.target, save);
    $$("#au-sources .chip").forEach((c) => c.onclick = () => { c.classList.toggle("on"); formDirty = true; });
    ["#au-interval", "#au-reminder", "#au-minrel"].forEach((id) => $(id).oninput = () => { formDirty = true; });
    $("#au-logs-box").addEventListener("toggle", () => { if ($("#au-logs-box").open) loadLogs(); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    // opening the Mini App triggers an immediate search when automation is on (server debounces: 5 min)
    try {
      const r = await W.api("/api/agent/automation/app-open", { method: "POST" });
      render(r);
      if (r.started) W.toast("Automation: searching now 🔄");
    } catch (e) { err(e); }
    schedule();
  };

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const home = $("#view-home");
      if (!document.hidden && home && home.classList.contains("active")) await refresh();
      schedule();
    }, POLL_MS);
  }

  async function busy(btn, fn) {
    if (btn.disabled) return; btn.disabled = true; const t = btn.textContent; btn.textContent = "…";
    try { await fn(); } catch (e) { err(e); } finally { btn.disabled = false; btn.textContent = t; }
  }

  async function act(btn, action) {
    await busy(btn, async () => {
      const r = await W.api(`/api/agent/automation/${action}`, { method: "POST" });
      if (action === "run") W.toast(r.message || "Started");
      else W.toast({ running: "Automation running ✅", paused: "Paused ⏸", stopped: "Stopped ⏹" }[r.state] || "Done");
      render(r);
      setTimeout(refresh, 1200);
    });
  }

  async function refresh() {
    try { render(await W.api("/api/agent/automation")); } catch (e) { if (e.status !== 403) err(e); }
    if ($("#au-logs-box").open) loadLogs();
  }

  function render(a) {
    if (!a || !a.state) return;
    const prev = last;
    last = a;
    // a run finished since the last poll: refresh the dashboard numbers and activity below
    if (prev && (prev.last_run_at !== a.last_run_at || prev.running_now !== a.running_now) && W.reloadHome) W.reloadHome();
    const dot = { running: "on", paused: "wait", stopped: "" }[a.state];
    $("#au-dot").className = "dot " + (a.running_now ? "wait" : dot);
    $("#au-title").textContent = `Full automation · ${{ running: "Running", paused: "Paused", stopped: "Stopped" }[a.state]}` + (a.running_now ? " · searching…" : "");
    const parts = [`Last run ${when(a.last_run_at)}${a.last_run_at ? " · " + a.last_status : ""}`];
    if (a.state === "running") parts.push(`Next run ${when(a.next_run_at)} · every ${a.search_interval_minutes} min`);
    parts.push(`Reminder every ${a.reminder_interval_minutes} min`);
    $("#au-sub").textContent = parts.join(" · ");
    const c = a.counters || {};
    const stats = [["New last run", c.last_new_leads], ["Sent to you", c.leads_notified_total], ["Runs", c.runs_total]];
    $("#au-stats").replaceChildren(...stats.map(([k, v]) => { const d = el("div", "stat"); d.append(el("b", null, String(v ?? 0)), el("span", null, k)); return d; }));
    const errs = [];
    if (a.last_error) errs.push("Last errors: " + a.last_error);
    if (a.notify_error) errs.push("Bot notifications: " + a.notify_error);
    const bo = Object.entries(a.provider_backoff || {});
    if (bo.length) errs.push("Backing off: " + bo.map(([k, v]) => `${k} (retry in ${Math.ceil(v.retry_in_sec / 60)} min)`).join(", "));
    $("#au-err").textContent = errs.join(" · ");
    $("#au-err").classList.toggle("hidden", !errs.length);
    $("#au-start").classList.toggle("hidden", a.state === "running");
    $("#au-pause").classList.toggle("hidden", a.state !== "running");
    $("#au-stop").classList.toggle("hidden", a.state === "stopped");
    $("#au-run").disabled = !!a.running_now;
    if (!formDirty) {
      $("#au-interval").value = a.search_interval_minutes;
      $("#au-reminder").value = a.reminder_interval_minutes;
      $("#au-minrel").value = a.min_relevance;
      $$("#au-sources .chip").forEach((ch) => ch.classList.toggle("on", (a.sources || []).includes(ch.dataset.s)));
    }
  }

  async function save() {
    const int = W.intField;
    const body = {
      search_interval_minutes: int("#au-interval", "Search interval (minutes)", 5, 1440),
      reminder_interval_minutes: int("#au-reminder", "Reminder interval (minutes)", 1, 1440),
      min_relevance: int("#au-minrel", "Minimum match %", 0, 100),
      sources: $$("#au-sources .chip.on").map((c) => c.dataset.s),
    };
    if (!body.sources.length) throw new Error("Pick at least one source.");
    const r = await W.api("/api/agent/automation/settings", { method: "PUT", body: JSON.stringify(body) });
    formDirty = false;
    render(r);
    W.toast("Automation settings saved ✅");
  }

  async function loadLogs() {
    try {
      const d = await W.api("/api/agent/automation/logs?limit=60");
      const rows = d.logs.map((l) => { const r = el("div", "log " + l.level); r.textContent = `${new Date(l.ts).toLocaleString()} · ${l.kind} · ${l.message}`; return r; });
      $("#au-logs").replaceChildren(...(rows.length ? rows : [el("div", "empty", "No automation activity yet.")]));
    } catch (e) { err(e); }
  }
  W.automationState = () => last;
})();
