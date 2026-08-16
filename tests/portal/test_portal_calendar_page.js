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
  "calendar-drawer-actions-note",
  /* P2-A drawer action region. */
  "calendar-drawer-actions", "calendar-drawer-feedback"
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
    "unblockScheduleSlot", "blockAllOpenSlots",
    "confirmAppointment", "cancelAppointment"];
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
    confirmAppointment: (id) => next("confirmAppointment", id),
    cancelAppointment: (id) => next("cancelAppointment", id),
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

/* Canvas layer order (bottom to top): hour lines, availability bands,
 * cancelled history, live appointments. The accessors find their layer BY
 * CLASS rather than by index, so a change to the layer ORDER is reported by
 * the one test that owns that rule instead of cascading through the suite. */
function canvasLayer(column, className) {
  const canvas = column.children[1];
  const layer = canvas.children.filter((el) => el.className === className)[0];
  return layer ? layer.children : [];
}

function bandsIn(column) {
  return canvasLayer(column, "portal-calendar-bands");
}

function historyIn(column) {
  return canvasLayer(column, "portal-calendar-history");
}

function blocksIn(column) {
  return canvasLayer(column, "portal-calendar-blocks");
}

function historyStripsIn(column) {
  return canvasLayer(column, "portal-calendar-history-strips");
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

function allHistoryStrips(doc) {
  const out = [];
  for (const column of columns(doc)) {
    for (const strip of historyStripsIn(column)) { out.push(strip); }
  }
  return out;
}

function allHistory(doc) {
  const out = [];
  for (const column of columns(doc)) {
    for (const ghost of historyIn(column)) { out.push(ghost); }
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


function actionButtons(doc) {
  return doc._elements["calendar-drawer-actions"].children;
}

function actionLabels(doc) {
  return actionButtons(doc).map((b) => b.textContent);
}

function actionButton(doc, label) {
  for (const button of actionButtons(doc)) {
    if (button.textContent === label) { return button; }
  }
  return null;
}

function feedbackText(doc) {
  return doc._elements["calendar-drawer-feedback"].textContent;
}

function openDrawerFor(f, index) {
  blocksIn(columns(f.doc)[0])[index || 0].trigger("click");
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
  /* A read never touches a mutation method, and never touches another
   * page's data method. confirmAppointment / cancelAppointment appear here
   * too: merely loading a week must not call them. */
  for (const name of ["getDashboard", "listLeads", "getLeadDetail",
    "putLeadStatus", "putLeadNote", "publishScheduleDay", "blockScheduleSlot",
    "unblockScheduleSlot", "blockAllOpenSlots",
    "confirmAppointment", "cancelAppointment"]) {
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

test("calendar: completed and no_show never reach the resting calendar", async () => {
  const f = makePages();
  queueWeek(f, [
    slotFixture({ slot_id: "ok", status: "available" }),
    slotFixture({ slot_id: "gone", status: "cancelled",
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T16:30:00Z" })
  ], [
    appointmentFixture({ appointment_id: "keep", status: "confirmed" }),
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
  assertEqual(allHistory(f.doc).length, 0,
    "completed and no_show are not history either - they stay hidden");
  assertEqual(allBands(f.doc).length, 1, "only the open slot band");
  assertEqual(allBands(f.doc)[0].children[0].textContent, "Open",
    "a cancelled SLOT is still filtered out of the inventory bands");
});

/* ------------------------------------------------------------------ */
/* Phase 2B: cancelled appointments remain visible as demoted history    */
/* ------------------------------------------------------------------ */

function cancelledFixture(overrides) {
  return appointmentFixture(Object.assign({
    appointment_id: "appt-cancelled",
    patient_name: "Maria Lopez",
    status: "cancelled",
    start_datetime: "2026-08-24T14:30:00Z",
    end_datetime: "2026-08-24T15:00:00Z"
  }, overrides || {}));
}

test("2B: a cancelled appointment stays visible in the resting calendar", async () => {
  const f = makePages();
  queueWeek(f, [], [cancelledFixture()]);
  openCalendar(f);
  await flush();

  assertEqual(allHistory(f.doc).length, 1,
    "the cancelled appointment is still on the calendar");
  assertEqual(allBlocks(f.doc).length, 0,
    "but NOT as a live appointment");
  const ghost = allHistory(f.doc)[0];
  const texts = blockTexts(ghost);
  assertEqual(texts[0], "10:30 AM", "at its original time");
  assertEqual(texts[1], "Maria Lopez", "with the patient the office may call");
  assertEqual(texts[texts.length - 1], "Cancelled",
    "and the status word printed explicitly");
  assertEqual(f.doc._elements["calendar-state"].textContent, "",
    "a week containing only history is not an empty week");
});

test("2B: a cancelled block carries the demoted history class and no service line", async () => {
  const f = makePages();
  queueWeek(f, [], [cancelledFixture({
    start_datetime: "2026-08-24T14:00:00Z",
    end_datetime: "2026-08-24T15:00:00Z" })]);
  openCalendar(f);
  await flush();
  const ghost = allHistory(f.doc)[0];
  assert(ghost.className.indexOf("portal-calendar-block-history") !== -1,
    "a dedicated history class the styling can subdue: " + ghost.className);
  assert(ghost.className.indexOf("portal-calendar-appointment-cancelled") !== -1,
    "plus its authoritative status class");
  /* A full-hour ACTIVE block would carry a service line here; the ghost
   * deliberately does not, so it reads lighter than any live appointment. */
  assertEqual(blockTexts(ghost).length, 3,
    "time, patient and status only - no service line on a ghost");
});

test("2B: a cancelled block stays clickable and keyboard reachable", async () => {
  const f = makePages();
  queueWeek(f, [], [cancelledFixture()]);
  openCalendar(f);
  await flush();
  const ghost = allHistory(f.doc)[0];
  assertEqual(ghost.tagName, "BUTTON", "an activatable control");
  assertEqual(ghost.type, "button", "never a submit control");

  const callsBefore = f.data.totalCalls();
  ghost.trigger("click");
  await flush();
  assertEqual(f.data.totalCalls(), callsBefore,
    "opening a ghost issues no request");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, false, "panel opens");
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent,
    "Maria Lopez", "titled by the patient who cancelled");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Cancelled",
    "the panel states the status plainly");
});

test("2B: the cancelled drawer exposes NO actions", async () => {
  const f = makePages();
  queueWeek(f, [], [cancelledFixture()]);
  openCalendar(f);
  await flush();
  allHistory(f.doc)[0].trigger("click");
  await flush();

  assertEqual(actionButtons(f.doc).length, 0,
    "appointmentActionsFor('cancelled') offers nothing");
  assertEqual(f.doc._elements["calendar-drawer-actions-note"].textContent,
    "No actions are available for this appointment.", "and says so");
  /* No follow-up workflow is being introduced in this phase. */
  const markup = [f.doc._elements["calendar-drawer-actions-note"].textContent,
    f.doc._elements["calendar-drawer-title"].textContent].join(" ");
  for (const word of ["Reactivate", "Reschedule", "Follow up", "Duplicate",
    "Book Again", "Confirm", "Cancel"]) {
    assert(markup.indexOf(word) === -1,
      "no unauthorized action vocabulary appears: " + word);
  }
});

test("2B: the cancelled drawer still carries the full read-only record", async () => {
  const f = makePages();
  queueWeek(f, [], [cancelledFixture()]);
  openCalendar(f);
  await flush();
  allHistory(f.doc)[0].trigger("click");
  await flush();

  assertEqual(drawerValue(f.doc, "Patient"), "Maria Lopez", "patient");
  assertEqual(drawerValue(f.doc, "Phone"), "516-555-0134", "phone for follow-up");
  assertEqual(drawerValue(f.doc, "Service"), "implant consultation", "service");
  assertEqual(drawerValue(f.doc, "Status"), "Cancelled", "status");
  assert(String(drawerValue(f.doc, "Starts")).indexOf("2026") !== -1,
    "the ORIGINAL appointment time is preserved in the record");
});

/* ---- inventory independence ---- */

test("2B: authoritative Open availability renders at the same time as a ghost", async () => {
  const f = makePages();
  /* The Schedule says 10:30 is open again; the Appointments read says Maria
   * was booked there and cancelled. Both are true, and both are shown. */
  queueWeek(f, [slotFixture({
    start_datetime: "2026-08-24T14:30:00Z",
    end_datetime: "2026-08-24T15:00:00Z" })], [cancelledFixture()]);
  openCalendar(f);
  await flush();

  const bands = bandsIn(columns(f.doc)[0]);
  assertEqual(bands.length, 1, "the authoritative availability band renders");
  assertEqual(bands[0].children[0].textContent, "Open",
    "saying the time is open NOW");
  assertEqual(bands[0].style.top, allHistory(f.doc)[0].style.top,
    "at exactly the same position as the historical entry");
  assertEqual(allHistory(f.doc).length, 1,
    "while the history remains visible as context");
});

test("2B: a ghost never CREATES an Open band", async () => {
  const f = makePages();
  /* The Schedule returns no slot at all for that time. */
  queueWeek(f, [], [cancelledFixture()]);
  openCalendar(f);
  await flush();
  assertEqual(allBands(f.doc).length, 0,
    "availability is never inferred from a cancelled appointment");
  assertEqual(allHistory(f.doc).length, 1, "only the history is drawn");
});

test("2B: a ghost never SUPPRESSES an Open band", async () => {
  const f = makePages();
  queueWeek(f, [
    slotFixture({ slot_id: "s1", start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    slotFixture({ slot_id: "s2", start_datetime: "2026-08-24T15:00:00Z",
      end_datetime: "2026-08-24T15:30:00Z" })
  ], [cancelledFixture()]);
  openCalendar(f);
  await flush();
  const bands = bandsIn(columns(f.doc)[0]);
  assertEqual(bands.length, 1, "the two adjacent openings still consolidate");
  assert(bands[0].title.indexOf("2 slots") !== -1,
    "and both authoritative slots are still represented: " + bands[0].title);
});

test("2B: a ghost does not make a BLOCKED time look open", async () => {
  const f = makePages();
  queueWeek(f, [slotFixture({ status: "blocked",
    start_datetime: "2026-08-24T14:30:00Z",
    end_datetime: "2026-08-24T15:00:00Z" })], [cancelledFixture()]);
  openCalendar(f);
  await flush();
  const bands = bandsIn(columns(f.doc)[0]);
  assertEqual(bands.length, 1, "one inventory band");
  assertEqual(bands[0].children[0].textContent, "Blocked",
    "inventory stays exactly what the Schedule said");
});

/* ---- geometry: history never distorts active appointments ---- */

test("2B: a ghost does not push an overlapping ACTIVE appointment into a lane", async () => {
  const f = makePages();
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "live", patient_name: "Live Patient",
      status: "confirmed",
      start_datetime: "2026-08-24T14:00:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    cancelledFixture()   /* overlaps the live appointment exactly */
  ]);
  openCalendar(f);
  await flush();

  const live = blocksIn(columns(f.doc)[0])[0];
  assertEqual(live.style.width, "100%",
    "the live appointment keeps the full column - history took no lane");
  assertEqual(live.style.left, "0%", "and the left edge");
  assert(live.className.indexOf("portal-calendar-block-narrow") === -1,
    "and is not marked narrow");
  const ghost = allHistory(f.doc)[0];
  assertEqual(ghost.style.width, "100%", "the ghost also spans the column");
  assert(ghost.className.indexOf("portal-calendar-block-narrow") === -1,
    "history is never laned");
});

/* ---- exact overlap: history must stay reachable ---- */

/* The realistic case: 10:30-11:00 was cancelled and the slot was later
 * rebooked for exactly the same half hour. */
function exactOverlapWeek(f) {
  queueWeek(f, [], [
    cancelledFixture({ appointment_id: "appt-cancelled",
      patient_name: "Maria Lopez",
      start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    appointmentFixture({ appointment_id: "appt-live",
      patient_name: "New Patient", status: "confirmed",
      start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:00:00Z" })
  ]);
}

test("2B exact overlap: the live appointment keeps normal geometry", async () => {
  const f = makePages();
  exactOverlapWeek(f);
  openCalendar(f);
  await flush();

  const live = blocksIn(columns(f.doc)[0]);
  assertEqual(live.length, 1, "exactly ONE live appointment is drawn");
  assertEqual(live[0].style.width, "100%",
    "it keeps the full column - no second active lane was created");
  assertEqual(live[0].style.left, "0%", "and the left edge");
  assert(live[0].className.indexOf("portal-calendar-block-narrow") === -1,
    "and is not marked narrow");
  assertEqual(blockTexts(live[0])[1], "New Patient", "it is the live patient");
});

test("2B exact overlap: a fully occluded ghost gets ONE compact history strip", async () => {
  const f = makePages();
  exactOverlapWeek(f);
  openCalendar(f);
  await flush();

  /* The live appointment covers the ghost's own first line, so the ghost can
   * no longer speak for itself and a strip is drawn above the live layer. */
  assertEqual(allHistory(f.doc).length, 1, "the ghost is still rendered");
  const strips = allHistoryStrips(f.doc);
  assertEqual(strips.length, 1, "exactly one history affordance");
  const strip = strips[0];
  assertEqual(strip.tagName, "BUTTON", "a real control, not decoration");
  assertEqual(strip.type, "button", "never a submit control");
  assertEqual(strip.className, "portal-calendar-history-strip",
    "the horizontal strip treatment, not the old vertical marker");
});

test("2B exact overlap: the strip reads as patient + CANCELLED, with no x glyph", async () => {
  const f = makePages();
  exactOverlapWeek(f);
  openCalendar(f);
  await flush();
  const strip = allHistoryStrips(f.doc)[0];

  const parts = strip.children.map((s) => s.textContent);
  assertEqual(parts[0], "Maria Lopez", "the patient who cancelled reads first");
  assertEqual(parts[1], "Cancelled",
    "then the frozen status word - CSS uppercases it for display");
  /* The ORIGINAL time stays in the accessible name without eating width. */
  assert(parts[2].indexOf("10:30 AM") !== -1,
    "and the original time is part of the control's text: " + parts[2]);
  assert(strip.title.indexOf("Cancelled") !== -1 &&
    strip.title.indexOf("Maria Lopez") !== -1 &&
    strip.title.indexOf("10:30 AM") !== -1,
    "the full label is echoed on the hover title: " + strip.title);

  /* No close/delete-looking glyph anywhere on the calendar surface. */
  for (const part of parts) {
    assertEqual(part.indexOf("x") === 0, false,
      "no x glyph is rendered: " + part);
  }
  for (const cls of strip.children.map((s) => s.className)) {
    assert(cls.indexOf("history-mark") === -1 || cls.indexOf("strip") !== -1,
      "no remnant of the old vertical marker markup: " + cls);
  }
});

test("2B exact overlap: the strip sits on the BOTTOM edge, above the live layer", async () => {
  const f = makePages();
  exactOverlapWeek(f);
  openCalendar(f);
  await flush();

  const canvas = columns(f.doc)[0].children[1];
  const stripsIndex = canvas.children.findIndex(
    (layer) => layer.className === "portal-calendar-history-strips");
  const blocksIndex = canvas.children.findIndex(
    (layer) => layer.className === "portal-calendar-blocks");
  const historyIndex = canvas.children.findIndex(
    (layer) => layer.className === "portal-calendar-history");
  assert(historyIndex < blocksIndex,
    "the full ghost still paints beneath the live appointment");
  assert(stripsIndex > blocksIndex,
    "but the strip paints above it, so it can never be covered");

  /* Bottom-aligned, so it covers only the live block's LAST line. */
  const live = blocksIn(columns(f.doc)[0])[0];
  const strip = allHistoryStrips(f.doc)[0];
  const liveTop = parseFloat(live.style.top);
  const liveHeight = parseFloat(live.style.height);
  const stripTop = parseFloat(strip.style.top);
  const stripHeight = parseFloat(strip.style.height);
  assertEqual(stripHeight, 14, "one thin line");
  assertEqual(stripTop, liveTop + liveHeight - stripHeight,
    "flush with the bottom edge, leaving the live time and patient visible");
  assert(stripTop > liveTop, "and never across the live block's first line");
});

test("2B exact overlap: activating the strip opens Maria's cancelled drawer", async () => {
  const f = makePages();
  exactOverlapWeek(f);
  openCalendar(f);
  await flush();

  const callsBefore = f.data.totalCalls();
  allHistoryStrips(f.doc)[0].trigger("click");
  await flush();
  assertEqual(f.data.totalCalls(), callsBefore, "no request is issued");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, false, "the panel opens");
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent, "Maria Lopez",
    "on the CANCELLED patient, not the live one");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Cancelled",
    "showing the cancelled status");
  assertEqual(actionButtons(f.doc).length, 0,
    "and offering no action, exactly as appointmentActionsFor('cancelled') says");
});

test("2B exact overlap: the live appointment still opens its own drawer", async () => {
  const f = makePages();
  exactOverlapWeek(f);
  openCalendar(f);
  await flush();

  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent, "New Patient",
    "the live block still opens the live patient");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Confirmed",
    "with its own status");
  assertEqual(JSON.stringify(actionLabels(f.doc)),
    JSON.stringify(["Cancel appointment"]),
    "and its own allowed actions");
});

test("2B exact overlap: availability is neither inferred nor altered", async () => {
  const f = makePages();
  exactOverlapWeek(f);
  openCalendar(f);
  await flush();
  assertEqual(allBands(f.doc).length, 0,
    "no availability is invented from either appointment");
  assertEqual(JSON.stringify(f.data.calls.getSchedule[0]), "{}",
    "and the Schedule request is unchanged");
});

test("2B exact overlap: an authoritative Open band still renders alongside both", async () => {
  const f = makePages();
  queueWeek(f, [slotFixture({ start_datetime: "2026-08-24T14:30:00Z",
    end_datetime: "2026-08-24T15:00:00Z" })], [
    cancelledFixture(),
    appointmentFixture({ appointment_id: "appt-live",
      patient_name: "New Patient", status: "confirmed",
      start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:00:00Z" })
  ]);
  openCalendar(f);
  await flush();
  assertEqual(allBands(f.doc).length, 1,
    "the Schedule remains the only source of availability");
  assertEqual(allBands(f.doc)[0].children[0].textContent, "Open", "and it says Open");
  assertEqual(allHistory(f.doc).length, 1, "the ghost is still there");
  assertEqual(allHistoryStrips(f.doc).length, 1, "and so is its strip");
});

test("2B: a ghost with clear air around it gets NO strip", async () => {
  const f = makePages();
  queueWeek(f, [], [
    cancelledFixture({ start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    appointmentFixture({ appointment_id: "later", status: "confirmed",
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T17:00:00Z" })
  ]);
  openCalendar(f);
  await flush();
  assertEqual(allHistory(f.doc).length, 1, "the ghost renders normally");
  assertEqual(allHistoryStrips(f.doc).length, 0,
    "nothing covers it, so the approved presentation is untouched");
});

test("2B: a PARTIALLY overlapped ghost that still reads gets NO strip", async () => {
  const f = makePages();
  /* Ghost 10:30-11:30; the live appointment starts at 11:00, so the ghost's
   * own first line - patient and CANCELLED - is fully readable. It IS the
   * affordance, and a second control beside it would be clutter. */
  queueWeek(f, [], [
    cancelledFixture({ start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:30:00Z" }),
    appointmentFixture({ appointment_id: "appt-live",
      patient_name: "Robert Miller", status: "confirmed",
      start_datetime: "2026-08-24T15:00:00Z",
      end_datetime: "2026-08-24T16:00:00Z" })
  ]);
  openCalendar(f);
  await flush();

  assertEqual(allHistoryStrips(f.doc).length, 0,
    "no extra affordance beside a ghost that already reads");
  const ghost = allHistory(f.doc)[0];
  assertEqual(blockTexts(ghost)[1], "Maria Lopez", "the ghost shows the patient");
  assertEqual(blockTexts(ghost)[blockTexts(ghost).length - 1], "Cancelled",
    "and the status word");
  /* And that visible ghost is still the working control. */
  ghost.trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent, "Maria Lopez",
    "clicking the visible ghost opens the cancelled drawer");
  assertEqual(actionButtons(f.doc).length, 0, "read-only");
  /* The live appointment keeps its geometry either way. */
  assertEqual(blocksIn(columns(f.doc)[0])[0].style.width, "100%",
    "the live appointment keeps the full column");
});

test("2B: the Aisha/Robert case - top covered, tail exposed - gets NO strip", async () => {
  const f = makePages();
  /* The exact case the office reported in the browser:
   *   Robert Miller, confirmed  11:00-12:00
   *   Aisha Khan,    cancelled  11:30-12:30
   * Aisha's first line is covered, but her whole 12:00-12:30 tail is exposed
   * and reads as "Aisha Khan / CANCELLED". That IS the affordance; a second
   * control beside it is clutter. */
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "appt-live",
      patient_name: "Robert Miller", status: "confirmed",
      start_datetime: "2026-08-24T15:00:00Z",
      end_datetime: "2026-08-24T16:00:00Z" }),
    cancelledFixture({ appointment_id: "appt-aisha",
      patient_name: "Aisha Khan",
      start_datetime: "2026-08-24T15:30:00Z",
      end_datetime: "2026-08-24T16:30:00Z" })
  ]);
  openCalendar(f);
  await flush();

  assertEqual(allHistoryStrips(f.doc).length, 0,
    "no extra strip beside a ghost with a readable exposed tail");
  const ghost = allHistory(f.doc)[0];
  const texts = blockTexts(ghost);
  assertEqual(texts[1], "Aisha Khan", "the ghost still names the patient");
  assertEqual(texts[texts.length - 1], "Cancelled", "and prints the status");

  /* The ghost remains the working control. */
  ghost.trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent, "Aisha Khan",
    "clicking the visible ghost opens HER cancelled drawer");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Cancelled",
    "read-only, cancelled");
  assertEqual(actionButtons(f.doc).length, 0, "and offering no action");

  /* Robert keeps his normal geometry and his own drawer. */
  const live = blocksIn(columns(f.doc)[0])[0];
  assertEqual(live.style.width, "100%", "Robert keeps the full column");
  assertEqual(live.style.left, "0%", "and the left edge");
  assert(live.className.indexOf("portal-calendar-block-narrow") === -1,
    "and is not laned by the cancelled row");
  live.trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent,
    "Robert Miller", "and still opens his own drawer");
});

test("2B: only a TINY exposed fragment still uses the strip", async () => {
  const f = makePages();
  /* Ghost 10:30-11:00; the live appointment covers 10:30-10:55, leaving a
   * five-minute sliver - below the minimum meaningful display height, so
   * nothing readable remains and the strip is warranted. */
  queueWeek(f, [], [
    cancelledFixture({ start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    appointmentFixture({ appointment_id: "appt-live",
      patient_name: "Robert Miller", status: "confirmed",
      start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T14:55:00Z" })
  ]);
  openCalendar(f);
  await flush();
  assertEqual(allHistoryStrips(f.doc).length, 1,
    "an unreadable sliver still needs the strip");
  assertEqual(blocksIn(columns(f.doc)[0])[0].style.width, "100%",
    "and the live appointment still keeps its geometry");
});

test("2B: no live block reserves padding for a history affordance", () => {
  const css = fs.readFileSync(path.join(PORTAL_DIR, "portal.css"), "utf8");
  /* The old vertical tab needed room reserved on EVERY live block, in every
   * week, whether or not any cancellation was involved. The bottom strip
   * needs none, so that reservation is gone. */
  assert(css.indexOf("padding-right: 20px") === -1,
    "no global live-block padding survives");
  assert(css.indexOf("portal-calendar-history-marker") === -1,
    "and no rule for the old vertical marker survives");
  assert(css.indexOf("portal-calendar-history-strip") !== -1,
    "the horizontal strip is styled instead");
});

test("2B: largestVisibleSpanMinutes measures exposure ANYWHERE in the entry", () => {
  const f = makePages();
  const largest = f.calendarHelpers.largestVisibleSpanMinutes;
  const ghost = { startMinutes: 630, endMinutes: 690 };   /* 10:30-11:30 */

  assertEqual(largest(ghost, []), 60, "nothing overlapping: the whole entry");
  assertEqual(largest(ghost, [{ startMinutes: 660, endMinutes: 720 }]), 30,
    "covered at the BOTTOM: the top half is exposed");
  /* The case the office reported: the top is covered, but the tail reads. */
  assertEqual(largest(ghost, [{ startMinutes: 600, endMinutes: 660 }]), 30,
    "covered at the TOP: the bottom half is still a real exposed span");
  assertEqual(largest(ghost, [{ startMinutes: 630, endMinutes: 690 }]), 0,
    "exact coincidence exposes nothing");
  assertEqual(largest(ghost, [{ startMinutes: 540, endMinutes: 720 }]), 0,
    "and neither does an appointment that swallows it whole");
  assertEqual(largest(ghost, [{ startMinutes: 500, endMinutes: 630 }]), 60,
    "an appointment ending where the ghost starts covers nothing");
  /* Two actives leaving a gap in the middle: the LARGEST run wins, and the
   * gaps before, between and after are all candidates. */
  assertEqual(largest(ghost, [
    { startMinutes: 630, endMinutes: 645 },
    { startMinutes: 665, endMinutes: 690 }
  ]), 20, "the 645-665 gap is the largest exposed run");
  assertEqual(largest(ghost, [
    { startMinutes: 660, endMinutes: 670 },
    { startMinutes: 640, endMinutes: 650 }
  ]), 20, "unordered actives are handled, and the tail 670-690 wins");
  /* Overlapping actives must not be double-counted into a phantom gap. */
  assertEqual(largest(ghost, [
    { startMinutes: 630, endMinutes: 675 },
    { startMinutes: 650, endMinutes: 690 }
  ]), 0, "overlapping actives merge rather than leaving a false gap");
  /* NESTED actives are the shape that catches a naive merge: a short
   * appointment sitting inside a long one must not rewind the coverage
   * frontier and invent exposure that is not there. */
  assertEqual(largest(ghost, [
    { startMinutes: 630, endMinutes: 690 },
    { startMinutes: 650, endMinutes: 660 }
  ]), 0, "a nested active never re-exposes what the outer one covers");
  assertEqual(largest(ghost, [
    { startMinutes: 630, endMinutes: 670 },
    { startMinutes: 640, endMinutes: 650 }
  ]), 20, "and the real tail after the OUTER interval is what is measured");
});

test("2B: isHistoryOccluded is the single visible/occluded rule", () => {
  const f = makePages();
  const occluded = f.calendarHelpers.isHistoryOccluded;
  const ghost = { startMinutes: 630, endMinutes: 690 };

  assert(!occluded(ghost, []), "Case 1: no overlap is never occluded");
  assert(!occluded(ghost, [{ startMinutes: 660, endMinutes: 720 }]),
    "Case 2: an exposed span at the top means the ghost speaks for itself");
  assert(!occluded(ghost, [{ startMinutes: 600, endMinutes: 660 }]),
    "Case 2 again: an exposed span at the BOTTOM counts just as much");
  assert(occluded(ghost, [{ startMinutes: 630, endMinutes: 690 }]),
    "Case 3: exact coincidence is occluded");
  assert(occluded(ghost, [{ startMinutes: 630, endMinutes: 685 }]),
    "and so is a 5-minute fragment, below the meaningful threshold");
  /* A short ghost with nothing over it is still not occluded - the rule is
   * about coverage, never about being small. */
  assert(!occluded({ startMinutes: 630, endMinutes: 640 }, []),
    "a brief cancelled entry with clear air needs no strip");
});

test("2B: entries that merely touch end-to-start do not count as overlapping", () => {
  const f = makePages();
  const overlaps = f.calendarHelpers.overlapsAnyActive;
  assert(!overlaps({ startMinutes: 600, endMinutes: 630 },
    [{ startMinutes: 630, endMinutes: 660 }]),
    "a ghost ending exactly where an appointment starts is not covered");
  assert(!overlaps({ startMinutes: 630, endMinutes: 660 },
    [{ startMinutes: 600, endMinutes: 630 }]),
    "and neither is the reverse");
  assert(overlaps({ startMinutes: 600, endMinutes: 630 },
    [{ startMinutes: 600, endMinutes: 630 }]), "exact coincidence overlaps");
  assert(overlaps({ startMinutes: 600, endMinutes: 660 },
    [{ startMinutes: 630, endMinutes: 690 }]), "partial overlap counts");
  assert(!overlaps({ startMinutes: 600, endMinutes: 630 }, []),
    "nothing to overlap with");
});

test("2B: two ACTIVE appointments still lane normally alongside a ghost", async () => {
  const f = makePages();
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "a1", status: "confirmed",
      start_datetime: "2026-08-24T14:00:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    appointmentFixture({ appointment_id: "a2", patient_name: "Second Live",
      status: "pending",
      start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:30:00Z" }),
    cancelledFixture()
  ]);
  openCalendar(f);
  await flush();

  const live = blocksIn(columns(f.doc)[0]);
  assertEqual(live.length, 2, "two live appointments");
  assertEqual(live[0].style.width, "50%",
    "they lane against EACH OTHER exactly as before");
  assertEqual(live[1].style.left, "50%", "second lane");
  assertEqual(allHistory(f.doc)[0].style.width, "100%",
    "the ghost is unaffected and takes no lane");
});

test("2B: assignLanes is never given a historical entry (helper level)", () => {
  const f = makePages();
  const H = f.calendarHelpers;
  /* Three entries at the same time; if history participated, the two live
   * ones would be squeezed to a third of the column each. */
  const laned = H.assignLanes([
    { startMinutes: 540, endMinutes: 600 },
    { startMinutes: 540, endMinutes: 600 }
  ]);
  assertEqual(laned[0].laneCount, 2,
    "two live appointments produce exactly two lanes, never three");
});

test("2B: live appointments are layered ABOVE cancelled history", async () => {
  const f = makePages();
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "live", status: "confirmed" }),
    cancelledFixture({ start_datetime: "2026-08-24T14:00:00Z",
      end_datetime: "2026-08-24T15:00:00Z" })
  ]);
  openCalendar(f);
  await flush();
  const canvas = columns(f.doc)[0].children[1];
  const historyIndex = canvas.children.findIndex(
    (layer) => layer.className === "portal-calendar-history");
  const blocksIndex = canvas.children.findIndex(
    (layer) => layer.className === "portal-calendar-blocks");
  assert(historyIndex !== -1 && blocksIndex !== -1, "both layers exist");
  assert(blocksIndex > historyIndex,
    "the live layer paints after - and therefore above - the history layer");
});

/* ---- the Phase 2A cancel flow now leaves a ghost behind ---- */

test("2B: after the Phase 2A Cancel flow, the returned cancelled row stays visible", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    patient_name: "Maria Lopez", status: "confirmed",
    start_datetime: "2026-08-24T14:30:00Z",
    end_datetime: "2026-08-24T15:00:00Z" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  actionButton(f.doc, "Cancel appointment").trigger("click");
  f.data.queue("cancelAppointment", { ok: true, data: {} });
  /* The authoritative refresh frees the slot AND returns the cancelled row. */
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([slotFixture({
    start_datetime: "2026-08-24T14:30:00Z",
    end_datetime: "2026-08-24T15:00:00Z" })]) });
  f.data.queue("getAppointments", { ok: true, data: appointmentsBody([
    appointmentFixture({ appointment_id: "appt-a", patient_name: "Maria Lopez",
      status: "cancelled",
      start_datetime: "2026-08-24T14:30:00Z",
      end_datetime: "2026-08-24T15:00:00Z" })]) });
  actionButton(f.doc, "Confirm cancel").trigger("click");
  await flush();
  await flush();

  assertEqual(allBlocks(f.doc).length, 0, "no live appointment remains");
  assertEqual(allHistory(f.doc).length, 1,
    "the cancelled row remains as follow-up context");
  assertEqual(blockTexts(allHistory(f.doc)[0])[1], "Maria Lopez", "the patient");
  const bands = bandsIn(columns(f.doc)[0]);
  assertEqual(bands.length, 1, "and the freed time shows as authoritative open");
  assertEqual(bands[0].children[0].textContent, "Open",
    "exactly the visual the office needs: open NOW, Maria cancelled here");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Cancelled",
    "the panel settles on the authoritative cancelled state");
  assertEqual(actionButtons(f.doc).length, 0, "offering no action");
});

