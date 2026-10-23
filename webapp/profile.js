/* Profile & invites pane: Telegram connection status, referral link, stats, leaderboard, limits, sign-out.
   "Connected" is only shown from the server's /api/me answer, which is true only when the request's
   initData passed the server-side HMAC check. Everything is rendered with textContent. */
(() => {
  const W = window.W3 || (window.W3 = {});
  const $ = (s) => document.querySelector(s);
  const el = (...a) => W.el(...a);
  const tg = window.Telegram && window.Telegram.WebApp;
  let me = null;

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => W.toast("Link copied 📋"), () => fallbackCopy(text));
    } else fallbackCopy(text);
  }
  function fallbackCopy(text) {
    const ta = document.createElement("textarea"); ta.value = text; document.body.append(ta); ta.select();
    try { document.execCommand("copy"); W.toast("Link copied 📋"); } catch (_) { W.toast("Copy not supported here"); }
    ta.remove();
  }

  function stat(label, value) {
    const c = el("div", "stat"); c.append(el("b", null, String(value)), el("span", null, label)); return c;
  }

  W.loadMe = async () => {
    try {
      me = await W.api("/api/me");
      if (W.state) W.state.me = me;
    } catch (e) {
      $("#me-dot").className = "dot"; $("#me-title").textContent = "Not connected";
      $("#me-sub").textContent = e.message === "auth" ? "Open the app from the bot inside Telegram." : "Couldn't reach the server.";
      return;
    }
    $("#me-dot").className = "dot " + (me.connected ? "on" : "");
    $("#me-title").textContent = me.connected ? "Connected with Telegram" : "Not connected";
    const who = [me.first_name, me.username ? "@" + me.username : ""].filter(Boolean).join(" · ");
    $("#me-sub").textContent = `${who || "Telegram user"}${me.is_admin ? " · Admin" : ""} · Joined ${me.registered_at ? new Date(me.registered_at).toLocaleDateString() : "today"}`;

    const r = me.referral || {};
    const link = r.app_link || r.bot_link;
    $("#ref-link").textContent = link || "Invite link isn't available yet (the server doesn't know the bot username).";
    $("#ref-copy").disabled = !link; $("#ref-share").disabled = !link;
    $("#ref-copy").onclick = () => link && copyText(link);
    $("#ref-share").onclick = () => {
      if (!r.share_url) return;
      if (tg && tg.openTelegramLink) tg.openTelegramLink(r.share_url); else W.openLink(r.share_url);
    };
    const s = [stat("Invited", r.invited || 0), stat("Valid", r.valid || 0)];
    if (r.points_enabled || r.points) s.push(stat("Points", r.points || 0));
    $("#ref-stats").replaceChildren(...s);
    $("#ref-points-label").textContent = r.points_enabled
      ? `${r.points_per_referral} points per valid referral. ${r.points_label || ""}`
      : (r.points ? r.points_label || "" : "Reward points are currently turned off. Invites are still counted.");

    const L = me.limits || {};
    $("#me-limits").textContent = L.unlimited ? "Admin: no daily limits."
      : (L.search ? `Job searches: ${L.search.left} of ${L.search.limit} left · Message drafts: ${L.draft.left} of ${L.draft.limit} left · Resets ${L.resets}` : "");

    try {
      const lb = await W.api("/api/referrals/leaderboard?limit=10");
      const box = $("#leaderboard");
      if (!lb.leaderboard.length) box.replaceChildren(el("div", "muted small", "No valid invites yet. Be the first!"));
      else box.replaceChildren(...lb.leaderboard.map((x) => {
        const row = el("div", "lbrow");
        row.append(el("span", "rank", `#${x.rank}`), el("span", "name", x.name + (x.id ? ` (${x.id})` : "")),
          el("span", "val", `${x.valid} invite${x.valid === 1 ? "" : "s"}`));
        return row;
      }));
    } catch (_) {}
  };

  // Sign out: clears local UI state only. Telegram identity is managed by Telegram itself.
  function signOut() {
    try { localStorage.clear(); sessionStorage.clear(); } catch (_) {}
    if (W.state) Object.keys(W.state).forEach((k) => { if (typeof W.state[k] !== "function") W.state[k] = null; });
    document.querySelectorAll(".list, #leaderboard, #ref-stats, #adm-rows").forEach((n) => n.replaceChildren());
    document.querySelectorAll("input, textarea").forEach((n) => { if (n.type !== "checkbox") n.value = ""; });
    me = null;
    $("#signed-out").classList.remove("hidden");
  }
  const so = $("#sign-out");
  if (so) so.onclick = () => {
    const msg = "Sign out on this device? Your Telegram account stays signed in to Telegram.";
    if (tg && tg.showConfirm) tg.showConfirm(msg, (ok) => ok && signOut());
    else if (confirm(msg)) signOut();
  };
  const again = $("#sign-in-again");
  if (again) again.onclick = () => location.reload();
  const close = $("#close-app");
  if (close) close.onclick = () => { if (tg && tg.close) tg.close(); };
})();
