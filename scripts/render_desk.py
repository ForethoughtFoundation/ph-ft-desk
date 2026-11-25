#!/usr/bin/env python3
"""Render the desk page from desk.json.

    render_desk.py [DESK_JSON] [--out DIR] [--no-linear]

The agent writes desk.json (schema in ../references/desk-json.md); this script adds each
project's Linear board and writes index.html into the output folder, which desk_server.py
serves. It never sends anything anywhere.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import importlib.machinery
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
sys.path.insert(0, str(HERE))
from desk_config import FIRST, ME, STATE_DIR as STATE, path as cfg_path  # noqa: E402
import desk_projects  # noqa: E402

LINEAR_SCRIPT = cfg_path("linear_tasks")
WORKSPACE = "forethought"

E = lambda s: html.escape(str(s or ""), quote=True)

# ---------------------------------------------------------------- text markup
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
ISSUE_RE = re.compile(r"(?<![\w/-])(AS-\d+)(?![\w-])")
# Live issue states by identifier, set by regions(); a closed issue's reference renders struck.
ISSUES: dict[str, dict] = {}


def issue_closed(ident: str) -> bool:
    return ((ISSUES.get(ident) or {}).get("state") or {}).get("type") in ("completed", "canceled")



def load_linear_tasks():
    """Load the task-manager's extensionless linear-tasks script as a module (load_module() is gone in Python 3.15)."""
    loader = importlib.machinery.SourceFileLoader("linear_tasks", str(LINEAR_SCRIPT))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("linear_tasks", loader))
    loader.exec_module(mod)
    return mod

def safe_url(url: str) -> str:
    return url if re.match(r"^(https?://|linear://|mailto:|#)", url) else "#"


def linear_app(url: str) -> str:
    """Open Linear links in the desktop app."""
    return re.sub(r"^https://linear\.app/", "linear://", url)


def md(text: str) -> str:
    """Escape, then allow [text](url), **bold** and bare AS-123 issue ids."""
    out, pos = [], 0
    for m in LINK_RE.finditer(text or ""):
        out.append(_inline(text[pos:m.start()]))
        out.append(f'<a href="{E(linear_app(safe_url(m.group(2))))}">{_inline(m.group(1), ids=False)}</a>')
        pos = m.end()
    out.append(_inline((text or "")[pos:]))
    return "".join(out)


def md_live(text: str) -> str:
    """md(), with each semicolon-separated phrase struck once every issue it names is closed."""
    parts = re.split(r"(;\s+)", text or "")
    out = []
    for part in parts:
        refs = ISSUE_RE.findall(part)
        if refs and not part.startswith(";") and all(issue_closed(r) for r in refs):
            out.append(f'<s class="gone">{md(part)}</s>')
        else:
            out.append(md(part))
    return "".join(out)


def _inline(s: str, ids: bool = True) -> str:
    s = E(s)
    s = BOLD_RE.sub(r"<strong>\1</strong>", s)
    if ids:
        s = ISSUE_RE.sub(
            lambda m: f'<a class="iss{" iss-closed" if issue_closed(m.group(1)) else ""}" href="linear://{WORKSPACE}/issue/{m.group(1)}">{m.group(1)}</a>', s
        )
    return s


# ---------------------------------------------------------------- icons
STATE_ICONS = {
    "triage": '<svg class="st" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="6" fill="var(--st-triage)"/><path d="M4 5.6h6M8.4 4l1.6 1.6M10 8.4H4M5.6 10L4 8.4" stroke="var(--surface)" stroke-width="1.3" fill="none" stroke-linecap="round"/></svg>',
    "backlog": '<svg class="st" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5.6" fill="none" stroke="var(--st-backlog)" stroke-width="1.5" stroke-dasharray="1.6 1.6"/></svg>',
    "unstarted": '<svg class="st" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5.6" fill="none" stroke="var(--st-todo)" stroke-width="1.5"/></svg>',
    "started": '<svg class="st" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5.6" fill="none" stroke="var(--st-progress)" stroke-width="1.5"/><path d="M7 3.5A3.5 3.5 0 0 1 7 10.5Z" fill="var(--st-progress)"/></svg>',
    "done": '<svg class="st" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="6" fill="var(--st-done)"/><path d="M4.4 7.2l1.8 1.8 3.5-3.7" fill="none" stroke="var(--surface)" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "waiting": '<svg class="st" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5.6" fill="none" stroke="var(--st-progress)" stroke-width="1.5"/><path d="M7 4v3.2l2 1.3" fill="none" stroke="var(--st-progress)" stroke-width="1.4" stroke-linecap="round"/></svg>',
}
PRIO_ICONS = {
    "No priority": '<svg class="pr" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 8h2.4M6.8 8h2.4M11.6 8H14" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    "Low": '<svg class="pr" viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="9" width="3" height="5" rx="1" fill="currentColor"/><rect x="6" y="6" width="3" height="8" rx="1" fill="currentColor" opacity=".3"/><rect x="11" y="3" width="3" height="11" rx="1" fill="currentColor" opacity=".3"/></svg>',
    "Medium": '<svg class="pr" viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="9" width="3" height="5" rx="1" fill="currentColor"/><rect x="6" y="6" width="3" height="8" rx="1" fill="currentColor"/><rect x="11" y="3" width="3" height="11" rx="1" fill="currentColor" opacity=".3"/></svg>',
    "High": '<svg class="pr" viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="9" width="3" height="5" rx="1" fill="currentColor"/><rect x="6" y="6" width="3" height="8" rx="1" fill="currentColor"/><rect x="11" y="3" width="3" height="11" rx="1" fill="currentColor"/></svg>',
    "Urgent": '<svg class="pr" viewBox="0 0 16 16" aria-hidden="true"><rect x="1.5" y="1.5" width="13" height="13" rx="3" fill="var(--accent)"/><path d="M8 4.5v4.2M8 11v.3" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>',
}
CAL_ICON = '<svg viewBox="0 0 12 12" aria-hidden="true"><rect x="1.5" y="2.5" width="9" height="8" rx="1.5" fill="none" stroke="currentColor"/><path d="M1.5 5h9M4 1.2v2.4M8 1.2v2.4" stroke="currentColor"/></svg>'
BOARD_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1.5" y="2" width="3.4" height="12" rx="1" fill="currentColor"/><rect x="6.3" y="2" width="3.4" height="8" rx="1" fill="currentColor" opacity=".7"/><rect x="11.1" y="2" width="3.4" height="5" rx="1" fill="currentColor" opacity=".45"/></svg>'
CHEV = '<svg class="chev" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5l3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'

