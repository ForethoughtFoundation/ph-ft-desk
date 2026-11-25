#!/usr/bin/env python3
"""Email and Slack threads inside the desk: read a desk item's whole thread, and reply to it. Calendar items
show their event's details, with Accept, Maybe and Decline.

Routes (POST JSON; the server checks the page's token):
  /api/threads/get    {key, href?, fresh?}                       the thread, newest last, with reply defaults
  /api/threads/send   {key, href?, text, to?, cc?, subject?,     Peter's reply, exactly as his composer shows it
                       reply_to_id?, mode?}
  /api/threads/rsvp   {key, href?, status, note?}                Peter's answer to a calendar invitation

An answer goes only when Peter presses Accept, Maybe or Decline, in the panel or the row's hover actions.
The event answered is the one the server read for that item (for a series invitation, the whole series).

Nothing here sends on its own. /api/threads/send runs only when Peter presses Send (or ⌘↵) on a composer
that shows where the reply goes: for email the To, Cc and Subject, for Slack the conversation and whether
it goes in the thread. That button press is his approval of the text, as with the Comments tab's replies.
Slack replies are his own words, so they go without the slack skill's "[agent] " prefix. The reply target
(the Gmail message, the Slack channel and thread) is the one the server found when it last read the thread
for that item, never one the page names.

FT_DESK_THREADS_DRY_RUN=1 in the server's environment makes the send route return the command it would
run, and send nothing.

Reading: Gmail through `gog --json gmail thread get`; Slack through the slack skill's client
(conversations.replies, or the conversation around a message that has no thread). Message bodies, names
and links are untrusted: HTML email is reduced to text here, and the page renders everything as text.

Run as a script, this file is the Slack worker the routes call in a subprocess, with the slack skill's
Python, so a hung request can't hold a server thread:  desk_threads.py slack-read|slack-send < JSON
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from desk_config import CONFIG, path as cfg_path  # noqa: E402

ACCT = CONFIG["gmail"]["account"]
MY_EMAILS = {e.lower() for e in [ACCT, *CONFIG.get("gdocs", {}).get("my_emails", [])]}
SLACK_CFG = CONFIG["slack"]
SLACK_CLI = cfg_path("slack")
SLACK_PY = SLACK_CLI.parent.parent / ".venv" / "bin" / "python"
DRY_RUN = os.environ.get("FT_DESK_THREADS_DRY_RUN") == "1"
CAL_ACCT = CONFIG["calendar"]["account"]
CAL_ID = CONFIG["calendar"].get("calendar_id", "primary")

KEY = re.compile(r"[A-Za-z0-9:._@+\-]{1,300}")
HEX16 = re.compile(r"[0-9a-f]{16}")
EVENT_KEY = re.compile(r"cal(?:-invite)?:([A-Za-z0-9_]{5,200})(?::[0-9TZ:.+\-]{0,40})?")
EVENT_ID = re.compile(r"[A-Za-z0-9_]{5,200}")
RSVP = {"accepted": "Accepted", "tentative": "Answered maybe", "declined": "Declined"}
GMAIL_ID = re.compile(r"[0-9a-f]{12,24}")
SLACK_KEY = re.compile(r"slack(?:-todo)?:([a-z0-9_-]{1,40}):([CDG][A-Z0-9]{6,15}):(\d{10})\.?(\d{6})")
CHANNEL = re.compile(r"[CDG][A-Z0-9]{6,15}")
TS = re.compile(r"\d{10}\.\d{6}")
WS = re.compile(r"[a-z0-9_-]{1,40}")
ADDR = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,62}[A-Za-z0-9])?)+")
CACHE_TTL = 60
MAX_EMAIL_MESSAGES = 60
MAX_SLACK_MESSAGES = 200
SLACK_CONTEXT = 12          # messages either side of a lone message, so the conversation around it shows
SLACK_EVENT_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
                        "channel_archive", "channel_unarchive", "pinned_item", "group_join", "group_leave"}

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}     # key -> (fetched at, thread)
_targets: dict[str, dict] = {}                 # key -> where a reply goes, from the last read
_recent: dict[str, float] = {}                 # hash of a sent reply -> when, against double sends


# ---------------------------------------------------------------- which thread an item is
def _item(key: str) -> dict | None:
    try:
        import desk_live as live
        return live.item_index(live.live_desk()).get(key)
    except Exception:  # the desk couldn't be read: fall back to the key and the page's link
        return None


def _gmail_link_id(link: str) -> str | None:
    u = urlparse(link or "")
    if u.scheme != "https" or u.hostname != "mail.google.com" or not u.path.startswith("/mail/"):
        return None
    last = (u.fragment or "").rstrip("/").rsplit("/", 1)[-1]
    return last if GMAIL_ID.fullmatch(last) else None


def _calendar_link_id(link: str) -> str | None:
    """The event ID in a Google Calendar link (?eid= is base64 of "<event id> <calendar>")."""
    u = urlparse(link or "")
    if u.scheme != "https" or u.hostname not in ("www.google.com", "calendar.google.com"):
        return None
    eid = (parse_qs(u.query).get("eid") or [""])[0]
    try:
        first = base64.urlsafe_b64decode(eid + "=" * (-len(eid) % 4)).decode().split(" ")[0]
    except Exception:
        return None
    return first if EVENT_ID.fullmatch(first) else None


def _slack_domains() -> dict[str, str]:
    """Slack domain (forethought-ea) -> the desk's workspace name (forethought)."""
    out = {}
    for ws, c in SLACK_CFG.items():
        host = urlparse(c.get("url") or "").hostname or ""
        if host.endswith(".slack.com"):
            out[host[: -len(".slack.com")]] = ws
    return out


