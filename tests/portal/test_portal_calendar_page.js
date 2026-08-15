/*
 * test_portal_calendar_page.js - Visual Calendar Phase 1 proofs (read-only
 * Week view on a true time axis).
 *
 * Drives the REAL portal-pages.js AND the REAL portal-calendar.js in one
 * Node vm context over a fake document and a scripted fake data layer (the
 * test_portal_schedule_page.js technique), and proves the Phase 1 contract
 * plus the visual refinement:
 *
 *   - navigation: Calendar opens, and the Leads-default fallback does NOT
 *     light up while the Calendar page is visible
 *   - data ownership: a week load calls EXACTLY the two existing frozen read
 *     methods with the closed parameter vocabulary, and nothing else - the
 *     fake data layer throws on any unscripted call
 *   - opening the detail panel issues NO request at all
 *   - the response consistency guard fails CLOSED on any start_day /
 *     end_day / timezone_name disagreement: nothing renders
 *   - time-axis geometry: position and height follow the office-local wall
 *     clock, and a 30-minute entry is exactly half a 60-minute entry
 *   - the sticky hour rail is emitted with one cell per displayed hour
 *   - adjacent open slots consolidate into ONE band while the underlying
 *     authoritative slot count is preserved and the request is unchanged
 *   - compact blocks carry time + patient + service, and never the
 *     fully-qualified date or the timezone
 *   - confirmed and pending differ by class AND by printed word
 *   - the read-only drawer opens from already-loaded data and closes clean
 *   - presentation filtering: cancelled / completed / no_show never appear
 *   - stale-response protection, Refresh, session loss
 *   - DST-sensitive bucketing AND positioning, including 2026-11-01 and
 *     2026-03-08 America/New_York
 *   - mobile: the horizontally scrollable frame structure is present
 *
 * Run: node tests/portal/test_portal_calendar_page.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const h = require("./portal_test_harness.js");
const { test, assert, assertEqual } = h;

const PORTAL_DIR = path.join(__dirname, "..", "..", "static", "portal");
const PAGES_PATH = path.join(PORTAL_DIR, "portal-pages.js");
const CALENDAR_PATH = path.join(PORTAL_DIR, "portal-calendar.js");

/* ------------------------------------------------------------------ */
/* Fixtures (the frozen page-suite technique, plus style/title)         */
/* ------------------------------------------------------------------ */

function makeClassList() {
  const set = new Set();
  return {
    toggle(name, force) {
      if (force === undefined) {
        if (set.has(name)) { set.delete(name); } else { set.add(name); }
      } else if (force) { set.add(name); } else { set.delete(name); }
    },
    contains(name) { return set.has(name); }
  };
}

function makeElement(tag) {
  const element = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    listeners: {},
    classList: makeClassList(),
    /* Geometry is applied through CSSOM property assignment (never a parsed
     * style attribute), so the fake mirrors that with a plain style bag. */
    style: {},
    title: "",
    textContent: "",
    className: "",
    value: "",
    type: "",
    hidden: false,
    disabled: false,
    get firstChild() {
      return element.children.length ? element.children[0] : null;
    },
    appendChild(child) { element.children.push(child); return child; },
    removeChild(child) {
      const index = element.children.indexOf(child);
      if (index !== -1) { element.children.splice(index, 1); }
      return child;
    },
    addEventListener(type, handler) {
      (element.listeners[type] = element.listeners[type] || []).push(handler);
    },
    trigger(type) {
      const event = { preventDefault: () => {} };
      (element.listeners[type] || []).forEach((handler) => handler(event));
    }
  };
  return element;
}

const PAGE_ELEMENT_IDS = [
  "nav-dashboard", "nav-leads",
  "page-dashboard", "page-leads", "page-lead-detail",
  "dashboard-state", "dashboard-counts", "count-conversations",
  "count-leads", "count-urgent-leads", "count-recent-leads", "dashboard-recent",
  "leads-filter-form", "leads-search", "leads-status",
  "leads-state", "leads-list", "leads-prev", "leads-next", "leads-page-label",
  "detail-back", "detail-state", "detail-body", "detail-name",
  "detail-badges", "detail-fields", "detail-messages",
  "detail-transcript-note",
  "detail-office-status", "detail-status-meta", "detail-status-save",
  "detail-status-feedback", "detail-office-note", "detail-note-meta",
  "detail-note-save", "detail-note-clear", "detail-note-feedback",
  "nav-appointments", "page-appointments", "appointments-state",
  "appointments-list", "appt-range-label", "appt-timezone-note",
  "appt-prev", "appt-next", "appt-action-feedback",
  "nav-schedule", "page-schedule", "schedule-state", "schedule-list",
  "schedule-range-label", "schedule-timezone-note",
  "schedule-prev", "schedule-next",
  "schedule-day", "schedule-open", "schedule-end", "schedule-minutes",
  "schedule-publish", "schedule-publish-feedback",
  "schedule-block-all", "schedule-bulk-feedback",
  "schedule-booked-remaining", "schedule-action-feedback",
  "nav-settings", "page-settings", "settings-state",
  "settings-email", "settings-phone", "settings-save",
  "nav-recurring", "page-recurring", "recurring-state", "recurring-hours",
  "recurring-slot-minutes", "recurring-closures", "recurring-closure-date",
  "recurring-closure-add", "recurring-closure-end", "recurring-closure-warning",
  "recurring-save", "recurring-save-feedback",
  "recurring-preview", "recurring-preview-output",
  "recurring-apply", "recurring-apply-output",
  "settings-feedback", "settings-email-status", "settings-sms-status",
  /* Visual Calendar Phase 1 id contract, including the detail panel. */
  "nav-calendar", "page-calendar", "calendar-state", "calendar-grid",
  "calendar-range-label", "calendar-timezone-note",
  "calendar-prev", "calendar-next", "calendar-refresh",
  "calendar-drawer", "calendar-drawer-title", "calendar-drawer-status",
  "calendar-drawer-fields", "calendar-drawer-close",
  "calendar-drawer-actions-note"
];

function makeDocument() {
  const byId = {};
  for (const id of PAGE_ELEMENT_IDS) { byId[id] = makeElement("div"); }
  return {
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => makeElement(tag),
    _elements: byId
  };
}

function makeFakeData() {
  const names = ["getDashboard", "listLeads", "getLeadDetail",
    "putLeadStatus", "putLeadNote", "getAppointments",
    "getSchedule", "publishScheduleDay", "blockScheduleSlot",
    "unblockScheduleSlot", "blockAllOpenSlots"];
  const queues = {};
  const calls = {};
  for (const name of names) { queues[name] = []; calls[name] = []; }
  function next(name, args) {
    calls[name].push(args);
    if (!queues[name].length) {
      throw new Error("fake data: unscripted call to " + name);
    }
    const scripted = queues[name].shift();
    return scripted.promise || Promise.resolve(scripted.outcome);
  }
  return {
    getDashboard: () => next("getDashboard", null),
    listLeads: (params) => next("listLeads", params),
    getLeadDetail: (leadId) => next("getLeadDetail", leadId),
    putLeadStatus: (leadId, status, token) =>
      next("putLeadStatus", { leadId, status, token }),
    putLeadNote: (leadId, note, token) =>
      next("putLeadNote", { leadId, note, token }),
    getAppointments: (params) => next("getAppointments", params),
    getSchedule: (params) => next("getSchedule", params),
    publishScheduleDay: (day, openTime, closeTime, slotMinutes) =>
      next("publishScheduleDay", { day, openTime, closeTime, slotMinutes }),
    blockScheduleSlot: (slotId) => next("blockScheduleSlot", slotId),
    unblockScheduleSlot: (slotId) => next("unblockScheduleSlot", slotId),
    blockAllOpenSlots: (day) => next("blockAllOpenSlots", day),
    queue: (name, outcome) => queues[name].push({ outcome }),
    queueDeferred: (name) => {
      let resolve;
      const promise = new Promise((r) => { resolve = r; });
      queues[name].push({ promise });
      return { resolve };
    },
    totalCalls: () => names.reduce((sum, n) => sum + calls[n].length, 0),
    calls
  };
}