# Board columns, in Linear's order; done and cancelled states are left off.
COLUMNS = [("Triage", "triage", "#FC7840"), ("Backlog", "backlog", "#bec2c8"), ("Todo", "unstarted", "#e2e2e2"),
           ("In Progress", "started", "#f2c94c"), ("Waiting", "waiting", "#f2c94c"),
           ("Done", "done", "#5e6ad2")]
SOURCE_TAGS = {"email": ("t-email", "Email"), "slack": ("t-slack", "Slack"), "docs": ("t-docs", "Docs"),
               "linear": ("t-linear", "Linear"), "cal": ("t-cal", "Calendar"), "calendar": ("t-cal", "Calendar"),
               "desk": ("t-desk", "Desk"), "signal": ("t-slack", "Signal"), "whatsapp": ("t-docs", "WhatsApp"),
               "repos": ("t-linear", "Repos")}


# ---------------------------------------------------------------- Linear
def fetch_boards(project_ids: list[str]) -> dict[str, list[dict]]:
    if not project_ids:
        return {}
    lt = load_linear_tasks()
    boards = {}
    for pid in project_ids:
        q = ("""query($id: String!) { project(id: $id) { issues(first: 150, filter: {state: {type: {nin: ["completed", "canceled"]}}}) {
              nodes { """ + ISSUE_FIELDS + " } } } }")
        try:
            data = lt.gql(q, {"id": pid})
        except TypeError:
            data = lt.gql(q.replace("$id: String!", "").replace("$id", json.dumps(pid)).replace("query()", "query"))
        boards[pid] = data["project"]["issues"]["nodes"]
    return boards


ISSUE_FIELDS = """identifier title url dueDate priorityLabel description state { name type } assignee { name displayName }
              labels { nodes { name } } comments(first: 3) { nodes { body createdAt user { name } } }"""


def fetch_issues(identifiers: set[str]) -> list[dict]:
    """Issues linked from the desk's text but on no board, so their links can open the peek panel too."""
    nums = sorted({int(i.split("-")[1]) for i in identifiers if i.startswith("AS-")})
    if not nums:
        return []
    lt = load_linear_tasks()
    q = ("""query($n: [Float!]) { issues(first: 250, filter: {team: {key: {eq: "AS"}}, number: {in: $n}}) { nodes { """
         + ISSUE_FIELDS + " project { name } } } }")
    data = lt.gql(q, {"n": nums}, soft=True) or {}
    return data.get("issues", {}).get("nodes", [])


def label_linear_projects(desk: dict) -> None:
    """Label Linear items with their issue's project name instead of plain "Linear"."""
    items = [it for sec in desk.get("sections", []) for g in sec.get("groups", []) for it in g["items"]
             if (it.get("source") or "").lower() == "linear" and it.get("source_label", "Linear") == "Linear"]
    uuid_of = {}
    for it in items:
        m = re.match(r"linear:([0-9a-f-]{36})", it.get("key") or "")
        if m:
            uuid_of[id(it)] = m.group(1)
    if not uuid_of:
        return
    lt = load_linear_tasks()
    q = """query($ids: [ID!]) { issues(first: 100, filter: {id: {in: $ids}}) { nodes { id project { name } } } }"""
    data = lt.gql(q, {"ids": sorted(set(uuid_of.values()))}, soft=True) or {}
    names = {n["id"]: (n.get("project") or {}).get("name") for n in data.get("issues", {}).get("nodes", [])}
    for it in items:
        name = names.get(uuid_of.get(id(it)))
        if name:
            it["source_label"] = name


def initials(name: str) -> str:
    parts = re.split(r"[\s.]+", name.strip())
    return "".join(p[0] for p in parts[:2]).upper()


def due_chip(due: str | None, today: str) -> str:
    if not due:
        return ""
    import datetime as dt
    d = dt.date.fromisoformat(due)
    t = dt.date.fromisoformat(today)
    cls = "due-over" if d < t else "due-today" if d == t else ""
    label = "Today" if d == t else d.strftime("%a %-d %b")
    return f'<span class="due {cls}">{CAL_ICON}{E(label)}</span>'


def column_of(issue: dict) -> str | None:
    name, typ = issue["state"]["name"], issue["state"]["type"]
    if name == "Waiting":
        return "Waiting"
    return {"triage": "Triage", "backlog": "Backlog", "unstarted": "Todo", "started": "In Progress", "completed": "Done"}.get(typ)


PLUS_ICON = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2v8M2 6h8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>'


def others_card(issue: dict) -> bool:
    """Assigned to someone other than Peter. Unassigned cards still need an owner, so they stay prominent."""
    who = issue.get("assignee")
    return bool(who) and (who.get("name") or who.get("displayName")) != ME


def render_board(name: str, issues: list[dict], today: str, idx: int) -> str:
    cols = []
    for col, icon, colour in COLUMNS:
        # Cards someone else owns sink to the bottom and render faded (.card-other).
        cards = sorted((i for i in issues if column_of(i) == col), key=others_card)
        # Empty columns other than Todo and In Progress show only while a card is dragged.
        spare = not cards and col not in ("Todo", "In Progress")
        lis = []
        for i in cards:
            who = i.get("assignee")
            av = (f'<span class="av" title="{E(who.get("name") or who.get("displayName"))}">{E(initials(who.get("name") or who.get("displayName")))}</span>'
                  if who else '<span class="av av-none" title="Unassigned"><span class="sr">Unassigned</span></span>')
            prio = i.get("priorityLabel") or "No priority"
            lis.append(
                f'<li{" class=\"card-other\"" if others_card(i) else ""}><a class="card" href="{E(linear_app(i["url"]))}"><span class="card-top"><span class="card-id">{E(i["identifier"])}</span>{av}</span>'
                f'<span class="card-title">{E(i["title"])}</span><span class="card-meta"><span class="prio" title="Priority: {E(prio)}">'
                f'{PRIO_ICONS.get(prio, PRIO_ICONS["No priority"])}<span class="sr">Priority: {E(prio)}</span></span>{due_chip(i.get("dueDate"), today)}</span></a>'
                f'<button class="rej-btn" type="button" data-tip="Reject|⌫"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.3"/><path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4"/></svg><span class="sr">Reject {E(i["identifier"])}</span></button>'
                f'<button class="done-btn" type="button" data-tip="Mark done|E"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.3"/><path d="M5.2 8.2l1.9 1.9 3.8-4"/></svg><span class="sr">Mark {E(i["identifier"])} done</span></button></li>'
            )
        body = f'<ul class="cards">{"".join(lis)}</ul>'
        cls = "col" + (" empty" if not lis else "") + (" spare" if spare else "")
        add = ("" if col == "Done" else f'<button class="col-add" type="button" data-tip="Add a task|⇧A">{PLUS_ICON}'
                                           f'<span class="sr">Add a task to {col}</span></button>')
        cols.append(f'<section class="{cls}" data-col="{col}" style="--c:{colour}"><header class="col-h">{STATE_ICONS[icon]}'
                    f'<span class="col-name">{col}</span><span class="col-n">{len(cards)}</span>{add}</header>{body}</section>')
    return f'<tr class="b-row" id="board-{idx}" hidden><td colspan="6" data-no-edit><div class="board" role="region" aria-label="{E(name)} board" tabindex="0">{"".join(cols)}</div></td></tr>'


