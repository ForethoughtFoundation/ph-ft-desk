// Quick create (Q, from anywhere on the desk): Linear and Todoist tasks typed with Todoist-style dates, #project,
// +person and p1–p4 in each task's first line. The server (desk_quickadd.py) creates them, or holds them in its
// outbox until the connection is back; while the server itself is unreachable they wait in localStorage.
(() => {
  // ================================================================ parser (pure: node tests load it)
  const WEEKDAYS = { sun: 0, sunday: 0, mon: 1, monday: 1, tue: 2, tues: 2, tuesday: 2, wed: 3, weds: 3, wednesday: 3,
    thu: 4, thur: 4, thurs: 4, thursday: 4, fri: 5, friday: 5, sat: 6, saturday: 6 };
  const MONTHS = { jan: 0, january: 0, feb: 1, february: 1, mar: 2, march: 2, apr: 3, april: 3, may: 4, jun: 5, june: 5,
    jul: 6, july: 6, aug: 7, august: 7, sep: 8, sept: 8, september: 8, oct: 9, october: 9, nov: 10, november: 10, dec: 11, december: 11 };
  const NUMWORDS = { a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10 };
  const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const alt = o => Object.keys(o).sort((a, b) => b.length - a.length).join("|");
  const WD = alt(WEEKDAYS), MO = alt(MONTHS), NW = alt(NUMWORDS);
  // A token starts at a word start that isn't escaped with a backslash or part of a #project or +person.
  const L = "(?<![\\p{L}\\p{N}_\\\\#+@/])", R = "(?![\\p{L}\\p{N}_])";

  const pad = n => String(n).padStart(2, "0");
  const day0 = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
  const isoDate = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hm = (h, m) => `${pad(h)}:${pad(m)}`;
  const upcoming = (today, wd) => addDays(today, (wd - today.getDay() + 7) % 7);          // today counts
  const nextMonday = today => addDays(today, (1 - today.getDay() + 7) % 7 || 7);          // never today
  const inNextWeek = (today, wd) => addDays(nextMonday(today), (wd + 6) % 7);             // that day of next week
  const validDay = (y, m, d) => d >= 1 && d <= new Date(y, m + 1, 0).getDate();
  // A day and month with no year: this year, or next year once it has passed.
  const dayMonth = (today, m, d, y) => {
    if (y != null) { if (y < 100) y += 2000; return validDay(y, m, d) ? new Date(y, m, d) : null; }
    for (const yy of [today.getFullYear(), today.getFullYear() + 1]) {
      if (!validDay(yy, m, d)) continue;
      const c = new Date(yy, m, d);
      if (c >= today) return c;
    }
    return null;
  };
  const num = s => NUMWORDS[s.toLowerCase()] ?? parseInt(s, 10);
  // "at 3" with no am or pm: business hours, so 1–7 is afternoon and 8–12 morning.
  const guessHour = h => (h >= 1 && h <= 7 ? h + 12 : h);

  const RULES = [
    { kind: "date", re: "(today|tod|tomorrow|tmrw|tmr|tom)", fn: (m, c) => {
      if (/^(tom|tod)$/i.test(m[1]) && m[1] !== m[1].toLowerCase()) return null;   // "Tom" is a name
      return { date: /^tod/i.test(m[1]) ? c.today : addDays(c.today, 1) };
    } },
    { kind: "date", re: `(?:on\\s+)?(?:(next|this)\\s+)?(${WD})`, fn: (m, c) => {
      const wd = WEEKDAYS[m[2].toLowerCase()];
      return { date: (m[1] || "").toLowerCase() === "next" ? inNextWeek(c.today, wd) : upcoming(c.today, wd) };
    } },
    { kind: "date", re: "next\\s+week", fn: (m, c) => ({ date: nextMonday(c.today) }) },
    { kind: "date", re: "next\\s+month", fn: (m, c) => ({ date: new Date(c.today.getFullYear(), c.today.getMonth() + 1, 1) }) },
    { kind: "date", re: "(?:(this|next)\\s+)?weekend", fn: (m, c) =>
      ({ date: (m[1] || "").toLowerCase() === "next" ? inNextWeek(c.today, 6) : upcoming(c.today, 6) }) },
    { kind: "date", re: `in\\s+(\\d{1,3}|${NW})\\s*(days?|d|weeks?|wks?|w|months?|mths?|hours?|hrs?|h|minutes?|mins?)`, fn: (m, c) => {
      const n = num(m[1]), u = m[2].toLowerCase();
      if (!n) return null;
      if (/^(h|hrs?|hours?|mins?|minutes?)$/.test(u)) {
        const t = new Date(c.now.getTime() + n * (u.startsWith("h") ? 3600e3 : 60e3));
        return { date: day0(t), time: hm(t.getHours(), t.getMinutes()) };
      }
      if (/^(months?|mths?)$/.test(u)) return { date: new Date(c.today.getFullYear(), c.today.getMonth() + n, c.today.getDate()) };
      return { date: addDays(c.today, /^w/.test(u) ? n * 7 : n) };
    } },
    { kind: "date", re: `(\\d{1,2})(?:st|nd|rd|th)?\\s+(?:of\\s+)?(${MO})(?:\\s+(\\d{4}))?`, fn: (m, c) => {
      const d = dayMonth(c.today, MONTHS[m[2].toLowerCase()], +m[1], m[3] ? +m[3] : null);
      return d && { date: d };
    } },
    { kind: "date", re: `(${MO})\\s+(\\d{1,2})(?:st|nd|rd|th)?(?:,?\\s+(\\d{4}))?`, fn: (m, c) => {
      const d = dayMonth(c.today, MONTHS[m[1].toLowerCase()], +m[2], m[3] ? +m[3] : null);
      return d && { date: d };
    } },
    { kind: "date", re: "(\\d{4})-(\\d{1,2})-(\\d{1,2})", fn: m => {
      const y = +m[1], mo = +m[2] - 1, d = +m[3];
      return mo >= 0 && mo < 12 && validDay(y, mo, d) ? { date: new Date(y, mo, d) } : null;
    } },
    // British order: 21/11 is 21 November.
    { kind: "date", re: "(\\d{1,2})/(\\d{1,2})(?:/(\\d{4}|\\d{2}))?", fn: (m, c) => {
      const mo = +m[2] - 1;
      if (mo < 0 || mo > 11) return null;
      const d = dayMonth(c.today, mo, +m[1], m[3] ? +m[3] : null);
      return d && { date: d };
    } },
    { kind: "time", re: "(?:at\\s+|@)?(\\d{1,2})(?:[:.](\\d{2}))?\\s*([ap])\\.?m\\.?", fn: m => {
      let h = +m[1]; const mi = +(m[2] || 0);
      if (h < 1 || h > 12 || mi > 59) return null;
      if (m[3].toLowerCase() === "p" && h < 12) h += 12;
      if (m[3].toLowerCase() === "a" && h === 12) h = 0;
      return { time: hm(h, mi) };
    } },
    { kind: "time", re: "(?:at\\s+|@)?([01]?\\d|2[0-3]):([0-5]\\d)", fn: m => ({ time: hm(+m[1], +m[2]) }) },
    { kind: "time", re: "(?:at|@)\\s*(\\d{1,2})(?![:.]?\\d)", fn: m => {
      const h = +m[1];
      return h <= 23 ? { time: hm(guessHour(h), 0) } : null;
    } },
    { kind: "time", re: "(noon|midday)", fn: () => ({ time: "12:00" }) },
    { kind: "time", re: "(eod|end\\s+of\\s+(?:the\\s+)?day)", fn: () => ({ time: "17:00" }) },
  ].map(r => ({ ...r, rx: new RegExp(L + r.re + R, "giu") }));

  const norm = s => String(s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, " ").trim();
  const words = s => norm(s).split(" ").filter(Boolean);

  // How well typed words name a project: 0 for no match. Prefixes of the name's words, from its start (best) or
  // anywhere in it; the whole name run together ("aiusage"); and, for one word, its letters in order ("ops").
  function scoreProject(typed, name) {
    const t = Array.isArray(typed) ? typed.flatMap(words) : words(typed), n = words(name);
    if (!t.length || !n.length) return 0;
    const tj = t.join(""), nj = n.join("");
    if (tj === nj) return 100;
    for (let i = 0; i + t.length <= n.length; i++) {
      if (t.every((w, k) => n[i + k].startsWith(w))) return (i === 0 ? 60 : 40) + (t.length === n.length ? 10 : 0);
    }
    if (nj.startsWith(tj)) return 30;
    if (t.length === 1 && tj.length >= 2 && tj[0] === nj[0]) {
      let j = 0;
      for (const ch of nj) if (ch === tj[j]) j++;
      if (j === tj.length) return 10;
    }
    return 0;
  }
  // Projects matching typed words, best first: score, then the desk's own projects, then the shorter name.
  function rankProjects(typed, projects) {
    return (projects || []).map(p => ({ p, s: scoreProject(typed, p.name) })).filter(x => x.s > 0)
      .sort((a, b) => b.s - a.s || (b.p.mine ? 1 : 0) - (a.p.mine ? 1 : 0) || a.p.name.length - b.p.name.length || a.p.name.localeCompare(b.p.name))
      .map(x => x.p);
  }

  // The #project token at `at` (the index of "#"): the longest run of words after it that names a project.
  function projectAt(line, at, projects) {
    const rest = line.slice(at + 1), ws = [];
    const rx = /[^\s#+]+/gy;
    let pos = 0;
    for (let i = 0; i < 6; i++) {
      if (i > 0) { const sp = /^[ \t]+/.exec(rest.slice(pos)); if (!sp) break; pos += sp[0].length; }
      rx.lastIndex = pos;
      const m = rx.exec(rest);
      if (!m) break;
      pos += m[0].length;
      ws.push({ w: m[0], end: pos });
    }
    const most = Math.max(1, ...(projects || []).map(p => words(p.name).length));
    for (let k = Math.min(ws.length, most); k >= 1; k--) {
      const hit = rankProjects(ws.slice(0, k).map(x => x.w), projects)[0];
      if (hit) return { start: at, end: at + 1 + ws[k - 1].end, project: hit };
    }
    return { start: at, end: at + 1 + (ws[0] ? ws[0].end : 0), project: null, query: ws[0] ? ws[0].w : "" };
  }

  function parse(text, ctx = {}) {
    const now = ctx.now || new Date(), today = day0(now);
    const c = { now, today };
    const value = String(text || "");
    const nl = value.indexOf("\n");
    const line = nl < 0 ? value : value.slice(0, nl);
    const description = nl < 0 ? "" : value.slice(nl + 1).replace(/^\s*\n/, "").trimEnd();
    const tokens = [];
    const taken = (s, e) => tokens.some(t => s < t.end && e > t.start);
    const out = { title: "", description, due: null, project: null, projectMiss: null, who: null, priority: null, tokens };

    // #project, +person and p1–p4 first, so a project called "Next week" isn't read as a date.
    for (const m of line.matchAll(/(?<![\p{L}\p{N}_\\])#/gu)) {
      if (out.project || taken(m.index, m.index + 1)) continue;
      const pa = projectAt(line, m.index, ctx.projects);
      if (pa.project) { out.project = pa.project; tokens.push({ start: pa.start, end: pa.end, kind: "project" }); }
      else if (pa.query && !out.projectMiss) out.projectMiss = pa.query;
    }
    if (ctx.people && ctx.people.length) {
      for (const m of line.matchAll(/(?<![\p{L}\p{N}_\\])\+([\p{L}]+)(?![\p{L}\p{N}_])/gu)) {
        if (out.who || taken(m.index, m.index + m[0].length)) continue;
        const q = m[1].toLowerCase();
        const hits = q === "me" ? [ctx.defaultWho] : ctx.people.filter(p => p.startsWith(q));
        if (hits.length === 1 && hits[0]) { out.who = hits[0]; tokens.push({ start: m.index, end: m.index + m[0].length, kind: "who" }); }
      }
    }
    for (const m of line.matchAll(/(?<![\p{L}\p{N}_\\])[pP]([1-4])(?![\p{L}\p{N}_])/gu)) {
      if (out.priority || taken(m.index, m.index + m[0].length)) continue;
      out.priority = +m[1]; tokens.push({ start: m.index, end: m.index + m[0].length, kind: "priority" });
    }

    // Dates and times: every candidate, then the earliest and longest that don't overlap; the first date and the
    // first time are used, and later ones stay in the title.
    const cands = [];
    for (const r of RULES) {
      r.rx.lastIndex = 0;
      for (const m of line.matchAll(r.rx)) {
        const v = r.fn(m, c);
        if (v) cands.push({ start: m.index, end: m.index + m[0].length, v });
      }
    }
    cands.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
    const kept = [];
    for (const x of cands) if (!taken(x.start, x.end) && !kept.some(k => x.start < k.end && x.end > k.start)) kept.push(x);
    let date = null, time = null;
    for (const x of kept) {
      const useDate = x.v.date && !date, useTime = x.v.time && !time;
      if (x.v.date && x.v.time ? !(useDate && useTime) : !(useDate || useTime)) continue;
      if (useDate) date = x.v.date;
      if (useTime) time = x.v.time;
      tokens.push({ start: x.start, end: x.end, kind: "due" });
    }
    if (time && !date) {   // a time alone: today, or tomorrow once it has passed
      const [h, mi] = time.split(":").map(Number);
      date = h * 60 + mi > now.getHours() * 60 + now.getMinutes() ? today : addDays(today, 1);
    }
    if (date) out.due = { date: isoDate(date), time };

    tokens.sort((a, b) => a.start - b.start);
    let title = "", pos = 0;
    for (const t of tokens) { title += line.slice(pos, t.start) + " "; pos = t.end; }
    title += line.slice(pos);
    out.title = title.replace(/(^|\s)\\(?=[\p{L}\p{N}#+@])/gu, "$1").replace(/\s+/g, " ")
      .replace(/^[\s,;:–—-]+|[\s,;:–—-]+$/g, "").trim();
    return out;
  }

  function dueLabel(due, now = new Date()) {
    if (!due) return "";
    const [y, m, d] = due.date.split("-").map(Number);
    const day = new Date(y, m - 1, d), today = day0(now);
    const diff = Math.round((day - today) / 86400e3);
    let s;
    if (diff === 0) s = "Today";
    else if (diff === 1) s = "Tomorrow";
    else if (diff === -1) s = "Yesterday";
    else if (diff > 1 && diff < 7) s = DAY_NAMES[day.getDay()];
    else s = `${DAY_NAMES[day.getDay()].slice(0, 3)} ${d} ${MONTH_ABBR[m - 1]}${y !== today.getFullYear() ? " " + y : ""}`;
    return due.time ? `${s} ${due.time}` : s;
  }
  const PRIORITY_LINEAR = { 1: "Urgent", 2: "High", 3: "Medium", 4: "Low" };   // Todoist's p1 is Linear's P0, Urgent
  const priorityLabel = (p, dest) => (dest === "linear" ? PRIORITY_LINEAR[p] : `Priority ${p}`);

  const API = { parse, scoreProject, rankProjects, dueLabel, priorityLabel };
  if (typeof module === "object" && module.exports) { module.exports = API; return; }
  if (typeof document === "undefined") return;

  // ================================================================ page
  const TOKEN = document.querySelector('meta[name="desk-token"]')?.content || "";
  const isMac = navigator.platform.includes("Mac");
  const FIELD = "input, textarea, select, [contenteditable]";
  const LS = { draft: "ftdesk.quickadd.draft", pending: "ftdesk.quickadd.pending", meta: "ftdesk.quickadd.meta", seen: "ftdesk.quickadd.seen" };
  const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } };
  const lsSet = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const uid = () => (crypto.randomUUID ? crypto.randomUUID() : "qa-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12));
  const DEST = { linear: "Linear", todoist: "Todoist" };
  const WHO = { peter: "Peter", sam: "Sam", none: "No one" };

  class ServerDown extends Error {}
  const api = async (path, body) => {
    let r;
    try {
      r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN }, body: JSON.stringify(body || {}) });
    } catch (e) { throw new ServerDown(e.message); }
    if (r.status === 404 || r.status >= 502) throw new ServerDown(r.statusText);   // a server without this feature yet, or down
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  };
  const toast = (msg, action) => {
    const t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg; t.hidden = false;
    if (action) {
      const b = document.createElement("button"); b.type = "button"; b.className = "toast-act"; b.textContent = action.label;
      b.addEventListener("click", () => { t.hidden = true; action.run(); });
      t.append(" ", b);
    }
    clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), action ? 8000 : 3600);
  };

  const ICON = {
    due: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M2.5 6.5h11M5.5 2v3M10.5 2v3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
    project: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 2.5 4.8 13.5M11.2 2.5 10 13.5M2.8 5.8h11M2.2 10.2h11" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
    who: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="5.5" r="2.6" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M3 13.5c.6-2.6 2.6-4 5-4s4.4 1.4 5 4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
    priority: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 14V2.5M3.5 3h8l-1.8 3 1.8 3h-8" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" stroke-linecap="round"/></svg>',
    warn: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M8 4.8v3.6M8 10.8v.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    plus: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2v8M2 6h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  };

  // ---------------------------------------------------------------- state
  let meta = lsGet(LS.meta, null), metaAt = 0;
  let dest = "linear", back = null, boxes = [], lastEnter = null, opener = null;
  let server = { waiting: 0, items: [], held: [], recent: [] };
  const inflight = new Set();

  const ctxFor = d => {
    const m = (meta || {})[d] || {};
    return { now: new Date(), projects: m.projects || [], people: d === "linear" ? (m.people || ["peter", "sam", "none"]) : [], defaultWho: m.default_who || "peter" };
  };
  const loadMeta = async () => {
    if (Date.now() - metaAt < 10 * 60e3 && meta) return;
    try { meta = await api("/api/quickadd/meta", {}); metaAt = Date.now(); lsSet(LS.meta, meta); boxes.forEach(render); }
    catch { /* offline: the cached lists will do */ }
  };

  // ---------------------------------------------------------------- dialog
  function build() {
    back = document.createElement("div");
    back.className = "qa-back";
    back.innerHTML = `<div class="qa" role="dialog" aria-modal="true" aria-labelledby="qa-h">
      <div class="qa-top"><h2 id="qa-h">Quick create</h2>
        <div class="qa-seg" role="radiogroup" aria-label="Create in">
          <button type="button" role="radio" data-dest="linear" data-tip="Linear|⇧⌘T"><i class="qa-dot qa-dot-l" aria-hidden="true"></i>Linear</button>
          <button type="button" role="radio" data-dest="todoist" data-tip="Todoist|⇧⌘T"><i class="qa-dot qa-dot-t" aria-hidden="true"></i>Todoist</button>
        </div></div>
      <div class="qa-boxes"></div>
      <div class="qa-foot">
        <button type="button" class="btn sm qa-more" data-tip="Another task|↵↵">${ICON.plus} Another task</button>
        <span class="qa-sp"></span>
        <button type="button" class="btn" data-a="cancel" data-tip="Close|Esc">Cancel</button>
        <button type="button" class="btn primary" data-a="save"><span class="qa-save-t">Create</span> <kbd>⌘↵</kbd></button>
      </div></div>`;
    back.addEventListener("mousedown", e => { if (e.target === back) close(); });
    back.querySelector(".qa-seg").addEventListener("click", e => { const b = e.target.closest("[data-dest]"); if (b) setDest(b.dataset.dest); });
    back.querySelector(".qa-more").addEventListener("click", () => addBox(boxes[boxes.length - 1], "", true));
    back.querySelector('[data-a="cancel"]').addEventListener("click", () => close());
    back.querySelector('[data-a="save"]').addEventListener("click", () => save());
    document.body.appendChild(back);
  }
  const isOpen = () => !!back && back.isConnected;

  function open(draft) {
    if (isOpen()) { boxes[0]?.ta.focus(); return; }
    opener = document.activeElement;
    build();
    const d = draft || lsGet(LS.draft, null);
    dest = d?.dest === "todoist" ? "todoist" : "linear";
    boxes = [];
    (d?.boxes?.length ? d.boxes : [""]).forEach(t => addBox(null, t, false));
    setDest(dest, true);
    const last = boxes[boxes.length - 1].ta;
    last.focus(); last.setSelectionRange(last.value.length, last.value.length);
    loadMeta();
  }
  function close(keepDraft = true) {
    if (!isOpen()) return;
    if (keepDraft) saveDraft(); else lsSet(LS.draft, null);
    back.remove(); back = null; boxes = []; lastEnter = null;
    if (opener?.isConnected) opener.focus({ preventScroll: true });
  }
  const saveDraft = () => {
    const texts = boxes.map(b => b.ta.value);
    lsSet(LS.draft, texts.some(t => t.trim()) ? { dest, boxes: texts.filter((t, i) => t.trim() || i === 0) } : null);
  };

  function setDest(d, quiet) {
    dest = d;
    back.querySelectorAll(".qa-seg [data-dest]").forEach(b => {
      const on = b.dataset.dest === d;
      b.setAttribute("aria-checked", String(on)); b.tabIndex = on ? 0 : -1;
      b.dataset.tip = `${on ? DEST[d] : "Switch to " + DEST[b.dataset.dest]}|⇧⌘T`;
    });
    back.querySelector(".qa").dataset.dest = d;
    boxes.forEach(render);
    if (!quiet) saveDraft();
  }

  let boxSeq = 0;
  function addBox(after, text, focus) {
    const n = ++boxSeq;
    const el = document.createElement("div");
    el.className = "qa-box";
    el.innerHTML = `<div class="qa-field"><div class="qa-hl" aria-hidden="true"></div>
      <textarea rows="1" spellcheck="true" autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false"
        aria-controls="qa-sug-${n}" aria-describedby="qa-chips-${n}"></textarea>
      <ul class="qa-sug" id="qa-sug-${n}" role="listbox" aria-label="Suggestions" hidden></ul></div>
      <div class="qa-chips" id="qa-chips-${n}"></div>`;
    const box = { el, ta: el.querySelector("textarea"), hl: el.querySelector(".qa-hl"), chips: el.querySelector(".qa-chips"), sug: el.querySelector(".qa-sug"), sugItems: [], sugAt: 0, parsed: null };
    box.ta.value = text || "";
    const list = back.querySelector(".qa-boxes");
    const i = after ? boxes.indexOf(after) + 1 : boxes.length;
    list.insertBefore(el, boxes[i]?.el || null);
    boxes.splice(i, 0, box);
    box.ta.addEventListener("input", () => { box.ta.removeAttribute("aria-invalid"); render(box); suggest(box); saveDraft(); });
    box.ta.addEventListener("click", () => suggest(box));
    box.ta.addEventListener("keyup", e => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) suggest(box); });
    box.ta.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== box.ta) hideSug(box); }, 120));
    box.sug.addEventListener("mousedown", e => { const li = e.target.closest("li"); if (li) { e.preventDefault(); accept(box, +li.dataset.i); } });
    labelBoxes();
    render(box);
    if (focus) { box.ta.focus(); }
    return box;
  }
  function removeBox(box) {
    const i = boxes.indexOf(box);
    box.el.remove(); boxes.splice(i, 1);
    labelBoxes(); updateSave();
    const to = boxes[Math.max(0, i - 1)].ta;
    to.focus(); to.setSelectionRange(to.value.length, to.value.length);
    saveDraft();
  }
  const labelBoxes = () => boxes.forEach((b, i) => {
    b.ta.setAttribute("aria-label", boxes.length > 1 ? `Task ${i + 1}` : "Task");
    b.ta.placeholder = i === 0 ? (dest === "linear" ? "Task  tomorrow 3pm  #project  +sam  p2" : "Task  fri 9am  #project  p2") : "Another task";
  });

  const autosize = ta => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; };
  function render(box) {
    const p = box.parsed = parse(box.ta.value, ctxFor(dest));
    labelBoxes();
    // Highlight the tokens in a layer behind the (transparent) textarea, wrapped exactly as the text is.
    const v = box.ta.value;
    let html = "", pos = 0;
    for (const t of p.tokens) { html += esc(v.slice(pos, t.start)) + `<mark class="qa-tk qa-k-${t.kind}">${esc(v.slice(t.start, t.end))}</mark>`; pos = t.end; }
    box.hl.innerHTML = html + esc(v.slice(pos)) + "​";
    autosize(box.ta);
    const m = (meta || {})[dest] || {};
    const chip = (kind, label, dflt) => `<span class="qa-chip qa-k-${kind}${dflt ? " qa-dflt" : ""}">${ICON[kind]}${esc(label)}</span>`;
    let chips = "";
    if (v.trim()) {
      if (p.due) chips += chip("due", dueLabel(p.due));
      chips += p.project ? chip("project", p.project.name) : chip("project", m.default_project || (dest === "linear" ? "Operations" : "Inbox"), true);
      if (dest === "linear") chips += p.who ? chip("who", WHO[p.who] || p.who) : chip("who", WHO[m.default_who] || "Peter", true);
      if (p.priority) chips += `<span class="qa-chip qa-k-priority qa-p${p.priority}">${ICON.priority}${esc(priorityLabel(p.priority, dest))}</span>`;
      if (p.projectMiss) chips += `<span class="qa-chip qa-warn">${ICON.warn}No project matches #${esc(p.projectMiss)}</span>`;
      if (!p.title) chips += `<span class="qa-chip qa-warn">${ICON.warn}Needs a title</span>`;
    }
    box.chips.innerHTML = chips;
    updateSave();
  }
  const filled = () => boxes.filter(b => b.ta.value.trim());
  function updateSave() {
    if (!isOpen()) return;
    const n = filled().length;
    back.querySelector(".qa-save-t").textContent = n > 1 ? `Create ${n} tasks` : "Create";
  }

  // ---------------------------------------------------------------- # and + suggestions
  function trigger(box) {
    const ta = box.ta, v = ta.value, c = ta.selectionStart;
    if (c !== ta.selectionEnd) return null;
    const nl = v.indexOf("\n");
    if (nl >= 0 && c > nl) return null;
    const before = v.slice(0, c);
    const m = /(?<![\p{L}\p{N}_\\])([#+])([^#+\n]*)$/u.exec(before);
    if (!m) return null;
    const ch = m[1], q = m[2], at = c - q.length - 1;
    if (ch === "+") {
      if (dest !== "linear" || /\s/.test(q)) return null;
      const people = ctxFor(dest).people.filter(p => p.startsWith(q.toLowerCase()));
      return people.length ? { at, end: c, ch, items: people.map(p => ({ label: WHO[p] || p, insert: p })) } : null;
    }
    if (/\s{2,}|^\s/.test(q) || q.split(/\s+/).length > 6) return null;
    const projects = ctxFor(dest).projects;
    const hits = q.trim() ? rankProjects(q.trim().split(/\s+/), projects)
      : [...projects].sort((a, b) => (b.mine ? 1 : 0) - (a.mine ? 1 : 0) || a.name.localeCompare(b.name));
    // Once the words after # stop naming a project, they are the title: stop suggesting.
    if (!hits.length) return null;
    return { at, end: c, ch, items: hits.slice(0, 8).map(p => ({ label: p.name, insert: p.name, mine: p.mine })) };
  }
  function suggest(box) {
    if (box.sugOff === box.ta.value) return;   // Esc closed it: wait for the next change
    const t = trigger(box);
    if (!t) return hideSug(box);
    box.sugT = t; box.sugItems = t.items; box.sugAt = Math.min(box.sugAt, t.items.length - 1);
    if (box.sugAt < 0) box.sugAt = 0;
    box.sug.innerHTML = t.items.map((it, i) => `<li role="option" id="${box.sug.id}-${i}" data-i="${i}" aria-selected="${i === box.sugAt}">${t.ch === "#" ? ICON.project : ICON.who}<span>${esc(it.label)}</span></li>`).join("");
    box.sug.hidden = false;
    box.ta.setAttribute("aria-expanded", "true");
    box.ta.setAttribute("aria-activedescendant", `${box.sug.id}-${box.sugAt}`);
  }
  function hideSug(box) {
    box.sug.hidden = true; box.sugItems = []; box.sugAt = 0;
    box.ta.setAttribute("aria-expanded", "false"); box.ta.removeAttribute("aria-activedescendant");
  }
  function moveSug(box, d) {
    box.sugAt = (box.sugAt + d + box.sugItems.length) % box.sugItems.length;
    box.sug.querySelectorAll("li").forEach((li, i) => li.setAttribute("aria-selected", String(i === box.sugAt)));
    box.ta.setAttribute("aria-activedescendant", `${box.sug.id}-${box.sugAt}`);
    box.sug.querySelector(`[data-i="${box.sugAt}"]`)?.scrollIntoView({ block: "nearest" });
  }
  function accept(box, i) {
    const t = box.sugT, it = t && t.items[i];
    if (!it) return;
    const v = box.ta.value, after = v.slice(t.end);
    const ins = t.ch + it.insert + (after.startsWith(" ") ? "" : " ");
    box.ta.value = v.slice(0, t.at) + ins + after;
    const c = t.at + ins.length + (after.startsWith(" ") ? 1 : 0);
    box.ta.setSelectionRange(c, c);
    hideSug(box); render(box); saveDraft();
  }

  // ---------------------------------------------------------------- keys
  const isToggleKey = e => e.code === "KeyT" && e.shiftKey && (isMac ? e.metaKey : e.ctrlKey) && !e.altKey;
  const isSaveKey = e => e.key === "Enter" && (isMac ? e.metaKey : e.ctrlKey);
  const boxOf = el => boxes.find(b => b.ta === el);

  function onKey(e) {
    if (e.isComposing || e.keyCode === 229) return;
    if (!isOpen()) {
      if ((e.key === "q" || e.key === "Q") && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey && !e.target.closest?.(FIELD) && !e.repeat) {
        e.preventDefault(); e.stopPropagation(); open();
      }
      return;
    }
    // Open: every key belongs to the dialog, so no desk shortcut fires behind it.
    e.stopPropagation();
    if (!back.contains(e.target)) { e.preventDefault(); boxes[0]?.ta.focus(); return; }
    const box = boxOf(e.target);
    const sugOpen = box && !box.sug.hidden && box.sugItems.length;
    if (isToggleKey(e)) { e.preventDefault(); setDest(dest === "linear" ? "todoist" : "linear"); return; }
    if (isSaveKey(e)) { e.preventDefault(); save(); return; }
    if (e.key === "Escape") {
      e.preventDefault();
      if (sugOpen) { box.sugOff = box.ta.value; hideSug(box); } else close();
      return;
    }
    if (e.key === "Tab" && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (sugOpen && !e.shiftKey) { e.preventDefault(); accept(box, box.sugAt); return; }
      const f = [...back.querySelectorAll("textarea, button:not([disabled])")].filter(x => x.tabIndex >= 0 && x.offsetParent !== null);
      const i = f.indexOf(document.activeElement);
      if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
      return;
    }
    if (!box) {   // on a button: arrows move through the segmented control
      if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && e.target.closest(".qa-seg")) {
        e.preventDefault(); setDest(dest === "linear" ? "todoist" : "linear"); back.querySelector(`.qa-seg [data-dest="${dest}"]`).focus();
      }
      return;
    }
    const ta = box.ta, v = ta.value, s = ta.selectionStart, end = ta.selectionEnd;
    if (sugOpen && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); moveSug(box, e.key === "ArrowDown" ? 1 : -1); return; }
    if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (sugOpen) { e.preventDefault(); accept(box, box.sugAt); return; }
      if (!v.trim()) { e.preventDefault(); return; }
      const now = Date.now();
      // Enter on an empty last line, or a second Enter straight after the first: drop the new line, open a new box.
      if (s === end && s === v.length && v.endsWith("\n")) {
        e.preventDefault(); ta.value = v.replace(/\n+$/, ""); render(box); addBox(box, "", true); saveDraft(); lastEnter = null; return;
      }
      if (lastEnter && lastEnter.ta === ta && now - lastEnter.t < 450 && s === end && s === lastEnter.pos && v[s - 1] === "\n") {
        e.preventDefault(); ta.value = v.slice(0, s - 1) + v.slice(s); render(box); addBox(box, "", true); saveDraft(); lastEnter = null; return;
      }
      lastEnter = { ta, t: now, pos: s + 1 };
      return;
    }
    if (e.key === "Backspace" && !v && boxes.length > 1) { e.preventDefault(); removeBox(box); return; }
    if (e.key === "ArrowUp" && s === 0 && end === 0 && !e.shiftKey) {
      const prev = boxes[boxes.indexOf(box) - 1];
      if (prev) { e.preventDefault(); prev.ta.focus(); prev.ta.setSelectionRange(prev.ta.value.length, prev.ta.value.length); }
      return;
    }
    if (e.key === "ArrowDown" && s === v.length && end === v.length && !e.shiftKey) {
      const next = boxes[boxes.indexOf(box) + 1];
      if (next) { e.preventDefault(); next.ta.focus(); next.ta.setSelectionRange(next.ta.value.length, next.ta.value.length); }
    }
  }
  // On window in the capture phase, after desk.js's backstop (which returns stray letters to a field that lost
  // focus), and before every other shortcut handler.
  addEventListener("keydown", onKey, true);

  // ---------------------------------------------------------------- saving and sending
  function itemFrom(box) {
    const p = parse(box.ta.value, ctxFor(dest));
    const m = (meta || {})[dest] || {};
    let project = p.project;
    if (!project && dest === "linear") project = (m.projects || []).find(x => x.name === m.default_project) || { id: null, name: m.default_project || "" };
    return { id: uid(), dest, title: p.title, description: p.description, project_id: project?.id || null, project_name: project?.name || null,
      who: dest === "linear" ? p.who || null : null, due: p.due?.date || null, time: p.due?.time || null, priority: p.priority || null, raw: box.ta.value };
  }
  function save() {
    const fs = filled();
    if (!fs.length) { close(false); return; }
    const bad = fs.find(b => !parse(b.ta.value, ctxFor(dest)).title);
    if (bad) { bad.ta.setAttribute("aria-invalid", "true"); bad.ta.focus(); return; }
    const items = fs.map(itemFrom);
    lsSet(LS.pending, [...lsGet(LS.pending, []), ...items]);   // safe before anything is sent
    close(false);
    send(items);
  }
  const dropPending = ids => lsSet(LS.pending, lsGet(LS.pending, []).filter(x => !ids.includes(x.id)));
  const refOf = (r, it) => r.ref && it.dest === "linear" ? r.ref : `“${it.title}”`;

  async function send(items, quiet) {
    items = items.filter(x => !inflight.has(x.id));
    if (!items.length) return;
    items.forEach(x => inflight.add(x.id));
    let res;
    try { res = await api("/api/quickadd/add", { items }); }
    catch (e) {
      if (e instanceof ServerDown) { if (!quiet) toast(`${items.length > 1 ? items.length + " tasks" : "Task"} saved here; will sync when the desk server is back`); paintSync(); return; }
      dropPending(items.map(x => x.id));
      toast(`Couldn't add ${items.length > 1 ? "the tasks" : `“${items[0].title}”`}: ${e.message}`, { label: "Edit", run: () => open({ dest: items[0].dest, boxes: items.map(x => x.raw) }) });
      return;
    } finally { items.forEach(x => inflight.delete(x.id)); }
    dropPending(items.map(x => x.id));
    const by = s => items.filter(it => res.results[it.id]?.status === s);
    const created = by("created"), queued = by("queued"), similar = by("similar"), refused = by("refused");
    markSeen(created.map(x => x.id));
    if (similar.length) {
      const r = res.results[similar[0].id].similar || {};
      toast(similar.length > 1 ? `${similar.length} not added: similar tasks are open` : `Not added: ${r.id} is open with a similar title`,
        { label: "Add anyway", run: () => send(similar.map(x => ({ ...x, force: true }))) });
    } else if (refused.length) {
      const it = refused[0];
      toast(`Couldn't add “${it.title}”: ${res.results[it.id].error}`, { label: "Edit", run: () => open({ dest: it.dest, boxes: refused.map(x => x.raw) }) });
    } else if (created.length || queued.length) {
      const parts = [];
      if (created.length === 1) parts.push(`${refOf(res.results[created[0].id], created[0])} added${created[0].dest === "todoist" ? " to Todoist" : ""}`);
      else if (created.length) parts.push(`${created.length} tasks added`);
      if (queued.length) parts.push(`${queued.length} waiting to sync`);
      if (!quiet || created.length) toast(parts.join("; "));
    }
    paintSync();
  }

  // ---------------------------------------------------------------- sync indicator
  const pill = document.createElement("button");
  pill.type = "button"; pill.className = "qa-sync"; pill.hidden = true;
  pill.setAttribute("aria-haspopup", "true"); pill.setAttribute("aria-expanded", "false");
  const pop = document.createElement("div");
  pop.className = "qa-sync-pop"; pop.hidden = true; pop.setAttribute("role", "dialog"); pop.setAttribute("aria-label", "Tasks not yet in Linear or Todoist");
  document.body.append(pill, pop);

  function paintSync() {
    const local = lsGet(LS.pending, []).filter(x => !server.items.some(s => s.id === x.id));
    const waiting = server.waiting + local.length, held = server.held.length;
    pill.hidden = !waiting && !held;
    if (pill.hidden) { pop.hidden = true; pill.setAttribute("aria-expanded", "false"); return; }
    const parts = [];
    if (waiting) parts.push(`${waiting} waiting to sync`);
    if (held) parts.push(`${held} not added`);
    pill.innerHTML = `<i aria-hidden="true"></i>${parts.join(" · ")}`;
    pill.classList.toggle("qa-held", !!held);
    const err = server.items.find(x => x.error)?.error || (local.length ? "The desk server isn't reachable" : "");
    if (err) pill.dataset.tip = err; else delete pill.dataset.tip;
    if (!pop.hidden) paintPop(local);
  }
  function paintPop(local = lsGet(LS.pending, []).filter(x => !server.items.some(s => s.id === x.id))) {
    const row = (x, extra = "") => `<li><span class="qa-sp-t">${esc(x.title)}</span><span class="qa-sp-d">${DEST[x.dest]}</span>${extra}</li>`;
    const waiting = [...server.items, ...local];
    const held = server.held.map(x => row(x, `<span class="qa-sp-why">${x.similar ? `Similar to ${esc(x.similar.id)}: ${esc(x.similar.title)}` : esc(x.error || "")}</span>
      <span class="qa-sp-acts">${x.similar ? `<button type="button" class="btn sm" data-h="force" data-id="${esc(x.id)}">Add anyway</button>` : ""}
      <button type="button" class="btn sm" data-h="edit" data-id="${esc(x.id)}">Edit</button>
      <button type="button" class="btn sm" data-h="discard" data-id="${esc(x.id)}">Discard</button></span>`)).join("");
    pop.innerHTML = (waiting.length ? `<h3>Waiting to sync</h3><ul>${waiting.map(x => row(x)).join("")}</ul>
        <div class="qa-sp-foot"><button type="button" class="btn sm" data-h="retry">Retry now</button></div>` : "")
      + (held ? `<h3>Not added</h3><ul>${held}</ul>` : "");
  }
  pill.addEventListener("click", () => {
    pop.hidden = !pop.hidden; pill.setAttribute("aria-expanded", String(!pop.hidden));
    if (!pop.hidden) { paintPop(); pop.querySelector("button")?.focus(); }
  });
  document.addEventListener("mousedown", e => { if (!pop.hidden && !pop.contains(e.target) && !pill.contains(e.target)) { pop.hidden = true; pill.setAttribute("aria-expanded", "false"); } });
  pop.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); pop.hidden = true; pill.setAttribute("aria-expanded", "false"); pill.focus(); } });
  pop.addEventListener("click", async e => {
    const b = e.target.closest("[data-h]");
    if (!b) return;
    const h = b.dataset.h, id = b.dataset.id;
    b.disabled = true;
    try {
      if (h === "retry") {
        const local = lsGet(LS.pending, []);
        if (local.length) await send(local, true);
        const r = await api("/api/quickadd/retry", {});
        toast(r.waiting ? `${r.waiting} still waiting${r.error ? ": " + r.error : ""}` : r.held ? `${r.held} not added: see the list` : "All synced");
      } else if (h === "edit") {
        const x = server.held.find(y => y.id === id);
        await api("/api/quickadd/held", { id, action: "discard" });
        if (x) open({ dest: x.dest, boxes: [x.raw || x.title] });
      } else {
        const r = await api("/api/quickadd/held", { id, action: h });
        if (h === "force") toast(r.result?.status === "created" ? `${r.result.ref || "Task"} added` : r.result?.status === "queued" ? "Waiting to sync" : `Couldn't add it: ${r.result?.error || ""}`);
      }
    } catch (err) { toast(err instanceof ServerDown ? "The desk server isn't reachable" : `Couldn't do that: ${err.message}`); }
    finally { b.disabled = false; }
  });

  // Background sends that went through since this page last looked get a toast.
  const markSeen = ids => { const s = lsGet(LS.seen, { at: Date.now() / 1000, ids: [] }); s.ids = [...new Set([...s.ids, ...ids])].slice(-50); lsSet(LS.seen, s); };
  function announce(recent) {
    const s = lsGet(LS.seen, null);
    if (!s) { lsSet(LS.seen, { at: Date.now() / 1000, ids: recent.map(r => r.id) }); return; }
    const fresh = recent.filter(r => r.bg && r.at > s.at && !s.ids.includes(r.id));
    if (fresh.length) toast(fresh.length === 1 ? `Synced: ${fresh[0].dest === "linear" && fresh[0].ref ? fresh[0].ref + " " : ""}“${fresh[0].title}”` : `Synced ${fresh.length} tasks`);
    lsSet(LS.seen, { at: Math.max(s.at, ...recent.map(r => r.at)), ids: [...new Set([...s.ids, ...recent.map(r => r.id)])].slice(-50) });
  }

  window.addEventListener("desk:sync", e => {
    const q = e.detail?.ext?.quickadd;
    if (q && !q.error) { server = { waiting: q.waiting || 0, items: q.items || [], held: q.held || [], recent: q.recent || [] }; announce(server.recent); }
    const local = lsGet(LS.pending, []);
    if (local.length) send(local, true);   // the server answered the sync, so it's back
    paintSync();
  });
  setInterval(() => { const local = lsGet(LS.pending, []); if (local.length) send(local, true); }, 20000);
  paintSync();
})();
