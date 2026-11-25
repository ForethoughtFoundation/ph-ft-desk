#!/usr/bin/env python3
"""The shadow page (http://127.0.0.1:4848/shadow): the judge's verdicts beside the last full run's.

Until the cutover the judge only watches. This page lists, for every conversation both have
seen, where they disagree about whether it needs Peter, and takes his verdict on who was
right. Those marks are the evidence for switching the desk over to the judge.

    desk_shadow.py            print the comparison as JSON
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desk_arrivals as arrivals  # noqa: E402
import desk_live as live  # noqa: E402
import desk_store as store  # noqa: E402

WEB = HERE.parent / "web"
MARKS = {"judge": "Judge right", "run": "Run right", "neither": "Both wrong"}


def run_id() -> str:
    return (live.load(live.DESK / "desk.json", {}).get("header") or {}).get("title") or "no run"


def unit_of(key: str) -> str | None:
    """The conversation a run item's key belongs to."""
    row = store.db().execute("SELECT unit FROM messages WHERE key=?", (key,)).fetchone()
    if row:
        return row["unit"]
    parts = key.split(":")
    if parts[0] == "gmail" and len(parts) >= 2:
        return f"gmail:{parts[1]}"
    if parts[0] in ("gdoc", "linear", "signal", "whatsapp") and len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    if parts[0] == "slack" and len(parts) >= 3:
        return ":".join(parts[:3])
    return None


def run_view() -> dict[str, dict]:
    """What the last run said about each conversation it listed: urgent, new, open or noise."""
    desk = live.load(live.DESK / "desk.json", {})
    rank = {"urgent": 3, "new": 2, "still-open": 2, "archive": 0}
    out: dict[str, dict] = {}
    for sec in desk.get("sections", []):
        if sec["id"] not in rank:
            continue
        for g in sec.get("groups") or []:
            for it in g["items"]:
                unit = unit_of(it.get("key") or "")
                if unit and rank[sec["id"]] >= rank.get((out.get(unit) or {}).get("section"), -1):
                    out[unit] = {"section": sec["id"], "n": it.get("n"), "text": ((it.get("head") or "") + " " + (it.get("text") or "")).strip()}
    return out


def compare() -> dict:
    wms = arrivals.run_watermarks()
    run, rid = run_view(), run_id()
    marks = {r["unit"]: dict(r) for r in store.db().execute("SELECT * FROM marks WHERE run=?", (rid,))}
    rows = store.db().execute(
        "SELECT u.*, j.verdict, j.revision AS jrev, j.finished FROM units u JOIN judgements j ON j.unit = u.id "
        "WHERE j.status = 'done' AND j.revision = (SELECT max(revision) FROM judgements WHERE unit = u.id AND status = 'done') "
        "ORDER BY u.last_other_ts DESC").fetchall()
    disagree, waiting, agree = [], [], []
    for r in rows:
        v = json.loads(r["verdict"])
        seen_by_run = (r["last_other_ts"] or 0) <= arrivals.unit_wm(r["id"], wms)
        rv = run.get(r["id"])
        row = {"unit": r["id"], "label": r["label"], "link": r["link"], "source": r["source"], "at": r["last_other_ts"],
               "judge": v, "current": r["jrev"] == r["revision"], "run": rv, "mark": marks.get(r["id"])}
        if not seen_by_run:
            waiting.append(row)
            continue
        run_urgent, run_listed = bool(rv) and rv["section"] == "urgent", bool(rv) and rv["section"] != "archive"
        kind = None
        if v["urgency"] == "urgent" and not run_urgent:
            kind = "Judge says urgent; the run didn't"
        elif run_urgent and v["urgency"] != "urgent":
            kind = "The run said urgent; the judge doesn't"
        elif v["needs_peter"] and not run_listed:
            kind = "Judge says it needs you; the run didn't list it"
        elif rv and rv["section"] == "still-open" and not v["needs_peter"]:
            kind = "The run kept it open; the judge says nothing is needed"
        if kind:
            disagree.append({**row, "kind": kind})
        else:
            agree.append(row)
    judge = store.source("judge")
    return {"run": rid, "disagree": disagree, "waiting": waiting, "agree": agree,
            "judge": {"detail": judge.get("detail") or "not run yet", "last_ok": judge.get("last_ok"), "complete": bool(judge.get("complete", 1))}}


def mark(unit: str, verdict: str, note: str) -> dict:
    if verdict == "clear":
        store.db().execute("DELETE FROM marks WHERE unit=? AND run=?", (unit, run_id()))
        return {"ok": True}
    if verdict not in MARKS or not store.db().execute("SELECT 1 FROM units WHERE id=?", (unit,)).fetchone():
        raise ValueError("bad mark")
    store.db().execute("INSERT OR REPLACE INTO marks(unit, run, verdict, note, at) VALUES(?,?,?,?,?)", (unit, run_id(), verdict, note, time.time()))
    return {"ok": True}


