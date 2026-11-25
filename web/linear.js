// Changes not yet in Linear: a pill at the bottom left counts those waiting (in the server's outbox, desk_linear.py,
// or in this browser while the server is down) and those Linear refused; its list offers Retry now, and Retry or
// Discard for a refused one. Cards and the peek mark an issue with a change still waiting.
(() => {
  const ftDesk = (window.ftDesk = window.ftDesk || {});
  const TOKEN = document.querySelector('meta[name="desk-token"]')?.content || "";
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const api = async (path, body) => {
    let r;
    try { r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN }, body: JSON.stringify(body || {}) }); }
    catch { throw new Error("The desk server isn't reachable"); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  };
  const toast = (m, a) => ftDesk.toast?.(m, a);

  let server = { waiting: 0, items: [], held: [] };
  const local = () => (ftDesk.outbox?.() || []);
  const issueOf = x => x.issue || x.body?.issue;
  ftDesk.pendingFor = id => server.items.filter(x => x.issue === id).length + local().filter(x => x.body?.issue === id).length;

  const pill = document.createElement("button");
  pill.type = "button"; pill.className = "lq-sync"; pill.hidden = true;
  pill.setAttribute("aria-haspopup", "dialog"); pill.setAttribute("aria-expanded", "false");
  const pop = document.createElement("div");
  pop.className = "lq-pop"; pop.hidden = true; pop.setAttribute("role", "dialog"); pop.setAttribute("aria-label", "Changes not yet applied");
  document.body.append(pill, pop);

  const place = () => {
    const qa = document.querySelector(".qa-sync:not([hidden])");
    const left = qa ? qa.getBoundingClientRect().right + 8 : null;
    pill.style.left = left ? left + "px" : "";
    pop.style.left = left ? Math.min(left, innerWidth - pop.offsetWidth - 16) + "px" : "";
  };
  let lastWaiting = 0;
  function paint(fromServer) {
    const loc = local(), waiting = server.items.length + loc.length, held = server.held.length;
    if (fromServer === true) { if (lastWaiting && !waiting && !held) toast("Changes synced"); lastWaiting = waiting; }
    pill.hidden = !waiting && !held;
    markCards();
    if (pill.hidden) { closePop(); return; }
    const parts = [];
    if (waiting) parts.push(`${waiting} ${waiting === 1 ? "change" : "changes"} waiting to sync`);
    if (held) parts.push(`${held} not applied`);
    pill.innerHTML = `<i aria-hidden="true"></i>${parts.join(" · ")}`;
    pill.classList.toggle("lq-held", !!held);
    const err = server.items.find(x => x.error)?.error || (loc.length ? "The desk server isn't reachable" : "");
    if (err) pill.dataset.tip = err; else delete pill.dataset.tip;
    place();
    if (!pop.hidden) paintPop();
  }
  function paintPop() {
    const row = (label, extra = "") => `<li><span class="lq-t">${esc(label)}</span>${extra}</li>`;
    const waiting = [...server.items.map(x => row(x.label)), ...local().map(x => row(x.label))];
    const held = server.held.map(x => row(x.label, `<span class="lq-why">${esc(x.error || "")}</span>
      <span class="lq-acts"><button type="button" class="btn sm" data-h="retry" data-id="${esc(x.id)}">Retry</button>
      <button type="button" class="btn sm" data-h="discard" data-id="${esc(x.id)}">Discard</button></span>`));
    pop.innerHTML = (waiting.length ? `<h3>Waiting to sync</h3><ul>${waiting.join("")}</ul>
        <div class="lq-foot"><button type="button" class="btn sm" data-h="now">Retry now</button></div>` : "")
      + (held.length ? `<h3>Not applied</h3><ul>${held.join("")}</ul>` : "");
    place();
  }
  const closePop = () => { if (!pop.hidden) { pop.hidden = true; pill.setAttribute("aria-expanded", "false"); } };
  pill.addEventListener("click", () => {
    if (!pop.hidden) { closePop(); return; }
    pop.hidden = false; pill.setAttribute("aria-expanded", "true"); paintPop(); pop.querySelector("button")?.focus();
  });
  document.addEventListener("mousedown", e => { if (!pop.hidden && !pop.contains(e.target) && !pill.contains(e.target)) closePop(); });
  pop.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); closePop(); pill.focus(); } });
  pop.addEventListener("click", async e => {
    const b = e.target.closest("[data-h]");
    if (!b) return;
    b.disabled = true;
    try {
      if (b.dataset.h === "now") {
        if (local().length && !(await ftDesk.flushOutbox?.())) throw new Error("The desk server isn't reachable");
        const r = await api("/api/linear/retry", {});
        toast(r.waiting ? `${r.waiting} still waiting${r.error ? ": " + r.error : ""}` : r.held ? `${r.held} not applied: see the list` : "Changes synced");
      } else {
        const r = await api("/api/linear/held", { id: b.dataset.id, action: b.dataset.h });
        if (b.dataset.h === "retry") toast(r.result?.status === "applied" ? "Applied" : r.result?.status === "queued" ? "Waiting to sync" : `Not applied: ${r.result?.error || ""}`);
        else toast("Discarded");
      }
    } catch (err) { toast(err.message); }
    finally { b.disabled = false; }
  });

  // A card whose issue has a change waiting carries a small pulsing dot.
  function markCards() {
    const ids = new Set([...server.items.map(issueOf), ...local().map(issueOf)].filter(Boolean));
    document.querySelectorAll(".card-wrap").forEach(li => {
      const id = li.querySelector(".card-id")?.textContent, on = ids.has(id);
      li.classList.toggle("lq-pending", on);
      const dot = li.querySelector(".lq-dot");
      if (on && !dot) li.querySelector(".card-id")?.insertAdjacentHTML("afterend", '<span class="lq-dot" title="Waiting to sync"><span class="sr">Waiting to sync</span></span>');
      else if (!on && dot) dot.remove();
    });
  }

  window.addEventListener("desk:sync", e => {
    const q = e.detail?.ext?.linear;
    if (q && !q.error) server = { waiting: q.waiting || 0, items: q.items || [], held: q.held || [] };
    paint(true);
  });
  window.addEventListener("desk:outbox", () => paint());
  addEventListener("resize", place);
  setInterval(place, 2000);   // follows the quick-create pill beside it
  paint();
})();
