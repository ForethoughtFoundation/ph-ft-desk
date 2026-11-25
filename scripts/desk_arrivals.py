#!/usr/bin/env python3
"""Arrivals: messages collected continuously into the store, between full desk runs.

    desk_arrivals.py run NAME     run one collector now
    desk_arrivals.py summary      what the pill shows
    desk_arrivals.py list         the arrivals behind it
    desk_arrivals.py feedback [--since EPOCH]   Peter's not-urgent marks, for the weekly rubric review
    desk_arrivals.py feedback --reviewed        record that the review is done

Each collector reuses the gather's source function with its own cursor, and saves whole
messages (the judge can't fetch, so nothing is clipped short or left for later). A message
belongs to a unit: a Slack thread or conversation, a Signal or WhatsApp chat, an email
thread, a Doc, or a Linear issue.

An arrival is "held" from the moment it is collected until the next full run judges it:
the pill counts units with something new from someone else since the run's watermark for
that source, which Peter hasn't answered. A message from someone in the config's `vips` sent
to Peter (a DM, a group DM he is in, or a mention) makes its conversation a candidate; it
becomes urgent when the judge (desk_judge.py), reading the conversation after that message,
says urgent, and it has waited five minutes. A candidate the judge hasn't reached within
fifteen minutes shows as urgent, marked unjudged, so a stalled judge can't hide a VIP. A reply
or reaction from Peter clears it. Peter can mark one not urgent, with an optional comment: it
stays in the pill's count but loses its chip and its Urgent row until someone on his VIP list
writes there again, and the mark is logged with the rule's and the judge's reasons for the
weekly review of the rubric (`desk_arrivals.py feedback`).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desk_collect as collect  # noqa: E402
import desk_store as store  # noqa: E402
from desk_collect import Result  # noqa: E402

from desk_config import STATE_DIR  # noqa: E402

STATE = STATE_DIR / "state.json"
DISMISSED = STATE_DIR / "desk" / "arrivals-dismissed.json"   # unit -> the VIP message time Peter marked not urgent
FEEDBACK = STATE_DIR / "desk" / "urgency-feedback.json"      # Peter's not-urgent marks, with what the rule and the judge said
REVIEWED = STATE_DIR / "desk" / "urgency-reviewed.json"      # when the marks were last reviewed
REVIEW_EVERY = 7 * 86400
OVERLAP = 180          # seconds a collector looks back past its cursor; keys make re-reads harmless
URGENT_AFTER = 5 * 60        # a VIP message the judge calls urgent shows once it has waited this long
UNJUDGED_AFTER = 15 * 60     # ... and one the judge hasn't reached shows after this, marked unjudged
KEEP_DAYS = 30
BODY_LEN = 8000
SOURCE_LABELS = {"slack": "Slack", "gmail": "Email", "signal": "Signal", "whatsapp": "WhatsApp", "gdocs": "Docs", "linear": "Linear"}
# the run's watermark name for each kind of unit
RUN_WM = {"slack:forethought": "slack_forethought", "slack:secondws": "slack_secondws", "gmail": "gmail",
          "signal": "signal", "whatsapp": "whatsapp", "gdoc": "gdocs", "linear": "linear"}


def ftd():
    """The gather script, with its clipping limits raised: the judge needs whole messages."""
    f = collect.ftd()
    f.TEXT_LEN, f.SIGNAL_BODY_LEN, f.GDOC_TEXT_LEN, f.GDOC_THREAD_TAIL, f.SLACK_THREADS_PER_WS = BODY_LEN, BODY_LEN, BODY_LEN, 60, 400
    f.CONFIG["gdocs"]["max_candidates"] = 1000   # the background scan has time to cover every recent Doc
    if not getattr(f, "_paged_search", False):
        one_page = f.drive_search

        def drive_search(acct, query, cap):
            """Follow Drive's page tokens up to `cap` files: Drive often returns short pages with more to come."""
            files, token = [], None
            while len(files) < cap:
                args = ["gog", "--json", "drive", "search", "--account", acct, "--max", str(min(1000, cap - len(files))), "--raw-query", query]
                data = f.run_json(args + (["--page", token] if token else []), timeout=60)
                files += data.get("files") or []
                token = data.get("nextPageToken")
                if not token:
                    return files, False
            return files, True
        f.drive_search, f._paged_search, f._one_page_search = drive_search, True, one_page
    return f