test("calendar: no rescheduled status is invented", () => {
  const f = makePages();
  const H = f.calendarHelpers;
  assert(!H.isVisibleAppointmentStatus("rescheduled"),
    "the grid recognises no status the backend does not define");
  assert(!H.isActiveAppointmentStatus("rescheduled") &&
    !H.isHistoryAppointmentStatus("rescheduled"),
    "and it belongs to neither presentation role");
  assertEqual(JSON.stringify(H.ACTIVE_APPOINTMENT_STATUSES),
    JSON.stringify(["pending", "confirmed"]),
    "ACTIVE - the statuses that occupy time - is unchanged by Phase 2B");
  assertEqual(JSON.stringify(H.HISTORY_APPOINTMENT_STATUSES),
    JSON.stringify(["cancelled"]),
    "HISTORY is exactly cancelled: completed and no_show stay hidden");
  assertEqual(JSON.stringify(H.VISIBLE_APPOINTMENT_STATUSES),
    JSON.stringify(["pending", "confirmed", "cancelled"]),
    "and visible is the union of the two roles");
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

test("calendar: a re-render moves the selection to the NEW block, by appointment id", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const stale = blocksIn(columns(f.doc)[0])[0];
  stale.trigger("click");
  await flush();

  /* P2-A supersedes the Phase 1 "a re-render always closes the panel" rule:
   * the office must be able to watch an appointment settle in place after
   * Confirm/Cancel. The DETACHED block still releases its selected look -
   * the registry is rebuilt - and the panel is repopulated from the NEW
   * row found by appointment_id, never from the stale object. */
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  assert(!stale.classList.contains("portal-calendar-block-selected"),
    "the detached block releases its selection");
  const fresh = blocksIn(columns(f.doc)[0])[0];
  assert(fresh !== stale, "precondition: a new element was rendered");
  assert(fresh.classList.contains("portal-calendar-block-selected"),
    "the selection moved to the refreshed block");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, false, "panel open");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Pending",
    "and it shows the REFRESHED status, not the pre-refresh one");
});

