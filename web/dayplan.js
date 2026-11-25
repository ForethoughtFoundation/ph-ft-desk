// Day plan, at the top of the Plans tab: today's schedule from the calendars, the S1 to S4 sessions (saved as
// Peter types, and written into Day.md by the server), and today's #checkin post. The Desk view gets a one-line
// prompt on weekdays until the check-in is posted. Data comes from desk_dayplan.py, on every sync as ext.dayplan.
(() => {
  const root = document.getElementById("dayplan");
  if (!root) return;
  const top = document.getElementById("desk-top");
  const TOKEN = document.querySelector('meta[name="desk-token"]').content;
  const api = async (path, body) => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN }, body: JSON.stringify(body || {}) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  };
  const E = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = msg => {
    const t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg; t.hidden = false;
    clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 3200);
  };
  // The check-in's markup as Slack shows it: **bold**, <u>underline</u>, ~~strike~~ (as post-day-to-slack.py reads it)
  const inline = s => {
    const re = /\*\*(.+?)\*\*|<u>(.+?)<\/u>|~~(.+?)~~|~(.+?)~/g;
    let out = "", pos = 0, m;
    while ((m = re.exec(s))) {
      out += E(s.slice(pos, m.index));
      if (m[1] !== undefined) out += `<b>${E(m[1])}</b>`;
      else if (m[2] !== undefined) out += `<u>${E(m[2])}</u>`;
      else out += `<s>${E(m[3] ?? m[4])}</s>`;
      pos = re.lastIndex;
    }
    return out + E(s.slice(pos));
  };
  // Mirrors tidy_sessions in desk_dayplan.py: a blank line stays only before an underlined heading; no "---".
  const tidy = text => {
    const lines = text.replace(/\r/g, "").split("\n").map(l => l.replace(/\s+$/, "")).filter(l => l.trim() !== "---");
    const out = [];
    lines.forEach((l, i) => {
      if (l.trim()) out.push(l);
      else if (out.length && out[out.length - 1].trim()) {
        const next = lines.slice(i + 1).find(x => x.trim()) || "";
        if (next.trimStart().startsWith("<u>")) out.push("");
      }
    });
    return out;
  };
  const MARK = "<u>Sessions:</u>";

  let info = null, revKey = null, dirty = false, saving = null, saveTimer = null, posting = false, refreshed = false;
  let saveState = "", saveError = "";

  root.innerHTML = `<section class="dp" aria-labelledby="dp-h">
      <header class="dp-top"><h2 id="dp-h">Day plan</h2><span class="dp-date"></span></header>
      <p class="dp-err" hidden></p>
      <div class="dp-conflict" role="alert" hidden></div>
      <div class="dp-grid">
        <div class="dp-edit">
          <h3 class="dp-h3">Schedule</h3>
          <ul class="dp-sched"></ul>
          <p class="dp-calnote" hidden></p>
          <div class="dp-sesshead"><label class="dp-h3" for="dp-sessions">Sessions</label><span class="dp-saved" aria-live="polite"></span></div>
          <textarea id="dp-sessions" class="dp-sessions" spellcheck="true" rows="5" aria-label="Sessions"></textarea>
        </div>
        <section class="dp-post" aria-label="Check-in for #checkin">
          <div class="dp-posthead"><span class="dp-chan">#checkin</span><span class="dp-status"></span></div>
          <div class="dp-preview"></div>
          <div class="dp-actions"></div>
        </section>
      </div>
    </section>`;
  const $ = s => root.querySelector(s);
  const ta = $("#dp-sessions");
  const set = (el, html) => { if (el._html !== html) { el.innerHTML = html; el._html = html; } };

  const fit = () => {
    if (!ta.offsetParent) return;   // hidden tab: measured again when it shows
    ta.style.height = "auto"; ta.style.height = Math.max(ta.scrollHeight + 2, 96) + "px";
  };
  let lastW = 0;   // refit when the tab shows or the width changes (the lines rewrap)
  new ResizeObserver(() => { const w = ta.offsetParent ? ta.clientWidth : 0; if (w && w !== lastW) fit(); lastW = w; }).observe(root);
  // The text the button will post: the server's block, with the sessions as they are in the field right now.
  const composed = () => {
    if (!info) return "";
    const lines = info.block.split("\n");
    const at = lines.indexOf(MARK);
    return [...lines.slice(0, at + 1), ...tidy(ta.value)].join("\n");
  };

  function renderSaved() {
    const el = $(".dp-saved");
    el.classList.toggle("bad", !!saveError);
    el.textContent = saveError ? `Not saved: ${saveError}` : saveState;
  }

  function renderPreview() {
    if (!info) return;
    set($(".dp-preview"), composed().split("\n").map(l => l.trim() ? `<div>${inline(l)}</div>` : '<div class="dp-gap"></div>').join(""));
    renderPost();
  }

  function renderPost() {
    const s = info.slack, p = s.posted, text = composed();
    const changed = p && (p.differs || text !== info.block);
    let status;
    if (p) status = `<a class="dp-ok" href="${E(p.url)}" target="_blank" rel="noopener">Posted at ${E(p.at)}</a>${changed ? '<span class="dp-chg">Out of date</span>' : ""}`;
    else if (!s.known) status = s.error ? `<span class="dp-bad">Can't check #checkin</span>` : '<span class="dp-wait">Checking…</span>';
    else status = '<span class="dp-not">Not posted</span>';
    set($(".dp-status"), status);
    const dis = posting || !!info.why_not;
    const title = info.why_not ? ` title="${E(info.why_not)}"` : "";
    let acts = "";
    if (!p) acts = `<button type="button" class="btn primary dp-go" data-act="post"${dis ? " disabled" : ""}${title}>${posting ? "Posting…" : "Post to #checkin"}</button>`;
    else if (changed) acts = `<button type="button" class="btn primary dp-go" data-act="update"${dis ? " disabled" : ""}${title}>${posting ? "Updating…" : "Update post"}</button>`;
    if (s.error && !s.known) acts += `<span class="dp-why">${E(s.error)}</span>`;
    else if (info.why_not) acts += `<span class="dp-why">${E(info.why_not)}</span>`;
    set($(".dp-actions"), acts);
    $(".dp-actions").hidden = !acts;
  }

  function renderConflict() {
    const c = info.conflict, box = $(".dp-conflict");
    box.hidden = !c;
    if (!c) { set(box, ""); return; }
    const rows = c.diff.map(r => `<div class="dl dl-${r.k}"><span class="dl-m" aria-hidden="true">${r.k === "desk" ? "−" : r.k === "obs" ? "+" : ""}</span><span class="sr">${r.k === "desk" ? "Desk only: " : r.k === "obs" ? "Note only: " : ""}</span><span class="dl-t">${E(r.t) || "&nbsp;"}</span></div>`).join("");
    set(box, `<p class="dp-ch"><b>Day.md was edited outside the desk.</b> Nothing is written to it until you choose.</p>
      <div class="dp-legend" aria-hidden="true"><span class="lg lg-desk">− Desk</span><span class="lg lg-obs">+ Note</span></div>
      <div class="dp-diff">${rows}</div>
      <div class="dp-cacts"><button type="button" class="btn sm" data-resolve="note">Keep the note's version</button><button type="button" class="btn sm" data-resolve="desk">Overwrite the note</button></div>`);
  }

  function renderNudge() {
    if (!top) return;
    const show = info && info.weekday && info.slack.known && !info.slack.posted;
    set(top, show ? `<div class="dp-nudge" role="status"><span>You haven't posted today's check-in.</span><button type="button" class="btn sm primary" data-dp-open>Open day plan</button></div>` : "");
  }

  function render() {
    if (!info) return;
    $(".dp-date").textContent = info.heading;
    const err = info.day_md.error;
    $(".dp-err").hidden = !err; $(".dp-err").textContent = err || "";
    set($(".dp-sched"), info.schedule.length ? info.schedule.map(l => {
      const m = l.match(/^(\d{2})(\d{2})\s+(.*)$/);
      return m ? `<li><time>${m[1]}:${m[2]}</time><span>${inline(m[3])}</span></li>` : `<li><time></time><span>${inline(l)}</span></li>`;
    }).join("") : `<li class="dp-none">${info.calendar.loaded ? "No events scheduled." : "Loading the calendar…"}</li>`);
    const cal = info.calendar, note = $(".dp-calnote");
    note.hidden = !cal.error; note.textContent = cal.error ? `Calendar: ${cal.error}` : "";
    // Take the server's sessions when they changed other than by typing here (a new day, the note's version adopted),
    // or when the field is idle and differs; never while Peter is typing or a save is in flight.
    const key = `${info.date}:${info.rev}`;
    if (!dirty && !saving && (key !== revKey || (document.activeElement !== ta && ta.value !== info.sessions))) {
      ta.value = info.sessions; fit();
    }
    revKey = key;
    renderConflict(); renderPreview(); renderNudge();
  }

  async function save() {
    clearTimeout(saveTimer);
    if (saving) { await saving; if (!dirty) return; }
    const sent = ta.value;
    dirty = false; saveState = "Saving…"; saveError = ""; renderSaved();
    saving = api("/api/dayplan/save", { sessions: sent }).then(d => {
      if (ta.value !== sent) dirty = true;
      info = d; saveState = "Saved"; renderSaved(); render();
      setTimeout(() => { if (saveState === "Saved") { saveState = ""; renderSaved(); } }, 2500);
    }).catch(e => { dirty = true; saveError = e.message; renderSaved(); })
      .finally(() => { saving = null; });
    return saving;
  }

  ta.addEventListener("input", () => {
    dirty = true; saveError = ""; saveState = ""; renderSaved(); fit(); renderPreview();
    clearTimeout(saveTimer); saveTimer = setTimeout(save, 900);
  });
  ta.addEventListener("blur", () => { if (dirty) save(); });
  ta.addEventListener("keydown", e => { if (e.key === "Escape") ta.blur(); });

  root.addEventListener("click", async e => {
    const go = e.target.closest(".dp-go");
    if (go && !go.disabled) {
      posting = true; renderPost();
      try {
        if (dirty || saving) await save();
        if (saveError) throw new Error("the plan isn't saved");
        const d = await api("/api/dayplan/post", { text: composed(), action: go.dataset.act });
        info = d; toast(d.message || "Posted");
      } catch (err) { toast(`Not posted: ${err.message}`); }
      finally { posting = false; render(); }
      return;
    }
    const r = e.target.closest("[data-resolve]");
    if (r) {
      r.disabled = true;
      try {
        if (r.dataset.resolve === "note") { clearTimeout(saveTimer); dirty = false; await saving; }
        info = await api("/api/dayplan/resolve", { choice: r.dataset.resolve });
        if (r.dataset.resolve === "note") revKey = null;
        toast(r.dataset.resolve === "note" ? "Took the note's version" : "Wrote the desk's plan to Day.md");
        render();
      } catch (err) { r.disabled = false; toast(err.message); }
    }
  });

  top?.addEventListener("click", e => {
    if (!e.target.closest("[data-dp-open]")) return;
    const tab = document.getElementById("tab-week");
    if (tab && tab.getAttribute("aria-selected") !== "true") tab.click();
    requestAnimationFrame(() => {
      root.scrollIntoView({ block: "start" });
      ta.focus({ preventScroll: true });
      ta.setSelectionRange(ta.value.length, ta.value.length);
    });
  });

  window.addEventListener("desk:sync", e => {
    const d = e.detail?.ext?.dayplan;
    if (!d || d.error || !d.date) return;
    info = d; render();
    // Collectors fill these within minutes of the server starting; until then, ask once.
    if (!refreshed && (!d.calendar.loaded || !d.slack.known)) {
      refreshed = true;
      api("/api/dayplan/refresh", {}).then(x => { info = x; render(); }).catch(() => {});
    }
  });
})();