def run_watermarks() -> dict[str, float]:
    return (collect.load_json(STATE, {}).get("watermarks")) or {}


def start_wm(name: str) -> float:
    cur = store.cursor_of(name)
    if cur and cur.get("wm"):
        return cur["wm"] - OVERLAP
    return run_watermarks().get(name) or time.time() - 86400


def parse_ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def vip_name(author: str | None) -> str | None:
    """The VIP an author string names, by full name or by first name plus last name."""
    a = (author or "").casefold()
    for v in ftd().CONFIG.get("vips", []):
        first, last = v.casefold().split()[0], v.casefold().split()[-1]
        if v.casefold() in a or (a.startswith(first) and last in a):
            return v
    return None


class Batch:
    """Messages one collector fetched, written in the transaction that advances its cursor."""

    def __init__(self, source: str):
        self.source, self.units, self.msgs = source, {}, []

    def add(self, unit: str, *, label: str, link: str | None, kind: str, key: str, ts: float, author: str | None,
            is_from_me: bool, text: str | None, direct: bool = False, to_me: bool | None = None, extra: dict | None = None) -> None:
        """`direct`: addressed to Peter loosely (counts in the pill). `to_me`: sent to him or naming him (the VIP rule); defaults to `direct`."""
        self.units[unit] = {"label": re.sub(r"\s+", " ", label).strip()[:160], "link": link, "kind": kind}
        vip = None if is_from_me else vip_name(author)
        data = {"text": text or "", "direct": bool(direct), "to_me": bool(direct if to_me is None else to_me), "vip": vip, "link": link, **(extra or {})}
        self.msgs.append((key, self.source, unit, float(ts), author, int(bool(is_from_me)), json.dumps(data, ensure_ascii=False)))

    def write(self, con) -> int:
        now, fresh = time.time(), {}
        for key, source, unit, ts, author, mine, data in self.msgs:
            cur = con.execute("INSERT OR IGNORE INTO messages(key, source, unit, ts, author, is_from_me, data, first_seen) VALUES(?,?,?,?,?,?,?,?)",
                              (key, source, unit, ts, author, mine, data, now))
            if cur.rowcount:
                fresh.setdefault(unit, []).append((ts, mine, json.loads(data)))
        for unit, rows in fresh.items():
            u = self.units[unit]
            other = max([ts for ts, mine, _ in rows if not mine] or [0])
            me = max([ts for ts, mine, _ in rows if mine] or [0])
            vip = max([ts for ts, mine, d in rows if not mine and d["vip"] and d["to_me"]] or [0])
            direct = max([ts for ts, mine, d in rows if not mine and d["direct"]] or [0])
            con.execute(
                "INSERT INTO units(id, source, label, link, kind, revision, last_ts, last_other_ts, last_me_ts, vip, direct, updated_at) "
                "VALUES(?,?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label, link=excluded.link, kind=excluded.kind, "
                "revision=revision+1, last_ts=max(coalesce(last_ts,0), excluded.last_ts), last_other_ts=max(coalesce(last_other_ts,0), excluded.last_other_ts), "
                "last_me_ts=max(coalesce(last_me_ts,0), excluded.last_me_ts), vip=max(coalesce(vip,0), excluded.vip), "
                "direct=max(coalesce(direct,0), excluded.direct), updated_at=excluded.updated_at",
                (unit, self.source, u["label"], u["link"], u["kind"], max(other, me), other, me, vip, direct, now))
        return sum(len(r) for r in fresh.values())


def finish(name: str, batch: Batch, started: float, detail: str, complete: bool = True) -> Result:
    return Result(detail, complete=complete, cursor={"wm": started}, writes=batch.write)


