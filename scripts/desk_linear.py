"""Linear changes made from the desk (moves, done, reject, title and description edits, comments, and the peek panel's
state, assignee, priority, due date and project), sent through a durable outbox so that none is lost offline.

Every change is written to the outbox before anything else, applied to the Linear snapshot the page is drawn from
(so the page shows it at once), then sent. A network failure (DNS, connection, timeout, a 5xx, a rate limit, or
1Password unreachable for the API key) leaves it in the outbox, and the retry collector sends the outbox, oldest
first, every 30 seconds until it goes through. While a change waits, every refetch of the boards has it laid over
them again, so the page keeps showing it.

Nothing is applied twice: an update sets absolute values (a state, an assignee), so a retry after a lost answer
changes nothing, and a comment carries the page's id as its Linear id, which a retry checks for before creating.
A change Linear refuses (the issue was deleted, a value is no longer valid) is never retried: answered at once, it
goes back to the page, which undoes it; found by a background retry, it is held, the page lists it with Linear's
reason and Discard, and the boards are refetched so the snapshot shows Linear's state again.

Outbox (DESK/linear-outbox.json; a test copy on another port keeps its own):
    {"items": [change waiting to be sent], "held": [change Linear refused in the background],
     "done": {id: result}, "recent": [{"id", "issue", "label", "at"}]}
A change: {"id", "issue", "kind": "update" | "comment", "set": {field: value}, "body", "label", "queued_at",
           "attempts", "error"}. `set` holds Linear values with the names the snapshot needs:
    state {id, name, type}, assignee {id, name} or None, priority 0-4, due "YYYY-MM-DD" or None,
    title, description, project {id, name} or None.

The team's states, members and projects are cached in DESK/linear-team.json, refreshed hourly, so that a change
can be resolved (a board column to a state) and the menus filled while offline.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desk_collect as collect  # noqa: E402
import desk_store as store  # noqa: E402
from desk_config import CONFIG, DESK, ME, PORT  # noqa: E402

DEFAULT_PORT = CONFIG.get("server", {}).get("port", 4848)
TEST_COPY = PORT != DEFAULT_PORT
OUTBOX = DESK / ("linear-outbox.json" if not TEST_COPY else f"linear-outbox-{PORT}.json")
TEAM_CACHE = DESK / "linear-team.json"
TEAM_EVERY = 3600
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
ISSUE_RE = re.compile(r"^[A-Z]{1,10}-\d{1,6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DONE_DAYS, RECENT_HOURS = 14, 24
PRIORITIES = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
TYPE_ORDER = ["triage", "backlog", "unstarted", "started", "completed", "canceled"]
# Board column -> (state type, preferred state name)
COLUMN_STATES = {"Triage": ("triage", "Triage"), "Backlog": ("backlog", "Backlog"), "Todo": ("unstarted", "Todo"),
                 "In Progress": ("started", "In Progress"), "Waiting": ("started", "Waiting"), "Done": ("completed", "Done"),
                 "Rejected": ("canceled", "Rejected")}
JOINT = "joint"   # the task-manager's label for a task with several owners; one assignee drops it, as `linear-tasks update --who` does

FILE_LOCK = threading.Lock()   # read-modify-write of the outbox
PROC_LOCK = threading.Lock()   # one sender at a time, so changes go out in order and none twice


class Offline(Exception):
    """Linear couldn't be reached, or answered in a way a later retry may fix."""


class Refused(Exception):
    """Linear refused the change for a reason a retry won't fix."""


# ---------------------------------------------------------------- outbox file
def _load() -> dict:
    try:
        data = json.loads(OUTBOX.read_text())
    except (OSError, ValueError):
        data = {}
    for k, v in (("items", []), ("held", []), ("done", {}), ("recent", [])):
        data.setdefault(k, v)
    return data


def _save(data: dict) -> None:
    now = time.time()
    data["done"] = {k: v for k, v in data["done"].items() if now - v.get("at", now) < DONE_DAYS * 86400}
    data["recent"] = [r for r in data["recent"] if now - r.get("at", now) < RECENT_HOURS * 3600][-20:]
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTBOX.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, OUTBOX)


def _update(fn):
    with FILE_LOCK:
        data = _load()
        out = fn(data)
        _save(data)
        return out


