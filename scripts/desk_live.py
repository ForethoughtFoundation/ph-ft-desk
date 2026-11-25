#!/usr/bin/env python3
"""The desk as it is now: the last full run's desk.json brought up to date with the store's facts.

A full run writes the judged parts (items, each project's Now, Next and Risk). Everything that
needs no judgement comes from the collectors' snapshots here, every time the page asks: the
Linear boards, the calendar and what is due, hours, and which Slack to-dos and unassigned
tasks still stand.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desk_store as store  # noqa: E402

from desk_config import DESK, ME  # noqa: E402
DUE_SHOWN = 8  # dated issues listed beside a day; the rest are counted


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def all_items(desk: dict):
    for sec in desk.get("sections", []):
        for g in sec.get("groups") or []:
            yield from g["items"]


def issue_index(linear: dict) -> dict[str, dict]:
    """Every issue the Linear snapshot knows, by identifier."""
    out = dict(linear.get("refs") or {})
    for pool in (linear.get("mine") or [], linear.get("unassigned") or [], *(linear.get("boards") or {}).values()):
        for i in pool:
            out[i["identifier"]] = i
    return out


def closed(issue: dict) -> bool:
    return (issue.get("state") or {}).get("type") in ("completed", "canceled")


# ---------------------------------------------------------------- day plan
def fmt_day(d: dt.date) -> str:
    return d.strftime("%a %-d %b")


def day_plan(calendar: dict | None, linear: dict | None, run_plan: dict, now: dt.datetime) -> dict:
    """Today's and tomorrow's events and what is due, from the calendar and Linear snapshots.
    The full run's links from an event or due line to a desk item (`n`) are kept."""
    if calendar is None:
        return run_plan
    today, tomorrow = now.date(), now.date() + dt.timedelta(days=1)
    run_days = {d.get("label"): d for d in run_plan.get("days", [])}
    days = []
    for label, day in (("Today", today), ("Tomorrow", tomorrow)):
        run_day = run_days.get(label) if run_days.get(label, {}).get("date") == fmt_day(day) else {}
        linked = {e.get("start"): e.get("n") for e in (run_day or {}).get("events", []) if e.get("n")}
        events, due = [], []
        for e in calendar.get("events", []):
            if e["all_day"]:
                if e["start"] <= day.isoformat() < e["end"]:
                    due.append({"text": e["summary"], "status": "All day"})
                continue
            start, end = dt.datetime.fromisoformat(e["start"]), dt.datetime.fromisoformat(e["end"])
            if start.astimezone().date() != day:
                continue
            hm = start.astimezone().strftime("%H:%M")
            row = {"start": hm, "end": end.astimezone().strftime("%H:%M") if end.astimezone().date() == day else "23:59",
                   "label": e["summary"]}
            if e.get("response") == "needsAction":
                row.update(note="unanswered", warn=True)
            elif e.get("personal"):
                row["note"] = "personal"
            if linked.get(hm):
                row["n"] = linked[hm]
            events.append(row)
        # Desk items the run pinned to the day (they carry `n`, not an issue id) stay until the next run.
        # The run's Linear deadlines stand in until the collector has a Linear snapshot to replace them.
        due += [d for d in (run_day or {}).get("due", []) if (d.get("n") and not d.get("id")) or (linear is None and d.get("id"))]
        pinned = set(re.findall(r"\b[A-Z]{1,10}-\d+\b", " ".join(d.get("text") or "" for d in due)))  # whole ids: AS-1 isn't AS-12
        dated = []
        for i in sorted((linear or {}).get("mine") or [], key=lambda i: i.get("dueDate") or "", reverse=True):
            d = i.get("dueDate")
            if not d or closed(i) or (i.get("state") or {}).get("name") == "Waiting" or i["identifier"] in pinned:
                continue
            dd = dt.date.fromisoformat(d)
            if day == today and dd <= today:
                late = (today - dd).days
                dated.append({"id": i["identifier"], "text": i["title"], "warn": True,
                              "status": "Due today" if late == 0 else f"Overdue {late} day{'s' if late != 1 else ''}"})
            elif day == tomorrow and dd == tomorrow:
                dated.append({"id": i["identifier"], "text": i["title"], "status": "Due tomorrow"})
        due += dated[:DUE_SHOWN]
        if len(dated) > DUE_SHOWN:
            due.append({"text": f"{len(dated) - DUE_SHOWN} more overdue", "more": True})
        entry = {"label": label, "date": fmt_day(day), "events": events, "due": due}
        if day == today:
            entry["now"] = now.strftime("%H:%M")
        days.append(entry)
    later = []
    for i in sorted((linear or {}).get("mine") or [], key=lambda i: i.get("dueDate") or ""):
        d = i.get("dueDate")
        if d and not closed(i) and dt.date.fromisoformat(d) > tomorrow:
            later.append({"when": fmt_day(dt.date.fromisoformat(d)), "id": i["identifier"], "text": i["title"]})
    if linear is None:
        later = run_plan.get("later", [])
    return {"days": days, "later": later}