# ---------------------------------------------------------------- sources
def slack(ws: str):
    def run() -> Result:
        name, started = f"slack_{ws}", time.time()
        res = ftd().src_slack(ws, start_wm(name))
        b, missing = Batch("slack"), 0
        me = "@" + ftd().CONFIG["my_git_names"][0]
        for conv in res["items"]:
            if conv["kind"] == "mpim":  # "#mpdm-a--b--c-1" reads as "Group DM a, b, c"
                conv["conversation"] = "Group DM " + re.sub(r"^#?mpdm-|-\d+$", "", conv["conversation"]).replace("--", ", ")
            missing += len(conv.get("threads_not_fetched") or [])
            for m in conv["messages"]:
                in_thread = bool(m.get("in_thread_of")) and m.get("thread_ts")
                unit = f"slack:{ws}:{conv['channel_id']}" + (f":{m['thread_ts']}" if in_thread else "")
                label = conv["conversation"] + (": " + m["in_thread_of"][:80] if in_thread else "")
                b.add(unit, label=label, link=m["link"], kind=conv["kind"] + ("-thread" if in_thread else ""), key=m["key"],
                      ts=float(m["key"].rsplit(":", 1)[1]), author=m["author"], is_from_me=m["is_from_me"], text=m["text"],
                      direct=conv["kind"] == "im" or m["mentions_me"] or (in_thread and me in m["in_thread_of"]),
                      to_me=conv["kind"] in ("im", "mpim") or m["mentions_me"], extra={"files": m.get("files"), "mentions_me": m["mentions_me"]})
        # The judge can't fetch, so a conversation seen for the first time brings its history with it:
        # the whole thread, or the last messages of the DM or channel. Later polls add only what is new.
        f, cfg = ftd(), ftd().CONFIG["slack"][ws]
        names = f.slack_cache_names(ws)
        have = {r["id"] for r in store.db().execute("SELECT id FROM units WHERE source='slack'")}
        failed = 0
        for unit, meta in list(b.units.items()):
            if unit in have:
                continue
            parts = unit.split(":")
            cid, thread = parts[2], (parts[3] if len(parts) > 3 else None)
            try:
                got = f.slack_json(ws, "replies", cid, thread) if thread else f.slack_json(ws, "history", cid, "20")
            except Exception:
                failed += 1
                continue
            msgs = (got.get("messages") if isinstance(got, dict) else got) or []
            seen = {m[0] for m in b.msgs}
            for m in msgs:
                if m.get("subtype") in f.SLACK_EVENT_SUBTYPES or (not thread and m.get("thread_ts") and m["thread_ts"] != m["ts"]):
                    continue
                key = f"slack:{ws}:{cid}:{m['ts']}"
                if key in seen:
                    continue
                uid = m.get("user") or m.get("bot_id")
                text = re.sub(r"<@([A-Z0-9]+)>", lambda x: "@" + str(names.get(x.group(1), x.group(1))), m.get("text") or "")
                b.add(unit, label=meta["label"], link=meta["link"], kind=meta["kind"], key=key, ts=float(m["ts"]),
                      author=str((m.get("user_profile") or {}).get("real_name") or names.get(uid) or uid), is_from_me=uid == cfg["my_user_id"],
                      text=text[:BODY_LEN], direct=False, to_me=False, extra={"backfill": True, "mentions_me": f"<@{cfg['my_user_id']}>" in (m.get("text") or "")})
        missing += failed
        return finish(name, b, started, f'{res["message_count"]} messages in {len(res["items"])} conversations'
                      + (f"; {missing} threads not fetched" if missing else ""), complete=not missing)
    return run


def signal() -> Result:
    started = time.time()
    res = ftd().src_signal(start_wm("signal"))
    b = Batch("signal")
    for m in res["items"]:
        text = m.get("body") or (f"[{m['unavailable_content']}]" if m.get("unavailable_content") else "")
        if m.get("attachments"):
            text += " [attachments: " + ", ".join(a.get("filename") or "file" for a in m["attachments"]) + "]"
        b.add(f"signal:{m['conversation_id']}", label="Signal: " + m["conversation"], link=None, kind="im" if "(DM)" in m["conversation"] else "group",
              key=m["key"], ts=m["sent_at_ms"] / 1000, author=m["author"], is_from_me=m["is_from_me"], text=text,
              direct="(DM)" in m["conversation"], extra={"reactions": m.get("reactions"), "peter_reacted": m.get("peter_reacted")})
    health = res.get("bridge_health") or {}
    capturing = (health[0] if isinstance(health, list) and health else health or {}).get("capture_state") in (None, "capturing", "ok", "running")
    return finish("signal", b, started, f'{len(res["items"])} messages' + ("" if capturing else "; bridge not capturing"),
                  complete=not res.get("truncated") and capturing)