def pending() -> list[dict]:
    return _load()["items"]


# ---------------------------------------------------------------- talking to Linear
NET_RE = re.compile(r"URLError|urlopen error|timed out|TimeoutError|ConnectionError|nodename nor servname|"
                    r"Name or service not known|Temporary failure in name resolution|Network is unreachable|"
                    r"No route to host|Bad file descriptor|SSL|i/o timeout|dial tcp|from 1Password", re.I)


def _reachable() -> None:
    """Fail fast when offline, rather than after linear-tasks' own retries."""
    try:
        socket.create_connection(("api.linear.app", 443), timeout=4).close()
    except OSError as e:
        raise Offline(f"Linear unreachable ({e.strerror or e})") from None


def _gql(query: str, variables: dict | None = None) -> dict:
    """One GraphQL call through linear-tasks' key and HTTP code, with its failures sorted into Offline and Refused."""
    lt = collect.linear()
    try:
        auth = lt.auth_header()
    except SystemExit as e:   # linear-tasks exits when 1Password can't give it the key
        raise Offline(f"the Linear key couldn't be read ({e})") from None
    try:
        status, out = lt.http_json({"query": query, "variables": variables or {}}, {"Authorization": auth},
                                   repeatable=query.lstrip().startswith("query"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise Offline(f"Linear unreachable ({getattr(e, 'reason', e)})") from None
    msg = "; ".join(e.get("message", "") for e in out.get("errors") or []) or f"HTTP {status}"
    if status in (401, 429) or status >= 500 or lt.rate_limited(status, out):
        raise Offline(f"Linear answered {status}: {msg}"[:300])
    if status >= 400 or out.get("errors"):
        raise Refused(msg[:300])
    return out["data"]


# ---------------------------------------------------------------- the team: states, members, projects
def _fetch_team() -> dict:
    _reachable()
    t = _gql("""query { team(id: "%s") { id
        states(first: 50) { nodes { id name type position } }
        labels(first: 100) { nodes { id name } }
        members(first: 100) { nodes { id name displayName active app } } } }""" % collect.TEAM)["team"]
    projects = _gql("""query { projects(first: 200, filter: {accessibleTeams: {id: {eq: "%s"}}}) { nodes { id name state } } }""" % t["id"])
    return {"at": time.time(), "team_id": t["id"],
            "states": sorted(t["states"]["nodes"], key=lambda s: (TYPE_ORDER.index(s["type"]) if s["type"] in TYPE_ORDER else 9, s["position"], s["name"])),
            "labels": {l["name"].lower(): l["id"] for l in t["labels"]["nodes"]},
            "members": sorted([{"id": m["id"], "name": m["name"] or m["displayName"]} for m in t["members"]["nodes"]
                               if m.get("active", True) and not m.get("app")], key=lambda m: (m["name"] != ME, m["name"])),
            "projects": sorted([{"id": p["id"], "name": p["name"]} for p in projects["projects"]["nodes"]
                                if p.get("state") not in ("completed", "canceled")], key=lambda p: p["name"].lower())}


_team_lock = threading.Lock()


def team(refresh: bool = False) -> dict:
    """The cached team, fetched if there is none (or `refresh`). Raises Offline when there is none and Linear can't be reached."""
    with _team_lock:
        try:
            cache = json.loads(TEAM_CACHE.read_text())
        except (OSError, ValueError):
            cache = {}
        if cache.get("states") and not refresh:
            return cache
        try:
            cache = _fetch_team()
        except (Offline, Refused):
            if cache.get("states"):
                return cache
            raise
        tmp = TEAM_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False))
        os.replace(tmp, TEAM_CACHE)
        return cache


def state_for_column(column: str) -> dict:
    if column not in COLUMN_STATES:
        raise ValueError("bad column")
    typ, name = COLUMN_STATES[column]
    states = [s for s in team()["states"] if s["type"] == typ]
    if column == "In Progress":
        states = [s for s in states if s["name"] != "Waiting"]
    elif column == "Waiting":
        states = [s for s in states if s["name"] == "Waiting"]
    if not states:
        raise ValueError(f"no {column} state in this team")
    s = next((s for s in states if s["name"] == name), states[0])
    return {"id": s["id"], "name": s["name"], "type": s["type"]}