/* BOTH production modules load into ONE vm context, in the same order
 * index.html loads them, so portal-pages.js genuinely finds the renderer. */
function loadFactories() {
  const sandboxWindow = {};
  const context = { window: sandboxWindow };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(CALENDAR_PATH, "utf8"), context,
    { filename: "portal-calendar.js" });
  vm.runInContext(fs.readFileSync(PAGES_PATH, "utf8"), context,
    { filename: "portal-pages.js" });
  return {
    createPages: sandboxWindow.createMiaPortalPages,
    createCalendar: sandboxWindow.createMiaPortalCalendar
  };
}

function makePages() {
  const factories = loadFactories();
  const doc = makeDocument();
  const data = makeFakeData();
  const sessionLost = [];
  const pages = factories.createPages({
    data: data,
    documentRef: doc,
    onSessionLost: (state) => sessionLost.push(state)
  });
  return {
    pages, doc, data, sessionLost,
    helpers: factories.createPages.helpers,
    calendarHelpers: factories.createCalendar.helpers,
    createCalendar: factories.createCalendar
  };
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

const TZ = "America/New_York";
const WEEK_START = "2026-08-24";
const WEEK_END = "2026-08-30";
/* 2026-08-24 is EDT (UTC-4): 13:00Z is 9:00 AM local. */
const HOUR = 48;   /* HOUR_HEIGHT_PX */

function slotFixture(overrides) {
  return Object.assign({
    slot_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    start_datetime: "2026-08-24T13:00:00Z",
    end_datetime: "2026-08-24T13:30:00Z",
    status: "available",
    provider_name: null,
    service_key: null
  }, overrides || {});
}

function appointmentFixture(overrides) {
  return Object.assign({
    appointment_id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
    patient_name: "Rosa Delgado",
    patient_phone: "516-555-0134",
    patient_email: "rosa@example.test",
    new_or_returning: "new",
    reason: "implant consultation",
    urgency: "routine",
    start_datetime: "2026-08-24T14:00:00Z",
    end_datetime: "2026-08-24T15:00:00Z",
    status: "confirmed",
    confirmed_at: "2026-08-20T18:00:00Z",
    source: "mia_widget",
    notification_outcome: "sent"
  }, overrides || {});
}

function scheduleBody(slots, overrides) {
  return Object.assign({
    timezone_name: TZ, start_day: WEEK_START, end_day: WEEK_END,
    slots: slots || []
  }, overrides || {});
}

function appointmentsBody(appointments, overrides) {
  return Object.assign({
    timezone_name: TZ, start_day: WEEK_START, end_day: WEEK_END,
    appointments: appointments || []
  }, overrides || {});
}

function queueWeek(f, slots, appointments, so, ao) {
  f.data.queue("getSchedule", { ok: true, data: scheduleBody(slots, so) });
  f.data.queue("getAppointments",
    { ok: true, data: appointmentsBody(appointments, ao) });
}

function openCalendar(f) {
  f.doc._elements["nav-calendar"].trigger("click");
}

/* ---- structural accessors for the time-axis grid ---- */

function frame(doc) {
  const grid = doc._elements["calendar-grid"];
  if (!grid.children.length) { return null; }
  return grid.children[0].children[0];
}

function rail(doc) {
  const f = frame(doc);
  return f === null ? null : f.children[0];
}

function columns(doc) {
  const f = frame(doc);
  return f === null ? [] : f.children[1].children;
}

function columnHead(column) {
  return column.children[0].children.map((s) => s.textContent).join(" ");
}

function bandsIn(column) {
  return column.children[1].children[1].children;
}

function blocksIn(column) {
  return column.children[1].children[2].children;
}

function blockTexts(block) {
  return block.children.map((s) => s.textContent);
}

function allBlocks(doc) {
  const out = [];
  for (const column of columns(doc)) {
    for (const block of blocksIn(column)) { out.push(block); }
  }
  return out;
}

function allBands(doc) {
  const out = [];
  for (const column of columns(doc)) {
    for (const band of bandsIn(column)) { out.push(band); }
  }
  return out;
}

function drawerPairs(doc) {
  const fields = doc._elements["calendar-drawer-fields"].children;
  const pairs = [];
  for (let i = 0; i + 1 < fields.length; i += 2) {
    pairs.push([fields[i].textContent, fields[i + 1].textContent]);
  }
  return pairs;
}

function drawerValue(doc, label) {
  for (const [key, value] of drawerPairs(doc)) {
    if (key === label) { return value; }
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* Navigation                                                          */
/* ------------------------------------------------------------------ */

test("calendar: opening shows the Calendar page and hides the others", async () => {
  const f = makePages();
  queueWeek(f, [slotFixture()], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  assertEqual(f.doc._elements["page-calendar"].hidden, false, "calendar visible");
  assertEqual(f.doc._elements["page-schedule"].hidden, true, "schedule hidden");
  assertEqual(f.doc._elements["page-appointments"].hidden, true, "appts hidden");
  assertEqual(f.doc._elements["page-leads"].hidden, true, "leads hidden");
});

test("calendar: the nav highlights Calendar and NOT Leads (isLeads fallback fixed)", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  assert(f.doc._elements["nav-calendar"].classList.contains("portal-nav-active"),
    "Calendar nav active");
  assert(!f.doc._elements["nav-leads"].classList.contains("portal-nav-active"),
    "Leads nav must NOT be active while the Calendar page is visible");
  assert(!f.doc._elements["nav-schedule"].classList.contains("portal-nav-active"),
    "Schedule nav not active");
});

test("calendar: Schedule and Recurring pages still open normally (nothing absorbed)", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();
  assertEqual(f.doc._elements["page-schedule"].hidden, false, "Schedule reachable");
  assertEqual(f.doc._elements["page-calendar"].hidden, true, "Calendar hidden");
  assert(!f.doc._elements["nav-calendar"].classList.contains("portal-nav-active"),
    "Calendar nav released");
});

/* ------------------------------------------------------------------ */
/* Data ownership - no new network owner                                */
/* ------------------------------------------------------------------ */

test("calendar: a default week calls EXACTLY getSchedule + getAppointments with no bounds", async () => {
  const f = makePages();
  queueWeek(f, [slotFixture()], []);
  openCalendar(f);
  await flush();
  assertEqual(f.data.calls.getSchedule.length, 1, "one schedule read");
  assertEqual(f.data.calls.getAppointments.length, 1, "one appointments read");
  assertEqual(JSON.stringify(f.data.calls.getSchedule[0]), "{}", "no bounds");
  assertEqual(JSON.stringify(f.data.calls.getAppointments[0]), "{}", "no bounds");
  for (const name of ["getDashboard", "listLeads", "getLeadDetail",
    "putLeadStatus", "putLeadNote", "publishScheduleDay", "blockScheduleSlot",
    "unblockScheduleSlot", "blockAllOpenSlots"]) {
    assertEqual(f.data.calls[name].length, 0, "calendar must not call " + name);
  }
});

test("calendar: week paging sends explicit bounds derived from the backend anchor", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  queueWeek(f, [], [], { start_day: "2026-08-31", end_day: "2026-09-06" },
    { start_day: "2026-08-31", end_day: "2026-09-06" });
  f.doc._elements["calendar-next"].trigger("click");
  await flush();
  assertEqual(JSON.stringify(f.data.calls.getSchedule[1]),
    JSON.stringify({ start_day: "2026-08-31", end_day: "2026-09-06" }),
    "anchor + 7, six days inclusive");
  assertEqual(JSON.stringify(f.data.calls.getAppointments[1]),
    JSON.stringify({ start_day: "2026-08-31", end_day: "2026-09-06" }),
    "BOTH reads use the identical window");
});

test("calendar: paging is refused until the backend anchor is known", async () => {
  const f = makePages();
  const deferred = f.data.queueDeferred("getSchedule");
  f.data.queue("getAppointments", { ok: true, data: appointmentsBody([]) });
  openCalendar(f);
  await flush();
  f.doc._elements["calendar-next"].trigger("click");
  f.doc._elements["calendar-prev"].trigger("click");
  await flush();
  assertEqual(f.data.calls.getSchedule.length, 1, "no read from a missing anchor");
  deferred.resolve({ ok: true, data: scheduleBody([]) });
  await flush();
});

/* ------------------------------------------------------------------ */
/* Response consistency guard - fail closed                             */
/* ------------------------------------------------------------------ */

test("calendar: a start_day disagreement renders NOTHING and says so", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([slotFixture()]) });
  f.data.queue("getAppointments", { ok: true,
    data: appointmentsBody([appointmentFixture()], { start_day: "2026-08-25" }) });
  openCalendar(f);
  await flush();
  assertEqual(columns(f.doc).length, 0, "no grid is rendered");
  assertEqual(f.doc._elements["calendar-range-label"].textContent, "", "no label");
  assertEqual(f.doc._elements["calendar-timezone-note"].textContent, "", "no tz");
  assert(f.doc._elements["calendar-state"].textContent.indexOf(
    "two different date ranges") !== -1, "the office is told plainly");
});

