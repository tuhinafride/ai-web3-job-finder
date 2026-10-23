/* Web3 Job Finder Mini App. Every API call is authenticated with Telegram initData,
   which the server verifies with an HMAC of the bot token. No secrets live in this file. */
(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg ? tg.initData : "";
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#0a1428"); tg.setBackgroundColor("#0a1428"); } catch (_) {}
  }

  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text; // textContent = no HTML injection
    return n;
  };

  const state = { mode: "all", q: "", category: "", remote: false, has_salary: false, verified_only: false,
                  offset: 0, total: 0, categories: {}, profile: null };
  const LIMIT = 20;

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": initData, ...(opts.headers || {}) },
    });
    if (res.status === 401) { $("#gate").classList.remove("hidden"); throw new Error("auth"); }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  }

  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 1800);
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
  }

  function openLink(url) {
    if (!/^https?:\/\//i.test(url)) return;
    if (tg && tg.openLink) tg.openLink(url); else window.open(url, "_blank", "noopener");
  }

  function ago(iso) {
    if (!iso) return "";
    const d = (Date.now() - new Date(iso).getTime()) / 86400000;
    if (d < 1) return "today";
    if (d < 2) return "1 day ago";
    if (d < 60) return `${Math.floor(d)} days ago`;
    return new Date(iso).toLocaleDateString();
  }

  // ------------------------------------------------ job cards
  function jobCard(job) {
    const c = el("div", "card job");
    const row = el("div", "row");
    const left = el("div");
    left.append(el("h3", null, job.title), el("div", "company", job.company));
    const star = el("button", "star" + (job.saved ? " on" : ""), job.saved ? "★" : "☆");
    star.setAttribute("aria-label", "Save job");
    star.onclick = async (e) => { e.stopPropagation(); await toggleSave(job, star); };
    row.append(left, star);
    const meta = el("div", "meta");
    meta.append(el("span", "badge " + (job.source_type === "company_ats" ? "ats" : "agg"),
      job.source_type === "company_ats" ? "🏢 Company board" : "🔎 Third-party"));
    meta.append(el("span", "pill", job.category_label));
    if (job.remote) meta.append(el("span", "pill", "🌍 Remote"));
    else if (job.location) meta.append(el("span", "pill", "📍 " + job.location.slice(0, 40)));
    if (job.salary) meta.append(el("span", "pill money", "💰 " + job.salary.slice(0, 40)));
    if (job.match_score) meta.append(el("span", "pill score", `🎯 ${job.match_score}% match`));
    if (job.posted_at) meta.append(el("span", "pill", ago(job.posted_at)));
    c.append(row, meta);
    c.onclick = () => openDetail(job.id);
    return c;
  }

  async function toggleSave(job, starBtn) {
    try {
      if (job.saved) await api(`/api/saved/${job.id}`, { method: "DELETE" });
      else await api(`/api/saved/${job.id}`, { method: "POST" });
      job.saved = !job.saved;
      if (starBtn) { starBtn.textContent = job.saved ? "★" : "☆"; starBtn.classList.toggle("on", job.saved); }
      toast(job.saved ? "Saved ⭐" : "Removed");
      if (currentView === "saved") loadSaved();
    } catch (e) { if (e.message !== "auth") toast("Could not update"); }
  }

  // ------------------------------------------------ list
  async function loadJobs(reset = true) {
    const list = $("#list");
    if (reset) {
      state.offset = 0;
      list.replaceChildren(...Array.from({ length: 4 }, () => el("div", "skeleton")));
    }
    const p = new URLSearchParams({ q: state.q, category: state.category, offset: state.offset, limit: LIMIT });
    ["remote", "has_salary", "verified_only"].forEach((k) => state[k] && p.set(k, "true"));
    if (state.mode === "forme") p.set("for_me", "true");
    try {
      const data = await api(`/api/jobs?${p}`);
      if (reset) list.replaceChildren();
      state.total = data.total;
      if (!data.jobs.length && reset) {
        const msg = state.mode === "forme" && !(state.profile && (state.profile.roles.length || state.profile.skills.length))
          ? "Set your roles and skills in Profile to see matches."
          : "No jobs match these filters right now.";
        list.append(el("div", "empty", msg));
      }
      data.jobs.forEach((j) => list.append(jobCard(j)));
      state.offset += data.jobs.length;
      $("#more").classList.toggle("hidden", state.offset >= state.total);
    } catch (e) {
      if (e.message !== "auth") list.replaceChildren(el("div", "empty", "Couldn't load jobs. Pull down to retry."));
    }
  }

  function renderCategoryChips() {
    const wrap = $("#cat-chips");
    wrap.replaceChildren();
    const all = el("button", "chip" + (state.category === "" ? " on" : ""), "All roles");
    all.onclick = () => { state.category = ""; renderCategoryChips(); loadJobs(); };
    wrap.append(all);
    Object.entries(state.categories).forEach(([key, label]) => {
      const b = el("button", "chip" + (state.category === key ? " on" : ""), label);
      b.onclick = () => { state.category = key; renderCategoryChips(); loadJobs(); };
      wrap.append(b);
    });
    wrap.classList.toggle("hidden", state.mode === "forme");
  }

  // ------------------------------------------------ detail
  async function openDetail(id) {
    const box = $("#detail");
    box.replaceChildren(el("div", "skeleton"));
    $("#sheet").classList.remove("hidden");
    try {
      const j = await api(`/api/jobs/${id}`);
      box.replaceChildren();
      box.append(el("h2", null, j.title), el("div", "company muted", j.company));
      const meta = el("div", "meta");
      meta.append(el("span", "badge " + (j.source_type === "company_ats" ? "ats" : "agg"),
        j.source_type === "company_ats" ? "🏢 Company board" : "🔎 Third-party"));
      meta.append(el("span", "pill", j.category_label));
      if (j.location) meta.append(el("span", "pill", "📍 " + j.location));
      if (j.remote) meta.append(el("span", "pill", "🌍 Remote"));
      meta.append(j.salary ? el("span", "pill money", "💰 " + j.salary) : el("span", "pill", "Salary not listed"));
      if (j.match_score) meta.append(el("span", "pill score", `🎯 ${j.match_score}% match`));
      if (j.posted_at) meta.append(el("span", "pill", "Posted " + ago(j.posted_at)));
      box.append(meta);
      if (j.requirements.length) {
        box.append(el("div", "label", "Requirements"));
        const ul = el("ul");
        j.requirements.forEach((r) => ul.append(el("li", null, r)));
        box.append(ul);
      }
      if (j.tags.length) {
        const tags = el("div", "meta");
        j.tags.forEach((t) => tags.append(el("span", "pill", "#" + t)));
        box.append(tags);
      }
      const btns = el("div", "btns");
      const apply = el("button", "btn", "Apply ↗");
      apply.onclick = () => openLink(j.apply_url);
      const save = el("button", "btn secondary", j.saved ? "★ Saved" : "☆ Save");
      save.onclick = async () => { await toggleSave(j); save.textContent = j.saved ? "★ Saved" : "☆ Save"; };
      btns.append(apply, save);
      box.append(btns);
      const src = el("button", "btn ghost", "View original listing ↗");
      src.onclick = () => openLink(j.url);
      box.append(src);
      box.append(el("p", "note", `Source: ${j.source_name}. ${j.source_note}`));
      if (j.description) box.append(el("div", "desc", j.description));
      if (tg && tg.BackButton) { tg.BackButton.show(); tg.BackButton.onClick(closeDetail); }
    } catch (e) {
      if (e.message !== "auth") box.replaceChildren(el("div", "empty", "This job is no longer available."));
    }
  }
  function closeDetail() {
    $("#sheet").classList.add("hidden");
    if (tg && tg.BackButton) { tg.BackButton.hide(); tg.BackButton.offClick(closeDetail); }
  }
  $("#sheet-close").onclick = closeDetail;
  $("#sheet").onclick = (e) => { if (e.target.id === "sheet") closeDetail(); };

  // ------------------------------------------------ saved
  async function loadSaved() {
    const list = $("#saved-list");
    list.replaceChildren(el("div", "skeleton"));
    try {
      const data = await api("/api/saved");
      list.replaceChildren();
      if (!data.jobs.length) list.append(el("div", "empty", "Tap ☆ on any job to save it here."));
      data.jobs.forEach((j) => list.append(jobCard(j)));
    } catch (e) { if (e.message !== "auth") list.replaceChildren(el("div", "empty", "Couldn't load saved jobs.")); }
  }

  // ------------------------------------------------ profile
  function renderProfile() {
    const p = state.profile;
    const wrap = $("#role-chips");
    wrap.replaceChildren();
    Object.entries(state.categories).forEach(([key, label]) => {
      const b = el("button", "chip" + (p.roles.includes(key) ? " on" : ""), label);
      b.onclick = () => {
        p.roles = p.roles.includes(key) ? p.roles.filter((r) => r !== key) : [...p.roles, key];
        b.classList.toggle("on");
      };
      wrap.append(b);
    });
    $("#skills").value = p.skills.join(", ");
    $("#remote_only").checked = p.remote_only;
    $("#alerts_enabled").checked = p.alerts_enabled;
  }
  $("#save-profile").onclick = async () => {
    const p = state.profile;
    p.skills = $("#skills").value.split(",").map((s) => s.trim()).filter(Boolean).slice(0, 20);
    p.remote_only = $("#remote_only").checked;
    p.alerts_enabled = $("#alerts_enabled").checked;
    try {
      state.profile = await api("/api/profile", { method: "PUT", body: JSON.stringify(p) });
      toast("Profile saved ✅");
    } catch (e) { if (e.message !== "auth") toast("Could not save"); }
  };

  // ------------------------------------------------ navigation
  let currentView = "jobs";
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      currentView = t.dataset.view;
      document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
      $(`#view-${currentView}`).classList.add("active");
      if (currentView === "jobs") { state.mode = t.dataset.mode; renderCategoryChips(); loadJobs(); }
      if (currentView === "saved") loadSaved();
      if (currentView === "profile") renderProfile();
    };
  });
  document.querySelectorAll(".toggle").forEach((b) => {
    b.onclick = () => { const f = b.dataset.flag; state[f] = !state[f]; b.classList.toggle("on", state[f]); loadJobs(); };
  });
  let debounce;
  $("#q").addEventListener("input", (e) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { state.q = e.target.value.trim(); loadJobs(); }, 350);
  });
  $("#more").onclick = () => loadJobs(false);

  // ------------------------------------------------ boot
  (async () => {
    if (!initData) { $("#gate").classList.remove("hidden"); return; }
    try {
      const [meta, profile] = await Promise.all([api("/api/meta"), api("/api/profile")]);
      state.categories = meta.categories;
      state.profile = profile;
      $("#stat").textContent = `${meta.stats.total} live Web3 jobs from company boards & job sites`;
      renderCategoryChips();
      loadJobs();
    } catch (e) { if (e.message !== "auth") $("#stat").textContent = "Couldn't reach the server"; }
  })();
})();