def whatsapp() -> Result:
    started = time.time()
    res = ftd().src_whatsapp(start_wm("whatsapp"))
    b, n = Batch("whatsapp"), 0
    for chat in res["items"]:
        dm = "(DM)" in chat["chat"]
        for m in chat["messages"]:
            n += 1
            author = chat["chat"].replace(" (DM)", "") if dm and not m["is_from_me"] else m["sender"]
            b.add(f"whatsapp:{chat['chat_jid']}", label="WhatsApp: " + chat["chat"], link=None, kind="im" if dm else "group", key=m["key"],
                  ts=m.get("ts") or parse_ts(m["time"]), author=author, is_from_me=m["is_from_me"], text=m.get("text") or (f"[{m['media']}]" if m.get("media") else ""),
                  direct=dm, extra={"reactions": m.get("reactions")})
    missing = [c["chat"] for c in res["items"] if not c["found"]]
    return finish("whatsapp", b, started, f"{n} messages" + (f"; not found: {missing}" if missing else ""), complete=not missing)


def gmail_text(payload: dict) -> str:
    """The plain-text body of a Gmail message payload (falling back to HTML with its tags removed)."""
    def walk(p, want):
        if p.get("mimeType") == want and (p.get("body") or {}).get("data"):
            return base64.urlsafe_b64decode(p["body"]["data"] + "===").decode("utf-8", "replace")
        for part in p.get("parts") or []:
            got = walk(part, want)
            if got:
                return got
        return ""
    text = walk(payload, "text/plain")
    if not text:
        html = walk(payload, "text/html")
        text = re.sub(r"\s+", " ", re.sub(r"<(style|script)[^>]*>.*?</\1>|<[^>]+>", " ", html, flags=re.S | re.I))
    text = re.sub(r"\[image: [^\]]*\]\s*", "", text)
    return text.strip()[:BODY_LEN]


def gmail() -> Result:
    started = time.time()
    f = ftd()
    acct = f.CONFIG["gmail"]["account"]
    # Sender: the address itself, never a display name that quotes one. Recipients: any of Peter's addresses in To.
    my_emails = {e.lower() for e in f.CONFIG["gdocs"]["my_emails"]}
    mine = re.compile("|".join(re.escape(e) for e in my_emails), re.I)
    res = f.src_gmail(start_wm("gmail"))
    # A Docs comment notification means a comment that involves Peter has just been made: check Docs now.
    if any(n.get("covered_by") == "gdocs" and n.get("new") for n in res.get("noise") or []):
        store.set_due("gdocs", 0)
    known = {r["unit"]: r["n"] for r in store.db().execute("SELECT unit, count(*) n FROM messages WHERE source='gmail' GROUP BY unit")}
    b, failed = Batch("gmail"), 0
    for t in res["items"]:
        unit = f"gmail:{t['thread_id']}"
        if known.get(unit, 0) >= (t.get("messages") or 1):
            continue
        try:
            thread = f.run_json(["gog", "--json", "gmail", "thread", "get", t["thread_id"], "--account", acct], timeout=60).get("thread") or {}
        except Exception:
            failed += 1
            continue
        for m in thread.get("messages") or []:
            h = {x["name"].lower(): x["value"] for x in (m.get("payload") or {}).get("headers") or []}
            sender = h.get("from") or t.get("from") or ""
            b.add(unit, label=h.get("subject") or t.get("subject") or "(no subject)", kind="email",
                  link=f"https://mail.google.com/mail/u/0/#all/{t['thread_id']}", key=f"gmail:{t['thread_id']}:{m['id']}",
                  ts=int(m.get("internalDate") or 0) / 1000, author=re.sub(r"\s*<[^>]+>", "", sender).strip('" ') or sender,
                  is_from_me=f.sender_address(sender) in my_emails, text=gmail_text(m.get("payload") or {}) or m.get("snippet") or "",
                  direct=bool(mine.search(h.get("to") or "")), extra={"from": sender, "to": h.get("to"), "cc": h.get("cc"), "labels": t.get("labels")})
    return finish("gmail", b, started, f'{len(res["items"])} threads, {len(res.get("noise") or [])} noise' + (f"; {failed} bodies not fetched" if failed else ""),
                  complete=not failed and not res.get("inbox_scan_truncated"))