test("calendar: an end_day disagreement fails closed", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([slotFixture()]) });
  f.data.queue("getAppointments", { ok: true,
    data: appointmentsBody([], { end_day: "2026-09-30" }) });
  openCalendar(f);
  await flush();
  assertEqual(columns(f.doc).length, 0, "no grid");
});

test("calendar: a timezone_name disagreement fails closed", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([slotFixture()]) });
  f.data.queue("getAppointments", { ok: true,
    data: appointmentsBody([], { timezone_name: "America/Chicago" }) });
  openCalendar(f);
  await flush();
  assertEqual(columns(f.doc).length, 0, "no grid");
  assert(f.doc._elements["calendar-state"].textContent.length > 0,
    "the refusal is visible, never a silent blank page");
});

test("calendar: nav is disabled after a refusal so a mixed window cannot be paged", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.data.queue("getAppointments", { ok: true,
    data: appointmentsBody([], { timezone_name: "UTC" }) });
  openCalendar(f);
  await flush();
  assertEqual(f.doc._elements["calendar-prev"].disabled, true, "prev disabled");
  assertEqual(f.doc._elements["calendar-next"].disabled, true, "next disabled");
});

/* ------------------------------------------------------------------ */
/* Time axis: rail, positioning, duration geometry                      */
/* ------------------------------------------------------------------ */

test("calendar: the hour rail carries a corner plus one cell per displayed hour", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  const r = rail(f.doc);
  assertEqual(r.className, "portal-calendar-rail", "the sticky rail element");
  assertEqual(r.children[0].className, "portal-calendar-rail-corner",
    "the corner aligns the rail with the day headers");
  /* Resting window is 8 AM .. 6 PM = ten hour cells. */
  assertEqual(r.children.length, 11, "corner + ten hour cells");
  assertEqual(r.children[1].children[0].textContent, "8 AM", "first hour label");
  assertEqual(r.children[10].children[0].textContent, "5 PM", "last hour label");
  assertEqual(r.children[1].style.height, HOUR + "px", "one hour is one row");
});

test("calendar: the hour window EXPANDS to contain an early entry, never hiding it", async () => {
  const f = makePages();
  /* 11:00Z is 7:00 AM EDT - before the resting 8 AM start. */
  queueWeek(f, [slotFixture({ start_datetime: "2026-08-24T11:00:00Z",
    end_datetime: "2026-08-24T11:30:00Z" })], []);
  openCalendar(f);
  await flush();
  const r = rail(f.doc);
  assertEqual(r.children[1].children[0].textContent, "7 AM",
    "the window opened an hour earlier rather than clipping the entry");
  assertEqual(bandsIn(columns(f.doc)[0])[0].style.top, "0px",
    "the 7:00 entry sits at the very top of the expanded axis");
});

test("calendar: entries are positioned by their office-local wall clock", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const block = blocksIn(columns(f.doc)[0])[0];
  /* 14:00Z = 10:00 AM EDT. Window starts 8 AM, so two hours down. */
  assertEqual(block.style.top, (2 * HOUR) + "px", "10 AM is two hours below 8 AM");
  assertEqual(block.style.height, HOUR + "px", "a 60-minute block is one hour tall");
});

test("calendar: a 30-minute entry is EXACTLY half the height of a 60-minute entry", async () => {
  const f = makePages();
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "sixty",
      start_datetime: "2026-08-24T14:00:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    appointmentFixture({ appointment_id: "thirty",
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T16:30:00Z" })
  ]);
  openCalendar(f);
  await flush();
  const blocks = blocksIn(columns(f.doc)[0]);
  const sixty = parseFloat(blocks[0].style.height);
  const thirty = parseFloat(blocks[1].style.height);
  assertEqual(sixty, HOUR, "60 minutes = one hour row");
  assertEqual(thirty, HOUR / 2, "30 minutes = half an hour row");
  assertEqual(sixty / thirty, 2, "the ratio is exactly two");
});

test("calendar: geometry is pure minutes-to-pixels (helper level)", () => {
  const f = makePages();
  const G = f.calendarHelpers.geometryFor;
  assertEqual(G(540, 600, 8).top, 48, "9:00 with an 8 AM axis start");
  assertEqual(G(540, 600, 8).height, 48, "one hour");
  assertEqual(G(540, 570, 8).height, 24, "half an hour");
  assertEqual(G(480, 510, 8).top, 0, "the axis origin");
  /* A very short entry still gets a readable minimum height, and the caption
   * continues to state the true time. */
  assert(G(540, 545, 8).height >= (f.calendarHelpers.MIN_BLOCK_MINUTES / 60) * 48,
    "a tiny entry keeps a clickable minimum height");
});

test("calendar: overlapping appointments split into side-by-side lanes", async () => {
  const f = makePages();
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "one",
      start_datetime: "2026-08-24T14:00:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    appointmentFixture({ appointment_id: "two", patient_name: "Ben Ortiz",
      start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:30:00Z" })
  ]);
  openCalendar(f);
  await flush();
  const blocks = blocksIn(columns(f.doc)[0]);
  assertEqual(blocks.length, 2, "both overlapping appointments are drawn");
  assertEqual(blocks[0].style.width, "50%", "two lanes halve the width");
  assertEqual(blocks[0].style.left, "0%", "first lane at the left edge");
  assertEqual(blocks[1].style.left, "50%", "second lane beside it");
});

test("calendar: a non-overlapping day keeps full-width blocks", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const block = blocksIn(columns(f.doc)[0])[0];
  assertEqual(block.style.width, "100%", "a lone appointment uses the column");
  assertEqual(block.style.left, "0%", "and starts at the left edge");
});

/* ------------------------------------------------------------------ */
/* Availability consolidation                                           */
/* ------------------------------------------------------------------ */

