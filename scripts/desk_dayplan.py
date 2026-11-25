#!/usr/bin/env python3
"""The Day plan at the top of the Plans tab: today's schedule from the calendars, Peter's S1 to S4
sessions, whether today's check-in is in Forethought's #checkin, and Day.md kept in step.

The desk is the source of truth for the sessions. Every save writes the check-in block into Day.md
(a Markdown note), replacing only that block, unless Day.md's block has changed since the desk last
wrote or accepted it: then nothing is written and the page shows the difference, with "Keep the notes app's
version" and "Overwrite the note". The block's format and its parsing come from the briefing skill's
scripts (update-day-schedule.py, post-day-to-slack.py), loaded by path.

Posting happens only when Peter presses the button, with the exact text the page showed him. Before
every post the desk asks Slack whether today's check-in is already there, so a retry never posts twice.
Slack calls run in the slack skill's own Python (it has `requests`), as this file's command line:

    <slack venv python> desk_dayplan.py slack check|post|update   < JSON request   > JSON reply

The server imports this file as a feature module (ROUTES, COLLECTORS, sync_info).
"""
from __future__ import annotations

import datetime as dt
import difflib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from desk_config import CONFIG, DESK, PERSONAL_CALENDAR, PORT, path as cfg_path  # noqa: E402

BRIEFING = Path.home() / ".agents/skills/briefing/scripts"
SLACK_SCRIPTS = cfg_path("slack").parent
SLACK_PY = SLACK_SCRIPTS.parent / ".venv/bin/python"
DAY_MD = Path(os.environ.get("FT_DESK_DAY_MD") or "~/Documents/Notes/Day.md").expanduser()
DEFAULT_PORT = CONFIG.get("server", {}).get("port", 4848)
STATE = DESK / ("dayplan.json" if PORT == DEFAULT_PORT else f"dayplan-{PORT}.json")  # a test copy keeps its own state

WORKSPACE, CHANNEL, CHANNEL_NAME = "forethought", "C0CHECKIN00", "checkin"
ME = CONFIG["slack"]["forethought"]["my_user_id"]
SLACK_URL = "https://forethought-ea.slack.com"
FT_CAL = CONFIG["calendar"]
PERSONAL = PERSONAL_CALENDAR or {"account": "", "calendars": []}

SCHEDULE_MARKER, SESSIONS_MARKER = "<u>Schedule:</u>", "<u>Sessions:</u>"
NO_EVENTS = "No events scheduled."
SEED = "S1: \nS2: \nS3: \nS4: "
# The briefing's rules for the schedule (references/work-sessions.md): only events with other people in them;
# Personal events never go in; some titles Peter can't edit get a fixed name.
LEFT_OUT = re.compile(r"\bpersonal\b", re.I)  # titles of private events to leave out
SOLO = re.compile(r"^\s*(blog post|focus( time| block)?|deep work|work session|s[1-4])\b", re.I)
RENAMES = {"weekly team meeting": "All hands meeting"}
TCC_MSG = "macOS is blocking Day.md: allow Forethought Desk under System Settings > Privacy & Security > Files and Folders > Documents."


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, BRIEFING / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_post = _load("post_day_to_slack", "post-day-to-slack.py")   # extract_block, to_elements, plain, today_heading, todays_post
_upd = _load("update_day_schedule", "update-day-schedule.py")  # date_heading


# ---------------------------------------------------------------- the block
def heading(day: dt.date) -> str:
    return _upd.date_heading(day).strip()


def tidy_sessions(text: str) -> list[str]:
    """Session lines as Day.md and the post will hold them. A blank line survives only before an underlined
    heading (`<u>Comment</u>`), and a `---` line never: post-day-to-slack.py's extract_block ends the block at
    either, so keeping them would make the next read of Day.md look like an edit made outside the desk."""
    lines = [l.rstrip() for l in (text or "").replace("\r", "").split("\n") if l.strip() != "---"]
    out = []
    for i, line in enumerate(lines):
        if line.strip():
            out.append(line)
        elif out and out[-1].strip():
            following = next((l for l in lines[i + 1:] if l.strip()), "")
            if following.lstrip().startswith("<u>"):
                out.append("")
    return out