# ---------------------------------------------------------------- sections kept current
def todo_pill(t: dict, now: dt.datetime) -> dict | None:
    if t.get("snoozed_until"):
        return {"kind": "later", "text": "Snoozed until " + dt.datetime.fromisoformat(t["snoozed_until"]).strftime("%a %-d %b %H:%M")}
    if not t.get("due"):
        return None
    due = dt.datetime.fromisoformat(t["due"])
    days = (now.date() - due.date()).days
    if days > 0:
        return {"kind": "over", "text": f"{days} day{'s' if days != 1 else ''} overdue"}
    if days == 0:
        return {"kind": "over" if due < now else "today", "text": "Overdue" if due < now else "Due today"}
    return {"kind": "later", "text": "Due " + due.strftime("%a %-d %b")}


def reconcile(desk: dict, todos: dict | None, linear: dict | None, now: dt.datetime) -> None:
    """Drop Slack to-dos Peter has ticked and unassigned tasks that have found an owner; add any new ones
    in a plain form, numbered after the run's items; refresh how overdue each to-do is."""
    next_n = max([it.get("n") or 0 for it in all_items(desk)] or [0]) + 1
    secs = {s["id"]: s for s in desk.get("sections", [])}

    def section(sid, title, after, nav=None):
        if sid not in secs:
            sec = {"id": sid, "title": title, "groups": [{"items": []}]}
            if nav:
                sec["nav"] = nav
            order = [s["id"] for s in desk["sections"]]
            pos = max([order.index(a) + 1 for a in after if a in order] or [len(order)])
            desk["sections"].insert(pos, sec)
            secs[sid] = sec
        return secs[sid]

    if todos is not None:
        live = {t["key"]: t for t in todos.get("items", [])}
        sec = section("slack-todos", "Slack to-dos", ["urgent"]) if live or "slack-todos" in secs else None
        if sec:
            # Slack to-dos sit straight after Urgent, above New, whatever order the run wrote.
            desk["sections"].remove(sec)
            order = [x["id"] for x in desk["sections"]]
            desk["sections"].insert(order.index("urgent") + 1 if "urgent" in order else 0, sec)
            items = [it for g in sec["groups"] for it in g["items"] if it.get("key") in live]
            for it in items:
                it.pop("pill", None)
                pill = todo_pill(live[it["key"]], now)
                if pill:
                    it["pill"] = pill
            have = {it["key"] for it in items}
            for key, t in live.items():
                if key in have:
                    continue
                who = "You" if t.get("is_from_me") else (t.get("author") or "Someone")
                item = {"n": next_n, "key": key, "source": "slack", "source_label": "Slack to-do",
                        "head": f"{who}, {t['conversation']}:", "link": t.get("link"), "text": (t.get("text") or "")[:240],
                        "context": f"A message on Peter's Slack To-dos list, in {t['conversation']}. Read the thread at the link "
                                   "to see what it needs. The desk's Done, Due and Snooze buttons change it on Peter's Slack To-dos list."}
                pill = todo_pill(t, now)
                if pill:
                    item["pill"] = pill
                items.append(item)
                next_n += 1
            for it in items:   # the row's current due date and snooze, for its Due and Snooze menus
                t = live[it["key"]]
                it["due"], it["snoozed_until"] = t.get("due"), t.get("snoozed_until")
            awake = [it for it in items if not it.get("snoozed_until")]
            asleep = [it for it in items if it.get("snoozed_until")]
            sec["groups"] = [{"items": awake}] + ([{"label": "Snoozed", "items": asleep}] if asleep else [])
    if linear is not None:
        # The run lists unassigned tasks that are past the backlog; the same rule here.
        live = {f"linear-unassigned:{i['id']}": i for i in linear.get("unassigned") or []
                if (i.get("state") or {}).get("type") != "backlog"}
        sec = section("unassigned", "Unassigned tasks", ["urgent", "new", "slack-todos"], "Unassigned") if live or "unassigned" in secs else None
        if sec:
            items = [it for g in sec["groups"] for it in g["items"] if it.get("key") in live]
            have = {it["key"] for it in items}
            for key, i in live.items():
                if key in have:
                    continue
                project = (i.get("project") or {}).get("name") or "No project"
                due = f"due {fmt_day(dt.date.fromisoformat(i['dueDate']))}" if i.get("dueDate") else "no due date"
                items.append({"n": next_n, "key": key, "source": "linear", "source_label": project,
                              "head": f"\"{i['title']}\" ({i['identifier']}):", "link": i["url"],
                              "text": f"{project}, {due}. Needs an owner, joint work or reject.",
                              "context": f"Linear issue {i['identifier']} has no assignee. Read it through the task-manager skill, "
                                         "then propose an owner (Peter or Sam), joint work, or rejecting it."})
                next_n += 1
            sec["groups"] = [{"items": items}]
    desk["sections"] = [s for s in desk["sections"] if any(g["items"] for g in s.get("groups") or [])]


