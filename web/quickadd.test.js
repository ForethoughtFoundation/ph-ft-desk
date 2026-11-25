// Tests for quick create's parser: node --test web/quickadd.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const { parse, dueLabel, priorityLabel, scoreProject } = require("./quickadd.js");

const FRI = new Date(2026, 10, 20, 10, 0);   // Friday 20 November 2026, 10:00
const MON = new Date(2026, 10, 23, 10, 0);   // Monday 23 November 2026
const SAT = new Date(2026, 10, 21, 10, 0);
const PROJECTS = ["Web analytics dashboard", "Cybersecurity", "Distribution", "Forethought Audio", "Operations", "Research corpus",
  "Research pipeline ideas", "Research pipeline misc", "Blue sky"].map((name, i) => ({ id: `p${i}`, name, mine: name === "Cybersecurity" }));
const ctx = (now = FRI, extra = {}) => ({ now, projects: PROJECTS, people: ["peter", "pat", "sam", "none"], defaultWho: "peter", ...extra });
const p = (text, now, extra) => parse(text, ctx(now, extra));
const due = (text, now) => p(text, now).due;

test("relative days", () => {
  assert.deepEqual(due("Call Bob today"), { date: "2026-11-20", time: null });
  assert.deepEqual(due("Call Bob tod"), { date: "2026-11-20", time: null });
  assert.deepEqual(due("Call Bob tomorrow"), { date: "2026-11-21", time: null });
  assert.deepEqual(due("Call Bob tom"), { date: "2026-11-21", time: null });
  assert.deepEqual(due("Call Bob tmrw"), { date: "2026-11-21", time: null });
  assert.equal(p("Call Bob tomorrow").title, "Call Bob");
});

test("a capitalised Tom is a name", () => {
  const r = p("Call Tom about the budget");
  assert.equal(r.due, null);
  assert.equal(r.title, "Call Tom about the budget");
});

test("weekdays: the next one, today included", () => {
  assert.equal(due("Draft fri").date, "2026-11-20");
  assert.equal(due("Draft friday").date, "2026-11-20");
  assert.equal(due("Draft sat").date, "2026-11-21");
  assert.equal(due("Draft mon").date, "2026-11-23");
  assert.equal(due("Draft Monday").date, "2026-11-23");
  assert.equal(due("Draft thurs").date, "2026-11-26");
  assert.equal(p("Meet Noor on friday").title, "Meet Noor");
});

test("next week is next Monday, as in Todoist; next <day> is that day of next week", () => {
  assert.equal(due("Plan next week").date, "2026-11-23");
  assert.equal(due("Plan next mon").date, "2026-11-23");
  assert.equal(due("Plan next fri").date, "2026-11-27");
  assert.equal(due("Plan next week", MON).date, "2026-11-30");
  assert.equal(due("Plan next fri", MON).date, "2026-12-04");
  assert.equal(due("Plan fri", MON).date, "2026-11-27");
  assert.equal(due("Plan next month").date, "2026-12-01");
  assert.equal(due("Plan weekend").date, "2026-11-21");
  assert.equal(due("Plan this weekend", SAT).date, "2026-11-21");
  assert.equal(due("Plan next weekend").date, "2026-11-28");
});

test("in N days, weeks, months, hours", () => {
  assert.equal(due("Chase in 3 days").date, "2026-11-23");
  assert.equal(due("Chase in a week").date, "2026-11-27");
  assert.equal(due("Chase in 2 weeks").date, "2026-12-04");
  assert.equal(due("Chase in 1 month").date, "2026-12-20");
  assert.deepEqual(due("Chase in 2 hours"), { date: "2026-11-20", time: "12:00" });
  assert.equal(p("Chase in 3 days").title, "Chase");
  assert.equal(due("Never mind in two minds"), null);
});

