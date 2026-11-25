"""Quick create: Linear and Todoist tasks typed into the desk's Q box (web/quickadd.js parses them).

Every task the page sends is written to a durable outbox before anything else, then created:
- Linear through the task-manager's `linear-tasks add`, so its labels, defaults and duplicate guard apply;
- Todoist through its Sync API, with the token the `td` CLI keeps (read through td's own code, never printed),
  so the due date and time go in as structured fields.

A network failure (DNS, connection, timeout, a 5xx) leaves the task in the outbox, and the retry collector sends
the outbox, oldest first, every minute until it goes through. A retry can't create a twin: a Linear task carries
the Action ID `desk-quickadd-<id>`, which linear-tasks reports as "exists" on a second try, and a Todoist command
carries the page's id as its uuid, which Todoist applies once. Success is recorded (in `done`) in the same write
that takes the task out of the outbox, so a page that resends an id it never heard back about gets the first result.

Outbox (DESK/quickadd-outbox.json; a test copy on another port keeps its own):
    {"items": [task waiting to be sent], "held": [task a retry couldn't send for a reason a retry won't fix],
     "done": {id: {"ref", "url", "title", "dest", "at"}}, "recent": [{"id", "ref", "title", "dest", "at", "bg"}]}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desk_collect as collect  # noqa: E402
import desk_store as store  # noqa: E402
from desk_config import CONFIG, DESK, FIRST, LINEAR_WHO, PORT, path as cfg_path  # noqa: E402

DEFAULT_PORT = CONFIG.get("server", {}).get("port", 4848)
TEST_COPY = PORT != DEFAULT_PORT
OUTBOX = DESK / ("quickadd-outbox.json" if not TEST_COPY else f"quickadd-outbox-{PORT}.json")
PROJECTS_CACHE = DESK / "quickadd-projects.json"
LINEAR_SCRIPT = cfg_path("linear_tasks")
_qa = CONFIG.get("quickadd", {})
LINEAR_DEFAULT_PROJECT = _qa.get("linear_project", "Operations")   # a task with no #project goes here
PEOPLE = ("peter", "sam", "none")                                  # linear-tasks' --who values
TODOIST_SYNC = "https://api.todoist.com/api/v1/sync"
TODOIST_PROJECTS = "https://api.todoist.com/api/v1/projects"
PROJECTS_EVERY = 3600
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
DONE_DAYS, RECENT_HOURS = 14, 24

FILE_LOCK = threading.Lock()   # read-modify-write of the outbox
PROC_LOCK = threading.Lock()   # one sender at a time, so the outbox goes out in order and nothing is sent twice


class Offline(Exception):
    """The service couldn't be reached, or answered in a way a later retry may fix."""


class Refused(Exception):
    """The service refused the task for a reason a retry won't fix (a bad project, an ambiguous match)."""


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
    os.replace(tmp, OUTBOX)   # atomic: a crash leaves the old file or the new one, never half of each


def _update(fn):
    with FILE_LOCK:
        data = _load()
        out = fn(data)
        _save(data)
        return out


# ---------------------------------------------------------------- validation
def _clean(raw: dict) -> dict:
    """One task from the page, checked. Raises ValueError (a 400) for anything malformed."""
    if not isinstance(raw, dict):
        raise ValueError("each task must be an object")
    tid = str(raw.get("id") or "")
    if not ID_RE.match(tid):
        raise ValueError("bad task id")
    dest = raw.get("dest")
    if dest not in ("linear", "todoist"):
        raise ValueError("dest must be linear or todoist")
    title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()
    if not title:
        raise ValueError("every task needs a title")
    if len(title) > 300:
        raise ValueError("the title is longer than 300 characters")
    desc = str(raw.get("description") or "").strip()
    if len(desc) > 16000:
        raise ValueError("the description is too long")
    due = raw.get("due") or None
    if due is not None:
        try:
            dt.date.fromisoformat(str(due))
        except ValueError:
            raise ValueError(f"bad due date {due!r}") from None
    tm = raw.get("time") or None
    if tm is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(tm)):
        raise ValueError(f"bad due time {tm!r}")
    if tm and not due:
        raise ValueError("a due time needs a date")
    pri = raw.get("priority")
    if pri not in (None, 1, 2, 3, 4):
        raise ValueError("priority must be 1 to 4")
    who = raw.get("who") or None
    if who is not None and who not in PEOPLE:
        raise ValueError(f"unknown person {who}; use {', '.join(PEOPLE)}")
    return {"id": tid, "dest": dest, "title": title, "description": desc, "due": due, "time": tm, "priority": pri,
            "project_id": str(raw.get("project_id") or "")[:64] or None,
            "project_name": str(raw.get("project_name") or "")[:200] or None,
            "who": who, "force": bool(raw.get("force")), "raw": str(raw.get("raw") or "")[:17000],
            "queued_at": time.time(), "attempts": 0, "error": None}


