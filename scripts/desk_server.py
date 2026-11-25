#!/usr/bin/env python3
"""Serve the desk page on http://127.0.0.1:4848 and act on its buttons.

    desk_server.py serve        run in the foreground
    desk_server.py ensure       start it in the background unless it is already up
    desk_server.py stop

The page is composed on each request from the last run's desk.json and the store's live facts
(desk_live.py), which the collectors started here keep current (desk_collect.py). The open
page asks every few seconds which of its regions have changed and swaps those in place.

Refresh runs the full desk as a Claude Code background session (`claude --bg`) and follows it here:
it closes the session once the page re-renders, and when the run blocks, fails or stops early the page
explains why, with Open in Warp (resume that conversation in a tab) and Run again in Warp.

Start opens a Claude Code tab in Warp through the pane skill's launcher, with a handoff
built here from the desk's items (the page sends only an item key and Peter's note). The tasks
list reads each session's state from the agent-session registry (`agent-panes --json`),
and Focus brings that session's Warp tab to the front.

Only this machine can reach it: it binds to 127.0.0.1, checks the Host header, and every
API call must carry the page's token in an X-Desk-Token header, which a page on another
site cannot send without a CORS preflight that this server never answers. The one exception
is the publication desk's Run prompt button: pages from publish.example.org (behind
Cloudflare Access) may POST a prompt to /api/agent, and only there (CROSS_ORIGIN_ROUTES).
"""
from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_config import CONFIG, DESK, FIRST, LINEAR_WHO, PORT, ROOT, path as cfg_path  # noqa: E402  FT_DESK_PORT runs a second copy for testing

DEFAULT_PORT = CONFIG.get("server", {}).get("port", 4848)
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
# Pages on other sites that may call a route, with no desk token: the browser sets Origin, and the
# preflight (do_OPTIONS) is answered for these pairs only.
CROSS_ORIGIN_ROUTES = {"/api/agent": {"https://publish.example.org"}}
# Prompts from another site open at most one tab per CROSS_GAP seconds and CROSS_PER_HOUR an hour, so a page
# gone wrong can't open a flood of sessions (and a double click opens one). The desk's own page isn't limited.
CROSS_GAP, CROSS_PER_HOUR = 5, 20
CROSS_RUNS: list[float] = []
CROSS_LOCK = threading.Lock()


def cross_origin_allowance() -> None:
    """Take one cross-origin run, or raise ValueError saying when the next is allowed."""
    now = time.monotonic()
    with CROSS_LOCK:
        CROSS_RUNS[:] = [t for t in CROSS_RUNS if now - t < 3600]
        if CROSS_RUNS and now - CROSS_RUNS[-1] < CROSS_GAP:
            raise ValueError(f"one started a moment ago; try again in {CROSS_GAP - int(now - CROSS_RUNS[-1])} seconds")
        if len(CROSS_RUNS) >= CROSS_PER_HOUR:
            raise ValueError(f"{CROSS_PER_HOUR} prompts from the publication desk in the past hour, the most allowed; "
                             "use the desk's A box, or try again later")
        CROSS_RUNS.append(now)
TASKS = DESK / "tasks.json"
QUEUE = DESK / "queue.json"
PIDFILE = DESK / ("server.pid" if PORT == DEFAULT_PORT else f"server-{PORT}.pid")
LOG = DESK / "server.log"
ARCHIVED = DESK / "archived.json"
PROJECTS = DESK / "projects.json"
FT = Path(__file__).resolve().parent / "ft-desk"
SPAWN = cfg_path("spawn_agent_panes")
SPAWN_LOG = cfg_path("spawn_log")
AGENT_PANES = cfg_path("agent_panes")
LOCK = threading.Lock()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import desk_arrivals as arrivals  # noqa: E402
import desk_collect as collect  # noqa: E402
import desk_comments as comments  # noqa: E402
import desk_sessions as sessions  # noqa: E402
import desk_live as live  # noqa: E402
import desk_projects  # noqa: E402
import desk_store as store  # noqa: E402
import render_desk  # noqa: E402
import desk_shadow as shadow  # noqa: E402

SCHEDULER: collect.Scheduler | None = None
VENV_PYTHON = store.DIR / "venv/bin/python"


def collect_judge() -> collect.Result:
    """Run the shadow judge over conversations that have changed, in the desk's own Python environment (it has the Anthropic SDK)."""
    if not VENV_PYTHON.exists():
        return collect.Result("the judge's Python environment is missing", complete=False)
    r = subprocess.run([str(VENV_PYTHON), str(Path(__file__).resolve().parent / "desk_judge.py"), "run"],
                       capture_output=True, text=True, timeout=1500)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "judge failed")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    return collect.Result(out["detail"], complete=not out.get("no_key") and not out.get("failed"))


def collect_comments_judge() -> collect.Result:
    """Sort changed comment threads into "waiting on you" and FYI, with draft replies, in the desk's own Python environment."""
    if not VENV_PYTHON.exists():
        return collect.Result("the judge's Python environment is missing", complete=False)
    r = subprocess.run([str(VENV_PYTHON), str(Path(__file__).resolve().parent / "desk_comments.py"), "judge"],
                       capture_output=True, text=True, timeout=1500)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "comments judge failed")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    return collect.Result(out["detail"], complete=not out.get("no_key") and not out.get("failed"))


def collect_sessions_judge() -> collect.Result:
    """Summarise Claude Code and Codex sessions that have newly stopped for Peter, in the desk's own Python environment."""
    if not VENV_PYTHON.exists():
        return collect.Result("the judge's Python environment is missing", complete=False)
    r = subprocess.run([str(VENV_PYTHON), str(Path(__file__).resolve().parent / "desk_sessions.py"), "judge"],
                       capture_output=True, text=True, timeout=1500)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "sessions judge failed")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    return collect.Result(out["detail"], complete=not out.get("no_key") and not out.get("failed"))


# Every collector the service runs. A None label keeps a source off the page's strip.
SOURCES = {**collect.FACTS, **arrivals.ARRIVALS, "judge": (collect_judge, 60, None),
           "comments_sweep": (comments.sweep, 12 * 3600, "Older Docs"), "comments_judge": (collect_comments_judge, 60, None),
           "sessions_judge": (collect_sessions_judge, 20, None)}
