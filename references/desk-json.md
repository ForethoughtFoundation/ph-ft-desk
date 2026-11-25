# desk.json

The agent writes this file; `scripts/render_desk.py` turns it into the page and `scripts/desk_server.py` reads items' `context` from it when Peter presses Start. Text fields are plain text: the renderer escapes them, then allows `[text](url)` links, `**bold**`, and bare Linear ids (`AS-123`), which link to the issue in the Linear app.

```json
{
  "date": "2026-11-11",
  "header": {"title": "Wed 11 Nov 18:11", "since": "Since Tue 3 Nov 17:12", "counts": {"urgent": 4, "new": 22, "open": 9}},
  "projects": [
    {"name": "Publication desk", "id": "<Linear project uuid>", "now": "…", "next": "…", "risk": "…"}
  ],
  "sections": [
    {"id": "urgent", "title": "Urgent", "groups": [{"items": [ITEM]}]},
    {"id": "new", "title": "New", "groups": [{"label": "Email", "items": [ITEM]}, {"label": "Slack", "items": [ITEM]}]},
    {"id": "slack-todos", "title": "Slack to-dos", "groups": [{"items": [ITEM]}]},
    {"id": "unassigned", "title": "Unassigned tasks", "nav": "Unassigned", "groups": [{"items": [ITEM]}]},
    {"id": "still-open", "title": "Still open", "groups": [{"items": [ITEM]}]},
    {"id": "archive", "title": "Archive as noise?", "nav": "Archive", "groups": [{"items": [ITEM]}],
     "foot": "Reply \"archive all\", \"archive all except 9\", or \"keep 9\"."},
    {"id": "declined", "title": "Declined automatically", "nav": "Declined", "groups": [{"items": [ITEM]}]},
    {"id": "let-go", "title": "Let go this run", "nav": "Let go", "groups": [{"items": [ITEM]}]}
  ],
  "day_plan": {
    "days": [
      {"label": "Today", "date": "Wed 11 Nov", "now": "18:11",
       "events": [{"start": "16:00", "end": "19:00", "label": "Gathertown cowork", "note": "unanswered", "warn": true, "n": 4}],
       "due": [{"n": 30, "text": "Sam: which automations to port", "status": "Overdue 1 day", "warn": true},
               {"id": "AS-101", "text": "Draft the quarterly plan", "status": "Due today", "warn": true}]},
      {"label": "Tomorrow", "date": "Thu 12 Nov", "events": [], "due": []}
    ],
    "later": [{"when": "Fri 13 Nov", "id": "AS-102", "text": "Send Robin the agenda"}]
  },
  "hours": {"text": "6.0 of 20 days used …", "pct": 30.0},
  "checked": [{"text": "email 12 new, 30 noise"}, {"text": "Docs unavailable: timed out", "bad": true}]
}
```

Leave out empty sections. `projects` holds only Now, Next and Risk, one entry per active board of Peter's, matched to its board by `id`, then by `name`; which boards exist, their grouping, status and owners come from `scripts/desk_projects.py`. `nav` is the side-menu label when it differs from `title`.

ITEM:

```json
{
  "n": 7,
  "key": "the gather's key for the item (stable across runs)",
  "source": "email | slack | signal | whatsapp | docs | linear | cal | repos | desk",
  "source_label": "Slack DM",
  "head": "Kit, Slack DM:",
  "link": "https://…",
  "text": "the summary after the head",
  "time": "Mon 15:14",
  "pill": {"kind": "over | today | later", "text": "Overdue 2 days"},
  "title": "Warp tab name, 20 characters max",
  "context": "3-6 sentences a fresh Claude Code session needs to act on the item"
}
```

Only `n`, `key` and `text` are required. In `day_plan`, an event's `end` is drawn as a span only for events of 90 minutes or more; `n` or `id` link an event or due item to its desk item or Linear issue.