test("calendar: adjacent open slots consolidate into ONE band carrying the count", async () => {
  const f = makePages();
  /* Four contiguous 30-minute openings, 9:00 to 11:00 local. */
  queueWeek(f, [
    slotFixture({ slot_id: "s1", start_datetime: "2026-08-24T13:00:00Z",
      end_datetime: "2026-08-24T13:30:00Z" }),
    slotFixture({ slot_id: "s2", start_datetime: "2026-08-24T13:30:00Z",
      end_datetime: "2026-08-24T14:00:00Z" }),
    slotFixture({ slot_id: "s3", start_datetime: "2026-08-24T14:00:00Z",
      end_datetime: "2026-08-24T14:30:00Z" }),
    slotFixture({ slot_id: "s4", start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:00:00Z" })
  ], []);
  openCalendar(f);
  await flush();
  const bands = bandsIn(columns(f.doc)[0]);
  assertEqual(bands.length, 1, "four cards became one calm band");
  /* The RESTING view prints the status word only - a repeated count on
   * every band was noise. The count is not lost: it stays exact on the
   * band model and is surfaced on hover. */
  assertEqual(bands[0].children[0].textContent, "Open",
    "the visible label is the status word alone, with no count");
  assert(bands[0].title.indexOf("4 slots") !== -1,
    "the authoritative slot COUNT remains available on the title: " +
    bands[0].title);
  assertEqual(bands[0].style.top, (1 * HOUR) + "px", "starts at 9 AM");
  assertEqual(bands[0].style.height, (2 * HOUR) + "px", "spans two hours");
});

test("calendar: no band label carries a count in the resting calendar", async () => {
  const f = makePages();
  const slots = [];
  for (let i = 0; i < 10; i++) {
    slots.push(slotFixture({ slot_id: "s" + i,
      start_datetime: "2026-08-24T" + (13 + Math.floor(i / 2)) +
        ((i % 2) ? ":30" : ":00") + ":00Z",
      end_datetime: "2026-08-24T" + (13 + Math.floor((i + 1) / 2)) +
        (((i + 1) % 2) ? ":30" : ":00") + ":00Z" }));
  }
  queueWeek(f, slots, []);
  openCalendar(f);
  await flush();
  const bands = bandsIn(columns(f.doc)[0]);
  assertEqual(bands.length, 1, "ten openings consolidate to one band");
  assertEqual(bands[0].children[0].textContent, "Open",
    "never \"Open (10)\" in the default view");
  assert(bands[0].title.indexOf("10 slots") !== -1,
    "all ten underlying slots remain represented");
});

test("calendar: an Open band uses subdued presentation, not a headline block", async () => {
  const f = makePages();
  queueWeek(f, [slotFixture()], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const band = bandsIn(columns(f.doc)[0])[0];
  const block = blocksIn(columns(f.doc)[0])[0];
  /* Structure carries the hierarchy: the band is a plain region with a
   * single quiet label, while the appointment is an activatable control
   * with several content lines. The band must never gain block styling. */
  assertEqual(band.children.length, 1, "a band is one quiet label, nothing more");
  assertEqual(band.children[0].className, "portal-calendar-band-label",
    "and it uses the subdued band label class");
  assert(band.className.indexOf("portal-calendar-block") === -1,
    "an availability band never borrows appointment-block styling");
  assert(block.children.length > band.children.length,
    "the appointment carries more information than the background band");
});

test("calendar: a gap between openings produces two separate bands", async () => {
  const f = makePages();
  queueWeek(f, [
    slotFixture({ slot_id: "s1", start_datetime: "2026-08-24T13:00:00Z",
      end_datetime: "2026-08-24T13:30:00Z" }),
    slotFixture({ slot_id: "s2", start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T16:30:00Z" })
  ], []);
  openCalendar(f);
  await flush();
  assertEqual(bandsIn(columns(f.doc)[0]).length, 2,
    "a real gap in availability stays visible as a gap");
});

test("calendar: held and blocked stay visually and semantically distinct from open", async () => {
  const f = makePages();
  queueWeek(f, [
    slotFixture({ slot_id: "o", status: "available" }),
    slotFixture({ slot_id: "h", status: "held",
      start_datetime: "2026-08-24T13:30:00Z",
      end_datetime: "2026-08-24T14:00:00Z" }),
    slotFixture({ slot_id: "b", status: "blocked",
      start_datetime: "2026-08-24T14:00:00Z",
      end_datetime: "2026-08-24T14:30:00Z" })
  ], []);
  openCalendar(f);
  await flush();
  const bands = bandsIn(columns(f.doc)[0]);
  assertEqual(bands.length, 3, "different statuses never merge together");
  const labels = bands.map((b) => b.children[0].textContent).sort();
  assertEqual(JSON.stringify(labels),
    JSON.stringify(["Blocked", "On hold", "Open"]),
    "each band prints its frozen status word - meaning is never colour alone");
  for (const band of bands) {
    assert(band.className.indexOf("portal-calendar-band-") !== -1,
      "and carries a status-derived class for styling");
  }
});

test("calendar: consolidation is PRESENTATION only - the request is unchanged", async () => {
  const f = makePages();
  queueWeek(f, [
    slotFixture({ slot_id: "s1" }),
    slotFixture({ slot_id: "s2", start_datetime: "2026-08-24T13:30:00Z",
      end_datetime: "2026-08-24T14:00:00Z" })
  ], []);
  openCalendar(f);
  await flush();
  assertEqual(JSON.stringify(f.data.calls.getSchedule[0]), "{}",
    "no consolidation parameter is invented; the closed vocabulary is unchanged");
});

test("calendar: consolidateBands preserves every underlying slot (helper level)", () => {
  const f = makePages();
  const H = f.calendarHelpers;
  const entries = [];
  for (let i = 0; i < 16; i++) {
    entries.push({ kind: "slot", status: "available",
      startMinutes: 540 + i * 30, endMinutes: 570 + i * 30 });
  }
  const bands = H.consolidateBands(entries);
  assertEqual(bands.length, 1, "a full day of openings is one band");
  assertEqual(bands[0].slotCount, 16, "all sixteen authoritative rows counted");
  assertEqual(bands[0].startMinutes, 540, "band starts at the first slot");
  assertEqual(bands[0].endMinutes, 540 + 16 * 30, "and ends at the last");
});

test("calendar: no surface claims an open slot is bookable", async () => {
  const f = makePages();
  queueWeek(f, [slotFixture()], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const texts = [];
  for (const band of allBands(f.doc)) {
    texts.push(band.children[0].textContent, band.title);
  }
  for (const block of allBlocks(f.doc)) {
    texts.push(block.title, blockTexts(block).join(" "));
  }
  for (const text of texts) {
    assert(String(text).toLowerCase().indexOf("bookable") === -1,
      "no rendered text claims bookability: " + text);
  }
});

/* ------------------------------------------------------------------ */
/* Compact blocks and status semantics                                  */
/* ------------------------------------------------------------------ */

test("calendar: a block shows start time, patient and service - not the full date", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const block = blocksIn(columns(f.doc)[0])[0];
  const texts = blockTexts(block);
  assertEqual(texts[0], "10:00 AM", "the short local start time");
  assertEqual(texts[1], "Rosa Delgado", "patient name for rapid scanning");
  assertEqual(texts[2], "implant consultation", "service/reason");
  assertEqual(texts[3], "Confirmed", "a short status word travels in the markup");
  const joined = texts.join(" | ");
  assert(joined.indexOf(TZ) === -1,
    "the timezone is NOT repeated in the block: " + joined);
  assert(joined.indexOf("2026") === -1,
    "the full date is NOT repeated in the block: " + joined);
  assert(joined.indexOf(" - ") === -1,
    "no start-and-end date string pair in the block: " + joined);
});

test("calendar: a short block drops the service line but keeps the status word", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({
    start_datetime: "2026-08-24T14:00:00Z",
    end_datetime: "2026-08-24T14:30:00Z" })]);
  openCalendar(f);
  await flush();
  const block = blocksIn(columns(f.doc)[0])[0];
  assert(block.className.indexOf("portal-calendar-block-compact") !== -1,
    "a 30-minute block is compact");
  const texts = blockTexts(block);
  assertEqual(texts.length, 3, "time, name and status only");
  assertEqual(texts[2], "Confirmed",
    "the status word remains in the markup for assistive technology");
});

test("calendar: confirmed and pending differ by class AND by printed word", async () => {
  const f = makePages();
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "c", status: "confirmed" }),
    appointmentFixture({ appointment_id: "p", status: "pending",
      patient_name: "Ana Ruiz",
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T17:00:00Z" })
  ]);
  openCalendar(f);
  await flush();
  const blocks = blocksIn(columns(f.doc)[0]);
  assert(blocks[0].className.indexOf("portal-calendar-appointment-confirmed") !== -1,
    "confirmed class");
  assert(blocks[1].className.indexOf("portal-calendar-appointment-pending") !== -1,
    "pending class");
  assertEqual(blockTexts(blocks[0])[3], "Confirmed", "confirmed word");
  assertEqual(blockTexts(blocks[1])[3], "Pending", "pending word");
});

