// Live desk: Start / Comment / Queue on items, the Claude tasks list, keyboard shortcuts.
// Talks only to desk_server.py on the same origin, with the page's token.
(() => {
  const TOKEN = document.querySelector('meta[name="desk-token"]').content;
  const ASSETS = document.querySelector('meta[name="desk-assets"]')?.content;
  const ICON = {
    play: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 2l7 4-7 4z" fill="currentColor"/></svg>',
    plus: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2v8M2 6h8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    comment: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 2.5h8v5.5H5L2.5 10V8H2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
    archive: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M1.75 2.5h8.5v2h-8.5zM2.5 4.5v5h7v-5M4.75 6.5h2.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" stroke-linecap="round"/></svg>',
    open: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M5 2.5H2.5v7h7V7M7 2.5h2.5V5M9.5 2.5 5.5 6.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };
  const isMac = navigator.platform.includes("Mac");
  // Call before moving DOM that may hold the focused field; the returned function puts focus and caret back.
  const keepFocus = () => {
    const f = document.activeElement;
    if (!f?.matches?.("input, textarea")) return () => {};
    const { selectionStart: a, selectionEnd: b, selectionDirection: d } = f;
    return () => {
      if (!f.isConnected || document.activeElement === f) return;
      f.focus({ preventScroll: true });
      try { f.setSelectionRange(a, b, d); } catch { /* inputs such as datetime-local have no caret */ }
    };
  };

  // Backstop: if a field loses focus mid-word (the page changed around it), the next letters go back to the
  // field instead of firing shortcuts. Runs on window in the capture phase, so before every shortcut handler.
  {
    const FIELD = "input, textarea, select, [contenteditable]";
    let last = null, at = 0, caret = null;
    addEventListener("input", e => {
      if (!e.target.matches?.("input, textarea")) return;
      last = e.target; at = Date.now(); caret = [e.target.selectionStart, e.target.selectionEnd];
    }, true);
    addEventListener("mousedown", () => { last = null; }, true);
    addEventListener("keydown", e => {
      if (e.target.closest?.(FIELD)) { if (["Escape", "Tab", "Enter"].includes(e.key)) last = null; return; }
      if (!last || e.key.length !== 1 || e.metaKey || e.ctrlKey || e.altKey) return;
      if (Date.now() - at > 2000 || !last.isConnected) { last = null; return; }
      e.stopImmediatePropagation();
      last.focus({ preventScroll: true });   // the key's character then lands in the field
      try { last.setSelectionRange(...caret); } catch { /* no caret */ }
    }, true);
  }

  const api = async (path, body) => {
    const r = await fetch(path, body === undefined ? { headers: { "X-Desk-Token": TOKEN } } : {
      method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN }, body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  };
  const toast = (msg, action) => {
    const t = document.getElementById("toast");
    t.textContent = msg; t.hidden = false;
    if (action) {
      const b = document.createElement("button"); b.type = "button"; b.className = "toast-act"; b.textContent = action.label;
      b.addEventListener("click", () => { t.hidden = true; action.run(); });
      t.append(" ", b);
    }
    clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), action ? 6000 : 3200);
  };
  const fail = e => toast("Couldn't reach the desk server: " + e.message);
  // A change the desk makes in Linear: the projects region isn't swapped while one is in flight.
  let writes = 0;
  const write = async (path, body) => {
    writes++;
    try { return await api(path, body); }
    finally { writes--; setTimeout(() => sync(), 250); }
  };

  // ---------- durable writes ----------
  // A change (a Linear edit, a snooze) that can't reach the desk server waits in this browser's storage and is sent,
  // in order, once the server answers again; the server then holds it until Linear answers (desk_linear.py). Each
  // carries an id, so a resend after a lost answer is applied once. While anything waits here, later changes queue
  // behind it, so they keep their order.
  const OUT_KEY = "ftdesk.outbox";
  const outGet = () => { try { return JSON.parse(localStorage.getItem(OUT_KEY) || "[]"); } catch { return []; } };
  const outSet = v => { try { v.length ? localStorage.setItem(OUT_KEY, JSON.stringify(v)) : localStorage.removeItem(OUT_KEY); } catch { /* private mode */ } };
  const uid = () => (crypto.randomUUID ? crypto.randomUUID() : "d-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12));
  class ServerDown extends Error {}
  const post = async (path, body) => {
    let r;
    try { r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN }, body: JSON.stringify(body) }); }
    catch (e) { throw new ServerDown(e.message); }
    if (r.status >= 502) throw new ServerDown(r.statusText);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  };
  const outChanged = () => window.dispatchEvent(new CustomEvent("desk:outbox", { detail: outGet() }));
  let flushing = null;
  const flushOutbox = () => flushing || (flushing = (async () => {
    try {
      for (let q = outGet(); q.length; q = outGet()) {
        const it = q[0];
        let r;
        try { r = await post(it.path, it.body); }
        catch (e) {
          if (e instanceof ServerDown) return false;
          r = { status: "refused", error: e.message };
        }
        outSet(outGet().filter(x => x.id !== it.id)); outChanged();
        if (r?.status === "refused") toast(`Couldn't apply “${it.label}”: ${r.error}`);
      }
      return true;
    } finally { flushing = null; }
  })());
  // Send a change now, or keep it here when the server can't be reached. Resolves to the server's answer, or
  // {status: "queued", local: true}; rejects only when the server refused the request itself.
  const durable = async (path, body, label) => {
    const it = { id: body.id || uid(), path, body: { ...body }, label: label || path, at: Date.now() };
    if (outGet().length) {
      outSet([...outGet(), it]); outChanged(); flushOutbox();
      return { status: "queued", local: true };
    }
    try { return await post(path, it.body); }
    catch (e) {
      if (!(e instanceof ServerDown)) throw e;
      outSet([...outGet(), it]); outChanged();
      return { status: "queued", local: true };
    }
  };
  // A Linear change through the outboxes. Rejects when Linear refused it, so the caller can undo what it showed.
  const linearWrite = async (issue, change, label) => {
    writes++;
    try {
      const r = await durable("/api/linear/write", { id: uid(), issue, ...change }, label || issue);
      if (r.status === "refused") throw new Error(r.error || "Linear refused it");
      return r;
    } finally { writes--; setTimeout(() => sync(), 250); }
  };
  const queuedNote = r => (r.status === "queued" ? " · waiting to sync" : "");
  window.ftDesk = Object.assign(window.ftDesk || {}, { durable, linearWrite, toast, outbox: outGet, flushOutbox, uid });

  // ---------- items ----------
  // bind() prepares rows and cards the server has just rendered; it runs at load and after every region swap.
  // Everything else listens on the document, so swapped-in elements need nothing more.
  const bind = () => {
    document.querySelectorAll(".cards li:not(.card-wrap):not(.add-card)").forEach(li => {
      li.classList.add("card-wrap");
      li.dataset.key = "issue:" + li.querySelector(".card-id").textContent;
      li.draggable = true;
      li.querySelector(".card").draggable = false;
    });
    document.querySelectorAll(".dd[data-key]").forEach(el => {
      if (el.querySelector(":scope > .acts")) return;
      const acts = document.createElement("div");
      acts.className = "acts";
      acts.innerHTML = `<button type="button" class="cm" data-tip="Comment, then start">${ICON.comment} Comment</button>` +
        `<button type="button" class="go" data-tip="Start a Claude Code tab">${ICON.play} Start</button>`;
      el.appendChild(acts);
    });
    const noise = document.querySelector("#archive > .sec-h");
    if (noise && !noise.querySelector(".sec-acts")) {
      const a = document.createElement("span");
      a.className = "sec-acts"; a.dataset.noEdit = "";
      a.innerHTML = `<button type="button" class="btn sm" data-sa="pick">Select all</button>` +
        `<button type="button" class="btn sm" data-sa="archive">${ICON.archive} Archive all</button>`;
      noise.appendChild(a);
    }
    document.querySelectorAll(".row, .card-wrap").forEach(el => {
      if (el.querySelector(":scope > .acts")) return;
      const acts = document.createElement("div");
      acts.className = "acts";
      acts.innerHTML = `<button type="button" class="cm" data-tip="Comment, then start|M">${ICON.comment} Comment</button>` +
        `<button type="button" class="go" data-tip="Start a Claude Code tab|S">${ICON.play} Start</button>` +
        `<button type="button" class="q" data-tip="Add to the queue|⇧Q">${ICON.plus} Queue</button>` +
        (el.closest("#let-go") ? `<button type="button" class="kp" data-tip="Keep it, under Still open">${ICON.plus} Keep</button>` : "") +
        (el.classList.contains("row") ? `<button type="button" class="ar" data-tip="${el.dataset.key?.startsWith("gmail:") || /^[0-9a-f]{16}$/.test(el.dataset.key || "") ? "Archive in Gmail" : "Archive"}|E">${ICON.archive} Archive</button>` : "");
      el.appendChild(acts);
    });
  };
  bind();
  const items = () => [...document.querySelectorAll(".row, .card-wrap, .dd[data-key]")];
  const byKey = key => document.querySelector(`[data-key="${CSS.escape(key)}"]`);
  const labelOf = el => el.classList.contains("card-wrap")
    ? el.querySelector(".card-id").textContent + " " + el.querySelector(".card-title").textContent
    : el.classList.contains("dd") ? el.dataset.key.slice(6) + " " + el.querySelector(".due-txt").textContent
    : "#" + el.querySelector(".num").textContent + " " + (el.querySelector(".head")?.textContent.replace(/:$/, "") || el.querySelector(".text").textContent.slice(0, 60));

  const setRun = (el, html, cls = "") => {
    let r = el.querySelector(".run");
    if (!r) { r = document.createElement("span"); (el.querySelector(".text") || el.querySelector(".card") || el.querySelector(".due-txt")).appendChild(r); }
    r.className = "run " + cls; r.innerHTML = html;
    return r;
  };
  const addNote = (el, text) => {
    el.querySelector(".note")?.remove();
    if (!text) return;
    const n = document.createElement("span"); n.className = "note"; n.textContent = text;
    (el.querySelector(".text") || el.querySelector(".card") || el.querySelector(".due-txt")).appendChild(n);
  };

  // ---------- queue ----------
  let queue = new Map(); // key -> note
  const tray = document.getElementById("tray"), list = document.getElementById("tray-list");
  const renderTray = () => {
    if (list.contains(document.activeElement)) return;  // Peter is typing an instruction: rebuilding would wipe it
    tray.hidden = queue.size === 0;
    document.getElementById("tray-n").textContent = queue.size;
    list.innerHTML = "";
    items().forEach(el => { if (!queue.has(el.dataset.key) && el.classList.contains("queued")) { el.classList.remove("queued"); el.querySelector(".run.queued")?.remove(); } });
    queue.forEach((note, key) => {
      const el = byKey(key);
      if (el) { el.classList.add("queued"); if (!el.querySelector(".run:not(.queued)")) setRun(el, "Queued", "queued"); if (note && !el.querySelector(".note")) addNote(el, note); }
      const li = document.createElement("li");
      li.innerHTML = `<span class="lbl"></span><button class="rm" type="button" aria-label="Remove from queue">×</button><input type="text" placeholder="Instructions (optional)">`;
      li.querySelector(".lbl").textContent = el ? labelOf(el) : key;
      const inp = li.querySelector("input"); inp.value = note;
      inp.addEventListener("change", () => { queue.set(key, inp.value); api("/api/queue", { key, note: inp.value, label: el ? labelOf(el) : key }).catch(fail); });
      li.querySelector(".rm").onclick = () => unqueue(key);
      list.appendChild(li);
    });
  };
  const enqueue = (el, note = "") => {
    queue.set(el.dataset.key, note); addNote(el, note); renderTray();
    api("/api/queue", { key: el.dataset.key, note, label: labelOf(el) }).catch(fail);
  };
  const unqueue = key => { queue.delete(key); renderTray(); api("/api/unqueue", { key }).catch(fail); };
  document.getElementById("tray-min").addEventListener("click", e => {
    const min = tray.classList.toggle("min");
    e.target.textContent = min ? "Show" : "Hide"; e.target.setAttribute("aria-expanded", String(!min));
  });

  // ---------- start ----------
  const start = async (entries, mode = "each") => {
    // entries: [{key, note, label}]
    entries.forEach(({ key, note }) => { const el = byKey(key); if (el) { addNote(el, note); setRun(el, "Opening a Claude Code tab…"); } });
    try {
      const res = await api("/api/start", { mode, items: entries });
      entries.forEach(({ key }) => queue.delete(key));
      renderTray();
      toast(res.message || "Started");
      sync();
    } catch (e) {
      entries.forEach(({ key }) => byKey(key)?.querySelector(".run")?.remove());
      fail(e);
    }
  };
  const startOne = (el, note) => start([{ key: el.dataset.key, note: note ?? queue.get(el.dataset.key) ?? "", label: labelOf(el) }]);
  document.getElementById("tray-send").addEventListener("click", () => {
    const mode = document.getElementById("tray-mode").value;
    start([...queue].map(([key, note]) => ({ key, note, label: byKey(key) ? labelOf(byKey(key)) : key })), mode);
  });

  // ---------- comment ----------
  const openComposer = el => {
    let c = el.querySelector(":scope > .composer");
    if (c) { c.querySelector("textarea").focus(); return; }
    c = document.createElement("div"); c.className = "composer";
    const mod = isMac ? "⌘" : "Ctrl+";
    c.innerHTML = `<textarea placeholder="What should Claude do?" aria-label="Instructions for Claude"></textarea>
      <div class="cf"><button class="cancel" type="button">Cancel</button><span class="sp"></span>
      <button class="btn sm q2" type="button">${ICON.plus} Queue <kbd>${mod}⇧↵</kbd></button>
      <button class="btn sm accent s2" type="button">${ICON.play} Start task <kbd>${mod}↵</kbd></button></div>`;
    el.appendChild(c);
    const ta = c.querySelector("textarea"); ta.value = queue.get(el.dataset.key) || ""; ta.focus();
    if (el.classList.contains("dd")) c.querySelector(".q2").remove();
    const close = () => { c.remove(); select(el); };
    c.querySelector(".cancel").onclick = close;
    c.querySelector(".s2").onclick = () => { const v = ta.value.trim(); c.remove(); startOne(el, v); select(el); };
    const q2 = c.querySelector(".q2");
    if (q2) q2.onclick = () => { const v = ta.value.trim(); c.remove(); enqueue(el, v); select(el); };
    ta.addEventListener("keydown", e => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); ((e.shiftKey && q2) || c.querySelector(".s2")).click(); }
      if (e.key === "Escape") { e.preventDefault(); close(); }
    });
  };

  // ---------- hover actions ----------
  document.addEventListener("click", e => {
    const b = e.target.closest(".acts button");
    if (!b) return;
    const el = b.closest(".row, .card-wrap, .dd");
    e.preventDefault(); e.stopPropagation();
    if (b.classList.contains("cm")) openComposer(el);
    else if (b.classList.contains("go")) startOne(el);
    else if (b.classList.contains("ar")) archiveRows([el]);
    else if (b.classList.contains("kp")) { b.disabled = true; api("/api/keep", { key: el.dataset.key }).then(r => { toast(r.message); sync(); }).catch(e => { b.disabled = false; toast("Couldn't keep it: " + e.message); }); }
    else if (!queue.has(el.dataset.key)) enqueue(el);
  }, true);

  // ---------- instant tooltips: data-tip="Label|Key" ----------
  const tip = document.createElement("div");
  tip.className = "tip"; tip.hidden = true; tip.setAttribute("aria-hidden", "true");
  document.body.appendChild(tip);
  const tipEsc = s => s.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
  document.addEventListener("mouseover", e => {
    const t = e.target.closest?.("[data-tip]");
    if (!t) { tip.hidden = true; return; }
    const [label, key] = t.dataset.tip.split("|");
    tip.innerHTML = tipEsc(label) + (key ? ` <kbd>${tipEsc(key)}</kbd>` : "");
    tip.hidden = false;
    const r = t.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    const above = r.top - h - 6 >= 4;
    tip.style.left = Math.max(4, Math.min(innerWidth - w - 4, r.left + r.width / 2 - w / 2)) + "px";
    tip.style.top = (above ? r.top - h - 6 : r.bottom + 6) + "px";
  });
  ["mousedown", "scroll", "keydown"].forEach(ev => addEventListener(ev, () => (tip.hidden = true), true));

  // ---------- project rows open their board ----------
  document.addEventListener("click", e => {
    const tr = e.target.closest(".p-row");
    if (!tr || e.target.closest("a, button") || getSelection().toString()) return;
    tr.querySelector(".bt[aria-controls]")?.click();
  });

  // ---------- Claude tasks list ----------
  let tasks = [];
  const ago = iso => { const m = Math.round((Date.now() - Date.parse(iso)) / 60000); return m < 1 ? "just now" : m < 60 ? m + " min" : Math.round(m / 60) + " h"; };
  const STATUS = {
    working: t => `Working · ${ago(t.since || t.started_at)}`,
    waiting: t => `<b>Waiting for you</b> · ${ago(t.since || t.started_at)}`,
    starting: () => "Starting…",
    done: t => `Finished · ${ago(t.since || t.started_at)} ago`,
    closed: () => "Tab closed",
  };
  const focusTask = async t => {
    try { const r = await api("/api/focus", { session_id: t.session_id }); if (r.message) toast(r.message); }
    catch (e) { fail(e); }
  };
  const renderTasks = () => {
    const ul = document.querySelector(".rail-tasks .tasks");
    ul.innerHTML = "";
    document.querySelector(".tasks-empty").hidden = tasks.length > 0;
    const order = { waiting: 0, working: 1, starting: 1, done: 2, closed: 3 };
    [...tasks].sort((a, b) => order[a.status] - order[b.status] || Date.parse(b.started_at) - Date.parse(a.started_at)).forEach(t => {
      const li = document.createElement("li");
      li.hidden = t.status === "waiting";  // listed under Agents waiting
      li.className = "task " + (t.status === "starting" ? "working" : t.status === "closed" ? "done" : t.status);
      li.innerHTML = `<button type="button"><span class="dot" aria-hidden="true"></span><span class="t-l"></span><span class="t-m">${(STATUS[t.status] || STATUS.working)(t)} · tab ${esc(t.title)}</span><span class="t-go">${ICON.open}</span></button>`;
      li.querySelector(".t-l").textContent = t.label;
      const b = li.querySelector("button");
      b.title = t.status === "closed" ? "This tab has been closed" : "Bring this Warp tab to the front";
      b.disabled = t.status === "closed";
      b.onclick = () => focusTask(t);
      ul.appendChild(li);
      (t.keys || []).forEach(key => {
        const el = byKey(key);
        if (!el) return;
        if (t.note && !el.querySelector(".note")) addNote(el, t.note);
        const cls = t.status === "waiting" ? "waiting" : t.status === "done" || t.status === "closed" ? "done" : "";
        const text = t.status === "waiting" ? "Waiting for you" : t.status === "done" ? "Finished" : t.status === "closed" ? "Tab closed" : "Claude working";
        const r = setRun(el, `${text} · tab <b>${esc(t.title)}</b>`, cls);
        r.setAttribute("role", "button"); r.tabIndex = 0; r.title = b.title;
        r.onclick = e => { e.preventDefault(); e.stopPropagation(); focusTask(t); };
      });
    });
    const waiting = tasks.filter(t => t.status === "waiting").length;
    document.querySelector("[data-count=waiting]").textContent = waiting;
    document.querySelector(".rail-tasks .wait").hidden = true;  // the count is under Agents waiting
  };
  // ---------- archive ----------
  const recount = () => {
    document.querySelectorAll("#view-desk .sec").forEach(sec => {
      let total = 0;
      sec.querySelectorAll("ol.rows").forEach(ol => {
        const n = ol.querySelectorAll(".row:not([hidden])").length; total += n;
        const h = ol.previousElementSibling;
        if (h?.classList.contains("grp-h")) { h.querySelector(".grp-n").textContent = n; h.hidden = n === 0; }
      });
      if (!sec.querySelector("ol.rows")) return;
      const sn = sec.querySelector(".sec-n"); if (sn) sn.textContent = total;
      const nn = document.querySelector(`.rail a[data-target="${CSS.escape(sec.id)}"] .nav-n`); if (nn) nn.textContent = total;
    });
  };
  const hideArchived = keys => {
    let changed = false;
    keys.forEach(k => { const el = byKey(k); if (el?.classList.contains("row") && !el.hidden) { el.hidden = true; changed = true; } });
    if (changed) recount();
  };

  // ---------- staying current ----------
  // Every few seconds the page tells the server which version of each region it shows; the server sends back
  // the regions that have changed, and each is swapped in place unless Peter is in the middle of something in it.
  const refreshBtn = document.getElementById("refresh");
  const refreshLabel = t => (refreshBtn.querySelector(".refresh-l").textContent = t);
  refreshBtn.addEventListener("click", async () => {
    if (refreshBtn.disabled) return;
    refreshBtn.disabled = true; refreshLabel("Refreshing…");
    try { toast((await api("/api/refresh", {})).message); sync(); }
    catch (e) { refreshBtn.disabled = false; refreshLabel("Refresh"); fail(e); }
  });
  // A background refresh that is blocked, failed or slow explains itself under the header.
  const refreshNote = document.getElementById("refresh-note");
  let noteSig = "";
  const renderRefreshNote = n => {
    const sig = JSON.stringify(n || null);
    if (sig === noteSig) return;
    noteSig = sig; refreshNote.innerHTML = "";
    refreshNote.className = "refresh-note" + (n ? " rn-" + n.state : "");
    if (!n) return;
    const el = (tag, cls, text) => Object.assign(document.createElement(tag), { className: cls, textContent: text });
    refreshNote.append(el("p", "rn-t", n.title));
    if (n.reason) refreshNote.append(el("p", "rn-r", n.reason));
    if (n.detail) refreshNote.append(el("p", "rn-d", "Claude's last note: " + n.detail));
    const acts = el("div", "rn-a", "");
    const btn = (label, path, cls) => {
      const b = el("button", "btn sm" + (cls ? " " + cls : ""), label); b.type = "button";
      b.onclick = async () => {
        acts.querySelectorAll("button").forEach(x => (x.disabled = true));
        try { const r = await api(path, {}); if (r.message) toast(r.message); noteSig = ""; sync(); }
        catch (e) { acts.querySelectorAll("button").forEach(x => (x.disabled = false)); fail(e); }
      };
      acts.append(b);
    };
    if (n.can_open) btn("Open in Warp", "/api/refresh/open", "primary");
    if (n.state !== "slow") { btn("Run again in Warp", "/api/refresh/warp", n.can_open ? "" : "primary"); btn("Dismiss", "/api/refresh/dismiss", "ghost"); }
    refreshNote.append(acts);
  };
  // The live-sources list closes on a click elsewhere or Esc.
  document.addEventListener("click", e => { const d = document.getElementById("live-d"); if (d?.open && !d.contains(e.target)) d.open = false; });
  document.addEventListener("keydown", e => { const d = document.getElementById("live-d"); if (e.key === "Escape" && d?.open) { d.open = false; d.querySelector("summary").focus(); } });
  document.addEventListener("click", async e => {
    const b = e.target.closest("#check-now");
    if (!b) return;
    b.classList.add("busy");
    try { await api("/api/check", {}); setTimeout(() => sync(), 1500); } catch (err) { fail(err); }
  });

  let dragged = null, issuesHash = null, syncing = false;
  const regionBusy = el =>
    (dragged && el.contains(dragged)) || el.querySelector(".composer, .add-form, .add-card, .moving, .picked") ||
    (el.contains(document.activeElement) && document.activeElement.matches("input, textarea, select")) ||
    (el.dataset.region === "projects" && (writes > 0 || projects.menuOpen()));
  const selToken = () => !sel ? null
    : sel.dataset.key ? { key: sel.dataset.key, off: sel.classList.contains("acts-off") }
    : sel.classList.contains("p-row") ? { proj: sel.closest(".proj")?.dataset.name }
    : sel.classList.contains("pub-head") ? { q: "#view-desk .pub-head" } : null;
  const reselect = tok => {
    if (!tok) return;
    const el = tok.key ? byKey(tok.key) : tok.proj ? [...document.querySelectorAll("tbody.proj")].find(tb => tb.dataset.name === tok.proj)?.querySelector(".p-row") : document.querySelector(tok.q);
    sel = el || null;
    sel?.classList.add("kb-sel");
    if (tok.off) sel?.classList.add("acts-off");
  };
  const swap = (el, r) => {
    const open = [...el.querySelectorAll("tbody.proj.open")].map(tb => tb.dataset.name);
    const openD = [...el.querySelectorAll("details[id][open]")].map(d => d.id);
    el.innerHTML = r.html; el.dataset.hash = r.hash;
    openD.forEach(id => { const d = document.getElementById(id); if (d) d.open = true; });
    open.forEach(name => {
      const tb = [...el.querySelectorAll("tbody.proj")].find(tb => tb.dataset.name === name), bt = tb?.querySelector(".bt[aria-controls]");
      if (!bt) return;
      bt.setAttribute("aria-expanded", "true"); tb.classList.add("open");
      document.getElementById(bt.getAttribute("aria-controls")).hidden = false;
    });
  };
  const applyPub = () => {
    const pub = document.getElementById("pub");
    if (!pub) return;
    let open = false;
    try { open = localStorage.getItem("desk-pub-open") === "1"; } catch (_) {}
    pub.classList.toggle("open", open);
    pub.querySelector(".pub-toggle").setAttribute("aria-expanded", String(open));
    document.getElementById("pub-panel").hidden = !open;
  };
  // ---------- arrivals: held behind a pill until Peter looks ----------
  // The pill counts conversations with something new since the last run. Unanswered VIP messages the judge calls
  // urgent sit behind one Urgent pill; its popover shows why each is urgent, and "Not urgent" takes one off (with an
  // optional comment, logged for the rubric review) until that person writes there again.
  const arrBar = document.getElementById("arr-bar"), arrPop = document.getElementById("arr-pop");
  const urgPop = document.createElement("div");
  urgPop.className = "arr-pop urg-pop"; urgPop.id = "urg-pop"; urgPop.setAttribute("role", "dialog"); urgPop.setAttribute("aria-label", "Urgent"); urgPop.hidden = true;
  arrPop.after(urgPop);
  let arrSig = "", urgent = [];
  const h = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  // "Group DM danacole, noor, p" reads "Group DM: danacole, noor" (p is Peter's handle); "DM with Alex Morgan: Hi…" reads "DM thread: Hi…"
  const where = s => (s || "").replace(/^(Signal|WhatsApp): (.*) \(DM\)$/, "$1 DM").replace(/^DM with [^:]*: /, "DM thread: ").replace(/^DM with .*/, "DM")
    .replace(/^Group DM (.*)$/, (_, n) => "Group DM: " + n.split(", ").filter(x => x !== "p").join(", "));
  const urgAgo = ts => { const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60)); return m < 60 ? `${m} min` : m < 2880 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} d`; };
  const first = n => (n || "Someone").split(" ")[0];
  const urgRow = v => {
    const j = v.judge, id = "why-" + v.unit.replace(/[^\w-]/g, "_");
    return `<li class="urg-row" data-unit="${h(v.unit)}"><div class="urg-main"><a class="urg-go" href="${h(v.link || "#")}" target="_blank" rel="noopener">`
      + `<span class="arr-where">${h(where(v.where))}</span><span class="arr-text">${h(v.text)}</span></a>`
      + `<time>${urgAgo(v.since)}</time><button type="button" class="urg-why" aria-expanded="false" aria-controls="${id}">Why</button>`
      + `<button type="button" class="urg-nu">Not urgent</button></div>`
      + `<div class="urg-reason" id="${id}" hidden><p><b>Rule:</b> ${h(v.rule)}</p>`
      + (j ? `<p><b>Judge:</b> ${h(j.reason)}</p>` : `<p><b>Judge:</b> hasn't read it yet, so it shows after 15 minutes anyway.</p>`) + `</div></li>`;
  };
  const renderUrgent = () => {
    if (urgPop.querySelector(".urg-row.marked")) return;   // keep Peter's marks in view until he closes the popover
    const by = new Map();
    urgent.forEach(v => { if (!by.has(v.who)) by.set(v.who, []); by.get(v.who).push(v); });
    urgPop.innerHTML = urgent.length ? [...by].map(([who, vs]) => `<h3>${h(who)} <span>${vs.length}</span>`
      + (vs.length > 1 ? `<button type="button" class="urg-all">None urgent</button>` : "") + `</h3><ol>${vs.map(urgRow).join("")}</ol>`).join("")
      : '<p class="arr-none">Nothing urgent.</p>';
  };
  const renderArrivals = a => {
    if (!a) return;
    const sig = JSON.stringify(a);
    if (sig === arrSig) return;
    arrSig = sig;
    urgent = (a.vips || []).filter(v => v.urgent);
    arrBar.innerHTML = "";
    if (urgent.length) {
      const counts = new Map();
      urgent.forEach(v => counts.set(first(v.who), (counts.get(first(v.who)) || 0) + 1));
      const b = document.createElement("button");
      b.type = "button"; b.className = "arr urgent"; b.id = "urg-pill"; b.setAttribute("aria-haspopup", "dialog"); b.setAttribute("aria-expanded", String(!urgPop.hidden));
      b.innerHTML = `<span class="arr-dot" aria-hidden="true"></span><span>${urgent.length} urgent</span><span class="by"></span>`;
      b.querySelector(".by").textContent = [...counts].map(([n, c]) => c > 1 ? `${n} ${c}` : n).join(" · ");
      arrBar.appendChild(b);
    }
    if (a.total || a.other) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "arr"; b.id = "arr-pill"; b.setAttribute("aria-haspopup", "dialog"); b.setAttribute("aria-expanded", String(!arrPop.hidden));
      const by = Object.entries(a.by_source || {}).map(([k, n]) => `${k} ${n}`).join(", ");
      b.dataset.tip = "What's new since the last run|N";
      b.innerHTML = `<span>${a.total} new</span><span class="by"></span>`;
      b.querySelector(".by").textContent = by;
      arrBar.appendChild(b);
    } else closeArrivals();
    document.title = (a.urgent ? `(${a.urgent}) ` : "") + "Forethought desk";
    if (!arrPop.hidden) loadArrivals();
    if (!urgPop.hidden) renderUrgent();
  };
  const markNotUrgent = async row => {
    const unit = row.dataset.unit, label = row.querySelector(".arr-where").textContent;
    row._html = row.innerHTML; row.classList.add("marked");
    row.innerHTML = `<div class="urg-done"><span>${h(label)}: not urgent</span><input type="text" placeholder="Why not? (optional)" aria-label="Why it isn't urgent"><button type="button" class="urg-undo">Undo</button></div>`;
    try { await write("/api/arrivals/dismiss", { unit }); arrSig = ""; }
    catch (e) { row.classList.remove("marked"); row.innerHTML = row._html; toast(`Couldn't mark it: ${e.message}`); }
  };
  const saveComment = inp => {
    const row = inp.closest(".urg-row"), c = inp.value.trim();
    if (c === (inp.dataset.saved || "")) return;
    inp.dataset.saved = c;
    write("/api/arrivals/dismiss", { unit: row.dataset.unit, comment: c }).catch(e => toast(`Couldn't save the comment: ${e.message}`));
  };
  const closeUrgent = () => {
    if (urgPop.hidden) return;
    urgPop.querySelectorAll(".urg-done input").forEach(saveComment);
    urgPop.hidden = true; urgPop.innerHTML = ""; document.getElementById("urg-pill")?.setAttribute("aria-expanded", "false");
    sync();
  };
  const toggleUrgent = () => {
    if (!urgPop.hidden) return closeUrgent();
    closeArrivals();
    const top = Math.max(8, (arrBar.getBoundingClientRect().bottom || 0) + 8);
    urgPop.style.top = top + "px"; urgPop.style.maxHeight = `calc(100vh - ${top + 16}px)`;
    urgPop.hidden = false; renderUrgent(); document.getElementById("urg-pill")?.setAttribute("aria-expanded", "true");
  };
  urgPop.addEventListener("click", e => {
    const why = e.target.closest(".urg-why");
    if (why) { const d = document.getElementById(why.getAttribute("aria-controls")); d.hidden = !d.hidden; why.setAttribute("aria-expanded", String(!d.hidden)); return; }
    const nu = e.target.closest(".urg-nu");
    if (nu) { markNotUrgent(nu.closest(".urg-row")).then(() => nu.isConnected || urgPop.querySelector(".marked:last-of-type input")?.focus()); return; }
    const all = e.target.closest(".urg-all");
    if (all) { all.closest("h3").nextElementSibling.querySelectorAll(".urg-row:not(.marked)").forEach(markNotUrgent); all.remove(); return; }
    const undo = e.target.closest(".urg-undo");
    if (undo) {
      const row = undo.closest(".urg-row");
      write("/api/arrivals/dismiss", { unit: row.dataset.unit, undo: true })
        .then(() => { row.classList.remove("marked"); row.innerHTML = row._html; arrSig = ""; })
        .catch(e => toast(`Couldn't undo: ${e.message}`));
    }
  });
  urgPop.addEventListener("focusout", e => { if (e.target.matches(".urg-done input")) saveComment(e.target); });
  urgPop.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.matches(".urg-done input")) { e.preventDefault(); saveComment(e.target); e.target.blur(); } });
  const placePop = () => {
    const top = Math.max(8, (arrBar.getBoundingClientRect().bottom || 0) + 8);
    arrPop.style.top = top + "px"; arrPop.style.maxHeight = `calc(100vh - ${top + 16}px)`;
  };
  const loadArrivals = async () => {
    try { arrPop.innerHTML = (await api("/api/arrivals", {})).html; } catch (e) { fail(e); }
  };
  const closeArrivals = () => { arrPop.hidden = true; document.getElementById("arr-pill")?.setAttribute("aria-expanded", "false"); };
  const toggleArrivals = () => {
    if (!arrPop.hidden) return closeArrivals();
    closeUrgent();
    if (!document.getElementById("arr-pill")) return;
    placePop(); arrPop.hidden = false; document.getElementById("arr-pill").setAttribute("aria-expanded", "true");
    loadArrivals();
  };
  document.addEventListener("click", e => {
    if (e.target.closest("#urg-pill")) return toggleUrgent();
    if (e.target.closest("#arr-pill")) return toggleArrivals();
    if (!arrPop.hidden && !e.target.closest("#arr-pop")) closeArrivals();
    if (!urgPop.hidden && e.target.isConnected && !e.target.closest("#urg-pop")) closeUrgent();   // a row the click just replaced is no longer in the popover
  });
  document.addEventListener("keydown", e => {
    if (e.target.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "n") { e.preventDefault(); toggleArrivals(); }
    else if (e.key === "Escape" && !arrPop.hidden) { e.preventDefault(); e.stopImmediatePropagation(); closeArrivals(); }
    else if (e.key === "Escape" && !urgPop.hidden) { e.preventDefault(); e.stopImmediatePropagation(); closeUrgent(); }
  }, true);

  let archived = [];
  let syncAgain = false;
  const sync = async () => {
    if (syncing) { syncAgain = true; return; }  // the poll in flight predates the caller's write: run once more after it
    syncing = true;
    try {
      if (outGet().length && !(await flushOutbox())) return;   // send what waited here before taking the server's view
      const hashes = Object.fromEntries([...document.querySelectorAll("[data-region]")].map(el => [el.dataset.region, el.dataset.hash]));
      const s = await api("/api/sync", { hashes, issues_hash: issuesHash });
      const idle = !picked.size && !document.querySelector(".composer, .pv-edit") && !document.activeElement?.matches("input, textarea, select");
      if (ASSETS && s.assets !== ASSETS && idle && !writes) { location.reload(); return; }
      tasks = s.tasks || [];
      queue = new Map((s.queue || []).map(q => [q.key, q.note || ""]));
      archived = s.archived || [];
      if (s.issues) {
        Object.keys(ISSUES).forEach(k => delete ISSUES[k]);
        Object.assign(ISSUES, s.issues); issuesHash = s.issues_hash;
      }
      let swapped = false;
      const y = scrollY, tok = selToken();
      for (const [name, r] of Object.entries(s.regions || {})) {
        const el = document.querySelector(`[data-region="${name}"]`);
        if (!el || regionBusy(el)) continue;   // a busy region keeps its old hash, so the next sync offers it again
        swap(el, r); swapped = true;
      }
      if (swapped) {
        bind(); applyPub(); projects.adopt(s.projects);
        if (sel && !sel.isConnected) reselect(tok);
        if (peekId) {
          if (!ISSUES[peekId]) closePeek();
          else { peekLi = cardFor(peekId) || null; peekLi?.classList.add("pv-on"); if (!peekBox.querySelector(".pv-edit") && !peekBox.contains(document.activeElement)) renderPeek(); }
        }
        if (Math.abs(scrollY - y) > 1) scrollTo(0, y);
        window.dispatchEvent(new Event("scroll"));
      }
      renderTasks(); renderTray(); hideArchived(archived); renderArrivals(s.arrivals);
      window.dispatchEvent(new CustomEvent("desk:sync", { detail: s }));
      peekSyncChip();
      const busy = !!s.refreshing;
      refreshBtn.disabled = busy; refreshLabel(busy ? "Refreshing…" : "Refresh");
      renderRefreshNote(s.refresh);
      document.getElementById("check-now")?.classList.toggle("busy", (s.checking || []).length > 0);
    } catch (e) { /* server down: keep the page readable */ }
    finally {
      syncing = false;
      if (syncAgain) { syncAgain = false; setTimeout(sync, 0); }
    }
  };
  // The peek's "Waiting to sync" chip follows the outboxes.
  const peekSyncChip = () => {
    if (peekId && !peekBox.querySelector(".pv-edit") && !peekBox.contains(document.activeElement) && ISSUES[peekId]
        && !!peekBox.querySelector(".pv-sync") !== !!window.ftDesk.pendingFor?.(peekId)) renderPeek();
  };
  window.addEventListener("desk:outbox", () => setTimeout(peekSyncChip, 0));
  // Every 5 seconds in view; every 30 when hidden, so the Dock badge still learns of an urgent arrival.
  let ticks = 0;
  setInterval(() => { if (!document.hidden || ++ticks % 6 === 0) sync(); }, 5000);
  window.ftDesk.sync = sync;
  document.addEventListener("visibilitychange", () => { if (!document.hidden) sync(); });

  // ---------- keyboard ----------
  let sel = null;
  const visible = el => el.offsetParent !== null && !el.closest("[hidden]");
  const navItems = () => [...document.querySelectorAll("#view-desk .pub-head, #view-desk .pub-steps li, #view-desk .p-row, #view-desk .row, #view-desk .card-wrap")].filter(visible);
  const select = el => {
    sel?.classList.remove("kb-sel");
    sel = el;
    if (!el || el.classList.contains("dd")) { sel = null; return; }
    el.classList.add("kb-sel");
    el.classList.remove("acts-off");
    el.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  };
  // J and K start from the item under the pointer when the mouse has moved onto it since the last keyboard move,
  // so a still pointer that the page scrolls under doesn't fight the keyboard.
  let hoverEl = null, hoverFresh = false, lastXY = "";
  addEventListener("mousemove", e => {
    const xy = e.screenX + "," + e.screenY;
    if (xy === lastXY) return;   // a synthetic move after a scroll, not the mouse
    lastXY = xy;
    hoverEl = e.target.closest?.("#view-desk .pub-head, #view-desk .pub-steps li, #view-desk .p-row, #view-desk .row, #view-desk .card-wrap") || null;
    hoverFresh = true;
  }, { passive: true });
  addEventListener("mouseout", e => {
    const it = e.target.closest?.(".row, .card-wrap, .dp .dd");
    if (it && !it.contains(e.relatedTarget)) it.classList.add("acts-off");
  }, { passive: true });
  const move = d => {
    const list = navItems();
    if (!list.length) return;
    const from = hoverFresh && hoverEl && hoverEl !== sel && list.includes(hoverEl) ? hoverEl : sel;
    hoverFresh = false;
    const i = from ? list.indexOf(from) : -1;
    select(list[i < 0 ? (d > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, i + d))]);
  };
  const activate = el => {
    if (el.classList.contains("pub-head")) { el.querySelector(".pub-toggle").click(); return; }
    if (el.closest(".pub-steps")) { const a = el.querySelector("a"); if (a) window.open(a.href, "_blank"); return; }
    if (el.classList.contains("p-row")) { el.querySelector(".bt[aria-controls]")?.click(); return; }
    if (el.classList.contains("card-wrap") && peek(el)) return;
    const link = el.querySelector("a.head, a.card");
    if (link) window.open(link.href, link.href.startsWith("http") ? "_blank" : "_self");
  };
  // C and W each open their view, or go back to the desk from it.
  const toggleView = (id = "tab-plan") => {
    const tab = document.getElementById(id);
    (tab.getAttribute("aria-selected") === "true" ? document.getElementById("tab-desk") : tab).click();
  };
  const help = document.createElement("div");
  help.className = "kb-help"; help.hidden = true; help.setAttribute("role", "dialog"); help.setAttribute("aria-label", "Keyboard shortcuts");
  const M = isMac ? "⌘⌥M" : "Ctrl+Alt+M";
  help.innerHTML = `<h2>Keyboard shortcuts</h2><dl>
    <dt><kbd>J</kbd> <kbd>K</kbd></dt><dd>Next and previous item</dd>
    <dt><kbd>↵</kbd></dt><dd>Open the item, a project's board, or a Linear card's preview</dd>
    <dt><kbd>M</kbd> or <kbd>${M}</kbd></dt><dd>Comment on the item or project, then start or queue it</dd>
    <dt><kbd>S</kbd></dt><dd>Start a Claude Code tab on the item</dd>
    <dt><kbd>⇧Q</kbd></dt><dd>Add the item to the queue, or take it out</dd>
    <dt><kbd>⇧S</kbd></dt><dd>Send everything queued</dd>
    <dt><kbd>X</kbd></dt><dd>Select the item, or unselect it, to act on several at once</dd>
    <dt><kbd>E</kbd></dt><dd>Archive the item, or every selected item; on a Linear card, mark it done; on a Slack to-do, tick it off in Slack</dd>
    <dt><kbd>↵</kbd> with items selected</dt><dd>Start a Claude Code tab on each</dd>
    <dt><kbd>T</kbd></dt><dd>Bring the item's Claude tab to the front</dd>
    <dt><kbd>[</kbd> <kbd>]</kbd></dt><dd>Move the card to the previous or next column</dd>
    <dt><kbd>⌫</kbd></dt><dd>Reject the Linear card (Undo in the toast)</dd>
    <dt><kbd>⇧A</kbd></dt><dd>Add a Linear task to the project or column</dd>
    <dt><kbd>Z</kbd></dt><dd>Snooze the project; <kbd>Z</kbd> again until the next refresh, <kbd>2</kbd>–<kbd>5</kbd> for longer. On a Slack to-do, snooze it in Slack</dd>
    <dt><kbd>Q</kbd></dt><dd>Quick create Linear or Todoist tasks (<kbd>⇧⌘T</kbd> switches)</dd>
    <dt><kbd>A</kbd></dt><dd>Quick agent: start a Claude Code tab with a prompt</dd>
    <dt><kbd>D</kbd></dt><dd>Go to the desk</dd>
    <dt><kbd>C</kbd></dt><dd>Switch between Desk and Calendar</dd>
    <dt><kbd>W</kbd></dt><dd>Switch between Desk and Plans</dd>
    <dt><kbd>I</kbd></dt><dd>Switch between Desk and Comments</dd>
    <dt><kbd>Y</kbd></dt><dd>Switch between Desk and Agents waiting</dd>
    <dt><kbd>P</kbd></dt><dd>Show or hide the publication pipeline</dd>
    <dt><kbd>N</kbd></dt><dd>Show or hide what is new since the last run</dd>
    <dt><kbd>Esc</kbd></dt><dd>Clear the selection</dd>
    <dt><kbd>?</kbd></dt><dd>Show or hide this list</dd></dl>
    <h2>Keyboard shortcuts</h2><dl>
    <dt><kbd>J</kbd> <kbd>K</kbd></dt><dd>Next and previous thread</dd>
    <dt><kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd></dt><dd>Waiting on you, FYI, Done</dd>
    <dt><kbd>R</kbd></dt><dd>Reply</dd>
    <dt><kbd>⌘↵</kbd></dt><dd>Send the reply</dd>
    <dt><kbd>⇧D</kbd></dt><dd>Resolve the thread, or reopen it</dd>
    <dt><kbd>E</kbd></dt><dd>Archive the thread; in Done, bring it back</dd>
    <dt><kbd>U</kbd></dt><dd>Mark unread</dd>
    <dt><kbd>O</kbd></dt><dd>Open it in the Doc</dd>
    <dt><kbd>S</kbd></dt><dd>Start a Claude Code tab on it</dd>
    <dt><kbd>I</kbd></dt><dd>Back to the desk</dd>
    <dt><kbd>?</kbd></dt><dd>Show or hide this list</dd></dl>`;
  document.body.appendChild(help);
  help.addEventListener("click", () => (help.hidden = true));

  // ---------- publication strip: collapsed by default; open stays open across reloads ----------
  const pubToggle = () => document.querySelector("#pub .pub-toggle");
  document.addEventListener("click", e => {
    if (!e.target.closest("#pub .pub-toggle")) return;
    const pub = document.getElementById("pub"), open = !pub.classList.contains("open");
    try { localStorage.setItem("desk-pub-open", open ? "1" : ""); } catch (_) {}
    applyPub();
    // Collapsing hides the steps: keep the selection on the strip itself.
    if (sel && sel.closest(".pub-steps") && !open) select(pub.querySelector(".pub-head"));
  });
  applyPub();

  document.addEventListener("keydown", e => {
    const typing = e.target.closest("input, textarea, select, [contenteditable]");
    const commentKey = e.code === "KeyM" && e.altKey && (isMac ? e.metaKey : e.ctrlKey);
    if (commentKey) {
      const el = sel && !sel.classList.contains("p-row") ? sel : e.target.closest?.(".row, .card-wrap");
      if (el) { e.preventDefault(); openComposer(el); }
      return;
    }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    if (k === "?") { e.preventDefault(); help.hidden = !help.hidden; return; }
    if (k === "Escape") { if (!help.hidden) help.hidden = true; else if (picked.size) clearPicks(); else select(null); return; }
    if (k === "d") { e.preventDefault(); document.getElementById("tab-desk").click(); return; }
    if (k === "c" || k === "C") { e.preventDefault(); toggleView(); return; }
    if (k === "w" || k === "W") { e.preventDefault(); toggleView("tab-week"); return; }
    if (k === "i" || k === "I") { e.preventDefault(); toggleView("tab-comments"); return; }
    if ((k === "p" || k === "P") && pubToggle() && !document.getElementById("view-desk").hidden) { e.preventDefault(); pubToggle().click(); return; }
    if (k === "S" && e.shiftKey) { e.preventDefault(); if (queue.size) document.getElementById("tray-send").click(); return; }
    if (document.getElementById("view-desk").hidden) return;
    if (k === "j" || k === "ArrowDown" && sel) { e.preventDefault(); move(1); return; }
    if (k === "k" || k === "ArrowUp" && sel) { e.preventDefault(); move(-1); return; }
    if (k === "Enter" && picked.size) { e.preventDefault(); startPicked(); return; }
    if (k === "e" && picked.size) { e.preventDefault(); archivePicked(); return; }
    if (!sel || !visible(sel)) return;
    if ((k === "x" || k === "X") && sel.classList.contains("row")) { e.preventDefault(); togglePick(sel); return; }
    if (k === "e" && sel.classList.contains("row")) { e.preventDefault(); archiveRows([sel]); return; }
    if (k === "e" && sel.classList.contains("card-wrap")) { e.preventDefault(); moveCard(sel, sel.closest(".board").querySelector('.col[data-col="Done"]')); return; }
    if (k === "Enter") { e.preventDefault(); activate(sel); return; }
    if (sel.classList.contains("p-row") || sel.closest("#pub")) return;
    if (k === "s") { e.preventDefault(); startOne(sel); return; }
    if (k === "m") { e.preventDefault(); openComposer(sel); return; }
    if ((k === "Backspace" || k === "Delete") && sel.classList.contains("card-wrap")) { e.preventDefault(); rejectCard(sel); return; }
    if (k === "Q" && e.shiftKey) { e.preventDefault(); queue.has(sel.dataset.key) ? unqueue(sel.dataset.key) : enqueue(sel); return; }
    if ((k === "[" || k === "]") && sel.classList.contains("card-wrap")) {
      e.preventDefault();
      const cols = [...sel.closest(".board").querySelectorAll(".col")];
      const to = cols[cols.indexOf(sel.closest(".col")) + (k === "]" ? 1 : -1)];
      if (to) moveCard(sel, to);
      return;
    }
    if (k === "t") {
      const t = tasks.find(t => (t.keys || []).includes(sel.dataset.key) && t.status !== "closed");
      if (t) { e.preventDefault(); focusTask(t); }
    }
  });
  document.addEventListener("click", e => {
    const el = e.target.closest("#view-desk .p-row, #view-desk .row, #view-desk .card-wrap");
    if (el) select(el);
  });
  // ---------- multi-select ----------
  const picked = new Set();
  const bar = document.createElement("div");
  bar.className = "pick-bar"; bar.hidden = true; bar.setAttribute("role", "region"); bar.setAttribute("aria-label", "Selected items");
  bar.innerHTML = `<span class="pick-n" aria-live="polite"></span><span class="sp"></span>
    <button class="btn" type="button" data-a="clear">Clear <kbd>Esc</kbd></button>
    <button class="btn" type="button" data-a="archive">Archive <kbd>E</kbd></button>
    <button class="btn primary" type="button" data-a="start">${ICON.play} Start all <kbd>↵</kbd></button>`;
  document.body.appendChild(bar);
  const renderPicks = () => {
    bar.hidden = picked.size === 0;
    document.body.classList.toggle("picking", picked.size > 0);
    bar.querySelector(".pick-n").textContent = `${picked.size} selected`;
  };
  function togglePick(el) {
    picked.has(el) ? picked.delete(el) : picked.add(el);
    el.classList.toggle("picked", picked.has(el));
    renderPicks();
  }
  function clearPicks() { picked.forEach(el => el.classList.remove("picked")); picked.clear(); renderPicks(); }
  function startPicked() {
    const els = [...picked].filter(el => !el.hidden);
    clearPicks();
    start(els.map(el => ({ key: el.dataset.key, note: queue.get(el.dataset.key) || "", label: labelOf(el) })), "each");
  }
  async function archiveRows(els) {
    els = els.filter(el => !el.hidden);
    if (!els.length) return;
    const list = navItems(), after = list[list.indexOf(els[els.length - 1]) + 1];
    els.forEach(el => { el.hidden = true; el.classList.remove("picked"); picked.delete(el); });
    renderPicks(); recount();
    if (els.includes(sel)) select(after && !after.hidden ? after : null);
    try {
      const r = await api("/api/archive", { keys: els.map(el => el.dataset.key) });
      const back = els.filter(el => !r.archived.includes(el.dataset.key));
      back.forEach(el => (el.hidden = false));
      if (back.length) recount();
      const verb = els.every(el => el.dataset.key?.startsWith("slack-todo:")) ? "Done in Slack:" : "Archived";
      toast(back.length ? `${verb} ${els.length - back.length}; ${back.length} refused: ${r.refused[0] || ""}` : `${verb} ${els.length}`);
    } catch (e) {
      els.forEach(el => (el.hidden = false)); recount(); fail(e);
    }
  }
  function archivePicked() { archiveRows([...picked]); }
  document.addEventListener("click", e => {
    const b = e.target.closest("#archive .sec-acts button");
    if (!b) return;
    const rows = [...document.querySelectorAll("#archive .row:not([hidden])")];
    if (b.dataset.sa === "archive") archiveRows(rows);
    else if (rows.every(el => picked.has(el))) rows.forEach(el => togglePick(el));
    else rows.forEach(el => { if (!picked.has(el)) togglePick(el); });
  });
  bar.addEventListener("click", e => {
    const a = e.target.closest("button")?.dataset.a;
    if (a === "clear") clearPicks(); else if (a === "archive") archivePicked(); else if (a === "start") startPicked();
  });

  // ---------- drag cards between columns ----------
  // The card moves at once; the server then sets the Linear issue's state, and a refusal puts it back.
  const countCol = col => {
    const n = col.querySelectorAll(".card-wrap").length;
    col.querySelector(".col-n").textContent = n;
    col.classList.toggle("empty", n === 0);
  };
  async function moveCard(li, col, before = null) {
    const from = li.closest(".col"), next = li.nextElementSibling;
    if (col === from) return;  // order within a column isn't synced to Linear
    const ul = col.querySelector(".cards");
    ul.insertBefore(li, before || ul.firstElementChild);
    countCol(from); countCol(col);
    const id = li.querySelector(".card-id").textContent;
    li.classList.add("moving");
    try {
      const r = await linearWrite(id, { kind: "update", set: { column: col.dataset.col } }, `${id} to ${col.dataset.col}`);
      toast(`${id} moved to ${r.state || col.dataset.col}${queuedNote(r)}`);
    } catch (e) {
      if (from.isConnected) {
        from.querySelector(".cards").insertBefore(li, next && next.parentNode === from.querySelector(".cards") ? next : null);
        countCol(from); countCol(col);
      }
      toast(`Couldn't move ${id}: ${e.message}`);
    } finally { li.classList.remove("moving"); if (peekLi === li) renderPeek(); }
  }
  // Reject: the issue goes to Linear's Rejected state and leaves the board; the toast offers Undo.
  async function rejectCard(li) {
    const from = li.closest(".col"), ul = from.querySelector(".cards"), next = li.nextElementSibling, id = li.querySelector(".card-id").textContent;
    const back = from.dataset.col;
    const putBack = () => { if (ul.isConnected) { ul.insertBefore(li, next && next.parentNode === ul ? next : null); countCol(from); } };
    if (typeof peekLi !== "undefined" && peekLi === li) closePeek();
    if (sel === li) {
      const cards = [...li.closest(".board").querySelectorAll(".card-wrap")], n = cards.indexOf(li);
      select(cards[n + 1] || cards[n - 1] || null);
    }
    li.remove(); countCol(from);
    try {
      const r = await linearWrite(id, { kind: "update", set: { column: "Rejected" } }, `${id} rejected`);
      toast(`${id} rejected${queuedNote(r)}`, { label: "Undo", run: async () => {
        try { const u = await linearWrite(id, { kind: "update", set: { column: back } }, `${id} back to ${back}`); toast(`${id} back in ${back}${queuedNote(u)}`); }
        catch (e) { toast(`Couldn't undo: ${e.message}`); }
      } });
    } catch (e) { putBack(); toast(`Couldn't reject ${id}: ${e.message}`); }
  }
  document.addEventListener("click", e => {
    const b = e.target.closest(".card-wrap .rej-btn, .card-wrap .done-btn");
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    const li = b.closest(".card-wrap");
    if (b.classList.contains("rej-btn")) rejectCard(li);
    else moveCard(li, li.closest(".board").querySelector('.col[data-col="Done"]'));
  }, true);
  document.addEventListener("dragstart", e => {
    const li = e.target.closest?.(".card-wrap");
    if (!li) return;
    dragged = li;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", li.querySelector(".card-id").textContent);
    requestAnimationFrame(() => { li.classList.add("dragging"); li.closest(".board").classList.add("drag-on"); });
  });
  document.addEventListener("dragend", () => {
    dragged?.classList.remove("dragging");
    document.querySelectorAll(".drag-on").forEach(b => b.classList.remove("drag-on"));
    document.querySelectorAll(".col.drop").forEach(c => c.classList.remove("drop"));
    dragged = null;
  });
  const dropCol = e => {
    const col = e.target.closest?.(".col[data-col]");
    return col && dragged && dragged.closest(".board") === col.closest(".board") ? col : null;
  };
  document.addEventListener("dragover", e => {
    const col = dropCol(e);
    if (!col) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "move";
    document.querySelectorAll(".col.drop").forEach(c => c !== col && c.classList.remove("drop"));
    col.classList.add("drop");
  });
  document.addEventListener("dragleave", e => {
    const col = e.target.closest?.(".col[data-col]");
    if (col && !col.contains(e.relatedTarget)) col.classList.remove("drop");
  });
  document.addEventListener("drop", e => {
    const col = dropCol(e);
    if (!col) return;
    e.preventDefault();
    col.classList.remove("drop");
    const before = [...col.querySelectorAll(".card-wrap")].find(c => c !== dragged && e.clientY < c.getBoundingClientRect().top + c.offsetHeight / 2) || null;
    moveCard(dragged, col, before);
  });
  // ---------- peek: a Linear card's description and latest comments, without leaving the desk ----------
  const ISSUES = JSON.parse(document.getElementById("issue-data")?.textContent || "{}");
  const peekBox = document.getElementById("peek");
  let peekLi = null, peekId = null;  // the issue shown, and its board card when it has one
  const drafts = new Map(); // issue id -> unsent comment
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  // The schemes render_desk.safe_url allows; anything else (javascript:, data:) becomes "#".
  const safeHref = u => /^(https?:\/\/|linear:\/\/|mailto:|#)/i.test(u) ? u : "#";
  const link = (_, text, url) => `<a href="${safeHref(url)}">${text}</a>`;
  const mdInline = s => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\(&lt;([^)]+?)&gt;\)/g, link).replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, link)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2">$2</a>');
  const mdBlock = src => {
    const out = []; let list = null, para = [];
    const flush = () => { if (para.length) { out.push("<p>" + mdInline(para.join(" ")) + "</p>"); para = []; } };
    const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const raw of String(src || "").split("\n")) {
      const l = raw.trimEnd(); let m;
      if (!l.trim()) { flush(); close(); continue; }
      if ((m = l.match(/^#{1,4}\s+(.*)/))) { flush(); close(); out.push("<h3>" + mdInline(m[1]) + "</h3>"); continue; }
      if ((m = l.match(/^\s*[-*]\s+(?:\[( |x)\]\s+)?(.*)/))) { flush(); if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; } out.push("<li>" + (m[1] ? (m[1] === "x" ? "☑ " : "☐ ") : "") + mdInline(m[2]) + "</li>"); continue; }
      if ((m = l.match(/^\s*\d+[.)]\s+(.*)/))) { flush(); if (list !== "ol") { close(); out.push("<ol>"); list = "ol"; } out.push("<li>" + mdInline(m[1]) + "</li>"); continue; }
      close(); para.push(l.trim());
    }
    flush(); close();
    return out.join("");
  };
  const daysAgo = iso => { const d = Math.floor((Date.now() - new Date(iso)) / 864e5); return d < 1 ? "today" : d < 2 ? "yesterday" : d + " days ago"; };
  const idOf = li => li.querySelector(".card-id").textContent;
  const cardFor = id => [...document.querySelectorAll(".card-wrap")].find(li => idOf(li) === id);
  const openLinear = id => {
    const href = cardFor(id)?.querySelector("a.card").href || ISSUES[id].url.replace(/^https:\/\/linear\.app\//, "linear://");
    window.open(href, href.startsWith("http") ? "_blank" : "_self");
  };
  // Column a cardless issue would sit in, for undoing a reject.
  const colOf = i => i.state === "Waiting" ? "Waiting" : { triage: "Triage", backlog: "Backlog", unstarted: "Todo", started: "In Progress", completed: "Done" }[i.state_type];
  function renderPeek() {
    const id = peekId, i = ISSUES[id], col = peekLi?.closest(".col");
    const state = col ? col.dataset.col : i.state || "";
    const icon = col ? col.querySelector(".col-h .st").outerHTML : i.icon || "";
    const done = col ? state === "Done" : i.state_type === "completed";
    const gone = !col && i.state_type === "canceled";
    const due = i.due ? new Date(i.due + "T12:00") : null;
    const over = due && due < new Date(new Date().toDateString()) && !done && !gone;
    const prio = i.priority && i.priority !== "No priority" ? i.priority : "";
    const chip = (f, label, inner, cls = "") => `<button type="button" class="pv-chip pv-ed${cls}" data-f="${f}" aria-haspopup="${f === "due" ? "dialog" : "menu"}" aria-expanded="false" aria-label="${label}">${inner}</button>`;
    const waiting = window.ftDesk.pendingFor?.(id) || 0;
    const chips = [
      chip("state", `Status: ${esc(state)}`, `${icon}${esc(state)}`),
      chip("assignee", `Assignee: ${i.assignee ? esc(i.assignee) : "unassigned"}`, `${PERSON_ICON}${i.assignee ? esc(i.assignee) : "Unassigned"}`, i.assignee ? "" : " pv-unset"),
      chip("priority", `Priority: ${prio || "none"}`, `${DESK_ICONS.prio?.[prio || "No priority"] || ""}${esc(prio || "Priority")}`, prio ? "" : " pv-unset"),
      chip("due", due ? `Due ${due.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" })}` : "Due date: none",
        `${CAL_ICON}${due ? "Due " + due.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }) : "Due date"}`, over ? " over" : due ? "" : " pv-unset"),
      ...i.labels.map(l => `<span class="pv-chip">${esc(l)}</span>`),
      waiting ? `<span class="pv-chip pv-sync" role="status"><i aria-hidden="true"></i>Waiting to sync</span>` : "",
    ].join("");
    const comments = i.comments.map(c => comHtml(c)).join("");
    peekBox.innerHTML = `<div class="pv-top"><div class="pv-head"><span class="pv-id">${esc(id)}</span><span aria-hidden="true">·</span><button type="button" class="pv-proj pv-ed" data-f="project" aria-haspopup="menu" aria-expanded="false" aria-label="Project: ${esc(i.project || "none")}">${esc(i.project || "No project")}</button></div>
      <span class="pv-top-b">${gone ? "" : `<button class="pv-x pv-rej" type="button" data-pv="reject" data-tip="Reject|⌫" aria-label="Reject"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.3"/><path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4"/></svg></button>`}${done ? "" : `<button class="pv-x pv-tick" type="button" data-pv="done" data-tip="Mark done|E" aria-label="Mark done"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.3"/><path d="M5.2 8.2l1.9 1.9 3.8-4"/></svg></button>`}
      <button class="pv-x" type="button" data-pv="close" data-tip="Close|Esc" aria-label="Close">×</button></span></div>
      <div class="pv-body"><h2 class="pv-title">${esc(i.title)}</h2><div class="pv-props">${chips}</div>
      <div class="pv-desc pv-main">${mdBlock(i.description) || '<p class="pv-none">No description.</p>'}</div>
      <h3 class="pv-sec">${comments ? "Latest comments" : "Comments"}</h3><div class="pv-coms">${comments}</div>
      <form class="pv-add"><textarea rows="3" placeholder="Leave a comment…" aria-label="Comment on ${esc(id)}"></textarea>
      <button class="btn sm" type="submit">Comment <kbd>${isMac ? "⌘↵" : "Ctrl ↵"}</kbd></button></form></div>
      <div class="pv-foot"><button class="btn primary" type="button" data-pv="open">Open in Linear <kbd>${isMac ? "⌘↵" : "Ctrl ↵"}</kbd></button>
      ${done ? "" : '<button class="btn" type="button" data-pv="done">Mark done <kbd>E</kbd></button>'}
      <button class="btn" type="button" data-pv="start">Start <kbd>S</kbd></button></div>`;
    peekBox.querySelectorAll(".pv-desc a").forEach(a => { a.target = "_blank"; a.rel = "noopener"; });
    const ta = peekBox.querySelector(".pv-add textarea");
    ta.value = drafts.get(id) || "";
    ta.addEventListener("input", () => drafts.set(id, ta.value));
  }
  // ---- state, assignee, priority, due date and project, changed from the peek's chips ----
  const DESK_ICONS = JSON.parse(document.getElementById("desk-icons")?.textContent || "{}");
  const PERSON_ICON = '<svg class="pv-ic" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="5.5" r="2.6" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M3 13.5c.6-2.6 2.6-4 5-4s4.4 1.4 5 4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';
  const CAL_ICON = '<svg class="pv-ic" viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M2.5 6.5h11M5.5 2v3M10.5 2v3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';
  const stateIcon = (name, type) => DESK_ICONS.state?.[name === "Waiting" ? "waiting" : { completed: "done", canceled: "backlog" }[type] || type] || "";
  const META_KEY = "ftdesk.linear.meta";
  let linearMeta = null;
  try { linearMeta = JSON.parse(localStorage.getItem(META_KEY) || "null"); } catch { /* none cached */ }
  const loadMeta = async () => {
    try { linearMeta = await api("/api/linear/meta", {}); try { localStorage.setItem(META_KEY, JSON.stringify(linearMeta)); } catch { /* private mode */ } }
    catch (e) { if (!linearMeta) throw e; }   // offline: the last lists will do
    return linearMeta;
  };
  const isoDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  async function editProp(f, chip) {
    const id = peekId, i = ISSUES[id];
    if (!id) return;
    if (f === "due") {
      window.ftDesk.datePicker(chip, { value: i.due ? new Date(i.due + "T12:00") : null, clear: "Clear", label: "Due date",
        onPick: d => setProp(id, "due", d ? isoDay(d) : null, d ? `due ${d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}` : "no due date") });
      return;
    }
    let m;
    try { m = linearMeta && Date.now() - (linearMeta._at || 0) < 6e5 ? linearMeta : await loadMeta(); m._at = m._at || Date.now(); }
    catch (e) { toast(`Couldn't load Linear's options: ${e.message}`); return; }
    if (peekId !== id) return;
    const curState = i.state;
    const menus = {
      state: () => m.states.map(s => ({ value: s.id, label: s.name, html: stateIcon(s.name, s.type), checked: s.name === curState, state: s })),
      assignee: () => [...m.members.map(p => ({ value: p.id, label: p.name, html: PERSON_ICON, checked: i.assignee === p.name })),
        { value: null, label: "Unassigned", html: PERSON_ICON, checked: !i.assignee }],
      priority: () => m.priorities.map(p => ({ value: p.value, label: p.name, html: DESK_ICONS.prio?.[p.name] || "", checked: (i.priority || "No priority") === p.name })),
      project: () => [...m.projects.map(p => ({ value: p.id, label: p.name, checked: i.project === p.name })), { value: null, label: "No project", checked: !i.project }],
    };
    const labels = { state: "Status", assignee: "Assignee", priority: "Priority", project: "Project" };
    window.ftDesk.menu(chip, { label: labels[f], items: menus[f](), filter: f === "project" ? "Move to project…" : null,
      onPick: it => {
        if (it.checked) return;
        const what = { state: `moved to ${it.label}`, assignee: it.value ? `assigned to ${it.label}` : "unassigned", priority: `priority ${it.label}`,
          project: it.value ? `moved to ${it.label}` : "removed from its project" }[f];
        setProp(id, f, it.value, what, it);
      } });
  }
  // Show the change at once, send it, and undo it here if Linear refuses.
  async function setProp(id, f, value, what, it) {
    const i = ISSUES[id], was = { ...i }, li = peekId === id ? peekLi : cardFor(id);
    const from = li?.closest(".col"), next = li?.nextElementSibling;
    if (f === "state") {
      Object.assign(i, { state: it.state.name, state_type: it.state.type, icon: stateIcon(it.state.name, it.state.type) });
      const col = li && li.closest(".board")?.querySelector(`.col[data-col="${CSS.escape(colOf(i) || "")}"]`);
      if (li && col && col !== from) { col.querySelector(".cards").prepend(li); countCol(from); countCol(col); }
      else if (li && !col) { li.remove(); countCol(from); if (peekLi === li) peekLi = null; }
    } else if (f === "assignee") i.assignee = it.value ? it.label : null;
    else if (f === "priority") i.priority = it.label;
    else if (f === "due") i.due = value;
    else if (f === "project") i.project = it.value ? it.label : null;
    if (peekId === id) renderPeek();
    try {
      const r = await linearWrite(id, { kind: "update", set: { [f]: value } }, `${id} ${what}`);
      toast(`${id} ${what}${queuedNote(r)}`);
    } catch (e) {
      Object.assign(i, was);
      if (li && from?.isConnected && li.closest(".col") !== from) { from.querySelector(".cards").insertBefore(li, next && next.parentNode === from.querySelector(".cards") ? next : null); countCol(from); }
      if (peekId === id) { if (li && !peekLi && li.isConnected) peekLi = li; renderPeek(); }
      toast(`Couldn't change ${id}: ${e.message}`);
    }
  }
  peekBox?.addEventListener("click", e => {
    const b = e.target.closest(".pv-ed");
    if (b) { e.stopPropagation(); editProp(b.dataset.f, b); }
  }, true);
  // Peek a board card, or an issue by id (its card if it has one, else the issue on its own).
  function peek(target) {
    const li = typeof target === "string" ? cardFor(target) : target;
    const id = li ? idOf(li) : target;
    if (!id || !ISSUES[id]) return false;
    document.querySelector(".card-wrap.pv-on")?.classList.remove("pv-on");
    peekLi = li || null; peekId = id;
    if (li) { li.classList.add("pv-on"); if (sel !== li && visible(li)) select(li); }
    renderPeek();
    peekBox.hidden = false; peekBox.scrollTop = 0;
    peekBox.querySelector(".pv-body").scrollTop = 0;
    return true;
  }
  function closePeek() {
    peekLi?.classList.remove("pv-on");
    peekLi = peekId = null; peekBox.hidden = true;
  }
  const doneCol = li => li.closest(".board").querySelector('.col[data-col="Done"]');
  const stepPeek = d => {
    if (!peekLi) return;
    const cards = [...peekLi.closest(".board").querySelectorAll(".card-wrap")];
    const next = cards[cards.indexOf(peekLi) + d];
    if (next) peek(next);
  };
  const comHtml = c => `<div class="pv-com"><div class="pv-com-h"><b>${esc(c.who)}</b> ${daysAgo(c.at)}</div><div class="pv-desc">${mdBlock(c.body)}</div></div>`;
  async function postComment(form) {
    const ta = form.querySelector("textarea"), btn = form.querySelector("button"), id = peekId;
    const body = ta.value.trim();
    if (!body || btn.disabled) return;
    btn.disabled = ta.readOnly = true;
    try {
      const r = await linearWrite(id, { kind: "comment", body }, `Comment on ${id}`);
      const c = { who: r.who || "You", at: r.at || new Date().toISOString(), body };
      ISSUES[id].comments.unshift(c); drafts.delete(id);
      if (peekId === id) { renderPeek(); peekBox.querySelector(".pv-sec").textContent = "Latest comments"; }
      toast(`Comment added to ${id}${queuedNote(r)}`);
    } catch (err) {
      toast(`Couldn't comment on ${id}: ${err.message}`);
      btn.disabled = ta.readOnly = false;
    }
  }
  // ---- edit the title or description in place (click; Linear stores the description as Markdown) ----
  async function saveEdit(field, value) {
    const li = peekLi, id = peekId, i = ISSUES[id];
    if (value === i[field] || (field === "title" && !value.trim())) { renderPeek(); return; }
    try {
      const r = await linearWrite(id, { kind: "update", set: { [field]: value } }, `${id} ${field}`);
      i[field] = value;
      if (field === "title" && li) li.querySelector(".card-title").textContent = value.trim();
      toast(`${id} ${field} saved${queuedNote(r)}`);
    } catch (err) { toast(`Couldn't save ${id}: ${err.message}`); }
    if (peekId === id) renderPeek();
  }
  function editField(field) {
    if (!peekId || peekBox.querySelector(".pv-edit")) return;
    const i = ISSUES[peekId];
    const host = peekBox.querySelector(field === "title" ? ".pv-title" : ".pv-main");
    const ed = document.createElement("form"); ed.className = "pv-edit pv-edit-" + field;
    ed.innerHTML = `<textarea aria-label="${field === "title" ? "Title" : "Description"}"></textarea>
      <div class="pv-edit-b"><button class="btn sm" type="button" data-e="cancel">Cancel <kbd>Esc</kbd></button>
      <button class="btn sm primary" type="submit">Save <kbd>${field === "title" ? "↵" : isMac ? "⌘↵" : "Ctrl ↵"}</kbd></button></div>`;
    const ta = ed.querySelector("textarea");
    ta.value = i[field] || "";
    host.replaceWith(ed);
    const fit = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + 2 + "px"; };
    ta.addEventListener("input", fit); fit();
    ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
    const done = save => { ed.remove(); save ? saveEdit(field, field === "title" ? ta.value.replace(/\s*\n\s*/g, " ").trim() : ta.value) : renderPeek(); };
    ed.addEventListener("submit", e => { e.preventDefault(); done(true); });
    ed.querySelector('[data-e="cancel"]').addEventListener("click", () => done(false));
    ta.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(false); }
      else if (e.key === "Enter" && (field === "title" ? !e.shiftKey : (isMac ? e.metaKey : e.ctrlKey))) { e.preventDefault(); e.stopPropagation(); done(true); }
    });
  }
  peekBox?.addEventListener("click", e => {
    if (getSelection()?.toString()) return;  // selecting text to copy isn't a request to edit
    if (e.target.closest(".pv-title")) editField("title");
    else if (e.target.closest(".pv-main") && !e.target.closest("a")) editField("description");
  });
  peekBox?.addEventListener("submit", e => { if (e.target.matches(".pv-add")) { e.preventDefault(); postComment(e.target); } });
  peekBox?.addEventListener("keydown", e => {
    if (e.target.matches(".pv-add textarea")) {
      if (e.key === "Enter" && (isMac ? e.metaKey : e.ctrlKey)) { e.preventDefault(); e.stopPropagation(); postComment(e.target.form); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); e.target.blur(); }
    }
  });
  peekBox?.addEventListener("click", e => {
    const b = e.target.closest("[data-pv]");
    if (b && peekId) peekAction(b.dataset.pv);
  });
  // Board cards act through the board; an issue with no card here is changed in Linear directly.
  async function peekAction(a) {
    const id = peekId, li = peekLi, i = ISSUES[id];
    if (a === "close") return closePeek();
    if (a === "open") return openLinear(id);
    if (a === "start") return li ? startOne(li) : start([{ key: "issue:" + id, note: "", label: `${id} ${i.title}` }]);
    if (li) return a === "done" ? moveCard(li, doneCol(li)) : a === "reject" ? rejectCard(li) : undefined;
    const was = { state: i.state, state_type: i.state_type, icon: i.icon };
    const set = (r, type, icon) => Object.assign(i, { state: r.state, state_type: type, icon });
    if (a === "done") {
      try { const r = await linearWrite(id, { kind: "update", set: { column: "Done" } }, `${id} done`); set({ state: r.state || "Done" }, "completed", document.querySelector('.col[data-col="Done"] .col-h .st')?.outerHTML || i.icon); toast(`${id} moved to ${r.state || "Done"}${queuedNote(r)}`); }
      catch (e) { toast(`Couldn't move ${id}: ${e.message}`); }
      if (peekId === id) renderPeek();
    }
    if (a === "reject") {
      const back = colOf(was);
      try {
        const r = await linearWrite(id, { kind: "update", set: { column: "Rejected" } }, `${id} rejected`);
        set({ state: r.state || "Rejected" }, "canceled", i.icon); if (peekId === id) closePeek();
        toast(`${id} rejected${queuedNote(r)}`, back && { label: "Undo", run: async () => {
          try { const u = await linearWrite(id, { kind: "update", set: { column: back } }, `${id} back to ${back}`); Object.assign(i, was); toast(`${id} back in ${back}${queuedNote(u)}`); }
          catch (e) { toast(`Couldn't undo: ${e.message}`); }
        } });
      } catch (e) { toast(`Couldn't reject ${id}: ${e.message}`); }
    }
  }
  // A plain click on a card, or on an issue link elsewhere on the desk, peeks; ⌘-click still opens Linear.
  document.addEventListener("click", e => {
    const plain = !e.metaKey && !e.ctrlKey && !e.shiftKey;
    const a = e.target.closest("a.card");
    if (a && plain && peek(a.closest(".card-wrap"))) { e.preventDefault(); return; }
    const ref = !a && plain && e.target.closest('a[href*="/issue/AS-"]:not(#peek a)');
    const id = ref && ref.getAttribute("href").match(/\/issue\/(AS-\d+)/)?.[1];
    if (id && peek(id)) { e.preventDefault(); return; }
    // A plain click elsewhere on a Linear item's row peeks its issue too, as a click on its source tag does.
    const row = plain && !getSelection().toString() && e.target.closest("#view-desk .row");
    if (row && !e.target.closest("a, button, input, textarea, .acts, .composer")) {
      const link = row.querySelector('a.head[href*="/issue/AS-"], a.tag[href*="/issue/AS-"]');
      const rid = link?.getAttribute("href").match(/\/issue\/(AS-\d+)/)?.[1];
      if (rid && peek(rid)) return;
    }
    if (peekId && !e.target.closest("#peek, .card-wrap, .toast, .fm-pop, .cal-pop")) closePeek();
  }, true);
  document.addEventListener("keydown", e => {
    if (!peekId || e.target.closest("input, textarea, select, [contenteditable]")) return;
    const k = e.key;
    if (k === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closePeek(); return; }
    if (k === "Enter" && (isMac ? e.metaKey : e.ctrlKey)) { e.preventDefault(); e.stopImmediatePropagation(); openLinear(peekId); return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    // E and S act on the peeked issue, not on whatever desk row is selected behind it.
    if (k === "e" || k === "s") { e.preventDefault(); e.stopImmediatePropagation(); peekAction(k === "e" ? "done" : "start"); return; }
    if (k === "Backspace" || k === "Delete") { e.preventDefault(); e.stopImmediatePropagation(); if (peekBox.querySelector(".pv-rej")) peekAction("reject"); return; }
    if (k === "m") { e.preventDefault(); e.stopImmediatePropagation(); peekBox.querySelector(".pv-add textarea")?.focus(); return; }
    if (k === "Enter") { e.preventDefault(); e.stopImmediatePropagation(); return; }
    if (["j", "k", "ArrowDown", "ArrowUp"].includes(k)) { e.preventDefault(); e.stopImmediatePropagation(); stepPeek(k === "j" || k === "ArrowDown" ? 1 : -1); }
  }, true);
  // ---------- projects: snooze, fold away, daily review ----------
  // Snoozed projects fold into one line under the table. "Until next refresh" lasts until the next full desk run;
  // dated snoozes wake at their time. After each desk run, projects with a board carry a dot until opened or snoozed.
  const projects = (() => {
    const table = () => document.querySelector(".proj-tbl");
    const snoozes = new Map(), reviewed = new Set();
    const load = PS => {
      snoozes.clear(); reviewed.clear();
      Object.entries(PS.snoozes || {}).forEach(([k, v]) => snoozes.set(k, v ? new Date(v) : null));
      (PS.reviewed || []).forEach(n => reviewed.add(n));
    };
    load(JSON.parse(document.getElementById("project-state")?.textContent || "{}"));
    const all = () => [...document.querySelectorAll(".proj-tbl tbody.proj")];
    const nameOf = tb => tb.dataset.name;
    const ICON_Z = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8.6" r="5.6" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M8 5.8v3l2 1.2M3 2.6l1.8-1.2M13 2.6l-1.8-1.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
    const ICON_C = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 3.2h11v7.3H7l-3.5 2.8v-2.8h-1z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    const CHEV_Z = '<svg class="chev" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5l3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    const at = (d, h) => { const x = new Date(d); x.setHours(h, 0, 0, 0); return x; };
    const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
    const nextMonday = d => at(addDays(d, (8 - d.getDay()) % 7 || 7), 9);
    const hm = d => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    const dayHm = d => d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }) + " " + hm(d);
    const short = d => {
      const now = new Date();
      return d.toDateString() === now.toDateString() ? hm(d)
        : d.toDateString() === addDays(now, 1).toDateString() ? "tomorrow " + hm(d)
        : d - now < 6 * 864e5 ? d.toLocaleDateString("en-GB", { weekday: "short" }) + " " + hm(d)
        : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
    };
    const options = () => {
      const now = new Date(), two = new Date(+now + 2 * 36e5);
      return [
        { label: "Until next refresh", when: "", until: null },
        { label: "For 2 hours", when: hm(two), until: two },
        { label: "Until tomorrow", when: "09:00", until: at(addDays(now, 1), 9) },
        { label: "Until Monday", when: "09:00", until: nextMonday(now) },
        { label: "For 7 days", when: dayHm(at(addDays(now, 7), 9)), until: at(addDays(now, 7), 9) },
      ];
    };
    const asleep = name => snoozes.has(name) && (snoozes.get(name) === null || snoozes.get(name) > new Date());
    const chipText = until => until === null ? "until refresh" : "until " + short(until);
    const hasBoard = tb => !!tb.querySelector(".bt[aria-controls]") && tb.querySelector(".bt-n")?.textContent !== "0";

    // per-row snooze button, before the board button (added again whenever the table is swapped in)
    const decorate = () => all().forEach(tb => {
      const cell = tb.querySelector(".p-bt");
      if (cell.querySelector(".p-bt-in")) return;
      const inner = document.createElement("span"); inner.className = "p-bt-in";
      inner.append(...cell.childNodes); cell.append(inner);
      const zz = document.createElement("button");
      zz.type = "button"; zz.className = "zz"; zz.innerHTML = ICON_Z; zz.dataset.tip = "Snooze|Z";
      zz.setAttribute("aria-label", "Snooze " + nameOf(tb)); zz.setAttribute("aria-haspopup", "menu");
      const pc = document.createElement("button");
      pc.type = "button"; pc.className = "pc"; pc.innerHTML = ICON_C; pc.dataset.tip = "Comment, then start|M";
      pc.setAttribute("aria-label", "Comment on " + nameOf(tb));
      inner.prepend(pc, zz);
    });
    document.addEventListener("click", e => {
      const pc = e.target.closest(".proj-tbl .pc");
      if (pc) { e.stopPropagation(); compose(pc.closest("tbody.proj")); return; }
      const add = e.target.closest(".proj-tbl .col-add");
      if (add) { e.stopPropagation(); addForm(add.closest(".col")); return; }
      const zz = e.target.closest(".proj-tbl .zz");
      if (zz) { e.stopPropagation(); const tb = zz.closest("tbody.proj"); menuFor === tb ? closeMenu() : openMenu(tb); return; }
      // Opening a project's board counts as reviewing it.
      const bt = e.target.closest(".proj-tbl .bt[aria-controls]");
      if (bt) markReviewed(bt.closest("tbody.proj"));
    }, true);

    // the menu
    const menu = document.createElement("div");
    menu.className = "zmenu"; menu.hidden = true; menu.setAttribute("role", "menu");
    document.body.appendChild(menu);
    let menuFor = null;
    function openMenu(tb, pt) {
      closeMenu(); closeCtx();
      menuFor = tb;
      custom = null;
      menu.innerHTML = `<h3>Snooze ${esc(nameOf(tb))}</h3>` + options().map((o, i) =>
        `<button class="zi" type="button" role="menuitem" data-i="${i}"><span>${o.label}</span><span class="when">${o.when}</span><kbd>${i + 1}</kbd></button>`).join("") +
        `<hr><form class="zdate"><button type="button" class="zdate-btn" aria-haspopup="dialog" aria-expanded="false">${CAL}<span>Another time</span></button><button class="btn sm" type="submit">Snooze</button></form>`;
      menu.hidden = false;
      const zz = tb.querySelector(".zz"), r = pt ? { left: pt.x, right: pt.x + menu.offsetWidth, top: pt.y, bottom: pt.y } : zz.getBoundingClientRect();
      menu.style.left = Math.max(8, Math.min(innerWidth - menu.offsetWidth - 8, r.right - menu.offsetWidth)) + "px";
      menu.style.top = (r.bottom + 6 + menu.offsetHeight > innerHeight ? r.top - menu.offsetHeight - 6 : r.bottom + 6) + "px";
      zz.setAttribute("aria-expanded", "true");
      menu.querySelector(".zi").focus();
    }
    function closeMenu() {
      if (menu.hidden) return;
      if (menu.querySelector(".zdate-btn[aria-expanded=true]")) window.ftDesk.closePopover?.(false);
      menu.hidden = true;
      menuFor?.querySelector(".zz").removeAttribute("aria-expanded");
      menuFor = null;
    }
    // The custom time: a calendar opens from the date button, and Snooze snoozes until the time picked there.
    let custom = null;
    const CAL = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M2.5 6.5h11M5.5 2v3M10.5 2v3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';
    const pickCustom = btn => window.ftDesk.datePicker(btn, {
      value: custom, time: true, min: new Date(), label: "Snooze until",
      onPick: d => {
        custom = d;
        btn.querySelector("span").textContent = dayHm(d);
        menu.querySelector(".zdate .btn")?.focus();
      },
    });
    menu.addEventListener("click", e => {
      const b = e.target.closest(".zi");
      if (b) { snooze(menuFor, options()[+b.dataset.i].until); return; }
      const c = e.target.closest(".zdate-btn");
      if (c) pickCustom(c);
    });
    menu.addEventListener("submit", e => {
      e.preventDefault();
      if (custom && custom > new Date()) snooze(menuFor, custom);
      else if (!custom) pickCustom(menu.querySelector(".zdate-btn"));
      else toast("Pick a time in the future");
    });
    document.addEventListener("click", e => { if (!e.target.closest(".zmenu, .zz, .cal-pop")) { closeMenu(); closeCtx(); } });

    // Durable: a snooze made while the server is down is kept here and sent when it is back.
    const post = (name, action, until) => durable("/api/project", { name, action, until: until ? until.toISOString() : null },
      action === "snooze" ? `Snooze ${name}` : action === "wake" ? `Wake ${name}` : `Review ${name}`);
    async function snooze(tb, until) {
      const name = nameOf(tb), before = snoozes.has(name) ? snoozes.get(name) : undefined;
      closeMenu();
      if (tb.classList.contains("open")) tb.querySelector(".bt[aria-expanded]")?.click();
      const row = tb.querySelector(".p-row");
      if (sel === row) { const rows = navItems(), i = rows.indexOf(row); select(rows[i + 1] || rows[i - 1] || null); }
      snoozes.set(name, until); reviewed.add(name); render();
      try {
        const r = await post(name, "snooze", until);
        toast(`${name} snoozed ${chipText(until)}${queuedNote(r)}`, { label: "Undo", run: async () => {
          before === undefined ? snoozes.delete(name) : snoozes.set(name, before); render();
          try { await (before === undefined ? post(name, "wake") : post(name, "snooze", before)); } catch (e) { toast(`Couldn't undo: ${e.message}`); }
        } });
      } catch (e) {
        before === undefined ? snoozes.delete(name) : snoozes.set(name, before); render();
        toast(`Couldn't snooze ${name}: ${e.message}`);
      }
    }
    async function wake(name) {
      const before = snoozes.get(name);
      snoozes.delete(name); render();
      try { await post(name, "wake"); toast(`${name} is back`); }
      catch (e) { snoozes.set(name, before); render(); toast(`Couldn't wake ${name}: ${e.message}`); }
    }
    function markReviewed(tb) {
      const name = nameOf(tb);
      if (reviewed.has(name)) return;
      reviewed.add(name); render();
      post(name, "reviewed").catch(e => toast(`Couldn't save the review of ${name}: ${e.message}`));
    }

    // Per area, paused and other people's boards fold into one line of show/hide buttons. Which are open is
    // remembered in this browser, and reapplied whenever the server swaps the table in.
    const FOLD_KEY = "desk-proj-folds";
    let openFolds = new Set();
    try { openFolds = new Set(JSON.parse(localStorage.getItem(FOLD_KEY) || "[]")); } catch (_) {}
    function applyFolds() {
      document.querySelectorAll(".proj-tbl .g-fold-btn").forEach(b => b.setAttribute("aria-expanded", String(openFolds.has(b.dataset.fold))));
      all().forEach(tb => { if (tb.dataset.fold) tb.hidden = !openFolds.has(tb.dataset.fold); });
      document.querySelectorAll(".proj-tbl .p-plabel[data-fold]").forEach(tb => { tb.hidden = !openFolds.has(tb.dataset.fold); });
    }
    document.addEventListener("click", e => {
      const b = e.target.closest(".proj-tbl .g-fold-btn");
      if (!b) return;
      const k = b.dataset.fold;
      openFolds.has(k) ? openFolds.delete(k) : openFolds.add(k);
      try { localStorage.setItem(FOLD_KEY, JSON.stringify([...openFolds])); } catch (_) {}
      render();
    });

    // A role heading shows or hides its whole table; which roles are closed is remembered in this browser.
    const ROLE_KEY = "desk-roles-closed";
    let closedRoles = new Set();
    try { closedRoles = new Set(JSON.parse(localStorage.getItem(ROLE_KEY) || "[]")); } catch (_) {}
    function applyRoles() {
      document.querySelectorAll(".sec-projects .role-toggle").forEach(b => {
        const closed = closedRoles.has(b.dataset.role);
        b.setAttribute("aria-expanded", String(!closed));
        const wrap = document.getElementById(b.getAttribute("aria-controls"));
        if (wrap) wrap.hidden = closed;
      });
    }
    document.addEventListener("click", e => {
      const b = e.target.closest(".sec-projects .role-toggle");
      if (!b) return;
      const k = b.dataset.role;
      closedRoles.has(k) ? closedRoles.delete(k) : closedRoles.add(k);
      try { localStorage.setItem(ROLE_KEY, JSON.stringify([...closedRoles])); } catch (_) {}
      applyRoles();
    });

    // rendering
    const count = document.createElement("span"); count.className = "rv-count";
    function render() {
      if (!table()) return;
      const restoreFocus = keepFocus();
      const sleeping = all().filter(tb => asleep(nameOf(tb))), awake = all().filter(tb => !asleep(nameOf(tb)));
      all().forEach(tb => {
        tb.classList.remove("folded", "in-fold");
        tb.querySelector(".zchip")?.remove();
        tb.querySelector(".rv-dot")?.remove();
      });
      // Snoozed rows stay in place, hidden or dimmed, and fold into their area's line beside its paused and other
      // people's boards. Moving rows would break the grouping, and moving a row blurs any field being typed in.
      document.querySelectorAll(".proj-tbl .p-folds").forEach(row => {
        const area = row.dataset.area, z = sleeping.filter(tb => tb.dataset.area === area);
        row.querySelector(".g-snz-btn")?.remove();
        if (z.length) {
          const b = document.createElement("button");
          b.type = "button"; b.className = "g-fold-btn g-snz-btn"; b.dataset.fold = area + ":snoozed";
          b.innerHTML = `${ICON_Z}<span>Snoozed: ${z.map(tb => esc(nameOf(tb))).join(", ")}</span>${CHEV_Z}`;
          row.querySelector("td").appendChild(b);
        }
        row.hidden = !row.querySelector(".g-fold-btn");
      });
      applyFolds(); applyRoles();
      const todo = awake.filter(tb => !tb.dataset.fold && hasBoard(tb) && !reviewed.has(nameOf(tb)));
      todo.forEach(tb => tb.querySelector(".p-name").append(Object.assign(document.createElement("span"), { className: "rv-dot", title: "Not looked at since the last refresh" })));
      {
        sleeping.forEach(tb => {
          const open = openFolds.has(tb.dataset.area + ":snoozed");
          tb.classList.add(open ? "in-fold" : "folded");
          if (tb.classList.contains("open") && !open) tb.querySelector(".bt[aria-expanded]")?.click();
          const chip = document.createElement("span");
          chip.className = "zchip";
          chip.innerHTML = `${ICON_Z}${chipText(snoozes.get(nameOf(tb)))}<button type="button" aria-label="Wake ${esc(nameOf(tb))}" data-tip="Wake now">×</button>`;
          chip.querySelector("button").addEventListener("click", e => { e.stopPropagation(); wake(nameOf(tb)); });
          tb.querySelector(".p-name").appendChild(chip);
        });
      }
      const counted = awake.filter(tb => !tb.dataset.fold).length;  // folded paused and other people's boards don't count
      const h = document.getElementById("projects-h");
      h?.querySelector(".sec-n") && (h.querySelector(".sec-n").textContent = counted);
      const nav = document.querySelector('[data-target="projects"] .nav-n');
      if (nav) nav.textContent = counted;
      count.textContent = todo.length ? `${todo.length} to review` : "";
      if (h && todo.length) h.appendChild(count); else count.remove();
      restoreFocus();
    }
    decorate(); render();
    setInterval(() => { if ([...snoozes].some(([, u]) => u && u <= new Date())) render(); }, 30e3);

    // Z opens the menu for the selected project; Z again or 1 snoozes until refresh, 2–5 pick the others.
    document.addEventListener("keydown", e => {
      if (e.target.closest("input, textarea, select, [contenteditable]")) return;
      if (!ctx.hidden) {
        const stop = () => { e.preventDefault(); e.stopImmediatePropagation(); };
        if (e.key === "Escape") { stop(); closeCtx(); return; }
        if (e.key === "Enter" || e.key === " ") { e.stopImmediatePropagation(); return; }
        const hit = [...ctx.querySelectorAll(".zi")].find(b => b.dataset.k === e.key.toLowerCase());
        if (hit) { stop(); hit.click(); return; }
        if (["ArrowDown", "ArrowUp", "j", "k"].includes(e.key)) {
          stop();
          const items = [...ctx.querySelectorAll(".zi")], i = items.indexOf(document.activeElement);
          items[(i + (e.key === "ArrowDown" || e.key === "j" ? 1 : -1) + items.length) % items.length].focus();
        }
        return;
      }
      if (!menu.hidden) {
        const stop = () => { e.preventDefault(); e.stopImmediatePropagation(); };
        if (e.key === "Escape") { stop(); closeMenu(); return; }
        if (e.key === "Enter" || e.key === " ") { e.stopImmediatePropagation(); return; }   // the focused button's own click; no desk shortcut
        if (e.key === "z" || /^[1-5]$/.test(e.key)) { stop(); snooze(menuFor, options()[e.key === "z" ? 0 : +e.key - 1].until); return; }
        if (["ArrowDown", "ArrowUp", "j", "k"].includes(e.key)) {
          stop();
          const items = [...menu.querySelectorAll(".zi, .zdate button")], i = items.indexOf(document.activeElement);
          items[(i + (e.key === "ArrowDown" || e.key === "j" ? 1 : -1) + items.length) % items.length].focus();
        }
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey || !sel || !visible(sel)) return;
      const key = e.shiftKey && e.key.toLowerCase() === "a" ? "A" : e.key;   // ⇧A adds a task; A alone is quick agent
      if (key === "A" && sel.classList.contains("card-wrap") && sel.closest(".proj-tbl")) { e.preventDefault(); e.stopImmediatePropagation(); addForm(sel.closest(".col")); return; }
      if (!sel.classList.contains("p-row") || !["z", "m", "A"].includes(key)) return;
      e.preventDefault(); e.stopImmediatePropagation();
      const tb = sel.closest(".proj");
      if (key === "z") openMenu(tb); else if (key === "m") compose(tb); else addTo(tb);
    }, true);
    // ---------- right-click menu on a project row ----------
    const ctx = document.createElement("div");
    ctx.className = "zmenu pmenu"; ctx.hidden = true; ctx.setAttribute("role", "menu");
    document.body.appendChild(ctx);
    let ctxFor = null, ctxAt = null;
    function closeCtx() { if (!ctx.hidden) { ctx.hidden = true; ctxFor = null; } }
    function openCtx(tb, x, y) {
      closeMenu(); closeCtx();
      ctxFor = tb; ctxAt = { x, y };
      const name = nameOf(tb), link = tb.dataset.link;
      const item = (k, label, key, shown = key.toUpperCase()) => `<button class="zi" type="button" role="menuitem" data-k="${key}" data-a="${k}"><span>${label}</span><span></span><kbd>${shown}</kbd></button>`;
      ctx.innerHTML = `<h3>${esc(name)}</h3>` + item("comment", "Comment, then start", "m") +
        (tb.querySelector(".board") ? item("add", "Add a task", "a", "⇧A") : "") +
        (asleep(name) ? item("wake", "Wake now", "w") : item("snooze", "Snooze…", "z")) +
        (link ? item("open", "Open in Linear", "o") : "");
      ctx.hidden = false;
      ctx.style.left = Math.max(8, Math.min(innerWidth - ctx.offsetWidth - 8, x)) + "px";
      ctx.style.top = Math.max(8, Math.min(innerHeight - ctx.offsetHeight - 8, y)) + "px";
      ctx.querySelector(".zi").focus();
    }
    ctx.addEventListener("click", e => {
      const b = e.target.closest(".zi"), tb = ctxFor, at = ctxAt;
      if (!b || !tb) return;
      e.stopPropagation(); closeCtx();
      const a = b.dataset.a;
      if (a === "comment") compose(tb);
      else if (a === "add") addTo(tb);
      else if (a === "snooze") openMenu(tb, at);
      else if (a === "wake") wake(nameOf(tb));
      else if (a === "open") { const href = tb.dataset.link; window.open(href, href.startsWith("http") ? "_blank" : "_self"); }
    });
    document.addEventListener("contextmenu", e => {
      const row = e.target.closest(".proj-tbl .p-row");
      if (!row) { closeCtx(); return; }
      e.preventDefault();
      select(row);
      openCtx(row.closest("tbody.proj"), e.clientX, e.clientY);
    });
    addEventListener("scroll", e => { if (e.isTrusted) closeCtx(); }, true);  // not the synthetic scroll after a region swap

    // ---------- comment on a project: Peter's instructions open a Claude Code tab on it ----------
    function compose(tb) {
      let tr = tb.querySelector("tr.p-cm");
      if (tr) { tr.querySelector("textarea").focus(); return; }
      const name = nameOf(tb), mod = isMac ? "⌘" : "Ctrl+";
      tr = document.createElement("tr"); tr.className = "p-cm";
      tr.innerHTML = `<td colspan="5" data-no-edit><div class="composer"><textarea placeholder="What should Claude do?" aria-label="Instructions for Claude on ${esc(name)}"></textarea>
        <div class="cf"><button class="cancel" type="button">Cancel</button><span class="sp"></span>
        <button class="btn sm accent s2" type="button">${ICON.play} Start task <kbd>${mod}↵</kbd></button></div></div></td>`;
      tb.querySelector(".p-row").after(tr);
      const ta = tr.querySelector("textarea"); ta.focus();
      const row = tb.querySelector(".p-row");
      const close = () => { tr.remove(); select(row); };
      tr.querySelector(".cancel").onclick = close;
      tr.querySelector(".s2").onclick = () => {
        const v = ta.value.trim(); close();
        start([{ key: "project:" + name, note: v, label: name }]);
      };
      ta.addEventListener("keydown", e => {
        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); tr.querySelector(".s2").click(); }
        if (e.key === "Escape") { e.preventDefault(); close(); }
      });
    }

    // ---------- add a Linear task to a project's board ----------
    function addTo(tb) {
      const bt = tb.querySelector(".bt[aria-controls]");
      if (!bt) { toast(`${nameOf(tb)} has no Linear project`); return; }
      if (bt.getAttribute("aria-expanded") !== "true") bt.click();
      addForm(tb.querySelector('.col[data-col="Todo"]'));
    }
    function addForm(col) {
      const open = col.querySelector(".add-form");
      if (open) { open.querySelector("input").focus(); return; }
      const name = nameOf(col.closest("tbody.proj"));
      const li = document.createElement("li"); li.className = "add-card";
      li.innerHTML = `<form class="add-form"><input type="text" maxlength="300" placeholder="Title" aria-label="New task title">
        <textarea placeholder="Description" aria-label="New task description"></textarea>
        <div class="cf"><button class="cancel" type="button">Cancel</button><span class="sp"></span>
        <button class="btn sm accent" type="submit">Add <kbd>↵</kbd></button></div></form>`;
      col.querySelector(".cards").prepend(li); col.classList.remove("empty");
      const f = li.querySelector("form"), input = f.querySelector("input"), ta = f.querySelector("textarea");
      input.focus();
      const close = () => { li.remove(); col.classList.toggle("empty", !col.querySelector(".card-wrap")); };
      f.querySelector(".cancel").onclick = close;
      f.addEventListener("keydown", e => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
        if (e.key === "Enter" && e.target === ta && (e.metaKey || e.ctrlKey)) { e.preventDefault(); f.requestSubmit(); }
      });
      f.addEventListener("submit", e => {
        e.preventDefault();
        const title = input.value.trim();
        if (!title) { input.focus(); return; }
        send(li, col, name, title, ta.value.trim(), false);
      });
    }
    async function send(li, col, name, title, description, force) {
      li.innerHTML = `<div class="card card-pending"><span class="card-top"><span class="card-id">Adding…</span></span><span class="card-title"></span></div>`;
      li.querySelector(".card-title").textContent = title;
      if (!li.isConnected) col.querySelector(".cards").prepend(li);
      try {
        const r = await write("/api/add", { project: name, column: col.dataset.col, title, description, force });
        if (r.similar) {
          li.remove();
          toast(`Not added: ${r.similar.id} is open with a similar title`, { label: "Add anyway", run: () => send(li, col, name, title, description, true) });
          return;
        }
        li.querySelector(".card-id").textContent = r.id;
        toast(`${r.id} added to ${name}`);
      } catch (e) { li.remove(); toast(`Couldn't add the task: ${e.message}`); }
    }

    return {
      menuOpen: () => !menu.hidden || !ctx.hidden || !!window.ftDesk.popoverOpen?.(),
      // After the table has been swapped in: take the server's snoozes and review marks, and decorate the new rows.
      adopt: PS => { if (PS) load(PS); decorate(); render(); },
    };
  })();
  sync();
})();