# ---------------------------------------------------------------- sections
ROLE_ORDER = ["Engineering", "Communications", "Other"]


def project_structure() -> dict:
    """Areas and boards from desk_projects, or the desk run's own rows, ungrouped, if that can't be built."""
    try:
        return desk_projects.structure()
    except Exception:
        return {"areas": [{"slug": "", "name": "Projects", "role": "Other", "owner": ""}],
                "boards": [{**b, "area": ""} for b in desk_projects.boards()]}


RESP_ICON = '<svg class="p-resp" viewBox="0 0 16 16" aria-hidden="true"><path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v2.4h-2.4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'


def project_row(p: dict, boards: dict, today: str, idx: int, fold: str | None = None, area: str = "") -> str:
    """One board as one tbody: a compact row (name, one-line Next, overdue count, board button) and its hidden
    board. Now, Next and Risk show in full when the board is open (desk.css lays the row out as a grid)."""
    issues = boards.get(p.get("id"), [])
    open_issues = [i for i in issues if column_of(i) not in (None, "Done")]
    n_open = len(open_issues)
    overdue = sum(1 for i in open_issues if i.get("dueDate") and i["dueDate"] < today)
    link = linear_app(p.get("url") or p.get("link") or "")
    label = ("↳ " if p.get("parent") else "") + p["name"]
    resp = f'{RESP_ICON}<span class="sr">Responsibility: </span>' if p.get("kind") == "responsibility" else ""
    name = f'{resp}<span class="p-title">{E(label)}</span>'  # a click anywhere on the row shows the board; Linear is in the row's menu
    if p.get("id"):  # every Linear project gets a board, empty or not, so tasks can be added to it
        bt = (f'<button class="bt" type="button" aria-expanded="false" aria-controls="board-{idx}" data-tip="Show board|↵">{BOARD_ICON}'
              f'<span class="bt-n">{n_open}</span><span class="sr"> issues, show board for {E(p["name"])}</span>{CHEV}</button>')
    else:
        bt = '<span class="bt bt-empty" title="No open issues"><span class="bt-n">0</span></span>'
    row = (f'<tr class="p-row"><td class="p-name">{name}</td><td class="p-now" data-label="Now"><span>{md_live(p.get("now"))}</span></td>'
           f'<td class="p-next" data-label="Next"><span>{md_live(p.get("next"))}</span></td><td class="p-risk" data-label="Risk"><span>{md_live(p.get("risk"))}</span></td>'
           f'<td class="p-over" data-no-edit>{f"{overdue} overdue" if overdue else ""}</td>'
           f'<td class="p-bt" data-no-edit>{bt}</td></tr>')
    attrs = f' data-area="{E(area)}"' + (f' data-link="{E(link)}"' if link else "") + (f' data-fold="{E(fold)}" hidden' if fold else "")
    return f'<tbody class="proj" data-name="{E(p["name"])}"{attrs}>{row}{render_board(p["name"], issues, today, idx) if p.get("id") else ""}</tbody>'


def render_projects(structure: dict, boards: dict, today: str) -> str:
    """Every board, as one card per role. Each area is a row of the card: its name and owner on the left, its
    boards on the right, responsibilities first. Active boards that are Peter's (lead or member) show; per area,
    paused or not-started ones, other people's and snoozed ones fold into one line of show/hide buttons (desk.js
    remembers which are open, and adds the snoozed ones)."""
    areas, all_boards = structure.get("areas", []), structure.get("boards", [])
    roles = ROLE_ORDER + sorted({a.get("role") or "Other" for a in areas} - set(ROLE_ORDER))
    bodies, idx = [], 0
    for role in roles:
        role_areas = [a for a in areas if (a.get("role") or "Other") == role]
        blocks = []
        for a in role_areas:
            bs = [b for b in all_boards if b.get("area") == a["slug"]]
            if not bs:
                continue
            live = [b for b in bs if b.get("mine") and b.get("status") == "Active"]
            live = [b for b in live if b.get("kind") == "responsibility"] + [b for b in live if b.get("kind") != "responsibility"]
            later = [b for b in bs if b.get("mine") and b.get("status") != "Active"]
            others = [b for b in bs if not b.get("mine")]
            rows, prev = [], {"parent": None}

            def plabel(b, fold=None):
                # A parent is a Linear initiative, not a board, so it has no row: its name
                # heads its subprojects instead, hidden with them when they are folded.
                out = ""
                if b.get("parent") and b["parent"] != prev["parent"]:
                    out = (f'<tbody class="p-grp p-plabel" data-area="{E(a["slug"])}"{f' data-fold="{E(fold)}" hidden' if fold else ""} data-no-edit>'
                           f'<tr class="g-plabel"><td>{E(b["parent"])}</td></tr></tbody>')
                prev["parent"] = b.get("parent")
                return out
            for b in live:
                rows.append(plabel(b) + project_row(b, boards, today, idx, area=a["slug"])); idx += 1
            folds, hidden = [], []
            for status in ("Paused", "Not started"):
                g = [b for b in later if b.get("status") == status]
                if g:
                    key = f'{a["slug"]}:{status.lower().replace(" ", "-")}'
                    folds.append((key, f'{status}: {", ".join(b["name"] for b in g)}'))
                    hidden += [(key, b) for b in g]
            by_owner = {}
            for b in others:
                by_owner.setdefault(b.get("owner") or "Others", []).append(b)
            for owner, g in by_owner.items():
                key = f'{a["slug"]}:others-{owner.lower()}'
                folds.append((key, f'{owner}\'s: {", ".join(b["name"] for b in g)}'))
                hidden += [(key, b) for b in g]
            prev["parent"] = None
            for key, b in hidden:
                rows.append(plabel(b, key) + project_row(b, boards, today, idx, fold=key, area=a["slug"])); idx += 1
            # Every area gets the line, even with nothing folded: desk.js adds a "Snoozed" button to it when one
            # of the area's boards is snoozed, and hides the line while it has no buttons.
            btns = "".join(f'<button class="g-fold-btn{" g-others" if ":others-" in k else ""}" type="button" data-fold="{E(k)}" aria-expanded="false"><span>{E(t)}</span>{CHEV}</button>' for k, t in folds)
            rows.append(f'<tbody class="p-grp p-folds" data-area="{E(a["slug"])}" data-no-edit{"" if folds else " hidden"}><tr class="g-fold"><td>{btns}</td></tr></tbody>')
            blocks.append(f'<div class="area-blk"><div class="area-lbl" data-no-edit>{E(a["name"])}<span>{E(a.get("owner") or "")}</span></div>'
                          f'<table class="proj-tbl" aria-label="{E(a["name"])}">{"".join(rows)}</table></div>')
        if blocks:
            rid = "role-" + re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
            bodies.append(f'''<section class="role-card" aria-labelledby="{rid}"><h3 class="role-h" id="{rid}" data-no-edit><button class="role-toggle" type="button" aria-expanded="true" aria-controls="{rid}-tbl" data-role="{rid}">{CHEV}{E(role)}</button></h3>
      <div class="role-body" id="{rid}-tbl">{"".join(blocks)}</div></section>''')
    return f'''<section class="sec sec-projects" id="projects" aria-labelledby="projects-h">
      <h2 class="sec-h" id="projects-h">Projects &amp; responsibilities</h2>
      {"".join(bodies)}</section>'''