def compose(day: dt.date, schedule: list[str], sessions: str) -> list[str]:
    return [heading(day), SCHEDULE_MARKER, *(schedule or [NO_EVENTS]), "", SESSIONS_MARKER, *tidy_sessions(sessions)]


def norm(lines: list[str]) -> str:
    return "\n".join(l.rstrip() for l in lines).strip("\n")


def split_block(block: list[str]) -> dict:
    """Day.md's check-in block as {date, schedule lines, sessions text (everything under Sessions)}."""
    sched_at = next((i for i, l in enumerate(block) if l.strip() in (SCHEDULE_MARKER, "**Schedule:**")), None)
    sess_at = next((i for i, l in enumerate(block) if "Sessions:" in l), None)
    end = sess_at if sess_at is not None else len(block)
    schedule = [l.strip() for l in block[(sched_at + 1 if sched_at is not None else 1):end] if l.strip() and l.strip() != NO_EVENTS]
    sessions = "\n".join(block[sess_at + 1:]) if sess_at is not None else ""
    return {"date": block[0].strip() if block else "", "schedule": schedule, "sessions": sessions}


class DayMdError(Exception):
    pass


def read_day_md() -> tuple[str, list[str], int | None, float]:
    """(content, block lines, index of the block's first line or None, mtime)."""
    try:
        content = DAY_MD.read_text()
        mtime = DAY_MD.stat().st_mtime
    except FileNotFoundError:
        raise DayMdError(f"Day.md isn't at {DAY_MD}.") from None
    except (PermissionError, InterruptedError) as e:
        raise DayMdError(TCC_MSG) from None
    except OSError as e:
        raise DayMdError(f"Can't read Day.md: {e.strerror or e}.") from None
    lines = content.split("\n")
    start = next((i for i, l in enumerate(lines) if _post.DATE_LINE.match(l.strip())), None)
    if start is None:
        return content, [], None, mtime
    try:
        block = _post.extract_block(content)
    except SystemExit:
        return content, [], None, mtime
    return content, block, start, mtime


def write_day_md(content: str, start: int | None, old_len: int, block: list[str]) -> float:
    lines = content.split("\n")
    if start is None:   # no check-in block yet: add one at the end
        new = content.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"
    else:
        new = "\n".join(lines[:start] + block + lines[start + old_len:])
    try:
        DAY_MD.write_text(new)
        return DAY_MD.stat().st_mtime
    except (PermissionError, InterruptedError) as e:
        raise DayMdError(TCC_MSG) from None
    except OSError as e:
        raise DayMdError(f"Can't write Day.md: {e.strerror or e}.") from None


def line_diff(desk: str, note: str) -> list[dict]:
    a, b = desk.split("\n"), note.split("\n")
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out += [{"k": "same", "t": l} for l in a[i1:i2]]
        else:
            out += [{"k": "desk", "t": l} for l in a[i1:i2]] + [{"k": "obs", "t": l} for l in b[j1:j2]]
    return out


# ---------------------------------------------------------------- Slack text, both ways
def elements_md(elements: list[dict]) -> str:
    """Rich-text elements back to the block's markup, merging runs of the same style, so that a post made
    from Day.md and the desk's text compare equal however Slack split the elements."""
    runs: list[list] = []
    for el in elements:
        t = el.get("type")
        if t == "text":
            text = el.get("text", "")
        elif t == "link":
            text = el.get("text") or el.get("url", "")
        elif t == "emoji":
            text = f':{el.get("name", "")}:'
        elif t == "user":
            text = f'<@{el.get("user_id", "")}>'
        elif t == "channel":
            text = f'<#{el.get("channel_id", "")}>'
        else:
            text = el.get("text", "")
        st = el.get("style") or {}
        key = (bool(st.get("bold")), bool(st.get("underline")), bool(st.get("strike")))
        for k, part in enumerate(text.split("\n")):
            if k:
                runs.append([(False, False, False), "\n"])
            if part:
                if runs and runs[-1][0] == key and runs[-1][1] != "\n":
                    runs[-1][1] += part
                else:
                    runs.append([key, part])
    out = []
    for (bold, under, strike), text in runs:
        if text == "\n" or not (bold or under or strike) or not text.strip():
            out.append(text)
            continue
        lead, core, trail = re.match(r"^(\s*)(.*?)(\s*)$", text, re.S).groups()
        if strike:
            core = f"~~{core}~~"
        if under:
            core = f"<u>{core}</u>"
        if bold:
            core = f"**{core}**"
        out.append(lead + core + trail)
    return "\n".join(l.rstrip() for l in "".join(out).split("\n")).strip("\n")


