/* AI job discovery + outreach agent UI (every user; sending from a personal account is admin-only). Loaded after app.js; uses window.W3 helpers.
   Everything is rendered with textContent (no HTML injection). No secrets in this file. */
(() => {
  const W = window.W3 || (window.W3 = {});
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const el = (...a) => W.el(...a);
  const api = (...a) => W.api(...a);
  const toast = (m) => W.toast(m);
  const ROLE = {};
  const lstate = { source: "board", offset: 0, total: 0 };
  let dstate = "pending";
  let tstate = "saved,applied,interview,offer,rejected";

  const SRC = { x: "𝕏 X", google: "🔎 Google", telegram: "✈️ Telegram", board: "🏢 Board" };
  const PAY = { paid: "💰 Paid", unpaid: "Unpaid", unknown: "Pay unknown" };
  const CH = { telegram: "✈️ Telegram", x: "𝕏 X", email: "✉️ Email", manual: "📝 Manual apply" };
  const STATUS = ["new", "saved", "applied", "interview", "offer", "rejected", "skipped"];

  function err(e) { if (e && e.message !== "auth") toast(e.message || "Something went wrong"); }
  function seg(container, attr, onPick) {
    $$(`${container} .segbtn`).forEach((b) => b.onclick = () => {
      $$(`${container} .segbtn`).forEach((x) => x.classList.remove("on"));
      b.classList.add("on"); onPick(b.dataset[attr]);
    });
  }
  async function busy(btn, fn) {
    if (btn.disabled) return; btn.disabled = true; const t = btn.textContent; btn.textContent = "…";
    try { await fn(); } catch (e) { err(e); } finally { btn.disabled = false; if (btn.textContent === "…") btn.textContent = t; }
  }
  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => toast("Copied 📋"), () => toast("Copy failed"));
    else toast("Copy not supported here");
  }

  // ------------------------------------------------ init
  W.isOwner = false;     // true once the agent UI is set up (any signed-in user)
  W.isAdmin = false;
  W.initAgent = (isAdmin) => {
    W.isOwner = true;
    W.isAdmin = !!isAdmin;
    if (!W.isAdmin) $$(".admin-only").forEach((n) => { n.classList.remove("on"); });
    $("#user-nav").classList.add("hidden");
    $("#owner-nav").classList.remove("hidden");
    $("#src-seg").classList.remove("hidden");
    // move existing sections into Settings so nothing is duplicated
    const prof = $("#view-profile");
    Array.from(prof.children).forEach((c) => { if (!c.classList.contains("section-title")) $("#set-matching").append(c); });
    if (W.isAdmin) {   // personal Telegram account login is admin-only
      const acct = $("#view-account");
      Array.from(acct.children).forEach((c) => $("#set-account").append(c));
    }
    seg("#src-seg", "src", (src) => { lstate.source = src; showSource(); });
    seg("#out-seg", "ds", (v) => { dstate = v; loadDrafts(); });
    seg("#trk-seg", "ts", (v) => { tstate = v; loadTracker(); });
    seg("#set-seg", "ss", (v) => { $$(".setpane").forEach((p) => p.classList.add("hidden")); $(`#set-${v}`).classList.remove("hidden"); loadSettingsPane(v); });
    ["#f-role", "#f-pay", "#f-fresh", "#f-rel", "#f-sort", "#f-safe"].forEach((id) => $(id).onchange = () => loadLeads());
    let deb; $("#lq").oninput = () => { clearTimeout(deb); deb = setTimeout(loadLeads, 350); };
    $("#leads-more").onclick = () => loadLeads(false);
    $("#find-here").onclick = (e) => busy(e.target, () => findJobs(lstate.source && lstate.source !== "board" ? [lstate.source] : defaultSources()));
    $$("#find-sources .chip").forEach((c) => c.onclick = () => c.classList.toggle("on"));
    $("#find-go").onclick = (e) => busy(e.target, () => findJobs($$("#find-sources .chip.on").map((c) => c.dataset.s).filter((x) => W.isAdmin || x !== "telegram"), $("#find-q").value.trim()));
    $("#stop-go").onclick = (e) => busy(e.target, toggleStop);
    $("#p-save").onclick = (e) => busy(e.target, saveProfile);
    $("#a-save").onclick = (e) => busy(e.target, saveAgent);
    document.querySelector('#owner-nav .tab[data-view="home"]').click();
    if (W.isAdmin && W.initAutomation) W.initAutomation();
  };

  W.initOwner = () => W.initAgent(true);
  W.reloadHome = () => { if ($("#view-home").classList.contains("active")) loadHome(); };   // backwards compatible name
  const defaultSources = () => (W.isAdmin ? ["google", "x", "telegram"] : ["google", "x"]);

  W.onView = (view) => {
    if (!W.isOwner) return;
    if (view === "home") loadHome();
    if (view === "jobs") showSource();
    if (view === "outreach") loadDrafts();
    if (view === "tracker") loadTracker();
    if (view === "settings") { const on = $("#set-seg .segbtn.on"); loadSettingsPane(on ? on.dataset.ss : "me"); }
  };

  // ------------------------------------------------ dashboard
  let agentStopped = false, pollT = null;
  async function loadHome() {
    let d;
    try { d = await api("/api/agent/dashboard"); } catch (e) { return err(e); }
    agentStopped = d.agent.stopped;
    $("#ag-dot").className = "dot " + (d.agent.stopped ? "" : d.agent.running ? "wait" : "on");
    $("#ag-title").textContent = d.agent.stopped ? "Agent stopped" : d.agent.running ? "Searching…" : "Agent ready";
    const last = d.agent.last_search_at ? ` · Last search ${W.ago(d.agent.last_search_at)}` : "";
    if (d.is_admin) {
      $("#ag-sub").textContent = `Telegram account: ${d.telegram_connected ? "connected ✅" : "not connected"} · Sent today ${d.sent_today}/${d.agent.daily_limit}` + last;
    } else {
      const L = d.limits || {};
      $("#ag-sub").textContent = (L.search ? `Searches left today: ${L.search.left}/${L.search.limit} · Drafts left: ${L.draft.left}/${L.draft.limit}` : "") + last;
    }
    $("#stop-go").textContent = d.agent.stopped ? "▶️ Resume Agent" : "⏹ Stop Agent";
    $("#stop-go").className = "btn " + (d.agent.stopped ? "secondary" : "danger");
    const stats = [["Leads found", d.leads_total], ["New this week", d.leads_week], ["Paid roles", d.paid],
      ["Pending approval", d.pending], ["Sent", d.sent], ["Flagged risky", d.flagged],
      ["Applications", d.applied], ["Saved", d.saved], ["Board jobs", d.board_jobs]];
    $("#stats").replaceChildren(...stats.map(([k, v]) => { const c = el("div", "stat"); c.append(el("b", null, String(v)), el("span", null, k)); return c; }));
    const p = d.providers || {}, miss = [];
    if (d.is_admin && !p.google) miss.push("Google search (add LLM_API_KEY with a free Gemini key, or SERPER_API_KEY / BRAVE_API_KEY on Render)");
    if (d.is_admin && !p.x) miss.push("X search (add LLM_API_KEY with a free Gemini key, or X_BEARER_TOKEN / TWITTERAPI_IO_KEY)");
    if (d.is_admin && !p.llm) miss.push("AI scoring and drafts (add LLM_API_KEY, a free Gemini key from aistudio.google.com). Rules and your template are used until then");
    if (d.is_admin && !d.telegram_connected) miss.push("Telegram account (Settings → Telegram account) for channel search and sending");
    $("#prov-warn").classList.toggle("hidden", !miss.length);
    $("#prov-warn").replaceChildren(el("b", null, "Not set up yet: "), ...miss.map((m) => el("div", null, "• " + m)));
    $("#home-logs").replaceChildren(...(d.recent_logs.length ? d.recent_logs.map(logRow) : [el("div", "empty", "No activity yet. Tap Find Jobs.")]));
    const badge = $("#badge-pending"); badge.textContent = String(d.pending); badge.classList.toggle("hidden", !d.pending);
    clearTimeout(pollT);
    if (d.agent.running) pollT = setTimeout(() => { if ($("#view-home").classList.contains("active")) loadHome(); }, 4000);
  }
  function logRow(l) {
    const r = el("div", "log " + l.level);
    r.textContent = `${new Date(l.ts).toLocaleString()} · ${l.kind} · ${l.message}`;
    return r;
  }
  async function findJobs(sources, query = "") {
    if (!sources.length) return toast("Pick at least one source");
    const r = await api("/api/agent/search", { method: "POST", body: JSON.stringify({ sources, query }) });
    toast(r.message);
    setTimeout(() => { if ($("#view-home").classList.contains("active")) loadHome(); else loadLeads(); }, 1500);
    setTimeout(() => { if ($("#view-jobs").classList.contains("active")) loadLeads(); }, 15000);
  }
  async function toggleStop() {
    $("#stop-go").textContent = "…";
    const r = await api("/api/agent/stop", { method: "POST", body: JSON.stringify({ stop: !agentStopped }) });
    toast(r.stopped ? "Agent stopped. Nothing will be searched or sent." : "Agent resumed");
    await loadHome();
  }

  // ------------------------------------------------ leads
  function showSource() {
    const board = lstate.source === "board";
    $("#board-pane").classList.toggle("hidden", !board);
    $("#leads-pane").classList.toggle("hidden", board);
    if (!board) loadLeads();
  }
  async function loadLeads(reset = true) {
    if (reset) lstate.offset = 0;
    const p = new URLSearchParams({ source: lstate.source || "", role: $("#f-role").value, pay: $("#f-pay").value,
      freshness: $("#f-fresh").value, min_relevance: $("#f-rel").value, sort: $("#f-sort").value,
      hide_flagged: $("#f-safe").checked, q: $("#lq").value.trim(), offset: lstate.offset, limit: 20 });
    const list = $("#leads-list");
    if (reset) list.replaceChildren(el("div", "skeleton"));
    try {
      const d = await api(`/api/agent/leads?${p}`);
      if (!Object.keys(ROLE).length) {
        Object.assign(ROLE, d.roles);
        Object.entries(d.roles).forEach(([k, v]) => { const o = el("option", null, v); o.value = k; $("#f-role").append(o); });
      }
      if (reset) list.replaceChildren();
      if (!d.leads.length && reset) list.append(el("div", "empty", "No leads here yet. Tap Find Jobs to search."));
      d.leads.forEach((l) => list.append(leadCard(l)));
      lstate.offset += d.leads.length; lstate.total = d.total;
      $("#leads-more").classList.toggle("hidden", lstate.offset >= d.total);
    } catch (e) { err(e); }
  }
  function leadCard(l) {
    const c = el("div", "card job");
    c.append(el("h3", null, l.title || l.role_label), el("div", "company", l.project || "Project not named"));
    const m = el("div", "meta");
    m.append(el("span", "badge " + (l.source === "board" ? "ats" : "agg"), SRC[l.source] || l.source));
    m.append(el("span", "pill score", `🎯 ${l.relevance}%`));
    m.append(el("span", "pill" + (l.pay_status === "paid" ? " money" : ""), l.pay_detail ? "💰 " + l.pay_detail : PAY[l.pay_status]));
    m.append(el("span", "pill", l.role_label));
    if (l.remote === "yes") m.append(el("span", "pill", "🌍 Remote"));
    if (l.scam_risk !== "low") m.append(el("span", "pill risk-" + l.scam_risk, `⚠️ ${l.scam_risk} risk`));
    m.append(el("span", "pill", l.freshness === "old" ? "🕰 Old post" : l.posted_at ? "Posted " + W.ago(l.posted_at) : "Found " + W.ago(l.discovered_at)));
    if (l.contacts.length) m.append(el("span", "pill", `👤 ${l.contacts.length} contact${l.contacts.length > 1 ? "s" : ""}`));
    if (l.status !== "new") m.append(el("span", "pill", "📌 " + l.status));
    c.append(m);
    c.onclick = () => openLead(l.id);
    return c;
  }
  async function openLead(id) {
    const box = W.openSheet();
    box.replaceChildren(el("div", "skeleton"));
    let l;
    try { l = await api(`/api/agent/leads/${id}`); } catch (e) { return err(e); }
    box.replaceChildren();
    box.append(el("h2", null, l.title || l.role_label), el("div", "company muted", l.project || "Project not named"));
    const m = el("div", "meta");
    m.append(el("span", "badge " + (l.source === "board" ? "ats" : "agg"), SRC[l.source]),
      el("span", "pill score", `🎯 ${l.relevance}% match`), el("span", "pill", l.role_label));
    m.append(el("span", "pill", l.source_checked ? "✅ Source page loaded" : "Source not opened yet — verify"));
    if (l.ai_used) m.append(el("span", "pill", "🤖 AI analysed"));
    box.append(m);
    if (l.scam_flags.length) {
      box.append(el("div", "label", `⚠️ Scam risk: ${l.scam_risk}`));
      l.scam_flags.forEach((f) => box.append(el("div", "flag", "• " + f)));
    }
    const kv = (k, v) => { if (!v) return; const d = el("div", "kv"); d.append(el("b", null, k + ": "), document.createTextNode(v)); box.append(d); };
    kv("Pay", (PAY[l.pay_status] || "") + (l.pay_detail ? ` · ${l.pay_detail}` : ""));
    kv("Remote", l.remote === "yes" ? "Yes" : l.remote === "no" ? "No" : "Not stated");
    kv("Apply", l.apply_method);
    kv("Deadline", l.deadline);
    kv("Posted", l.posted_at ? new Date(l.posted_at).toLocaleDateString() + (l.freshness === "old" ? " (old announcement)" : "") : "Date not shown");
    kv("Discovered", new Date(l.discovered_at).toLocaleString());
    if (l.summary) { box.append(el("div", "label", "Summary")); box.append(el("p", "muted", l.summary)); }
    [["Responsibilities", l.responsibilities], ["Requirements", l.requirements]].forEach(([t, arr]) => {
      if (!arr.length) return; box.append(el("div", "label", t)); const ul = el("ul"); arr.forEach((x) => ul.append(el("li", null, x))); box.append(ul);
    });
    box.append(el("div", "label", "Public contacts"));
    if (!l.contacts.length) box.append(el("p", "muted small", "No public contact found in the source. Use the apply link, or draft a manual message."));
    l.contacts.forEach((c) => {
      const row = el("div", "kv");
      row.append(el("b", null, (CH[c.platform] || c.platform) + ": "), document.createTextNode((c.platform === "email" ? "" : "@") + c.handle + ` (${c.role_hint})` + (c.contacted ? " · already contacted" : "")));
      if (!c.contacted && ["telegram", "x", "email"].includes(c.platform) && l.scam_risk !== "high") {
        const b = el("button", "btn secondary", `✍️ Draft for ${c.platform === "email" ? c.handle : "@" + c.handle}`);
        b.onclick = (e) => busy(e.target, () => makeDraft(l.id, c.id));
        row.append(b);
      }
      box.append(row);
    });
    const btns = el("div", "btns");
    const gen = el("button", "btn", "✍️ Generate Message");
    gen.disabled = l.scam_risk === "high";
    if (gen.disabled) gen.textContent = "Drafting off: high scam risk";
    gen.onclick = (e) => busy(e.target, () => makeDraft(l.id));
    const save = el("button", "btn secondary", l.status === "saved" ? "★ Saved" : "☆ Save Job");
    save.onclick = (e) => busy(e.target, async () => { await setStatus(l.id, l.status === "saved" ? "new" : "saved"); l.status = l.status === "saved" ? "new" : "saved"; save.textContent = l.status === "saved" ? "★ Saved" : "☆ Save Job"; });
    btns.append(gen, save);
    box.append(btns);
    const src = el("button", "btn ghost", "Open source ↗");
    src.onclick = () => W.openLink(l.source_url);
    const skip = el("button", "btn ghost", "Skip (hide this lead)");
    skip.onclick = (e) => busy(e.target, async () => { await setStatus(l.id, "skipped"); W.closeDetail(); loadLeads(); });
    box.append(src, skip);
    if (l.drafts.length) {
      box.append(el("div", "label", "Drafts for this lead"));
      l.drafts.forEach((d) => box.append(el("div", "kv", `#${d.id} · ${CH[d.channel]} ${d.recipient ? "@" + d.recipient : ""} · ${d.status}`)));
    }
    box.append(el("p", "note", "Nothing here is marked verified unless the source page was actually loaded. Always confirm on the project's official site or X account before sharing personal details."));
  }
  async function setStatus(id, status) {
    await api(`/api/agent/leads/${id}`, { method: "PATCH", body: JSON.stringify({ status }) });
  }
  async function makeDraft(leadId, contactId = null) {
    const d = await api(`/api/agent/leads/${leadId}/draft`, { method: "POST", body: JSON.stringify({ contact_id: contactId }) });
    toast(d.status === "skipped" ? d.error || "Already contacted" : "Draft ready in Outreach ✉️");
    W.closeDetail();
    document.querySelector('#owner-nav .tab[data-view="outreach"]').click();
  }
  W.trackButton = (job) => {
    const b = el("button", "btn secondary", "✍️ Track & Generate Message");
    b.onclick = (e) => busy(e.target, async () => {
      const r = await api(`/api/agent/jobs/${job.id}/import`, { method: "POST" });
      await openLead(r.lead_id);
    });
    return b;
  };

  // ------------------------------------------------ drafts / outreach
  async function loadDrafts() {
    const list = $("#drafts-list");
    list.replaceChildren(el("div", "skeleton"));
    if (dstate === "contacts") return loadContacts(list);
    try {
      const d = await api(`/api/agent/drafts?status=${encodeURIComponent(dstate)}`);
      list.replaceChildren();
      if (!d.drafts.length) list.append(el("div", "empty", dstate === "pending" ? "No drafts waiting. Open a lead and tap Generate Message." : "Nothing here yet."));
      d.drafts.forEach((x) => list.append(draftCard(x)));
    } catch (e) { err(e); }
  }
  function draftCard(d) {
    const c = el("div", "card draft");
    c.append(el("h3", null, d.project || d.title || "Opportunity"));
    c.append(el("div", "to", `To: ${CH[d.channel] || d.channel} ${d.recipient ? (d.channel === "email" ? d.recipient : "@" + d.recipient) : "(no public contact — apply via source)"}`));
    const m = el("div", "meta");
    m.append(el("span", "badge agg", SRC[d.source] || d.source), el("span", "pill score", `🎯 ${d.relevance}%`),
      el("span", "pill", d.generated_by === "ai" ? "🤖 AI draft" : d.generated_by === "edited" ? "✏️ Edited" : "📄 Template"));
    if (d.scam_risk !== "low") m.append(el("span", "pill risk-" + d.scam_risk, `⚠️ ${d.scam_risk} risk`));
    m.append(el("span", "pill", d.status));
    c.append(m);
    const src = el("button", "btn ghost", "Source ↗"); src.style.marginTop = "6px"; src.onclick = () => W.openLink(d.source_url);
    c.append(src);
    const ta = el("textarea"); ta.value = d.text; ta.maxLength = 1500; ta.readOnly = d.status === "sent";
    c.append(ta);
    if (d.error) c.append(el("div", "flag", d.error));
    if (d.sent_at) c.append(el("div", "muted small", "Sent " + new Date(d.sent_at).toLocaleString()));
    const btns = el("div", "btns");
    const act = (action, extra = {}) => api(`/api/agent/drafts/${d.id}`, { method: "POST", body: JSON.stringify({ action, ...extra }) });
    if (d.status !== "sent") {
      if (d.channel === "telegram" && !W.isAdmin) {
        // normal users send manually from their own Telegram app
        const open = el("button", "btn", "📋 Copy & open Telegram chat");
        open.onclick = () => {
          copy(ta.value);
          const u = `https://t.me/${encodeURIComponent(d.recipient)}`;
          if (window.Telegram && Telegram.WebApp && Telegram.WebApp.openTelegramLink) Telegram.WebApp.openTelegramLink(u); else W.openLink(u);
        };
        btns.append(open);
      } else if (d.channel === "telegram") {
        const send = el("button", "btn", d.status === "failed" ? "🔁 Retry send" : "✅ Approve & Send");
        send.onclick = (e) => busy(e.target, async () => {
          const go = async () => { const r = await act(d.status === "failed" ? "retry" : "approve", { text: ta.value }); toast(r.message || r.status); loadDrafts(); };
          const msg = `Send this message from YOUR Telegram account to @${d.recipient}?`;
          if (window.Telegram && Telegram.WebApp && Telegram.WebApp.showConfirm) Telegram.WebApp.showConfirm(msg, (ok) => ok && go());
          else if (confirm(msg)) await go();
        });
        btns.append(send);
      } else if (d.channel === "x") {
        const open = el("button", "btn", "📋 Copy & open X profile");
        open.onclick = () => { copy(ta.value); W.openLink(`https://x.com/${encodeURIComponent(d.recipient)}`); };
        btns.append(open);
      } else if (d.channel === "email") {
        const mail = el("button", "btn", "✉️ Open email app");
        mail.onclick = () => { window.location.href = `mailto:${encodeURIComponent(d.recipient)}?subject=${encodeURIComponent("Paid opportunity inquiry: " + (d.project || ""))}&body=${encodeURIComponent(ta.value)}`; };
        btns.append(mail);
      } else {
        const cp = el("button", "btn", "📋 Copy message"); cp.onclick = () => copy(ta.value); btns.append(cp);
      }
      if (d.channel !== "telegram" || !W.isAdmin) {
        const done = el("button", "btn secondary", "✔️ Mark as sent");
        done.onclick = (e) => busy(e.target, async () => { await act("mark_sent"); toast("Recorded as sent"); loadDrafts(); });
        btns.append(done);
      }
      const save = el("button", "btn secondary", "✏️ Save edit");
      save.onclick = (e) => busy(e.target, async () => { await act("edit", { text: ta.value }); toast("Saved"); });
      const skip = el("button", "btn ghost", "⏭ Skip");
      skip.onclick = (e) => busy(e.target, async () => { await act("skip"); loadDrafts(); });
      const rej = el("button", "btn ghost", "✖ Reject");
      rej.onclick = (e) => busy(e.target, async () => { await act("reject"); loadDrafts(); });
      btns.append(save, skip, rej);
    }
    c.append(btns);
    return c;
  }
  async function loadContacts(list) {
    try {
      const d = await api("/api/agent/contacts");
      list.replaceChildren();
      if (!d.contacts.length) list.append(el("div", "empty", "Public contacts found in job posts will appear here."));
      d.contacts.forEach((c) => {
        const card = el("div", "card");
        card.append(el("h3", null, `${CH[c.platform] || c.platform} ${c.platform === "email" ? c.handle : "@" + c.handle}`));
        card.append(el("div", "company", `${c.project || c.title} · ${c.role_hint}`));
        const m = el("div", "meta");
        if (c.contacted) m.append(el("span", "pill", "✔️ Contacted"));
        if (c.scam_risk !== "low") m.append(el("span", "pill risk-" + c.scam_risk, `⚠️ ${c.scam_risk} risk`));
        card.append(m);
        card.onclick = () => openLead(c.lead_id);
        list.append(card);
      });
    } catch (e) { err(e); }
  }

  // ------------------------------------------------ tracker
  async function loadTracker() {
    const list = $("#tracker-list");
    if (tstate === "boards") return W.loadSaved(list);
    list.replaceChildren(el("div", "skeleton"));
    try {
      const d = await api(`/api/agent/leads?status=${encodeURIComponent(tstate)}&sort=date&limit=50`);
      list.replaceChildren();
      if (!d.leads.length) list.append(el("div", "empty", "Save a lead or send a message and it shows up here."));
      d.leads.forEach((l) => {
        const c = leadCard(l);
        const sel = el("select", "status");
        STATUS.forEach((s) => { const o = el("option", null, s[0].toUpperCase() + s.slice(1)); o.value = s; if (s === l.status) o.selected = true; sel.append(o); });
        sel.onclick = (e) => e.stopPropagation();
        sel.onchange = async () => { try { await setStatus(l.id, sel.value); toast("Updated"); } catch (e) { err(e); } };
        c.append(sel);
        list.append(c);
      });
    } catch (e) { err(e); }
  }

  // ------------------------------------------------ settings
  async function loadSettingsPane(v, keepWarn = false) {
    if (v === "me" && W.loadMe) W.loadMe();
    if (v === "account") W.loadAccount();
    if (v === "pro") {
      try {
        const p = await api("/api/agent/profile");
        ["name", "location", "headline", "experience", "skills", "links", "template"].forEach((k) => $(`#p-${k}`).value = p[k] || "");
        const wrap = $("#p-roles"); wrap.replaceChildren();
        Object.entries(p.roles).forEach(([k, label]) => {
          const b = el("button", "chip" + (p.preferred_roles.includes(k) ? " on" : ""), label);
          b.dataset.k = k; b.onclick = () => b.classList.toggle("on"); wrap.append(b);
        });
      } catch (e) { err(e); }
    }
    if (v === "agent") {
      try {
        const s = await api("/api/agent/settings");
        if (!keepWarn) $("#a-warn").classList.add("hidden");
        $("#a-limit").value = s.daily_limit; $("#a-gap").value = s.min_interval_sec; $("#a-auto").value = s.auto_search_hours;
        $("#a-queries").value = s.queries; $("#a-channels").value = s.tg_channels;
        const p = s.providers || {};
        $("#a-auto").disabled = !s.is_admin;
        if (!s.is_admin) { $("#a-prov").textContent = "Auto search, Telegram channel reading and sending from a Telegram account are admin-only. Your drafts are sent by you, manually."; return; }
        const chain = (c) => (c && c.length ? c.join(" → ") : "not set");
        $("#a-prov").textContent = `Google: ${chain(p.google_chain)} · X: ${chain(p.x_chain)} · Messages: ${p.llm_draft || "templates"} · Scoring: ${p.llm_qualify || "rules"}`;
      } catch (e) { err(e); }
    }
    if (v === "logs") {
      try {
        const d = await api("/api/agent/logs");
        $("#logs-list").replaceChildren(...(d.logs.length ? d.logs.map(logRow) : [el("div", "empty", "No logs yet.")]));
      } catch (e) { err(e); }
    }
  }
  async function saveProfile() {
    const body = {};
    ["name", "location", "headline", "experience", "skills", "links", "template"].forEach((k) => body[k] = $(`#p-${k}`).value);
    body.preferred_roles = $$("#p-roles .chip.on").map((b) => b.dataset.k);
    if (!body.template.trim()) throw new Error("The message template can't be empty.");
    if (body.template.length > 1500) throw new Error(`Template is too long (${body.template.length}/1500 characters).`);
    if (body.experience.length > 1500) throw new Error(`Experience is too long (${body.experience.length}/1500 characters).`);
    await api("/api/agent/profile", { method: "PUT", body: JSON.stringify(body) });
    toast("Profile saved ✅");
    await loadSettingsPane("pro");          // show exactly what the server stored
  }
  // strict integer parsing: "" / "abc" / "12abc" / 0 are errors, never silently replaced by a default
  function intField(id, label, min, max) {
    const raw = $(id).value.trim();
    if (!/^\d+$/.test(raw)) throw new Error(`${label}: enter a whole number (${min}–${max}).`);
    const n = parseInt(raw, 10);
    if (n < min || n > max) throw new Error(`${label}: must be between ${min} and ${max}.`);
    return n;
  }
  W.intField = intField;
  async function saveAgent() {
    const body = { queries: $("#a-queries").value };
    if (W.isAdmin) {
      body.daily_limit = intField("#a-limit", "Max messages per 24h", 1, 30);
      body.min_interval_sec = intField("#a-gap", "Minimum gap (seconds)", 60, 3600);
      body.auto_search_hours = intField("#a-auto", "Auto search hours", 0, 48);
      body.tg_channels = $("#a-channels").value;
    }
    const r = await api("/api/agent/settings", { method: "PUT", body: JSON.stringify(body) });
    const warn = $("#a-warn");
    warn.textContent = (r.warnings || []).join(" ");
    warn.classList.toggle("hidden", !(r.warnings || []).length);
    toast((r.warnings || []).length ? "Saved with notes ⚠️" : "Agent settings saved ✅");
    await loadSettingsPane("agent", true);  // reload: channels/queries come back normalised by the server
  }
})();