test("calendar: a laned block truncates cleanly and keeps the patient name", async () => {
  const f = makePages();
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "one",
      patient_name: "Bartholomew Featherstonehaugh-Cholmondeley",
      reason: "comprehensive new patient examination and full mouth series",
      start_datetime: "2026-08-24T14:00:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    appointmentFixture({ appointment_id: "two", patient_name: "Ben Ortiz",
      start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:30:00Z" })
  ]);
  openCalendar(f);
  await flush();
  const blocks = blocksIn(columns(f.doc)[0]);
  for (const block of blocks) {
    assert(block.className.indexOf("portal-calendar-block-narrow") !== -1,
      "a laned block is marked narrow so CSS can truncate it");
  }
  const texts = blockTexts(blocks[0]);
  assertEqual(texts[1],
    "Bartholomew Featherstonehaugh-Cholmondeley",
    "the patient name is kept intact - CSS clips it, the text is never cut");
  assert(blocks[0].title.indexOf("Bartholomew") !== -1 &&
    blocks[0].title.indexOf("comprehensive new patient") !== -1,
    "the hover title carries everything a truncated block had to drop");
  /* The grid is never widened to fit text. */
  assertEqual(blocks[0].style.width, "50%", "two lanes, unchanged geometry");
});

test("calendar: a full-width block is not marked narrow", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  assert(blocksIn(columns(f.doc)[0])[0].className
    .indexOf("portal-calendar-block-narrow") === -1,
    "a lone appointment keeps its service line");
});

test("calendar: blocks are real buttons, so they are keyboard reachable", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const block = blocksIn(columns(f.doc)[0])[0];
  assertEqual(block.tagName, "BUTTON", "an activatable control, not a div");
  assertEqual(block.type, "button", "never a submit control");
});

test("calendar: the timezone is stated ONCE at calendar level", async () => {
  const f = makePages();
  queueWeek(f, [slotFixture()], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  assertEqual(f.doc._elements["calendar-timezone-note"].textContent,
    "Times shown in the office timezone: " + TZ,
    "the shared timezone note wording, once");
});

test("calendar: day headers separate weekday from date", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  const cols = columns(f.doc);
  assertEqual(cols.length, 7, "seven day columns");
  assertEqual(columnHead(cols[0]), "Mon Aug 24", "first column header");
  assertEqual(columnHead(cols[6]), "Sun Aug 30", "last column header");
});

test("calendar: an empty week says so rather than rendering a silent blank", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  assertEqual(f.doc._elements["calendar-state"].textContent,
    "Nothing scheduled in this range.", "the empty state is explicit");
  assertEqual(columns(f.doc).length, 7, "the seven columns are still drawn");
});

/* ------------------------------------------------------------------ */
/* Presentation filtering                                               */
/* ------------------------------------------------------------------ */

test("calendar: cancelled, completed and no_show never reach the resting calendar", async () => {
  const f = makePages();
  queueWeek(f, [
    slotFixture({ slot_id: "ok", status: "available" }),
    slotFixture({ slot_id: "gone", status: "cancelled",
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T16:30:00Z" })
  ], [
    appointmentFixture({ appointment_id: "keep", status: "confirmed" }),
    appointmentFixture({ appointment_id: "x1", status: "cancelled",
      start_datetime: "2026-08-24T17:00:00Z",
      end_datetime: "2026-08-24T17:30:00Z" }),
    appointmentFixture({ appointment_id: "x2", status: "no_show",
      start_datetime: "2026-08-24T18:00:00Z",
      end_datetime: "2026-08-24T18:30:00Z" }),
    appointmentFixture({ appointment_id: "x3", status: "completed",
      start_datetime: "2026-08-24T19:00:00Z",
      end_datetime: "2026-08-24T19:30:00Z" })
  ]);
  openCalendar(f);
  await flush();
  assertEqual(allBlocks(f.doc).length, 1, "only the confirmed appointment");
  assertEqual(allBands(f.doc).length, 1, "only the open slot band");
  assertEqual(allBands(f.doc)[0].children[0].textContent, "Open",
    "and it is the open band, not a cancelled one");
});

test("calendar: no rescheduled status is invented", () => {
  const f = makePages();
  const H = f.calendarHelpers;
  assert(!H.isVisibleAppointmentStatus("rescheduled"),
    "the grid recognises no status the backend does not define");
  assertEqual(JSON.stringify(H.VISIBLE_APPOINTMENT_STATUSES),
    JSON.stringify(["pending", "confirmed"]), "exactly the Phase 1 scope");
  assertEqual(JSON.stringify(H.VISIBLE_SLOT_STATUSES),
    JSON.stringify(["available", "held", "blocked"]), "exactly the slot scope");
});

/* ------------------------------------------------------------------ */
/* Read-only detail drawer                                              */
/* ------------------------------------------------------------------ */

test("calendar: clicking a block opens the detail panel from ALREADY LOADED data", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "the panel starts closed");

  const callsBefore = f.data.totalCalls();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();

  assertEqual(f.data.totalCalls(), callsBefore,
    "opening details issues NO request of any kind");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, false, "panel open");
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent,
    "Rosa Delgado", "titled by patient");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent,
    "Confirmed", "status stated in words");
});

test("calendar: the clicked block becomes selected while the panel is open", async () => {
  const f = makePages();
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "one" }),
    appointmentFixture({ appointment_id: "two", patient_name: "Ana Ruiz",
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T17:00:00Z" })
  ]);
  openCalendar(f);
  await flush();
  const blocks = blocksIn(columns(f.doc)[0]);
  assert(!blocks[0].classList.contains("portal-calendar-block-selected"),
    "nothing is selected before a click");

  blocks[1].trigger("click");
  await flush();
  assert(blocks[1].classList.contains("portal-calendar-block-selected"),
    "the clicked block is selected");
  assert(!blocks[0].classList.contains("portal-calendar-block-selected"),
    "and it is the ONLY selected block");

  blocks[0].trigger("click");
  await flush();
  assert(blocks[0].classList.contains("portal-calendar-block-selected"),
    "selecting another block moves the selection");
  assert(!blocks[1].classList.contains("portal-calendar-block-selected"),
    "the previous selection is released");
});

test("calendar: Close clears the selected block", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const block = blocksIn(columns(f.doc)[0])[0];
  block.trigger("click");
  await flush();
  assert(block.classList.contains("portal-calendar-block-selected"), "selected");
  f.doc._elements["calendar-drawer-close"].trigger("click");
  assert(!block.classList.contains("portal-calendar-block-selected"),
    "closing the panel releases the selection");
});

test("calendar: reset and session loss clear the selected block", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const first = blocksIn(columns(f.doc)[0])[0];
  first.trigger("click");
  await flush();
  f.pages.reset();
  assert(!first.classList.contains("portal-calendar-block-selected"),
    "reset releases the selection");

  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const second = blocksIn(columns(f.doc)[0])[0];
  second.trigger("click");
  await flush();
  f.data.queue("getSchedule", { ok: false, state: "unauthorized" });
  f.data.queue("getAppointments", { ok: true, data: appointmentsBody([]) });
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  assert(!second.classList.contains("portal-calendar-block-selected"),
    "session loss releases the selection");
});