def _slack_link(link: str) -> dict | None:
    u = urlparse(link or "")
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host.endswith(".slack.com"):
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 3 or parts[0] not in ("archives", "messages") or not CHANNEL.fullmatch(parts[1]):
        return None
    m = re.fullmatch(r"p(\d{10})(\d{6})", parts[2])
    if not m:
        return None
    thread = (parse_qs(u.query).get("thread_ts") or [None])[0]
    return {"ws": _slack_domains().get(host[: -len(".slack.com")]), "channel": parts[1],
            "ts": f"{m.group(1)}.{m.group(2)}", "thread_ts": thread if thread and TS.fullmatch(thread) else None}


def resolve(key: str, href: str = "") -> dict:
    """What thread a desk item is: {"kind": "email", "thread_id"} or {"kind": "slack", "ws", "channel", "ts", "thread_ts"}.
    The desk's own copy of the item (and its link) wins; the page's link is used only for an item the desk no longer lists."""
    key, href = str(key or ""), str(href or "")[:2000]
    if not KEY.fullmatch(key):
        raise ValueError("Not a desk item key")
    item = _item(key)
    link = (item or {}).get("link") or href
    source = ((item or {}).get("source") or "").lower()

    em, eid = EVENT_KEY.fullmatch(key), _calendar_link_id(link)
    if em or eid:
        return {"kind": "event", "event_id": em.group(1) if em else eid, "archive_keys": [key]}

    gid = _gmail_link_id(link)
    key_gid = key.split(":", 1)[1] if key.startswith("gmail:") else key if HEX16.fullmatch(key) else None
    if key_gid and not HEX16.fullmatch(key_gid):
        key_gid = None
    if gid or key_gid or source == "email":
        tid = gid or key_gid
        if not tid:
            raise ValueError("This email item has no Gmail thread to show")
        # The item's link names the email its text is about; the key may name an older thread of the same sender.
        keys = [key] + ([f"gmail:{tid}"] if tid != key_gid else [])
        return {"kind": "email", "thread_id": tid, "archive_keys": keys}

    sl = _slack_link(link)
    km = SLACK_KEY.fullmatch(key)
    if sl or km:
        d = dict(sl or {})
        if km:
            d.setdefault("channel", km.group(2))
            d.setdefault("ts", f"{km.group(3)}.{km.group(4)}")
            if not d.get("ws"):
                d["ws"] = km.group(1)
        if not d.get("ws"):
            d["ws"] = next(iter(SLACK_CFG))
        if d["ws"] not in SLACK_CFG or not WS.fullmatch(d["ws"]):
            raise ValueError(f"Slack workspace {d['ws']} isn't one the desk reads")
        return {"kind": "slack", "ws": d["ws"], "channel": d["channel"], "ts": d["ts"], "thread_ts": d.get("thread_ts"),
                "archive_keys": [key]}
    raise ValueError("This item has no email, Slack thread or calendar event to show")


# ---------------------------------------------------------------- Gmail
def _gog(args: list[str], timeout: int = 60, stdin: str | None = None) -> str:
    r = subprocess.run(["gog", *args, "--account", ACCT], capture_output=True, text=True, timeout=timeout, input=stdin)
    if r.returncode:
        raise RuntimeError("Gmail: " + re.sub(r"\s+", " ", (r.stderr or r.stdout or "gog failed").strip())[:300])
    return r.stdout


class _Text(HTMLParser):
    """HTML email to plain text: block elements become line breaks, links keep their address, scripts and styles go."""
    BLOCK = {"p", "div", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section",
             "article", "header", "footer", "hr", "pre", "center"}
    SKIP = {"script", "style", "head", "title", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.href: list[str | None] = []
        self.atext: list[int] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag in self.BLOCK:
            self.out.append("\n\n" if tag in ("p", "blockquote", "table", "h1", "h2", "h3") else "\n")
        elif tag == "li":
            self.out.append("\n• ")
        elif tag == "td":
            self.out.append(" ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.href.append(href if re.match(r"(https?:|mailto:)", href, re.I) else None)
            self.atext.append(len(self.out))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "a" and self.href:
            href, start = self.href.pop(), self.atext.pop()
            text = "".join(self.out[start:]).strip()
            if href and text and href.removeprefix("mailto:") not in text and len(href) < 300:
                self.out.append(f" ({href})")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"[ \t\r\n\f]+", " ", data))

    def text(self) -> str:
        s = "".join(self.out).replace("\xa0", " ")
        s = re.sub(r"[​-‍⁠﻿͏­]", "", s)
        s = "\n".join(line.strip() for line in s.split("\n"))
        return re.sub(r"\n{3,}", "\n\n", s).strip()


