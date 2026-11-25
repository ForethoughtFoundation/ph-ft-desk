// The publication pipeline as the desk shows it: what needs Peter, what is with others, and the target
// dates for this week and next. Run by `ft-desk publication` under `op run`, which supplies the
// publication desk's Access service token (PUB_STATE_URL, PUB_STATE_CLIENT_ID, PUB_STATE_CLIENT_SECRET).
//
//   node publication.mjs OUT_JSON VIEWER_EMAIL PROJECT_JS
//
// Who needs to act on what comes from the publication desk's own code (project.js in team-repo), so the
// desk and publish.example.org always agree.
import { writeFileSync } from "node:fs";

const [out, viewer, PROJECT_JS] = process.argv.slice(2);
const base = (process.env.PUB_STATE_URL || "").replace(/\/+$/, "");
if (!out || !viewer || !PROJECT_JS || !base) {
  console.error("usage: node publication.mjs OUT_JSON VIEWER_EMAIL PROJECT_JS (with PUB_STATE_URL set)");
  process.exit(2);
}
const { nextActions, group, withEpisodes, publication, nameOf } = await import(PROJECT_JS);
const { ROUNDUPS, dayName } = await import(PROJECT_JS.replace(/project\.js$/, "roundups.js"));

const headers = { "CF-Access-Client-Id": process.env.PUB_STATE_CLIENT_ID || "", "CF-Access-Client-Secret": process.env.PUB_STATE_CLIENT_SECRET || "" };
const res = await fetch(`${base}/api/pieces`, { headers, redirect: "manual" });
if (!res.ok) {
  console.error(`publish.example.org answered ${res.status}`);
  process.exit(1);
}
const pieces = withEpisodes((await res.json()).pieces.filter((s) => !s.test && !s.removed_at));

const TZ = "Europe/London";
const now = Date.now();
const isoDay = (t) => new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" }).format(t);
const today = isoDay(now);
const addDays = (d, n) => { const x = new Date(`${d}T12:00:00Z`); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
const dow = new Date(`${today}T12:00:00Z`).getUTCDay();
const monday = addDays(today, -((dow + 6) % 7));
const daysBetween = (a, b) => Math.round((Date.parse(`${b}T12:00:00Z`) - Date.parse(`${a}T12:00:00Z`)) / 86400000);
const pageUrl = (s) => `${base}/p/${s.slug}`;
const firstName = (who) => (nameOf(who) || who || "").split(/\s+/)[0];

const rows = pieces.map((s) => {
  const acts = nextActions(s, viewer, now, 2);
  const live = publication(s).live > 0;
  const lateBy = s.target_date && s.target_date < today && !live && s.stage !== "done" ? daysBetween(s.target_date, today) : 0;
  return { s, acts, g: group(s, acts), live, lateBy };
});

// Peter's steps: the most overdue first; the same step on several pieces becomes one line.
const steps = [];
for (const r of rows.filter((r) => r.g === "needs")) {
  for (const a of r.acts.filter((a) => a.mine && a.kind !== "none" && !a.held)) {
    const late = r.lateBy ? `${r.lateBy} ${r.lateBy === 1 ? "day" : "days"} late` : "";
    const waited = a.stale ? `${a.age} days` : "";
    // A chase says what it's for: "Chase Pat: post his thread".
    const title = a.id === "chase" && a.waiting?.reason ? `Chase ${firstName(a.waiting.who)}: ${a.waiting.reason.replace(/^([A-Z])([a-z]*)(?=[\s,;:.]|$)/, (_, c, rest) => c.toLowerCase() + rest)}` : a.title;
    steps.push({ title, piece: r.s.title, url: pageUrl(r.s), when: late || waited, hot: !!(late || waited),
                 rank: (r.lateBy ? 1000 + r.lateBy : 0) + (a.stale ? 100 + a.age : 0) });
  }
}
const merged = [];
for (const st of steps.sort((a, b) => b.rank - a.rank)) {
  const same = merged.find((m) => m.title === st.title && !m.hot && !st.hot);
  if (same) { same.pieces.push(st.piece); same.url = `${base}/`; }
  else merged.push({ ...st, pieces: [st.piece] });
}
// This week's roundups, while any is due: the dashboard's own Needs you row, first.
const rd = await fetch(`${base}/api/roundups`, { headers, redirect: "manual" });
if (!rd.ok) {
  console.error(`publish.example.org answered ${rd.status} for the roundups`);
  process.exit(1);
}
const { week: rdWeek, status: rdStatus } = await rd.json();
const rdDue = ROUNDUPS.filter(({ key }) => rdStatus[key] === "due");
if (rdDue.length) merged.unshift({ title: `Check and post the ${rdDue.length === 1 ? "roundup" : "roundups"}`,
  pieces: [`Weekly roundups, ${dayName(rdWeek)}: ${rdDue.map((x) => x.name).join(" · ")}`], url: `${base}/roundups/${rdWeek}`, when: "", hot: false });
const yours = merged.map(({ title, pieces: ps, url, when, hot }) => ({
  title: ps.length > 1 ? `${title} (${ps.length})` : title, sub: ps.join(" · "), url, when, hot }));

// What other people owe, with the oldest wait first.
const others = rows.filter((r) => r.g === "waiting").map((r) => {
  const w = (r.s.waiting || [])[0];
  return { title: w ? `${firstName(w.who)}: ${w.reason}` : "With others", sub: r.s.title, url: pageUrl(r.s), when: "Waiting" };
});

// Today: pieces due out today that aren't live yet.
const due = rows.filter((r) => r.s.target_date === today && !r.live && r.s.stage !== "done").map((r) => {
  const w = (r.s.waiting || [])[0];
  return `${r.s.title}${w ? `, waiting on ${firstName(w.who)}` : ""}`;
});

// Two weeks of target dates, Monday to Friday.
const weeks = [0, 1].map((wk) => ({
  label: wk ? "Next week" : "This week",
  days: [0, 1, 2, 3, 4].map((i) => {
    const date = addDays(monday, 7 * wk + i);
    const d = new Date(`${date}T12:00:00Z`);
    return {
      date, today: date === today,
      label: `${d.toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" })} ${d.getUTCDate()}`,
      pieces: rows.filter((r) => r.s.target_date === date).map((r) => ({
        title: r.s.title, url: pageUrl(r.s),
        state: r.g === "needs" ? (r.lateBy ? "late" : "you") : r.lateBy ? "late" : r.live || r.s.stage === "done" ? "live" : "wait",
      })),
    };
  }),
}));

const summary = {
  fetched_at: new Date(now).toISOString(),
  url: `${base}/`,
  counts: {
    you: rows.filter((r) => r.g === "needs").length + (rdDue.length ? 1 : 0),
    overdue: rows.filter((r) => r.lateBy || r.acts.some((a) => a.mine && a.stale)).length,
    others: others.length,
  },
  today: due,
  yours, others, weeks,
};
writeFileSync(out, JSON.stringify(summary, null, 1));
const c = summary.counts;
console.log(`Publication: ${c.you} need you, ${c.overdue} overdue, ${c.others} with others`);