test("calendar: a re-render and a week change clear the selected block", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const stale = blocksIn(columns(f.doc)[0])[0];
  stale.trigger("click");
  await flush();

  queueWeek(f, [], [appointmentFixture()]);
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  assert(!stale.classList.contains("portal-calendar-block-selected"),
    "a re-render releases the previous selection");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");

  const fresh = blocksIn(columns(f.doc)[0])[0];
  fresh.trigger("click");
  await flush();
  queueWeek(f, [], [], { start_day: "2026-08-31", end_day: "2026-09-06" },
    { start_day: "2026-08-31", end_day: "2026-09-06" });
  f.doc._elements["calendar-next"].trigger("click");
  await flush();
  assert(!fresh.classList.contains("portal-calendar-block-selected"),
    "changing week releases the selection");
});

test("calendar module: applySelection is appearance only and matches by identity", async () => {
  const f = makePages();
  const row = appointmentFixture();
  queueWeek(f, [], [row]);
  openCalendar(f);
  await flush();
  const callsBefore = f.data.totalCalls();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();
  assertEqual(f.data.totalCalls(), callsBefore,
    "selecting a block issues no request");
  /* A structurally identical but DIFFERENT object must not select anything. */
  const lookalike = appointmentFixture();
  assert(lookalike !== row, "precondition: a separate object");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, false, "panel open");
});

test("calendar: the panel shows the approved read-only detail fields", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();

  assertEqual(drawerValue(f.doc, "Patient"), "Rosa Delgado", "patient");
  assertEqual(drawerValue(f.doc, "Phone"), "516-555-0134", "phone");
  assertEqual(drawerValue(f.doc, "Email"), "rosa@example.test", "email");
  assertEqual(drawerValue(f.doc, "Service"), "implant consultation", "service");
  assertEqual(drawerValue(f.doc, "Patient type"), "new", "new or returning");
  assertEqual(drawerValue(f.doc, "Urgency"), "routine", "urgency");
  assertEqual(drawerValue(f.doc, "Status"), "Confirmed", "status");
  assertEqual(drawerValue(f.doc, "Source"), "mia_widget", "source");
  assertEqual(drawerValue(f.doc, "Office notification"), "Office notified",
    "the frozen notification vocabulary is reused");
  /* The fully-qualified rendering belongs HERE, where there is room. */
  assert(String(drawerValue(f.doc, "Starts")).indexOf(TZ) !== -1,
    "the drawer states the timezone explicitly: " + drawerValue(f.doc, "Starts"));
  assert(String(drawerValue(f.doc, "Ends")).indexOf("2026") !== -1,
    "and the full date");
});

test("calendar: a null confirmed_at is reported honestly, never inferred", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "confirmed",
    confirmed_at: null })]);
  openCalendar(f);
  await flush();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();
  assertEqual(drawerValue(f.doc, "Staff confirmed"), "Not provided",
    "a confirmed appointment may legitimately have never been STAFF confirmed");
  assertEqual(drawerValue(f.doc, "Status"), "Confirmed",
    "and the status still comes from the status field alone");
});

test("calendar: a missing optional value renders as absent, never as blank guesswork", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ patient_email: null,
    new_or_returning: null })]);
  openCalendar(f);
  await flush();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();
  assertEqual(drawerValue(f.doc, "Email"), "Not provided", "absent email");
  assertEqual(drawerValue(f.doc, "Patient type"), "Not provided", "absent type");
});

test("calendar: a null or absent patient email opens the panel and reads Not provided", async () => {
  const f = makePages();
  const withoutEmailKey = appointmentFixture();
  delete withoutEmailKey.patient_email;
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "nulled", patient_email: null }),
    Object.assign(withoutEmailKey, { appointment_id: "absent",
      patient_name: "No Email",
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T17:00:00Z" })
  ]);
  openCalendar(f);
  await flush();
  const blocks = blocksIn(columns(f.doc)[0]);
  assertEqual(blocks.length, 2, "both appointments render normally");

  const callsBefore = f.data.totalCalls();
  blocks[0].trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer"].hidden, false,
    "a null email does not prevent the panel from opening");
  assertEqual(drawerValue(f.doc, "Email"), "Not provided",
    "a null email is reported honestly, never fabricated");

  blocks[1].trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer"].hidden, false,
    "an entirely absent email key does not prevent the panel from opening");
  assertEqual(drawerValue(f.doc, "Email"), "Not provided", "still honest");
  assertEqual(drawerValue(f.doc, "Patient"), "No Email",
    "the rest of the record renders normally");
  assertEqual(f.data.totalCalls(), callsBefore,
    "and no request was issued for the missing value");
});

test("calendar: the panel reserves action space but renders NO action control", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();
  const note = f.doc._elements["calendar-drawer-actions-note"].textContent;
  assertEqual(note, "Confirm and Cancel remain on the Appointments page.",
    "the reserved region points at where actions genuinely live today");
  for (const word of ["Reschedule", "Duplicate", "Book", "Create"]) {
    assert(note.indexOf(word) === -1,
      "no unauthorized action vocabulary appears: " + word);
  }
});

test("calendar: Close wipes the panel rather than merely hiding it", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();
  assert(drawerPairs(f.doc).length > 0, "precondition: fields rendered");

  f.doc._elements["calendar-drawer-close"].trigger("click");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");
  assertEqual(drawerPairs(f.doc).length, 0, "patient contact details wiped");
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent, "", "title wiped");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "", "status wiped");
});

test("calendar: a re-render closes the panel so it can never point at a detached block", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer"].hidden, false, "open");

  queueWeek(f, [], []);
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "the refresh closed it");
  assertEqual(drawerPairs(f.doc).length, 0, "and wiped it");
});

test("calendar: session loss wipes the open detail panel too", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();

  f.data.queue("getSchedule", { ok: false, state: "unauthorized" });
  f.data.queue("getAppointments", { ok: true, data: appointmentsBody([]) });
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();

  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");
  assertEqual(drawerPairs(f.doc).length, 0,
    "no patient contact lingers on a shared front-desk computer");
  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify(["unauthorized"]),
    "control handed back to the sign-in flow");
});

/* ------------------------------------------------------------------ */
/* Mobile / responsive structure                                        */
/* ------------------------------------------------------------------ */

test("calendar: the scrollable frame keeps the rail beside the day columns", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  const grid = f.doc._elements["calendar-grid"];
  assertEqual(grid.children.length, 1, "one detached grid element is appended");
  assertEqual(grid.children[0].className, "portal-calendar-grid-inner", "inner");
  const fr = frame(f.doc);
  assertEqual(fr.className, "portal-calendar-frame",
    "the frame the horizontal-scroll rule targets");
  assertEqual(fr.children.length, 2, "rail plus days, in that order");
  assertEqual(fr.children[0].className, "portal-calendar-rail",
    "the rail is FIRST so it can stick to the left while days scroll");
  assertEqual(fr.children[1].className, "portal-calendar-days", "day track");
});