def html_to_text(src: str) -> str:
    p = _Text()
    try:
        p.feed(src)
        p.close()
    except Exception:  # malformed HTML: keep what was parsed
        pass
    return p.text()


def _walk(part: dict):
    yield part
    for p in part.get("parts") or []:
        yield from _walk(p)


def _hdrs(part: dict) -> dict[str, str]:
    return {(h.get("name") or "").lower(): h.get("value", "") for h in part.get("headers") or []}


def _decode(part: dict) -> str:
    data = (part.get("body") or {}).get("data") or ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)) if data else b""
    m = re.search(r'charset="?([A-Za-z0-9_\-]+)', _hdrs(part).get("content-type", ""))
    try:
        return raw.decode(m.group(1) if m else "utf-8", "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


QUOTE_STARTS = [
    re.compile(r"^On [^\n]{0,300}(?:\n[^\n]{0,300})?\bwrote:[ \t]*$", re.M),
    re.compile(r"^Le [^\n]{0,300}(?:\n[^\n]{0,300})?a écrit\s?:[ \t]*$", re.M),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.M | re.I),
    re.compile(r"^_{10,}\s*\nFrom: ", re.M),
    re.compile(r"^\*?\s?From\*?:\s[^\n]+\n(?:[^\n]*\n)?\*?\s?(?:Sent|Date|To)\*?:\s", re.M),
]


def split_quoted(text: str) -> tuple[str, str]:
    """The new part of a plain-text email, and the quoted history and signature after it (shown on request)."""
    text = text.replace("\r\n", "\n").strip("\n")
    cut = len(text)
    for rx in QUOTE_STARTS:
        m = rx.search(text)
        if m and m.start() < cut:
            cut = m.start()
    # A run of "> " lines that reaches the end, blank lines allowed.
    lines = text.split("\n")
    i = len(lines)
    while i > 0 and (lines[i - 1].startswith(">") or not lines[i - 1].strip()):
        i -= 1
    if i < len(lines) and any(line.startswith(">") for line in lines[i:]):
        cut = min(cut, len("\n".join(lines[:i])))
    main, rest = text[:cut].rstrip(), text[cut:].strip("\n")
    sig = main.rfind("\n-- \n")
    if sig > 0:
        main, rest = main[:sig].rstrip(), (main[sig + 1:] + ("\n\n" + rest if rest else "")).strip("\n")
    if not main.strip():   # all of it is quoted (a bare forward): show it all
        return text, ""
    if not re.sub(r"[>\s]", "", rest):
        rest = ""
    return main, rest


def unwrap(text: str) -> str:
    """Undo the hard line wrapping of plain-text email (about 76 characters a line), so paragraphs reflow in the panel.
    A line joins the next when the next line's first word wouldn't have fitted on it; lists, quotes and short lines stay."""
    lines = text.split("\n")
    widths = [len(line) for line in lines if 50 <= len(line) <= 80 and not line.startswith(">")]
    if len(widths) < 2:
        return text
    wrap = max(widths)
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not nxt.strip() or line.startswith(">") or nxt.startswith(">") or re.match(r"\s*([-*•–]|\d+[.)])\s", nxt):
            out.append("\n")
            continue
        first = nxt.split()[0] if nxt.split() else ""
        out.append(" " if len(line.rstrip()) >= 40 and len(line.rstrip()) + 1 + len(first) > wrap - 2 else "\n")
    return "".join(out[:-1]).rstrip() if out else text


def _addrs(value: str) -> list[dict]:
    out = []
    for name, addr in getaddresses([value or ""]):
        if addr and ADDR.fullmatch(addr):
            out.append({"name": name.strip().strip('"'), "addr": addr})
    return out


def _fmt(a: dict) -> str:
    return f'{a["name"]} <{a["addr"]}>' if a["name"] and a["name"].lower() != a["addr"].lower() else a["addr"]