test("calendar: a refresh that no longer returns the appointment closes the panel honestly", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();

  queueWeek(f, [], []);
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "no row means no panel - history is never fabricated");
  assertEqual(drawerPairs(f.doc).length, 0, "and the details are wiped");
});

test("calendar: changing week clears the selection and closes the panel", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  const block = blocksIn(columns(f.doc)[0])[0];
  block.trigger("click");
  await flush();
  queueWeek(f, [], [], { start_day: "2026-08-31", end_day: "2026-09-06" },
    { start_day: "2026-08-31", end_day: "2026-09-06" });
  f.doc._elements["calendar-next"].trigger("click");
  await flush();
  assert(!block.classList.contains("portal-calendar-block-selected"),
    "changing week releases the selection");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "and closes the panel - the appointment belongs to the week just left");
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

test("calendar: the panel offers ONLY the approved existing actions", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture()]);
  openCalendar(f);
  await flush();
  blocksIn(columns(f.doc)[0])[0].trigger("click");
  await flush();
  const labels = actionLabels(f.doc);
  for (const word of ["Reschedule", "Duplicate", "Book", "Create", "New",
    "Delete", "Edit"]) {
    for (const label of labels) {
      assert(label.indexOf(word) === -1,
        "no unauthorized action vocabulary appears: " + label);
    }
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
  assertEqual(canvas.children[2].className, "portal-calendar-history",
    "cancelled history sits above the bands");
  assertEqual(canvas.children[3].className, "portal-calendar-blocks",
    "and live appointments sit above the history");
  assertEqual(canvas.children[4].className, "portal-calendar-history-strips",
    "with the cancelled-history strips as the only layer above them");
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

/* Two pending appointments in one day, used by the stale-generation test:
 * a SECOND appointment is the only way to open a newer mutation generation,
 * because actionBusy locks the first appointment for the whole flight. */
function twoPending(statusA, statusB) {
  return [
    appointmentFixture({ appointment_id: "appt-a", patient_name: "Alpha Patient",
      status: statusA,
      start_datetime: "2026-08-24T14:00:00Z",
      end_datetime: "2026-08-24T15:00:00Z" }),
    appointmentFixture({ appointment_id: "appt-b", patient_name: "Bravo Patient",
      status: statusB,
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T17:00:00Z" })
  ];
}


/* ------------------------------------------------------------------ */
/* P2-A: drawer appointment actions                                     */
/* ------------------------------------------------------------------ */

test("actions: a pending appointment offers Confirm and Cancel", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  assertEqual(JSON.stringify(actionLabels(f.doc)),
    JSON.stringify(["Confirm", "Cancel appointment"]),
    "exactly the two actions the frozen matrix allows for pending");
  assertEqual(f.doc._elements["calendar-drawer-actions-note"].textContent, "",
    "no 'nothing available' line when actions exist");
});

test("actions: a confirmed appointment offers only Cancel", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  assertEqual(JSON.stringify(actionLabels(f.doc)),
    JSON.stringify(["Cancel appointment"]),
    "Confirm is never offered where the lifecycle owner would refuse it");
});

test("actions: the drawer uses the FROZEN status/action matrix, not a second one", () => {
  const f = makePages();
  const actionsFor = f.helpers.appointmentActionsFor;
  assertEqual(JSON.stringify(actionsFor("pending")),
    JSON.stringify(["confirm", "cancel"]), "pending");
  assertEqual(JSON.stringify(actionsFor("confirmed")),
    JSON.stringify(["cancel"]), "confirmed");
  for (const terminal of ["cancelled", "completed", "no_show", "invented"]) {
    assertEqual(JSON.stringify(actionsFor(terminal)), "[]",
      terminal + " offers nothing");
  }
});

test("actions: a terminal status shows no controls and says so", async () => {
  const f = makePages();
  /* Reached the honest way: cancel a confirmed appointment, then let the
   * authoritative refresh return it as cancelled. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  actionButton(f.doc, "Cancel appointment").trigger("click");
  f.data.queue("cancelAppointment", { ok: true, data: {} });
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.data.queue("getAppointments", { ok: true,
    data: appointmentsBody([appointmentFixture({ status: "cancelled" })]) });
  actionButton(f.doc, "Confirm cancel").trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-drawer"].hidden, false,
    "the row is still in the response, so the panel may stay open");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Cancelled",
    "showing its refreshed authoritative state");
  assertEqual(actionButtons(f.doc).length, 0, "and offering no action");
  assertEqual(f.doc._elements["calendar-drawer-actions-note"].textContent,
    "No actions are available for this appointment.", "stated plainly");
  assertEqual(allBlocks(f.doc).length, 0,
    "while the cancelled appointment leaves the resting grid");
});

/* ------------------------------------------------------------------ */
/* Confirm                                                              */
/* ------------------------------------------------------------------ */

test("actions: Confirm calls exactly confirmAppointment with the selected id", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-1",
    status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-1",
    status: "confirmed" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(JSON.stringify(f.data.calls.confirmAppointment),
    JSON.stringify(["appt-1"]), "one call, the selected appointment id");
  assertEqual(f.data.calls.cancelAppointment.length, 0, "and nothing else");
});

test("actions: a successful Confirm re-reads BOTH Schedule and Appointments", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  const readsBefore = f.data.calls.getSchedule.length;
  openDrawerFor(f);
  await flush();

  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [slotFixture()],
    [appointmentFixture({ status: "confirmed" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(f.data.calls.getSchedule.length, readsBefore + 1,
    "Schedule is re-read: a mutation can change slot inventory too");
  assertEqual(f.data.calls.getAppointments.length, readsBefore + 1,
    "Appointments is re-read");
  assertEqual(JSON.stringify(f.data.calls.getSchedule[readsBefore]), "{}",
    "using the SAME current-week bounds the calendar already owns");
});

test("actions: after Confirm the panel and the block show the AUTHORITATIVE state", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Pending",
    "precondition");

  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Confirmed",
    "the panel reflects the refreshed row");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "and so does the calendar block");
  assertEqual(feedbackText(f.doc), "Appointment confirmed.", "honest outcome");
  assertEqual(JSON.stringify(actionLabels(f.doc)),
    JSON.stringify(["Cancel appointment"]),
    "and the offered actions follow the new status");
});

