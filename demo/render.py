#!/usr/bin/env python3
"""Render the desk page from invented data, for screenshots and for anyone reading the code without the services
it reads. Nothing here touches the real desk: the config, state folder and boards are all built in OUT_DIR.

    python3 demo/render.py OUT_DIR      # writes OUT_DIR/state/desk/index.html; open it in a browser

The page's own tabs that call the desk server (Comments, Threads, Agents) stay empty in this static copy.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-out").resolve()
    desk_dir = out / "state" / "desk"
    desk_dir.mkdir(parents=True, exist_ok=True)

    cfg = json.loads((SKILL / "config.example.json").read_text())
    cfg["me"].update(name="Peter Hartree", first="Peter", linear_who="me", personal_calendar=None)
    cfg["paths"].update(root=str(out), state="state")
    (out / "config.json").write_text(json.dumps(cfg, indent=1))
    os.environ["FT_DESK_CONFIG"] = str(out / "config.json")

    shutil.copy(HERE / "desk.json", desk_dir / "desk.json")
    boards = json.loads((HERE / "boards.json").read_text())
    boards["built_at"] = time.time() + 10**9  # the cache never expires, so nothing asks the map or Linear
    (desk_dir / "boards.json").write_text(json.dumps(boards, indent=1))

    sys.path.insert(0, str(SKILL / "scripts"))
    import render_desk

    desk = json.loads((HERE / "desk.json").read_text())
    desk["_boards"] = json.loads((HERE / "issues.json").read_text())
    rg, peek = render_desk.compose(desk, desk_dir, None, fetch_refs=False)
    (desk_dir / "index.html").write_text(render_desk.page(rg, peek, "demo", {}))
    print(desk_dir / "index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