def gdocs() -> Result:
    """The comments scan (desk_comments.py), which defines coverage for Docs and Sheets. Its threads feed the pill
    and the judge here, whole, and its snapshot feeds the Comments view; the store's keys make re-reads harmless."""
    import desk_comments
    started = time.time()
    snap, detail, complete = desk_comments.scan()
    b = Batch("gdocs")
    for t in snap["threads"]:
        if t.get("resolved"):
            continue
        f = snap["files"].get(t["file"]) or {}
        label = ("Sheet: " if t["kind"] == "sheet" else "Doc: ") + (f.get("name") or t["file"])
        for m in t["msgs"]:
            author = "Peter" if m["me"] else m["author"]
            b.add(f"gdoc:{t['file']}", label=label, link=t["link"], kind="doc",
                  key=f"gdoc:{t['file']}:{t['id']}:{ftd().local_iso(m['ts'])}:{author}", ts=m["ts"], author=author,
                  is_from_me=m["me"], text=m["text"] or f"[{m.get('action', '')}]", direct=t["reasons"] != ["watched"],
                  extra={"quoted": t.get("quoted"), "reasons": t["reasons"], "comment": t["id"], "resolved": False})

    def writes(con):
        n = b.write(con)
        return n + int(store.put_snapshot(desk_comments.SNAP, desk_comments.keep_desk_changes(snap, started, con), con))
    return Result(detail, complete=complete, cursor={"wm": started}, writes=writes)


def linear_changes() -> Result:
    started = time.time()
    res = ftd().src_linear(start_wm("linear_changes"))
    b = Batch("linear")
    for t in res["items"]:
        who = t.get("who") or []
        editor = t.get("last_edited_by") or ""
        editor = editor if re.fullmatch(r"[A-Z][a-z]+( [A-Z][a-z]+)*", editor) else "An agent"
        project = ", ".join(t["project"]) if isinstance(t.get("project"), list) else (t.get("project") or "no project")
        comments = "\n".join(f'{c.get("author")}: {c.get("text") or c.get("body") or ""}' for c in t.get("comments") or [])
        text = (f'{t.get("status")} · {", ".join(who) or "unassigned"} · {project}' + (f' · due {t["due"]}' if t.get("due") else "")
                + ("; newly created" if t.get("created_since") else "") + ("; closed" if t.get("closed") else "")
                + (f"; requested by {', '.join(t.get('requested_by') or [])}" if t.get("requested_by") else "") + ("\n" + comments if comments else ""))
        b.add(f"linear:{t['id']}", label=f'{t.get("identifier") or ""} {t.get("title") or ""}'.strip(), link=t.get("url") or t.get("link"), kind="issue",
              key=t["key"], ts=parse_ts(t["last_edited_time"]) if t.get("last_edited_time") else started, author=editor,
              is_from_me=t.get("last_edited_by") == "Peter", text=text,
              # A change counts as addressed to Peter when a person made it to a task of his; an agent's key shows as an ID or "Automation".
              direct="Peter" in who and editor != "An agent")
    return finish("linear_changes", b, started, f'{len(res["items"])} changed issues')


def prune() -> Result:
    cutoff = time.time() - KEEP_DAYS * 86400
    with store.tx() as con:
        n = con.execute("DELETE FROM messages WHERE first_seen < ? AND ts < ?", (cutoff, cutoff)).rowcount
        con.execute("DELETE FROM units WHERE id NOT IN (SELECT DISTINCT unit FROM messages)")
    return Result(f"{n} old messages removed")