# ---------------------------------------------------------------- reachability and error classes
def _reachable(host: str) -> None:
    """Fail fast when offline, rather than after linear-tasks' own retries."""
    try:
        socket.create_connection((host, 443), timeout=4).close()
    except OSError as e:
        raise Offline(f"{host} unreachable ({e.strerror or e})") from None


NET_RE = re.compile(r"URLError|urlopen error|timed out|TimeoutError|ConnectionError|ConnectionResetError|RemoteDisconnected|"
                    r"IncompleteRead|nodename nor servname|Name or service not known|Temporary failure in name resolution|"
                    r"Network is unreachable|No route to host|Bad file descriptor|SSLError|SSL:|socket\.|"
                    r"Linear API failed \((5\d\d|429)\)|Linear returned 5|from 1Password", re.I)


# ---------------------------------------------------------------- Linear
def _linear_status(who: str) -> str:
    """Peter's own task is committed work (Not started); one he gives Sam or no one waits in Unprocessed to be taken."""
    return "Not started" if who == LINEAR_WHO else "Unprocessed"


def _linear_item(it: dict) -> dict:
    who = it.get("who") or LINEAR_WHO
    desc = it["description"] or f"Added by {FIRST} from the desk."
    if it.get("time"):
        desc += f"\n\nDue at {it['time']}."   # Linear's due date has no time
    item = {"title": it["title"], "description": desc, "project": it.get("project_id") or it.get("project_name") or LINEAR_DEFAULT_PROJECT,
            "who": who, "status": _linear_status(who), "action_id": f"desk-quickadd-{it['id']}"}
    if it.get("due"):
        item["due"] = it["due"]
    if it.get("priority"):
        item["priority"] = f"P{it['priority'] - 1}"   # Todoist's p1 (highest) is Linear's P0 (Urgent) ... p4 is P3 (Low)
    return item


