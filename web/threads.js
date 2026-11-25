// Threads: read an email or Slack item's whole thread in a side panel, and reply to it. A calendar item shows its
// event's details there instead, with Accept, Maybe and Decline (also in the row's hover actions).
// Data comes from desk_threads.py through the server. A reply goes only when Peter presses Send (or ⌘↵) on a
// composer that shows exactly where it goes and what it says: that press is his approval of the text.
(() => {
  const TOKEN = document.querySelector('meta[name="desk-token"]')?.content;
  if (!TOKEN) return;
  const isMac = navigator.platform.includes("Mac");
  const SEND_KEY = isMac ? "⌘↵" : "Ctrl ↵";
  const api = async (path, body) => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN }, body: JSON.stringify(body || {}) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  };
  const E = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
  const ICON = {
    thread: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 2.5h8v5H6L3.5 9.5v-2H2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M4 4.5h4M4 6h2.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/></svg>',
    archive: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4.5h11v2h-11zM3.5 6.5v6h9v-6M6.5 9h3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
    open: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 3h4v4M13 3 7.5 8.5M11.5 9.5V13H3V4.5h3.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    yes: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    no: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4.5 4.5 7 7m0-7-7 7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    clip: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10.5 5 6 9.5a1.4 1.4 0 0 0 2 2l5-5a2.8 2.8 0 0 0-4-4l-5 5a4.2 4.2 0 0 0 6 6l4-4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
  };

  // ---------- which rows have a thread to show ----------
  const SLACK_KEY = /^slack(?:-todo)?:[a-z0-9_-]{1,40}:[CDG][A-Z0-9]{6,15}:\d{10}\.?\d{6}$/;
  const GMAIL_HREF = /^https:\/\/mail\.google\.com\/mail\/u\/\d+\/#.*\/[0-9a-f]{12,24}\/?$/;
  const SLACK_HREF = /^https:\/\/[a-z0-9-]+\.slack\.com\/(?:archives|messages)\/[CDG][A-Z0-9]{6,15}\/p\d{16}/;
  const hrefOf = row => (row.querySelector("a.head") || row.querySelector("a.tag"))?.href || "";
  const kindOf = row => {
    if (!row || !row.matches("#view-desk .row")) return null;
    const k = row.dataset.key || "", h = hrefOf(row);
    if (/^(gmail:)?[0-9a-f]{16}$/.test(k) || GMAIL_HREF.test(h)) return "email";
    if (SLACK_KEY.test(k) || SLACK_HREF.test(h)) return "slack";
    if (/^cal(-invite)?:[A-Za-z0-9_]{5,200}(:|$)/.test(k) || /^https:\/\/(www|calendar)\.google\.com\/calendar\/event\?eid=/.test(h)) return "event";
    return null;
  };
  const visible = el => el.offsetParent !== null && !el.closest("[hidden]");
  const threadRows = () => [...document.querySelectorAll("#view-desk .row")].filter(r => visible(r) && kindOf(r));
  const rowFor = key => document.querySelector(`#view-desk .row[data-key="${CSS.escape(key)}"]`);

  // A "Thread" button in each such row's hover actions, or Accept and Decline for a calendar event
  // (desk.js rebuilds the actions after every region swap).
  const addButtons = () => {
    document.querySelectorAll("#view-desk .row > .acts").forEach(acts => {
      const kind = kindOf(acts.parentElement);
      if (!kind || acts.querySelector(".th, .rsvp")) return;
      if (kind === "event") {
        acts.insertAdjacentHTML("afterbegin", `<button type="button" class="rsvp yes" data-rsvp="accepted">${ICON.yes} Accept</button><button type="button" class="rsvp" data-rsvp="declined">${ICON.no} Decline</button>`);
        return;
      }
      const b = document.createElement("button");
      b.type = "button"; b.className = "th"; b.dataset.tip = "Read and reply here|↵";
      b.innerHTML = `${ICON.thread} Thread`;
      acts.prepend(b);
    });
  };
  addButtons();
  addEventListener("desk:sync", addButtons);
  const help = document.querySelector(".kb-help");
  if (help && !help.querySelector(".th-help")) {
    help.insertAdjacentHTML("beforeend", `<h2 class="th-help">Email and Slack threads</h2><dl>
      <dt><kbd>↵</kbd></dt><dd>Open the selected email or Slack item's thread, or calendar event</dd>
      <dt><kbd>J</kbd> <kbd>K</kbd></dt><dd>Next and previous thread</dd>
      <dt><kbd>R</kbd></dt><dd>Write a reply</dd>
      <dt><kbd>${SEND_KEY}</kbd></dt><dd>Send the reply</dd>
      <dt><kbd>E</kbd></dt><dd>Archive the item</dd>
      <dt><kbd>Esc</kbd></dt><dd>Leave the reply box, then close</dd></dl>`);
  }

  // ---------- the panel ----------
  const panel = document.createElement("aside");
  panel.className = "th-panel"; panel.id = "th-panel"; panel.hidden = true;
  panel.setAttribute("aria-label", "Thread");
  panel.innerHTML = `<div class="th-top"><div class="th-head"><span class="th-src"></span><span class="th-count"></span></div>
      <span class="th-top-b"><a class="btn sm th-open" target="_blank" rel="noopener">${ICON.open}<span></span></a>
      <button class="th-x" type="button" data-th="archive" data-tip="Archive|E" aria-label="Archive">${ICON.archive}</button>
      <button class="th-x" type="button" data-th="close" data-tip="Close|Esc" aria-label="Close">×</button></span></div>
    <div class="th-body"><h2 class="th-title"></h2><div class="th-msgs" aria-live="polite"></div></div>
    <form class="th-comp" autocomplete="off"></form>`;
  document.body.appendChild(panel);
  const $ = s => panel.querySelector(s);
  const body = $(".th-body"), msgsEl = $(".th-msgs"), form = $(".th-comp");

  let cur = null;        // {key, href, kind, data}
  let seq = 0, sending = false, filledFor = null, answering = false;
  const isOpen = () => !panel.hidden;

  // What Peter has typed, per item, kept across reloads of the page.
  const drafts = new Map();
  const loadDraft = key => {
    if (drafts.has(key)) return drafts.get(key);
    try { const v = JSON.parse(sessionStorage.getItem("th-draft:" + key) || "null"); if (v) drafts.set(key, v); return v; } catch { return null; }
  };
  const saveDraft = () => {
    if (!cur || filledFor !== cur.key) return;
    const d = Object.fromEntries([...form.querySelectorAll("input[name], textarea[name]")].map(f => [f.name, f.value]));
    d.mode = form.dataset.mode;
    drafts.set(cur.key, d);
    try { d.body ? sessionStorage.setItem("th-draft:" + cur.key, JSON.stringify(d)) : sessionStorage.removeItem("th-draft:" + cur.key); } catch { /* no storage */ }
  };
  const dropDraft = key => { drafts.delete(key); try { sessionStorage.removeItem("th-draft:" + key); } catch { /* no storage */ } };

  // ---------- rendering ----------
  const PALETTE = ["#325e76", "#76419c", "#266433", "#822d2e", "#8a5a12", "#5e6ad2", "#a92817", "#3d6b6b"];
  const initials = n => (n || "?").replace(/[^\p{L}\p{N}\s]/gu, "").split(/\s+/).filter(Boolean).map(w => w[0]).slice(0, 2).join("").toUpperCase() || "?";
  const colour = n => PALETTE[[...(n || "")].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % PALETTE.length];
  const av = (name, me) => `<span class="th-av" style="--c:${me ? "var(--ink-muted)" : colour(name)}" aria-hidden="true">${E(initials(name))}</span>`;
  const when = t => {
    const d = new Date(t * 1000), now = new Date();
    const hm = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return hm;
    if (now - d < 6 * 864e5) return d.toLocaleDateString("en-GB", { weekday: "short" }) + " " + hm;
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(d.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) }) + ", " + hm;
  };
  const full = t => new Date(t * 1000).toLocaleString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" });
  const nameOf = a => (a.match(/^(.*?)\s*<[^>]+>$/)?.[1] || a).replace(/^"|"$/g, "");
  const kb = n => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
  const safe = u => /^(https?:|mailto:)/i.test(u || "") ? u : "";
  const anchor = (href, text) => {
    const a = document.createElement("a");
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = text;
    return a;
  };
  // Plain text with its web addresses made into links; everything goes in as text, never as HTML.
  const URL_RE = /\bhttps?:\/\/[^\s<>()"]+[^\s<>()".,;:!?'’]/g;
  const fillText = (el, text) => {
    let pos = 0;
    for (const m of text.matchAll(URL_RE)) {
      if (m.index > pos) el.append(text.slice(pos, m.index));
      el.append(anchor(m[0], m[0]));
      pos = m.index + m[0].length;
    }
    if (pos < text.length) el.append(text.slice(pos));
    return el;
  };
  const fillSegs = (el, segs) => {
    for (const s of segs || []) {
      if (s.m) { const b = document.createElement("span"); b.className = "th-men"; b.textContent = s.m; el.append(b); }
      else if (s.u && safe(s.u)) el.append(anchor(s.u, s.t || s.u));
      else fillText(el, s.t || "");
    }
    return el;
  };
  const div = (cls, html = "") => { const d = document.createElement("div"); d.className = cls; d.innerHTML = html; return d; };

  function emailMsg(m, collapsed) {
    const el = document.createElement("article");
    el.className = "th-msg" + (m.me ? " me" : "") + (collapsed ? " collapsed" : "");
    const rcpt = [m.to.length ? "to " + m.to.map(nameOf).join(", ") : "", m.cc.length ? "cc " + m.cc.map(nameOf).join(", ") : ""].filter(Boolean).join(" · ");
    el.innerHTML = `<header class="th-mh">${av(m.author, m.me)}<span class="th-who"><b>${E(m.me ? "You" : m.author)}</b>${m.addr && !m.me ? ` <span class="th-addr">${E(m.addr)}</span>` : ""}</span><time title="${E(full(m.ts))}">${E(when(m.ts))}</time>
      <span class="th-rcpt" title="${E([m.to.length ? "To: " + m.to.join(", ") : "", m.cc.length ? "Cc: " + m.cc.join(", ") : ""].filter(Boolean).join("\n"))}">${E(rcpt)}</span></header>`;
    const snip = div("th-snip"); snip.textContent = (m.body || m.quoted || "").replace(/\s+/g, " ").slice(0, 160);
    el.append(snip);
    el.append(fillText(div("th-text"), m.body || (m.files.length ? "" : "(no text)")));
    if (m.quoted) {
      const more = document.createElement("button");
      more.type = "button"; more.className = "th-more"; more.textContent = "Show quoted text"; more.setAttribute("aria-expanded", "false");
      const q = fillText(div("th-quoted"), m.quoted); q.hidden = true;
      more.addEventListener("click", () => { q.hidden = !q.hidden; more.setAttribute("aria-expanded", String(!q.hidden)); more.textContent = q.hidden ? "Show quoted text" : "Hide quoted text"; });
      el.append(more, q);
    }
    if (m.files.length) {
      const ul = document.createElement("ul"); ul.className = "th-files";
      m.files.forEach(f => { const li = document.createElement("li"); li.innerHTML = ICON.clip; li.append(`${f.name} `); const s = document.createElement("span"); s.textContent = kb(f.size); li.append(s); ul.append(li); });
      el.append(ul);
    }
    return el;
  }

  function slackMsg(m, prev, focus) {
    const el = document.createElement("article");
    const grouped = prev && prev.author === m.author && m.ts - prev.ts < 300 && !m.reply_count && !prev.reply_count;
    el.className = "th-sm" + (m.me ? " me" : "") + (grouped ? " grouped" : "") + (focus ? " focus" : "");
    el.innerHTML = grouped ? "" : `<header class="th-mh">${av(m.author, m.me)}<span class="th-who"><b>${E(m.me ? "You" : m.author)}</b></span><time title="${E(full(m.ts))}">${E(when(m.ts))}</time></header>`;
    const t = fillSegs(div("th-text"), m.segs);
    if (m.edited) t.insertAdjacentHTML("beforeend", ' <span class="th-ed">(edited)</span>');
    el.append(t);
    m.atts.forEach(a => {
      const box = div("th-att");
      if (a.title) { const h = div("th-att-t"); a.link && safe(a.link) ? h.append(anchor(a.link, a.title)) : (h.textContent = a.title); box.append(h); }
      if (a.segs?.length) box.append(fillSegs(div(""), a.segs));
      el.append(box);
    });
    if (m.files.length) {
      const ul = document.createElement("ul"); ul.className = "th-files";
      m.files.forEach(f => { const li = document.createElement("li"); li.innerHTML = ICON.clip; li.append(f.link && safe(f.link) ? anchor(f.link, f.name) : f.name); ul.append(li); });
      el.append(ul);
    }
    const foot = [];
    if (m.reactions.length) foot.push(`<span class="th-reacts">${m.reactions.map(r => `<span title=":${E(r.name)}:">${E(r.e)}${r.n > 1 ? ` <b>${r.n}</b>` : ""}</span>`).join("")}</span>`);
    if (m.reply_count) foot.push(`<span class="th-replies">${m.reply_count} ${m.reply_count === 1 ? "reply" : "replies"}</span>`);
    if (foot.length) el.insertAdjacentHTML("beforeend", `<div class="th-foot">${foot.join("")}</div>`);
    return el;
  }

  function renderHead() {
    const k = cur.kind, slack = k === "slack";
    panel.classList.toggle("is-slack", slack);
    panel.classList.toggle("is-event", k === "event");
    $(".th-src").textContent = { slack: "Slack", email: "Email", event: "Calendar" }[k];
    $(".th-src").className = "th-src s-" + (k === "event" ? "cal" : k);
    const open = $(".th-open");
    open.href = cur.data?.link || cur.href || "#";
    open.querySelector("span").textContent = { slack: "Open in Slack", email: "Open in Gmail", event: "Open in Calendar" }[k];
    open.hidden = !(cur.data?.link || cur.href);
  }

  // ---------- calendar events ----------
  const day = d => d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  const hm = d => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  function evWhen(d) {
    if (d.all_day) {
      const s = new Date(d.start + "T00:00"), e = new Date(new Date(d.end + "T00:00") - 864e5);
      return s.toDateString() === e.toDateString() ? `${day(s)}, all day` : `${day(s)} – ${day(e)}`;
    }
    const s = new Date(d.start), e = new Date(d.end);
    return s.toDateString() === e.toDateString() ? `${day(s)}, ${hm(s)}–${hm(e)}` : `${day(s)} ${hm(s)} – ${day(e)} ${hm(e)}`;
  }
  const RESP = { accepted: "yes", tentative: "maybe", declined: "no", needsAction: "awaiting" };
  const span = (cls, text) => { const x = document.createElement("span"); x.className = cls; x.textContent = text; return x; };
  function renderEvent() {
    const d = cur.data;
    $(".th-count").textContent = "";
    msgsEl.replaceChildren();
    const when = div("ev-when"); when.textContent = evWhen(d);
    msgsEl.append(when);
    if (d.cancelled) msgsEl.append(span("ev-note ev-warn", "Cancelled"));
    else if (d.series || d.instance) msgsEl.append(span("ev-note", d.series ? "Repeats: your answer covers every event in the series" : "Repeats: your answer covers this one only"));
    const facts = document.createElement("dl"); facts.className = "ev-facts";
    const fact = (label, node) => { const dt = document.createElement("dt"); dt.textContent = label; const dd = document.createElement("dd"); dd.append(node); facts.append(dt, dd); };
    if (d.meet && safe(d.meet)) fact("Join", anchor(d.meet, d.meet.replace(/^https:\/\//, "")));
    if (d.location) fact("Where", fillText(document.createElement("span"), d.location));
    const o = d.organiser;
    if (o.email || o.name) fact("Organiser", document.createTextNode(o.me ? "You" : o.name || o.email));
    if (facts.children.length) msgsEl.append(facts);
    if (d.clashes.length) {
      const sec = div("ev-sec"); sec.append(span("ev-h", "Clashes with"));
      const ul = document.createElement("ul"); ul.className = "ev-clash";
      d.clashes.forEach(c => {
        const li = document.createElement("li");
        li.append(span("ev-t", `${hm(new Date(c.start))}–${hm(new Date(c.end))}`), " ", c.title);
        if (c.response === "declined" || c.response === "tentative") li.append(span("ev-r", c.response === "declined" ? " · declined" : " · maybe"));
        ul.append(li);
      });
      sec.append(ul); msgsEl.append(sec);
    }
    if (d.attendees.length) {
      const sec = div("ev-sec"), n = {};
      d.attendees.forEach(a => (n[a.response] = (n[a.response] || 0) + 1));
      sec.append(span("ev-h", `${d.attendees.length} ${d.attendees.length === 1 ? "guest" : "guests"}`),
        span("ev-sum", ["accepted", "tentative", "declined", "needsAction"].filter(r => n[r]).map(r => `${n[r]} ${RESP[r]}`).join(" · ")));
      const ul = document.createElement("ul"); ul.className = "ev-guests";
      const SHOW = 8;
      d.attendees.forEach((a, i) => {
        const li = document.createElement("li");
        li.className = "r-" + a.response; li.hidden = i >= SHOW;
        li.innerHTML = `<i aria-hidden="true">${a.response === "accepted" ? ICON.yes : a.response === "declined" ? ICON.no : a.response === "tentative" ? "?" : ""}</i>`;
        li.append(span("ev-g", a.me ? "You" : a.name || a.email));
        const extra = [a.organiser && "organiser", a.optional && "optional", a.response !== "accepted" && RESP[a.response]].filter(Boolean).join(", ");
        if (extra) li.append(span("ev-r", " " + extra));
        if (a.name && !a.me) li.title = a.email;
        ul.append(li);
      });
      sec.append(ul);
      if (d.attendees.length > SHOW) {
        const more = document.createElement("button");
        more.type = "button"; more.className = "th-more"; more.textContent = `Show all ${d.attendees.length}`;
        more.addEventListener("click", () => { ul.querySelectorAll("li[hidden]").forEach(li => (li.hidden = false)); more.remove(); });
        sec.append(more);
      }
      msgsEl.append(sec);
    }
    if (d.description.trim()) msgsEl.append(fillText(div("th-text ev-desc"), d.description.trim()));
    fillComposer();
    body.scrollTop = 0;
  }

  function renderThread() {
    const d = cur.data;
    renderHead();
    $(".th-title").textContent = d.title;
    if (d.kind === "event") { renderEvent(); return; }
    $(".th-count").textContent = d.kind === "slack" ? (d.mode === "thread" ? "Thread" : d.reply.is_dm ? "" : "Channel") : d.messages.length > 1 ? `${d.messages.length} messages` : "";
    msgsEl.replaceChildren();
    if (d.kind === "email") {
      const n = d.messages.length;
      d.messages.forEach((m, i) => msgsEl.append(emailMsg(m, n > 3 && i < n - 2)));
    } else {
      let prev = null;
      d.messages.forEach(m => { msgsEl.append(slackMsg(m, prev, d.mode === "conversation" && m.ts === d.focus_ts)); prev = m; });
    }
    fillComposer();
    requestAnimationFrame(() => {
      const f = msgsEl.querySelector(".th-sm.focus");
      if (f && d.mode === "conversation") f.scrollIntoView({ block: "center" });
      else body.scrollTop = body.scrollHeight;
    });
  }

  // ---------- composer: what it shows is exactly what is sent ----------
  function fillComposer() {
    const d = cur.data, key = cur.key;
    if (filledFor === key) { updateDest(); return; }   // a reload of the same thread keeps what Peter typed
    filledFor = key;
    if (d.kind === "event") {
      const mine = d.my_response;
      form.dataset.mode = "event";
      form.innerHTML = !d.can_respond
        ? `<p class="ev-me">${d.organiser.me ? "You're the organiser" : "You aren't on the guest list"}</p>`
        : `<div class="ev-ans-row">${mine === "needsAction" || !mine ? '<span class="ev-me">Going?</span>' : ""}
            <div class="ev-ans" role="group" aria-label="Your answer">${[["accepted", "Accept"], ["tentative", "Maybe"], ["declined", "Decline"]].map(([v, l]) => `<button type="button" class="btn sm" data-ans="${v}" aria-pressed="${mine === v}">${l}</button>`).join("")}</div></div>
          <input name="note" class="ev-note-in" maxlength="2000" placeholder="Note to the organiser (optional)" aria-label="Note to the organiser">
          <span class="th-err" role="alert"></span>`;
      return;
    }
    const dr = loadDraft(key) || {};
    if (d.kind === "email") {
      const mode = dr.mode || "all", r = d.reply;
      form.dataset.mode = mode;
      form.innerHTML = `<div class="th-seg" role="group" aria-label="Reply to"><button type="button" data-mode="all" aria-pressed="${mode === "all"}">Reply all</button><button type="button" data-mode="one" aria-pressed="${mode === "one"}">Reply</button></div>
        <label class="th-f"><span>To</span><textarea name="to" rows="1" spellcheck="false"></textarea></label>
        <label class="th-f"><span>Cc</span><textarea name="cc" rows="1" spellcheck="false"></textarea></label>
        <label class="th-f"><span>Subject</span><input name="subject"></label>
        <textarea name="body" rows="4" aria-label="Reply"></textarea>
        <div class="th-cb"><span class="th-err" role="alert"></span><button class="btn primary sm th-send" type="submit">Send <kbd>${SEND_KEY}</kbd></button></div>`;
      form.elements.to.value = dr.to ?? r[mode].to.join(", ");
      form.elements.cc.value = dr.cc ?? r[mode].cc.join(", ");
      form.elements.subject.value = dr.subject ?? r.subject;
    } else {
      // A top-level DM message is answered in the DM by default; a channel message in its thread.
      const mode = d.reply.can_post_to_channel && (dr.mode === "channel" || (!dr.mode && d.reply.is_dm)) ? "channel" : "thread";
      form.dataset.mode = mode;
      const toggle = d.reply.can_post_to_channel
        ? `<div class="th-seg" role="group" aria-label="Post"><button type="button" data-mode="thread" aria-pressed="${mode === "thread"}">In thread</button><button type="button" data-mode="channel" aria-pressed="${mode === "channel"}">${d.reply.is_dm ? "In the DM" : "In the channel"}</button></div>` : "";
      form.innerHTML = `<div class="th-row">${toggle}<span class="th-dest"></span></div>
        <textarea name="body" rows="3" aria-label="Reply"></textarea>
        <div class="th-cb"><span class="th-err" role="alert"></span><button class="btn primary sm th-send" type="submit">Send <kbd>${SEND_KEY}</kbd></button></div>`;
    }
    form.elements.body.value = dr.body || "";
    updateDest(); fit();
  }
  function updateDest() {
    const d = cur?.data;
    if (!d || d.kind === "event") return;
    const ta = form.elements.body;
    if (d.kind === "email") { ta.placeholder = "Write a reply"; return; }
    const inThread = form.dataset.mode === "thread";
    form.querySelector(".th-dest").innerHTML = `To <b>${E(d.reply.where)}</b>${inThread ? ", in the thread" : ""}`;
    ta.placeholder = inThread ? "Reply in the thread" : `Message ${d.reply.where}`;
  }
  // The body grows to 40% of the window; To and Cc grow to show every recipient.
  const fit = () => form.querySelectorAll("textarea").forEach(ta => {
    ta.style.height = "auto";
    ta.style.height = (ta.name === "body" ? Math.min(ta.scrollHeight + 2, innerHeight * 0.4) : ta.scrollHeight) + "px";
  });
  form.addEventListener("input", e => { fit(); saveDraft(); form.querySelector(".th-err").textContent = ""; });
  form.addEventListener("keydown", e => {   // To and Cc are one line each, however long
    if (e.key === "Enter" && !e.metaKey && !e.ctrlKey && e.target.matches(".th-f textarea")) e.preventDefault();
  });
  addEventListener("resize", () => { if (isOpen()) fit(); });
  form.addEventListener("click", e => {
    const a = e.target.closest(".ev-ans button");
    if (a && cur?.data?.kind === "event") { answer(cur.key, cur.href, a.dataset.ans, form.elements.note?.value || ""); return; }
    const b = e.target.closest(".th-seg button");
    if (!b) return;
    const mode = b.dataset.mode;
    form.dataset.mode = mode;
    form.querySelectorAll(".th-seg button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    if (cur.data.kind === "email") {   // Reply and Reply all set To and Cc afresh
      form.elements.to.value = cur.data.reply[mode].to.join(", ");
      form.elements.cc.value = cur.data.reply[mode].cc.join(", ");
      fit();
    }
    updateDest(); saveDraft();
  });
  form.addEventListener("submit", e => { e.preventDefault(); send(); });

  // An answer goes only on Peter's press of Accept, Maybe or Decline; then the item is archived.
  async function answer(key, href, status, note) {
    if (answering) return;
    answering = true;
    const inPanel = cur?.key === key && cur.data?.kind === "event";
    const err = inPanel && form.querySelector(".th-err");
    const btns = inPanel ? [...form.querySelectorAll(".ev-ans button")] : [];
    btns.forEach(b => (b.disabled = true));
    if (err) err.textContent = "";
    try {
      const r = await api("/api/threads/rsvp", { key, href, status, note });
      if (r.dry_run) { toast("Dry run: nothing sent"); console.info("rsvp dry run", r); return; }
      const res = await api("/api/archive", { keys: [key] }).catch(() => null);
      const done = (res?.archived || []).includes(key);
      const el = row(key);
      if (el && done) el.hidden = true;
      if (cur?.key === key) close();
      toast(r.message + (done ? " and archived" : ""));
    } catch (e) {
      if (err) err.textContent = e.message;
      toast("Not answered: " + e.message);
    } finally {
      answering = false;
      btns.forEach(b => (b.disabled = false));
    }
  }

  async function send() {
    if (!cur?.data || cur.data.kind === "event" || sending || filledFor !== cur.key) return;
    const ta = form.elements.body, err = form.querySelector(".th-err"), btn = form.querySelector(".th-send");
    if (!ta.value.trim()) { ta.focus(); return; }
    const d = cur.data, key = cur.key;
    const payload = { key, href: cur.href, text: ta.value };
    if (d.kind === "email") Object.assign(payload, { to: form.elements.to.value, cc: form.elements.cc.value, subject: form.elements.subject.value, reply_to_id: d.reply.reply_to_id });
    else payload.mode = form.dataset.mode;
    sending = true; btn.disabled = true; err.textContent = "";
    form.querySelectorAll("input, textarea").forEach(f => (f.readOnly = true));
    try {
      const r = await api("/api/threads/send", payload);
      if (r.dry_run) { toast("Dry run: nothing sent"); console.info("threads dry run", r); return; }
      dropDraft(key);
      if (cur?.key === key) { ta.value = ""; fit(); }
      toast(r.message || "Sent", { label: "Archive", run: () => archive(key) });
      if (cur?.key === key) load(true);
    } catch (e) {
      err.textContent = e.message;
      toast("Not sent: " + e.message);
      if (/changed|newer/i.test(e.message) && cur?.key === key) load(true);
    } finally {
      sending = false; btn.disabled = false;
      form.querySelectorAll("input, textarea").forEach(f => (f.readOnly = false));
    }
  }

  // ---------- open, load, step, archive, close ----------
  function selectRow(row) {
    // desk.js selects a row on a click in it: a click on its number moves the desk's selection here.
    if (row && !row.classList.contains("kb-sel")) row.querySelector(".num")?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  }
  function open(row) {
    const kind = kindOf(row);
    if (!kind) return;
    const key = row.dataset.key;
    if (isOpen() && cur?.key === key) return;
    saveDraft();
    cur = { key, href: hrefOf(row), kind, data: null };
    filledFor = null;
    panel.hidden = false;
    renderHead();
    selectRow(row);
    load(false);
  }
  async function load(fresh) {
    const my = ++seq, key = cur.key;
    if (!cur.data || cur.data.key !== key) {
      $(".th-title").textContent = row(key)?.querySelector(".head")?.textContent.replace(/:\s*$/, "") || "";
      $(".th-count").textContent = "";
      msgsEl.innerHTML = `<div class="th-load" role="status"><span></span><span></span><span></span><p>${cur.kind === "event" ? "Loading the event…" : "Loading the thread…"}</p></div>`;
      form.innerHTML = ""; form.hidden = true; filledFor = null;
    }
    try {
      const d = await api("/api/threads/get", { key, href: cur.href, fresh: !!fresh });
      if (my !== seq || cur?.key !== key) return;
      cur.data = d; form.hidden = false;
      renderThread();
    } catch (e) {
      if (my !== seq || cur?.key !== key) return;
      if (cur.data) { toast("Couldn't reload the thread: " + e.message); return; }
      msgsEl.innerHTML = "";
      const box = div("th-error", `<p></p><button type="button" class="btn sm">Try again</button>`);
      box.querySelector("p").textContent = e.message;
      box.querySelector("button").addEventListener("click", () => load(true));
      msgsEl.append(box);
    }
  }
  const row = key => rowFor(key);
  function step(dir) {
    const rows = threadRows();
    if (!rows.length) return;
    const i = rows.findIndex(r => r.dataset.key === cur?.key);
    const next = rows[i < 0 ? 0 : i + dir];
    if (next) { open(next); next.scrollIntoView({ block: "nearest" }); }
  }
  async function archive(key = cur?.key) {
    if (!key) return;
    const keys = cur?.key === key && cur.data?.archive_keys || [key];
    try {
      const r = await api("/api/archive", { keys });
      if (!(r.archived || []).includes(key)) { toast("Couldn't archive: " + ((r.refused || [])[0] || "refused")); return; }
      const el = row(key);
      if (el) el.hidden = true;
      dropDraft(key);
      if (cur?.key === key) close();
      toast(keys.length > 1 ? "Archived both emails" : cur?.kind === "slack" || /^slack/.test(key) ? "Marked done" : "Archived");
    } catch (e) { toast("Couldn't archive: " + e.message); }
  }
  function close() {
    saveDraft();
    seq++; panel.hidden = true; cur = null; filledFor = null;
  }
  panel.addEventListener("click", e => {
    const b = e.target.closest("[data-th]");
    if (b?.dataset.th === "close") close();
    else if (b?.dataset.th === "archive") archive();
    else if (e.target.closest(".th-msg.collapsed")) e.target.closest(".th-msg").classList.remove("collapsed");
  });

  // Plain clicks on an email or Slack row's head (or source tag) open its thread here; ⌘-click still opens Gmail or Slack.
  // Runs on window in the capture phase, before desk.js's handlers (whose hover-action handler would queue the item).
  addEventListener("click", e => {
    if (e.button !== 0) return;
    const t = e.target;
    if (t.closest?.("#th-panel")) return;
    const thb = t.closest?.("#view-desk .acts button.th");
    if (thb) { e.preventDefault(); e.stopImmediatePropagation(); open(thb.closest(".row")); return; }
    const rb = t.closest?.("#view-desk .acts button.rsvp");
    if (rb) { e.preventDefault(); e.stopImmediatePropagation(); const r = rb.closest(".row"); answer(r.dataset.key, hrefOf(r), rb.dataset.rsvp, ""); return; }
    const plain = !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
    const r = t.closest?.("#view-desk .row"), kind = r && kindOf(r);
    if (kind && plain && t.closest("a.head, a.tag")) { e.preventDefault(); open(r); return; }   // desk.js still selects the row
    // A plain click elsewhere in the row opens its thread too, unless it lands on another link or control.
    if (kind && plain && !getSelection().toString() && !t.closest("a, button, input, textarea, .acts, .composer")) { open(r); return; }
    if (!isOpen() || t.closest?.(".acts, .composer, #toast, .tip, .kb-help, .pick-bar")) return;
    if (kind && plain) { open(r); return; }       // another email or Slack row: show its thread instead
    if (t.closest?.('[role="tab"]') || !t.closest?.("#peek")) close();
  }, true);

  addEventListener("keydown", e => {
    const field = e.target.closest?.("input, textarea, select, [contenteditable]");
    const cmdEnter = e.key === "Enter" && (isMac ? e.metaKey : e.ctrlKey);
    if (isOpen()) {
      if (field && panel.contains(field)) {
        if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); field.blur(); }
        else if (cmdEnter) { e.preventDefault(); e.stopImmediatePropagation(); send(); }
        return;
      }
      if (field || e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key;
      const stop = () => { e.preventDefault(); e.stopImmediatePropagation(); };
      if (k === "Escape") { stop(); close(); return; }
      if (k === "r" || k === "R") { if (cur?.data) { stop(); const ta = form.elements.body; ta?.focus(); ta?.setSelectionRange(ta.value.length, ta.value.length); } return; }
      if (k === "j" || k === "ArrowDown") { stop(); step(1); return; }
      if (k === "k" || k === "ArrowUp") { stop(); step(-1); return; }
      if (k === "e") { stop(); archive(); return; }
      if (k === "Enter") { stop(); return; }
      return;   // S, M, Q and the rest act on the selected row, which is this thread's, through desk.js
    }
    // Enter on a selected email or Slack row opens its thread here, rather than Gmail or Slack.
    if (e.key !== "Enter" || field || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
    if (document.querySelector(".row.picked") || document.getElementById("peek")?.hidden === false || document.getElementById("view-desk")?.hidden) return;
    const sel = document.querySelector("#view-desk .row.kb-sel");
    if (!sel || !visible(sel) || !kindOf(sel)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    open(sel);
  }, true);
})();