def _gmail_message(m: dict) -> dict:
    payload = m.get("payload") or {}
    h = _hdrs(payload)
    plain = htm = None
    files = []
    for p in _walk(payload):
        ph = _hdrs(p)
        disp = ph.get("content-disposition", "").lower()
        name = p.get("filename")
        if name:
            if not (disp.startswith("inline") and ph.get("content-id")):   # inline images (logos) aren't attachments
                files.append({"name": name[:200], "size": (p.get("body") or {}).get("size") or 0})
            continue
        if disp.startswith("attachment"):
            continue
        mt = (p.get("mimeType") or "").lower()
        if mt == "text/plain" and plain is None:
            plain = _decode(p)
        elif mt == "text/html" and htm is None:
            htm = _decode(p)
    body = unwrap(plain.replace("\r\n", "\n")) if plain and plain.strip() else html_to_text(htm) if htm else ""
    body = re.sub(r"[​-‍⁠﻿͏]", "", body)
    body = re.sub(r"\n[ \t]*(?:\n[ \t]*){2,}", "\n\n", body)   # runs of blank lines (HTML mail's plain part) become one
    main, quoted = split_quoted(body)
    frm = (_addrs(h.get("from", "")) or [{"name": parseaddr(h.get("from", ""))[0], "addr": ""}])[0]
    return {"id": m.get("id"), "ts": int(m.get("internalDate") or 0) / 1000, "draft": "DRAFT" in (m.get("labelIds") or []),
            "author": frm["name"] or frm["addr"] or "Unknown sender", "addr": frm["addr"],
            "me": frm["addr"].lower() in MY_EMAILS,
            "to": [_fmt(a) for a in _addrs(h.get("to", ""))], "cc": [_fmt(a) for a in _addrs(h.get("cc", ""))],
            "_to": _addrs(h.get("to", "")), "_cc": _addrs(h.get("cc", "")), "_reply_to": _addrs(h.get("reply-to", "")),
            "_from": frm, "subject": h.get("subject", ""), "body": main[:60000], "quoted": quoted[:60000], "files": files}


def _uniq(people: list[dict], drop: set[str]) -> list[dict]:
    out, seen = [], set(drop)
    for a in people:
        k = a["addr"].lower()
        if k not in seen:
            seen.add(k)
            out.append(a)
    return out


def reply_defaults(target: dict) -> dict:
    """Gmail's Reply all and Reply, for the message replied to: To the sender (or Reply-To) and the other To
    recipients, Cc the Cc recipients, never Peter himself; on his own message, the people he wrote to."""
    if target["me"]:
        to_all = _uniq(target["_to"], MY_EMAILS)
        cc_all = _uniq(target["_cc"], MY_EMAILS | {a["addr"].lower() for a in to_all})
        to_one = to_all
    else:
        sender = target["_reply_to"] or [target["_from"]]
        to_all = _uniq(sender + target["_to"], MY_EMAILS)
        cc_all = _uniq(target["_cc"], MY_EMAILS | {a["addr"].lower() for a in to_all})
        to_one = _uniq(sender, MY_EMAILS)
    subject = target["subject"].strip()
    if not re.match(r"(re|aw|sv)\s*:", subject, re.I):
        subject = "Re: " + subject
    return {"reply_to_id": target["id"], "subject": subject,
            "all": {"to": [_fmt(a) for a in to_all], "cc": [_fmt(a) for a in cc_all]},
            "one": {"to": [_fmt(a) for a in to_one], "cc": []}}


def email_thread(d: dict) -> dict:
    data = json.loads(_gog(["--json", "gmail", "thread", "get", d["thread_id"]]) or "{}")
    msgs = [_gmail_message(m) for m in (data.get("thread") or {}).get("messages") or []]
    msgs = sorted((m for m in msgs if not m["draft"]), key=lambda m: m["ts"])[-MAX_EMAIL_MESSAGES:]
    if not msgs:
        raise RuntimeError("Gmail returned no messages for this thread")
    target = msgs[-1]
    reply = reply_defaults(target)
    for m in msgs:
        for k in ("_to", "_cc", "_reply_to", "_from", "draft"):
            m.pop(k, None)
    return {"kind": "email", "title": msgs[0]["subject"] or "(no subject)", "account": ACCT,
            "link": f"https://mail.google.com/mail/u/0/#all/{d['thread_id']}", "messages": msgs, "reply": reply,
            "_target": {"kind": "email", "reply_to_id": target["id"], "thread_id": d["thread_id"]}}