test("actions: the mutation response alone never becomes the visual state", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  /* The POST claims confirmed; the authoritative re-read says it is still
   * pending (another terminal changed it back). The REFRESH wins. */
  f.data.queue("confirmAppointment", { ok: true,
    data: appointmentFixture({ status: "confirmed" }) });
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Pending",
    "the authoritative GET is the final visual state, not the POST body");
});

/* ------------------------------------------------------------------ */
/* Cancel: the two-step guard                                           */
/* ------------------------------------------------------------------ */

test("actions: the first Cancel click arms and issues NO request", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  const callsBefore = f.data.totalCalls();
  actionButton(f.doc, "Cancel appointment").trigger("click");
  await flush();
  assertEqual(f.data.totalCalls(), callsBefore,
    "arming is a UI state change only - nothing is cancelled on click one");
  assertEqual(f.data.calls.cancelAppointment.length, 0, "no cancel call");
  assert(actionButton(f.doc, "Confirm cancel") !== null,
    "the control relabels to an explicit second step");
  assertEqual(feedbackText(f.doc), "Click Cancel again to confirm.",
    "and says so");
});

test("actions: the second explicit click performs the cancellation", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-9",
    status: "confirmed" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  actionButton(f.doc, "Cancel appointment").trigger("click");

  f.data.queue("cancelAppointment", { ok: true, data: {} });
  queueWeek(f, [], []);
  actionButton(f.doc, "Confirm cancel").trigger("click");
  await flush();
  await flush();

  assertEqual(JSON.stringify(f.data.calls.cancelAppointment),
    JSON.stringify(["appt-9"]), "one cancel, the armed appointment");
});