SOURCES["prune"] = (*arrivals.ARRIVALS["prune"][:2], None)


# Feature modules (scripts/desk_<name>.py), each optional. A module may define:
#   ROUTES      {"/api/<name>/...": fn(body: dict) -> dict}   POST handlers; raise ValueError or RuntimeError for a 400
#   COLLECTORS  {name: (fn, seconds, label or None)}          run by the scheduler like the desk's own collectors
#   sync_info() -> dict                                       sent to the open page on every sync, as ext[<name>]
def _load_extensions() -> dict:
    import importlib
    out = {}
    for name in ("dayplan", "quickadd", "threads", "linear", "slacktodos"):
        try:
            out[name] = importlib.import_module(f"desk_{name}")
        except ModuleNotFoundError as e:
            if e.name != f"desk_{name}":
                raise
    return out


EXTENSIONS = _load_extensions()
EXT_ROUTES = {path: fn for mod in EXTENSIONS.values() for path, fn in getattr(mod, "ROUTES", {}).items()}
for _mod in EXTENSIONS.values():
    SOURCES.update(getattr(_mod, "COLLECTORS", {}))


def ext_info() -> dict:
    out = {}
    for name, mod in EXTENSIONS.items():
        if hasattr(mod, "sync_info"):
            try:
                out[name] = mod.sync_info()
            except Exception as e:  # one module's failure mustn't stop the page syncing
                out[name] = {"error": f"{type(e).__name__}: {e}"}
    return out


# ---------------------------------------------------------------- storage