# ---------------------------------------------------------------- Slack (main process side)
def _slack_worker(cmd: str, req: dict, timeout: int) -> dict:
    py = str(SLACK_PY) if SLACK_PY.exists() else sys.executable
    r = subprocess.run([py, "-I", str(Path(__file__).resolve()), cmd], input=json.dumps(req),
                       capture_output=True, text=True, timeout=timeout)
    lines = [line for line in r.stdout.strip().splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("Slack: " + (re.sub(r"\s+", " ", r.stderr.strip())[-300:] or "the Slack client gave no answer"))
    out = json.loads(lines[-1])
    return out


def _slack_error(err: str) -> str:
    if err == "invalid_auth" or err == "not_authed":
        return f"Slack says {err}: the desk's Slack tokens have expired. Refresh them as in the slack skill's references/setup.md."
    return f"Slack says {err}"


def _permalink(ws: str, channel: str, ts: str, root: str | None) -> str:
    host = urlparse(SLACK_CFG[ws].get("url") or "").hostname or "app.slack.com"
    link = f"https://{host}/archives/{channel}/p{ts.replace('.', '')}"
    return link + (f"?thread_ts={root}&cid={channel}" if root and root != ts else "")


def slack_thread(d: dict) -> dict:
    tried = []
    for ws in [d["ws"]] + [w for w in SLACK_CFG if w != d["ws"]]:   # a shared channel may answer in another workspace
        out = _slack_worker("slack-read", {"ws": ws, "channel": d["channel"], "ts": d["ts"], "thread_ts": d.get("thread_ts"),
                                           "me": SLACK_CFG[ws].get("my_user_id")}, 90)
        if out.get("ok"):
            break
        tried.append(out.get("error") or "failed")
        if out.get("error") not in ("channel_not_found", "thread_not_found", "not_in_channel"):
            raise RuntimeError(_slack_error(out.get("error") or "unknown error"))
    else:
        raise RuntimeError(_slack_error(tried[0] if tried else "channel_not_found"))
    root = out["root"]
    item_top = d["ts"] == root
    where = out["channel_name"]
    return {"kind": "slack", "title": where, "workspace": ws, "mode": out["mode"],
            "link": _permalink(ws, d["channel"], d["ts"], root), "messages": out["messages"], "focus_ts": d["ts"],
            "reply": {"where": where, "is_dm": d["channel"].startswith("D"), "can_post_to_channel": item_top,
                      "thread_has_replies": out["mode"] == "thread"},
            "_target": {"kind": "slack", "ws": ws, "channel": d["channel"], "thread_ts": root, "can_post_to_channel": item_top,
                        "where": where}}


# ---------------------------------------------------------------- routes
def _public(t: dict) -> dict:
    return {k: v for k, v in t.items() if not k.startswith("_")}


def get(body: dict) -> dict:
    key = str(body.get("key") or "")
    d = resolve(key, str(body.get("href") or ""))
    with _lock:
        hit = _cache.get(key)
    if hit and not body.get("fresh") and time.time() - hit[0] < CACHE_TTL:
        return _public(hit[1])
    t = {"email": email_thread, "slack": slack_thread, "event": event_detail}[d["kind"]](d)
    t["key"], t["archive_keys"] = key, d["archive_keys"]
    with _lock:
        _cache[key] = (time.time(), t)
        _targets[key] = t["_target"]
        for k in [k for k, (at, _) in _cache.items() if time.time() - at > 600]:
            _cache.pop(k, None)
    return _public(t)


def _target(key: str, href: str) -> dict:
    with _lock:
        t = _targets.get(key)
    if t:
        return t
    get({"key": key, "href": href, "fresh": True})
    with _lock:
        return _targets[key]


def _recipients(value, label: str) -> list[str]:
    raw = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value or "")
    if re.search(r"[\r\n]", raw) or len(raw) > 5000:
        raise ValueError(f"{label} has a line break or is too long")
    out = []
    for chunk in [c for c in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", raw) if c.strip()]:
        name, addr = parseaddr(chunk.strip())
        if not addr or not ADDR.fullmatch(addr):
            raise ValueError(f"{label}: “{chunk.strip()[:80]}” isn't an email address")
        out.append(addr)
    return out


def _once(sig: str) -> None:
    now = time.time()
    with _lock:
        for k in [k for k, at in _recent.items() if now - at > 30]:
            _recent.pop(k, None)
        if sig in _recent:
            raise ValueError("That reply was sent a moment ago; not sending it twice")
        _recent[sig] = now


def send(body: dict) -> dict:
    key, href = str(body.get("key") or ""), str(body.get("href") or "")
    d = resolve(key, href)
    text = str(body.get("text") or "").replace("\r\n", "\n").rstrip()
    if not text.strip():
        raise ValueError("Nothing to send")
    if len(text) > 100_000:
        raise ValueError("That reply is too long")
    if d["kind"] == "event":
        raise ValueError("A calendar event takes an answer, not a reply")
    t = _target(key, href)
    if t["kind"] != d["kind"]:
        raise ValueError("The item changed; reopen its thread")
    return _send_email(key, t, body, text) if d["kind"] == "email" else _send_slack(key, t, body, text)


def _send_email(key: str, t: dict, body: dict, text: str) -> dict:
    if str(body.get("reply_to_id") or "") != t["reply_to_id"]:
        with _lock:
            _cache.pop(key, None)
        raise ValueError("The thread has a newer message; check it, then send again")
    to, cc = _recipients(body.get("to"), "To"), _recipients(body.get("cc"), "Cc")
    if not to:
        raise ValueError("Add someone in To")
    if len(to) + len(cc) > 50:
        raise ValueError("Too many recipients")
    subject = str(body.get("subject") or "").strip()
    if not subject or re.search(r"[\r\n]", subject) or len(subject) > 500:
        raise ValueError("The subject must be one line")
    argv = ["gog", "gmail", "send", "--reply-to-message-id", t["reply_to_id"], "--to", ",".join(to),
            *(["--cc", ",".join(cc)] if cc else []), "--subject", subject, "--body-file", "-", "--account", ACCT]
    _once(hashlib.sha256(json.dumps([key, to, cc, subject, text]).encode()).hexdigest())
    if DRY_RUN:
        return {"dry_run": True, "message": "Dry run: nothing sent", "argv": argv, "stdin": text}
    _gog(argv[1:-2], timeout=90, stdin=text)
    with _lock:
        _cache.pop(key, None)
    return {"message": "Sent to " + ", ".join(to + cc)}


def _send_slack(key: str, t: dict, body: dict, text: str) -> dict:
    if len(text) > 12000:
        raise ValueError("That message is too long for Slack")
    mode = str(body.get("mode") or "thread")
    if mode not in ("thread", "channel"):
        raise ValueError("Bad mode")
    if mode == "channel" and not t["can_post_to_channel"]:
        raise ValueError("This message sits in a thread; the reply goes in the thread")
    req = {"ws": t["ws"], "channel": t["channel"], "thread_ts": t["thread_ts"] if mode == "thread" else None, "text": text}
    if not (CHANNEL.fullmatch(req["channel"]) and WS.fullmatch(req["ws"]) and (req["thread_ts"] is None or TS.fullmatch(req["thread_ts"]))):
        raise ValueError("Bad Slack target")
    _once(hashlib.sha256(json.dumps([key, mode, text]).encode()).hexdigest())
    where = t["where"] + (", in the thread" if mode == "thread" else "")
    if DRY_RUN:
        return {"dry_run": True, "message": "Dry run: nothing sent", "where": where,
                "call": {"workspace": req["ws"], "method": "chat.postMessage", "channel": req["channel"],
                         "thread_ts": req["thread_ts"], "text": slack_escape(text)}}
    try:
        out = _slack_worker("slack-send", req, 60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Slack didn't answer in time; the message may have been posted, so check the thread before sending again")
    if not out.get("ok"):
        raise RuntimeError(_slack_error(out.get("error") or "unknown error"))
    with _lock:
        _cache.pop(key, None)
    return {"message": "Sent to " + where, "ts": out.get("ts")}


# ---------------------------------------------------------------- Calendar
def _gcal(args: list[str], timeout: int = 60) -> str:
    r = subprocess.run(["gog", *args, "--account", CAL_ACCT], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError("Calendar: " + re.sub(r"\s+", " ", (r.stderr or r.stdout or "gog failed").strip())[:300])
    return r.stdout


def _ts(v: str | None) -> float:
    import datetime as dt
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() if v else 0.0


def _clashes(e: dict) -> list[dict]:
    """Other timed events on the Forethought calendar that overlap this one. "Busy" blocks take the names the
    calendar collector gave them from Peter's personal calendars."""
    s, en = (e.get("start") or {}).get("dateTime"), (e.get("end") or {}).get("dateTime")
    if not s or not en:
        return []
    names = {}
    try:
        import desk_store as store
        for r in (store.snapshot("calendar") or {}).get("events") or []:
            if r.get("start") and "T" in r["start"]:
                names[_ts(r["start"])] = r.get("summary")
    except Exception:
        pass
    data = json.loads(_gcal(["--json", "cal", "events", CAL_ID, "--from", s, "--to", en, "--max", "50"]) or "{}")
    out = []
    for o in data.get("events") or []:
        os_, oe = (o.get("start") or {}).get("dateTime"), (o.get("end") or {}).get("dateTime")
        if o.get("id") == e.get("id") or o.get("status") == "cancelled" or not os_ or not oe:
            continue
        if not (_ts(os_) < _ts(en) and _ts(oe) > _ts(s)):
            continue
        me = next((a for a in o.get("attendees") or [] if a.get("self")), None)
        title = o.get("summary") or "Busy"
        if title.strip().lower() == "busy":
            title = names.get(_ts(os_)) or title
        out.append({"title": title, "start": os_, "end": oe, "response": me.get("responseStatus") if me else None})
    return sorted(out, key=lambda c: _ts(c["start"]))


def event_detail(d: dict) -> dict:
    data = json.loads(_gcal(["--json", "cal", "event", CAL_ID, d["event_id"]]) or "{}")
    e = data.get("event") or {}
    if not e.get("id"):
        raise RuntimeError("Calendar: the event wasn't found")
    start, end = e.get("start") or {}, e.get("end") or {}
    me = next((a for a in e.get("attendees") or [] if a.get("self")), None)
    org = e.get("organizer") or {}
    order = {"accepted": 0, "tentative": 1, "needsAction": 2, "declined": 3}
    people = sorted(({"name": a.get("displayName") or "", "email": a.get("email") or "",
                      "response": a.get("responseStatus") or "needsAction", "me": bool(a.get("self")),
                      "organiser": bool(a.get("organizer")), "optional": bool(a.get("optional"))}
                     for a in e.get("attendees") or [] if not a.get("resource")),
                    key=lambda p: (not p["me"], not p["organiser"], order.get(p["response"], 2), (p["name"] or p["email"]).lower()))
    desc = e.get("description") or ""
    if re.search(r"<(br|p|div|a|ul|ol|li|b|i|span|strong|em)\b", desc, re.I):
        desc = html_to_text(desc)
    meet = e.get("hangoutLink") or next((p.get("uri") for p in (e.get("conferenceData") or {}).get("entryPoints") or []
                                         if p.get("entryPointType") == "video"), None)
    try:
        clashes = _clashes(e)
    except Exception:
        clashes = []
    return {"kind": "event", "title": e.get("summary") or "(No title)", "link": e.get("htmlLink"),
            "start": start.get("dateTime") or start.get("date"), "end": end.get("dateTime") or end.get("date"),
            "all_day": "date" in start and "dateTime" not in start,
            "series": bool(e.get("recurrence")), "instance": bool(e.get("recurringEventId")),
            "location": e.get("location"), "meet": meet, "cancelled": e.get("status") == "cancelled",
            "organiser": {"name": org.get("displayName") or "", "email": org.get("email") or "", "me": bool(org.get("self"))},
            "description": desc[:20000], "attendees": people, "clashes": clashes,
            "my_response": me.get("responseStatus") if me else None, "can_respond": bool(me),
            "_target": {"kind": "event", "event_id": e["id"], "can_respond": bool(me)}}


def rsvp(body: dict) -> dict:
    key, href = str(body.get("key") or ""), str(body.get("href") or "")
    status = str(body.get("status") or "")
    if status not in RSVP:
        raise ValueError("The answer must be accepted, tentative or declined")
    note = str(body.get("note") or "").replace("\r\n", "\n").strip()
    if len(note) > 2000:
        raise ValueError("That note is too long")
    if resolve(key, href)["kind"] != "event":
        raise ValueError("This item isn't a calendar event")
    t = _target(key, href)
    if t.get("kind") != "event":
        raise ValueError("The item changed; reopen it")
    if not t.get("can_respond"):
        raise ValueError("You aren't a guest on this event, so there's nothing to answer")
    args = ["cal", "respond", CAL_ID, t["event_id"], "--status", status] + (["--comment", note] if note else [])
    if DRY_RUN:
        return {"dry_run": True, "command": ["gog", *args, "--account", CAL_ACCT]}
    _gcal(args)
    with _lock:
        _cache.pop(key, None)
    try:
        import desk_store as store
        store.set_due("calendar", time.time() + 1)
    except Exception:
        pass
    return {"ok": True, "status": status, "message": RSVP[status]}


ROUTES = {"/api/threads/get": get, "/api/threads/send": send, "/api/threads/rsvp": rsvp}


# ---------------------------------------------------------------- Slack worker (runs in the subprocess)
def slack_escape(text: str) -> str:
    """Slack reads <, > and & as markup; Peter's text is literal (a quote's leading > still makes a quote)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


EMOJI = {"+1": "👍", "thumbsup": "👍", "-1": "👎", "heart": "❤️", "white_check_mark": "✅", "heavy_check_mark": "✔️",
         "eyes": "👀", "pray": "🙏", "joy": "😂", "tada": "🎉", "raised_hands": "🙌", "100": "💯", "fire": "🔥",
         "thinking_face": "🤔", "slightly_smiling_face": "🙂", "smile": "😄", "laughing": "😆", "rocket": "🚀",
         "clap": "👏", "ok_hand": "👌", "muscle": "💪", "sob": "😭", "grinning": "😀", "sweat_smile": "😅",
         "bulb": "💡", "star": "⭐", "x": "❌", "warning": "⚠️", "point_up": "☝️", "wave": "👋", "heart_eyes": "😍",
         "upside_down_face": "🙃", "rolling_on_the_floor_laughing": "🤣", "melting_face": "🫠", "saluting_face": "🫡",
         "pray::skin-tone-2": "🙏", "hourglass_flowing_sand": "⏳", "white_heart": "🤍", "orange_heart": "🧡",
         "green_heart": "💚", "blue_heart": "💙", "purple_heart": "💜", "partying_face": "🥳", "handshake": "🤝",
         "brain": "🧠", "memo": "📝", "calendar": "📆", "thank_you": "🙏", "sparkles": "✨", "slightly_frowning_face": "🙁"}


def _segments(text: str, names) -> list[dict]:
    """Slack markup as segments the page renders as text: {"t"} text, {"m"} a mention, {"u", "t"} a link."""
    out, pos = [], 0
    emoji = lambda s: re.sub(r":([a-z0-9_+\-]{1,40}):", lambda m: EMOJI.get(m.group(1), m.group(0)), s)
    un = lambda s: emoji(s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    for m in re.finditer(r"<([^<>\n]{1,1000})>", text or ""):
        if m.start() > pos:
            out.append({"t": un(text[pos:m.start()])})
        target, _, label = m.group(1).partition("|")
        if target.startswith("@"):
            out.append({"m": "@" + (un(label) or names.user_name(target[1:]))})
        elif target.startswith("#"):
            out.append({"m": "#" + (un(label) or names.channel_name(target[1:]).lstrip("#"))})
        elif target.startswith("!"):
            out.append({"m": un(label) or "@" + target[1:].split("^")[0]})
        elif re.match(r"(https?://|mailto:)", target, re.I):
            out.append({"u": un(target), "t": un(label) or un(target).removeprefix("mailto:")})
        else:
            out.append({"t": un(m.group(0))})
        pos = m.end()
    if pos < len(text or ""):
        out.append({"t": un(text[pos:])})
    return out


def _slack_msg(msg: dict, names, me: str | None, blocks_text) -> dict | None:
    if msg.get("subtype") in SLACK_EVENT_SUBTYPES:
        return None
    uid = msg.get("user")
    author = names.user_name(uid, msg.get("user_profile")) if uid else \
        (msg.get("bot_profile") or {}).get("name") or msg.get("username") or "Bot"
    text = msg.get("text") or blocks_text(msg.get("blocks"))
    atts = []
    for a in msg.get("attachments") or []:
        title = a.get("title") or a.get("author_name") or ""
        link = a.get("title_link") or a.get("from_url") or ""
        body = a.get("text") or a.get("pretext") or ("" if title else a.get("fallback") or "")
        if title or body:
            atts.append({"title": html.unescape(title)[:300], "link": link if re.match(r"https?://", link or "") else "",
                         "segs": _segments((body or "")[:600], names)})
    files = [{"name": (f.get("name") or f.get("title") or "file")[:200],
              "link": f.get("permalink") if re.match(r"https://", f.get("permalink") or "") else ""}
             for f in msg.get("files") or []]
    reactions = []
    for r in msg.get("reactions") or []:
        name = r.get("name", "")
        reactions.append({"e": EMOJI.get(name) or EMOJI.get(name.split("::")[0]) or f":{name.split('::')[0]}:",
                          "n": r.get("count", 0), "name": name.split("::")[0]})
    return {"ts": msg.get("ts"), "author": author, "me": bool(me and uid == me), "segs": _segments(text, names),
            "atts": atts, "files": files, "reactions": reactions, "edited": bool(msg.get("edited")),
            "reply_count": msg.get("reply_count") or 0}


def _worker_client(ws: str):
    sys.path.insert(0, str(SLACK_CLI.parent))
    import slack_client as sc
    creds, ws = sc.load_config(ws)
    return sc, sc.make_client(creds)


def worker_read(req: dict) -> dict:
    ws, channel, ts, thread_ts, me = req["ws"], req["channel"], req["ts"], req.get("thread_ts"), req.get("me")
    if not (WS.fullmatch(ws) and CHANNEL.fullmatch(channel) and TS.fullmatch(ts) and (not thread_ts or TS.fullmatch(thread_ts))):
        return {"ok": False, "error": "bad request"}
    sc, client = _worker_client(ws)
    root = thread_ts or ts
    r = client.conversations_replies(channel, root)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "conversations.replies failed"}
    msgs = r.get("messages") or []
    parent = (msgs[0] if msgs else {}).get("thread_ts")
    if parent and parent != root:      # the item is a reply and its link didn't name the thread
        root = parent
        r = client.conversations_replies(channel, root)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "conversations.replies failed"}
        msgs = r.get("messages") or []
    names = sc.NameResolver(client, ws)
    mode = "thread"
    if len(msgs) <= 1:                 # a lone message: show the conversation around it
        mode = "conversation"
        before = client.conversations_history(channel, SLACK_CONTEXT, latest=root, inclusive=False)
        after = client.conversations_history(channel, SLACK_CONTEXT, oldest=root)
        if not before.get("ok"):
            return {"ok": False, "error": before.get("error") or "conversations.history failed"}
        msgs = list(reversed(before.get("messages") or [])) + msgs + list(reversed(after.get("messages") or []))
        seen, uniq = set(), []
        for m in sorted(msgs, key=lambda m: float(m.get("ts") or 0)):
            if m.get("ts") not in seen:
                seen.add(m.get("ts"))
                uniq.append(m)
        msgs = uniq
    out = [x for m in msgs[-MAX_SLACK_MESSAGES:] if (x := _slack_msg(m, names, me, sc.blocks_text))]
    name = names.channel_name(channel)
    names.save()
    return {"ok": True, "root": root, "mode": mode, "channel_name": name, "messages": out}


def worker_send(req: dict) -> dict:
    ws, channel, thread_ts, text = req["ws"], req["channel"], req.get("thread_ts"), req["text"]
    if not (WS.fullmatch(ws) and CHANNEL.fullmatch(channel) and (not thread_ts or TS.fullmatch(thread_ts)) and text.strip()):
        return {"ok": False, "error": "bad request"}
    _, client = _worker_client(ws)
    # Peter typed this himself: posted as he wrote it, without the slack skill's "[agent] " prefix.
    r = client.post_message(channel, slack_escape(text), thread_ts)
    return {"ok": bool(r.get("ok")), "error": r.get("error"), "ts": r.get("ts")}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        req = json.loads(sys.stdin.read() or "{}")
        res = worker_read(req) if cmd == "slack-read" else worker_send(req) if cmd == "slack-send" else {"ok": False, "error": "unknown command"}
    except Exception as e:  # reported to the server as an error, not a traceback
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(res, ensure_ascii=False))