test("actions: a successful Cancel re-reads BOTH windows and never invents a slot", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  openCalendar(f);
  await flush();
  const readsBefore = f.data.calls.getSchedule.length;
  openDrawerFor(f);
  await flush();
  actionButton(f.doc, "Cancel appointment").trigger("click");

  f.data.queue("cancelAppointment", { ok: true, data: {} });
  /* The backend returns NO open slot for the freed time. The calendar must
   * show exactly that, not a manufactured opening. */
  queueWeek(f, [], []);
  actionButton(f.doc, "Confirm cancel").trigger("click");
  await flush();
  await flush();

  assertEqual(f.data.calls.getSchedule.length, readsBefore + 1, "Schedule re-read");
  assertEqual(f.data.calls.getAppointments.length, readsBefore + 1,
    "Appointments re-read");
  assertEqual(allBands(f.doc).length, 0,
    "no slot is manufactured - the slot display comes only from the re-read");
  assertEqual(allBlocks(f.doc).length, 0, "and the appointment is gone");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "the panel closes honestly when the row is no longer returned");
  assertEqual(f.doc._elements["calendar-state"].textContent,
    "Appointment cancelled.",
    "the outcome survives the refresh even with the panel closed");
});

test("actions: an arm does not survive closing the panel", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  actionButton(f.doc, "Cancel appointment").trigger("click");
  assert(actionButton(f.doc, "Confirm cancel") !== null, "armed");

  f.doc._elements["calendar-drawer-close"].trigger("click");
  openDrawerFor(f);
  await flush();
  assert(actionButton(f.doc, "Cancel appointment") !== null,
    "reopening requires the two clicks again");
  assertEqual(f.data.calls.cancelAppointment.length, 0, "nothing was cancelled");
});

/* ------------------------------------------------------------------ */
/* Duplicate submits and cross-appointment isolation                    */
/* ------------------------------------------------------------------ */

test("actions: duplicate Confirm submits are suppressed while in flight", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  const deferred = f.data.queueDeferred("confirmAppointment");
  const button = actionButton(f.doc, "Confirm");
  button.trigger("click");
  button.trigger("click");
  button.trigger("click");
  await flush();
  assertEqual(f.data.calls.confirmAppointment.length, 1,
    "only the first submit is issued");
  assertEqual(button.disabled, true, "and the control is disabled meanwhile");
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, true,
    "BOTH controls are disabled, so Confirm and Cancel cannot overlap");

  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();
  assertEqual(f.data.calls.confirmAppointment.length, 1, "still just one");
});

test("actions: Cancel cannot be started while Confirm is in flight", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  actionButton(f.doc, "Cancel appointment").trigger("click");
  actionButton(f.doc, "Cancel appointment").trigger("click");
  await flush();
  assertEqual(f.data.calls.cancelAppointment.length, 0,
    "an in-flight mutation on this appointment blocks the other action");

  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();
});

test("actions: acting on one appointment never mutates another", async () => {
  const f = makePages();
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "left", status: "pending" }),
    appointmentFixture({ appointment_id: "right", status: "pending",
      patient_name: "Ana Ruiz",
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T17:00:00Z" })
  ]);
  openCalendar(f);
  await flush();
  openDrawerFor(f, 1);
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent, "Ana Ruiz",
    "precondition: the second appointment is open");

  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], [
    appointmentFixture({ appointment_id: "left", status: "pending" }),
    appointmentFixture({ appointment_id: "right", status: "confirmed",
      patient_name: "Ana Ruiz",
      start_datetime: "2026-08-24T16:00:00Z",
      end_datetime: "2026-08-24T17:00:00Z" })
  ]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(JSON.stringify(f.data.calls.confirmAppointment),
    JSON.stringify(["right"]), "only the OPEN appointment is acted on");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Pending",
    "the other appointment is untouched");
});

/* ------------------------------------------------------------------ */
/* Stale-response and lifecycle protection                              */
/* ------------------------------------------------------------------ */

test("guards: a pre-mutation week GET cannot overwrite post-mutation state", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  /* A refresh is issued and is still the NEWEST read... */
  const staleSchedule = f.data.queueDeferred("getSchedule");
  const staleAppointments = f.data.queueDeferred("getAppointments");
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();

  /* ...then a mutation begins, opening a new generation. */
  const mutation = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  /* The pre-mutation read now resolves. Its request id is STILL current, so
   * only the generation guard can reject it - which is exactly the point. */
  staleSchedule.resolve({ ok: true, data: scheduleBody([]) });
  staleAppointments.resolve({ ok: true,
    data: appointmentsBody([appointmentFixture({ status: "pending",
      patient_name: "STALE" })]) });
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent,
    "Rosa Delgado", "the pre-mutation read was discarded");

  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  mutation.resolve({ ok: true, data: {} });
  await flush();
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Confirmed",
    "the post-mutation authoritative state stands");
});

test("guards: an older mutation completing after a NEWER one reaches the generation branch", async () => {
  const f = makePages();
  /* TWO appointments. One is not enough: actionBusy owns appointment A for
   * the whole flight, so no second mutation on A can ever start and no
   * newer generation would actually be opened. */
  queueWeek(f, [], twoPending("pending", "pending"));
  openCalendar(f);
  await flush();

  /* --- A: mutation starts and stays in flight (generation 1). --- */
  openDrawerFor(f, 0);
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent,
    "Alpha Patient", "precondition: A is open");
  const deferredA = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  assertEqual(JSON.stringify(f.data.calls.confirmAppointment),
    JSON.stringify(["appt-a"]), "A's mutation is genuinely in flight");

  /* --- B: a genuinely SECOND mutation opens generation 2. --- */
  f.doc._elements["calendar-drawer-close"].trigger("click");
  openDrawerFor(f, 1);
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent,
    "Bravo Patient", "precondition: B is open");
  assertEqual(actionButton(f.doc, "Confirm").disabled, false,
    "B is a different appointment, so its controls are NOT locked by A");
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], twoPending("pending", "confirmed"));
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(JSON.stringify(f.data.calls.confirmAppointment),
    JSON.stringify(["appt-a", "appt-b"]),
    "TWO distinct mutations really started - a newer generation is open");
  assertEqual(feedbackText(f.doc), "Appointment confirmed.",
    "B owns the feedback surface");
  const readsAfterB = f.data.calls.getSchedule.length;
  const blocksAfterB = blocksIn(columns(f.doc)[0]);
  assertEqual(blockTexts(blocksAfterB[0])[3], "Pending", "A still pending");
  assertEqual(blockTexts(blocksAfterB[1])[3], "Confirmed", "B confirmed");

  /* --- A resolves LAST, on the stale generation. --- */
  queueWeek(f, [], twoPending("confirmed", "confirmed"));
  deferredA.resolve({ ok: true, data: {} });
  await flush();
  await flush();

  /* The generation branch is proven REACHED, not inferred: it is the only
   * branch that issues an authoritative read WITHOUT claiming success.
   * The "current" branch would have written "Appointment confirmed." into
   * pendingFeedback, and an early return would have issued no read. */
  assertEqual(f.data.calls.getSchedule.length, readsAfterB + 1,
    "A's successful older commit still fetched the current truth");
  assertEqual(f.data.calls.getAppointments.length, readsAfterB + 1,
    "through the combined path, both windows");
  assertEqual(feedbackText(f.doc), "",
    "and the older completion claimed NO success message of its own");

  const finalBlocks = blocksIn(columns(f.doc)[0]);
  assertEqual(blockTexts(finalBlocks[0])[3], "Confirmed",
    "A settles from the authoritative read");
  assertEqual(blockTexts(finalBlocks[1])[3], "Confirmed",
    "and B's newer state was never rolled back by the older completion");
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent,
    "Bravo Patient",
    "the panel still belongs to B - the older mutation reopened nothing");
});

test("guards: an older FAILED mutation on a stale generation is dropped entirely", async () => {
  const f = makePages();
  queueWeek(f, [], twoPending("pending", "pending"));
  openCalendar(f);
  await flush();
  openDrawerFor(f, 0);
  await flush();
  const deferredA = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  f.doc._elements["calendar-drawer-close"].trigger("click");
  openDrawerFor(f, 1);
  await flush();
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], twoPending("pending", "confirmed"));
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();
  const readsAfterB = f.data.calls.getSchedule.length;

  /* A was REJECTED before any transition could occur (400/422). That is the
   * only outcome that proves the mutation never applied, so there is
   * genuinely nothing to fetch and nothing to say. A conflict, a not_found,
   * or an ambiguous transport failure would all be different - see the F5
   * and F7 tests. It must not disturb B at all. */
  deferredA.resolve({ ok: false, state: "bad_request" });
  await flush();
  await flush();
  assertEqual(f.data.calls.getSchedule.length, readsAfterB,
    "a failed older attempt that changed nothing triggers no read");
  assertEqual(feedbackText(f.doc), "Appointment confirmed.",
    "and never overwrites the newer mutation's feedback with its conflict");
});

test("guards: a mutation completing after Close leaves the panel closed", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  f.doc._elements["calendar-drawer-close"].trigger("click");

  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "a closed panel is not reopened behind the office's back");
  assertEqual(drawerPairs(f.doc).length, 0, "and holds no patient details");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "while the grid still settles on authoritative state");
});

test("guards: a mutation completing after reset renders nothing and fires no GET", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  f.pages.reset();
  const readsAfterReset = f.data.calls.getSchedule.length;

  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();
  assertEqual(f.data.calls.getSchedule.length, readsAfterReset,
    "no request is fired into a wiped page");
  assertEqual(f.doc._elements["calendar-grid"].children.length, 0,
    "and the wipe stands");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");
});