test("day and month, British order", () => {
  for (const s of ["21 nov", "21 Nov", "21st November", "21 of nov", "nov 21", "November 21st", "21/11"]) {
    assert.equal(due(`Review ${s}`).date, "2026-11-21", s);
    assert.equal(p(`Review ${s} draft`).title, "Review draft", s);
  }
  assert.equal(due("Review 1/12").date, "2026-12-01");     // 1 December, not 12 January
  assert.equal(due("Review 12/1").date, "2027-01-12");
  assert.equal(due("Review 19/11").date, "2027-11-19");    // passed this year: next year
  assert.equal(due("Review 20/11").date, "2026-11-20");    // today counts
  assert.equal(due("Review 21/11/2027").date, "2027-11-21");
  assert.equal(due("Review 21/11/27").date, "2027-11-21");
  assert.equal(due("Review 3 jan 2027").date, "2027-01-03");
  assert.equal(due("Review 2026-12-25").date, "2026-12-25");
  const bad = p("Review 31/11");
  assert.equal(bad.due, null);
  assert.equal(bad.title, "Review 31/11");
});

test("times", () => {
  assert.deepEqual(due("Call at 3pm"), { date: "2026-11-20", time: "15:00" });
  assert.deepEqual(due("Call 3 pm"), { date: "2026-11-20", time: "15:00" });
  assert.deepEqual(due("Call 3.30pm"), { date: "2026-11-20", time: "15:30" });
  assert.deepEqual(due("Call at 15:00"), { date: "2026-11-20", time: "15:00" });
  assert.deepEqual(due("Call 15:00"), { date: "2026-11-20", time: "15:00" });
  assert.deepEqual(due("Call at 9am"), { date: "2026-11-21", time: "09:00" });   // passed today: tomorrow
  assert.deepEqual(due("Call 12pm"), { date: "2026-11-20", time: "12:00" });
  assert.deepEqual(due("Call 12am"), { date: "2026-11-21", time: "00:00" });
  assert.deepEqual(due("Call at noon"), { date: "2026-11-20", time: "12:00" });
  assert.deepEqual(due("Call at 3"), { date: "2026-11-20", time: "15:00" });
  assert.deepEqual(due("Call at 11"), { date: "2026-11-20", time: "11:00" });
  assert.equal(p("Call at 3pm").title, "Call");
});

test("date and time together", () => {
  assert.deepEqual(due("Call tomorrow at 3pm"), { date: "2026-11-21", time: "15:00" });
  assert.deepEqual(due("Call 3pm tomorrow"), { date: "2026-11-21", time: "15:00" });
  assert.deepEqual(due("Call fri 9:30"), { date: "2026-11-20", time: "09:30" });
  assert.deepEqual(due("Call 21/11 at 14:00"), { date: "2026-11-21", time: "14:00" });
  assert.equal(p("Call Bob tomorrow at 3pm about the grant").title, "Call Bob about the grant");
});

test("eod", () => {
  assert.deepEqual(due("Send the memo eod"), { date: "2026-11-20", time: "17:00" });
  assert.deepEqual(due("Send the memo mon eod"), { date: "2026-11-23", time: "17:00" });
  assert.deepEqual(due("Send the memo by end of day"), { date: "2026-11-20", time: "17:00" });
});

test("only the first date counts; later ones stay in the title", () => {
  const r = p("Move the call from fri to mon");
  assert.equal(r.due.date, "2026-11-20");
  assert.equal(r.title, "Move the call from to mon");
});

test("#project: prefixes, several words, fuzzy", () => {
  assert.equal(p("Fix MFA #cyber").project.name, "Cybersecurity");
  assert.equal(p("Fix MFA #cyber").title, "Fix MFA");
  assert.equal(p("#research pipeline ideas Kit's feedback").project.name, "Research pipeline ideas");
  assert.equal(p("#research pipeline ideas Kit's feedback").title, "Kit's feedback");
  assert.equal(p("#Research pipeline misc tidy").project.name, "Research pipeline misc");
  assert.equal(p("Check spend #web analytics").project.name, "Web analytics dashboard");
  assert.equal(p("Check spend #webanalytics").project.name, "Web analytics dashboard");
  assert.equal(p("#audio fix player").project.name, "Forethought Audio");
  assert.equal(p("#audio fix player").title, "fix player");
  assert.equal(p("Rota #ops").project.name, "Operations");
  assert.equal(p("Notes #blue sky call").project.name, "Blue sky");
  assert.equal(p("Notes #blue sky call").title, "Notes call");
});

