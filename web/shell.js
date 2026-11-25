(() => {
  // Board toggles. Listeners sit on the document because the server's regions are swapped in place.
  document.addEventListener("click", e => {
    const btn = e.target.closest(".bt[aria-controls]");
    if (!btn) return;
    const row = document.getElementById(btn.getAttribute("aria-controls"));
    const open = btn.getAttribute("aria-expanded") !== "true";
    btn.setAttribute("aria-expanded", String(open));
    row.hidden = !open;
    btn.closest("tbody").classList.toggle("open", open);
  });

  // Rail: mark the section in view
  const links = () => [...document.querySelectorAll(".nav a[data-target]")];
  const setCurrent = id => {
    const strip = document.querySelector(".nav ul");
    links().forEach(a => {
      if (a.dataset.target !== id) { a.removeAttribute("aria-current"); return; }
      if (a.getAttribute("aria-current") === "true") return;
      a.setAttribute("aria-current", "true");
      if (strip && strip.scrollWidth > strip.clientWidth) {
        const left = a.offsetLeft - (strip.clientWidth - a.offsetWidth) / 2;
        strip.scrollTo({ left, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      }
    });
  };
  const update = () => {
    if (document.getElementById("view-desk").hidden) { setCurrent(null); return; }
    const sections = links().map(a => document.getElementById(a.dataset.target)).filter(Boolean);
    if (!sections.length) return;
    const y = window.innerHeight * 0.25;
    let id = sections[0].id;
    for (const s of sections) if (s.getBoundingClientRect().top <= y) id = s.id;
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) id = sections[sections.length - 1].id;
    setCurrent(id);
  };
  let raf = 0;
  window.addEventListener("scroll", () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(update); }, { passive: true });
  window.addEventListener("resize", update);
  update();
})();

(() => {
  const tabs = [...document.querySelectorAll('.tabs [role="tab"]')];
  const show = (id, focus) => {
    tabs.forEach(t => {
      const on = t.id === id;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
      if (on && focus) t.focus();
    });
    window.dispatchEvent(new Event("scroll"));
  };
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => show(t.id));
    t.addEventListener("keydown", e => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      show(tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length].id, true);
    });
  });
  // Rail links and item numbers in the day plan go back to the desk
  document.addEventListener("click", e => {
    const a = e.target.closest('a[href^="#"]');
    if (!a || document.getElementById("view-desk").hidden === false) return;
    if (a.closest(".nav") || a.closest("#view-plan")) {
      show("tab-desk");
      const el = document.getElementById(a.getAttribute("href").slice(1));
      if (el) { e.preventDefault(); el.scrollIntoView({ block: "start" }); history.replaceState(null, "", a.getAttribute("href")); }
    }
  });
})();