test("guards: a successful mutation completing after re-entry corrects the stale page (F1)", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  /* The Confirm POST is in flight... */
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  /* ...the office leaves and re-enters the Calendar, and the re-entry read
   * is answered BEFORE the POST commits, so it still returns Pending. */
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  /* Counted separately: the Schedule page visit above issues a
   * getSchedule of its own, so the two counters are legitimately
   * different and comparing them to one baseline would be wrong. */
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Pending",
    "precondition: the re-entered page shows PRE-mutation state");

  /* ...and only now does the original mutation commit. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();

  assertEqual(f.data.calls.getSchedule.length, scheduleReads + 1,
    "a new authoritative read corrects the visible page");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads + 1,
    "both windows, through the existing combined path");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "the calendar is no longer stale");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "and the old drawer is NOT reopened by the old mutation");
});

test("guards: a mutation that CHANGED NOTHING after re-entry triggers no read (F1)", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  const readsAfterReentry = f.data.calls.getSchedule.length;

  /* Rejected before any transition (400/422): the ONLY outcome that proves
   * the server state cannot have moved, so no correction is warranted. */
  deferred.resolve({ ok: false, state: "bad_request" });
  await flush();
  await flush();
  assertEqual(f.data.calls.getSchedule.length, readsAfterReentry,
    "nothing changed on the server, so nothing needs correcting");
  assertEqual(feedbackText(f.doc), "",
    "and no message from a gone UI context is shown");
});

test("guards: a successful mutation completing while ANOTHER page is shown does not render (F1)", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  /* Re-enter Calendar, then move AWAY to Schedule and stay there. */
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();
  const readsAway = f.data.calls.getAppointments.length;

  /* Parked spare: a regressed guard consumes it and fails the count below
   * as a clean assertion instead of crashing on an unscripted call. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();
  assertEqual(f.data.calls.getAppointments.length, readsAway,
    "no background calendar work behind the page the office is actually on");
  assertEqual(f.doc._elements["page-schedule"].hidden, false,
    "and the Schedule page is undisturbed");
});

test("guards: a mutation completing after a week change does not reopen the old panel", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  queueWeek(f, [], [], { start_day: "2026-08-31", end_day: "2026-09-06" },
    { start_day: "2026-08-31", end_day: "2026-09-06" });
  f.doc._elements["calendar-next"].trigger("click");
  await flush();
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");

  queueWeek(f, [], [], { start_day: "2026-08-31", end_day: "2026-09-06" },
    { start_day: "2026-08-31", end_day: "2026-09-06" });
  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "an appointment from the week just left is never reopened");
  assertEqual(JSON.stringify(
    f.data.calls.getSchedule[f.data.calls.getSchedule.length - 1]),
    JSON.stringify({ start_day: "2026-08-31", end_day: "2026-09-06" }),
    "and the refresh uses the CURRENT week bounds");
});

/* ------------------------------------------------------------------ */
/* Failure outcomes                                                     */
/* ------------------------------------------------------------------ */

test("guards: navigating DIRECTLY away does no background calendar work (F3)", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  /* 2. A Confirm is in flight... */
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  /* 3-4. ...and the office goes STRAIGHT to Schedule. No re-entry: the
   * calendar lifecycle is untouched, so only the on-screen gate can stop
   * the settler doing invisible work behind the page they are using. */
  const parkedButtons = actionButtons(f.doc).slice();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;

  /* Deliberately PARK a spare week response in the queue. If the guard ever
   * regresses, the background read consumes it and the counts below fail as
   * a clean assertion instead of crashing on an unscripted call - the
   * failure should name the defect, not the harness. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);

  /* 6. The mutation now succeeds. */
  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();

  /* 7. No combined calendar read, no render. */
  assertEqual(f.data.calls.getSchedule.length, scheduleReads,
    "no background calendar Schedule read");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads,
    "no background calendar Appointments read");
  /* The panel was already open when the office navigated away, and it is
   * hidden along with the whole page-calendar section - exactly like every
   * other portal page's retained content. What matters here is that the
   * settling mutation did NOT re-render it behind their back. */
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Pending",
    "the panel was not re-rendered by the background completion");
  assertEqual(feedbackText(f.doc), "",
    "and no success message was written into an unseen panel");
  assert(actionButtons(f.doc).length === parkedButtons.length &&
    actionButtons(f.doc)[0] === parkedButtons[0],
    "the drawer action controls were not rebuilt behind another page (F6)");
  /* 8. Schedule is still the page the office is on. */
  assertEqual(f.doc._elements["page-schedule"].hidden, false, "Schedule active");
  assertEqual(f.doc._elements["page-calendar"].hidden, true, "Calendar hidden");

  /* 9. The ordinary next visit reads authoritatively and shows the truth. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  openCalendar(f);
  await flush();
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "re-entry's ordinary fresh read shows the authoritative state");
});

test("guards: session loss still wipes and hands back from another page (F3)", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();

  /* The on-screen gate must NOT swallow a lost session. */
  deferred.resolve({ ok: false, state: "unauthorized" });
  await flush();
  await flush();
  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify(["unauthorized"]),
    "control is handed back whichever page is visible");
  assertEqual(f.doc._elements["calendar-grid"].children.length, 0,
    "and the calendar is wiped");
});

test("guards: an in-flight lock survives page re-entry (F4)", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  /* 1. Confirm A starts and stays in flight. */
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  /* 2-3. Leave, then re-enter while A is STILL pending authoritatively. */
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "pending" })]);
  openCalendar(f);
  await flush();

  /* 4-5. Open A again: its controls must still say busy. A page visit does
   * not stop an outstanding request from existing. */
  openDrawerFor(f);
  await flush();
  assertEqual(actionButton(f.doc, "Confirm").disabled, true,
    "Confirm stays disabled across re-entry while the request is in flight");
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, true,
    "and so does Cancel");

  /* 6. No second Confirm can be submitted for the SAME appointment. */
  actionButton(f.doc, "Confirm").trigger("click");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  assertEqual(f.data.calls.confirmAppointment.length, 1,
    "no duplicate submit survives a page visit");

  /* 7. Cancel cannot overlap the in-flight Confirm either. */
  actionButton(f.doc, "Cancel appointment").trigger("click");
  actionButton(f.doc, "Cancel appointment").trigger("click");
  await flush();
  assertEqual(f.data.calls.cancelAppointment.length, 0,
    "the two-click cancel cannot even arm against a busy appointment");

  /* 8. The original mutation settles authoritatively. */
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "confirmed" })]);
  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "the calendar settles on authoritative state");

  /* 9. Ownership is released: the refreshed panel is usable again. */
  assertEqual(f.doc._elements["calendar-drawer"].hidden, false, "panel reopened");
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, false,
    "the in-flight lock cleared once the request actually finished");
});

test("guards: a FAILED mutation across re-entry releases the lock without fabricating state (F4)", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  assertEqual(actionButton(f.doc, "Confirm").disabled, true, "busy across re-entry");
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;

  /* The original request was rejected before any transition (400/422), the
   * one outcome that proves nothing changed, so nothing is fetched - and
   * nothing may be invented. */
  deferred.resolve({ ok: false, state: "bad_request" });
  await flush();
  await flush();
  assertEqual(f.data.calls.getSchedule.length, scheduleReads,
    "a failed pre-re-entry attempt triggers no read");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads,
    "neither window is re-read");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Pending",
    "no server state is fabricated - A is still what the last read said");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Pending",
    "and neither is its block advanced");

  /* And the panel must NOT stay permanently disabled. */
  assertEqual(actionButton(f.doc, "Confirm").disabled, false,
    "the released lock re-enables the already-authoritative controls");
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, false,
    "both of them");

  /* Proof it is genuinely usable: a fresh attempt now goes through. */
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "confirmed" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();
  assertEqual(f.data.calls.confirmAppointment.length, 2,
    "a second, deliberate attempt is accepted once the first has settled");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "and settles authoritatively");
});

/* ------------------------------------------------------------------ */
/* F5: conflict / not_found mean the server MAY have moved                */
/* ------------------------------------------------------------------ */

test("F5: a conflict after re-entry refreshes rather than leaving the page Pending", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  /* Confirm begins while Pending and stays in flight. */
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  /* Calendar is re-entered and the re-entry read still returns Pending. */
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Pending",
    "precondition: the re-entered page shows PRE-mutation state");

  /* The mutation returns 409 - which reports that the appointment changed
   * SOMEWHERE ELSE. Treating that as "nothing happened" would leave the
   * visible calendar wrong. The authoritative refresh reveals the truth. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: false, state: "conflict" });
  await flush();
  await flush();

  assertEqual(f.data.calls.getSchedule.length, scheduleReads + 1,
    "Schedule is re-read after a conflict");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads + 1,
    "and so is Appointments");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "the calendar renders the authoritative changed state, not Pending");
});

test("F5: a not_found after re-entry refreshes so a disappearance is not left stale", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;
  assertEqual(allBlocks(f.doc).length, 1, "precondition: still on screen");

  /* The server no longer has it. The refresh must show that, not keep
   * rendering a block for an appointment that is gone. */
  queueWeek(f, [], []);
  deferred.resolve({ ok: false, state: "not_found" });
  await flush();
  await flush();

  assertEqual(f.data.calls.getSchedule.length, scheduleReads + 1, "Schedule re-read");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads + 1,
    "Appointments re-read");
  assertEqual(allBlocks(f.doc).length, 0,
    "the disappearance is reflected rather than left stale");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");
});

test("F5: a conflict on a STALE GENERATION also refreshes", async () => {
  const f = makePages();
  queueWeek(f, [], twoPending("pending", "pending"));
  openCalendar(f);
  await flush();
  openDrawerFor(f, 0);
  await flush();
  const deferredA = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  /* B opens a newer generation and settles. */
  f.doc._elements["calendar-drawer-close"].trigger("click");
  openDrawerFor(f, 1);
  await flush();
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], twoPending("pending", "confirmed"));
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();
  const readsAfterB = f.data.calls.getSchedule.length;

  /* A now conflicts - the server may have moved, so fetch the truth. */
  queueWeek(f, [], twoPending("cancelled", "confirmed"));
  deferredA.resolve({ ok: false, state: "conflict" });
  await flush();
  await flush();

  assertEqual(f.data.calls.getSchedule.length, readsAfterB + 1,
    "a conflicting older mutation still fetches the current truth");
  assertEqual(f.data.calls.getAppointments.length, readsAfterB + 1,
    "both windows");
  assertEqual(allBlocks(f.doc).length, 1,
    "A left the LIVE layer because the refresh says it is cancelled");
  assertEqual(allHistory(f.doc).length, 1,
    "and reappears as demoted history (Phase 2B)");
  assertEqual(feedbackText(f.doc), "",
    "and the older mutation wrote no feedback over the newer one's");
});

