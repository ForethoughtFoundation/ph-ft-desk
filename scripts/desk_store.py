#!/usr/bin/env python3
"""The desk's store: one SQLite database that the desk service and the ft-desk commands share.

It lives outside the working folder (~/Projects syncs to another Mac through Syncthing, which
would corrupt a live database). WAL mode and short transactions make it safe for the service's
threads and a command-line process to write at once.

    snapshots   the latest facts per name (calendar, linear, slack_todos, hours ...), as JSON
    sources     each collector's health: last run, last success, complete or not, next run due
    actions     outside actions the desk took (archive, decline ...): pending, done or failed
    messages    raw arrivals from the continuous collectors, one row per message
    units       one row per conversation (a thread, DM, email thread or Docs thread)
    judgements  what the judge said about a unit at a given revision (shadow until cutover)
    marks       Peter's verdict where the judge and the full run disagree
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DIR = Path(os.environ.get("FT_DESK_STORE") or Path.home() / "Library/Application Support/Forethought Desk")
DB = DIR / "desk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (name TEXT PRIMARY KEY, data TEXT NOT NULL, hash TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY, last_run REAL, last_ok REAL, status TEXT, detail TEXT,
  complete INTEGER DEFAULT 1, next_due REAL DEFAULT 0, failures INTEGER DEFAULT 0, seconds REAL, cursor TEXT);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
  detail TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS actions_target ON actions(kind, target);
CREATE TABLE IF NOT EXISTS messages (
  key TEXT PRIMARY KEY, source TEXT NOT NULL, unit TEXT NOT NULL, ts REAL NOT NULL, author TEXT,
  is_from_me INTEGER DEFAULT 0, data TEXT NOT NULL, first_seen REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_unit ON messages(unit, ts);
CREATE INDEX IF NOT EXISTS messages_seen ON messages(first_seen);
CREATE TABLE IF NOT EXISTS units (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, label TEXT, link TEXT, kind TEXT,
  revision INTEGER NOT NULL DEFAULT 0, last_ts REAL, last_other_ts REAL, last_me_ts REAL,
  vip INTEGER DEFAULT 0, direct INTEGER DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS judgements (
  unit TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, model TEXT, verdict TEXT,
  error TEXT, created REAL NOT NULL, finished REAL, tokens_in INTEGER, tokens_out INTEGER,
  PRIMARY KEY (unit, revision));
CREATE TABLE IF NOT EXISTS spend (at REAL NOT NULL, model TEXT, cost REAL NOT NULL);
CREATE TABLE IF NOT EXISTS marks (unit TEXT NOT NULL, run TEXT NOT NULL, verdict TEXT NOT NULL, note TEXT, at REAL NOT NULL, PRIMARY KEY (unit, run));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_local = threading.local()
_init_lock = threading.Lock()
_ready = False


def db() -> sqlite3.Connection:
    """One connection per thread."""
    global _ready
    con = getattr(_local, "con", None)
    if con is None:
        DIR.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=15000")
        with _init_lock:
            if not _ready:
                con.executescript(SCHEMA)
                _ready = True
        _local.con = con
    return con


class tx:
    """`with tx() as con:` runs its statements as one transaction."""

    def __enter__(self):
        self.con = db()
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, *_):
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def digest(data) -> str:
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- snapshots
def put_snapshot(name: str, data, con=None) -> bool:
    """Save a snapshot; return True when it differs from the one before."""
    h = digest(data)
    c = con or db()
    row = c.execute("SELECT hash FROM snapshots WHERE name=?", (name,)).fetchone()
    if row and row["hash"] == h:
        return False
    c.execute("INSERT INTO snapshots(name, data, hash, updated_at) VALUES(?,?,?,?) "
              "ON CONFLICT(name) DO UPDATE SET data=excluded.data, hash=excluded.hash, updated_at=excluded.updated_at",
              (name, json.dumps(data, ensure_ascii=False, default=str), h, time.time()))
    return True


def snapshot(name: str, default=None):
    row = db().execute("SELECT data FROM snapshots WHERE name=?", (name,)).fetchone()
    return json.loads(row["data"]) if row else default


def snapshot_hashes() -> dict[str, str]:
    return {r["name"]: r["hash"] for r in db().execute("SELECT name, hash FROM snapshots")}


# ---------------------------------------------------------------- sources
def source(name: str) -> dict:
    row = db().execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
    return dict(row) if row else {"name": name, "failures": 0, "next_due": 0, "cursor": None}


def sources() -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM sources ORDER BY name")]


def record_run(name: str, *, ok: bool, detail: str = "", complete: bool = True, next_due: float, seconds: float,
               cursor=None, con=None) -> None:
    """Record a collector run. A cursor is saved only on success, in the caller's transaction."""
    c = con or db()
    now = time.time()
    prev = c.execute("SELECT failures, last_ok, cursor FROM sources WHERE name=?", (name,)).fetchone()
    failures = 0 if ok else (prev["failures"] if prev else 0) + 1
    last_ok = now if ok else (prev["last_ok"] if prev else None)
    cur = (json.dumps(cursor) if cursor is not None else (prev["cursor"] if prev else None)) if ok else (prev["cursor"] if prev else None)
    c.execute("INSERT INTO sources(name, last_run, last_ok, status, detail, complete, next_due, failures, seconds, cursor) "
              "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run, last_ok=excluded.last_ok, "
              "status=excluded.status, detail=excluded.detail, complete=excluded.complete, next_due=excluded.next_due, "
              "failures=excluded.failures, seconds=excluded.seconds, cursor=excluded.cursor",
              (name, now, last_ok, "ok" if ok else "error", detail[:500], int(complete), next_due, failures, round(seconds, 1), cur))


def cursor_of(name: str, default=None):
    c = source(name).get("cursor")
    return json.loads(c) if c else default


def set_due(name: str, when: float) -> None:
    db().execute("INSERT INTO sources(name, next_due) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET next_due=excluded.next_due", (name, when))


# ---------------------------------------------------------------- actions
def action_start(kind: str, target: str, detail: str = "") -> int:
    now = time.time()
    return db().execute("INSERT INTO actions(kind, target, status, detail, created, updated) VALUES(?,?,?,?,?,?)",
                        (kind, target, "pending", detail[:500], now, now)).lastrowid


def action_finish(action_id: int, ok: bool, detail: str = "") -> None:
    db().execute("UPDATE actions SET status=?, detail=?, updated=? WHERE id=?",
                 ("done" if ok else "failed", detail[:500], time.time(), action_id))


# ---------------------------------------------------------------- meta
def meta(key: str, default=None):
    row = db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(key: str, value) -> None:
    db().execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