# ---------------------------------------------------------------- the snapshot: show a change before Linear has it
def _patch_fields(op: dict) -> dict:
    """The snapshot fields an update changes, in the snapshot's own shape."""
    s, out = op.get("set") or {}, {}
    if "state" in s:
        out["state"] = {"name": s["state"]["name"], "type": s["state"]["type"]}
        out["completedAt"] = dt.datetime.now(dt.timezone.utc).isoformat() if s["state"]["type"] == "completed" else None
    if "assignee" in s:
        out["assignee"] = {"name": s["assignee"]["name"], "displayName": s["assignee"]["name"]} if s["assignee"] else None
    if "priority" in s:
        out["priorityLabel"] = PRIORITIES[s["priority"]]
    if "due" in s:
        out["dueDate"] = s["due"]
    for k in ("title", "description"):
        if k in s:
            out[k] = s[k]
    if "project" in s:
        out["project"] = s["project"]
    return out


def overlay(data: dict, ops: list[dict] | None = None) -> dict:
    """Lay changes still waiting to be sent over a Linear snapshot ({"boards", "mine", "unassigned", "refs"})."""
    ops = pending() if ops is None else ops
    if not ops or not data:
        return data
    boards = data.get("boards") or {}
    for op in ops:
        hits = [(pool, i) for pool in [*boards.values(), data.get("mine") or [], data.get("unassigned") or [],
                                         list((data.get("refs") or {}).values())] for i in pool if i.get("identifier") == op["issue"]]
        for pool, i in hits:
            if op["kind"] == "comment":
                nodes = (i.setdefault("comments", {}) or {}).setdefault("nodes", [])
                if not any(c.get("desk_id") == op["id"] for c in nodes):
                    nodes.insert(0, {"body": op["body"], "createdAt": dt.datetime.fromtimestamp(op["queued_at"], dt.timezone.utc).isoformat(),
                                     "user": {"name": ME}, "desk_id": op["id"]})
            else:
                i.update(_patch_fields(op))
        # A new project moves the card to that project's board, when the desk shows one.
        proj = (op.get("set") or {}).get("project", False)
        if proj is not False:
            for pid, nodes in boards.items():
                for i in list(nodes):
                    if i.get("identifier") == op["issue"] and (not proj or pid != proj["id"]):
                        nodes.remove(i)
                        if proj and proj["id"] in boards and not any(x.get("identifier") == op["issue"] for x in boards[proj["id"]]):
                            boards[proj["id"]].append(i)
    return data


def _patch_snapshot(op: dict) -> None:
    if TEST_COPY:   # a test copy shares the real store: its (often stubbed) changes stay out of the real desk
        return
    snap = store.snapshot("linear")
    if snap:
        store.put_snapshot("linear", overlay(snap, [op]))


def _refetch() -> None:
    """Have the Linear collector fetch the boards again soon (after a change went through, or was refused)."""
    if TEST_COPY:
        return
    store.set_meta("linear_dirty", True)
    store.set_due("linear", time.time() + 2)