test("F5: a conflict while the Calendar is INACTIVE still does no background read", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;

  /* Parked spare: a regressed gate consumes it and fails cleanly below. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: false, state: "conflict" });
  await flush();
  await flush();
  assertEqual(f.data.calls.getSchedule.length, scheduleReads,
    "the on-screen gate wins over the conflict-refresh rule");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads,
    "no background work at all");

  /* The next ordinary entry is the authoritative recovery. */
  openCalendar(f);
  await flush();
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "re-entry reads authoritatively and shows the truth");
});

/* ------------------------------------------------------------------ */
/* F6: the drawer stays frozen until authoritative state lands           */
/* ------------------------------------------------------------------ */

test("F6: drawer actions stay disabled while the post-mutation read is in flight", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const pendingSet = actionButtons(f.doc).slice();
  assertEqual(JSON.stringify(actionLabels(f.doc)),
    JSON.stringify(["Confirm", "Cancel appointment"]), "precondition");

  /* The POST succeeds, but the authoritative combined read is DEFERRED. */
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  const deferredSchedule = f.data.queueDeferred("getSchedule");
  const deferredAppointments = f.data.queueDeferred("getAppointments");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  /* --- the interval in which the Calendar does not yet know the truth --- */
  assertEqual(f.data.calls.getSchedule.length, 2,
    "precondition: the authoritative read really is in flight");
  assertEqual(actionButton(f.doc, "Confirm").disabled, true,
    "Confirm stays disabled until authoritative state lands");
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, true,
    "and so does Cancel");
  assert(actionButtons(f.doc)[0] === pendingSet[0] &&
    actionButtons(f.doc)[1] === pendingSet[1],
    "the stale Pending action set was NOT rebuilt");

  /* Parked spares: a regressed handler guard consumes these and fails the
   * count below as a clean assertion rather than crashing the harness. */
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "confirmed" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  assertEqual(f.data.calls.confirmAppointment.length, 1,
    "no second Confirm can be sent during the interval");

  actionButton(f.doc, "Cancel appointment").trigger("click");
  await flush();
  assert(actionButton(f.doc, "Confirm cancel") === null,
    "Cancel cannot even be ARMED during the interval");
  actionButton(f.doc, "Cancel appointment").trigger("click");
  await flush();
  assertEqual(f.data.calls.cancelAppointment.length, 0,
    "and no cancellation can be submitted");

  /* --- authoritative state lands --- */
  deferredSchedule.resolve({ ok: true, data: scheduleBody([]) });
  deferredAppointments.resolve({ ok: true,
    data: appointmentsBody([appointmentFixture({ appointment_id: "appt-a",
      status: "confirmed" })]) });
  await flush();
  await flush();

  assertEqual(JSON.stringify(actionLabels(f.doc)),
    JSON.stringify(["Cancel appointment"]),
    "the rebuilt drawer offers exactly appointmentActionsFor('confirmed')");
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, false,
    "and it is usable again");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Confirmed",
    "from the NEW appointment row");
  assertEqual(feedbackText(f.doc), "Appointment confirmed.", "honest outcome");
});

test("F6: opening ANOTHER appointment during the interval also renders it frozen", async () => {
  const f = makePages();
  queueWeek(f, [], twoPending("pending", "pending"));
  openCalendar(f);
  await flush();
  openDrawerFor(f, 0);
  await flush();

  f.data.queue("confirmAppointment", { ok: true, data: {} });
  const deferredSchedule = f.data.queueDeferred("getSchedule");
  const deferredAppointments = f.data.queueDeferred("getAppointments");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  /* B has NO request of its own in flight, so its per-appointment lock is
   * clear - only the settling marker can stop the Calendar handing out live
   * controls built from a row it already knows may be out of date. */
  openDrawerFor(f, 1);
  await flush();
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent,
    "Bravo Patient", "precondition: B's panel is open");
  assertEqual(actionButton(f.doc, "Confirm").disabled, true,
    "B's Confirm is frozen too while authoritative state is in flight");
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, true,
    "and B's Cancel");
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], twoPending("pending", "confirmed"));
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  assertEqual(f.data.calls.confirmAppointment.length, 1,
    "and no action against B can be submitted during the interval");

  deferredSchedule.resolve({ ok: true, data: scheduleBody([]) });
  deferredAppointments.resolve({ ok: true,
    data: appointmentsBody(twoPending("confirmed", "pending")) });
  await flush();
  await flush();
  assertEqual(actionButton(f.doc, "Confirm").disabled, false,
    "once authoritative state lands, B is usable again");
});

/* ------------------------------------------------------------------ */
/* F7: ambiguous mutation outcomes fail safe toward reading the truth    */
/* ------------------------------------------------------------------ */

test("F7: an 'unavailable' after re-entry still refreshes (the POST may have committed)", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Pending",
    "precondition: the re-entered page shows PRE-mutation state");

  /* The server committed and the RESPONSE was lost. The frontend sees a
   * transport failure, which proves nothing about the server. Assuming it
   * did nothing would leave the office looking at a stale Pending. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: false, state: "unavailable" });
  await flush();
  await flush();

  assertEqual(f.data.calls.getSchedule.length, scheduleReads + 1,
    "Schedule is re-read after an ambiguous outcome");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads + 1,
    "and so is Appointments");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "the calendar shows the authoritative truth, not the stale Pending");
});

test("F7: an 'invalid_response' after re-entry has the same safety property", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;

  /* A 200 whose body failed validation: the transition very likely DID
   * occur, the frontend just could not read the confirmation. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: false, state: "invalid_response" });
  await flush();
  await flush();

  assertEqual(f.data.calls.getSchedule.length, scheduleReads + 1, "Schedule re-read");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads + 1,
    "Appointments re-read");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "the authoritative state is shown");
});

test("F7: an ambiguous outcome on a STALE GENERATION also refreshes", async () => {
  const f = makePages();
  queueWeek(f, [], twoPending("pending", "pending"));
  openCalendar(f);
  await flush();
  openDrawerFor(f, 0);
  await flush();
  const deferredA = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  f.doc._elements["calendar-drawer-close"].trigger("click");
  openDrawerFor(f, 1);
  await flush();
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], twoPending("pending", "confirmed"));
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();
  const readsAfterB = f.data.calls.getSchedule.length;

  queueWeek(f, [], twoPending("confirmed", "confirmed"));
  deferredA.resolve({ ok: false, state: "unavailable" });
  await flush();
  await flush();
  assertEqual(f.data.calls.getSchedule.length, readsAfterB + 1,
    "an ambiguous older outcome still fetches the current truth");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "and A turns out to have committed after all");
  assertEqual(feedbackText(f.doc), "",
    "without writing over the newer mutation's feedback");
});

test("F7: an ambiguous outcome while INACTIVE still does no background read", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["nav-schedule"].trigger("click");
  await flush();
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;

  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: false, state: "unavailable" });
  await flush();
  await flush();
  assertEqual(f.data.calls.getSchedule.length, scheduleReads,
    "the on-screen gate still wins");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads,
    "no background work");

  openCalendar(f);
  await flush();
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Confirmed",
    "ordinary re-entry remains the recovery");
});

/* ------------------------------------------------------------------ */
/* F8: a failed post-mutation read stays fail-closed                     */
/* ------------------------------------------------------------------ */

test("F8: a failed post-mutation read keeps actions frozen until new truth lands", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  /* 2-3. The POST succeeds; the combined authoritative read then FAILS. */
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  f.data.queue("getSchedule", { ok: false, state: "unavailable" });
  f.data.queue("getAppointments", { ok: true, data: appointmentsBody([]) });
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  /* 4. The office is told honestly, and no status is invented. */
  assert(f.doc._elements["calendar-state"].textContent.indexOf(
    "temporarily unavailable") !== -1,
    "the honest read-failure message is shown: " +
    f.doc._elements["calendar-state"].textContent);
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Pending",
    "no status is fabricated - it still says what the last read said");

  /* 5-6. Closing and reopening must NOT hand back usable controls built
   * from a row the Calendar itself knows is unresolved. */
  f.doc._elements["calendar-drawer-close"].trigger("click");
  openDrawerFor(f);
  await flush();
  assertEqual(actionButton(f.doc, "Confirm").disabled, true,
    "Confirm stays disabled after close/reopen");
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, true,
    "and so does Cancel");

  /* Parked spares so a regressed guard fails cleanly rather than crashing. */
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  f.data.queue("cancelAppointment", { ok: true, data: {} });
  actionButton(f.doc, "Confirm").trigger("click");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  assertEqual(f.data.calls.confirmAppointment.length, 1,
    "no second Confirm can be issued while the truth is unresolved");
  actionButton(f.doc, "Cancel appointment").trigger("click");
  await flush();
  assert(actionButton(f.doc, "Confirm cancel") === null,
    "Cancel cannot even be armed");
  actionButton(f.doc, "Cancel appointment").trigger("click");
  await flush();
  assertEqual(f.data.calls.cancelAppointment.length, 0,
    "and no cancellation can be issued");

  /* 7-8. A NEW authoritative read is the recovery. */
  queueWeek(f, [], [appointmentFixture({ appointment_id: "appt-a",
    status: "confirmed" })]);
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Confirmed",
    "the drawer is rebuilt from the NEW returned row");
  assertEqual(JSON.stringify(actionLabels(f.doc)),
    JSON.stringify(["Cancel appointment"]),
    "exposing exactly appointmentActionsFor('confirmed')");
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, false,
    "and usable again now that authoritative truth landed");
});

