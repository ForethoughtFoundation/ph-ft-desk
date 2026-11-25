// Comments view: comment threads and suggested edits on Google Docs and Sheets, with reply and resolve.
// Data comes from desk_comments.py through the server; the desk's sync tells this view when it has changed.
(() => {
  const view = document.getElementById("view-comments");
  if (!view) return;
  const TOKEN = document.querySelector('meta[name="desk-token"]').content;
  const tab = document.getElementById("tab-comments"), tabN = document.getElementById("comments-n");
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
    clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), undo ? 6000 : 3200);
  };
  const ICON = {
    reply: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.5 4 2.5 8l4 4M3 8h6.5a4 4 0 0 1 4 4v1" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    resolve: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="m5.5 8.2 1.8 1.8 3.3-3.6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    open: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 3h4v4M13 3 7.5 8.5M11.5 9.5V13H3V4.5h3.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    start: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 2l7 4-7 4z" fill="currentColor"/></svg>',
    archive: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4.5h11v2h-11zM3.5 6.5v6h9v-6M6.5 9h3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
    back: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m10 4-4 4 4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };
  const PALETTE = ["#325e76", "#76419c", "#266433", "#822d2e", "#8a5a12", "#5e6ad2", "#a92817", "#3d6b6b"];
  const initials = n => (n || "?").split(/\s+/).filter(Boolean).map(w => w[0]).slice(0, 2).join("").toUpperCase();
  const colour = n => PALETTE[[...(n || "")].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % PALETTE.length];
  const av = (name, me) => `<span class="cm-av" style="--c:${me ? "var(--ink-muted)" : colour(name)}" aria-hidden="true">${E(initials(name))}</span>`;
  const first = n => (n || "").split(/\s+/)[0];
  const when = t => {
    const d = new Date(t * 1000), now = new Date();
    const hm = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return hm;
    if (now - d < 6 * 86400e3) return d.toLocaleDateString("en-GB", { weekday: "short" }) + " " + hm;
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  };

  let data = { items: [], files: {} }, hash = null, loaded = false;
  let filter = "wait", selKey = null, paneKey = null;
  const typed = new Map();          // key -> what Peter has typed in the reply box
  const docs = new Map();           // file -> blocks, cleared whenever the data changes
  const pending = new Set();        // keys with an action in flight

  view.innerHTML = `<div class="cm-bar"><div class="cm-seg" role="group" aria-label="Show">
      <button type="button" data-f="wait" data-tip="Waiting on you|1" aria-pressed="true">Waiting on you <span class="n"></span></button>
      <button type="button" data-f="fyi" data-tip="FYI|2" aria-pressed="false">FYI <span class="n"></span></button>
      <button type="button" data-f="done" data-tip="Done|3" aria-pressed="false">Done</button></div></div>
    <div class="cm-ib"><div class="cm-list" role="listbox" aria-label="Comment threads"></div><section class="cm-pane" aria-label="Thread"></section></div>`;
  const listEl = view.querySelector(".cm-list"), pane = view.querySelector(".cm-pane");

  const inFilter = (it, f) => f === "done" ? it.handled : !it.handled && (f === "wait" ? it.waiting : !it.waiting);
  const fileName = fid => (data.files[fid] || {}).name || "Untitled";
  const items = () => {
    const xs = data.items.filter(it => inFilter(it, filter));
    const recent = it => Math.max(it.last_other || 0, it.last_me || 0, it.ts || 0);
    const fileTime = {};
    xs.forEach(it => (fileTime[it.file] = Math.max(fileTime[it.file] || 0, recent(it))));
    const pos = it => it.block ?? 1e9;
    return xs.sort((a, b) => filter === "done" ? recent(b) - recent(a)
      : (fileTime[b.file] - fileTime[a.file]) || a.file.localeCompare(b.file) || pos(a) - pos(b) || recent(b) - recent(a));
  };
  const byKey = k => data.items.find(it => it.key === k);
  const others = it => it.type === "suggestion" ? [it.author] : [...new Set(it.msgs.filter(m => !m.me).map(m => m.author))];
  const preview = it => {
    if (it.type === "suggestion") return (it.del ? `Delete “${it.del}”` : "") + (it.del && it.ins ? ", " : "") + (it.ins ? `insert “${it.ins}”` : "");
    const o = it.msgs.filter(m => !m.me && m.text);
    return (o.find(m => m.ts > Math.max(it.read_at || 0, it.last_me || 0)) || o.find(m => m.ts > (it.last_me || 0)) || o[o.length - 1] || it.msgs[it.msgs.length - 1] || {}).text || "";
  };

  // ---------- list ----------
  function renderList() {
    const xs = items();
    const counts = { wait: 0, fyi: 0 };
    data.items.forEach(it => { if (!it.handled) counts[it.waiting ? "wait" : "fyi"]++; });
    view.querySelectorAll(".cm-seg button").forEach(b => {
      b.setAttribute("aria-pressed", String(b.dataset.f === filter));
      const n = b.querySelector(".n"); if (n) n.textContent = counts[b.dataset.f] || "";
    });
    tabN.textContent = counts.wait; tabN.hidden = !counts.wait;
    if (!xs.length) { listEl.innerHTML = `<p class="cm-empty">${filter === "wait" ? "Nothing waiting on you" : "Nothing here"}</p>`; return; }
    let html = "", cur = null;
    if (filter === "done" && xs.length > 150) xs.length = 150;
    for (const it of xs) {
      if (filter !== "done" && it.file !== cur) {
        cur = it.file;
        const f = data.files[it.file] || {};
        html += `<div class="cm-grp" data-file="${E(it.file)}"><span class="cm-grp-t">${f.kind === "sheet" ? '<span class="cm-kind">Sheet</span> ' : ""}${E(fileName(it.file))}</span>
          <span class="cm-grp-a"><button type="button" class="cm-ico" data-a="file-open" data-tip="Open the file" aria-label="Open the file">${ICON.open}</button>
          <button type="button" class="cm-ico" data-a="file-start" data-tip="Start a Claude tab on every open thread here" aria-label="Start on this file">${ICON.start}</button></span></div>`;
      }
      const who = others(it), lm = it.type === "suggestion" ? { author: it.author } : (it.msgs.filter(m => !m.me).pop() || it.msgs[it.msgs.length - 1] || {});
      const latest = lm.author;
      const chips = [it.mentions_me ? "Mentions you" : "", it.resolved ? "Resolved" : ""].filter(Boolean);
      html += `<div class="cm-it${it.unread ? " unread" : ""}" role="option" tabindex="-1" id="cm-${E(it.key)}" data-key="${E(it.key)}" aria-selected="${it.key === selKey}">
        ${av(latest || "?", !!lm.me)}<span class="cm-who">${E(who.map(first).join(", ") || "You")}${filter === "done" ? ` <span class="cm-in">${E(fileName(it.file))}</span>` : ""}</span><time>${E(when(Math.max(it.last_other || 0, it.last_me || 0, it.ts || 0)))}</time>
        <span class="cm-txt">${E(preview(it))}</span>
        ${it.anchor || it.quoted ? `<span class="cm-q">${E(it.quoted && it.quoted.length > 2 ? it.quoted : it.anchor)}</span>` : ""}
        ${chips.length ? `<span class="cm-chips">${chips.map(c => `<span class="cm-chip">${c}</span>`).join("")}</span>` : ""}
      </div>`;
    }
    listEl.innerHTML = html;
    listEl.setAttribute("aria-activedescendant", selKey ? "cm-" + selKey : "");
  }

  // ---------- reading pane ----------
  function ctxHTML(it, blocks) {
    if (!blocks || !blocks.length) return "";
    const on = new Set(it.blocks || (it.block != null ? [it.block] : []));
    const lo = Math.max(0, (it.block ?? 0) - 80), hi = Math.min(blocks.length, (it.block ?? 0) + 81);
    const q = it.quoted && it.quoted.length > 2 ? it.quoted : "";
    let out = "";
    for (let i = lo; i < hi; i++) {
      const b = blocks[i];
      let t = E(b.t);
      if (on.has(i)) {
        const qi = q ? b.t.indexOf(q) : -1;
        t = qi >= 0 ? E(b.t.slice(0, qi)) + `<mark>${E(q)}</mark>` + E(b.t.slice(qi + q.length)) : `<mark class="soft">${t}</mark>`;
      }
      const cls = b.k === "h" ? `cm-h cm-h${Math.min(b.lv || 1, 3)}` : b.k === "li" ? "cm-li" : "";
      out += `<p class="${cls}${on.has(i) ? " on" : ""}"${b.k === "li" ? ` style="--lv:${b.lv || 0}"` : ""} data-b="${i}">${t}</p>`;
    }
    return out;
  }

  function threadHTML(it) {
    let out = "", divided = false;
    it.msgs.forEach((m, i) => {
      if (!divided && it.read_at && !m.me && m.ts > it.read_at && i > 0) { out += `<div class="cm-new">New</div>`; divided = true; }
      const prev = it.msgs[i - 1];
      const cont = prev && prev.author === m.author && !(out.endsWith('New</div>'));
      const body = m.text ? E(m.text).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
        : `<span class="cm-act">${m.action === "resolve" ? "Resolved" : m.action === "reopen" ? "Reopened" : E(m.action || "")}</span>`;
      out += cont ? `<div class="cm-msg cont"><span></span><div class="cm-b">${body}</div></div>`
        : `<div class="cm-msg">${av(m.author, m.me)}<div><div class="cm-mh"><b>${E(m.me ? "You" : m.author)}</b><time>${E(when(m.ts))}</time></div><div class="cm-b">${body}</div></div></div>`;
    });
    return out;
  }

  async function renderPane(force) {
    const it = byKey(selKey);
    if (!it) { pane.innerHTML = `<p class="cm-empty">${data.items.length ? "" : "Loading…"}</p>`; paneKey = null; return; }
    const ta = pane.querySelector("textarea");
    if (!force && paneKey === it.key && ta && (document.activeElement === ta || ta.value !== ta.defaultValue)) {
      pane.querySelector(".cm-thread")?.replaceChildren(...new DOMParser().parseFromString(threadHTML(it), "text/html").body.childNodes);
      return;
    }
    paneKey = it.key;
    const f = data.files[it.file] || {};
    const sug = it.type === "suggestion";
    const draft = typed.has(it.key) ? typed.get(it.key) : (it.draft || "");
    const fromClaude = !typed.has(it.key) && it.draft && !it.resolved;
    pane.innerHTML = `
      <div class="cm-top">
        <button type="button" class="btn ghost sm cm-back" data-a="back" data-tip="Back|Esc">${ICON.back}<span class="l"> Back</span></button>
        ${sug ? "" : `<button type="button" class="btn ghost sm" data-a="reply" data-tip="Reply|R">${ICON.reply}<span class="l"> Reply</span></button>
        <button type="button" class="btn ghost sm" data-a="${it.resolved ? "reopen" : "resolve"}" data-tip="${it.resolved ? "Reopen" : "Resolve"}|⇧D">${ICON.resolve}<span class="l"> ${it.resolved ? "Reopen" : "Resolve"}</span></button>`}
        <button type="button" class="btn ghost sm" data-a="${it.archived ? "unarchive" : "archive"}" data-tip="${it.archived ? "Back to the inbox" : "Archive"}|E">${ICON.archive}<span class="l"> ${it.archived ? "Restore" : "Archive"}</span></button>
        <span class="sp"></span>
        <button type="button" class="btn ghost sm" data-a="open" data-tip="Open it in the ${f.kind === "sheet" ? "Sheet" : "Doc"}|O">${ICON.open}<span class="l"> Open in ${f.kind === "sheet" ? "Sheet" : "Doc"}</span></button>
        <button type="button" class="btn sm" data-a="start" data-tip="Start a Claude Code tab|S">${ICON.start}<span class="l"> Start</span></button>
      </div>
      <div class="cm-body">
        <h2 class="cm-file"><a href="${E(f.url || it.link)}" target="_blank" rel="noopener">${E(fileName(it.file))}</a></h2>
        ${it.section ? `<p class="cm-meta">${E(it.section)}</p>` : ""}
        ${f.kind === "sheet" ? (it.quoted ? `<div class="cm-ctx cm-cell">${E(it.quoted)}</div>` : "")
          : it.block == null ? (it.quoted && it.quoted.length > 2 ? `<div class="cm-ctx cm-cell"><p class="on"><mark>${E(it.quoted)}</mark></p></div>` : "")
          : `<div class="cm-ctx" tabindex="0" aria-label="The Doc around this ${sug ? "edit" : "comment"}"><p class="cm-none">Loading the Doc…</p></div>`}
        ${sug ? `<div class="cm-sug"><div class="cm-mh">${av(it.author)}<b>${E(it.author)}</b><time>${E(when(it.ts))}</time></div>
            <p>${it.del ? `<del>${E(it.del)}</del>` : ""}${it.del && it.ins ? " " : ""}${it.ins ? `<ins>${E(it.ins)}</ins>` : ""}</p></div>`
          : `<div class="cm-thread">${threadHTML(it)}</div>
        ${it.resolved ? "" : `<div class="cm-reply">${fromClaude ? `<p class="cm-draft-l">Draft by Claude</p>` : ""}
          <textarea rows="3" placeholder="Reply to ${E(others(it).map(first).join(" and ") || "the thread")}" aria-label="Reply">${E(draft)}</textarea>
          <div class="cm-reply-f"><label><input type="checkbox" class="cm-andres"> Resolve</label><span class="sp"></span>
          <button type="button" class="btn primary sm" data-a="send" data-tip="Send|${isMac ? "⌘↵" : "Ctrl+↵"}">Send</button></div></div>`}`}
      </div>`;
    const t2 = pane.querySelector("textarea"); if (t2) t2.defaultValue = draft;
    if (f.kind !== "sheet" && it.block != null) {
      let blocks = docs.get(it.file);
      if (!blocks) {
        try { blocks = (await api("/api/comments/doc", { file: it.file })).blocks; docs.set(it.file, blocks); } catch { blocks = []; }
      }
      if (paneKey !== it.key) return;
      const box = pane.querySelector(".cm-ctx");
      box.innerHTML = ctxHTML(it, blocks) || `<p class="cm-none">Not found in the Doc</p>`;
      box.dataset.placed = "";
      requestAnimationFrame(scrollCtx);
    }
  }
  // The window scrolls to the passage only while it can be measured; a hidden pane does it when it is shown.
  function scrollCtx() {
    const box = pane.querySelector(".cm-ctx"), a = box?.querySelector("p.on");
    if (!a || !box.clientHeight || box.dataset.placed) return;
    box.scrollTop = a.offsetTop - box.clientHeight * 0.3;
    box.dataset.placed = "1";
  }

  // ---------- selection ----------
  let readTimer = 0;
  function select(key, { open = true } = {}) {
    selKey = key;
    listEl.querySelectorAll(".cm-it").forEach(el => el.setAttribute("aria-selected", String(el.dataset.key === key)));
    listEl.setAttribute("aria-activedescendant", key ? "cm-" + key : "");
    const el = key && document.getElementById("cm-" + key);
    el?.scrollIntoView({ block: "nearest" });
    if (open && matchMedia("(max-width: 900px)").matches) view.classList.add("cm-reading");
    renderPane(true);
    clearTimeout(readTimer);
    const it = byKey(key);
    if (it && it.unread) readTimer = setTimeout(() => mark(it, "read"), 1200);
  }
  const visibleKeys = () => [...listEl.querySelectorAll(".cm-it")].map(el => el.dataset.key);
  function move(d) {
    const ks = visibleKeys(); if (!ks.length) return;
    const i = ks.indexOf(selKey);
    select(ks[i < 0 ? 0 : Math.max(0, Math.min(ks.length - 1, i + d))]);
  }
  const neighbour = key => { const ks = visibleKeys(), i = ks.indexOf(key); return ks[i + 1] || ks[i - 1] || null; };

  // ---------- actions ----------
  async function mark(it, action) {
    const was = { ...it };
    if (action === "read") { it.unread = false; it.read_at = Date.now() / 1000; }
    if (action === "unread") it.unread = true;
    if (action === "archive") { it.archived = true; it.handled = true; it.unread = false; }
    if (action === "unarchive") { it.archived = false; it.handled = !!(it.resolved || (it.msgs.length && it.msgs[it.msgs.length - 1].me)); }
    renderList();
    try { await api("/api/comments/act", { key: it.key, action }); }
    catch (e) { Object.assign(it, was); renderList(); toast("Couldn't save that: " + e.message); }
  }

  async function act(a, key = selKey) {
    const it = byKey(key);
    if (a === "back") { view.classList.remove("cm-reading"); return; }
    if (!it) return;
    const f = data.files[it.file] || {};
    if (a === "reply") { const t = pane.querySelector("textarea"); t?.focus(); t?.setSelectionRange(t.value.length, t.value.length); return; }
    if (a === "open") { window.open(it.link || f.url, "_blank"); return; }
    if (a === "start") return start("comment:" + it.key, `${fileName(it.file)}: ${preview(it).slice(0, 60)}`);
    if (a === "archive" || a === "unarchive") {
      const next = a === "archive" && filter !== "done" ? neighbour(key) : key;
      await mark(it, a);
      if (a === "archive") { select(inFilter(it, filter) ? key : next, { open: false }); toast("Archived", () => mark(it, "unarchive").then(() => select(key))); }
      else { toast("Back in the inbox"); renderPane(true); }
      return;
    }
    if (a === "unread") { mark(it, "unread"); clearTimeout(readTimer); toast("Marked unread"); return; }
    if (a === "resolve" || a === "reopen" || a === "send") {
      if (pending.has(key)) return;
      const ta = pane.querySelector("textarea");
      const text = a === "send" ? (ta?.value || "").trim() : "";
      if (a === "send" && !text) { ta?.focus(); return; }
      const resolve = a === "resolve" || (a === "send" && pane.querySelector(".cm-andres")?.checked);
      const action = a === "send" ? (resolve ? "resolve" : "reply") : a;
      pending.add(key);
      pane.querySelectorAll("[data-a]").forEach(b => (b.disabled = true));
      try {
        await api("/api/comments/act", { key, action, text });
        typed.delete(key);
        const now = Date.now() / 1000;
        if (text) { it.msgs.push({ author: "You", me: true, ts: now, text }); it.last_me = now; }
        if (action === "resolve") it.resolved = true;
        if (action === "reopen") it.resolved = false;
        it.handled = !!(it.resolved || it.archived || (it.msgs.length && it.msgs[it.msgs.length - 1].me));
        it.unread = false;
        const next = it.handled && filter !== "done" ? neighbour(key) : key;
        renderList();
        select(inFilter(it, filter) ? key : next, { open: false });
        toast(text ? (resolve ? "Replied and resolved" : "Replied") : action === "resolve" ? "Resolved" : "Reopened");
      } catch (e) {
        toast("Couldn't post: " + e.message);
        pane.querySelectorAll("[data-a]").forEach(b => (b.disabled = false));
      } finally { pending.delete(key); }
    }
  }

  async function start(key, label) {
    try { toast((await api("/api/start", { items: [{ key, note: "", label }], mode: "each" })).message || "Started"); }
    catch (e) { toast("Couldn't start: " + e.message); }
  }

  // ---------- events ----------
  view.addEventListener("click", e => {
    const b = e.target.closest("[data-a]");
    if (b && listEl.contains(b)) {
      e.stopPropagation();
      const fid = b.closest(".cm-grp").dataset.file;
      if (b.dataset.a === "file-open") window.open((data.files[fid] || {}).url, "_blank");
      else start("comments-file:" + fid, fileName(fid));
      return;
    }
    if (b) { act(b.dataset.a); return; }
    const f = e.target.closest(".cm-seg button");
    if (f) { setFilter(f.dataset.f); return; }
    const row = e.target.closest(".cm-it");
    if (row) select(row.dataset.key);
  });
  view.addEventListener("input", e => { if (e.target.matches(".cm-reply textarea")) { typed.set(selKey, e.target.value); pane.querySelector(".cm-draft-l")?.remove(); } });
  view.addEventListener("keydown", e => {
    if (!e.target.matches(".cm-reply textarea")) return;
    if (e.key === "Enter" && (isMac ? e.metaKey : e.ctrlKey)) { e.preventDefault(); act("send"); }
    else if (e.key === "Escape") { e.preventDefault(); e.target.blur(); }
  });
  function setFilter(f) {
    filter = f; renderList();
    const ks = visibleKeys();
    select(ks.includes(selKey) ? selKey : ks[0] || null, { open: false });
  }
  document.addEventListener("keydown", e => {
    if (view.hidden || e.target.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (!document.querySelector(".kb-help")?.hidden) return;
    const k = e.key;
    const keys = { j: () => move(1), k: () => move(-1), ArrowDown: () => move(1), ArrowUp: () => move(-1),
      "1": () => setFilter("wait"), "2": () => setFilter("fyi"), "3": () => setFilter("done"),
      r: () => act("reply"), D: () => act(byKey(selKey)?.resolved ? "reopen" : "resolve"),
      e: () => act(byKey(selKey)?.archived ? "unarchive" : "archive"), u: () => act("unread"),
      o: () => act("open"), s: () => act("start"), Escape: () => view.classList.remove("cm-reading") };
    if (keys[k]) { e.preventDefault(); keys[k](); }
  });

  // ---------- data ----------
  function place() {
    const top = view.getBoundingClientRect().top + scrollY;
    view.style.setProperty("--cm-top", Math.max(120, Math.round(top + 64)) + "px");
  }
  async function load() {
    try {
      const d = await api("/api/comments");
      if (d.hash === hash && loaded) return;
      data = d; hash = d.hash; loaded = true; docs.clear();
      renderList();
      if (!(selKey && byKey(selKey))) { selKey = visibleKeys()[0] || null; renderList(); }
      renderPane(false);
    } catch (e) { if (!loaded) pane.innerHTML = `<p class="cm-empty">Couldn't load comments: ${E(e.message)}</p>`; }
  }
  window.addEventListener("desk:sync", e => { if (e.detail.comments_hash && e.detail.comments_hash !== hash) load(); });
  new MutationObserver(() => {
    document.body.classList.toggle("cm-on", !view.hidden);
    if (!view.hidden) { place(); if (!loaded) load(); requestAnimationFrame(scrollCtx); }
  }).observe(view, { attributes: true, attributeFilter: ["hidden", "class"] });
  addEventListener("resize", () => { if (!view.hidden) place(); });
  load();
})();