# ---------------------------------------------------------------- validation
def _clean(body: dict) -> dict:
    """One change from the page, checked and resolved against the team. Raises ValueError (a 400) for anything malformed."""
    oid = str(body.get("id") or "")
    if not ID_RE.match(oid):
        raise ValueError("bad change id")
    issue = str(body.get("issue") or "")
    if not ISSUE_RE.match(issue):
        raise ValueError("bad issue")
    kind = body.get("kind")
    op = {"id": oid, "issue": issue, "queued_at": time.time(), "attempts": 0, "error": None}
    if kind == "comment":
        text = str(body.get("body") or "").strip()
        if not text or len(text) > 20000:
            raise ValueError("empty or overlong comment")
        return {**op, "kind": "comment", "body": text, "label": f"Comment on {issue}"}
    if kind != "update":
        raise ValueError("kind must be update or comment")
    raw, s, labels = body.get("set") or {}, {}, []
    if not isinstance(raw, dict) or not raw:
        raise ValueError("nothing to change")
    unknown = set(raw) - {"column", "state", "assignee", "priority", "due", "title", "description", "project"}
    if unknown:
        raise ValueError(f"can't change {', '.join(sorted(unknown))}")
    t = team() if set(raw) & {"column", "state", "assignee", "project"} else {}
    if "column" in raw:
        s["state"] = state_for_column(str(raw["column"]))
        labels.append(f"moved to {s['state']['name']}")
    elif "state" in raw:
        st = next((x for x in t["states"] if x["id"] == raw["state"]), None)
        if not st:
            raise ValueError("unknown state")
        s["state"] = {"id": st["id"], "name": st["name"], "type": st["type"]}
        labels.append(f"moved to {st['name']}")
    if "assignee" in raw:
        if raw["assignee"] is None:
            s["assignee"] = None
            labels.append("unassigned")
        else:
            m = next((x for x in t["members"] if x["id"] == raw["assignee"]), None)
            if not m:
                raise ValueError("unknown person")
            s["assignee"] = {"id": m["id"], "name": m["name"]}
            labels.append(f"assigned to {m['name']}")
    if "priority" in raw:
        if raw["priority"] not in PRIORITIES:
            raise ValueError("priority must be 0 to 4")
        s["priority"] = raw["priority"]
        labels.append(f"priority {PRIORITIES[raw['priority']]}")
    if "due" in raw:
        d = raw["due"]
        if d is not None:
            if not DATE_RE.match(str(d)):
                raise ValueError("bad due date")
            dt.date.fromisoformat(d)
        s["due"] = d
        labels.append(f"due {dt.date.fromisoformat(d).strftime('%a %-d %b')}" if d else "no due date")
    if "title" in raw:
        title = str(raw["title"] or "").strip()
        if not title or len(title) > 255 or "\n" in title:
            raise ValueError("the title must be one line of 1-255 characters")
        s["title"] = title
        labels.append("title edited")
    if "description" in raw:
        desc = str(raw["description"] or "")
        if len(desc) > 100000:
            raise ValueError("description too long")
        s["description"] = desc
        labels.append("description edited")
    if "project" in raw:
        if raw["project"] is None:
            s["project"] = None
            labels.append("no project")
        else:
            p = next((x for x in t["projects"] if x["id"] == raw["project"]), None)
            if not p:
                raise ValueError("unknown project")
            s["project"] = {"id": p["id"], "name": p["name"]}
            labels.append(f"moved to project {p['name']}")
    return {**op, "kind": "update", "set": s, "label": f"{issue} {', '.join(labels)}"}


# ---------------------------------------------------------------- sending
def _stub(op: dict) -> dict | None:
    """On a test copy (another port), FT_DESK_LINEAR_STUB=1 replaces Linear with the mode written in
    DESK/linear-stub-<port> (ok, offline or refuse), so the page and the outbox can be tested without real issues."""
    if not (TEST_COPY and os.environ.get("FT_DESK_LINEAR_STUB")):
        return None
    try:
        mode = (DESK / f"linear-stub-{PORT}").read_text().strip()
    except OSError:
        mode = "ok"
    time.sleep(0.3)
    if mode == "offline":
        raise Offline("stub: offline")
    if mode == "refuse":
        raise Refused(f"stub: {op['issue']} no longer exists")
    if op["kind"] == "comment":
        return {"who": ME, "at": dt.datetime.now(dt.timezone.utc).isoformat()}
    return {"state": (op["set"].get("state") or {}).get("name")}


