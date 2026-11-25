#!/usr/bin/env python3
"""The desk's collectors: each keeps one source current in the store, on its own cadence.

    desk_collect.py run NAME      run one collector now and print its health row
    desk_collect.py health        print every source's health

The desk service (desk_server.py) starts `Scheduler`, which runs each collector when it is
due, backs off after failures and records every run's health. A collector never sends
anything; the only outside write is the calendar's automatic decline of research-meeting
invitations, which the gather already made.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import importlib.machinery
import importlib.util
import json
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desk_store as store  # noqa: E402

from desk_config import CONFIG, DESK, PERSONAL_CALENDAR, ROOT, path  # noqa: E402,F401

FT = HERE / "ft-desk"
FT_HOURS = path("ft_hours")
CHECK_SYNC = path("check_sync")
LINEAR_SCRIPT = path("linear_tasks")
TEAM = "AS"
PERSONAL = PERSONAL_CALENDAR or {"account": "", "calendars": []}


def load_script(name: str, path: Path):
    """Load an extensionless script as a module."""
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


_mods: dict[str, object] = {}
_mods_lock = threading.Lock()


def ftd():
    with _mods_lock:
        if "ftd" not in _mods:
            _mods["ftd"] = load_script("ft_desk_cli", FT)
        return _mods["ftd"]


def linear():
    with _mods_lock:
        if "lt" not in _mods:
            _mods["lt"] = load_script("linear_tasks", LINEAR_SCRIPT)
        return _mods["lt"]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def desk_json() -> dict:
    return load_json(DESK / "desk.json", {})


class Result:
    def __init__(self, detail: str = "", complete: bool = True, cursor=None, changed: bool = False, writes=None):
        self.detail, self.complete, self.cursor, self.changed = detail, complete, cursor, changed
        self.writes = writes  # writes(con) saves what was fetched, in the transaction that advances the cursor


# ---------------------------------------------------------------- Linear
ISSUE_FIELDS = """id identifier title url dueDate priorityLabel description updatedAt completedAt
  state { name type } assignee { name displayName } project { id name }
  labels(first: 10) { nodes { name } } comments(first: 3) { nodes { body createdAt user { name } } }"""
OPEN = '{type: {nin: ["completed", "canceled"]}}'
# Functions that change a freshly fetched Linear snapshot before it is saved: desk_linear lays the changes still
# waiting in its outbox over it, so a refetch doesn't undo what the page shows.
LINEAR_OVERLAYS: list = []


def gql(query: str, variables: dict | None = None) -> dict:
    try:
        return linear().gql(query, variables or {})
    except SystemExit as e:  # linear-tasks exits on API and auth failures
        raise RuntimeError(f"Linear: {e}") from None


def gql_paged(query: str, variables: dict, path: list[str], cap: int = 400) -> tuple[list[dict], bool]:
    """All nodes of a connection, 50 at a time. Returns (nodes, complete)."""
    nodes, after = [], None
    while True:
        data = gql(query, {**variables, "after": after})
        for k in path:
            data = data[k]
        nodes += data["nodes"]
        if not data["pageInfo"]["hasNextPage"]:
            return nodes, True
        if len(nodes) >= cap:
            return nodes, False
        after = data["pageInfo"]["endCursor"]


def desk_issue_refs(desk: dict) -> set[str]:
    return set(re.findall(r"(?<![\w/-])(AS-\d+)(?![\w-])", json.dumps(desk, ensure_ascii=False)))


def collect_linear(force: bool = False) -> Result:
    """Boards for the desk's projects, Peter's dated issues, unassigned issues and issues the desk's text names.
    One small query a minute asks what changed; the boards are refetched only when something did."""
    prev = store.snapshot("linear") or {}
    cur = store.cursor_of("linear") or {}
    desk = desk_json()
    import desk_projects  # imported here: desk_projects imports this module for gql
    project_ids = desk_projects.board_ids()
    refs_wanted = sorted(desk_issue_refs(desk))
    now = dt.datetime.now(dt.timezone.utc)
    stale = time.time() - (cur.get("full_at") or 0) > 600
    same_inputs = cur.get("projects") == project_ids and cur.get("refs") == refs_wanted
    if prev and cur.get("since") and same_inputs and not stale and not force and not store.meta("linear_dirty"):
        q = """query($since: DateTimeOrDuration!) { issues(first: 50, filter: {team: {key: {eq: "%s"}}, updatedAt: {gt: $since}}) { nodes { identifier } } }""" % TEAM
        changed = gql(q, {"since": cur["since"]})["issues"]["nodes"]
        if not changed:
            return Result("no changes", cursor=cur)
    store.set_meta("linear_dirty", False)
    since = (now - dt.timedelta(seconds=5)).isoformat()
    midnight = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    complete = True
    boards = {}
    bq = ("""query($id: String!, $after: String, $mid: DateTimeOrDuration!) { project(id: $id) { issues(first: 50, after: $after,
            filter: {or: [{state: %s}, {completedAt: {gt: $mid}}]}) { nodes { %s } pageInfo { hasNextPage endCursor } } } }""" % (OPEN, ISSUE_FIELDS))

    def board(pid):
        return pid, gql_paged(bq, {"id": pid, "mid": midnight}, ["project", "issues"])

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        for pid, (nodes, ok) in pool.map(board, project_ids):
            boards[pid] = nodes
            complete = complete and ok
    horizon = (dt.date.today() + dt.timedelta(days=8)).isoformat()
    mq = ("""query($after: String, $due: TimelessDateOrDuration!) { issues(first: 50, after: $after, filter: {team: {key: {eq: "%s"}},
            assignee: {isMe: {eq: true}}, dueDate: {lte: $due}, state: %s}) { nodes { %s } pageInfo { hasNextPage endCursor } } }""" % (TEAM, OPEN, ISSUE_FIELDS))
    mine, ok = gql_paged(mq, {"due": horizon}, ["issues"])
    complete = complete and ok
    uq = ("""query($after: String) { issues(first: 50, after: $after, filter: {team: {key: {eq: "%s"}}, assignee: {null: true}, state: %s})
            { nodes { %s } pageInfo { hasNextPage endCursor } } }""" % (TEAM, OPEN, ISSUE_FIELDS))
    unassigned, ok = gql_paged(uq, {}, ["issues"])
    complete = complete and ok
    on_boards = {i["identifier"] for nodes in boards.values() for i in nodes}
    nums = sorted({int(r.split("-")[1]) for r in refs_wanted if r not in on_boards})
    refs = {}
    for start in range(0, len(nums), 50):
        rq = ("""query($n: [Float!]) { issues(first: 50, filter: {team: {key: {eq: "%s"}}, number: {in: $n}}) { nodes { %s } } }""" % (TEAM, ISSUE_FIELDS))
        for i in gql(rq, {"n": nums[start:start + 50]})["issues"]["nodes"]:
            refs[i["identifier"]] = i
    data = {"boards": boards, "mine": mine, "unassigned": unassigned, "refs": refs}
    for fn in LINEAR_OVERLAYS:
        data = fn(data)
    changed = store.put_snapshot("linear", data)
    n = sum(len(v) for v in boards.values())
    return Result(f"{n} issues on {len(boards)} boards", complete=complete, changed=changed,
                  cursor={"since": since, "projects": project_ids, "refs": refs_wanted, "full_at": time.time()})


def patch_linear_issue(identifier: str, **fields) -> None:
    """Apply a change the desk itself just made in Linear to the snapshot at once, then have the collector confirm it."""
    snap = store.snapshot("linear")
    if snap:
        pools = list(snap["boards"].values()) + [snap["mine"], snap["unassigned"], list(snap["refs"].values())]
        for pool in pools:
            for i in pool:
                if i["identifier"] == identifier:
                    i.update(fields)
        store.put_snapshot("linear", snap)
    store.set_meta("linear_dirty", True)
    store.set_due("linear", time.time() + 3)


# ---------------------------------------------------------------- calendar
def collect_calendar() -> Result:
    """Today's and tomorrow's events on the Forethought calendar. "Busy" blocks (calls booked through
    a public booking page) take their names from Peter's personal calendars, matched by start time."""
    f = ftd()
    cfg = f.CONFIG["calendar"]

    def events(account, cal):
        data = f.run_json(["gog", "--json", "cal", "events", cal, "--from", "today", "--days", "2",
                           "--account", account, "--max", "100"])
        return data.get("events") or []

    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        main = pool.submit(events, cfg["account"], cfg["calendar_id"])
        personal = [pool.submit(events, PERSONAL["account"], c) for c in PERSONAL["calendars"]]
        ft_events = main.result()
        names, personal_ok = {}, True
        for fut in personal:
            try:
                for e in fut.result():
                    start = (e.get("start") or {}).get("dateTime")
                    if start and e.get("summary") and e.get("status") != "cancelled":
                        names.setdefault(dt.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp(), e["summary"])
            except Exception:
                personal_ok = False
    out = []
    for e in ft_events:
        if e.get("status") == "cancelled":
            continue
        start, end = e.get("start") or {}, e.get("end") or {}
        me = next((a for a in e.get("attendees") or [] if a.get("self")), None)
        response = me.get("responseStatus") if me else None
        if response == "declined":
            continue
        all_day = "date" in start and "dateTime" not in start
        row = {"id": e.get("id"), "summary": e.get("summary") or "Busy", "all_day": all_day, "response": response,
               "link": e.get("htmlLink"), "meet": e.get("hangoutLink")}
        if all_day:
            row.update(start=start.get("date"), end=end.get("date"))
        else:
            s = dt.datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
            row.update(start=s.astimezone().isoformat(timespec="minutes"),
                       end=dt.datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00")).astimezone().isoformat(timespec="minutes"))
            if row["summary"].strip().lower() == "busy" and s.timestamp() in names:
                row.update(summary=names[s.timestamp()], personal=True)
        out.append(row)
    out.sort(key=lambda r: r["start"])
    changed = store.put_snapshot("calendar", {"events": out})
    return Result(f"{len(out)} events", complete=personal_ok, changed=changed)


# ---------------------------------------------------------------- the smaller facts
def collect_publication() -> Result:
    r = subprocess.run([str(FT), "publication"], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "publication fetch failed")
    return Result(r.stdout.strip()[:200], changed=store.put_snapshot("publication", load_json(DESK / "publication.json", {})))


def collect_hours() -> Result:
    text = subprocess.run([str(FT_HOURS), "status"], capture_output=True, text=True, timeout=60)
    data = subprocess.run([str(FT_HOURS), "status", "--json"], capture_output=True, text=True, timeout=60)
    if text.returncode or data.returncode:
        raise RuntimeError((text.stderr or data.stderr).strip()[-300:] or "ft-hours failed")
    j = json.loads(data.stdout)
    total = (j.get("days_used") or 0) + (j.get("days_left") or 0)
    lines = [l.strip() for l in text.stdout.splitlines() if l.strip()]
    lines[0] = re.sub(r"^Forethought hours:\s*", "", lines[0])
    snap = {"text": " ".join(lines).replace("Waiting on Peter:", "Waiting on you:"),
            "pct": round(100 * j["days_used"] / total, 1) if total else 0}
    return Result(f'{j.get("days_used")} days used', changed=store.put_snapshot("hours", snap))


def collect_slack_todos() -> Result:
    res = ftd().src_slack_todos(0)
    errors = res.get("workspace_errors")
    changed = store.put_snapshot("slack_todos", {"items": res["items"]})
    return Result(f'{len(res["items"])} to-dos' + (f"; {errors}" if errors else ""), complete=not errors, changed=changed)


def collect_metrics() -> Result:
    r = subprocess.run([str(CHECK_SYNC)], capture_output=True, text=True, timeout=120)
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if r.returncode == 2:
        raise RuntimeError("the metrics dashboard couldn't be reached")
    changed = store.put_snapshot("metrics", {"ok": r.returncode == 0, "lines": lines})
    return Result("ok" if r.returncode == 0 else "stale: " + "; ".join(lines)[:300], changed=changed)


# ---------------------------------------------------------------- week plan
def _doc_tab(tabs: list[dict], tab_id: str) -> dict | None:
    for t in tabs:
        if (t.get("tabProperties") or {}).get("tabId") == tab_id:
            return t
        found = _doc_tab(t.get("childTabs") or [], tab_id)
        if found:
            return found
    return None


def _doc_runs(par: dict) -> list[dict]:
    """A paragraph's text as runs: t (text), and b, i, s (bold, italic, struck) and u (link) when set.
    Suggested insertions are left out, so the plan reads as accepted."""
    runs = []
    for el in par.get("elements") or []:
        if "textRun" in el:
            tr = el["textRun"]
            if tr.get("suggestedInsertionIds"):
                continue
            st = tr.get("textStyle") or {}
            run = {"t": (tr.get("content") or "").replace("\n", "").replace("\x0b", " ")}
            run.update({k[0]: True for k in ("bold", "italic", "strikethrough") if st.get(k)})
            if (st.get("link") or {}).get("url"):
                run["u"] = st["link"]["url"]
        elif "richLink" in el:
            props = el["richLink"].get("richLinkProperties") or {}
            run = {"t": props.get("title") or props.get("uri") or "", "u": props.get("uri")}
        elif "person" in el:
            props = el["person"].get("personProperties") or {}
            run = {"t": props.get("name") or props.get("email") or ""}
        else:
            continue
        if not run["t"]:
            continue
        prev = runs[-1] if runs else None
        if prev and {k: v for k, v in prev.items() if k != "t"} == {k: v for k, v in run.items() if k != "t"}:
            prev["t"] += run["t"]
        else:
            runs.append(run)
    return runs


def week_plans_from_doc(doc: dict, tab_id: str, limit: int = 6) -> list[dict]:
    """The newest weeks (each Heading 1, newest first), with a section per person (each Heading 2 under it)."""
    tab = _doc_tab(doc.get("tabs") or [], tab_id)
    if not tab:
        raise RuntimeError(f"the doc has no tab {tab_id}")
    weeks, week = [], None
    for c in ((tab.get("documentTab") or {}).get("body") or {}).get("content") or []:
        p = c.get("paragraph")
        if not p:
            continue
        style = (p.get("paragraphStyle") or {}).get("namedStyleType", "")
        runs = _doc_runs(p)
        text = "".join(r["t"] for r in runs).strip()
        if style == "HEADING_1":
            if len(weeks) == limit:
                break
            week = {"title": text, "heading": p["paragraphStyle"].get("headingId"), "people": []}
            weeks.append(week)
        elif not week:
            continue
        elif style == "HEADING_2":
            week["people"].append({"name": text, "blocks": []})
        elif text and week["people"]:
            b = {"runs": runs}
            if p.get("bullet") is not None:
                b["level"] = p["bullet"].get("nestingLevel", 0)
            elif style.startswith("HEADING_"):
                b["head"] = True
            week["people"][-1]["blocks"].append(b)
    return weeks


def collect_week_plan() -> Result:
    """The team's week-plan doc (`week_plan` in config.json): its newest weeks, for the Week plan tab. Read through
    gog's Docs API export rather than gdoc, whose reads mark the doc as seen for other agent sessions."""
    cfg = CONFIG["week_plan"]
    r = subprocess.run(["gog", "docs", "cat", cfg["doc"], "--tab", cfg["tab"], "--raw", "--account", cfg["account"], "--no-input"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "gog docs cat failed")
    weeks = week_plans_from_doc(json.loads(r.stdout), cfg["tab"])
    snap = {"url": f'https://docs.google.com/document/d/{cfg["doc"]}/edit?tab={cfg["tab"]}', "weeks": weeks}
    week = weeks[0] if weeks else None
    detail = f'{week["title"]}: {", ".join(p["name"] for p in week["people"]) or "no one yet"}' if week else "no week in the doc"
    return Result(detail, changed=store.put_snapshot("week_plan", snap))


# ---------------------------------------------------------------- scheduler
# name -> (function, seconds between runs, label on the page's source strip)
FACTS = {
    "linear": (collect_linear, 60, "Linear"),
    "calendar": (collect_calendar, 120, "Calendar"),
    "slack_todos": (collect_slack_todos, 600, "Slack to-dos"),
    "publication": (collect_publication, 600, "Publication"),
    "hours": (collect_hours, 900, "Hours"),
    "metrics": (collect_metrics, 3600, "Metrics sync"),
    "week_plan": (collect_week_plan, 300, "Week plan"),
}
MAX_BACKOFF = 1800


def run_collector(name: str, table: dict) -> dict:
    fn, every, _ = table[name]
    t0 = time.monotonic()
    try:
        res = fn()
        with store.tx() as con:
            if res.writes:
                res.changed = bool(res.writes(con)) or res.changed
            store.record_run(name, ok=True, detail=res.detail, complete=res.complete, cursor=res.cursor,
                             next_due=time.time() + every * random.uniform(0.9, 1.1), seconds=time.monotonic() - t0, con=con)
        return {"name": name, "ok": True, "changed": res.changed}
    except Exception as e:  # a failed source keeps its cursor and retries with a growing delay
        failures = store.source(name).get("failures", 0) + 1
        delay = min(every * 2 ** min(failures, 6), MAX_BACKOFF)
        store.record_run(name, ok=False, detail=f"{type(e).__name__}: {e}", next_due=time.time() + delay,
                         seconds=time.monotonic() - t0)
        return {"name": name, "ok": False, "error": str(e)}


class Scheduler(threading.Thread):
    """Runs each collector when it is due. `on_change` is called after a run that changed the store."""

    def __init__(self, table: dict, on_change=None, workers: int = 4):
        super().__init__(daemon=True, name="desk-collectors")
        self.table, self.on_change = table, on_change
        self.pool = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="collect")
        self.running: set[str] = set()
        self.lock = threading.Lock()

    def check_now(self, names=None) -> None:
        for n in names or self.table:
            store.set_due(n, 0)

    def _done(self, name: str, fut: cf.Future) -> None:
        with self.lock:
            self.running.discard(name)
        try:
            res = fut.result()
        except Exception as e:  # never let a collector kill the loop
            print(f"collector {name} crashed: {e}", file=sys.stderr, flush=True)
            return
        if not res["ok"]:
            print(f"{time.strftime('%H:%M:%S')} collector {name} failed: {res.get('error')}", file=sys.stderr, flush=True)
        if res.get("changed") and self.on_change:
            self.on_change(name)

    def run(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as e:  # a locked database must not end the loop, or no collector runs again
                print(f"{time.strftime('%H:%M:%S')} scheduler: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(2)

    def _tick(self) -> None:
        now = time.time()
        for name in self.table:
            with self.lock:
                if name in self.running:
                    continue
                if (store.source(name).get("next_due") or 0) > now:
                    continue
                self.running.add(name)
            try:
                fut = self.pool.submit(run_collector, name, self.table)
            except Exception:
                with self.lock:
                    self.running.discard(name)
                raise
            fut.add_done_callback(lambda f, n=name: self._done(n, f))

    def busy(self) -> list[str]:
        with self.lock:
            return sorted(self.running)


def health(table: dict) -> list[dict]:
    """One row per source for the page's strip: fresh, stale (late or incomplete) or down."""
    rows, now = [], time.time()
    by = {s["name"]: s for s in store.sources()}
    for name, (_, every, label) in table.items():
        if not label:
            continue
        s = by.get(name) or {}
        last_ok = s.get("last_ok")
        if not last_ok:
            state = "down" if s.get("status") == "error" else "waiting"
        elif s.get("status") == "error" and now - last_ok > max(3 * every, 600):
            state = "down"
        elif s.get("status") == "error" or now - last_ok > max(3 * every, 300) or not s.get("complete", 1):
            state = "stale"
        else:
            state = "ok"
        rows.append({"name": name, "label": label, "state": state, "last_ok": last_ok, "every": every,
                     "detail": s.get("detail") or "", "complete": bool(s.get("complete", 1))})
    return rows


def main(argv: list[str]) -> int:
    if argv[:1] == ["run"] and len(argv) == 2 and argv[1] in FACTS:
        print(json.dumps(run_collector(argv[1], FACTS)))
        print(json.dumps(store.source(argv[1]), indent=1))
        return 0
    if argv[:1] == ["health"]:
        print(json.dumps(health(FACTS), indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