def render_item(it: dict) -> str:
    tag_cls, tag_label = SOURCE_TAGS.get((it.get("source") or "").lower(), ("t-none", ""))
    label = it.get("source_label") or tag_label
    href = E(linear_app(safe_url(it["link"]))) if it.get("link") else ""
    if label and href:  # the tag opens the item's thread, issue or email, as the head does
        tag = f'<a class="tag {tag_cls}" href="{href}" title="Open in {E(label)}"><span class="tag-l">{E(label)}</span></a>'
    elif label:
        tag = f'<span class="tag {tag_cls}" title="{E(label)}"><span class="tag-l">{E(label)}</span></span>'
    else:
        tag = '<span class="tag t-none" aria-hidden="true"></span>'
    head = it.get("head") or ""
    if head:
        head = (f'<a class="head" href="{href}">{_inline(head, ids=False)}</a>' if href
                else f'<strong class="head">{_inline(head, ids=False)}</strong>')
    meta = ""
    if it.get("pill"):
        meta = f'<span class="pill st-{E(it["pill"].get("kind", "later"))}">{E(it["pill"]["text"])}</span>'
    elif it.get("time"):
        meta = f'<span class="time">{E(it["time"])}</span>'
    meta = f'<span class="meta" data-no-edit>{meta}</span>' if meta else ""
    todo = "".join(f' data-{k.replace("_", "-")}="{E(it[k])}"' for k in ("due", "snoozed_until") if it.get(k))
    return (f'<li class="row" id="item-{E(it["n"])}" data-key="{E(it.get("key") or "n" + str(it["n"]))}"{todo}>'
            f'<span class="num" data-no-edit>{E(it["n"])}</span><span class="src" data-no-edit>{tag}</span>'
            f'<p class="text">{head} {md(it.get("text"))}</p>{meta}</li>')


def render_section(sec: dict) -> str:
    sid = sec["id"]
    groups = sec.get("groups") or [{"items": sec.get("items", [])}]
    n = sum(len(g["items"]) for g in groups)
    parts = [f'<section class="sec sec-{E(sid)}" id="{E(sid)}" aria-labelledby="{E(sid)}-h"><h2 class="sec-h" id="{E(sid)}-h">{E(sec["title"])} <span class="sec-n" data-no-edit>{n}</span></h2>']
    for g in groups:
        if g.get("label"):
            parts.append(f'<h3 class="grp-h">{E(g["label"])} <span class="grp-n" data-no-edit>{len(g["items"])}</span></h3>')
        # A source column that would say the same thing on every row is left out.
        labels = {i.get("source_label") or SOURCE_TAGS.get((i.get("source") or "").lower(), ("", ""))[1] for i in g["items"]}
        generic = {v[1] for v in SOURCE_TAGS.values()} | {"Slack to-do", g.get("label") or ""}
        one = len(labels) == 1 and labels <= generic
        parts.append(f'<ol class="rows{" one-src" if one else ""}">{"".join(render_item(i) for i in g["items"])}</ol>')
    if sec.get("foot") and sid != "archive":
        parts.append(f'<p class="sec-foot">{md(sec["foot"])}</p>')
    parts.append("</section>")
    return "".join(parts)


def render_status(desk: dict) -> str:
    h = desk.get("hours") or {}
    hours = ""
    if h:
        hours = (f'<h2>Hours</h2><div><p>{md(h.get("text"))}</p>'
                 f'<div class="hours-bar" data-no-edit aria-hidden="true"><span style="width: {float(h.get("pct", 0)):.1f}%"></span></div></div>')
    checked = "".join(f'<li class="{"chk-bad" if c.get("bad") else ""}">{md(c["text"])}</li>' for c in desk.get("checked", []))
    return f'<section class="status" aria-label="Hours and checks">{hours}<h2>Checked:</h2><ul class="checked">{checked}</ul></section>'


# ---------------------------------------------------------------- day plan
def hm(t: str) -> float:
    h, m = t.split(":")
    return int(h) + int(m) / 60


def pnum(ref: dict) -> str:
    if ref.get("n"):
        return f'<a class="pnum" href="#item-{E(ref["n"])}" data-no-edit>{E(ref["n"])}</a>'
    if ref.get("id"):
        return f'<a class="pnum id" href="linear://{WORKSPACE}/issue/{E(ref["id"])}" data-no-edit>{E(ref["id"])}</a>'
    return ""


