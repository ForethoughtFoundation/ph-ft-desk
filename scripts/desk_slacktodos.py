#!/usr/bin/env python3
"""Slack to-dos from the desk: tick one off, change its due date or snooze it, on Peter's own Slack To-dos list.

Routes (POST JSON; the server checks the page's token):
  /api/slacktodo/due     {key, due}      epoch seconds, or null to clear the due date
  /api/slacktodo/snooze  {key, until}    epoch seconds, or null to wake it

Ticking one off is the row's Archive (E, or the Done button): desk_server.archive() calls complete().
Both change only Peter's own list, through the slack skill's `todo-update`; nobody is notified. After a
change the slack_todos snapshot is patched in place, so the section shows it at the next sync rather
than after the next 10-minute collect.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess

import desk_store as store
from desk_config import path as cfg_path

SLACK = str(cfg_path("slack"))
KEY = re.compile(r"^slack-todo:([A-Za-z0-9_-]+):([A-Z][A-Z0-9]+):(\d{10,}\.\d{6})$")


def _parse(key: str) -> tuple[str, str, str]:
    m = KEY.fullmatch(key or "")
    if not m:
        raise ValueError(f"not a Slack to-do: {key!r}")
    return m.group(1), m.group(2), m.group(3)


def _update(key: str, *flags: str) -> dict:
    ws, cid, ts = _parse(key)
    r = subprocess.run([SLACK, "-w", ws, "todo-update", cid, ts, *flags], capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "slack todo-update failed") from None
    if not out.get("ok"):
        raise RuntimeError(f"Slack refused it: {out.get('error') or out}")
    return out.get("item") or {}


def _patch(key: str, fn) -> None:
    snap = store.snapshot("slack_todos")
    if not snap:
        return
    items = []
    for t in snap.get("items", []):
        if t.get("key") == key:
            t = fn(t)
        if t:
            items.append(t)
    store.put_snapshot("slack_todos", {**snap, "items": items})


def complete(key: str) -> None:
    """Tick the to-do off in Slack and drop it from the desk's list."""
    _update(key, "--done")
    _patch(key, lambda t: None)


def _epoch(value, name: str) -> int:
    epoch = 0 if value in (None, "", 0) else int(value)
    if epoch and not 1_000_000_000 < epoch < 4_000_000_000:
        raise ValueError(f"{name} must be epoch seconds")
    return epoch


def _iso(epoch: int) -> str | None:
    return dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="minutes") if epoch else None


def _set(key: str, flag: str, epoch: int) -> dict:
    """Set the due date or snooze, then patch the snapshot from the item Slack returns."""
    item = _update(key, flag, str(epoch))
    now = dt.datetime.now().timestamp()
    d, z = int(item.get("date_due") or 0), int(item.get("date_snoozed_until") or 0)
    _patch(key, lambda t: {**t, "due": _iso(d), "overdue": bool(d) and d < now, "snoozed_until": _iso(z) if z > now else None})
    return {"ok": True, "due": _iso(d), "snoozed_until": _iso(z) if z > now else None}


def due(body: dict) -> dict:
    return _set(str(body.get("key") or ""), "--due", _epoch(body.get("due"), "due"))


def snooze(body: dict) -> dict:
    return _set(str(body.get("key") or ""), "--snooze", _epoch(body.get("until"), "until"))


ROUTES = {"/api/slacktodo/due": due, "/api/slacktodo/snooze": snooze}