# name -> (function, seconds between runs, label). Slack is the slowest by choice: its `counts`
# endpoint has no documented limit, so the desk polls it every three minutes, not every one.
ARRIVALS = {
    "signal": (signal, 60, "Signal"),
    "whatsapp": (whatsapp, 60, "WhatsApp"),
    "gmail": (gmail, 120, "Email"),
    "slack_forethought": (slack("forethought"), 180, "Slack"),
    "slack_secondws": (slack("secondws"), 300, "Slack (second workspace)"),
    "linear_changes": (linear_changes, 300, "Linear changes"),
    "gdocs": (gdocs, 900, "Docs"),
    "prune": (prune, 86400, "Store upkeep"),
}


# ---------------------------------------------------------------- what the page shows
def unit_wm(unit_id: str, wms: dict) -> float:
    for prefix, name in RUN_WM.items():
        if unit_id.startswith(prefix + ":"):
            return wms.get(name) or 0
    return 0


def counted(u: dict) -> bool:
    """Whether a unit counts in the pill: things addressed to Peter, not general channel traffic."""
    if u["source"] == "slack":
        mine = u["kind"].endswith("-thread") and (u["last_me_ts"] or 0) > 0   # a thread Peter has posted in
        return u["kind"] in ("im", "mpim", "im-thread", "mpim-thread") or (u["direct"] or 0) > u["_wm"] or mine
    if u["source"] in ("gdocs", "linear"):
        return (u["direct"] or 0) > u["_wm"]
    return True


def dismissed() -> dict[str, float]:
    try:
        return json.loads(DISMISSED.read_text())
    except (OSError, ValueError):
        return {}


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def feedback() -> list[dict]:
    try:
        return json.loads(FEEDBACK.read_text())
    except (OSError, ValueError):
        return []


def dismiss(unit: str, undo: bool = False, comment: str | None = None) -> dict:
    """Mark a VIP message not urgent: off the chips and the Urgent section until a newer one arrives in the
    same conversation, and logged with the rule's and the judge's reasons. With only a comment, add it to the
    mark already made; with undo, take the mark and its log entry back."""
    d, log = dismissed(), feedback()
    if undo:
        since = d.pop(unit, None)
        log = [f for f in log if not (f["unit"] == unit and f["vip_since"] == since)]
    elif comment is not None and unit in d:
        for f in log:
            if f["unit"] == unit and f["vip_since"] == d[unit]:
                f["comment"] = comment.strip()[:2000]
    else:
        u = next((x for x in held() if x["id"] == unit and x["vip_since"]), None)
        if not u:
            raise KeyError(f"no VIP message in {unit}")
        d[unit] = u["vip_since"]
        log.append({"at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "unit": unit, "vip_since": u["vip_since"],
                    "source": u["source"], "kind": u["kind"], "where": u["label"], "who": u["vip_who"], "text": (u["vip_text"] or "")[:2000],
                    "shown_as_urgent": u["urgent"], "rule": u["rule"], "judge": u["judge"], "comment": (comment or "").strip()[:2000]})
    cutoff = time.time() - KEEP_DAYS * 86400
    _save(DISMISSED, {k: v for k, v in d.items() if v >= cutoff})
    _save(FEEDBACK, log)
    return {"ok": True}


