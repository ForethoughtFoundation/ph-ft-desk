// Quick agent (A, from anywhere on the desk): a box for a prompt that opens a new Claude Code tab in Warp on the
// working folder, as `cc f` would, with the prompt exactly as typed (desk_server.py's /api/agent). Styled like quick create.
(() => {
  const TOKEN = document.querySelector('meta[name="desk-token"]')?.content || "";
  const isMac = navigator.platform.includes("Mac");
  const DRAFT = "ftdesk.quickagent.draft";
  const lsGet = () => { try { return localStorage.getItem(DRAFT) || ""; } catch { return ""; } };
  const lsSet = v => { try { v ? localStorage.setItem(DRAFT, v) : localStorage.removeItem(DRAFT); } catch { /* private mode */ } };
  const toast = m => window.ftDesk?.toast?.(m);
  let back = null, opener = null;

  const isOpen = () => !!back && back.isConnected;
  function open() {
    if (isOpen()) { back.querySelector("textarea").focus(); return; }
    opener = document.activeElement;
    back = document.createElement("div");
    back.className = "qa-back";
    back.innerHTML = `<div class="qa qg" role="dialog" aria-modal="true" aria-labelledby="qg-h">
      <div class="qa-top"><h2 id="qg-h">Quick agent</h2></div>
      <div class="qa-boxes"><div class="qa-box"><div class="qa-field qg-field">
        <textarea rows="3" spellcheck="true" autocomplete="off" aria-labelledby="qg-h" placeholder="Prompt for a new Claude Code tab"></textarea></div></div></div>
      <div class="qa-foot"><span class="qa-sp"></span>
        <button type="button" class="btn" data-a="cancel" data-tip="Close|Esc">Cancel</button>
        <button type="button" class="btn primary" data-a="go">Start <kbd>${isMac ? "⌘↵" : "Ctrl ↵"}</kbd></button></div></div>`;
    document.body.appendChild(back);
    const ta = back.querySelector("textarea");
    ta.value = lsGet();
    const fit = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, innerHeight * 0.5) + "px"; };
    ta.addEventListener("input", () => { ta.removeAttribute("aria-invalid"); lsSet(ta.value); fit(); });
    back.addEventListener("mousedown", e => { if (e.target === back) close(); });
    back.querySelector('[data-a="cancel"]').addEventListener("click", () => close());
    back.querySelector('[data-a="go"]').addEventListener("click", () => go());
    fit(); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
  }
  function close() {
    if (!isOpen()) return;
    back.remove(); back = null;
    if (opener?.isConnected) opener.focus({ preventScroll: true });
  }
  async function go() {
    const ta = back.querySelector("textarea"), btn = back.querySelector('[data-a="go"]');
    const prompt = ta.value;
    if (!prompt.trim()) { ta.setAttribute("aria-invalid", "true"); ta.focus(); return; }
    if (btn.disabled) return;
    btn.disabled = ta.readOnly = true;
    try {
      const r = await fetch("/api/agent", { method: "POST", headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN }, body: JSON.stringify({ prompt }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || r.statusText);
      lsSet(""); close();
      toast(d.message || "Opened a Claude Code tab");
    } catch (e) {
      btn.disabled = ta.readOnly = false;
      toast(`Couldn't start it: ${e.message === "Load failed" || e.message === "Failed to fetch" ? "the desk server isn't reachable" : e.message}`);
    }
  }

  // On window in the capture phase, so it runs before the desk's shortcuts. A plain A opens the box unless something
  // else has the keyboard: a field, quick create, a menu or popover.
  addEventListener("keydown", e => {
    if (e.isComposing || e.keyCode === 229) return;
    if (!isOpen()) {
      if (e.key !== "a" || e.shiftKey || e.metaKey || e.ctrlKey || e.altKey || e.repeat) return;
      if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
      if (document.querySelector(".qa-back, .zmenu:not([hidden]), .fm-pop, .cal-pop")) return;
      e.preventDefault(); e.stopImmediatePropagation(); open();
      return;
    }
    // Open: every key belongs to the box.
    e.stopImmediatePropagation();
    if (!back.contains(e.target)) { e.preventDefault(); back.querySelector("textarea").focus(); return; }
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key === "Enter" && (isMac ? e.metaKey : e.ctrlKey)) { e.preventDefault(); go(); return; }
    if (e.key === "Tab") {
      const f = [...back.querySelectorAll("textarea, button:not([disabled])")];
      const i = f.indexOf(document.activeElement);
      if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
    }
  }, true);
})();
