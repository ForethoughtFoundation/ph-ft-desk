// Small popovers the desk's other parts open: a calendar for picking a date (and a time), and a menu of choices.
// ftDesk.datePicker(anchor, {value, time, min, clear, label, onPick}) and ftDesk.menu(anchor, {label, items, filter, onPick}).
// Both handle their keys on window in the capture phase, before any desk shortcut, and close on Esc or a click outside.
(() => {
  const ftDesk = (window.ftDesk = window.ftDesk || {});
  const pad = n => String(n).padStart(2, "0");
  const day0 = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
  const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const same = (a, b) => !!a && !!b && iso(a) === iso(b);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const CHEV = d => `<svg viewBox="0 0 12 12" aria-hidden="true"><path d="${d === "prev" ? "M7.5 2.5 4 6l3.5 3.5" : "M4.5 2.5 8 6l-3.5 3.5"}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  let open = null;   // {el, anchor, keys(e), close()}
  const place = (el, anchor) => {
    const r = anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left)) + "px";
    el.style.top = (r.bottom + 6 + h > innerHeight - 8 && r.top - h - 6 > 8 ? r.top - h - 6 : Math.min(r.bottom + 6, innerHeight - h - 8)) + "px";
  };
  const closeOpen = (refocus = true) => {
    if (!open) return;
    const o = open; open = null;
    o.el.remove();
    o.anchor?.setAttribute?.("aria-expanded", "false");
    if (refocus && o.anchor?.isConnected) o.anchor.focus({ preventScroll: true });
    o.onClose?.();
  };
  addEventListener("keydown", e => {
    if (!open || !open.el.contains(e.target)) {
      if (open && e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closeOpen(); }
      return;
    }
    // Every key typed in a popover is the popover's: none reaches the desk's shortcuts behind it.
    e.stopImmediatePropagation();
    if (e.key === "Escape") { e.preventDefault(); closeOpen(); return; }
    open.keys(e);
  }, true);
  document.addEventListener("mousedown", e => { if (open && !open.el.contains(e.target) && !open.anchor?.contains?.(e.target)) closeOpen(false); }, true);
  addEventListener("resize", () => closeOpen(false));
  ftDesk.closePopover = closeOpen;
  ftDesk.popoverOpen = () => !!open;

  // ---------------------------------------------------------------- calendar
  // Monday first, six weeks shown; a day from the month before or after is picked like any other. Picking a day
  // commits it (with the time field's time, when there is one) and closes the calendar.
  ftDesk.datePicker = (anchor, opts = {}) => {
    closeOpen(false);
    const min = opts.min ? day0(opts.min) : null;
    let value = opts.value ? new Date(opts.value) : null;
    let focus = day0(value || new Date());
    if (min && focus < min) focus = min;
    let month = new Date(focus.getFullYear(), focus.getMonth(), 1);
    const el = document.createElement("div");
    el.className = "cal-pop"; el.setAttribute("role", "dialog"); el.setAttribute("aria-label", opts.label || "Choose a date");
    const time = opts.time ? (value ? `${pad(value.getHours())}:${pad(value.getMinutes())}` : opts.defaultTime || "09:00") : null;
    el.innerHTML = `<div class="cal-head"><button type="button" class="cal-nav" data-m="-1" aria-label="Previous month">${CHEV("prev")}</button>
      <h3 class="cal-month" aria-live="polite"></h3><button type="button" class="cal-nav" data-m="1" aria-label="Next month">${CHEV("next")}</button></div>
      <table class="cal-grid" role="grid"><thead><tr>${WEEKDAYS.map(d => `<th scope="col" abbr="${d}">${d.slice(0, 2)}</th>`).join("")}</tr></thead><tbody></tbody></table>
      ${time || (opts.clear && value) ? `<div class="cal-foot">${time ? `<label class="cal-time">Time <input type="time" value="${time}" step="300"></label>` : ""}<span class="cal-sp"></span>
        ${opts.clear && value ? `<button type="button" class="btn sm cal-clear">${esc(opts.clear)}</button>` : ""}</div>` : ""}`;
    const tbody = el.querySelector("tbody"), timeIn = el.querySelector(".cal-time input");
    const disabled = d => !!min && d < min;
    const render = (focusDay = false) => {
      el.querySelector(".cal-month").textContent = month.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
      const start = addDays(month, -((month.getDay() + 6) % 7)), today = day0(new Date());
      let html = "";
      for (let w = 0; w < 6; w++) {
        html += "<tr>";
        for (let i = 0; i < 7; i++) {
          const d = addDays(start, w * 7 + i);
          const cls = ["cal-day", d.getMonth() !== month.getMonth() && "cal-out", same(d, today) && "cal-today", same(d, value) && "cal-sel"].filter(Boolean).join(" ");
          html += `<td role="gridcell"${same(d, value) ? ' aria-selected="true"' : ""}><button type="button" class="${cls}" data-d="${iso(d)}" tabindex="${same(d, focus) ? 0 : -1}"`
            + `${disabled(d) ? " disabled" : ""} aria-label="${d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" })}${same(d, today) ? ", today" : ""}">${d.getDate()}</button></td>`;
        }
        html += "</tr>";
      }
      tbody.innerHTML = html;
      el.querySelector('.cal-nav[data-m="-1"]').disabled = !!min && new Date(month.getFullYear(), month.getMonth(), 0) < min;
      if (focusDay) tbody.querySelector(`[data-d="${iso(focus)}"]`)?.focus();
    };
    const moveTo = d => {
      if (disabled(d)) d = min;
      focus = d;
      if (d.getMonth() !== month.getMonth() || d.getFullYear() !== month.getFullYear()) {
        // Stay on this month while the day is shown in its first or last row; turn the page otherwise.
        const shown = [...tbody.querySelectorAll("[data-d]")].some(b => b.dataset.d === iso(d));
        if (!shown) month = new Date(d.getFullYear(), d.getMonth(), 1);
      }
      render(true);
    };
    const pick = dstr => {
      const [y, m, dd] = dstr.split("-").map(Number);
      const out = new Date(y, m - 1, dd);
      if (timeIn) {
        const [hh, mm] = (timeIn.value || opts.defaultTime || "09:00").split(":").map(Number);
        out.setHours(hh, mm, 0, 0);
      }
      closeOpen();
      opts.onPick?.(out);
    };
    el.addEventListener("click", e => {
      const nav = e.target.closest(".cal-nav");
      if (nav) { month = new Date(month.getFullYear(), month.getMonth() + +nav.dataset.m, 1); render(); return; }
      const b = e.target.closest(".cal-day");
      if (b && !b.disabled) { pick(b.dataset.d); return; }
      if (e.target.closest(".cal-clear")) { closeOpen(); opts.onPick?.(null); }
    });
    const keys = e => {
      const onDay = e.target.closest?.(".cal-day");
      if (e.key === "Tab") {
        const f = [...el.querySelectorAll("button:not([disabled]), input")].filter(x => x.tabIndex >= 0);
        const i = f.indexOf(document.activeElement);
        if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
        return;
      }
      if (e.target === timeIn) { if (e.key === "Enter") { e.preventDefault(); pick(iso(focus)); } return; }
      if (!onDay) return;
      const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, h: -1, l: 1, k: -7, j: 7 }[e.key];
      if (step) { e.preventDefault(); moveTo(addDays(focus, step)); return; }
      if (e.key === "PageUp" || e.key === "PageDown") {
        e.preventDefault();
        const n = (e.key === "PageUp" ? -1 : 1) * (e.shiftKey ? 12 : 1);
        const t = new Date(focus.getFullYear(), focus.getMonth() + n, 1);
        month = t; moveTo(new Date(t.getFullYear(), t.getMonth(), Math.min(focus.getDate(), new Date(t.getFullYear(), t.getMonth() + 1, 0).getDate())));
        return;
      }
      if (e.key === "Home" || e.key === "End") { e.preventDefault(); moveTo(addDays(focus, e.key === "Home" ? -((focus.getDay() + 6) % 7) : 6 - ((focus.getDay() + 6) % 7))); return; }
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(onDay.dataset.d); }
    };
    document.body.appendChild(el);
    render();
    place(el, anchor);
    anchor.setAttribute?.("aria-expanded", "true");
    open = { el, anchor, keys, onClose: opts.onClose };
    tbody.querySelector(`[data-d="${iso(focus)}"]`)?.focus();
    return { close: () => open?.el === el && closeOpen() };
  };

  // ---------------------------------------------------------------- menu
  // items: [{value, label, html?, checked?, hint?}]. Arrows or J and K move, Enter picks, 1-9 pick by position
  // (in a menu without a filter), and a filter field narrows a long list as you type.
  ftDesk.menu = (anchor, opts = {}) => {
    closeOpen(false);
    const el = document.createElement("div");
    el.className = "fm-pop"; el.setAttribute("role", "menu"); el.setAttribute("aria-label", opts.label || "Choose");
    const items = opts.items || [];
    const numbered = !opts.filter && items.length <= 9;
    el.innerHTML = (opts.filter ? `<input type="text" class="fm-filter" placeholder="${esc(opts.filter)}" aria-label="${esc(opts.filter)}" autocomplete="off" spellcheck="false">` : "")
      + `<div class="fm-list"></div>`;
    const list = el.querySelector(".fm-list"), filter = el.querySelector(".fm-filter");
    list.classList.toggle("fm-noic", !items.some(it => it.html));
    const render = () => {
      const q = (filter?.value || "").trim().toLowerCase();
      const shown = items.map((it, i) => ({ it, i })).filter(({ it }) => !q || it.label.toLowerCase().includes(q));
      list.innerHTML = shown.map(({ it, i }, n) => `<button type="button" class="fm-item" role="menuitemradio" aria-checked="${!!it.checked}" data-i="${i}" tabindex="-1">`
        + `<span class="fm-ic" aria-hidden="true">${it.html || ""}</span><span class="fm-l">${esc(it.label)}</span>`
        + `<span class="fm-check" aria-hidden="true">${it.checked ? '<svg viewBox="0 0 12 12"><path d="M2.5 6.3 5 8.6l4.5-5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>' : ""}</span>`
        + `${numbered ? `<kbd>${n + 1}</kbd>` : ""}</button>`).join("") || '<p class="fm-none">No matches</p>';
    };
    const buttons = () => [...list.querySelectorAll(".fm-item")];
    const choose = b => { const it = items[+b.dataset.i]; closeOpen(); opts.onPick?.(it); };
    el.addEventListener("click", e => { const b = e.target.closest(".fm-item"); if (b) choose(b); });
    el.addEventListener("mousemove", e => { const b = e.target.closest(".fm-item"); if (b && document.activeElement !== b && document.activeElement !== filter) b.focus({ preventScroll: true }); });
    filter?.addEventListener("input", () => { render(); buttons()[0]?.classList.add("fm-hot"); });
    const keys = e => {
      const bs = buttons(), i = bs.indexOf(document.activeElement);
      const inFilter = e.target === filter;
      const dir = { ArrowDown: 1, ArrowUp: -1, ...(inFilter ? {} : { j: 1, k: -1 }) }[e.key];
      if (dir) {
        e.preventDefault();
        if (!bs.length) return;
        const hot = inFilter ? bs.findIndex(b => b.classList.contains("fm-hot")) : i;
        bs.forEach(b => b.classList.remove("fm-hot"));
        const to = bs[hot < 0 ? (dir > 0 ? 0 : bs.length - 1) : (hot + dir + bs.length) % bs.length];
        if (inFilter && dir < 0 && hot === 0) { filter.focus(); return; }
        to.focus();
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        const b = inFilter ? (bs.find(x => x.classList.contains("fm-hot")) || bs[0]) : bs[i];
        if (b) choose(b);
        return;
      }
      if (e.key === "Tab") { e.preventDefault(); return; }
      if (numbered && /^[1-9]$/.test(e.key) && bs[+e.key - 1]) { e.preventDefault(); choose(bs[+e.key - 1]); return; }
      if (filter && !inFilter && e.key.length === 1 && !e.metaKey && !e.ctrlKey) { filter.focus(); }
    };
    render();
    document.body.appendChild(el);
    place(el, anchor);
    anchor.setAttribute?.("aria-expanded", "true");
    open = { el, anchor, keys, onClose: opts.onClose };
    if (filter) filter.focus();
    else (buttons().find(b => b.getAttribute("aria-checked") === "true") || buttons()[0])?.focus();
    return { close: () => open?.el === el && closeOpen() };
  };
})();