def add_urgent_arrivals(desk: dict) -> None:
    """A VIP's message the judge calls urgent, unanswered for five minutes since the last run, joins the end of Urgent."""
    try:
        import desk_arrivals as arrivals
        hidden = set(load(DESK / "archived.json", []))
        hidden |= {k for k, v in (load(DESK.parent / "state.json", {}).get("items") or {}).items() if v.get("status") in ("done", "snoozed")}
        next_n = max([it.get("n") or 0 for it in all_items(desk)] or [0]) + 1
        items = arrivals.urgent_items(arrivals.held(), next_n, hidden)
    except Exception as e:  # the desk still renders if the arrivals can't be read
        print(f"urgent arrivals unavailable: {e}", file=sys.stderr, flush=True)
        return
    if not items:
        return
    sec = next((s for s in desk["sections"] if s["id"] == "urgent"), None)
    if not sec:
        sec = {"id": "urgent", "title": "Urgent", "groups": [{"items": []}]}
        desk["sections"].insert(0, sec)
    sec["groups"][-1]["items"] += items
    counts = desk["header"].setdefault("counts", {})
    counts["urgent"] = (counts.get("urgent") or 0) + len(items)


def add_urgency_review(desk: dict) -> None:
    """The weekly review of Peter's not-urgent marks, as an item under New when it is due."""
    try:
        import desk_arrivals as arrivals
        hidden = set(load(DESK / "archived.json", []))
        item = arrivals.review_item(max([it.get("n") or 0 for it in all_items(desk)] or [0]) + 1)
    except Exception as e:
        print(f"urgency review unavailable: {e}", file=sys.stderr, flush=True)
        return
    if not item or item["key"] in hidden:
        return
    sec = next((s for s in desk["sections"] if s["id"] == "new"), None)
    if not sec:
        sec = {"id": "new", "title": "New", "groups": [{"items": []}]}
        order = [s["id"] for s in desk["sections"]]
        desk["sections"].insert(order.index("urgent") + 1 if "urgent" in order else 0, sec)
    sec["groups"][-1]["items"].append(item)
    counts = desk["header"].setdefault("counts", {})
    counts["new"] = (counts.get("new") or 0) + 1


# ---------------------------------------------------------------- the whole desk
def move_kept(desk: dict) -> None:
    """Let-go items Peter has kept (made open items) move to Still open until the next run lists them there."""
    open_keys = {k for k, v in (load(DESK.parent / "state.json", {}).get("items") or {}).items() if v.get("status") == "open"}
    let_go = next((s for s in desk["sections"] if s.get("id") == "let-go"), None)
    if not let_go or not open_keys:
        return
    kept = [it for g in let_go["groups"] for it in g["items"] if it.get("key") in open_keys]
    if not kept:
        return
    for g in let_go["groups"]:
        g["items"] = [it for it in g["items"] if it.get("key") not in open_keys]
    still = next((s for s in desk["sections"] if s.get("id") == "still-open"), None)
    if not still:
        still = {"id": "still-open", "title": "Still open", "groups": [{"items": []}]}
        at = next((i for i, s in enumerate(desk["sections"]) if s.get("id") in ("archive", "declined", "let-go")), len(desk["sections"]))
        desk["sections"].insert(at, still)
    still["groups"][-1]["items"] = kept + still["groups"][-1]["items"]
    desk["sections"] = [s for s in desk["sections"] if any(g["items"] for g in s.get("groups") or [])]


def live_desk(now: dt.datetime | None = None) -> dict:
    """desk.json plus the facts. `_boards`, `_issues` and `_live` are for the renderer and the server."""
    now = now or dt.datetime.now().astimezone()
    desk = copy.deepcopy(load(DESK / "desk.json", {}))
    desk.setdefault("header", {"title": "No desk run yet", "since": ""})
    desk.setdefault("sections", [])
    desk.setdefault("projects", [])
    linear, calendar = store.snapshot("linear"), store.snapshot("calendar")
    hours, todos = store.snapshot("hours"), store.snapshot("slack_todos")
    desk["date"] = now.date().isoformat()
    desk["day_plan"] = day_plan(calendar, linear, desk.get("day_plan") or {}, now)
    if hours:
        desk["hours"] = hours
    reconcile(desk, todos, linear, now)
    add_urgent_arrivals(desk)
    add_urgency_review(desk)
    move_kept(desk)
    desk["_boards"] = (linear or {}).get("boards")
    desk["_issues"] = issue_index(linear) if linear else {}
    desk["week_plan"] = store.snapshot("week_plan")
    desk["_live"] = {"linear": linear is not None, "calendar": calendar is not None}
    return desk


def item_index(desk: dict) -> dict[str, dict]:
    out = {}
    for sec in desk.get("sections", []):
        for g in sec.get("groups") or []:
            for it in g["items"]:
                out[it.get("key") or f"n{it['n']}"] = {**it, "section": sec["title"]}
    return out
