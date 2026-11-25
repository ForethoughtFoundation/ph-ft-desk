// Slack to-dos: tick one off (Done, or E), change its due date (Due) or snooze it (Snooze, or Z), on Peter's own
// Slack To-dos list, from the row's hover actions. Done goes through the desk's archive (desk_server.archive);
// Due and Snooze through desk_slacktodos.py. Snoozed to-dos sit in the section's Snoozed group until they wake.
(() => {
  const TOKEN = document.querySelector('meta[name="desk-token"]')?.content;
  const ftDesk = window.ftDesk || {};
  if (!TOKEN || !ftDesk.datePicker || !ftDesk.menu) return;
  const api = async (path, body) => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  };
  const toast = (msg, action) => ftDesk.toast?.(msg, action);
  const ICON = {
    done: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6.3 5 8.6l4.5-5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    due: '<svg viewBox="0 0 12 12" aria-hidden="true"><rect x="1.75" y="2.5" width="8.5" height="7.5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M1.75 4.75h8.5M4 1.5v2M8 1.5v2" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>',
    snooze: '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6.5" r="4.2" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M6 4.4v2.3l1.5.9M2.2 2 3.5 1M9.8 2 8.5 1" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>',
  };
  const isTodo = el => el?.dataset.key?.startsWith("slack-todo:");
  const at = (d, h) => { const x = new Date(d); x.setHours(h, 0, 0, 0); return x; };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const hm = d => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const when = d => {
    const now = new Date();
    return d.toDateString() === now.toDateString() ? hm(d)
      : d.toDateString() === addDays(now, 1).toDateString() ? "tomorrow " + hm(d)
      : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }) + " " + hm(d);
  };
  const epoch = d => d ? Math.round(d / 1000) : null;

  // The row's hover actions: Archive reads Done, and Due and Snooze follow it (rebuilt after every region swap).
  const addButtons = () => {
    document.querySelectorAll("#view-desk .row > .acts").forEach(acts => {
      if (!isTodo(acts.parentElement) || acts.querySelector(".std")) return;
      const ar = acts.querySelector(".ar");
      if (ar) { ar.innerHTML = `${ICON.done} Done`; ar.dataset.tip = "Done in Slack|E"; }
      const mk = (cls, html, tipText) => { const b = document.createElement("button"); b.type = "button"; b.className = cls; b.innerHTML = html; b.dataset.tip = tipText; return b; };
      const btns = [mk("std", `${ICON.due} Due`, "Change the due date"), mk("stz", `${ICON.snooze} Snooze`, "Snooze in Slack|Z")];
      ar ? ar.after(...btns) : acts.append(...btns);
    });
  };
  addButtons();
  addEventListener("desk:sync", addButtons);

  async function change(row, path, body, done, undoBody) {
    try {
      await api(path, { key: row.dataset.key, ...body });
      toast(done, undoBody && { label: "Undo", run: () => api(path, { key: row.dataset.key, ...undoBody }).then(() => ftDesk.sync?.()).catch(e => toast("Couldn't undo: " + e.message)) });
      ftDesk.sync?.();
    } catch (e) { toast("Slack didn't take it: " + e.message); }
  }
  const prev = (row, attr) => { const v = row.dataset[attr]; return v ? epoch(new Date(v)) : null; };

  function pickDue(row, anchor) {
    const cur = row.dataset.due ? new Date(row.dataset.due) : null;
    ftDesk.datePicker(anchor, {
      value: cur, time: true, defaultTime: "09:00", clear: "No due date", label: "Due",
      onPick: d => change(row, "/api/slacktodo/due", { due: epoch(d) }, d ? "Due " + when(d) : "Due date cleared", { due: prev(row, "due") }),
    });
  }

  function snoozeMenu(row, anchor) {
    const now = new Date(), two = new Date(+now + 2 * 36e5), monday = at(addDays(now, (8 - now.getDay()) % 7 || 7), 9);
    const items = [
      { label: `For 2 hours (${hm(two)})`, until: two },
      { label: "Until tomorrow, 09:00", until: at(addDays(now, 1), 9) },
      { label: "Until Monday, 09:00", until: monday },
      { label: `For 7 days (${at(addDays(now, 7), 9).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })})`, until: at(addDays(now, 7), 9) },
      { label: "Another time…", other: true },
      ...(row.dataset.snoozedUntil ? [{ label: "Wake now", until: null }] : []),
    ];
    const go = until => change(row, "/api/slacktodo/snooze", { until: epoch(until) },
      until ? "Snoozed until " + when(until) : "Woken", { until: prev(row, "snoozedUntil") });
    ftDesk.menu(anchor, {
      label: "Snooze", items,
      onPick: it => it.other
        ? setTimeout(() => ftDesk.datePicker(anchor, { time: true, defaultTime: "09:00", min: new Date(), label: "Snooze until", onPick: d => d && go(d) }), 0)
        : go(it.until),
    });
  }

  // Window, capture phase: before desk.js's hover-action handler, which would queue the item.
  addEventListener("click", e => {
    const b = e.target.closest?.("#view-desk .acts button.std, #view-desk .acts button.stz");
    if (!b) return;
    e.preventDefault(); e.stopImmediatePropagation();
    const row = b.closest(".row");
    b.classList.contains("std") ? pickDue(row, b) : snoozeMenu(row, b);
  }, true);

  addEventListener("keydown", e => {
    if (e.key !== "z" || e.metaKey || e.ctrlKey || e.altKey || e.target.closest?.("input, textarea, select, [contenteditable]")) return;
    if (ftDesk.popoverOpen?.()) return;
    const row = document.querySelector("#view-desk .row.kb-sel");
    if (!isTodo(row) || row.hidden) return;
    e.preventDefault(); e.stopImmediatePropagation();
    snoozeMenu(row, row.querySelector(".stz") || row);
  }, true);
})();