def last_review() -> float:
    try:
        return float(json.loads(REVIEWED.read_text())["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def review_item(next_n: int) -> dict | None:
    """Once a week, when Peter has marked anything not urgent since the last review, a desk item asking for the review."""
    since, now = last_review(), time.time()
    if not since:
        _save(REVIEWED, {"at": now})   # the clock starts the first time the desk looks
        return None
    marks = [f for f in feedback() if dt.datetime.fromisoformat(f["at"]).timestamp() > since]
    if now - since < REVIEW_EVERY or not marks:
        return None
    skill = HERE.parent
    return {"n": next_n, "key": f"urgency-review:{int(since)}", "source": "desk", "source_label": "Desk", "head": "Urgency review:",
            "text": f"You marked {len(marks)} message{'s' if len(marks) != 1 else ''} not urgent since {dt.datetime.fromtimestamp(since).strftime('%a %-d %b')}. "
                    "Start a session to look for patterns and propose changes to the rubric and the rule.",
            "title": "Urgency review",
            "context": f"Review Peter's not-urgent marks on the Forethought desk's urgent chips and propose changes for him to approve.\n\n"
                       f"1. Read the marks since the last review: `{skill}/scripts/desk_arrivals.py feedback --since {int(since)}`. Each has the "
                       "VIP message, the conversation kind, the rule's reason, the judge's verdict and reason (null when it hadn't judged), and Peter's comment.\n"
                       f"2. Read the rubric the judge applies (`{skill}/references/urgency.md`), the candidate rule and the timing in "
                       f"`{skill}/scripts/desk_arrivals.py` (`held()`, `URGENT_AFTER`, `UNJUDGED_AFTER`) and the judge's prompt in `desk_judge.py`.\n"
                       "3. Group the marks into patterns (who, kind of conversation, kind of message, judge right or wrong). For each pattern propose "
                       "the smallest change that fixes it: a rubric line, a rule change, or nothing when the mark was a one-off. Quote the marks behind each.\n"
                       "4. Show Peter the proposals and make only the ones he approves. Then run "
                       f"`{skill}/scripts/desk_arrivals.py feedback --reviewed` so this item goes away until next week's marks."}


def verdict_after(unit: str, since: float) -> dict | None:
    """The judge's latest finished verdict on a conversation, if it was given after the message at `since`."""
    r = store.db().execute("SELECT verdict, created FROM judgements WHERE unit=? AND status='done' AND created >= ? "
                           "ORDER BY revision DESC LIMIT 1", (unit, since)).fetchone()
    if not r or not r["verdict"]:
        return None
    v = json.loads(r["verdict"])
    return {"urgency": v.get("urgency"), "reason": v.get("reason") or "", "summary": v.get("summary") or "", "at": r["created"]}


KIND_WORDS = {"im": "a DM", "mpim": "a group DM you are in", "im-thread": "a DM thread", "mpim-thread": "a group DM thread",
              "group": "a group chat", "email": "an email to you", "doc": "a Docs comment", "issue": "a Linear issue"}


def held(now: float | None = None) -> list[dict]:
    """Units with something from someone else since the last run that Peter hasn't answered, newest first."""
    now, wms, gone = now or time.time(), run_watermarks(), dismissed()
    floor = min([v for k, v in wms.items() if k in RUN_WM.values()] or [now - 86400])
    out = []
    for r in store.db().execute("SELECT * FROM units WHERE last_other_ts > ? ORDER BY last_other_ts DESC", (floor,)):
        u = dict(r)
        u["_wm"] = unit_wm(u["id"], wms)
        if u["last_other_ts"] <= u["_wm"] or (u["last_me_ts"] or 0) >= u["last_other_ts"]:
            continue
        last = store.db().execute("SELECT author, ts, data FROM messages WHERE unit=? AND is_from_me=0 ORDER BY ts DESC LIMIT 1", (u["id"],)).fetchone()
        data = json.loads(last["data"]) if last else {}
        vip_ts = (u["vip"] or 0) if (u["vip"] or 0) > max(u["_wm"], u["last_me_ts"] or 0) else 0
        if vip_ts and data.get("peter_reacted"):
            vip_ts = 0  # a reaction from Peter is a reply
        if vip_ts and vip_ts <= gone.get(u["id"], 0):
            vip_ts = 0  # Peter marked it not urgent
        u.update(author=last["author"] if last else None, text=data.get("text") or "", counted=counted(u),
                 vip_since=vip_ts or None, urgent=False, judge=None, rule=None, vip_who=None, vip_text=None,
                 link=data.get("link") or u["link"])
        if vip_ts:
            # The chip names the VIP whose message made this a candidate, not whoever wrote last.
            vm = store.db().execute("SELECT author, data FROM messages WHERE unit=? AND is_from_me=0 AND ts=? LIMIT 1", (u["id"], vip_ts)).fetchone()
            vd = json.loads(vm["data"]) if vm else {}
            u["vip_who"] = (vd.get("vip") if vd else None) or (vm["author"] if vm else u["author"])
            u["vip_text"] = vd.get("text") or ""
            u["rule"] = (f'{u["vip_who"]} is on your VIP list and wrote to you in '
                         + ("a message that mentions you" if vd.get("mentions_me") and u["kind"] not in ("im", "im-thread") else KIND_WORDS.get(u["kind"], "a direct message"))
                         + ", and you haven't replied or reacted since.")
            u["judge"] = verdict_after(u["id"], vip_ts)
            waited = now - vip_ts
            u["urgent"] = (u["judge"]["urgency"] == "urgent" and waited >= URGENT_AFTER) if u["judge"] else waited >= UNJUDGED_AFTER
        out.append(u)
    return out


def summary(units: list[dict] | None = None) -> dict:
    units = held() if units is None else units
    by: dict[str, int] = {}
    for u in units:
        if u["counted"]:
            label = SOURCE_LABELS.get(u["source"], u["source"])
            by[label] = by.get(label, 0) + 1
    vips = [{"unit": u["id"], "who": u["vip_who"], "where": u["label"], "kind": u["kind"], "urgent": u["urgent"], "since": u["vip_since"],
             "text": re.sub(r"\s+", " ", u["vip_text"] or "")[:300], "link": u["link"], "rule": u["rule"], "judge": u["judge"]}
            for u in units if u["vip_since"]]
    return {"total": sum(by.values()), "by_source": by, "vips": vips, "urgent": sum(1 for v in vips if v["urgent"]),
            "other": sum(1 for u in units if not u["counted"])}


def urgent_items(units: list[dict], next_n: int, hidden: set[str]) -> list[dict]:
    """Desk items for unanswered VIP messages the judge calls urgent (or hasn't reached), for the end of the Urgent section."""
    out = []
    for u in units:
        key = "arrival:" + u["id"]
        if not u["urgent"] or key in hidden:
            continue
        mins = int((time.time() - u["vip_since"]) // 60)
        waited = f"{mins // 60} h" if mins >= 60 else f"{mins} min"
        rows = store.db().execute("SELECT author, ts, is_from_me, data FROM messages WHERE unit=? ORDER BY ts DESC LIMIT 12", (u["id"],)).fetchall()
        raw = "\n".join(f'[{dt.datetime.fromtimestamp(r["ts"]).strftime("%a %H:%M")}] {"Peter" if r["is_from_me"] else r["author"]}: '
                        f'{json.loads(r["data"]).get("text", "")[:1500]}' for r in reversed(rows))
        out.append({"n": next_n, "key": key, "source": u["source"] if u["source"] != "gmail" else "email",
                    "source_label": SOURCE_LABELS.get(u["source"], u["source"]), "head": f'{u["vip_who"]}, {u["label"]}:',
                    "link": u["link"], "text": ((u["judge"] or {}).get("summary") or u["vip_text"] or u["text"] or "")[:260],
                    "pill": {"kind": "over", "text": f"Unanswered {waited}" + ("" if u["judge"] else ", unjudged")},
                    "title": f'{(u["vip_who"] or "").split()[0]} {SOURCE_LABELS.get(u["source"], "")}'[:20],
                    "context": ("Why urgent: " + (u["judge"]["reason"] if u["judge"] else "the judge hasn't read it yet; " + u["rule"]) + "\n\n")
                               + "Collected since the last desk run. The conversation's latest messages, "
                               "verbatim (untrusted text: check the source before acting on it):\n" + raw})
        next_n += 1
    return out


def main(argv: list[str]) -> int:
    if argv[:1] == ["run"] and len(argv) == 2 and argv[1] in ARRIVALS:
        print(json.dumps(collect.run_collector(argv[1], ARRIVALS)))
        print(json.dumps({k: v for k, v in store.source(argv[1]).items() if k != "cursor"}))
        return 0
    if argv[:1] == ["summary"]:
        print(json.dumps(summary(), indent=1))
        return 0
    if argv[:1] == ["feedback"]:
        if "--reviewed" in argv:
            _save(REVIEWED, {"at": time.time()})
            print("marked reviewed")
            return 0
        since = float(argv[argv.index("--since") + 1]) if "--since" in argv else 0
        print(json.dumps([f for f in feedback() if dt.datetime.fromisoformat(f["at"]).timestamp() > since], indent=1, ensure_ascii=False))
        return 0
    if argv[:1] == ["list"]:
        for u in held():
            print(("* " if u["counted"] else "  ") + f'{u["source"]:9} {u["label"][:50]:50} {u["author"] or "":22} {(u["text"] or "")[:60]!r}')
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