def render_day(day: dict) -> str:
    events = sorted(day.get("events", []), key=lambda e: hm(e["start"]))
    lo = min([10] + [int(hm(e["start"])) for e in events])
    hi = max([20] + [int(-(-hm(e.get("end") or e["start"]) // 1)) for e in events])
    span = hi - lo
    pos = lambda t: (hm(t) - lo) / span * 100
    ticks = "".join(f'<span class="tick" style="--s:{(h - lo) / span * 100:.3f}%" data-no-edit>{h}</span>' for h in range(lo, hi + 1))
    rows_end: list[float] = []
    lis = []
    for e in events:
        s = pos(e["start"])
        is_range = bool(e.get("end")) and hm(e["end"]) - hm(e["start"]) >= 1.5
        width = (pos(e["end"]) - s) if is_range else 16
        flip = not is_range and s > 78
        a, b = (s - width, s) if flip else (s, s + width)
        row = next((i for i, end in enumerate(rows_end) if end <= a), None)
        if row is None:
            rows_end.append(b)
            row = len(rows_end) - 1
        else:
            rows_end[row] = b
        cls = "ev" + (" range" if is_range else "") + (" flip" if flip else "") + (" warn" if e.get("warn") else "") + (" personal" if e.get("note") == "personal" else "")
        style = f"--s:{s:.3f}%;--row:{row + 1};" + (f"--e:{pos(e['end']):.3f}%;" if is_range else "")
        when = f'{e["start"]}–{e["end"]}' if is_range else e["start"]
        note = f' <span class="ev-note" data-no-edit>{E(e["note"])}</span>' if e.get("note") else ""
        lis.append(f'<li class="{cls}" style="{style}"><span class="ev-body"><time data-no-edit>{E(when)}</time> '
                   f'<span class="ev-label">{E(e["label"])}</span>{note} {pnum(e)}</span></li>')
    now = ""
    if day.get("now"):
        p = max(0, min(100, pos(day["now"])))
        now = (f'<div class="now" style="--s:{p:.3f}%" data-no-edit aria-hidden="true"><span>{E(day["now"])}</span></div>'
               f'<div class="past" style="--s:{p:.3f}%" aria-hidden="true"></div>')
        lis.insert(sum(1 for e in events if hm(e["start"]) <= hm(day["now"])), f'<li class="now-li" data-no-edit><time>{E(day["now"])}</time><span class="sr">now</span></li>')
    dues = "".join(
        f'<li class="dd dd-more"><a href="linear://{WORKSPACE}/my-issues/assigned">{E(d["text"])}</a></li>' if d.get("more") else
        f'<li class="dd"{f' data-key="issue:{E(d["id"])}"' if d.get("id") else ""}>{pnum(d)}<span class="due-txt">{md(d["text"])}</span>'
        f'<span class="due-st{" warn" if d.get("warn") else ""}" data-no-edit>{E(d.get("status"))}</span></li>'
        for d in day.get("due", [])
    )
    return (f'<section class="day" aria-label="{E(day["label"])}, {E(day["date"])}"><h3 class="day-h"><span>{E(day["label"])}</span> '
            f'<span class="day-date" data-no-edit>{E(day["date"])}</span></h3><div class="due-col"><ul class="due-list">{dues}</ul></div>'
            f'<div class="track" data-lo="{lo}" data-hi="{hi}" style="--rows:{max(1, len(rows_end))}"><div class="axis" aria-hidden="true">{ticks}</div>{now}'
            f'<ol class="events">{"".join(lis)}</ol></div></section>')


def render_plan(plan: dict) -> str:
    days = "".join(render_day(d) for d in plan.get("days", []))
    later = ""
    if plan.get("later"):
        lis = "".join(f'<li><span class="later-when" data-no-edit>{E(l["when"])}</span>{pnum(l)}<span>{md(l["text"])}</span></li>'
                      for l in plan["later"])
        later = f'<div class="later"><h3 class="later-h">Later</h3><ul>{lis}</ul></div>'
    return f'<div class="plan">{days}{later}</div>'


# ---------------------------------------------------------------- week plan
def _week_runs(runs: list[dict]) -> str:
    out = []
    for r in runs:
        h = E(r["t"])
        if r.get("u"):
            h = f'<a href="{E(safe_url(r["u"]))}" target="_blank" rel="noopener">{h}</a>'
        if r.get("b"):
            h = f"<strong>{h}</strong>"
        if r.get("i"):
            h = f"<em>{h}</em>"
        if r.get("s"):
            h = f"<s>{h}</s>"
        out.append(h)
    return "".join(out)


def _week_blocks(blocks: list[dict]) -> str:
    """Paragraphs and nested bullet lists. A paragraph that is all bold is a group heading ("[1-2 days] Writing")."""
    out, depth = [], -1
    for b in blocks:
        lvl = b.get("level")
        if lvl is None:
            out.append("</li></ul>" * (depth + 1)); depth = -1
            head = b.get("head") or all(r.get("b") or not r["t"].strip() for r in b["runs"])
            out.append(f'<p{" class=\"wk-h\"" if head else ""}>{_week_runs(b["runs"])}</p>')
            continue
        if lvl > depth:
            out.append("<ul>" * (lvl - depth))
        else:
            out.append("</li></ul>" * (depth - lvl) + "</li>")
        depth = lvl
        out.append(f"<li>{_week_runs(b['runs'])}")
    out.append("</li></ul>" * (depth + 1))
    return "".join(out)


def _iso_week(title: str) -> tuple[int, int] | None:
    """A week heading's ISO week: "2026W47 (week 6)" -> (2026, 47)."""
    m = re.match(r"\s*(\d{4})\s*-?\s*W(\d{1,2})\b", title)
    return (int(m[1]), int(m[2])) if m else None


def _own_section(week: dict | None) -> dict | None:
    return next((p for p in (week or {}).get("people", []) if p["name"].strip().lower() == FIRST.lower() and p["blocks"]), None)


def render_week(wp: dict | None, today: dt.date | None = None) -> str:
    """The Week plan tab: this week's section for the desk's owner, everyone else's folded below it. Until the owner
    writes this week's, it shows their most recent plan under a banner saying so."""
    if not wp:
        return '<div class="wk"><p class="wk-empty">The week plan hasn\'t been read yet.</p></div>'
    weeks = wp.get("weeks") or ([wp["week"]] if wp.get("week") else [])
    if not weeks:
        return f'<div class="wk"><p class="wk-empty">The <a href="{E(safe_url(wp["url"]))}" target="_blank" rel="noopener">week plan doc</a> has no week yet.</p></div>'
    today = today or dt.date.today()
    now, prev = today.isocalendar()[:2], (today - dt.timedelta(days=7)).isocalendar()[:2]
    # This week is the heading for the current ISO week; a doc whose headings carry no week code counts its newest as this week's.
    current = next((w for w in weeks if _iso_week(w["title"]) == now), None)
    if current is None and _iso_week(weeks[0]["title"]) is None:
        current = weeks[0]
    mine = _own_section(current)
    shown, note = current, ""
    if not mine:
        past = next((w for w in weeks if w is not current and _own_section(w) and (_iso_week(w["title"]) or (0, 0)) < now), None)
        if past:
            shown, mine = past, _own_section(past)
            which = "last week\u2019s" if _iso_week(past["title"]) == prev else "your most recent one"
            note = f'<p class="wk-note">You haven\u2019t written this week\u2019s plan yet. This is {which}.</p>'
    others = [p for p in (current or {}).get("people", []) if p["blocks"] and p["name"].strip().lower() != FIRST.lower()]
    suffix = " for this week" if shown is not current else ""
    others_html = "".join(
        f'<details class="wk-other" id="wk-{E(re.sub(r"[^a-z0-9]+", "-", p["name"].lower()).strip("-"))}">'
        f'<summary>{E(p["name"])}\u2019s plan{suffix}</summary><div class="wk-body">{_week_blocks(p["blocks"])}</div></details>'
        for p in others)
    if not shown:
        title, url = "This week", wp["url"]
    else:
        title, url = shown["title"], wp["url"] + (f'#heading={shown["heading"]}' if shown.get("heading") else "")
    body = _week_blocks(mine["blocks"]) if mine else '<p class="wk-empty">You haven\'t written a plan for this week yet.</p>'
    return (f'<div class="wk{" wk-old" if note else ""}">{note}<header class="wk-top"><h2>{E(title)}</h2>'
            f'<a class="wk-doc" href="{E(safe_url(url))}" target="_blank" rel="noopener">Open in Google Docs</a></header>'
            f'<div class="wk-body">{body}</div>{others_html}</div>')


# ---------------------------------------------------------------- publication
PUB_EXT = '<svg class="pub-ext" viewBox="0 0 12 12" aria-hidden="true"><path d="M5 2H2v8h8V7M7 2h3v3M10 2 5.5 6.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>'
PUB_CHEV = '<svg class="pub-chev" viewBox="0 0 12 12" aria-hidden="true"><path d="M4.5 3l3 3-3 3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'


def render_publication(out_dir: Path) -> str:
    """The publication strip under the header, from desk/publication.json (written by `ft-desk publication`)."""
    f = out_dir / "publication.json"
    if not f.exists():
        return ""
    try:
        pub = json.loads(f.read_text())
    except ValueError:
        return ""
    from datetime import datetime, timezone
    c = pub.get("counts", {})
    url = E(safe_url(pub.get("url") or "#"))
    ext = ' target="_blank" rel="noopener"'
    bits = [f'<span class="pub-you">{c.get("you", 0)} need you</span>' if c.get("you") else '<span class="pub-calm">Nothing needs you</span>']
    if c.get("overdue"):
        bits.append(f'<span class="pub-late">{c["overdue"]} overdue</span>')
    if c.get("others"):
        bits.append(f'<span class="pub-wait">{c["others"]} with others</span>')
    today = pub.get("today") or []
    if today:
        bits.append(f'<span class="pub-sep" aria-hidden="true"></span><span class="pub-today">Today: {E(" · ".join(today))}</span>')
    age = ""
    try:
        at = datetime.fromisoformat(pub["fetched_at"].replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - at).total_seconds() > 6 * 3600:
            age = f'<span class="pub-age">as of {E(at.astimezone().strftime("%a %-d %b %H:%M"))}</span>'
    except (KeyError, ValueError):
        pass

    def step(x: dict, cls: str = "") -> str:
        when = f'<span class="pub-when{" hot" if x.get("hot") else ""}">{E(x.get("when"))}</span>'
        return (f'<li class="{cls}"><a href="{E(safe_url(x.get("url") or "#"))}"{ext}>{E(x["title"])}</a>'
                f'<span class="pub-sub">{E(x.get("sub"))}</span>{when}</li>')

    steps = "".join(step(x) for x in pub.get("yours", [])) + "".join(step(x, "other") for x in pub.get("others", []))
    steps = f'<ol class="pub-steps">{steps}</ol>' if steps else '<p class="pub-none">Nothing to do.</p>'
    weeks = ""
    for w in pub.get("weeks", []):
        weeks += f'<span class="pub-wl">{E(w["label"])}</span>'
        for d in w["days"]:
            chips = "".join(f'<a class="pub-chip {E(x["state"])}" href="{E(safe_url(x["url"]))}" title="{E(x["title"])}"{ext}><span>{'<b class="pub-ok" aria-label="Published">✓</b> ' if x["state"] == "live" else ""}{E(x["title"])}</span></a>' for x in d["pieces"])
            weeks += f'<div class="pub-day{" today" if d.get("today") else ""}"><div class="pub-dn">{E(d["label"])}</div>{chips}</div>'
    return (
        '<section class="pub" id="pub" aria-label="Publication" data-no-edit><div class="pub-head">'
        '<button class="pub-toggle" type="button" aria-expanded="false" aria-controls="pub-panel" data-tip="Show or hide|P">'
        f'{PUB_CHEV}<span class="pub-label">Publication</span><span class="pub-sep" aria-hidden="true"></span>'
        f'<span class="pub-bits">{"".join(bits)}</span>{age}</button>'
        f'<a class="pub-open" href="{url}"{ext}>Open desk{PUB_EXT}</a></div>'
        '<div class="pub-panel" id="pub-panel" hidden>'
        f'<div><h2 class="pub-h">Your next steps</h2>{steps}</div>'
        f'<div><h2 class="pub-h">Target dates</h2><div class="pub-weeks">{weeks}</div>'
        '<div class="pub-key"><span class="k-you">Needs you</span><span class="k-late">Past target</span><span class="k-wait">With others</span></div></div>'
        '</div></section>'
    )


# ---------------------------------------------------------------- page
def peek_entry(i: dict, project: str | None) -> dict:
    return {"title": i["title"], "url": i["url"], "project": project, "description": i.get("description") or "",
            "priority": i.get("priorityLabel"), "due": i.get("dueDate"),
            "assignee": (i.get("assignee") or {}).get("name") or (i.get("assignee") or {}).get("displayName"),
            "labels": [l["name"] for l in (i.get("labels") or {}).get("nodes", [])],
            "state": (i.get("state") or {}).get("name"), "state_type": (i.get("state") or {}).get("type"),
            "comments": [{"who": (c.get("user") or {}).get("name") or "Someone", "at": c["createdAt"], "body": c["body"]}
                         for c in (i.get("comments") or {}).get("nodes", [])]}


REFRESH_SVG = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M10 6a4 4 0 1 1-1.2-2.85M10 1.5v2.5H7.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>'


def render_strip(health: list[dict] | None) -> str:
    """The live sources' state as one quiet word ("Live") or a short summary of what is late; a click shows each source."""
    if health is None:
        return ""
    import datetime as dt
    bad = [h for h in health if h["state"] in ("stale", "down")]

    def when(h):
        return dt.datetime.fromtimestamp(h["last_ok"]).strftime("%H:%M") if h.get("last_ok") else "never"

    if not bad:
        state, label = "ok", "Starting" if any(h["state"] == "waiting" for h in health) else "Live"
    else:
        state = "down" if any(h["state"] == "down" for h in bad) else "stale"
        if len(bad) * 2 >= len(health):
            oks = [h["last_ok"] for h in bad if h.get("last_ok")]
            label = f'Not updating since {dt.datetime.fromtimestamp(max(oks)).strftime("%H:%M")}' if oks else "Not updating"
        elif len(bad) == 1:
            label = f'{bad[0]["label"]} {"down" if bad[0]["state"] == "down" else "late"}'
        else:
            label = f"{len(bad)} sources late"
    blocked = any("bad file descriptor" in (h.get("detail") or "").lower() for h in bad)

    def err(h):
        d = re.sub(r"^\w+Error: ", "", h.get("detail") or "")
        return d if len(d) <= 120 else d[:119] + "…"
    rows = "".join(
        f'<li class="ls-{E(h["state"])}"><span class="live-dot" aria-hidden="true"></span><span class="ls-l">{E(h["label"])}</span>'
        f'<span class="ls-t">{E(when(h))}{"" if h["complete"] or h["state"] == "down" else ", partial"}</span>'
        + (f'<span class="ls-e">{E(err(h))}</span>' if h["state"] in ("stale", "down") and h.get("detail") and not blocked else "")
        + "</li>" for h in health)
    note = '<p class="ls-note">Connections are being blocked. Is TripMode on?</p>' if blocked else ""
    return (f'<span aria-hidden="true">·</span> <details class="live-d" id="live-d"><summary class="live-s live-{state}"><span class="live-dot" aria-hidden="true"></span>{E(label)}</summary>'
            f'<div class="live-pop">{note}<ul class="ls">{rows}</ul>'
            f'<button class="check-now" id="check-now" type="button">Check now</button></div></details>')


def label_from_snapshot(desk: dict) -> None:
    """Label Linear items with their issue's project name instead of plain "Linear"."""
    by_id = {i["id"]: i for i in (desk.get("_issues") or {}).values() if i.get("id")}
    for sec in desk.get("sections", []):
        for g in sec.get("groups") or []:
            for it in g["items"]:
                m = re.match(r"linear:([0-9a-f-]{36})", it.get("key") or "")
                if m and (it.get("source") or "").lower() == "linear" and it.get("source_label", "Linear") == "Linear":
                    name = ((by_id.get(m.group(1)) or {}).get("project") or {}).get("name")
                    if name:
                        it["source_label"] = name


def regions(desk: dict, boards: dict, out_dir: Path = STATE / "desk", health: list[dict] | None = None) -> dict[str, str]:
    """The page's parts, each of which the open page swaps in place when its HTML changes."""
    global ISSUES
    ISSUES = desk.get("_issues") or {}
    today = desk["date"]
    h = desk["header"]
    c = h.get("counts", {})
    count = lambda s: sum(len(g["items"]) for g in (s.get("groups") or [{"items": s.get("items", [])}]))
    urgent = [s for s in desk["sections"] if s["id"] == "urgent"]
    struct = project_structure()
    shown = sum(1 for b in struct["boards"] if b.get("mine") and b.get("status") == "Active")
    nav = ([(s["id"], s.get("nav") or s["title"], count(s)) for s in urgent] + [("projects", "Projects", shown)]
           + [(s["id"], s.get("nav") or s["title"], count(s)) for s in desk["sections"] if s["id"] != "urgent"])
    return {
        "title": E(h["title"]),
        "since": E(h.get("since")),
        "strip": render_strip(health),
        "stats": "".join(f'<li{" class=\"s-urgent\"" if k == "urgent" else ""}><b>{c[k]}</b> {k}</li>' for k in ("urgent", "new", "open") if k in c),
        "nav": "".join(f'<li><a href="#{E(i)}" data-target="{E(i)}"><span class="nav-l">{E(l)}</span><span class="nav-n">{n}</span></a></li>' for i, l, n in nav),
        "pub": render_publication(out_dir),
        "projects": render_projects(struct, boards, today),
        "urgent": "".join(render_section(s) for s in desk["sections"] if s["id"] == "urgent"),
        "secs": "".join(render_section(s) for s in desk["sections"] if s["id"] != "urgent"),
        "status": render_status(desk),
        "plan": render_plan(desk.get("day_plan") or {}),
        "week": render_week(desk.get("week_plan")),
    }


def peek_data(desk: dict, boards: dict, html: str, fetch_refs: bool) -> dict:
    """Every issue a card or link on the page can open in the peek panel."""
    peek = {i["identifier"]: peek_entry(i, p.get("name")) for p in project_structure()["boards"] for i in boards.get(p.get("id"), [])}
    known = desk.get("_issues") or {}
    refs = set(re.findall(r"/issue/(AS-\d+)", html)) - peek.keys()
    for r in sorted(refs & known.keys()):
        peek[r] = peek_entry(known[r], (known[r].get("project") or {}).get("name"))
    if fetch_refs and refs - known.keys():
        for i in fetch_issues(refs - known.keys()):
            peek[i["identifier"]] = peek_entry(i, (i.get("project") or {}).get("name"))
    state_icons = {"triage": "triage", "backlog": "backlog", "unstarted": "unstarted", "started": "started",
                   "completed": "done", "canceled": "backlog"}
    for v in peek.values():
        v["icon"] = STATE_ICONS["waiting" if v["state"] == "Waiting" else state_icons.get(v["state_type"], "backlog")]
    return peek


def rhash(html: str) -> str:
    import hashlib
    return hashlib.sha1(html.encode()).hexdigest()[:12]


# Feature modules: web/<name>.js and web/<name>.css, each optional, loaded after the desk's own files.
MODULES = ("controls", "comments", "sessions", "dayplan", "quickadd", "threads", "linear", "quickagent", "slacktodos")


def assets(ext: str) -> list[Path]:
    base = ["desk.css"] if ext == "css" else ["shell.js", "desk.js"]
    return [WEB / f for f in base] + [f for m in MODULES if (f := WEB / f"{m}.{ext}").exists()]


def asset_rev() -> str:
    return str(max(int(f.stat().st_mtime) for f in assets("css") + assets("js")))


def page(rg: dict[str, str], peek: dict, token: str, project_state: dict) -> str:
    R = lambda name: f'data-region="{name}" data-hash="{rhash(rg[name])}"'
    peek_json = json.dumps(peek, ensure_ascii=False).replace("<", "\\u003c")
    projects_json = json.dumps(project_state, ensure_ascii=False).replace("<", "\\u003c")
    icons_json = json.dumps({"state": STATE_ICONS, "prio": PRIO_ICONS}).replace("<", "\\u003c")
    css = "\n".join(f.read_text() for f in assets("css"))
    js = "\n".join(f.read_text() for f in assets("js"))
    logo = (WEB / "logo-plain.svg").read_text()
    return f'''<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="desk-token" content="{E(token)}">
<meta name="desk-assets" content="{asset_rev()}">
<title>Forethought desk</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500&family=Inter:wght@400;500;600&display=swap">
<style>{css}</style></head><body>
<div class="shell">
  <aside class="rail" data-no-edit>
    <div class="brand">{logo}<span class="brand-sub">Desk</span></div>
    <nav class="nav" aria-label="Sections"><ul {R("nav")}>{rg["nav"]}</ul></nav>
    <section class="rail-tasks" aria-labelledby="rt-h">
      <h2 id="rt-h">Claude tasks <span class="wait" hidden><span data-count="waiting">0</span> waiting</span></h2>
      <ul class="tasks"></ul><p class="tasks-empty">None started yet.</p>
    </section>
  </aside>
  <main class="main">
    <div class="arr-bar" id="arr-bar" data-no-edit></div>
    <header class="top">
      <div>
        <p class="eyebrow" data-no-edit>Forethought desk</p>
        <h1 data-no-edit {R("title")}>{rg["title"]}</h1>
        <div class="since" data-no-edit><span {R("since")}>{rg["since"]}</span> <span aria-hidden="true">·</span> <button class="refresh" id="refresh" type="button" data-tip="Run the full desk again in the background">{REFRESH_SVG}<span class="refresh-l">Refresh</span></button> <div class="live" {R("strip")}>{rg["strip"]}</div></div>
        <div class="refresh-note" id="refresh-note" role="status" data-no-edit></div>
      </div>
      <div class="top-r" data-no-edit>
        <div class="tabs" role="tablist" aria-label="View"><button type="button" role="tab" id="tab-desk" data-tip="Desk|D" aria-controls="view-desk" aria-selected="true">Desk</button><button type="button" role="tab" id="tab-plan" data-tip="Calendar|C" aria-controls="view-plan" aria-selected="false" tabindex="-1">Calendar</button><button type="button" role="tab" id="tab-week" data-tip="Plans|W" aria-controls="view-week" aria-selected="false" tabindex="-1">Plans</button><button type="button" role="tab" id="tab-comments" data-tip="Comments|I" aria-controls="view-comments" aria-selected="false" tabindex="-1">Comments<span class="tab-n" id="comments-n" hidden></span></button><button type="button" role="tab" id="tab-sessions" data-tip="Agents waiting|Y" aria-controls="view-sessions" aria-selected="false" tabindex="-1">Agents waiting<span class="tab-n" id="sessions-n" hidden></span></button></div>
        <ul class="stats" aria-label="Summary" {R("stats")}>{rg["stats"]}</ul>
      </div>
    </header>
    <div class="view" id="view-desk" role="tabpanel" aria-labelledby="tab-desk"><div id="desk-top" data-no-edit></div><div class="rg" {R("urgent")}>{rg["urgent"]}</div><div class="rg" {R("pub")}>{rg["pub"]}</div><div class="rg" {R("projects")}>{rg["projects"]}</div><div class="rg" {R("secs")}>{rg["secs"]}</div><div class="rg" {R("status")}>{rg["status"]}</div></div>
    <div class="view dp" id="view-plan" role="tabpanel" aria-labelledby="tab-plan" hidden {R("plan")}>{rg["plan"]}</div>
    <div class="view" id="view-week" role="tabpanel" aria-labelledby="tab-week" hidden><div id="dayplan" data-no-edit></div><div class="rg" {R("week")}>{rg["week"]}</div></div>
    <div class="view cm-view" id="view-comments" role="tabpanel" aria-labelledby="tab-comments" hidden></div>
    <div class="view ss-view" id="view-sessions" role="tabpanel" aria-labelledby="tab-sessions" hidden></div>
  </main>
</div>
<aside class="tray" id="tray" aria-label="Queued for Claude" hidden>
  <div class="tray-h"><strong>Queued for Claude <span class="tray-n" id="tray-n">0</span></strong><span class="sp"></span>
    <button class="tray-x" type="button" id="tray-min" aria-expanded="true">Hide</button></div>
  <ul class="tray-list" id="tray-list"></ul>
  <div class="tray-f">
    <label>Open as <select id="tray-mode"><option value="each">a tab each</option><option value="one">one tab, all together</option></select></label>
    <span class="sp"></span>
    <button class="btn primary" type="button" id="tray-send" data-tip="Send all|⇧S"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 2l7 4-7 4z" fill="currentColor"/></svg> Send all</button>
  </div>
</aside>
<div class="arr-pop" id="arr-pop" role="dialog" aria-label="New since the last run" hidden></div>
<div class="toast" id="toast" role="status" hidden></div>
<aside class="peek" id="peek" aria-label="Issue preview" hidden></aside>
<script type="application/json" id="issue-data">{peek_json}</script>
<script type="application/json" id="project-state">{projects_json}</script>
<script type="application/json" id="desk-icons">{icons_json}</script>
<script>{js}</script>
</body></html>
'''


def compose(desk: dict, out_dir: Path = STATE / "desk", health: list[dict] | None = None, fetch_refs: bool = False) -> tuple[dict, dict]:
    """(regions, peek data) for a desk from desk_live.live_desk()."""
    boards = desk.get("_boards") or {}
    label_from_snapshot(desk)
    rg = regions(desk, boards, out_dir, health)
    return rg, peek_data(desk, boards, rg["urgent"] + rg["projects"] + rg["secs"] + rg["status"] + rg["plan"], fetch_refs)


def ensure_token(out: Path) -> str:
    token_file = out / ".token"
    if not token_file.exists():
        import secrets
        token_file.write_text(secrets.token_urlsafe(24))
        token_file.chmod(0o600)
    return token_file.read_text().strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("desk_json", nargs="?", default=str(STATE / "desk" / "desk.json"))
    ap.add_argument("--out", default=str(STATE / "desk"))
    ap.add_argument("--no-linear", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(HERE))
    if Path(a.desk_json).resolve() == (STATE / "desk" / "desk.json").resolve():
        # The live desk: the run's desk.json plus the collectors' facts. The desk service composes the same
        # page on every request; this file is what it serves if the store is unreadable, and its
        # modification time tells the Mac app that a run has finished.
        import desk_collect
        import desk_live
        import desk_store
        if not a.no_linear and (desk_store.snapshot("linear") is None or desk_collect.desk_issue_refs(desk_live.load(Path(a.desk_json), {})) - set(desk_live.issue_index(desk_store.snapshot("linear") or {}))):
            desk_collect.run_collector("linear", {"linear": (lambda: desk_collect.collect_linear(force=True), 60, "Linear")})
        desk = desk_live.live_desk()
        health = desk_collect.health(desk_collect.FACTS)
    else:  # a sample desk.json, for mock-ups: no store, no live facts
        desk = json.loads(Path(a.desk_json).read_text())
        ids = desk_projects.board_ids()
        desk["_boards"] = {} if a.no_linear else fetch_boards(ids)
        health = None
    rg, peek = compose(desk, out, health, fetch_refs=not a.no_linear)
    from desk_server import project_state  # snoozes and review state, reset by each new desk run
    (out / "index.html").write_text(page(rg, peek, ensure_token(out), project_state()))
    boards = desk.get("_boards") or {}
    print(json.dumps({"page": str(out / "index.html"), "projects": len(desk_projects.board_ids()), "issues": sum(len(v) for v in boards.values())}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