def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def save(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


# ---------------------------------------------------------------- project snoozes
# projects.json: {"run": <desk run>, "snoozes": {project: ISO time, or null for "until next refresh"},
#                 "reviewed": [projects looked at since the run]}.
# A new desk run (a new desk.json header) wakes the until-refresh snoozes and starts a fresh review.

def desk_run() -> str:
    h = load(DESK / "desk.json", {}).get("header", {})
    return f'{h.get("title", "")} | {h.get("since", "")}'


def project_state() -> dict:
    with LOCK:
        st = load(PROJECTS, {})
        run, now = desk_run(), dt.datetime.now(dt.timezone.utc)
        snoozes = {k: v for k, v in (st.get("snoozes") or {}).items()
                   if v is None or dt.datetime.fromisoformat(v) > now}
        reviewed = st.get("reviewed") or []
        if st.get("run") != run:
            snoozes = {k: v for k, v in snoozes.items() if v is not None}
            reviewed = []
        new = {"run": run, "snoozes": snoozes, "reviewed": reviewed}
        if new != st:
            save(PROJECTS, new)
        return new


def project_action(name: str, action: str, until) -> dict:
    """Snooze a project (until an ISO time, or the next desk run when until is None), wake it, or mark it reviewed."""
    if not name or len(name) > 200 or action not in ("snooze", "wake", "reviewed"):
        raise ValueError("bad project action")
    if action == "snooze" and until is not None:
        t = dt.datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        if t.tzinfo is None or t <= dt.datetime.now(dt.timezone.utc):
            raise ValueError("snooze time must be in the future, with a time zone")
        until = t.astimezone(dt.timezone.utc).isoformat()
    st = project_state()
    with LOCK:
        if action == "snooze":
            st["snoozes"][name] = until
            st["reviewed"] = sorted(set(st["reviewed"]) | {name})
        elif action == "wake":
            st["snoozes"].pop(name, None)
        else:
            st["reviewed"] = sorted(set(st["reviewed"]) | {name})
        save(PROJECTS, st)
    return {"ok": True, **st}


def token() -> str:
    return (DESK / ".token").read_text().strip()


def desk_items() -> dict[str, dict]:
    """Every item on the page, including to-dos and unassigned tasks added since the last run."""
    return live.item_index(live.live_desk())


# ---------------------------------------------------------------- the live page
_composed: tuple[tuple, dict] | None = None
_compose_lock = threading.Lock()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def composed() -> dict:
    """The page's regions and issue data as of now, rebuilt only when something they show has changed."""
    global _composed
    health = collect.health(SOURCES)
    held = arrivals.held()
    key = (_mtime(DESK / "desk.json"), _mtime(DESK / "publication.json"), _mtime(DESK / "archived.json"), _mtime(DESK.parent / "state.json"),
           tuple(sorted(store.snapshot_hashes().items())), tuple((u["id"], u["urgent"]) for u in held if u["vip_since"]),
           int(time.time() // 60), tuple((h["name"], h["state"], int((h["last_ok"] or 0) // 60)) for h in health))
    with _compose_lock:
        if _composed and _composed[0] == key:
            return _composed[1]
        rg, peek = render_desk.compose(live.live_desk(), DESK, health)
        out = {"regions": {k: {"hash": render_desk.rhash(v), "html": v} for k, v in rg.items()},
               "issues": peek, "issues_hash": store.digest(peek), "rg": rg}
        out["arrivals"] = arrivals.summary(held)
        _composed = (key, out)
        return out


def arrivals_html() -> str:
    """What is behind the pill: each held conversation's latest message, as collected, grouped by source."""
    E = render_desk.E
    units = arrivals.held()
    groups: dict[str, list[dict]] = {}
    for u in units:
        if u["counted"]:
            groups.setdefault(arrivals.SOURCE_LABELS.get(u["source"], u["source"]), []).append(u)

    def row(u):
        when = dt.datetime.fromtimestamp(u["last_other_ts"])
        t = when.strftime("%H:%M") if when.date() == dt.date.today() else when.strftime("%a %H:%M")
        text = re.sub(r"\s+", " ", u["text"] or "")[:180]
        who = f'<b>{E(u["author"] or "Someone")}</b>'
        body = f'{who}<span class="arr-where">{E(u["label"])}</span><span class="arr-text">{E(text)}</span>'
        inner = f'<a href="{E(render_desk.safe_url(u["link"]))}" target="_blank" rel="noopener">{body}</a>' if u.get("link") else f"<span>{body}</span>"
        return f'<li class="{"arr-vip" if u["vip_since"] else ""}">{inner}<time>{E(t)}</time></li>'

    if not groups:
        return '<p class="arr-none">Nothing new for you since the last run.</p>'
    return "".join(f'<h3>{E(label)} <span>{len(us)}</span></h3><ol>{"".join(row(u) for u in us)}</ol>' for label, us in groups.items())


def sync(body: dict) -> dict:
    """What the open page needs to bring itself up to date: only the regions whose hash it doesn't have."""
    c = composed()
    have = body.get("hashes") or {}
    tasks = task_states()
    out = {"regions": {k: v for k, v in c["regions"].items() if have.get(k) != v["hash"]},
           "issues_hash": c["issues_hash"], "assets": render_desk.asset_rev(),
           "tasks": tasks, "refreshing": refreshing(tasks), "refresh": refresh_note(refresh_state()), "queue": load(QUEUE, []), "archived": load(ARCHIVED, []),
           "projects": project_state(), "checking": [n for n in (SCHEDULER.busy() if SCHEDULER else []) if SOURCES[n][2]],
           "arrivals": arrivals.summary(), "comments_hash": comments.view_hash(), "sessions_hash": sessions.view_hash(),
           "ext": ext_info()}
    if body.get("issues_hash") != c["issues_hash"]:
        out["issues"] = c["issues"]
    return out


# ---------------------------------------------------------------- handoffs
RULES = (
    f"Working folder: {ROOT}. Follow its AGENTS.md and team-repo/AGENTS.md, "
    "but skip the start-of-conversation orientation and go straight to this task. "
    "Never send, post, comment or publish anything (Slack, email, Signal, Google Doc comments, Linear comments seen by others) "
    "without first showing Peter the exact text and getting his approval. "
    "When you have something for Peter to look at or decide, stop and say so briefly."
)
NO_NOTE = ("Peter gave no instructions. Work out what this item needs, do the safe preparatory work "
           "(reading, checking, drafting), and propose the next step.")
RULES, NO_NOTE = RULES.replace("Peter", FIRST), NO_NOTE.replace("Peter", FIRST)  # written for Peter; config.json's "me" names the owner


def short_title(text: str, prefix: str) -> str:
    words, out = text.replace(":", " ").split(), prefix
    for w in words:
        if len(out) + 1 + len(w) > 20:
            break
        out += " " + w
    return out[:20]


def describe(key: str, label: str) -> tuple[str, str]:
    """Return (handoff text for one item, tab title)."""
    if key.startswith("issue:"):
        ident = key.split(":", 1)[1]
        title_text = label.split(" ", 1)[1] if " " in label else ""
        return (f"Linear issue {ident} ({title_text}) in the Linear team. Read it, its project and comments "
                f"through the task-manager skill first.", short_title(title_text, ident))
    if key.startswith(("comment:", "comments-file:")):
        return comments.describe(key)
    if key.startswith("project:"):
        name = key.split(":", 1)[1]
        p = desk_projects.find(name)
        if not p:
            raise ValueError(f"unknown project {name}; regenerate the desk")
        lines = [f"Project \"{name}\". Read its folder in team-repo (the projects index in team-repo/README.md) "
                 f"and its open Linear tasks through the task-manager skill first."]
        for field in ("now", "next", "risk"):
            if p.get(field):
                lines.append(f"{field.capitalize()} (from the desk): {p[field]}")
        return "\n".join(lines), short_title(name, "").strip() or name[:20]
    it = desk_items().get(key)
    if not it:
        raise ValueError(f"unknown desk item {key}; regenerate the desk")
    lines = [f"Desk item #{it['n']} ({it['section']}, {it.get('source_label') or it.get('source') or 'desk'}): "
             f"{it.get('head', '')} {it.get('text', '')}".strip()]
    if it.get("link"):
        lines.append(f"Link: {it['link']}")
    if it.get("context"):
        lines.append(f"Context: {it['context']}")
    return "\n".join(lines), it.get("title") or short_title(it.get("head") or it.get("text", ""), f"#{it['n']}")


def handoff(blocks: list[tuple[str, str]], generated: str, marker: str) -> str:
    parts = [f"You're picking up work from {FIRST}'s Forethought desk (generated {generated})."]
    for i, (desc, note) in enumerate(blocks, 1):
        head = f"Item {i} of {len(blocks)}. " if len(blocks) > 1 else ""
        parts.append(f"{head}{desc}\n{FIRST}'s instructions: {note or NO_NOTE}")
    if len(blocks) > 1:
        parts.append("Work through the items in order; finish or park each before starting the next.")
    parts.append(RULES)
    parts.append(f"(desk task {marker})")
    return "\n\n".join(parts)


def spawn(tasks: list[dict]) -> list[str]:
    """Open one Warp tab per task dict {title, prompt, marker}; return session ids."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("WARP_TERMINAL_SESSION_UUID", "TERM_PROGRAM", "WARP_IS_LOCAL_SHELL_SESSION",
                        "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "CODEX_THREAD_ID")}
    args = [sys.executable, str(SPAWN), "reserve", "--mode", "tab", "--count", str(len(tasks))]
    for t in tasks:
        args += ["--title", t["title"]]
    r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "reserve failed")
    reservation = json.loads(r.stdout.strip().splitlines()[0])["reservation"]
    manifest = [{"agent": "claude", "model": "opus", "account": "forethought", "dir": str(ROOT),
                 "title": t["title"], "prompt": t["prompt"],
                 **({"resume_session_id": t["resume"]} if t.get("resume") else {})} for t in tasks]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(manifest, f)
    r = subprocess.run([sys.executable, str(SPAWN), "fulfil", reservation, f.name],
                       capture_output=True, text=True, env=env, timeout=60)
    os.unlink(f.name)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "fulfil failed")
    ids = []
    log = SPAWN_LOG.read_text(errors="replace").splitlines() if SPAWN_LOG.exists() else []
    for t in tasks:
        sid = t.get("resume") or ""
        for line in ([] if sid or not t["marker"] else reversed(log)):
            if t["marker"] in line:
                sid = json.loads(line).get("session_id", "")
                break
        ids.append(sid)
    return ids


def start(items: list[dict], mode: str) -> dict:
    desk = load(DESK / "desk.json", {})
    generated = desk.get("header", {}).get("title", "")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    groups = [items] if mode == "one" and len(items) > 1 else [[i] for i in items]
    jobs = []
    for g in groups:
        described = [(describe(i["key"], i.get("label", "")), i.get("note", "").strip()) for i in g]
        marker = uuid.uuid4().hex[:12]
        title = described[0][0][1] if len(g) == 1 else f"desk batch ({len(g)})"
        jobs.append({"title": title, "marker": marker, "items": g,
                     "prompt": handoff([(d[0], note) for d, note in described], generated, marker)})
    ids = spawn(jobs)
    with LOCK:
        tasks = load(TASKS, [])
        queue = load(QUEUE, [])
        started = {i["key"] for i in items}
        for job, sid in zip(jobs, ids):
            notes = [i.get("note", "").strip() for i in job["items"] if i.get("note", "").strip()]
            tasks.append({"id": job["marker"], "session_id": sid, "title": job["title"],
                          "label": job["items"][0].get("label") if len(job["items"]) == 1 else f"{len(job['items'])} desk items",
                          "keys": [i["key"] for i in job["items"]], "note": notes[0] if len(notes) == 1 else "",
                          "started_at": now})
        save(TASKS, tasks)
        save(QUEUE, [q for q in queue if q["key"] not in started])
    n = len(jobs)
    return {"message": f"Opened {n} Claude Code tab{'s' if n != 1 else ''}: " + ", ".join(j["title"] for j in jobs)}


def quick_agent(prompt: str) -> dict:
    """Quick agent (A): a new Claude Code tab in Warp on the working folder, as `cc f` opens, with Peter's prompt exactly as typed."""
    prompt = prompt.strip()
    if not prompt or len(prompt) > 20000:
        raise ValueError("the prompt is empty or too long")
    title = short_title(prompt.splitlines()[0], "").strip() or "quick agent"
    if os.environ.get("FT_DESK_AGENT_DRY_RUN"):
        return {"message": f"Would open a Claude Code tab: {title}", "title": title, "prompt": prompt}
    spawn([{"title": title, "marker": "", "prompt": prompt}])
    return {"message": f"Opened a Claude Code tab: {title}", "title": title}


REFRESH_KEY = "desk:refresh"

# Refresh runs the full desk as a Claude Code background session (`claude --bg`), so no Warp tab opens.
# desk/refresh.json holds the run: {job, session_id, started_at, state, reason, detail}. State is one of
# running, done, blocked (waiting on a permission prompt or question), problem (finished, but reported a failed
# step), stopped (finished without re-rendering the page), failed (the session died or never started),
# handed (opened in Warp) or dismissed. The page shows every state but running, done, handed and dismissed
# as a note under the header, with Open in Warp, Run again in Warp and Dismiss.
REFRESH = DESK / "refresh.json"
REFRESH_LOCK = threading.Lock()
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
CLAUDE_HOME = Path(os.environ.get("FT_DESK_CLAUDE_HOME", Path.home() / ".claude"))  # the Forethought account's config dir
REFRESH_SLOW = 45 * 60
OK_LINE, PROBLEM_LINE = "DESK REFRESH OK", "DESK REFRESH PROBLEM:"
WAITING_STATES = {"blocked", "needs_input", "input_required", "permission_pending", "waiting"}
NOTE_STATES = {"blocked", "problem", "stopped", "failed"}


def claude_env() -> dict:
    """The environment for a Claude Code process on the Forethought account, free of this process's session."""
    return {k: v for k, v in os.environ.items()
            if k not in ("WARP_TERMINAL_SESSION_UUID", "TERM_PROGRAM", "WARP_IS_LOCAL_SHELL_SESSION",
                         "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "CODEX_THREAD_ID")}


def claude(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run([CLAUDE, *args], capture_output=True, text=True, env=claude_env(), cwd=str(ROOT),
                          stdin=subprocess.DEVNULL, timeout=timeout)


_jobs_cache: tuple[float, dict | None] = (0.0, None)


def bg_jobs() -> dict | None:
    """Claude Code's background sessions by short id, or None when the list can't be read."""
    global _jobs_cache
    if time.time() - _jobs_cache[0] < 3:
        return _jobs_cache[1]
    try:
        r = claude("agents", "--json", "--all", timeout=20)
        out = {j["id"]: j for j in json.loads(r.stdout) if j.get("id")} if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        out = None
    _jobs_cache = (time.time(), out)
    return out


def job_detail(job: str) -> str:
    """Claude Code's own one-line account of where a background session got to."""
    try:
        return str(json.loads((CLAUDE_HOME / "jobs" / job / "state.json").read_text()).get("detail") or "").strip()
    except (OSError, ValueError):
        return ""


def last_reply(session_id: str) -> str:
    """The text of the session's last reply, from its transcript."""
    if not session_id:
        return ""
    for f in (CLAUDE_HOME / "projects").glob(f"*/{session_id}.jsonl"):
        try:
            lines = f.read_text(errors="replace").splitlines()[-400:]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("type") != "assistant":
                continue
            text = "".join(c.get("text", "") for c in (m.get("message") or {}).get("content") or [] if c.get("type") == "text")
            if text.strip():
                return text.strip()
    return ""


def end_job(job: str, wait: bool = False) -> None:
    """Stop a background session and take it off Claude Code's list; its conversation is kept."""
    def go():
        for cmd in (("stop", job), ("rm", job)):
            try:
                claude(*cmd, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass
    go() if wait else threading.Thread(target=go, daemon=True).start()


def started_ts(r: dict) -> float:
    return dt.datetime.fromisoformat(r["started_at"]).timestamp()


def refresh_state() -> dict:
    """The refresh run, brought up to date with its background session."""
    with REFRESH_LOCK:
        r = load(REFRESH, {})
        if r.get("state") not in ("running", "blocked"):
            return r
        jobs = bg_jobs()
        if jobs is None:
            return r  # can't tell this time; keep the last answer
        j = jobs.get(r["job"])
        age = time.time() - started_ts(r)
        before = dict(r)  # before any change, so a newly learnt session id is saved too (Open in Warp needs it)
        if j and not r.get("session_id"):
            r["session_id"] = j.get("sessionId", "")
        status, st = (j or {}).get("status", ""), (j or {}).get("state", "")
        if j is None:
            if age > 60 and page_mtime() >= started_ts(r):  # gone from Claude Code's list, but it re-rendered the page
                r.update(state="done", reason="", detail="")
            elif age > 60:
                r.update(state="failed", reason="The background Claude Code session ended before it finished, "
                                                "so the desk wasn't updated.", detail=job_detail(r["job"]))
        elif status == "waiting" or st in WAITING_STATES:
            r.update(state="blocked", reason="Claude stopped at a permission prompt or a question that nobody "
                                             "could answer in the background. Open it in Warp to answer, and it carries on.",
                     detail=job_detail(r["job"]))
        elif st == "failed":
            r.update(state="failed", reason="Claude Code reported that the background session failed.",
                     detail=job_detail(r["job"]))
        elif st == "done":
            reply = last_reply(r.get("session_id", ""))
            tail = reply.splitlines()[-1].strip().strip("`") if reply else ""
            if tail.startswith(PROBLEM_LINE):
                r.update(state="problem", reason=tail[len(PROBLEM_LINE):].strip(), detail="")
            elif page_mtime() >= started_ts(r):
                r.update(state="done", reason="", detail="")
                end_job(r["job"])
            else:
                r.update(state="stopped", reason="Claude finished without updating the desk page.",
                         detail=tail or job_detail(r["job"]))
        else:
            r["state"] = "running"
            r["slow"] = age > REFRESH_SLOW
        if r != before:
            if r["state"] != "running":
                r["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save(REFRESH, r)
        return r


def watch_refresh() -> None:
    """Follow a background refresh from the server, so it is tracked and closed even with no page open."""
    def go():
        while load(REFRESH, {}).get("state") in ("running", "blocked"):
            time.sleep(10)
            try:
                refresh_state()
            except Exception as e:  # keep watching; the page shows the last state
                print(f"refresh watch: {e}", file=sys.stderr)
    threading.Thread(target=go, daemon=True).start()


def refresh_note(r: dict) -> dict | None:
    """What the page shows under its header about the refresh run, if anything."""
    state = r.get("state")
    if state == "running" and r.get("slow"):
        mins = int((time.time() - started_ts(r)) // 60)
        return {"state": "slow", "title": "Refresh is taking longer than usual",
                "reason": f"It started {mins} minutes ago and is still running. Open it in Warp to see where it is.",
                "detail": job_detail(r["job"]), "can_open": True}
    if state not in NOTE_STATES:
        return None
    titles = {"blocked": "Refresh is waiting for you", "problem": "Refresh finished with a problem",
              "stopped": "Refresh stopped before updating the desk", "failed": "Refresh failed"}
    return {"state": state, "title": titles[state], "reason": r.get("reason", ""), "detail": r.get("detail", ""),
            "can_open": bool(r.get("session_id"))}


def warp_refreshing(tasks: list[dict]) -> bool:
    """A refresh in a Warp tab is under way until its session renders the page; the tab may stay open after that."""
    for t in tasks:
        if t.get("keys") != [REFRESH_KEY] or t["status"] not in ("starting", "working"):
            continue
        if page_mtime() < dt.datetime.fromisoformat(t["started_at"]).timestamp():
            return True
    return False


def refreshing(tasks: list[dict]) -> bool:
    return refresh_state().get("state") == "running" or warp_refreshing(tasks)


def refresh_prompt(marker: str, background: bool) -> str:
    p = (f"{FIRST} pressed Refresh on the Forethought desk page. Run the ft-desk skill now in standalone mode, all of it: "
         "gather, read and judge, write desk.json, render it and make sure the desk server is running, then track and "
         "commit" + ("" if background else ", and give the chat summary") + ". The open desk page reloads itself when "
         "index.html changes, so there's no need to open it.")
    if background:
        p += (f"\n\nThis session runs in the background with nobody watching, so don't ask {FIRST} anything, and skip the "
              "chat summary: nobody reads it, and the page carries everything. If a step fails or is blocked, carry on "
              f"with the rest where you can. Your final reply is a single line: `{OK_LINE}` if every step worked, or "
              f"`{PROBLEM_LINE}` followed by one or two plain sentences on what failed and what {FIRST} needs to do. "
              "The desk page shows him that line.")
    return p + f"\n\n(desk task {marker})"


def track_refresh_tab(marker: str, sid: str) -> None:
    with LOCK:
        tasks = load(TASKS, [])
        tasks.append({"id": marker, "session_id": sid, "title": "desk refresh", "label": "Desk refresh",
                      "keys": [REFRESH_KEY], "note": "", "started_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        save(TASKS, tasks)


def retire_refresh(state: str, wait: bool = False) -> None:
    """End the current background run, if any, and record why."""
    with REFRESH_LOCK:
        r = load(REFRESH, {})
        if r.get("job") and r.get("state") in ("running", "blocked", "problem", "stopped"):
            end_job(r["job"], wait)
        if r:
            r["state"] = state
            save(REFRESH, r)


_refresh_start = threading.Lock()


def refresh_desk() -> dict:
    """Run the full desk again in a Claude Code background session; the page reloads when it re-renders."""
    if not _refresh_start.acquire(blocking=False):  # a double click or a second window: one run only
        return {"message": "The desk is already refreshing"}
    try:
        return _refresh_desk()
    finally:
        _refresh_start.release()


def _refresh_desk() -> dict:
    if refreshing(task_states()):
        return {"message": "The desk is already refreshing"}
    retire_refresh("dismissed")
    marker = uuid.uuid4().hex[:12]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        r = claude("--bg", "--settings", '{"fastMode":true}', "--model", "opus", "-n", "desk refresh",
                   refresh_prompt(marker, True), timeout=90)
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"backgrounded\W+([0-9a-f]{6,})", out)
        err = "" if m else (out.strip().splitlines() or ["no output"])[-1]
    except (OSError, subprocess.TimeoutExpired) as e:
        m, err = None, str(e)
    if not m:
        save(REFRESH, {"job": "", "session_id": "", "started_at": now, "ended_at": now, "state": "failed",
                       "reason": "Claude Code couldn't start a background session.", "detail": err})
        return {"message": "Refresh couldn't start: see the note under the header"}
    save(REFRESH, {"job": m.group(1), "session_id": "", "started_at": now, "state": "running", "marker": marker})
    watch_refresh()
    return {"message": "Refreshing the desk in the background"}


def refresh_in_warp() -> dict:
    """Run the full desk again in a new Warp tab, as Refresh did before it ran in the background."""
    retire_refresh("handed")
    marker = uuid.uuid4().hex[:12]
    sid = spawn([{"title": "desk refresh", "marker": marker, "prompt": refresh_prompt(marker, False)}])[0]
    track_refresh_tab(marker, sid)
    return {"message": "Refreshing the desk in a new Claude Code tab"}


def open_refresh() -> dict:
    """Stop the background run and resume its conversation in a Warp tab, so Peter can see it and unblock it."""
    r = refresh_state()
    sid = r.get("session_id") or ""
    if not sid:
        return refresh_in_warp()
    was = r.get("state")
    retire_refresh("handed", wait=True)  # the session must be stopped before it is resumed
    if was == "blocked":
        prompt = ("This desk refresh was running in the background and stopped at a permission prompt or question that "
                  f"nobody could answer there. {FIRST} has opened it here. Retry the step that was waiting so he can answer "
                  "it, then finish the refresh as the skill says.")
    elif was == "running":
        prompt = (f"{FIRST} has moved this desk refresh from the background into this tab. Carry on from where you were "
                  "and finish the refresh as the skill says.")
    else:
        prompt = (f"{FIRST} opened this desk refresh from the desk page, which reported: "
                  f"{r.get('reason') or 'a problem'} {r.get('detail') or ''}".strip()
                  + f"\n\nSay in two or three sentences what went wrong and what you need from him, then wait.")
    marker = uuid.uuid4().hex[:12]
    spawn([{"title": "desk refresh", "marker": marker, "prompt": prompt + f"\n\n(desk task {marker})", "resume": sid}])
    track_refresh_tab(marker, sid)
    return {"message": "Opened the refresh in a Claude Code tab"}


def page_mtime() -> float:
    try:
        return (DESK / "index.html").stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------- status and focus
_panes_cache: tuple[float, dict] = (0.0, {})


def panes_by_session() -> dict[str, dict]:
    global _panes_cache
    if time.time() - _panes_cache[0] < 2:
        return _panes_cache[1]
    try:
        r = subprocess.run([str(AGENT_PANES), "--json", "--all"], capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout)
        out = {p["session_id"]: p for p in data.get("panes", []) if p.get("session_id")}
    except (OSError, ValueError, subprocess.SubprocessError):
        out = _panes_cache[1]
    _panes_cache = (time.time(), out)
    return out


def task_states() -> list[dict]:
    panes = panes_by_session()
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for t in load(TASKS, []):
        started = dt.datetime.fromisoformat(t["started_at"])
        p = panes.get(t.get("session_id") or "")
        since = t["started_at"]
        if not p:
            status = "starting" if (now - started).total_seconds() < 180 else "closed"
        else:
            since = p.get("last_event_at") or since
            if p["status"] == "closed":
                status = "closed"
            elif p["status"] in ("ended", "dead"):
                status = "done"
            elif p.get("last_event") == "Stop":
                status = "waiting"
            elif (p.get("last_event") == "SessionStart"
                  and dt.datetime.fromisoformat(since.replace("Z", "+00:00")) - started > dt.timedelta(minutes=3)):
                status = "waiting"  # resumed or compacted, with no prompt since
            else:
                status = "working"
        if status == "closed" and (now - started).total_seconds() > 12 * 3600:
            continue
        if (now - started).total_seconds() > 3 * 86400:
            continue
        out.append({**t, "status": status, "since": since, "pane_uuid": (p or {}).get("pane_uuid")})
    return out


def focus(session_id: str) -> str:
    p = panes_by_session().get(session_id)
    if not p or p.get("status") == "closed":
        raise ValueError("That tab is no longer open")
    spec = importlib.util.spec_from_file_location("spawn_agent_panes", SPAWN)
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    db = next((d for d in sp.warp_database_candidates() if d.is_file()), None)
    if not db:
        raise ValueError("Warp's session database wasn't found")
    origin = sp.WarpTabOrigin(database=db, session_uuid=p["pane_uuid"])
    subprocess.run(["osascript", "-e", 'tell application "Warp" to activate'], capture_output=True, timeout=10)
    for _ in range(40):  # wait until Warp is frontmost before any menu clicks
        r = subprocess.run(["osascript", "-e", 'tell application "System Events" to return frontmost of process "Warp"'],
                           capture_output=True, text=True, timeout=5)
        if r.stdout.strip() == "true":
            break
        time.sleep(0.05)
    else:
        raise ValueError("Warp didn't come to the front")
    time.sleep(0.15)
    state = sp.read_warp_tab_state(db, p["pane_uuid"], allow_inactive=True)
    if state.window_count > 1 and state.active_window_id != state.origin_window_id:
        warning = sp.focus_warp_window(origin)
        if warning:
            raise ValueError("Couldn't switch Warp window: " + warning)
        state = sp.read_warp_tab_state(db, p["pane_uuid"], allow_inactive=True)
    if state.active_index != state.origin_index:
        r = subprocess.run(["osascript", "-e", sp.build_tab_restore_applescript(), str(state.origin_index),
                            str(state.active_index), str(state.tab_count)], capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise ValueError("Couldn't switch Warp tab: " + r.stderr.strip())
    return ""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ft-desk"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self._cross_origin():
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)

    def _cross_origin(self) -> bool:
        """A request from a page another site serves, to a route that site may call."""
        origin = self.headers.get("Origin")
        return bool(origin) and origin in CROSS_ORIGIN_ROUTES.get(self.path.split("?")[0], set())

    def _guard(self, api: bool) -> bool:
        if self.headers.get("Host") not in HOSTS:
            self._send(403, {"error": "bad host"})
            return False
        if api and self.command == "POST" and self._cross_origin():
            return True
        try:
            expected = token()
        except OSError:  # no token yet: a page must load first
            expected = None
        if api and (not expected or self.headers.get("X-Desk-Token") != expected):
            self._send(403, {"error": "bad token; reload the page"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in HOSTS}:
            self._send(403, {"error": "bad origin"})
            return False
        return True

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            if not self._guard(False):
                return
            if not (DESK / "desk.json").exists():
                return self._send(404, b"No desk yet. Run the ft-desk skill.", "text/plain")
            try:
                c = composed()
                html_text = render_desk.page(c["rg"], c["issues"], render_desk.ensure_token(DESK), project_state())
            except Exception:  # the store or a renderer failed: serve the last page a run wrote
                import traceback
                traceback.print_exc()
                page = DESK / "index.html"
                if not page.exists():
                    return self._send(500, b"The desk couldn't be built; see server.log.", "text/plain")
                html_text = page.read_text()
            return self._send(200, html_text.encode(), "text/html; charset=utf-8")
        if path == "/shadow":
            if not self._guard(False):
                return
            return self._send(200, shadow.page(render_desk.ensure_token(DESK)).encode(), "text/html; charset=utf-8")
        if path == "/api/state":
            if not self._guard(True):
                return
            return self._send(200, {"tasks": task_states(), "queue": load(QUEUE, []), "archived": load(ARCHIVED, []),
                                    "page_mtime": page_mtime(), "self_render": False})  # for a page loaded before the live desk
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard(True):
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, {"error": "JSON only"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 200_000)) or b"{}")
            path = self.path.split("?")[0]
            if path == "/api/start":
                items = [{"key": str(i["key"]), "note": str(i.get("note") or "")[:4000], "label": str(i.get("label") or "")[:200]}
                         for i in body.get("items", [])][:12]
                if not items:
                    return self._send(400, {"error": "nothing to start"})
                return self._send(200, start(items, "one" if body.get("mode") == "one" else "each"))
            if path == "/api/agent":
                prompt = str(body.get("prompt") or "")
                if self._cross_origin():
                    if not prompt.strip() or len(prompt) > 20000:
                        raise ValueError("the prompt is empty or too long")
                    try:
                        cross_origin_allowance()
                    except ValueError as e:
                        return self._send(429, {"error": str(e)})
                return self._send(200, quick_agent(prompt))
            if path == "/api/queue":
                with LOCK:
                    q = [x for x in load(QUEUE, []) if x["key"] != body["key"]]
                    q.append({"key": str(body["key"]), "note": str(body.get("note") or "")[:4000], "label": str(body.get("label") or "")[:200]})
                    save(QUEUE, q)
                return self._send(200, {"ok": True})
            if path == "/api/unqueue":
                with LOCK:
                    save(QUEUE, [x for x in load(QUEUE, []) if x["key"] != body.get("key")])
                return self._send(200, {"ok": True})
            if path == "/api/sync":
                return self._send(200, sync(body))
            if path == "/api/arrivals":
                return self._send(200, {"html": arrivals_html()})
            if path == "/api/arrivals/dismiss":
                c = body.get("comment")
                return self._send(200, arrivals.dismiss(str(body.get("unit") or ""), bool(body.get("undo")), c if isinstance(c, str) else None))
            if path == "/api/mark":
                return self._send(200, shadow.mark(str(body.get("unit") or ""), str(body.get("verdict") or ""), str(body.get("note") or "")[:2000]))
            if path == "/api/check":
                if SCHEDULER:
                    SCHEDULER.check_now()
                return self._send(200, {"message": "Checking every source"})
            if path == "/api/refresh":
                return self._send(200, refresh_desk())
            if path == "/api/refresh/open":
                return self._send(200, open_refresh())
            if path == "/api/refresh/warp":
                return self._send(200, refresh_in_warp())
            if path == "/api/refresh/dismiss":
                retire_refresh("dismissed")
                return self._send(200, {"ok": True})
            if path == "/api/keep":
                return self._send(200, keep(str(body.get("key") or "")))
            if path == "/api/archive":
                return self._send(200, archive([str(k) for k in body.get("keys", [])]))
            if path == "/api/move":
                return self._send(200, move(str(body.get("issue") or ""), str(body.get("column") or "")))
            if path == "/api/edit":
                return self._send(200, edit_issue(str(body.get("issue") or ""), body.get("title"), body.get("description")))
            if path == "/api/add":
                return self._send(200, add_issue(str(body.get("project") or ""), str(body.get("column") or ""),
                                                 str(body.get("title") or ""), str(body.get("description") or ""), bool(body.get("force"))))
            if path == "/api/comment":
                return self._send(200, comment(str(body.get("issue") or ""), str(body.get("body") or "")))
            if path == "/api/project":
                return self._send(200, project_action(str(body.get("name") or ""), str(body.get("action") or ""), body.get("until")))
            if path == "/api/comments":
                return self._send(200, {**comments.view(), "hash": comments.view_hash()})
            if path == "/api/comments/doc":
                return self._send(200, comments.doc_context(str(body.get("file") or "")))
            if path == "/api/comments/act":
                return self._send(200, comments.act(str(body.get("key") or ""), str(body.get("action") or ""), str(body.get("text") or "")))
            if path == "/api/sessions":
                v = sessions.view()
                return self._send(200, {**v, "hash": sessions.view_hash(v)})
            if path == "/api/sessions/send":
                return self._send(200, sessions.send(str(body.get("session_id") or ""), str(body.get("text") or "")[:8000], focus))
            if path == "/api/sessions/mark":
                return self._send(200, sessions.mark(str(body.get("session_id") or ""), str(body.get("action") or ""), body.get("value")))
            if path == "/api/focus":
                return self._send(200, {"message": focus(str(body.get("session_id") or ""))})
            if path in EXT_ROUTES:
                return self._send(200, EXT_ROUTES[path](body))
            return self._send(404, {"error": "not found"})
        except (ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # report it, rather than dropping the connection ("Load failed" in the page)
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_OPTIONS(self):
        if self.headers.get("Host") not in HOSTS or not self._cross_origin():
            return self._send(405, {"error": "no CORS"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()


# ---------------------------------------------------------------- archive
def keep(key: str) -> dict:
    """Make a let-go item an open item again, so it stays under Still open."""
    it = desk_items().get(key)
    if not it:
        raise ValueError("That item is no longer on the desk")
    summary = " ".join(x for x in (it.get("head", ""), it.get("text", "")) if x)[:300] or key
    args = [str(FT), "items", "add", key, summary]
    if it.get("source"):
        args += ["--source", it["source"]]
    if it.get("link"):
        args += ["--link", it["link"]]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "items add failed")
    return {"message": "Kept: it's under Still open"}


def archive(keys: list[str]) -> dict:
    """Archive desk items: Gmail threads leave the inbox, Slack to-dos are ticked off in Slack; every item is
    marked done so later runs skip it."""
    import re
    keys = [k for k in keys if k][:200]
    noise = [k for k in keys if re.fullmatch(r"[0-9a-f]{16}", k)]           # the "Archive as noise?" list
    mail = [k for k in keys if re.fullmatch(r"gmail:[0-9a-f]{16}", k)]      # email items Peter chose to archive
    refused = []

    def run(args):
        r = subprocess.run([str(FT), *args], capture_output=True, text=True, timeout=120)
        for line in (r.stdout + r.stderr).splitlines():
            if line.startswith("Refused") or "rror" in line:
                refused.append(line.strip())
        return r.returncode == 0

    # An item is marked done only once its thread has left the inbox: a refused or failed archive stays open.
    failed: set[str] = set()
    for group, args in ((noise, ["archive", *noise]), (mail, ["archive", "--judged", *[k.split(":", 1)[1] for k in mail]])):
        if not group:
            continue
        ids = {k: store.action_start("gmail-archive", k) for k in group}
        before = len(refused)
        ok = run(args)
        named = {k for k in group if any(k.split(":")[-1] in r for r in refused[before:])}
        if not ok and not named:
            named = set(group)
            refused.append("the archive command failed; nothing was archived")
        failed |= named
        for k, aid in ids.items():
            store.action_finish(aid, k not in named, "" if k not in named else "refused or failed")
    for k in [k for k in keys if k.startswith("slack-todo:")]:
        aid = store.action_start("slack-todo-done", k)
        try:
            EXTENSIONS["slacktodos"].complete(k)
            store.action_finish(aid, True, "")
        except Exception as e:  # noqa: BLE001 - shown to Peter; the item stays open
            failed.add(k)
            refused.append(f"Slack to-do not ticked: {e}")
            store.action_finish(aid, False, str(e)[:200])
    done = [k for k in keys if k not in failed]
    if done:
        run(["items", "done", *done])
    with LOCK:
        save(ARCHIVED, sorted(set(load(ARCHIVED, [])) | set(done)))
    return {"archived": done, "refused": refused}


# ---------------------------------------------------------------- Linear changes
# The page sends its Linear changes to /api/linear/write (desk_linear.py, through its offline outbox). These three
# routes keep the old contract for a page loaded before it: an error for anything Linear refused.
LINEAR_SCRIPT = cfg_path("linear_tasks")


def _legacy(identifier: str, kind: str, **fields) -> dict:
    import desk_linear
    r = desk_linear.write({"id": uuid.uuid4().hex, "issue": identifier, "kind": kind, **fields})
    if r["status"] == "refused":
        raise RuntimeError(r["error"])
    return r


def move(identifier: str, column: str) -> dict:
    r = _legacy(identifier, "update", set={"column": column})
    return {"ok": True, "state": r.get("state") or column}


def edit_issue(identifier: str, title, description) -> dict:
    data = {k: v for k, v in (("title", title), ("description", description)) if v is not None}
    _legacy(identifier, "update", set=data)
    return {"ok": True, **data}


def comment(identifier: str, text: str) -> dict:
    r = _legacy(identifier, "comment", body=text)
    return {"ok": True, "who": r.get("who") or FIRST, "at": r.get("at") or dt.datetime.now(dt.timezone.utc).isoformat()}


# Board column -> the task-manager's status name for a new task
ADD_STATUSES = {"Triage": "Unprocessed", "Backlog": "Backlog", "Todo": "Not started", "In Progress": "In progress", "Waiting": "Waiting"}


def add_issue(project: str, column: str, title: str, description: str, force: bool) -> dict:
    """Create a task for Peter on a project's board, through the task-manager's CLI (its labels, checks and duplicate guard)."""
    title, description = title.strip(), description.strip()
    p = desk_projects.find(project)
    p = p if p and p.get("id") else None
    if not p or column not in ADD_STATUSES or not title or len(title) > 300 or len(description) > 20000:
        raise ValueError("bad project, column or title")
    item = {"title": title, "description": description or "Added by Peter from the desk.", "project": p["id"],
            "who": LINEAR_WHO, "status": ADD_STATUSES[column]}
    r = subprocess.run([sys.executable, str(LINEAR_SCRIPT), "add", "--stdin", "--json", *(["--force"] if force else [])],
                       input=json.dumps([item]), capture_output=True, text=True, timeout=90)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "linear-tasks add failed")
    res = json.loads(r.stdout)[0]
    t = res.get("task") or {}
    ident = t.get("identifier") or t.get("id") or ""
    if res.get("skipped"):
        return {"ok": False, "similar": {"id": ident, "title": t.get("title", "")}}
    store.set_meta("linear_dirty", True)
    store.set_due("linear", time.time() + 1)
    return {"ok": True, "id": ident, "title": t.get("title", title)}


def listening() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "serve"
    DESK.mkdir(parents=True, exist_ok=True)
    if cmd == "serve":
        # Bind first: a second copy that loses the race for the port must exit before it writes the
        # PID file (which `stop` reads) or starts collectors.
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        PIDFILE.write_text(str(os.getpid()))
        global SCHEDULER
        if not os.environ.get("FT_DESK_NO_COLLECT"):  # a test copy on another port serves the live store without collecting
            SCHEDULER = collect.Scheduler(SOURCES, workers=6)
            SCHEDULER.start()
            watch_refresh()
        server.serve_forever()
        return 0
    if cmd == "ensure":
        if listening():
            print(f"http://127.0.0.1:{PORT}/ (already running)")
            return 0
        with open(LOG, "a") as log:
            subprocess.Popen([sys.executable, __file__, "serve"], stdout=log, stderr=log,  # FT_DESK_PORT and FT_DESK_NO_COLLECT pass through
                             stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(DESK))
        for _ in range(30):
            if listening():
                print(f"http://127.0.0.1:{PORT}/")
                return 0
            time.sleep(0.1)
        print("desk server did not start; see " + str(LOG), file=sys.stderr)
        return 1
    if cmd == "stop":
        try:
            os.kill(int(PIDFILE.read_text()), signal.SIGTERM)
            print("stopped")
        except (OSError, ValueError):
            print("not running")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
