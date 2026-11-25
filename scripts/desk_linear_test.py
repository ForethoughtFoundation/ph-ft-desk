"""Tests for the Linear outbox (desk_linear.py): python3 scripts/desk_linear_test.py

Runs as a test copy (another port) with Linear stubbed, a temporary outbox and a made-up team, so it touches
neither Linear nor the real desk's files."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["FT_DESK_PORT"] = "4898"
os.environ["FT_DESK_LINEAR_STUB"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import desk_linear as L  # noqa: E402

TEAM = {"at": 0, "team_id": "t", "labels": {"joint": "lj"},
        "states": [{"id": "s-todo", "name": "Todo", "type": "unstarted", "position": 1},
                   {"id": "s-prog", "name": "In Progress", "type": "started", "position": 0},
                   {"id": "s-wait", "name": "Waiting", "type": "started", "position": 1000},
                   {"id": "s-done", "name": "Done", "type": "completed", "position": 3},
                   {"id": "s-rej", "name": "Rejected", "type": "canceled", "position": 1000}],
        "members": [{"id": "u-peter", "name": "Peter Hartree"}, {"id": "u-sam", "name": "Sam Lee"}],
        "projects": [{"id": "p-ops", "name": "Operations"}, {"id": "p-desk", "name": "Desk"}]}


def snap():
    issue = {"identifier": "AS-1", "title": "T", "state": {"name": "Todo", "type": "unstarted"}, "assignee": None,
             "priorityLabel": "No priority", "dueDate": None, "project": {"id": "p-ops", "name": "Operations"},
             "comments": {"nodes": []}}
    return {"boards": {"p-ops": [issue], "p-desk": []}, "mine": [], "unassigned": [dict(issue)], "refs": {}}


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        L.OUTBOX, L.TEAM_CACHE, L.DESK = d / "outbox.json", d / "team.json", d
        L.TEAM_CACHE.write_text(json.dumps(TEAM))
        self.mode("ok")

    def tearDown(self):
        self.dir.cleanup()

    def mode(self, m):
        (L.DESK / f"linear-stub-{L.PORT}").write_text(m)

    def write(self, oid, **kw):
        return L.write({"id": oid, "issue": "AS-1", "kind": "update", **kw})

    def test_columns_resolve_to_states(self):
        self.assertEqual(L.state_for_column("In Progress")["id"], "s-prog")
        self.assertEqual(L.state_for_column("Waiting")["id"], "s-wait")
        self.assertEqual(L.state_for_column("Rejected")["id"], "s-rej")

    def test_applied_once(self):
        r = self.write("op-00000001", set={"column": "Done"})
        self.assertEqual(r["status"], "applied")
        self.mode("refuse")   # a resend of the same id gets the first answer, without reaching Linear
        self.assertEqual(self.write("op-00000001", set={"column": "Done"})["status"], "applied")
        self.assertEqual(L.pending(), [])

    def test_offline_keeps_order_and_retries(self):
        self.mode("offline")
        self.assertEqual(self.write("op-00000001", set={"priority": 2})["status"], "queued")
        self.assertEqual(self.write("op-00000002", set={"due": "2026-12-04"})["status"], "queued")
        self.assertEqual([o["id"] for o in L.pending()], ["op-00000001", "op-00000002"])
        self.assertEqual(L.pending()[0]["attempts"], 2)
        self.mode("ok")
        res = L.process()
        self.assertEqual(list(res), ["op-00000001", "op-00000002"])
        self.assertTrue(all(r["status"] == "applied" for r in res.values()))
        self.assertEqual(L.pending(), [])

    def test_refused_in_background_is_held_not_retried(self):
        self.mode("offline")
        self.write("op-00000001", set={"assignee": "u-sam"})
        self.mode("refuse")
        res = L.process()
        self.assertEqual(res["op-00000001"]["status"], "held")
        self.assertEqual(L.pending(), [])
        self.assertEqual(len(L._load()["held"]), 1)
        self.assertEqual(L.process(), {})   # nothing left to loop on
        L.held_action({"id": "op-00000001", "action": "discard"})
        self.assertEqual(L._load()["held"], [])

    def test_refused_at_once_goes_back(self):
        self.mode("refuse")
        r = self.write("op-00000001", set={"column": "Done"})
        self.assertEqual(r["status"], "refused")
        self.assertEqual(L._load()["held"], [])

    def test_bad_values_are_400s(self):
        for bad in ({"priority": 7}, {"assignee": "u-nobody"}, {"due": "4 Dec"}, {"state": "s-nope"}, {"colour": "red"}):
            with self.assertRaises(ValueError):
                self.write("op-00000009", set=bad)

    def test_overlay_shows_waiting_changes(self):
        ops = [L._clean({"id": "op-00000001", "issue": "AS-1", "kind": "update",
                         "set": {"state": "s-prog", "assignee": "u-peter", "priority": 1, "due": "2026-12-04", "project": "p-desk"}}),
               L._clean({"id": "op-00000002", "issue": "AS-1", "kind": "comment", "body": "Hello"})]
        out = L.overlay(snap(), ops)
        self.assertEqual(out["boards"]["p-ops"], [])
        i = out["boards"]["p-desk"][0]
        self.assertEqual((i["state"]["name"], i["assignee"]["name"], i["priorityLabel"], i["dueDate"]),
                         ("In Progress", "Peter Hartree", "Urgent", "2026-12-04"))
        self.assertEqual(i["comments"]["nodes"][0]["body"], "Hello")
        self.assertEqual(out["unassigned"][0]["project"]["name"], "Desk")
        again = L.overlay(out, ops)   # laying the same changes over twice adds nothing
        self.assertEqual(len(again["boards"]["p-desk"]), 1)
        self.assertEqual(len(again["boards"]["p-desk"][0]["comments"]["nodes"]), 1)


if __name__ == "__main__":
    unittest.main()
