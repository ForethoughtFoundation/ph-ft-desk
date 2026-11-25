"""The desk's settings, read from config.json beside the skill: who the desk is for, where its state
and the tools it calls live, and the server's port. Every script takes paths and identity from here."""
from __future__ import annotations

import json
import os
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
CONFIG = json.loads(Path(os.environ.get("FT_DESK_CONFIG") or SKILL / "config.json").read_text())  # demo/render.py sets it

_me = CONFIG["me"]
ME = _me["name"]                      # as Linear and git show it
FIRST = _me.get("first") or ME.split()[0]
ABOUT = _me.get("about", "")          # one clause for the judge: what the person does, with whom
LINEAR_WHO = _me.get("linear_who", "me")   # linear-tasks' --who for tasks added from the page
PUBLICATION_VIEWER = _me.get("publication_viewer", "")
PERSONAL_CALENDAR = _me.get("personal_calendar")  # {"account", "calendars"}, or None

_paths = CONFIG["paths"]
ROOT = Path(_paths["root"]).expanduser()


def path(key: str) -> Path:
    """A path from config.json: ~ expands; a relative path is relative to the working folder (ROOT)."""
    p = Path(_paths[key]).expanduser()
    return p if p.is_absolute() else ROOT / p


STATE_DIR = path("state")
DESK = STATE_DIR / "desk"
PORT = int(os.environ.get("FT_DESK_PORT") or CONFIG.get("server", {}).get("port", 4848))