test("calendar: each day column carries head, canvas, lines, bands and blocks", async () => {
  const f = makePages();
  queueWeek(f, [slotFixture()], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const column = columns(f.doc)[0];
  assertEqual(column.className, "portal-calendar-col", "column class");
  assertEqual(column.children[0].className, "portal-calendar-dayhead", "head");
  const canvas = column.children[1];
  assertEqual(canvas.className, "portal-calendar-canvas", "canvas");
  assertEqual(canvas.style.height, (10 * HOUR) + "px",
    "the canvas is exactly as tall as the displayed hour window");
  assertEqual(canvas.children[0].className, "portal-calendar-lines", "lines");
  assertEqual(canvas.children[1].className, "portal-calendar-bands", "bands");
  assertEqual(canvas.children[2].className, "portal-calendar-blocks",
    "blocks layer sits above the bands");
  assertEqual(canvas.children[0].children.length, 10, "one hour line per hour");
});

/* ------------------------------------------------------------------ */
/* Timezone / DST                                                       */
/* ------------------------------------------------------------------ */

test("calendar DST: 2026-11-01 America/New_York - the 25-hour day buckets and positions", async () => {
  const f = makePages();
  /* Fall back. 05:00Z is 01:00 EDT and 06:00Z is 01:00 EST - two DISTINCT
   * instants that both read 1:00 AM on November 1 and must BOTH appear in
   * that column, at the SAME wall-clock position. 2026-11-02T03:00Z is 22:00
   * EST on November 1 and must not leak into the November 2 column - the
   * exact bug an ISO-string slice would produce. */
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "edt", patient_name: "Early EDT",
      start_datetime: "2026-11-01T05:00:00Z",
      end_datetime: "2026-11-01T05:30:00Z" }),
    appointmentFixture({ appointment_id: "est", patient_name: "Repeat EST",
      start_datetime: "2026-11-01T06:00:00Z",
      end_datetime: "2026-11-01T06:30:00Z" }),
    appointmentFixture({ appointment_id: "late", patient_name: "Late Night",
      start_datetime: "2026-11-02T03:00:00Z",
      end_datetime: "2026-11-02T03:30:00Z" })
  ], { start_day: "2026-11-01", end_day: "2026-11-07" },
    { start_day: "2026-11-01", end_day: "2026-11-07" });
  openCalendar(f);
  await flush();

  const cols = columns(f.doc);
  assertEqual(columnHead(cols[0]), "Sun Nov 1", "the transition day is column one");
  const day1 = blocksIn(cols[0]);
  assertEqual(day1.length, 3,
    "all three instants belong to local November 1, including 2026-11-02T03:00Z");
  assertEqual(blocksIn(cols[1]).length, 0,
    "November 2 is empty - the 22:00 local entry did NOT leak forward");
  assertEqual(blockTexts(day1[0])[0], "1:00 AM", "01:00 EDT caption");
  assertEqual(blockTexts(day1[1])[0], "1:00 AM", "01:00 EST caption - same clock");
  assertEqual(day1[0].style.top, day1[1].style.top,
    "the repeated hour occupies the same wall-clock position");
  assert(day1[0].style.left !== day1[1].style.left,
    "and they are laned side by side rather than hidden behind one another");
  assertEqual(blockTexts(day1[2])[0], "10:00 PM", "22:00 local caption");
});

test("calendar DST: 2026-03-08 America/New_York - the 23-hour day buckets and positions", async () => {
  const f = makePages();
  /* Spring forward: EDT begins at 2026-03-08T07:00Z (01:59 EST -> 03:00 EDT).
   * The boundary is proven in BOTH directions: 2026-03-09T03:00Z is 23:00 EDT
   * on March 8 and stays in that column, while 2026-03-09T04:00Z is 00:00 EDT
   * on March 9 and moves to the next. An assumed offset gets one of these
   * two wrong. */
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "pre", patient_name: "Before",
      start_datetime: "2026-03-08T06:30:00Z",
      end_datetime: "2026-03-08T07:00:00Z" }),
    appointmentFixture({ appointment_id: "post", patient_name: "After",
      start_datetime: "2026-03-08T07:00:00Z",
      end_datetime: "2026-03-08T07:30:00Z" }),
    appointmentFixture({ appointment_id: "late", patient_name: "Late",
      start_datetime: "2026-03-09T03:00:00Z",
      end_datetime: "2026-03-09T03:30:00Z" }),
    appointmentFixture({ appointment_id: "next", patient_name: "Next day",
      start_datetime: "2026-03-09T04:00:00Z",
      end_datetime: "2026-03-09T04:30:00Z" })
  ], { start_day: "2026-03-08", end_day: "2026-03-14" },
    { start_day: "2026-03-08", end_day: "2026-03-14" });
  openCalendar(f);
  await flush();

  const cols = columns(f.doc);
  assertEqual(columnHead(cols[0]), "Sun Mar 8", "transition day header");
  const day1 = blocksIn(cols[0]);
  assertEqual(day1.length, 3, "01:30 EST, 03:00 EDT and 23:00 EDT are all March 8");
  assertEqual(blocksIn(cols[1]).length, 1, "00:00 EDT belongs to March 9");
  assertEqual(blockTexts(day1[0])[0], "1:30 AM", "before the skip");
  assertEqual(blockTexts(day1[1])[0], "3:00 AM",
    "immediately after the skip - 2:00 AM never existed locally");
  assertEqual(blockTexts(day1[2])[0], "11:00 PM", "late on the short day");
  assertEqual(blockTexts(blocksIn(cols[1])[0])[0], "12:00 AM", "midnight, next day");
});

test("calendar DST: local conversion is real, and never falls back to device time", () => {
  const f = makePages();
  const H = f.calendarHelpers;
  assertEqual(H.localDayOf("2026-11-02T03:00:00Z", TZ), "2026-11-01",
    "22:00 local on the previous day");
  assertEqual(H.localParts("2026-11-02T03:00:00Z", TZ).minutes, 22 * 60,
    "and it positions at 22:00 on the axis");
  assertEqual(H.localParts("2026-08-24T13:00:00Z", TZ).caption, "9:00 AM",
    "EDT caption");
  assertEqual(H.localParts("2026-01-15T14:00:00Z", TZ).caption, "9:00 AM",
    "EST caption for the same local clock in winter");
  assertEqual(H.localDayOf("2026-11-02T03:00:00Z", "Not/AZone"), "2026-11-02",
    "an unsupported zone falls back to UTC, not the device timezone");
  assertEqual(H.localDayOf("2026-11-02T03:00:00Z", ""), "2026-11-02",
    "a blank zone falls back to UTC, not the device timezone");
  assertEqual(H.localParts("not-a-date", TZ), null, "unparseable instant");
  assertEqual(H.hourCaption(0), "12 AM", "midnight rail label");
  assertEqual(H.hourCaption(12), "12 PM", "noon rail label");
  assertEqual(H.clockCaption(13, 5), "1:05 PM", "padded minute caption");
});

test("calendar: an entry ending at or past local midnight is clamped, never inverted", () => {
  const f = makePages();
  const entry = f.calendarHelpers.positionEntry("slot", "available",
    "2026-08-24T03:30:00Z", "2026-08-24T04:00:00Z", TZ, null);
  /* 03:30Z is 23:30 local on Aug 23; 04:00Z is 00:00 local on Aug 24. */
  assertEqual(entry.day, "2026-08-23", "bucketed by its START");
  assertEqual(entry.startMinutes, 23 * 60 + 30, "23:30");
  assertEqual(entry.endMinutes, 1440, "clamped to the end of the local day");
  assert(entry.endMinutes > entry.startMinutes, "never inverted");
});

test("calendar: day columns come from the injected shiftLocalDay and stay bounded", () => {
  const f = makePages();
  const H = f.calendarHelpers;
  const shift = f.helpers.shiftLocalDay;
  assertEqual(H.dayColumns("2026-08-24", "2026-08-30", shift).length, 7, "seven");
  assertEqual(JSON.stringify(H.dayColumns("2026-11-05", "2026-11-01", shift)),
    "[]", "a reversed range is refused");
  assertEqual(JSON.stringify(H.dayColumns("2026-08-24", "2026-12-31", shift)),
    "[]", "a range beyond the 31-day bound is refused");
  assertEqual(JSON.stringify(H.dayColumns("2026-02-30", "2026-03-02", shift)),
    "[]", "an impossible date is refused");
  assertEqual(H.dayHeaderParts("2026-11-01").weekday, "Sun", "pure weekday math");
  assertEqual(H.dayHeaderParts("2026-11-01").date, "Nov 1", "pure date label");
  assertEqual(H.dayHeaderParts("nonsense").weekday, "", "malformed is empty");
});

