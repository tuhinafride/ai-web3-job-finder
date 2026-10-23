/* Admin panel. Only initialised when /api/me says is_admin; every /api/admin call is re-checked on the
   server (initData HMAC + admin id), so hiding this tab is cosmetic, not the security boundary. */
(() => {
  const W = window.W3 || (window.W3 = {});
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const el = (...a) => W.el(...a);
  const tg = window.Telegram && window.Telegram.WebApp;
  const PAGE = 20;
  const ust = { q: "", status: "", offset: 0, total: 0 };
  let inited = false;

  function err(e) { if (e && e.message !== "auth") W.toast(e.message || "Something went wrong"); }
  const fmt = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "—");

  W.initAdmin = () => {
    if (inited) return; inited = true;
    $$("#adm-seg .segbtn").forEach((b) => b.onclick = () => {
      $$("#adm-seg .segbtn").forEach((x) => x.classList.remove("on")); b.classList.add("on");
      $$(".admpane").forEach((p) => p.classList.add("hidden")); $(`#adm-${b.dataset.as}`).classList.remove("hidden");
      loadPane(b.dataset.as);
    });
    let deb;
    $("#adm-q").oninput = () => { clearTimeout(deb); deb = setTimeout(() => { ust.q = $("#adm-q").value.trim(); ust.offset = 0; loadUsers(); }, 350); };
    $("#adm-status").onchange = () => { ust.status = $("#adm-status").value; ust.offset = 0; loadUsers(); };
    $("#adm-prev").onclick = () => { ust.offset = Math.max(0, ust.offset - PAGE); loadUsers(); };
    $("#adm-next").onclick = () => { if (ust.offset + PAGE < ust.total) { ust.offset += PAGE; loadUsers(); } };
    $("#adm-csv").onclick = exportCsv;
    $("#rw-save").onclick = saveRewards;
    const prev = W.onView;
    W.onView = (view, ds) => {
      if (prev) prev(view, ds);
      if (view === "admin") { const on = $("#adm-seg .segbtn.on"); loadPane(on ? on.dataset.as : "dash"); }
    };
  };

  function loadPane(p) {
    if (p === "dash") loadDash();
    if (p === "users") loadUsers();
    if (p === "rewards") loadRewards();
    if (p === "audit") loadAudit();
  }

  // ------------------------------------------------ dashboard
  async function loadDash() {
    let d;
    try { d = await W.api("/api/admin/stats"); } catch (e) { return err(e); }
    const items = [["Total users", d.total_users], ["Opened Mini App", d.opened_app], ["DAU", d.dau], ["WAU", d.wau],
      ["MAU", d.mau], ["New today", d.new_today], ["Referral links", d.referral_links],
      ["Referral sign-ups", d.referral_registrations], ["Suspended", d.suspended]];
    $("#adm-stats").replaceChildren(...items.map(([k, v]) => { const c = el("div", "stat"); c.append(el("b", null, String(v)), el("span", null, k)); return c; }));
    drawChart($("#adm-chart"), d.trend);
    const top = $("#adm-top");
    if (!d.top_referrers.length) top.replaceChildren(el("div", "muted small", "No valid referrals yet."));
    else top.replaceChildren(...d.top_referrers.map((x) => {
      const r = el("div", "lbrow");
      r.append(el("span", "rank", `#${x.rank}`), el("span", "name", `${x.name} (${x.id})`), el("span", "val", `${x.valid} · ${x.points} pts`));
      return r;
    }));
  }

  // Simple inline SVG line chart (no external libraries / CDN).
  function drawChart(box, trend) {
    const NS = "http://www.w3.org/2000/svg";
    const w = 340, h = 150, pad = { l: 26, r: 8, t: 10, b: 20 };
    const max = Math.max(1, ...trend.map((t) => Math.max(t.active, t.new, t.referrals)));
    const x = (i) => pad.l + (i * (w - pad.l - pad.r)) / Math.max(1, trend.length - 1);
    const y = (v) => h - pad.b - (v * (h - pad.t - pad.b)) / max;
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`); svg.setAttribute("class", "svgchart"); svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "30 day activity chart");
    const mk = (tag, attrs) => { const n = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); return n; };
    [0, 0.5, 1].forEach((f) => {
      const yy = y(max * f);
      svg.append(mk("line", { x1: pad.l, x2: w - pad.r, y1: yy, y2: yy, class: "grid" }));
      const t = mk("text", { x: 2, y: yy + 3, class: "axis" }); const v = max * f; t.textContent = Number.isInteger(v) ? String(v) : v.toFixed(1); svg.append(t);
    });
    [["active", "a"], ["new", "n"], ["referrals", "r"]].forEach(([key, cls]) => {
      const pts = trend.map((t, i) => `${x(i).toFixed(1)},${y(t[key]).toFixed(1)}`).join(" ");
      svg.append(mk("polyline", { points: pts, class: "ln " + cls }));
    });
    [0, trend.length - 1].forEach((i) => {
      if (!trend[i]) return;
      const t = mk("text", { x: i ? w - pad.r - 30 : pad.l, y: h - 5, class: "axis" }); t.textContent = trend[i].day.slice(5); svg.append(t);
    });
    box.replaceChildren(svg);
  }

  // ------------------------------------------------ users
  async function loadUsers() {
    const p = new URLSearchParams({ q: ust.q, status: ust.status, offset: ust.offset, limit: PAGE });
    let d;
    try { d = await W.api(`/api/admin/users?${p}`); } catch (e) { return err(e); }
    ust.total = d.total;
    const body = $("#adm-rows");
    if (!d.users.length) {
      const tr = el("tr"); const td = el("td", "muted", "No users match."); td.colSpan = 9; tr.append(td); body.replaceChildren(tr);
    } else body.replaceChildren(...d.users.map(userRow));
    const end = Math.min(ust.offset + PAGE, d.total);
    $("#adm-page").textContent = d.total ? `${ust.offset + 1}–${end} of ${d.total}` : "0 users";
    $("#adm-prev").disabled = ust.offset === 0; $("#adm-next").disabled = end >= d.total;
  }
  function userRow(u) {
    const tr = el("tr");
    [String(u.id), u.username ? "@" + u.username : "—", u.first_name || "—", fmt(u.registered_at), fmt(u.last_active_at),
      u.referral_source, String(u.successful_referrals)].forEach((v) => tr.append(el("td", null, v)));
    const st = el("td"); st.append(el("span", "pill " + (u.status === "suspended" ? "risk-high" : "ok"), u.is_admin ? "admin" : u.status)); tr.append(st);
    const act = el("td");
    if (!u.is_admin && u.id !== (W.state && W.state.userId)) {
      const suspend = u.status !== "suspended";
      const b = el("button", "btn " + (suspend ? "danger" : "secondary") + " tiny", suspend ? "Suspend" : "Reactivate");
      b.onclick = () => changeStatus(u, suspend ? "suspended" : "active");
      act.append(b);
    }
    tr.append(act);
    return tr;
  }
  function changeStatus(u, status) {
    const who = u.username ? "@" + u.username : u.first_name || u.id;
    const go = async () => {
      try {
        await W.api(`/api/admin/users/${u.id}/status`, { method: "POST", body: JSON.stringify({ status, reason: "" }) });
        W.toast(status === "suspended" ? "User suspended" : "User reactivated"); loadUsers();
      } catch (e) { err(e); }
    };
    const msg = status === "suspended" ? `Suspend ${who}? They will lose access to the app.` : `Reactivate ${who}?`;
    if (tg && tg.showConfirm) tg.showConfirm(msg, (ok) => ok && go()); else if (confirm(msg)) go();
  }
  async function exportCsv() {
    const p = new URLSearchParams({ q: ust.q, status: ust.status });
    try {
      const res = await fetch(`/api/admin/users.csv?${p}`, { headers: { "X-Telegram-Init-Data": (tg && tg.initData) || "" } });
      if (!res.ok) throw new Error(res.status === 403 ? "Admins only" : "Export failed");
      const text = await res.text();
      const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
      const a = document.createElement("a"); a.href = url; a.download = "users.csv"; document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).catch(() => {});
      W.toast("CSV ready (also copied) ⬇️");
    } catch (e) { err(e); }
  }

  // ------------------------------------------------ rewards
  async function loadRewards() {
    try {
      const c = await W.api("/api/admin/referral-config");
      $("#rw-enabled").checked = c.enabled; $("#rw-points").value = c.points_per_referral;
      $("#rw-cap").textContent = `Fraud guard: at most ${c.hourly_cap} valid referrals per inviter per hour (REFERRAL_HOURLY_CAP). Only brand-new users count; self-invites and invites from suspended users are ignored.`;
    } catch (e) { err(e); }
  }
  async function saveRewards() {
    const pts = parseInt($("#rw-points").value, 10);
    if (isNaN(pts) || pts < 0 || pts > 1000) return W.toast("Points must be 0–1000");
    try {
      await W.api("/api/admin/referral-config", { method: "PUT", body: JSON.stringify({ enabled: $("#rw-enabled").checked, points_per_referral: pts }) });
      W.toast("Saved ✅");
    } catch (e) { err(e); }
  }

  // ------------------------------------------------ audit
  async function loadAudit() {
    try {
      const d = await W.api("/api/admin/audit?limit=100");
      const list = $("#adm-audit-list");
      if (!d.entries.length) list.replaceChildren(el("div", "empty", "No admin actions yet."));
      else list.replaceChildren(...d.entries.map((e) => el("div", "log", `${fmt(e.created_at)} · admin ${e.admin_id} · ${e.action} · ${e.target} ${e.details}`)));
    } catch (e) { err(e); }
  }
})();