test("an unmatched #word stays in the title", () => {
  const r = p("Buy #zzz paper");
  assert.equal(r.project, null);
  assert.equal(r.projectMiss, "zzz");
  assert.equal(r.title, "Buy #zzz paper");
});

test("a project's words aren't read as a date", () => {
  const projects = [{ id: "x", name: "Next week planning" }];
  const r = parse("Agenda #next week planning", { now: FRI, projects });
  assert.equal(r.project.name, "Next week planning");
  assert.equal(r.due, null);
  assert.equal(r.title, "Agenda");
});

test("+person for Linear", () => {
  assert.equal(p("Review +sam").who, "sam");
  assert.equal(p("Review +sa").who, "sam");
  assert.equal(p("Review +pe").who, "peter");
  assert.equal(p("Review +me").who, "peter");
  assert.equal(p("Review +none").who, "none");
  assert.equal(p("Review +sam").title, "Review");
  const amb = p("Review +p");
  assert.equal(amb.who, null);
  assert.equal(amb.title, "Review +p");
  const td = parse("Review +sam", { now: FRI, projects: [], people: [] });   // Todoist: no people
  assert.equal(td.who, null);
  assert.equal(td.title, "Review +sam");
});

test("p1 to p4", () => {
  assert.equal(p("Ship it p1").priority, 1);
  assert.equal(p("Ship it P2").priority, 2);
  assert.equal(p("Ship it p4").priority, 4);
  assert.equal(p("Ship it p1").title, "Ship it");
  assert.equal(p("Ship it p5").priority, null);
  assert.equal(p("Convert mp3 files").priority, null);
  assert.equal(priorityLabel(1, "linear"), "Urgent");
  assert.equal(priorityLabel(2, "linear"), "High");
  assert.equal(priorityLabel(3, "linear"), "Medium");
  assert.equal(priorityLabel(4, "linear"), "Low");
  assert.equal(priorityLabel(1, "todoist"), "Priority 1");
});

test("everything at once, with highlight spans", () => {
  const text = "Draft brief tomorrow 3pm #distribution +sam p2";
  const r = p(text);
  assert.equal(r.title, "Draft brief");
  assert.deepEqual(r.due, { date: "2026-11-21", time: "15:00" });
  assert.equal(r.project.name, "Distribution");
  assert.equal(r.who, "sam");
  assert.equal(r.priority, 2);
  assert.deepEqual(r.tokens.map(t => [t.kind, text.slice(t.start, t.end)]),
    [["due", "tomorrow"], ["due", "3pm"], ["project", "#distribution"], ["who", "+sam"], ["priority", "p2"]]);
});

test("a backslash keeps a word as text", () => {
  const r = p("Read \\friday essay");
  assert.equal(r.due, null);
  assert.equal(r.title, "Read friday essay");
  assert.equal(p("Tag \\#cyber").title, "Tag #cyber");
});

test("further lines are the description, unparsed", () => {
  const r = p("Write memo tomorrow\nSee the thread from Alex\nnext week is fine too");
  assert.equal(r.title, "Write memo");
  assert.equal(r.due.date, "2026-11-21");
  assert.equal(r.description, "See the thread from Alex\nnext week is fine too");
});

test("chip labels", () => {
  assert.equal(dueLabel({ date: "2026-11-20", time: null }, FRI), "Today");
  assert.equal(dueLabel({ date: "2026-11-21", time: "15:00" }, FRI), "Tomorrow 15:00");
  assert.equal(dueLabel({ date: "2026-11-25", time: null }, FRI), "Wednesday");
  assert.equal(dueLabel({ date: "2026-12-04", time: null }, FRI), "Fri 4 Dec");
  assert.equal(dueLabel({ date: "2027-01-01", time: "09:00" }, FRI), "Fri 1 Jan 2027 09:00");
});

test("project scores prefer the start of the name", () => {
  assert.ok(scoreProject("cyber", "Cybersecurity") > scoreProject("security", "Cybersecurity"));
  assert.ok(scoreProject("research", "Research corpus") > 0);
  assert.equal(scoreProject("zzz", "Operations"), 0);
});