def _run_linear(item: dict, force: bool) -> dict:
    """linear-tasks add for one item: {"ref", "url", "title"}, or {"similar": {...}} when its duplicate guard skipped it."""
    _reachable("api.linear.app")
    try:
        r = subprocess.run([sys.executable, str(LINEAR_SCRIPT), "add", "--stdin", "--json", *(["--force"] if force else [])],
                           input=json.dumps([item]), capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        raise Offline("linear-tasks timed out") from None
    out = (r.stderr or "") + (r.stdout or "")
    if r.returncode:
        lines = [x for x in out.strip().splitlines() if x.strip()]
        msg = lines[-1] if lines else "linear-tasks add failed"
        if "Traceback" in out or NET_RE.search(out):
            raise Offline(msg[:300])
        raise Refused(msg.removeprefix("linear-tasks: ")[:300])
    res = json.loads(r.stdout)[0]
    t = res.get("task") or {}
    ref = t.get("identifier") or t.get("id") or ""
    if res.get("skipped") and res.get("reason") != "exists":   # "exists": an earlier try created it
        return {"similar": {"id": ref, "title": t.get("title", ""), "url": t.get("url"), "why": res.get("why") or res.get("reason")}}
    return {"ref": ref, "url": t.get("url"), "title": t.get("title") or item["title"]}


# ---------------------------------------------------------------- Todoist
_token: dict = {}


def _td_paths() -> tuple[str, Path]:
    """The node binary and td's auth module (td is a node script; the desk app's PATH has neither)."""
    cands = [CONFIG.get("paths", {}).get("td"), shutil.which("td"), str(Path.home() / ".local/node-default/bin/td")]
    for c in filter(None, cands):
        p = Path(os.path.expanduser(c))
        if p.exists():
            auth = p.resolve().parent / "lib" / "auth.js"
            node = p.parent / "node"
            node = str(node) if node.exists() else shutil.which("node") or ""
            if auth.exists() and node:
                return node, auth
    raise Offline("can't find the Todoist CLI (td) and node to read its token")


def _todoist_token() -> str:
    if os.environ.get("TODOIST_API_TOKEN"):
        return os.environ["TODOIST_API_TOKEN"]
    if _token.get("v"):
        return _token["v"]
    node, auth = _td_paths()
    js = "const m = await import(process.argv[1]); process.stdout.write(await m.getApiToken());"
    r = subprocess.run([node, "--input-type=module", "-e", js, auth.as_uri()], capture_output=True, text=True, timeout=30)
    tok = r.stdout.strip()
    if r.returncode or not tok:
        raise Offline("td has no Todoist token (`td auth status`)")
    _token["v"] = tok
    return tok


def _todoist(url: str, data: dict | None = None, retry_auth: bool = True):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data is not None else None,
                                 headers={"Authorization": f"Bearer {_todoist_token()}"}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code in (401, 403) and retry_auth and not os.environ.get("TODOIST_API_TOKEN"):
            _token.clear()   # td may have a fresh token since
            return _todoist(url, data, retry_auth=False)
        if e.code >= 500 or e.code == 429:
            raise Offline(f"Todoist returned {e.code}") from None
        raise Refused(f"Todoist refused it ({e.code}): {body}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise Offline(f"Todoist unreachable ({getattr(e, 'reason', e)})") from None


def _todoist_args(it: dict) -> dict:
    args = {"content": it["title"]}
    if it["description"]:
        args["description"] = it["description"]
    if it.get("project_id"):
        args["project_id"] = it["project_id"]   # none: the Inbox
    if it.get("due"):
        # A floating date-time: the time in whatever zone Peter is in. A time pages his phone; a date alone doesn't.
        args["due"] = {"date": f"{it['due']}T{it['time']}:00" if it.get("time") else it["due"]}
    if it.get("priority"):
        args["priority"] = 5 - it["priority"]   # the API counts the other way: 4 is p1
    return args


def _run_todoist(it: dict) -> dict:
    _reachable("api.todoist.com")
    temp = f"qa-{it['id']}"
    cmd = {"type": "item_add", "uuid": it["id"], "temp_id": temp, "args": _todoist_args(it)}
    out = _todoist(TODOIST_SYNC, {"commands": json.dumps([cmd])})
    status = (out.get("sync_status") or {}).get(it["id"])
    if status != "ok":
        if isinstance(status, dict):
            raise Refused(f"Todoist refused it: {status.get('error') or status}")
        raise Offline(f"Todoist didn't confirm the task ({status!r})")
    real = (out.get("temp_id_mapping") or {}).get(temp)
    return {"ref": real or "", "url": f"https://app.todoist.com/app/task/{real}" if real else None, "title": it["title"]}


# ---------------------------------------------------------------- the stub, for a test copy only
def _stub(it: dict) -> dict | None:
    """On a test copy (another port), FT_DESK_QUICKADD_STUB=1 replaces the network calls with the mode written in
    DESK/quickadd-stub-<port> (ok, offline, similar or refuse), so the page and the outbox can be tested offline."""
    if not (TEST_COPY and os.environ.get("FT_DESK_QUICKADD_STUB")):
        return None
    try:
        mode = (DESK / f"quickadd-stub-{PORT}").read_text().strip()
    except OSError:
        mode = "ok"
    time.sleep(0.4)
    if mode == "offline":
        raise Offline("stub: offline")
    if mode == "refuse":
        raise Refused("stub: no project matches")
    if mode == "similar" and it["dest"] == "linear" and not it.get("force"):
        return {"similar": {"id": "AS-1", "title": "A similar open task", "url": None, "why": "near-identical title"}}
    n = int(time.time() * 1000) % 100000
    return {"ref": f"AS-{n}" if it["dest"] == "linear" else str(n), "url": None, "title": it["title"]}


def _create(it: dict) -> dict:
    stub = _stub(it)
    if stub is not None:
        return stub
    if it["dest"] == "linear":
        return _run_linear(_linear_item(it), it.get("force", False))
    return _run_todoist(it)


# ---------------------------------------------------------------- sending
def process(requested: set[str] | None = None) -> dict:
    """Send the outbox oldest first. A destination that fails to answer is skipped for the rest of the pass, so its
    tasks keep their order. Returns {id: result} for every task this pass dealt with or left waiting."""
    requested = requested or set()
    results: dict[str, dict] = {}
    with PROC_LOCK:
        blocked: dict[str, str] = {}
        for it in list(_load()["items"]):
            if it["dest"] in blocked:
                results[it["id"]] = {"status": "queued", "error": blocked[it["dest"]]}
                continue
            try:
                res = _create(it)
            except Offline as e:
                blocked[it["dest"]] = str(e)

                def keep(d, tid=it["id"], err=str(e)):
                    for x in d["items"]:
                        if x["id"] == tid:
                            x["attempts"] = x.get("attempts", 0) + 1
                            x["error"], x["tried_at"] = err, time.time()
                _update(keep)
                results[it["id"]] = {"status": "queued", "error": str(e)}
                continue
            except Refused as e:
                res = {"refused": str(e)}
            except Exception as e:  # an unexpected failure: keep the task and say why
                blocked[it["dest"]] = f"{type(e).__name__}: {e}"
                results[it["id"]] = {"status": "queued", "error": blocked[it["dest"]]}
                continue
            results[it["id"]] = _settle(it, res, background=it["id"] not in requested)
    return results


def _settle(it: dict, res: dict, background: bool) -> dict:
    """Record what happened to a task and take it out of the outbox, in one write."""
    tid = it["id"]

    def apply(d):
        d["items"] = [x for x in d["items"] if x["id"] != tid]
        if "ref" in res:
            rec = {"ref": res["ref"], "url": res.get("url"), "title": res.get("title") or it["title"], "dest": it["dest"], "at": time.time()}
            d["done"][tid] = rec
            d["recent"].append({"id": tid, **rec, "bg": background})
            return {"status": "created", **rec}
        reason = {"similar": res["similar"], "error": None} if "similar" in res else {"similar": None, "error": res.get("refused")}
        if background:   # nobody is waiting on this request: hold it for the page to decide
            d["held"] = [x for x in d["held"] if x["id"] != tid] + [{**it, **reason, "held_at": time.time()}]
            return {"status": "held", **reason}
        return {"status": "similar" if "similar" in res else "refused", **reason}

    out = _update(apply)
    if out["status"] == "created" and it["dest"] == "linear" and not TEST_COPY:
        store.set_meta("linear_dirty", True)     # the boards pick the new task up at once
        store.set_due("linear", time.time() + 1)
    return out


# ---------------------------------------------------------------- routes
def add(body: dict) -> dict:
    """Queue the page's tasks, then send the outbox. A task id already sent returns its first result."""
    raw = body.get("items")
    if not isinstance(raw, list) or not raw or len(raw) > 50:
        raise ValueError("items must be a list of 1 to 50 tasks")
    items = [_clean(x) for x in raw]
    early: dict[str, dict] = {}

    def enqueue(d):
        waiting = {x["id"] for x in d["items"]}
        for it in items:
            if it["id"] in d["done"]:
                early[it["id"]] = {"status": "created", **d["done"][it["id"]]}
            elif it["id"] in waiting:
                pass   # a resend of a task already queued: it goes out with the rest
            else:
                d["held"] = [x for x in d["held"] if x["id"] != it["id"]]
                d["items"].append(it)
    _update(enqueue)
    results = process({it["id"] for it in items})
    return {"results": {it["id"]: early.get(it["id"]) or results.get(it["id"]) or {"status": "queued"} for it in items},
            "waiting": len(_load()["items"])}


def retry(body: dict) -> dict:
    process()
    d = _load()
    return {"waiting": len(d["items"]), "held": len(d["held"]), "error": next((x.get("error") for x in d["items"] if x.get("error")), None)}


def held_action(body: dict) -> dict:
    """Add a held task anyway (skipping the duplicate guard) or discard it."""
    tid, action = str(body.get("id") or ""), body.get("action")
    if action not in ("force", "discard"):
        raise ValueError("action must be force or discard")

    def take(d):
        hit = next((x for x in d["held"] if x["id"] == tid), None)
        d["held"] = [x for x in d["held"] if x["id"] != tid]
        if hit and action == "force":
            hit = {k: v for k, v in hit.items() if k not in ("similar", "error", "held_at")}
            d["items"].append({**hit, "force": True, "error": None})
        return hit
    hit = _update(take)
    if not hit:
        raise ValueError("That task is no longer held")
    if action == "discard":
        return {"ok": True}
    res = process({tid}).get(tid) or {"status": "queued"}
    return {"ok": True, "result": res}


# ---------------------------------------------------------------- projects for # completion
def _linear_projects() -> list[dict]:
    import desk_projects
    mine = {b["name"] for b in desk_projects.boards() if b.get("mine")}
    out = []
    for p in collect.linear().all_projects():
        if (p.get("status") or "").lower() in ("done", "completed", "canceled", "cancelled"):
            continue
        out.append({"id": p["id"], "name": p["title"], "mine": p["title"] in mine})
    return sorted(out, key=lambda p: p["name"].lower())


def _todoist_projects() -> list[dict]:
    out, cursor = [], None
    for _ in range(10):
        q = {"limit": 200, **({"cursor": cursor} if cursor else {})}
        page = _todoist(f"{TODOIST_PROJECTS}?{urllib.parse.urlencode(q)}")
        for p in page.get("results", []):
            if not p.get("is_archived") and not p.get("is_deleted"):
                out.append({"id": p["id"], "name": p["name"], "inbox": bool(p.get("inbox_project")), "order": p.get("child_order", 0)})
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return out


def _cache() -> dict:
    try:
        return json.loads(PROJECTS_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def refresh_projects(which=("linear", "todoist")) -> dict:
    """Fetch the projects lists and cache them. A failed fetch keeps the last list."""
    cache, errors = _cache(), []
    for name, fn in (("linear", _linear_projects), ("todoist", _todoist_projects)):
        if name not in which:
            continue
        try:
            cache[name] = {"at": time.time(), "projects": fn()}
        except (Exception, SystemExit) as e:   # linear-tasks exits on API failures
            errors.append(f"{name}: {e}")
    tmp = PROJECTS_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False))
    os.replace(tmp, PROJECTS_CACHE)
    if errors and not any(cache.get(n) for n in which):
        raise RuntimeError("; ".join(errors))
    return cache


_refreshing = threading.Lock()


def _refresh_in_background(which) -> None:
    def run():
        if _refreshing.acquire(blocking=False):
            try:
                refresh_projects(which)
            except Exception:
                pass
            finally:
                _refreshing.release()
    threading.Thread(target=run, daemon=True, name="quickadd-projects").start()


def meta(body: dict) -> dict:
    """What the box needs to parse: each destination's projects and defaults. Works offline from the cache."""
    cache = _cache()
    missing = [n for n in ("linear", "todoist") if not cache.get(n)]
    if missing:
        try:
            cache = refresh_projects(missing)
        except RuntimeError:
            pass
    stale = [n for n in ("linear", "todoist") if time.time() - (cache.get(n) or {}).get("at", 0) > PROJECTS_EVERY]
    if stale:
        _refresh_in_background(stale)
    lin = (cache.get("linear") or {}).get("projects") or [
        {"id": b["id"], "name": b["name"], "mine": b.get("mine", True)} for b in __import__("desk_projects").boards() if b.get("id")]
    tdo = (cache.get("todoist") or {}).get("projects") or []
    inbox = next((p for p in tdo if p.get("inbox")), None)
    return {"linear": {"projects": lin, "default_project": LINEAR_DEFAULT_PROJECT, "people": list(PEOPLE), "default_who": LINEAR_WHO},
            "todoist": {"projects": tdo, "default_project": inbox["name"] if inbox else "Inbox"}}


ROUTES = {
    "/api/quickadd/meta": meta,
    "/api/quickadd/add": add,
    "/api/quickadd/retry": retry,
    "/api/quickadd/held": held_action,
}


# ---------------------------------------------------------------- collectors and sync
def collect_outbox() -> collect.Result:
    """Send whatever is waiting. Never raises for a failed send, so the scheduler keeps trying every minute
    instead of backing off."""
    if not _load()["items"]:
        return collect.Result("nothing waiting")
    res = process()
    sent = sum(1 for r in res.values() if r.get("status") == "created")
    left = len(_load()["items"])
    return collect.Result(f"{sent} sent, {left} waiting" + (f" ({next(iter(res.values())).get('error')})" if left else ""), complete=not left)


def collect_projects() -> collect.Result:
    cache = refresh_projects()
    return collect.Result(", ".join(f"{n} {len((cache.get(n) or {}).get('projects') or [])}" for n in ("linear", "todoist")))


COLLECTORS = {
    "quickadd_outbox": (collect_outbox, 60, None),
    "quickadd_projects": (collect_projects, PROJECTS_EVERY, None),
}

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
            "items": [{"id": x["id"], "title": x["title"], "dest": x["dest"], "error": x.get("error")} for x in d["items"]],
            "held": [{"id": x["id"], "title": x["title"], "dest": x["dest"], "raw": x.get("raw", ""),
                      "similar": x.get("similar"), "error": x.get("error")} for x in d["held"]],
            "recent": d["recent"][-10:],
        }
    return _info["v"]


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["retry"]:
        print(json.dumps(retry({}), indent=1))
    elif cmd == ["projects"]:
        c = refresh_projects()
        print(json.dumps({n: len((c.get(n) or {}).get("projects") or []) for n in c}))
    elif cmd == ["status"]:
        print(json.dumps(_load(), indent=1, ensure_ascii=False))
    else:
        print("desk_quickadd.py retry | projects | status")