def _send(op: dict) -> dict:
    stub = _stub(op)
    if stub is not None:
        return stub
    _reachable()
    found = _gql("""query($id: String!) { issue(id: $id) { id labels { nodes { name } } } }""", {"id": op["issue"]})
    issue = found.get("issue")
    if not issue:
        raise Refused(f"{op['issue']} isn't in Linear any more")
    if op["kind"] == "comment":
        if op.get("attempts"):   # an earlier try may have gone through with its answer lost
            try:
                c = _gql("""query($id: String!) { comment(id: $id) { createdAt user { name } } }""", {"id": op["id"]}).get("comment")
            except Refused:
                c = None
            if c:
                return {"who": (c.get("user") or {}).get("name") or ME, "at": c["createdAt"]}
        r = _gql("""mutation($i: CommentCreateInput!) { commentCreate(input: $i) { success comment { createdAt user { name } } } }""",
                 {"i": {"id": op["id"], "issueId": issue["id"], "body": op["body"]}})["commentCreate"]
        if not r or not r["success"]:
            raise Refused("Linear didn't accept the comment")
        return {"who": (r["comment"].get("user") or {}).get("name") or ME, "at": r["comment"]["createdAt"]}
    s, inp = op["set"], {}
    if "state" in s:
        inp["stateId"] = s["state"]["id"]
    if "assignee" in s:
        inp["assigneeId"] = s["assignee"]["id"] if s["assignee"] else None
        joint = team().get("labels", {}).get(JOINT)
        if joint and any(l["name"].lower() == JOINT for l in issue["labels"]["nodes"]):
            inp["removedLabelIds"] = [joint]
    if "priority" in s:
        inp["priority"] = s["priority"]
    if "due" in s:
        inp["dueDate"] = s["due"]
    for k in ("title", "description"):
        if k in s:
            inp[k] = s[k]
    if "project" in s:
        inp["projectId"] = s["project"]["id"] if s["project"] else None
    r = _gql("""mutation($id: String!, $in: IssueUpdateInput!) { issueUpdate(id: $id, input: $in) { success issue { state { name } } } }""",
             {"id": issue["id"], "in": inp})["issueUpdate"]
    if not r or not r["success"]:
        raise Refused("Linear didn't accept the change")
    return {"state": r["issue"]["state"]["name"]}


def process(requested: set[str] | None = None) -> dict:
    """Send the outbox oldest first, stopping at the first change Linear can't be reached for, so the rest keep
    their order. Returns {id: result} for every change this pass dealt with or left waiting."""
    requested = requested or set()
    results: dict[str, dict] = {}
    with PROC_LOCK:
        blocked = None
        for op in list(_load()["items"]):
            if blocked:
                results[op["id"]] = {"status": "queued", "error": blocked}
                continue
            try:
                res = _send(op)
            except (Offline, Exception) as e:   # an unexpected failure is kept and retried too, with its reason shown
                if isinstance(e, Refused):
                    results[op["id"]] = _settle(op, {"refused": str(e)}, background=op["id"] not in requested)
                    continue
                blocked = str(e) if isinstance(e, Offline) else f"{type(e).__name__}: {e}"

                def keep(d, oid=op["id"], err=blocked):
                    for x in d["items"]:
                        if x["id"] == oid:
                            x["attempts"] = x.get("attempts", 0) + 1
                            x["error"], x["tried_at"] = err, time.time()
                _update(keep)
                results[op["id"]] = {"status": "queued", "error": blocked}
                continue
            results[op["id"]] = _settle(op, res, background=op["id"] not in requested)
    return results


def _settle(op: dict, res: dict, background: bool) -> dict:
    """Record what happened to a change and take it out of the outbox, in one write."""
    oid = op["id"]

    def apply(d):
        d["items"] = [x for x in d["items"] if x["id"] != oid]
        if "refused" not in res:
            rec = {**res, "issue": op["issue"], "at": time.time()}
            d["done"][oid] = rec
            d["recent"].append({"id": oid, "issue": op["issue"], "label": op["label"], "at": rec["at"], "bg": background})
            return {"status": "applied", **rec}
        if background:   # nobody is waiting on this request: hold it for the page to show
            d["held"] = [x for x in d["held"] if x["id"] != oid] + [{**op, "error": res["refused"], "held_at": time.time()}]
            return {"status": "held", "error": res["refused"]}
        return {"status": "refused", "error": res["refused"]}

    out = _update(apply)
    _refetch()
    return out


# ---------------------------------------------------------------- routes
def write(body: dict) -> dict:
    """Queue one change, show it in the snapshot, then send the outbox. A change id already sent returns its first result."""
    op = _clean(body)
    early = {}

    def enqueue(d):
        if op["id"] in d["done"]:
            early["r"] = {"status": "applied", **d["done"][op["id"]]}
        elif not any(x["id"] == op["id"] for x in d["items"]):
            d["held"] = [x for x in d["held"] if x["id"] != op["id"]]
            d["items"].append(op)
    _update(enqueue)
    if early:
        return early["r"]
    _patch_snapshot(op)
    res = process({op["id"]}).get(op["id"]) or {"status": "queued"}
    return {**res, "label": op["label"], "waiting": len(pending())}


