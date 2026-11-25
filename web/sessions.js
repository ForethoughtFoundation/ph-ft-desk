// Agents waiting: Claude Code and Codex sessions that have stopped for Peter, with what each needs and replies
// he can send from here. Data comes from desk_sessions.py through the server; the desk's sync says when it changed.
(() => {
  const view = document.getElementById("view-sessions");
  if (!view) return;
  const TOKEN = document.querySelector('meta[name="desk-token"]').content;
  const tabN = document.getElementById("sessions-n");
  const isMac = navigator.platform.includes("Mac");
  const api = async (path, body) => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN }, body: JSON.stringify(body || {}) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  };
  const E = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (msg, undo) => {
    const t = document.getElementById("toast");
    t.textContent = msg; t.hidden = false;
    if (undo) {
      const b = document.createElement("button"); b.type = "button"; b.className = "toast-act"; b.textContent = "Undo";
      b.addEventListener("click", () => { t.hidden = true; undo(); });
      t.append(" ", b);
    }
    clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), undo ? 6000 : 3600);
  };

  const PRI = {
    now: { label: "Now", sla: 30 * 60 },
    today: { label: "Today", sla: 4 * 3600 },
    later: { label: "Whenever", sla: 2 * 86400 },
    wrap: { label: "Wrap up", sla: 86400 },
  };
  const ORDER = ["now", "today", "later", "wrap"];
  const OPEN_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 3h4v4M13 3 7.5 8.5M11.5 9.5V13H3V4.5h3.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const EXPAND_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  let data = { items: [], snoozed: 0 }, hash = null, loaded = false, selId = null;
  let sort = "pri";
  try { sort = localStorage.getItem("desk-sessions-sort") || "pri"; } catch (_) {}
  const typed = new Map();       // session id -> what Peter has typed
  const sending = new Map();     // session id -> the stop (its `since`) a reply or dismissal went to
  const pending = it => !!it && sending.has(it.session_id) && sending.get(it.session_id) === it.since;  // a later stop shows again

  const waited = it => Math.max(0, (Date.now() - Date.parse(it.since)) / 1000);
  const over = it => waited(it) > PRI[it.priority].sla;
  const fmt = s => {
    const m = Math.floor(s / 60);
    if (m < 1) return "just now";
    if (m < 60) return m + " min";
    const h = Math.floor(m / 60);
    if (h < 24) return h + " h" + (h < 10 && m % 60 ? " " + (m % 60) + " min" : "");
    const d = Math.floor(h / 24);
    return d + " d" + (h % 24 ? " " + (h % 24) + " h" : "");
  };
  const age = it => `<span class="ss-age${over(it) ? " over" : ""}" title="Waiting since ${E(new Date(it.since).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" }))}">${over(it) ? "Overdue · " : ""}${fmt(waited(it))}</span>`;
  const chip = p => `<span class="ss-pri ${p}">${PRI[p].label}</span>`;
  const title = it => it.title || it.tab || "Untitled session";
  const folder = f => (f || "").replace(/^\/Users\/[^/]+\//, "~/");
  const visible = () => data.items.filter(it => !pending(it));
  const urgency = (a, b) => ORDER.indexOf(a.priority) - ORDER.indexOf(b.priority) || waited(b) / PRI[b.priority].sla - waited(a) / PRI[a.priority].sla;
  const recency = (a, b) => waited(a) - waited(b);
  const groups = () => {
    const xs = visible();
    if (sort === "recent") return [[null, xs.sort(recency)]];
    return ORDER.map(p => [p, xs.filter(it => it.priority === p).sort(urgency)]).filter(([, l]) => l.length);
  };
  const ordered = () => groups().flatMap(([, l]) => l);
  const byId = id => data.items.find(it => it.session_id === id);

  view.innerHTML = `<div class="ss-ib">
      <div class="ss-side">
        <div class="ss-bar"><div class="cm-seg" role="group" aria-label="Order">
          <button type="button" data-sort="pri" aria-pressed="false">By priority</button>
          <button type="button" data-sort="recent" aria-pressed="false">Most recent</button></div></div>
        <div class="ss-list" role="listbox" aria-label="Sessions waiting on you"></div>
      </div>
      <section class="ss-pane" aria-label="Session"></section></div>`;
  const listEl = view.querySelector(".ss-list"), pane = view.querySelector(".ss-pane");

  // ---------- list ----------
  function renderList() {
    view.querySelectorAll("[data-sort]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.sort === sort)));
    const gs = groups();
    if (!gs.length) { listEl.innerHTML = '<p class="cm-empty">Nothing is waiting on you.</p>'; return; }
    listEl.innerHTML = gs.map(([p, l]) => (p ? `<div class="ss-grp">${chip(p)}<span class="n">${l.length}</span></div>` : "") + l.map(it => `
      <div class="ss-it" role="option" tabindex="-1" data-id="${E(it.session_id)}" aria-selected="${it.session_id === selId}">
        <span class="t">${E(title(it))}</span>${age(it)}
        <span class="s">${E(it.ask || (it.last || "").replace(/\s+/g, " ").slice(0, 200))}</span>
        <span class="m">${p ? "" : chip(it.priority)}${it.kind ? `<span>${E(it.kind)}</span>` : ""}<span class="tabname">${E(it.tab || folder(it.folder))}</span>${it.judging ? '<span class="ss-busy">Summarising</span>' : ""}</span>
      </div>`).join("")).join("");
  }

  // ---------- pane ----------
  function renderPane() {
    const it = byId(selId);
    if (!it || pending(it)) { pane.innerHTML = visible().length ? "" : '<p class="cm-empty">Nothing is waiting on you.</p>'; return; }
    const acts = (it.actions || []).map((a, n) => `<button type="button" class="btn${n === 0 ? " primary" : ""}" data-send="${n}" data-tip="${E(a.send)}|${n + 1}">${E(a.label)}</button>`).join("");
    pane.innerHTML = `<div class="ss-body">
      <div class="ss-meta">${age(it)}${it.kind ? `<span>${E(it.kind)}</span>` : ""}<span class="tabname">tab “${E(it.tab || "untitled")}” · ${E(folder(it.folder))}</span></div>
      <h2>${E(title(it))}</h2>
      ${it.blocking ? `<p class="ss-block">${E(it.blocking)}</p>` : ""}
      <div class="ss-pris" role="group" aria-label="Priority">${ORDER.map(p => `<button type="button" class="ss-pri ${p}" data-pri="${p}" aria-pressed="${p === it.priority}">${PRI[p].label}</button>`).join("")}</div>
      ${it.ask ? `<p class="ss-ask">${E(it.ask)}</p>` : it.judging ? '<p class="ss-ask muted">Summarising…</p>' : ""}
      ${it.error && !it.judged ? `<p class="ss-err">Couldn't summarise: ${E(it.error)}</p>` : ""}
      <div class="ss-acts">${acts}
        <button type="button" class="btn ghost" data-a="focus" data-tip="Bring the tab to the front|T">${OPEN_ICON} Open tab</button>
        <button type="button" class="btn ghost" data-a="snooze" data-tip="Hide for 2 hours|Z">Snooze</button>
        <button type="button" class="btn ghost" data-a="dismiss" data-tip="Hide until it next stops|E">Done</button></div>
      <div class="ss-reply"><textarea rows="2" aria-label="Reply to this session" placeholder="Reply to the session…">${E(typed.get(it.session_id) || "")}</textarea>
        <button type="button" class="btn" data-a="send" data-tip="Send|${isMac ? "⌘↵" : "Ctrl+↵"}">Send</button></div>
      ${it.summary ? `<h3>Summary</h3><p>${E(it.summary)}</p>` : ""}
      ${(it.steps || []).length ? `<h3>What it did</h3><ol class="ss-steps">${it.steps.map(s => `<li>${E(s)}</li>`).join("")}</ol>` : ""}
      <h3>Its last message</h3><div class="ss-last">${E(it.last || "(nothing yet)")}</div></div>`;
  }

  function select(id, { scroll = true } = {}) {
    selId = id; renderList(); renderPane();
    const row = listEl.querySelector(`[data-id="${CSS.escape(id || "")}"]`);
    if (row && scroll) row.scrollIntoView({ block: "nearest" });
  }
  function move(d) {
    const xs = ordered(), i = xs.findIndex(it => it.session_id === selId);
    const next = xs[Math.max(0, Math.min(xs.length - 1, i + d))];
    if (next) select(next.session_id);
  }
  function afterLeaving(id) {
    const xs = ordered(), i = xs.findIndex(it => it.session_id === id);
    return (xs[i + 1] || xs[i - 1] || {}).session_id || null;
  }

  // ---------- actions ----------
  async function send(id, text) {
    const it = byId(id);
    if (!it || !text.trim()) return;
    const next = afterLeaving(id);
    sending.set(id, it.since); typed.delete(id);
    if (selId === id) selId = next;
    renderAll();
    try {
      const r = await api("/api/sessions/send", { session_id: id, text });
      toast(r.message);
      if (r.unconfirmed) { sending.delete(id); renderAll(); }
    } catch (e) {
      sending.delete(id); typed.set(id, text); selId = id; renderAll();
      toast("Not sent: " + e.message);
    }
  }
  async function mark(id, action, value) {
    // Durable when the desk offers it: a mark made while the server is down is sent when it is back.
    const body = { session_id: id, action, value };
    try { await (window.ftDesk?.durable ? window.ftDesk.durable("/api/sessions/mark", body, `${action[0].toUpperCase()}${action.slice(1)} “${title(byId(id) || {})}”`) : api("/api/sessions/mark", body)); }
    catch (e) { toast(e.message); }
    load(true);
  }
  function hide(id, action, value, msg) {
    const next = afterLeaving(id);
    sending.set(id, byId(id)?.since); if (selId === id) selId = next; renderAll();
    mark(id, action, value).then(() => sending.delete(id));
    toast(msg, () => mark(id, action === "dismiss" ? "undismiss" : "snooze", null));
  }
  async function act(a) {
    const it = byId(selId);
    if (!it) return;
    if (a === "focus") { try { const r = await api("/api/focus", { session_id: it.session_id }); if (r.message) toast(r.message); } catch (e) { toast(e.message); } }
    else if (a === "snooze") hide(it.session_id, "snooze", new Date(Date.now() + 2 * 3600e3).toISOString(), `Snoozed “${title(it)}” for 2 hours`);
    else if (a === "dismiss") hide(it.session_id, "dismiss", null, `“${title(it)}” hidden until it next stops`);
    else if (a === "send") send(it.session_id, pane.querySelector(".ss-reply textarea")?.value || "");
    else if (a === "reply") pane.querySelector(".ss-reply textarea")?.focus();
  }

  view.addEventListener("click", e => {
    const s = e.target.closest("[data-sort]");
    if (s) { sort = s.dataset.sort; try { localStorage.setItem("desk-sessions-sort", sort); } catch (_) {} renderAll(); return; }
    const b = e.target.closest("[data-send]");
    if (b) { const it = byId(selId); if (it) send(it.session_id, it.actions[+b.dataset.send].send); return; }
    const p = e.target.closest("[data-pri]");
    if (p) { const it = byId(selId); if (it && p.dataset.pri !== it.priority) { it.priority = p.dataset.pri; renderAll(); mark(it.session_id, "priority", p.dataset.pri); } return; }
    const a = e.target.closest("[data-a]");
    if (a) { act(a.dataset.a); return; }
    const row = e.target.closest(".ss-it");
    if (row) select(row.dataset.id, { scroll: false });
  });
  view.addEventListener("input", e => { if (e.target.matches(".ss-reply textarea")) typed.set(selId, e.target.value); });
  view.addEventListener("keydown", e => {
    if (!e.target.matches(".ss-reply textarea")) return;
    if (e.key === "Enter" && (isMac ? e.metaKey : e.ctrlKey)) { e.preventDefault(); act("send"); }
    else if (e.key === "Escape") { e.preventDefault(); e.target.blur(); }
  });
  document.addEventListener("keydown", e => {
    if (view.hidden || e.target.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (!document.querySelector(".kb-help")?.hidden) return;
    const k = e.key, it = byId(selId);
    if (/^[1-3]$/.test(k) && it?.actions?.[+k - 1]) { e.preventDefault(); send(it.session_id, it.actions[+k - 1].send); return; }
    const keys = { j: () => move(1), k: () => move(-1), ArrowDown: () => move(1), ArrowUp: () => move(-1),
      r: () => act("reply"), t: () => act("focus"), z: () => act("snooze"), e: () => act("dismiss") };
    if (keys[k]) { e.preventDefault(); keys[k](); }
  });

  // ---------- the rail ----------
  const rail = document.createElement("section");
  rail.className = "rail-wait"; rail.setAttribute("aria-labelledby", "rw-h");
  document.querySelector(".rail-tasks")?.before(rail);
  const openView = id => {
    if (id) selId = id;
    const tab = document.getElementById("tab-sessions");
    if (tab.getAttribute("aria-selected") !== "true") tab.click();
    else renderAll();
  };
  function renderRail() {
    const xs = ordered(), od = xs.filter(over), shown = xs.slice(0, 7);
    rail.innerHTML = `<h2 id="rw-h">Agents waiting <span class="n">${xs.length || ""}</span>
        <button type="button" class="rw-open" data-tip="Open Agents waiting|Y" aria-label="Open Agents waiting">${EXPAND_ICON}</button></h2>
      ${xs.length ? `<div class="cm-seg rw-seg" role="group" aria-label="Order"><button type="button" data-sort="pri" aria-pressed="${sort === "pri"}">Priority</button><button type="button" data-sort="recent" aria-pressed="${sort === "recent"}">Recent</button></div>` : ""}
      ${od.length ? `<button type="button" class="rw-over">${od.length} overdue</button>` : ""}
      <ul>${shown.map(it => `<li><button type="button" data-id="${E(it.session_id)}"><span class="d ${it.priority}" aria-hidden="true"></span><span class="l">${E(title(it))}</span><span class="m">${over(it) ? `<b>Overdue ${fmt(waited(it))}</b>` : fmt(waited(it))}${it.ask ? " · " + E(it.ask) : ""}</span></button></li>`).join("")}</ul>
      ${xs.length > shown.length ? `<button type="button" class="rw-more">${xs.length - shown.length} more</button>` : ""}
      ${!xs.length && loaded ? '<p class="tasks-empty">Nothing waiting.</p>' : ""}`;
  }
  rail.addEventListener("click", e => {
    const s = e.target.closest("[data-sort]");
    if (s) { sort = s.dataset.sort; try { localStorage.setItem("desk-sessions-sort", sort); } catch (_) {} renderAll(); return; }
    const row = e.target.closest("[data-id]");
    if (row) { openView(row.dataset.id); return; }
    if (e.target.closest(".rw-over")) { openView(ordered().find(over)?.session_id); return; }
    if (e.target.closest(".rw-open, .rw-more")) openView();
  });
  document.addEventListener("keydown", e => {
    if (e.target.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "y" || e.key === "Y") { e.preventDefault(); document.getElementById(view.hidden ? "tab-sessions" : "tab-desk").click(); }
  });

  // ---------- data ----------
  function renderAll() {
    if (!byId(selId) || pending(byId(selId))) selId = ordered()[0]?.session_id || null;
    renderList(); renderPane(); renderRail();
    const n = visible().length;
    tabN.textContent = n; tabN.hidden = n === 0;
  }
  function place() {
    const top = view.getBoundingClientRect().top + scrollY;
    view.style.setProperty("--cm-top", Math.max(120, Math.round(top + 64)) + "px");
  }
  async function load(force) {
    try {
      const d = await api("/api/sessions");
      if (!force && d.hash === hash && loaded) return;
      const keepTyping = document.activeElement?.matches(".ss-reply textarea");
      data = d; hash = d.hash; loaded = true;
      if (keepTyping) { renderList(); renderRail(); } else renderAll();
    } catch (e) { if (!loaded) pane.innerHTML = `<p class="cm-empty">Couldn't load sessions: ${E(e.message)}</p>`; }
  }
  window.addEventListener("desk:sync", e => { if (e.detail.sessions_hash && e.detail.sessions_hash !== hash) load(); });
  setInterval(() => { if (loaded) { renderRail(); if (!view.hidden && !document.activeElement?.matches(".ss-reply textarea")) renderList(); } }, 60000);
  new MutationObserver(() => { if (!view.hidden) { place(); renderAll(); } }).observe(view, { attributes: true, attributeFilter: ["hidden"] });
  addEventListener("resize", () => { if (!view.hidden) place(); });
  load();
})();