def page(token: str) -> str:
    import render_desk
    E = render_desk.E
    c = compare()

    def when(ts):
        d = dt.datetime.fromtimestamp(ts or 0)
        return d.strftime("%H:%M") if d.date() == dt.date.today() else d.strftime("%a %-d %b %H:%M")

    def card(r, markable):
        v, rv = r["judge"], r["run"]
        label = E(r["label"])
        title = f'<a href="{E(render_desk.safe_url(r["link"]))}" target="_blank" rel="noopener">{label}</a>' if r.get("link") else label
        asks = "".join(f'<li>{E(a["who"])}: {E(a["what"])}{" (" + E(a["deadline"]) + ")" if a.get("deadline") else ""}</li>' for a in v.get("asks") or [])
        run_html = (f'<p><span class="sh-tag t-{E(rv["section"])}">{E({"still-open": "open", "archive": "noise"}.get(rv["section"], rv["section"]))}</span> '
                    f'#{E(rv["n"])} {E(rv["text"])}</p>') if rv else '<p class="sh-none">Not listed</p>'
        m = (r.get("mark") or {}).get("verdict")
        buttons = "".join(f'<button type="button" class="btn sm{" on" if m == k else ""}" data-mark="{k}" aria-pressed="{str(m == k).lower()}">{E(t)}</button>'
                          for k, t in MARKS.items()) if markable else ""
        return (f'<li class="sh-card" data-unit="{E(r["unit"])}"><header><h3>{title}</h3><time>{E(when(r["at"]))}</time></header>'
                + (f'<p class="sh-kind">{E(r["kind"])}</p>' if r.get("kind") else "")
                + f'<div class="sh-cols"><section><h4>Judge{"" if r["current"] else " (before the latest message)"}</h4>'
                  f'<p><span class="sh-tag t-{E(v["urgency"])}">{E(v["urgency"])}</span> <b>{E(v["head"])}</b> {E(v["summary"])}</p>'
                + (f'<ul>{asks}</ul>' if asks else "") + f'<p class="sh-why">{E(v["reason"])}</p></section>'
                  f'<section><h4>Run</h4>{run_html}</section></div>'
                + (f'<div class="sh-marks">{buttons}</div>' if buttons else "") + "</li>")

    def block(title, rows, markable=False, fold=False):
        if not rows:
            return ""
        body = f'<ol class="sh-list">{"".join(card(r, markable) for r in rows)}</ol>'
        head = f'{E(title)} <span class="sec-n">{len(rows)}</span>'
        chev = '<svg class="sh-chev" viewBox="0 0 12 12" aria-hidden="true"><path d="M4.5 3l3 3-3 3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        return (f'<details class="sh-sec"><summary><h2 class="sec-h">{chev}{head}</h2></summary>{body}</details>' if fold
                else f'<section class="sh-sec"><h2 class="sec-h">{head}</h2>{body}</section>')

    marked = sum(1 for r in c["disagree"] if r.get("mark"))
    j = c["judge"]
    import desk_judge
    sp = desk_judge.spend()
    status = (E(j["detail"]) + (f' · last ran {E(when(j["last_ok"]))}' if j.get("last_ok") else "")
              + f' · ${sp["today"]:.2f} today, ${sp["week"]:.2f} in 7 days')
    empty = "" if (c["disagree"] or c["waiting"] or c["agree"]) else '<p class="sh-none">No verdicts yet.</p>'
    css = (WEB / "desk.css").read_text()
    return f'''<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="desk-token" content="{E(token)}"><title>Shadow judge</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500&family=Inter:wght@400;500;600&display=swap">
<style>{css}</style></head><body class="shadow">
<main class="sh-main">
  <header class="top"><div><p class="eyebrow">Forethought desk</p><h1>Shadow judge</h1>
  <p class="since">Against the run of {E(c["run"])} · {marked} of {len(c["disagree"])} marked · <a href="/">Desk</a></p>
  <p class="sh-status live-{"ok" if j["complete"] else "stale"}"><span class="live-dot" aria-hidden="true"></span>{status}</p></div></header>
  {empty}{block("Disagreements", c["disagree"], markable=True)}{block("Not yet in a run", c["waiting"])}{block("Agreed", c["agree"], fold=True)}
</main>
<div class="toast" id="toast" role="status" hidden></div>
<script>
(() => {{
  const TOKEN = document.querySelector('meta[name="desk-token"]').content;
  const toast = m => {{ const t = document.getElementById("toast"); t.textContent = m; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 3000); }};
  document.addEventListener("click", async e => {{
    const b = e.target.closest("[data-mark]");
    if (!b) return;
    const card = b.closest(".sh-card"), was = b.classList.contains("on");
    try {{
      const r = await fetch("/api/mark", {{ method: "POST", headers: {{ "Content-Type": "application/json", "X-Desk-Token": TOKEN }},
        body: JSON.stringify({{ unit: card.dataset.unit, verdict: was ? "clear" : b.dataset.mark }}) }});
      if (!r.ok) throw new Error((await r.json()).error || r.statusText);
      card.querySelectorAll("[data-mark]").forEach(x => {{ const on = x === b && !was; x.classList.toggle("on", on); x.setAttribute("aria-pressed", String(on)); }});
    }} catch (err) {{ toast("Couldn't save: " + err.message); }}
  }});
}})();
</script></body></html>'''


if __name__ == "__main__":
    print(json.dumps(compare(), indent=1, ensure_ascii=False, default=str))