def canonical(md: str) -> str:
    """The desk's text as it will read back from Slack."""
    els = []
    lines = md.split("\n")
    for i, line in enumerate(lines):
        els += _post.to_elements(line)
        if i < len(lines) - 1:
            els.append({"type": "text", "text": "\n"})
    return elements_md(els)


def slack_payload(md: str) -> dict:
    lines = md.split("\n")
    els = []
    for i, line in enumerate(lines):
        els += _post.to_elements(line)
        if i < len(lines) - 1:
            els.append({"type": "text", "text": "\n"})
    blocks = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": els}]}]
    return {"text": "\n".join(_post.plain(l) for l in lines), "blocks": json.dumps(blocks)}


def message_md(m: dict) -> str:
    for b in m.get("blocks") or []:
        if b.get("type") == "rich_text":
            els = []
            for sec in b.get("elements", []):
                els += sec.get("elements", []) if sec.get("type") == "rich_text_section" else []
            return elements_md(els)
    return (m.get("text") or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def permalink(ts: str) -> str:
    return f"{SLACK_URL}/archives/{CHANNEL}/p{ts.replace('.', '')}"


# ---------------------------------------------------------------- the slack skill's Python runs this part
def slack_main(action: str) -> dict:
    sys.path.insert(0, str(SLACK_SCRIPTS))
    from slack_client import load_config, make_client
    req = json.load(sys.stdin)
    creds, _ = load_config(WORKSPACE)
    client = make_client(creds)

    def todays():
        ts = _post.todays_post(client, CHANNEL, ME, req["heading"])
        if not ts:
            return None
        h = client._post("conversations.history", {"channel": CHANNEL, "latest": ts, "inclusive": "true", "limit": "1"})
        if not h.get("ok"):
            raise RuntimeError(f"Slack: {h.get('error')}")
        m = next((m for m in h.get("messages", []) if m.get("ts") == ts), {})
        return {"ts": ts, "md": message_md(m), "edited": bool(m.get("edited"))}

    if action == "check":
        # todays_post ignores Slack errors; ask once so a bad token shows as an error, not "not posted"
        probe = client._post("conversations.history", {"channel": CHANNEL, "limit": "1"})
        if not probe.get("ok"):
            raise RuntimeError(f"Slack: {probe.get('error')}")
        return {"post": todays()}
    if action == "post":
        existing = todays()
        if existing:   # posted already (from here or elsewhere): never post twice
            return {"post": existing, "already": True}
        res = client._post("chat.postMessage", {"channel": CHANNEL, **slack_payload(req["md"]),
                                                "unfurl_links": "false", "unfurl_media": "false"})
        if not res.get("ok"):
            return {"error": res.get("error") or "unknown error"}
        return {"post": {"ts": res["ts"], "md": canonical(req["md"]), "edited": False}}
    if action == "update":
        res = client._post("chat.update", {"channel": CHANNEL, "ts": req["ts"], **slack_payload(req["md"])})
        if not res.get("ok"):
            return {"error": res.get("error") or "unknown error"}
        return {"post": {"ts": req["ts"], "md": canonical(req["md"]), "edited": True}}
    raise SystemExit(f"unknown action {action}")


class Unclear(RuntimeError):
    """The Slack call may or may not have gone through."""


def slack(action: str, req: dict, timeout: int = 45) -> dict:
    try:
        r = subprocess.run([str(SLACK_PY), str(Path(__file__).resolve()), "slack", action], input=json.dumps(req),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Unclear("Slack didn't answer in time") from None
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Unclear((r.stderr or r.stdout).strip()[-300:] or "no reply from the Slack client") from None
    return out


# ---------------------------------------------------------------- state
# dayplan.json: {"days": {"YYYY-MM-DD": plan}}. A plan: sessions (as typed), edited (Peter has typed since the
# seed), rev (bumped when the sessions change other than by typing, so the page refills its field), adopted
# (Day.md schedule lines taken in with the note's version), adopted_matched (which of their times were calendar
# events then), written (the last Day.md block the desk wrote or accepted), conflict ({note} while Day.md
# holds edits made outside the desk), calendar ({events, at, error}), slack ({post, at, error}), saved_at.
_lock = threading.RLock()
_post_lock = threading.Lock()
_state: dict | None = None
_daymd = {"error": None, "mtime": None, "date": None}


def _load_state() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(STATE.read_text())
        except (OSError, ValueError):
            _state = {}
        _state.setdefault("days", {})
    return _state


def _save_state() -> None:
    s = _load_state()
    keep = sorted(s["days"])[-14:]
    s["days"] = {k: s["days"][k] for k in keep}
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    os.replace(tmp, STATE)


def today() -> dt.date:
    return dt.date.today()


def plan_for(day: dt.date | None = None) -> dict:
    """Today's plan, seeded from Day.md when it is dated today, else with empty S1 to S4."""
    day = day or today()
    with _lock:
        days = _load_state()["days"]
        p = days.get(day.isoformat())
        if p is None:
            p = days[day.isoformat()] = {"sessions": SEED, "edited": False, "rev": 1, "adopted": [], "adopted_matched": None,
                                         "written": None, "conflict": None, "calendar": None, "slack": None, "saved_at": None}
            try:
                _, block, _, _ = read_day_md()
                if block and block[0].strip() == heading(day):
                    _adopt(p, block)
            except DayMdError as e:
                _daymd["error"] = str(e)
            _save_state()
        return p


def _adopt(p: dict, block: list[str]) -> None:
    """Take Day.md's block into the desk: its sessions, and its schedule lines as names for calendar events."""
    parts = split_block(block)
    p["sessions"] = parts["sessions"] or SEED
    p["adopted"] = parts["schedule"]
    cal = p.get("calendar") or {}
    p["adopted_matched"] = _match_times(p["adopted"], cal["events"]) if cal.get("events") is not None else None
    p["written"] = norm(block)
    p["conflict"] = None
    p["rev"] = p.get("rev", 0) + 1


def _hhmm(line: str) -> str | None:
    m = re.match(r"^(\d{4})\s", line)
    return m.group(1) if m else None


def _match_times(adopted: list[str], events: list[dict]) -> list[str]:
    times = {e["hhmm"] for e in events}
    return sorted({t for l in adopted if (t := _hhmm(l)) and t in times})


def schedule(p: dict) -> list[str]:
    """The calendar's events as `HHMM Label`. An event takes its name from a Day.md line at the same time
    (the briefing writes "Call: Jane Doe" where the calendar says "Jane Doe (SavvyCal, 25 min)");
    Day.md lines at no event's time stay, unless they named an event that has since gone."""
    events = ((p.get("calendar") or {}).get("events"))
    adopted = p.get("adopted") or []
    if events is None:
        return list(adopted)
    if p.get("adopted_matched") is None and adopted:
        p["adopted_matched"] = _match_times(adopted, events)
    matched = set(p.get("adopted_matched") or [])
    by_time: dict[str, list[str]] = {}
    for l in adopted:
        if (t := _hhmm(l)):
            by_time.setdefault(t, []).append(l)
    rows, used = [], set()
    for e in events:
        names = by_time.get(e["hhmm"]) or []
        if names and e["hhmm"] not in used:
            used.add(e["hhmm"])
            rows += [(e["hhmm"], n) for n in names]
        elif not names:
            rows.append((e["hhmm"], f'{e["hhmm"]} {e["label"]}'))
    times = {e["hhmm"] for e in events}
    for l in adopted:
        t = _hhmm(l)
        if t is None:
            rows.append(("9999", l))
        elif t not in times and t not in matched:
            rows.append((t, l))
    return [l for _, l in sorted(rows, key=lambda r: r[0])]


def block_for(p: dict, day: dt.date | None = None) -> list[str]:
    return compose(day or today(), schedule(p), p["sessions"])


# ---------------------------------------------------------------- Day.md sync
def sync_day_md(p: dict, write: bool) -> None:
    """Check Day.md against the last block the desk wrote or accepted; with write, put the desk's block in.
    Never overwrites edits made outside the desk: those become a conflict for Peter to settle."""
    day = today()
    try:
        content, block, start, mtime = read_day_md()
    except DayMdError as e:
        _daymd.update(error=str(e))
        return
    _daymd.update(error=None, mtime=mtime, date=block[0].strip() if block else None)
    mine = block_for(p, day)
    dated_today = bool(block) and block[0].strip() == heading(day)
    if dated_today and norm(block) != (p.get("written") or ""):
        if norm(block) == norm(mine):
            p["written"], p["conflict"] = norm(block), None
        elif not p.get("edited") and not p.get("written"):
            _adopt(p, block)   # nothing typed on the desk yet: the note's plan (often the briefing's) is the plan
        else:
            p["conflict"] = {"note": norm(block), "at": time.time()}
        return
    p["conflict"] = None
    if not write or (dated_today and norm(block) == norm(mine)):
        return
    try:
        _daymd["mtime"] = write_day_md(content, start, len(block), mine)
    except DayMdError as e:
        _daymd.update(error=str(e))
        return
    p["written"] = norm(mine)
    _daymd["date"] = mine[0]


def overwrite_day_md(p: dict) -> None:
    try:
        content, block, start, _ = read_day_md()
        mine = block_for(p)
        _daymd["mtime"] = write_day_md(content, start, len(block), mine)
    except DayMdError as e:
        _daymd.update(error=str(e))
        raise RuntimeError(str(e)) from None
    p["written"], p["conflict"] = norm(mine), None
    _daymd.update(error=None, date=mine[0])


# ---------------------------------------------------------------- collectors
def _result(detail: str, complete: bool = True, changed: bool = False):
    import desk_collect
    return desk_collect.Result(detail, complete=complete, changed=changed)


def fetch_events(day: dt.date) -> tuple[list[dict], bool]:
    """Today's events across the Forethought calendar and Peter's personal ones, as the briefing builds the schedule."""
    import concurrent.futures as cf
    import desk_collect
    f = desk_collect.ftd()
    lo = dt.datetime.combine(day, dt.time()).astimezone()
    hi = lo + dt.timedelta(days=1)

    def events(account, cal):
        data = f.run_json(["gog", "--json", "cal", "events", cal, "--from", lo.isoformat(), "--to", hi.isoformat(),
                           "--account", account, "--max", "100"])
        return data.get("events") or [] if isinstance(data, dict) else []

    sources = [("ft", FT_CAL["account"], FT_CAL["calendar_id"])] + [("personal", PERSONAL["account"], c) for c in PERSONAL["calendars"]]
    out, seen, complete = [], set(), True
    with cf.ThreadPoolExecutor(max_workers=len(sources)) as pool:
        futs = [(kind, pool.submit(events, acct, cal)) for kind, acct, cal in sources]
        results = []
        for kind, fut in futs:
            try:
                results.append((kind, fut.result()))
            except Exception:
                if kind == "ft":
                    raise
                complete = False
    for kind, evs in results:
        for e in evs:
            start = (e.get("start") or {}).get("dateTime")
            summary = (e.get("summary") or "").strip()
            if not start or e.get("status") == "cancelled" or e.get("transparency") == "transparent":
                continue   # all-day markers, cancelled events, and blocks marked free are not appointments
            me = next((a for a in e.get("attendees") or [] if a.get("self")), None)
            if me and me.get("responseStatus") == "declined":
                continue
            if (kind == "ft" and summary.lower() == "busy") or not summary or LEFT_OUT.search(summary) or SOLO.match(summary):
                continue   # Forethought's Busy blocks mirror Peter's own events
            s = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone()
            if s.date() != day:
                continue
            label = RENAMES.get(summary.lower(), summary)
            key = (s.strftime("%H%M"), label.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"hhmm": s.strftime("%H%M"), "label": label, "start": s.isoformat(timespec="minutes")})
    out.sort(key=lambda e: e["start"])
    return out, complete


def collect_schedule():
    day = today()
    p = plan_for(day)
    try:
        events, complete = fetch_events(day)
    except Exception as e:
        with _lock:
            p["calendar"] = {**(p.get("calendar") or {"events": None}), "error": str(e)[:300], "failed_at": time.time()}
            _save_state()
        raise
    with _lock:
        before = (p.get("calendar") or {}).get("events")
        p["calendar"] = {"events": events, "at": time.time(), "error": None if complete else "a personal calendar didn't load"}
        _save_state()
    return _result(f"{len(events)} events", complete=complete, changed=before != events)


def collect_checkin():
    day = today()
    p = plan_for(day)
    try:
        out = slack("check", {"heading": _post.plain(heading(day))})
        if out.get("error"):
            raise RuntimeError(out["error"])
    except Exception as e:
        with _lock:
            p["slack"] = {**(p.get("slack") or {}), "error": str(e)[:300]}
            _save_state()
        raise RuntimeError(f"#{CHANNEL_NAME}: {e}") from None
    with _lock:
        before = (p.get("slack") or {}).get("post")
        p["slack"] = {"post": out.get("post"), "at": time.time(), "error": None}
        _save_state()
    return _result("posted" if out.get("post") else "not posted", changed=before != out.get("post"))


def collect_day_md():
    """Notice edits made outside the desk even while Peter isn't typing on the desk (Day.md is read only when its mtime moves)."""
    p = plan_for()
    try:
        mtime = DAY_MD.stat().st_mtime
    except OSError as e:
        _daymd["error"] = (TCC_MSG if isinstance(e, (PermissionError, InterruptedError))
                           else f"Day.md isn't at {DAY_MD}." if isinstance(e, FileNotFoundError) else f"Can't read Day.md: {e.strerror or e}.")
        return _result(_daymd["error"], complete=False)
    if mtime == _daymd.get("mtime") and not _daymd.get("error"):
        return _result("unchanged")
    with _lock:
        before = p.get("conflict")
        sync_day_md(p, write=False)
        _save_state()
    return _result(_daymd["error"] or ("edited outside the desk" if p.get("conflict") else "in step"),
                   complete=not _daymd["error"], changed=before != p.get("conflict"))


COLLECTORS = {
    "dayplan_schedule": (collect_schedule, 120, None),
    "dayplan_checkin": (collect_checkin, 180, "#checkin"),
    "dayplan_daymd": (collect_day_md, 20, None),
}


# ---------------------------------------------------------------- what the page sees
def view() -> dict:
    day = today()
    with _lock:
        p = plan_for(day)
        block = block_for(p, day)
        text = norm(block)
        cal = p.get("calendar") or {}
        sl = p.get("slack") or {}
        post = sl.get("post")
        posted = None
        if post:
            posted = {"ts": post["ts"], "url": permalink(post["ts"]),
                      "at": dt.datetime.fromtimestamp(float(post["ts"])).strftime("%H:%M"),
                      "differs": canonical(text) != (post.get("md") or "")}
        conflict = None
        if p.get("conflict"):
            conflict = {"diff": line_diff(text, p["conflict"]["note"])}
        why_not = None
        if cal.get("events") is None:
            why_not = "Waiting for the calendar" if not cal.get("error") else "The calendar didn't load"
        return {
            "date": day.isoformat(), "heading": _post.plain(heading(day)), "weekday": day.weekday() < 5,
            "schedule": schedule(p), "calendar": {"at": cal.get("at"), "error": cal.get("error"), "loaded": cal.get("events") is not None},
            "sessions": p["sessions"], "rev": p.get("rev", 1), "saved_at": p.get("saved_at"),
            "block": text,
            "slack": {"known": sl.get("at") is not None, "at": sl.get("at"), "error": sl.get("error"), "posted": posted},
            "conflict": conflict, "day_md": {"error": _daymd.get("error"), "path": str(DAY_MD)},
            "why_not": why_not,
        }


_last_view: dict = {}


def sync_info() -> dict:
    """Sent on every 5-second sync. A Day.md read can stall for seconds when macOS asks about Documents access,
    so while one holds the lock the page gets the last view instead of waiting."""
    global _last_view
    if not _lock.acquire(timeout=0.3):
        return _last_view or {"error": "busy"}
    try:
        _last_view = view()
        return _last_view
    finally:
        _lock.release()


# ---------------------------------------------------------------- routes
def r_save(body: dict) -> dict:
    sessions = str(body.get("sessions") or "")[:8000]
    with _lock:
        p = plan_for()
        if sessions != p["sessions"]:
            p["sessions"], p["edited"] = sessions, True
        p["saved_at"] = time.time()
        sync_day_md(p, write=True)
        _save_state()
    return view()


def r_resolve(body: dict) -> dict:
    choice = body.get("choice")
    with _lock:
        p = plan_for()
        if choice == "note":
            try:
                _, block, _, _ = read_day_md()
            except DayMdError as e:
                raise RuntimeError(str(e)) from None
            if not block or block[0].strip() != heading(today()):
                raise ValueError("Day.md no longer holds today's plan")
            _adopt(p, block)
            p["edited"] = True
        elif choice == "desk":
            overwrite_day_md(p)
        else:
            raise ValueError("choice must be note or desk")
        _save_state()
    return view()


def r_post(body: dict) -> dict:
    """Post today's check-in, or edit it in place: only the text the page showed Peter when he pressed the button."""
    action = "update" if body.get("action") == "update" else "post"
    shown = str(body.get("text") or "")
    day = today()
    if not _post_lock.acquire(blocking=False):
        raise RuntimeError("Already posting")
    try:
        with _lock:
            p = plan_for(day)
            text = norm(block_for(p, day))
            if shown != text:
                raise ValueError("The plan changed while you pressed the button. Check the preview and press again.")
            if (p.get("calendar") or {}).get("events") is None:
                raise ValueError("The calendar hasn't loaded yet")
            post = (p.get("slack") or {}).get("post")
        req = {"heading": _post.plain(heading(day)), "md": text}
        if action == "update":
            if not post:
                raise ValueError("There's no check-in today to update")
            req["ts"] = post["ts"]
        try:
            out = slack(action, req)
        except Unclear as e:
            # It may have gone through: ask Slack rather than guess, so pressing again can't post twice
            try:
                check = slack("check", {"heading": req["heading"]})
            except Unclear:
                check = {}
            if check and "error" not in check:
                with _lock:
                    p["slack"] = {"post": check.get("post"), "at": time.time(), "error": None}
                    _save_state()
            if check.get("post") and action == "post":
                return {**view(), "message": "Posted to #checkin"}
            raise RuntimeError(f"Slack didn't confirm ({e}). Check #checkin before trying again.") from None
        if out.get("error"):
            raise RuntimeError(f"Slack: {out['error']}")
        with _lock:
            p["slack"] = {"post": out["post"], "at": time.time(), "error": None}
            p["saved_at"] = time.time()
            sync_day_md(p, write=True)
            _save_state()
        msg = ("It was already posted, so nothing was sent" if out.get("already")
               else "Updated the post in #checkin" if action == "update" else "Posted to #checkin")
        return {**view(), "message": msg}
    finally:
        _post_lock.release()


def r_refresh(body: dict) -> dict:
    """Check the calendar, #checkin and Day.md now (the page asks when the Plans tab opens on stale facts)."""
    errors = []
    for fn in (collect_schedule, collect_checkin, collect_day_md):
        try:
            fn()
        except Exception as e:
            errors.append(str(e))
    return {**view(), "errors": errors}


ROUTES = {
    "/api/dayplan/save": r_save,
    "/api/dayplan/resolve": r_resolve,
    "/api/dayplan/post": r_post,
    "/api/dayplan/refresh": r_refresh,
}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "slack":
        try:
            print(json.dumps(slack_main(sys.argv[2])))
        except Exception as e:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
    elif len(sys.argv) == 2 and sys.argv[1] == "view":
        print(json.dumps(view(), indent=1, ensure_ascii=False))
    else:
        sys.exit(__doc__)
