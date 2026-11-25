#!/usr/bin/env python3
"""The desk's list of boards: every responsibility and project, grouped by role and area, with the Linear facts
the page needs and the Now, Next and Risk a desk run wrote.

  - Structure (role, area, owner, kind, parent) comes from team-repo's `skills/map/scripts/map --json --offline`, the
    only list of projects code may read (team-repo AGENTS.md, "Where each fact lives").
  - Linear gives each board's id, URL, status, lead and members.
  - desk.json's "projects" gives Now, Next and Risk, matched by Linear id, then by name.

The result is cached in the desk's state folder for ten minutes, so a render or a collector run costs one map run
and one Linear query at most. If Linear can't be read, the last cached list is used; if there is none, boards keep
their structure but have no id, so they show no board.

    desk_projects.py            # print the list as JSON
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from desk_config import DESK, ME, ROOT  # noqa: E402

MAP = ROOT / "team-repo" / "skills" / "map" / "scripts" / "map"
CACHE = DESK / "boards.json"
TTL = 600
ROLE_ORDER = ["Engineering", "Communications", "Other"]
# Linear project status types, in the task-manager's names (team-repo AGENTS.md, "Projects").
# Paused is Linear's Backlog status: a custom paused one needs a higher plan (task-manager's PROJECT_STATUSES).
STATUS = {"started": "Active", "paused": "Paused", "planned": "Not started", "backlog": "Paused"}
QUERY = """{ projects(first: 60, filter: {state: {nin: ["completed", "canceled"]}}) { nodes { id name url
    status { name type } lead { name } members(first: 10) { nodes { name } } } } }"""


def _map() -> dict:
    out = subprocess.run([str(MAP), "--json", "--offline"], capture_output=True, text=True, timeout=60, check=True).stdout
    return json.loads(out)


def _linear() -> dict[str, dict]:
    import desk_collect as collect  # imported here: desk_collect imports this module for its board ids
    nodes = collect.gql(QUERY)["projects"]["nodes"]
    return {n["name"]: n for n in nodes}


def _desk_rows() -> list[dict]:
    try:
        return json.loads((DESK / "desk.json").read_text()).get("projects", [])
    except (OSError, ValueError):
        return []


def build() -> dict:
    m = _map()
    linear = _linear()
    rows = _desk_rows()
    by_id = {r["id"]: r for r in rows if r.get("id")}
    by_name = {r["name"]: r for r in rows if r.get("name")}
    first = ME.split()[0]
    areas = [{"slug": a["slug"], "name": a["name"], "role": a["role"], "owner": a["owner"]} for a in m.get("areas", [])]
    parents = {p["parent"] for p in m["projects"] if p["parent"]}
    boards = []
    for p in m["projects"]:
        if p["slug"] in parents:
            continue  # a parent is a Linear initiative, not a board; its subprojects carry its name
        lin = linear.get(p["name"]) or {}
        row = by_id.get(lin.get("id")) or by_name.get(p["name"]) or {}
        lead = (lin.get("lead") or {}).get("name") or ""
        members = [x["name"] for x in (lin.get("members") or {}).get("nodes", [])]
        people = ([lead] if lead else []) + [x for x in members if x != lead]
        parent = next((q["name"] for q in m["projects"] if q["slug"] == p["parent"]), None) if p["parent"] else None
        boards.append({
            "name": p["name"], "slug": p["slug"], "area": p["area"], "kind": p["kind"], "parent": parent,
            "id": lin.get("id"), "url": lin.get("url") or "",
            "status": STATUS.get((lin.get("status") or {}).get("type"), "Active"),
            "owner": lead.split()[0] if lead else "", "collaborators": [x.split()[0] for x in people[1:]],
            "mine": any(x == ME or x.split()[0] == first for x in people),
            "now": row.get("now", ""), "next": row.get("next", ""), "risk": row.get("risk", ""),
        })
    return {"built_at": time.time(), "areas": areas, "boards": boards}


def structure(force: bool = False) -> dict:
    """The cached list, rebuilt when it is older than TTL, when desk.json is newer, or when forced."""
    try:
        cached = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cached = None
    desk_m = (DESK / "desk.json").stat().st_mtime if (DESK / "desk.json").exists() else 0
    if cached and not force and time.time() - cached.get("built_at", 0) < TTL and desk_m <= cached.get("built_at", 0):
        return cached
    try:
        data = build()
    except Exception:
        if cached:
            return cached
        raise
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(CACHE)
    return data


def boards() -> list[dict]:
    try:
        return structure()["boards"]
    except Exception:  # no map, no Linear and no cache: fall back to the run's own rows
        return [{**r, "url": r.get("link", ""), "mine": True, "kind": "project", "area": "", "status": "Active",
                 "owner": "", "collaborators": [], "parent": None} for r in _desk_rows()]


def find(name: str) -> dict | None:
    return next((b for b in boards() if b["name"] == name), None)


def board_ids() -> list[str]:
    return [b["id"] for b in boards() if b.get("id")]


if __name__ == "__main__":
    print(json.dumps(structure(force="--force" in sys.argv), ensure_ascii=False, indent=1))