def retry(body: dict) -> dict:
    process()
    d = _load()
    return {"waiting": len(d["items"]), "held": len(d["held"]), "error": next((x.get("error") for x in d["items"] if x.get("error")), None)}


def held_action(body: dict) -> dict:
    """Discard a change Linear refused, or try it once more."""
    oid, action = str(body.get("id") or ""), body.get("action")
    if action not in ("discard", "retry"):
        raise ValueError("action must be discard or retry")

    def take(d):
        hit = next((x for x in d["held"] if x["id"] == oid), None)
        d["held"] = [x for x in d["held"] if x["id"] != oid]
        if hit and action == "retry":
            d["items"].append({k: v for k, v in hit.items() if k not in ("held_at",)} | {"error": None})
        return hit
    hit = _update(take)
    if not hit:
        raise ValueError("That change is no longer held")
    if action == "discard":
        _refetch()
        return {"ok": True}
    _patch_snapshot(hit)
    return {"ok": True, "result": process({oid}).get(oid) or {"status": "queued"}}


def meta(body: dict) -> dict:
    """What the peek panel's menus offer: the team's states, members and projects, and the priorities."""
    try:
        t = team()
    except Offline as e:
        raise RuntimeError(f"Linear can't be reached and the team isn't cached yet ({e})") from None
    return {"states": [{"id": s["id"], "name": s["name"], "type": s["type"]} for s in t["states"] if s["type"] != "duplicate"],
            "members": t["members"], "projects": t["projects"], "me": ME,
            "priorities": [{"value": v, "name": n} for v, n in [(1, "Urgent"), (2, "High"), (3, "Medium"), (4, "Low"), (0, "No priority")]]}


ROUTES = {
    "/api/linear/write": write,
    "/api/linear/meta": meta,
    "/api/linear/retry": retry,
    "/api/linear/held": held_action,
}


# ---------------------------------------------------------------- collectors and sync
def collect_outbox() -> collect.Result:
    """Send whatever is waiting. Never raises for a failed send, so the scheduler keeps trying instead of backing off."""
    if not pending():
        return collect.Result("nothing waiting")
    res = process()
    sent = sum(1 for r in res.values() if r.get("status") == "applied")
    left = len(pending())
    return collect.Result(f"{sent} sent, {left} waiting" + (f" ({next(iter(res.values())).get('error')})" if left and res else ""), complete=not left)


def collect_team() -> collect.Result:
    t = team(refresh=True)
    return collect.Result(f"{len(t['states'])} states, {len(t['members'])} people, {len(t['projects'])} projects")


COLLECTORS = {
    "linear_outbox": (collect_outbox, 30, None),
    "linear_team": (collect_team, TEAM_EVERY, None),
}

# Every refetch of the boards gets the changes still waiting laid over it.
if overlay not in collect.LINEAR_OVERLAYS:
    collect.LINEAR_OVERLAYS.append(overlay)

_info: dict = {}


def sync_info() -> dict:
    """The outbox's state for the page, re-read only when the file changes."""
    try:
        mtime = OUTBOX.stat().st_mtime
    except OSError:
        return {"waiting": 0, "items": [], "held": [], "recent": []}
    if _info.get("mtime") != mtime:
        d = _load()
        _info["mtime"] = mtime
        _info["v"] = {
            "waiting": len(d["items"]),
            "items": [{"id": x["id"], "issue": x["issue"], "label": x["label"], "error": x.get("error")} for x in d["items"]],
            "held": [{"id": x["id"], "issue": x["issue"], "label": x["label"], "error": x.get("error")} for x in d["held"]],
            "recent": d["recent"][-10:],
        }
    return _info["v"]


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["retry"]:
        print(json.dumps(retry({}), indent=1))
    elif cmd == ["team"]:
        t = team(refresh=True)
        print(json.dumps({k: len(v) for k, v in t.items() if isinstance(v, (list, dict))}))
    elif cmd == ["status"]:
        print(json.dumps(_load(), indent=1, ensure_ascii=False))
    else:
        print("desk_linear.py retry | team | status")