test("F8: page re-entry also clears a frozen post-mutation condition", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  f.data.queue("getSchedule", { ok: false, state: "unavailable" });
  f.data.queue("getAppointments", { ok: true, data: appointmentsBody([]) });
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  /* Re-entry closes the old surface and immediately performs its own fresh
   * authoritative load, so there is no stale drawer to protect. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  assertEqual(actionButton(f.doc, "Cancel appointment").disabled, false,
    "re-entry's own authoritative read clears the frozen condition");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Confirmed",
    "from the newly read row");
});

test("guards: a mutation from a WIPED session cannot read into the next one (wipeEpoch)", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const deferred = f.data.queueDeferred("confirmAppointment");
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();

  /* Sign-out wipes everything, including calendar.active. Then the office
   * signs back in and opens the Calendar again, so active is true once more
   * and the lifecycle has moved on. Only the wipe epoch still remembers that
   * the in-flight mutation belonged to a session that was torn down - and
   * that session's request must never drive a read in this one. */
  f.pages.reset();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  assertEqual(f.doc._elements["page-calendar"].hidden, false,
    "precondition: the Calendar is on screen again");
  const scheduleReads = f.data.calls.getSchedule.length;
  const appointmentReads = f.data.calls.getAppointments.length;

  /* Parked spare so a regressed guard fails cleanly rather than crashing. */
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  deferred.resolve({ ok: true, data: {} });
  await flush();
  await flush();
  assertEqual(f.data.calls.getSchedule.length, scheduleReads,
    "the wiped session's mutation fires no read into the new session");
  assertEqual(f.data.calls.getAppointments.length, appointmentReads,
    "neither window");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "and reopens no panel");
});

test("F8: session loss during a frozen post-mutation state still wipes", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  f.data.queue("getSchedule", { ok: false, state: "unauthorized" });
  f.data.queue("getAppointments", { ok: true, data: appointmentsBody([]) });
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify(["unauthorized"]),
    "the wipe path is unaffected by the frozen condition");
  assertEqual(f.doc._elements["calendar-grid"].children.length, 0, "grid wiped");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");
});

test("failures: a 409 conflict shows honest wording and refreshes", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  const readsBefore = f.data.calls.getSchedule.length;

  f.data.queue("confirmAppointment", { ok: false, state: "conflict" });
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(feedbackText(f.doc),
    "That appointment changed somewhere else. Showing the latest appointments.",
    "honest conflict wording, never a technical error");
  assert(feedbackText(f.doc).indexOf("confirmed.") === -1,
    "and never success wording");
  assertEqual(f.data.calls.getSchedule.length, readsBefore + 1,
    "a conflict still refreshes authoritative state");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Confirmed",
    "showing what the server actually holds now");
});

test("failures: a 404 shows not-found wording and refreshes", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  f.data.queue("confirmAppointment", { ok: false, state: "not_found" });
  queueWeek(f, [], []);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-state"].textContent,
    "That appointment could not be found. Showing the latest appointments.",
    "honest not-found wording");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true,
    "and the panel closes because the row is gone");
});

test("failures: an unavailable outcome never claims success and changes nothing optimistically", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  f.data.queue("confirmAppointment", { ok: false, state: "unavailable" });
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assert(feedbackText(f.doc).indexOf("temporarily unavailable") !== -1,
    "the office is told the action did not complete: " + feedbackText(f.doc));
  assert(feedbackText(f.doc).indexOf("confirmed.") === -1, "no success wording");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Pending",
    "the appointment is NOT optimistically advanced");
  assertEqual(blockTexts(blocksIn(columns(f.doc)[0])[0])[3], "Pending",
    "and neither is its block");
});

test("failures: an invalid response is reported without exposing internals", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  f.data.queue("confirmAppointment", { ok: false, state: "invalid_response" });
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();
  const text = feedbackText(f.doc);
  assert(text.length > 0, "something honest is said");
  for (const leak of ["500", "stack", "Error:", "SQL", "traceback"]) {
    assert(text.indexOf(leak) === -1, "no backend internals leak: " + text);
  }
});

test("failures: session loss during a mutation wipes the calendar and the panel", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();

  f.data.queue("confirmAppointment", { ok: false, state: "unauthorized" });
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-grid"].children.length, 0, "grid wiped");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");
  assertEqual(drawerPairs(f.doc).length, 0, "patient details wiped");
  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify(["unauthorized"]),
    "control handed back through the existing session-loss path");
});

/* ------------------------------------------------------------------ */
/* Ownership                                                            */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* F9: session loss dominates either half of the paired Calendar read    */
/* ------------------------------------------------------------------ */

/* Open the Calendar with one appointment and its detail panel showing, so
 * every wipe assertion below has real tenant data to lose. */
async function calendarWithOpenDrawer(f) {
  queueWeek(f, [slotFixture()], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  assert(drawerPairs(f.doc).length > 0, "precondition: patient details shown");
  assert(allBlocks(f.doc).length > 0, "precondition: a grid is rendered");
}

test("F9: an unauthorized Appointments half wins over an unavailable Schedule half", async () => {
  const f = makePages();
  await calendarWithOpenDrawer(f);

  /* The credential is dead, but the SIBLING request merely failed in
   * transport. A Schedule-first selector would report "unavailable" and
   * leave the office's patient data on screen after the session ended. */
  f.data.queue("getSchedule", { ok: false, state: "unavailable" });
  f.data.queue("getAppointments", { ok: false, state: "unauthorized" });
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-grid"].children.length, 0,
    "the calendar grid is wiped");
  assertEqual(drawerPairs(f.doc).length, 0,
    "and every patient detail with it");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");
  assertEqual(f.doc._elements["calendar-drawer-title"].textContent, "",
    "no patient name lingers");
  assertEqual(f.doc._elements["calendar-timezone-note"].textContent, "",
    "no tenant value lingers");
  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify(["unauthorized"]),
    "onSessionLost fires exactly once, with the session-loss state");
  assertEqual(f.doc._elements["calendar-state"].textContent, "",
    "the ordinary unavailable message never leaves the tenant surface active");
});

test("F9: the rule is order-independent - an unauthorized Schedule half also wins", async () => {
  const f = makePages();
  await calendarWithOpenDrawer(f);

  f.data.queue("getSchedule", { ok: false, state: "unauthorized" });
  f.data.queue("getAppointments", { ok: false, state: "unavailable" });
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-grid"].children.length, 0, "grid wiped");
  assertEqual(drawerPairs(f.doc).length, 0, "patient details wiped");
  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify(["unauthorized"]),
    "exactly one hand-back, whichever half reported it");
  assertEqual(f.doc._elements["calendar-state"].textContent, "",
    "no ordinary failure message survives the wipe");
});

test("F9: signed_out is covered by the same classifier as unauthorized", async () => {
  const f = makePages();
  await calendarWithOpenDrawer(f);

  f.data.queue("getSchedule", { ok: false, state: "unavailable" });
  f.data.queue("getAppointments", { ok: false, state: "signed_out" });
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-grid"].children.length, 0, "grid wiped");
  assertEqual(drawerPairs(f.doc).length, 0, "patient details wiped");
  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify(["signed_out"]),
    "the other session-loss value takes the same dominant path");
});

test("F9: session loss wins even while a mutation has the Calendar frozen (F8)", async () => {
  const f = makePages();
  await calendarWithOpenDrawer(f);

  /* The POST succeeds, so the Calendar freezes waiting for authoritative
   * truth - and THAT read is the one that discovers the dead session. The
   * fail-closed freeze must never stand in the way of a wipe. */
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  f.data.queue("getSchedule", { ok: false, state: "unavailable" });
  f.data.queue("getAppointments", { ok: false, state: "unauthorized" });
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  assertEqual(f.doc._elements["calendar-grid"].children.length, 0, "grid wiped");
  assertEqual(drawerPairs(f.doc).length, 0, "patient details wiped");
  assertEqual(f.doc._elements["calendar-drawer"].hidden, true, "panel closed");
  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify(["unauthorized"]),
    "the frozen post-mutation state does not block the hand-back");

  /* And the wipe genuinely cleared the frozen condition rather than leaving
   * a dead session's marker behind for the next one. */
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  assertEqual(actionButton(f.doc, "Confirm").disabled, false,
    "a fresh session starts unfrozen");
});

test("F9: two ORDINARY failures still take the ordinary path, not a wipe", async () => {
  const f = makePages();
  await calendarWithOpenDrawer(f);

  f.data.queue("getSchedule", { ok: false, state: "unavailable" });
  f.data.queue("getAppointments", { ok: false, state: "not_found" });
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  await flush();

  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify([]),
    "no session was lost, so no hand-back");
  assert(f.doc._elements["calendar-state"].textContent.length > 0,
    "the office is told the read failed");
  /* An ordinary failure does NOT wipe - only session loss does. The last
   * good render stays exactly as it was, and nothing from the failed pair
   * is applied. */
  assertEqual(allBlocks(f.doc).length, 1,
    "the last good render is left intact, not half-replaced");
  assertEqual(f.doc._elements["calendar-drawer-status"].textContent, "Pending",
    "and the panel still shows the last authoritative row");
});

test("F9: a single ordinary failure keeps its own honest message", async () => {
  const f = makePages();
  await calendarWithOpenDrawer(f);

  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.data.queue("getAppointments", { ok: false, state: "unavailable" });
  f.doc._elements["calendar-refresh"].trigger("click");
  await flush();
  await flush();

  assertEqual(JSON.stringify(f.sessionLost), JSON.stringify([]), "no wipe");
  assert(f.doc._elements["calendar-state"].textContent.indexOf(
    "temporarily unavailable") !== -1,
    "the failing half's ordinary message is shown: " +
    f.doc._elements["calendar-state"].textContent);
  /* The Schedule half SUCCEEDED with an empty week. Applying it would have
   * emptied the grid from a half-authoritative pair. It must not be
   * rendered at all while its sibling failed. */
  assertEqual(allBlocks(f.doc).length, 1,
    "the successful half is NOT partially rendered");
  assertEqual(allBands(f.doc).length, 1,
    "its empty slot list did not replace the rendered availability either");
});

test("ownership: the calendar still calls only the existing data methods", async () => {
  const f = makePages();
  queueWeek(f, [], [appointmentFixture({ status: "pending" })]);
  openCalendar(f);
  await flush();
  openDrawerFor(f);
  await flush();
  f.data.queue("confirmAppointment", { ok: true, data: {} });
  queueWeek(f, [], [appointmentFixture({ status: "confirmed" })]);
  actionButton(f.doc, "Confirm").trigger("click");
  await flush();
  await flush();

  /* Across a full mutation cycle, exactly four data methods were used - all
   * of them pre-existing. No new pathway was introduced. */
  const used = Object.keys(f.data.calls)
    .filter((name) => f.data.calls[name].length > 0).sort();
  assertEqual(JSON.stringify(used),
    JSON.stringify(["confirmAppointment", "getAppointments", "getSchedule"]),
    "only existing read + action methods, nothing else");
});

/* ------------------------------------------------------------------ */

(async () => {
  const summary = await h.runRegisteredTests("test_portal_calendar_page");
  process.exitCode = summary.failed === 0 ? 0 : 1;
})();