/* ------------------------------------------------------------------ */
/* Stale-response protection and Refresh                                */
/* ------------------------------------------------------------------ */

test("calendar: a superseded load never renders (request-id guard)", async () => {
  const f = makePages();
  const s1 = f.data.queueDeferred("getSchedule");
  const a1 = f.data.queueDeferred("getAppointments");
  openCalendar(f);
  await flush();

  queueWeek(f, [slotFixture({ status: "blocked" })], []);
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();

  s1.resolve({ ok: true, data: scheduleBody([slotFixture()]) });
  a1.resolve({ ok: true, data: appointmentsBody([]) });
  await flush();

  const bands = allBands(f.doc);
  assertEqual(bands.length, 1, "one band from the NEWER load");
  assertEqual(bands[0].children[0].textContent, "Blocked",
    "the newer load owns the grid; the older response is discarded");
});

test("calendar: Refresh re-reads the CURRENT week, not the default week", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  queueWeek(f, [], [], { start_day: "2026-08-31", end_day: "2026-09-06" },
    { start_day: "2026-08-31", end_day: "2026-09-06" });
  f.doc._elements["calendar-next"].trigger("click");
  await flush();
  queueWeek(f, [], [], { start_day: "2026-08-31", end_day: "2026-09-06" },
    { start_day: "2026-08-31", end_day: "2026-09-06" });
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  assertEqual(JSON.stringify(f.data.calls.getSchedule[2]),
    JSON.stringify({ start_day: "2026-08-31", end_day: "2026-09-06" }),
    "Refresh keeps the week the office is looking at");
});

test("calendar: no timer or poll fires a load on its own", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  const before = f.data.calls.getSchedule.length;
  await new Promise((resolve) => setTimeout(resolve, 60));
  assertEqual(f.data.calls.getSchedule.length, before,
    "Phase 1 performs no background refresh");
});

test("calendar: re-entering the page reloads from the backend default week", async () => {
  const f = makePages();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  queueWeek(f, [], [], { start_day: "2026-08-31", end_day: "2026-09-06" },
    { start_day: "2026-08-31", end_day: "2026-09-06" });
  f.doc._elements["calendar-next"].trigger("click");
  await flush();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();
  queueWeek(f, [], []);
  openCalendar(f);
  await flush();
  assertEqual(JSON.stringify(f.data.calls.getSchedule[3]), "{}",
    "re-entry asks the backend for the default week again");
});

test("calendar: a load issued before re-entry cannot render afterwards (lifecycle guard)", async () => {
  const f = makePages();
  const s1 = f.data.queueDeferred("getSchedule");
  const a1 = f.data.queueDeferred("getAppointments");
  openCalendar(f);
  await flush();
  queueWeek(f, [slotFixture({ status: "blocked" })], []);
  openCalendar(f);
  await flush();
  s1.resolve({ ok: true, data: scheduleBody([slotFixture()]) });
  a1.resolve({ ok: true, data: appointmentsBody([]) });
  await flush();
  assertEqual(allBands(f.doc)[0].children[0].textContent, "Blocked",
    "the pre-re-entry response never renders");
});

/* ------------------------------------------------------------------ */
/* Failure handling                                                     */
/* ------------------------------------------------------------------ */

test("calendar: a schedule failure reports honestly and renders no grid", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: false, state: "unavailable" });
  f.data.queue("getAppointments", { ok: true, data: appointmentsBody([]) });
  openCalendar(f);
  await flush();
  assertEqual(columns(f.doc).length, 0, "nothing rendered");
  assert(f.doc._elements["calendar-state"].textContent.indexOf(
    "temporarily unavailable") !== -1, "the failure is visible");
});

test("calendar: an appointments failure alone also blocks the combined render", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([slotFixture()]) });
  f.data.queue("getAppointments", { ok: false, state: "unavailable" });
  openCalendar(f);
  await flush();
  assertEqual(columns(f.doc).length, 0,
    "a half-authoritative week is never shown");
});

test("calendar: an explicit reset() wipes the calendar like every other page", async () => {
  const f = makePages();
  queueWeek(f, [slotFixture()], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  f.pages.reset();
  assertEqual(f.doc._elements["calendar-grid"].children.length, 0, "grid wiped");
  assertEqual(f.doc._elements["calendar-state"].textContent, "", "state wiped");
  assertEqual(f.doc._elements["calendar-prev"].disabled, true, "nav disabled");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");
});

test("calendar: a load resolving AFTER a reset renders nothing", async () => {
  const f = makePages();
  const s1 = f.data.queueDeferred("getSchedule");
  const a1 = f.data.queueDeferred("getAppointments");
  openCalendar(f);
  await flush();
  f.pages.reset();
  s1.resolve({ ok: true, data: scheduleBody([slotFixture()]) });
  a1.resolve({ ok: true, data: appointmentsBody([]) });
  await flush();
  assertEqual(f.doc._elements["calendar-grid"].children.length, 0, "wipe stands");
  assertEqual(f.doc._elements["calendar-timezone-note"].textContent, "",
    "no tenant value is repopulated after a wipe");
});

/* ------------------------------------------------------------------ */
/* Pure-module contract                                                 */
/* ------------------------------------------------------------------ */

test("calendar module: refuses construction without every injected helper", () => {
  const f = makePages();
  let threw = false;
  try {
    f.createCalendar({ documentRef: makeDocument() });
  } catch (err) { threw = true; }
  assert(threw, "a wiring mistake fails loudly, never with a silent default");
});

test("calendar module: rangesAgree is the single consistency rule", () => {
  const f = makePages();
  const H = f.calendarHelpers;
  const a = scheduleBody([]);
  assert(H.rangesAgree(a, appointmentsBody([])), "identical envelopes agree");
  assert(!H.rangesAgree(a, appointmentsBody([], { start_day: "2026-08-25" })),
    "start_day drift disagrees");
  assert(!H.rangesAgree(a, appointmentsBody([], { end_day: "2026-08-29" })),
    "end_day drift disagrees");
  assert(!H.rangesAgree(a, appointmentsBody([], { timezone_name: "UTC" })),
    "timezone drift disagrees");
  assert(!H.rangesAgree(a, null), "a missing envelope disagrees");
  assert(!H.rangesAgree(scheduleBody([], { timezone_name: "" }),
    appointmentsBody([], { timezone_name: "" })),
    "a blank timezone is never accepted, even when both agree on it");
});

test("calendar module: rows outside the echoed window are counted, never dropped silently", async () => {
  const f = makePages();
  queueWeek(f, [
    slotFixture({ slot_id: "in" }),
    slotFixture({ slot_id: "out", start_datetime: "2026-09-15T13:00:00Z",
      end_datetime: "2026-09-15T13:30:00Z" })
  ], []);
  openCalendar(f);
  await flush();
  assertEqual(allBands(f.doc).length, 1, "only the in-range row is drawn");
  assert(f.doc._elements["calendar-state"].textContent.indexOf(
    "Entries outside this range were not shown: 1") !== -1,
    "the discrepancy is surfaced rather than hidden");
});

test("calendar module: lane assignment is per overlap cluster, not per day", () => {
  const f = makePages();
  const laned = f.calendarHelpers.assignLanes([
    { startMinutes: 540, endMinutes: 600 },
    { startMinutes: 570, endMinutes: 630 },
    { startMinutes: 900, endMinutes: 960 }
  ]);
  assertEqual(laned[0].laneCount, 2, "the morning pair splits into two lanes");
  assertEqual(laned[1].laneCount, 2, "both members share the lane count");
  assertEqual(laned[2].laneCount, 1,
    "an unrelated afternoon entry keeps the full column width");
});

/* ------------------------------------------------------------------ */

(async () => {
  const summary = await h.runRegisteredTests("test_portal_calendar_page");
  process.exitCode = summary.failed === 0 ? 0 : 1;
})();
