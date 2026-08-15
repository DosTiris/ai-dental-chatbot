/*
 * test_portal_pages.js - P3-B1 portal-pages.js proofs.
 *
 * Executes the REAL portal-pages.js in a Node vm against a handcrafted
 * DOM shim (the tests/test_widget_*.js technique) and a scripted fake
 * DATA layer - portal-pages performs no network of its own, so no fetch
 * exists anywhere in this suite. Proves:
 *   - entry lands on a fresh Dashboard: loading -> counts + recent list,
 *     or an explicit empty message, or an explicit error message (a page
 *     is never silently blank);
 *   - navigation to Leads loads the list with the default closed query;
 *   - filters submit the trimmed search + selected status and RESTART at
 *     offset 0; empty filtered results get the filtered empty wording;
 *   - clicking a lead row opens the detail page for exactly that id and
 *     renders fields + transcript via textContent;
 *   - the pager arithmetic and button states follow the backend total,
 *     and Next requests the next offset;
 *   - "unauthorized"/"signed_out" outcomes WIPE rendered content and hand
 *     control back through onSessionLost;
 *   - a stale (superseded) response can never overwrite a newer one;
 *   - reset() clears every rendered tenant value;
 *   - the pure helpers (pagerModel, leadBadges, formatTimestamp,
 *     emptyLeadsMessage) behave.
 *
 * P3-B2 v1.0.1 audit-correction proofs (each fails against the v1.0.0
 * portal-pages.js):
 *   - a delayed mutation response for Lead A never renders into Lead B
 *     (responses are bound to the authoritative lead, not just a sequence);
 *   - status and note in-flight lifecycles are fully independent - one
 *     control's completion never re-enables the other mid-flight;
 *   - a mutation response applies ONLY its own control's value + token:
 *     an older status response cannot roll back a newer note result, and
 *     vice versa;
 *   - a 409 conflict refresh never overwrites or re-enables the sibling
 *     control's in-flight request.
 *
 * P3-B2 v1.0.2 audit-correction proofs (each fails against v1.0.1):
 *   - a conflict refresh that STARTS while the sibling is in flight must
 *     not roll the sibling back even when the sibling settles (busy flag
 *     cleared, newer value + token applied) BEFORE the stale refresh
 *     response arrives - proven in both directions (status refresh vs a
 *     settling note, and note refresh vs a settling status).
 *
 * Run: node tests/portal/test_portal_pages.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const h = require("./portal_test_harness.js");
const { test, assert, assertEqual } = h;

const PAGES_PATH = path.join(__dirname, "..", "..", "static", "portal",
  "portal-pages.js");

/* ------------------------------------------------------------------ */
/* Minimal DOM shim (the widget-test technique, reduced to what        */
/* portal-pages.js actually uses)                                      */
/* ------------------------------------------------------------------ */

function makeClassList() {
  const values = new Set();
  return {
    add: (...items) => items.forEach((item) => values.add(item)),
    remove: (...items) => items.forEach((item) => values.delete(item)),
    toggle: (item, force) => {
      const wanted = force === undefined ? !values.has(item) : !!force;
      if (wanted) values.add(item); else values.delete(item);
      return wanted;
    },
    contains: (item) => values.has(item)
  };
}

function makeElement(tag) {
  const element = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    listeners: {},
    classList: makeClassList(),
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

/* Every id portal-pages.js reads must exist, exactly as index.html
 * provides it (a missing id here means index.html and pages drifted). */
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
  /* P4-A: the Schedule page element set (index.html provides these). */
  "nav-schedule", "page-schedule", "schedule-state", "schedule-list",
  "schedule-range-label", "schedule-timezone-note",
  "schedule-prev", "schedule-next",
  "schedule-day", "schedule-open", "schedule-end", "schedule-minutes",
  "schedule-publish", "schedule-publish-feedback",
  "schedule-block-all", "schedule-bulk-feedback",
  "schedule-booked-remaining", "schedule-action-feedback",
  "nav-settings", "page-settings", "settings-state",
  "settings-email", "settings-phone", "settings-save",
  /* P4-B id-contract growth (disclosed, no behavior change): the recurring
   * panel ids that wireEvents/showPage/resetContent reference must exist in
   * the fake DOM (identical house pattern as P6-A). */
  "nav-recurring", "page-recurring", "recurring-state", "recurring-hours",
  "recurring-slot-minutes", "recurring-closures", "recurring-closure-date",
  "recurring-closure-add", "recurring-closure-end", "recurring-closure-warning",
  "recurring-save", "recurring-save-feedback",
  "recurring-preview", "recurring-preview-output",
  "recurring-apply", "recurring-apply-output",
  "settings-feedback", "settings-email-status", "settings-sms-status"
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

/* Scripted fake data layer: every call takes the NEXT queued outcome (or
 * a pending deferred), and records its arguments for assertions. */
function makeFakeData() {
  const queues = { getDashboard: [], listLeads: [], getLeadDetail: [],
    putLeadStatus: [], putLeadNote: [], getAppointments: [],
    getSchedule: [], publishScheduleDay: [], blockScheduleSlot: [],
    unblockScheduleSlot: [], blockAllOpenSlots: [] };
  const calls = { getDashboard: [], listLeads: [], getLeadDetail: [],
    putLeadStatus: [], putLeadNote: [], getAppointments: [],
    getSchedule: [], publishScheduleDay: [], blockScheduleSlot: [],
    unblockScheduleSlot: [], blockAllOpenSlots: [] };
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
    /* P4-A schedule surface (same scripted-queue discipline). */
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
    calls
  };
}

function loadPagesFactory() {
  const sandboxWindow = {};
  const context = { window: sandboxWindow };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(PAGES_PATH, "utf8"), context,
    { filename: "portal-pages.js" });
  return sandboxWindow.createMiaPortalPages;
}

function makePages() {
  const factory = loadPagesFactory();
  const doc = makeDocument();
  const data = makeFakeData();
  const sessionLost = [];
  const pages = factory({
    data: data,
    documentRef: doc,
    onSessionLost: (state) => sessionLost.push(state)
  });
  return { pages, doc, data, sessionLost, helpers: factory.helpers };
}

/* Flush resolved promise chains. */
function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function leadFixture(overrides) {
  return Object.assign({
    lead_id: "11111111-1111-1111-1111-111111111111",
    lead_name: "Jordan Rivera",
    lead_phone: "516-555-0100",
    lead_email: null,
    lead_reason: "cleaning",
    lead_status: "new",
    lead_patient_type: "new",
    lead_time_window: null,
    lead_is_emergency: false,
    lead_is_priority: false,
    lead_is_outside_hours: false,
    lead_outside_hours_note: null,
    lead_email_opt_out: false,
    last_lead_at: "2026-08-10T15:00:00+00:00",
    created_at: "2026-08-10T14:00:00+00:00"
  }, overrides || {});
}

/* ------------------------------------------------------------------ */
/* Dashboard                                                            */
/* ------------------------------------------------------------------ */

test("factory fails loudly without its wiring", () => {
  const factory = loadPagesFactory();
  let threw = false;
  try { factory({}); } catch (e) { threw = true; }
  assert(threw, "missing deps must throw");
});

test("enter lands on Dashboard and renders counts + recent leads", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "Test Dental", total_conversations: 7, total_leads: 3,
    urgent_leads: 2, leads_last_7_days: 1,
    recent_leads: [leadFixture(), leadFixture({ lead_id: "2", lead_name: "Sam" })]
  } });
  env.pages.enter();
  assertEqual(env.doc._elements["page-dashboard"].hidden, false, "dashboard shown");
  assertEqual(env.doc._elements["page-leads"].hidden, true, "leads hidden");
  assertEqual(env.doc._elements["dashboard-state"].textContent, "Loading...",
    "loading state visible first");
  await flush();
  assertEqual(env.doc._elements["dashboard-state"].textContent, "", "loading cleared");
  assertEqual(env.doc._elements["count-conversations"].textContent, "7", "count");
  assertEqual(env.doc._elements["count-urgent-leads"].textContent, "2", "count");
  assertEqual(env.doc._elements["dashboard-counts"].hidden, false, "counts shown");
  assertEqual(env.doc._elements["dashboard-recent"].children.length, 2, "rows");
});

test("an empty dashboard says so explicitly", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "Test Dental", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: []
  } });
  env.pages.enter();
  await flush();
  assert(env.doc._elements["dashboard-state"].textContent.indexOf("No leads yet") === 0,
    "explicit empty message");
});

test("an unavailable dashboard shows the honest error and no counts", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  assert(env.doc._elements["dashboard-state"].textContent
    .indexOf("temporarily unavailable") !== -1, "error visible");
  assertEqual(env.doc._elements["dashboard-counts"].hidden, true, "counts hidden");
});

/* ------------------------------------------------------------------ */
/* Leads list                                                           */
/* ------------------------------------------------------------------ */

test("Leads nav loads the list with the default closed query", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  env.data.queue("listLeads", { ok: true,
    data: { total: 0, limit: 25, offset: 0, leads: [] } });
  env.doc._elements["nav-leads"].trigger("click");
  assertEqual(env.doc._elements["page-leads"].hidden, false, "leads page shown");
  await flush();
  const params = env.data.calls.listLeads[0];
  assertEqual(params.status, "", "no status filter");
  assertEqual(params.q, "", "no search");
  assertEqual(params.limit, 25, "page size");
  assertEqual(params.offset, 0, "first page");
  assertEqual(env.doc._elements["leads-state"].textContent,
    "No leads yet. New leads will appear here.", "unfiltered empty wording");
});

test("filters submit trimmed search + status and restart at offset 0", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  env.data.queue("listLeads", { ok: true,
    data: { total: 0, limit: 25, offset: 0, leads: [] } });
  env.doc._elements["nav-leads"].trigger("click");
  await flush();

  env.doc._elements["leads-search"].value = "  rivera  ";
  env.doc._elements["leads-status"].value = "completed";
  env.data.queue("listLeads", { ok: true,
    data: { total: 0, limit: 25, offset: 0, leads: [] } });
  env.doc._elements["leads-filter-form"].trigger("submit");
  await flush();
  const params = env.data.calls.listLeads[1];
  assertEqual(params.q, "rivera", "search trimmed");
  assertEqual(params.status, "completed", "status passed");
  assertEqual(params.offset, 0, "offset restarted");
  assertEqual(env.doc._elements["leads-state"].textContent,
    "No leads match these filters.", "filtered empty wording");
});

test("rows render and clicking one opens the detail for exactly that id", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  env.data.queue("listLeads", { ok: true, data: { total: 2, limit: 25,
    offset: 0, leads: [
      leadFixture({ lead_is_emergency: true }),
      leadFixture({ lead_id: "lead-2", lead_name: "Sam Patel" })
    ] } });
  env.doc._elements["nav-leads"].trigger("click");
  await flush();
  const list = env.doc._elements["leads-list"];
  assertEqual(list.children.length, 2, "two rows");
  const firstButton = list.children[0].children[0];
  assert(firstButton.children.some((child) =>
    child.className === "portal-badge" && child.textContent === "Emergency"),
    "emergency badge rendered");

  env.data.queue("getLeadDetail", { ok: true, data: Object.assign(
    leadFixture(), { messages: [
      { role: "user", content: "hello", created_at: "2026-08-10T14:00:00+00:00" },
      { role: "assistant", content: "hi", created_at: "2026-08-10T14:00:05+00:00" }
    ], messages_total: 2, messages_truncated: false }) });
  firstButton.trigger("click");
  assertEqual(env.doc._elements["page-lead-detail"].hidden, false, "detail shown");
  await flush();
  assertEqual(env.data.calls.getLeadDetail[0],
    "11111111-1111-1111-1111-111111111111", "detail for the clicked id");
  assertEqual(env.doc._elements["detail-name"].textContent, "Jordan Rivera",
    "name rendered");
  assertEqual(env.doc._elements["detail-messages"].children.length, 2,
    "transcript rendered");
  const speakers = env.doc._elements["detail-messages"].children
    .map((line) => line.children[0].textContent);
  assertEqual(speakers.join(","), "Patient,Mia", "roles mapped for the office");
  assertEqual(env.doc._elements["detail-body"].hidden, false, "body shown");
});

test("a missing lead detail shows the honest not-found message", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  env.data.queue("listLeads", { ok: true, data: { total: 1, limit: 25,
    offset: 0, leads: [leadFixture()] } });
  env.doc._elements["nav-leads"].trigger("click");
  await flush();
  env.data.queue("getLeadDetail", { ok: false, state: "not_found" });
  env.doc._elements["leads-list"].children[0].children[0].trigger("click");
  await flush();
  assert(env.doc._elements["detail-state"].textContent
    .indexOf("could not be found") !== -1, "not-found message");
  assertEqual(env.doc._elements["detail-body"].hidden, true, "no stale body");
});

test("the pager follows the backend total and Next requests the next offset", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  const pageOf = (offset) => ({ ok: true, data: { total: 60, limit: 25,
    offset: offset, leads: [leadFixture()] } });
  env.data.queue("listLeads", pageOf(0));
  env.doc._elements["nav-leads"].trigger("click");
  await flush();
  assertEqual(env.doc._elements["leads-page-label"].textContent,
    "Showing 1-25 of 60", "label");
  assertEqual(env.doc._elements["leads-prev"].disabled, true, "prev disabled");
  assertEqual(env.doc._elements["leads-next"].disabled, false, "next enabled");

  env.data.queue("listLeads", pageOf(25));
  env.doc._elements["leads-next"].trigger("click");
  await flush();
  assertEqual(env.data.calls.listLeads[1].offset, 25, "next offset requested");
  assertEqual(env.doc._elements["leads-prev"].disabled, false, "prev enabled");
});

/* ------------------------------------------------------------------ */
/* Session loss, staleness, reset                                       */
/* ------------------------------------------------------------------ */

test("unauthorized wipes rendered content and hands control back", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "Test Dental", total_conversations: 7, total_leads: 3,
    urgent_leads: 2, leads_last_7_days: 1, recent_leads: [leadFixture()]
  } });
  env.pages.enter();
  await flush();
  env.data.queue("listLeads", { ok: false, state: "unauthorized" });
  env.doc._elements["nav-leads"].trigger("click");
  await flush();
  assertEqual(env.sessionLost.join(","), "unauthorized", "handed back once");
  assertEqual(env.doc._elements["dashboard-recent"].children.length, 0,
    "rendered tenant rows wiped");
  assertEqual(env.doc._elements["count-leads"].textContent, "", "counts wiped");
});

test("a stale (superseded) list response can never overwrite the newer one", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  const slow = env.data.queueDeferred("listLeads");
  env.doc._elements["nav-leads"].trigger("click");   /* request 1 (slow) */
  env.data.queue("listLeads", { ok: true, data: { total: 1, limit: 25,
    offset: 0, leads: [leadFixture({ lead_name: "Newest Result" })] } });
  env.doc._elements["nav-leads"].trigger("click");   /* request 2 (fast) */
  await flush();
  /* Now the OLD request resolves late with different rows. */
  slow.resolve({ ok: true, data: { total: 1, limit: 25, offset: 0,
    leads: [leadFixture({ lead_name: "Stale Result" })] } });
  await flush();
  const names = env.doc._elements["leads-list"].children
    .map((item) => item.children[0].children[0].textContent);
  assertEqual(names.join(","), "Newest Result", "stale response ignored");
});

test("reset clears every rendered tenant value and the filter inputs", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "Test Dental", total_conversations: 7, total_leads: 3,
    urgent_leads: 2, leads_last_7_days: 1, recent_leads: [leadFixture()]
  } });
  env.pages.enter();
  await flush();
  env.doc._elements["leads-search"].value = "rivera";
  env.pages.reset();
  assertEqual(env.doc._elements["count-conversations"].textContent, "", "counts cleared");
  assertEqual(env.doc._elements["dashboard-recent"].children.length, 0, "recent cleared");
  assertEqual(env.doc._elements["leads-search"].value, "", "search cleared");
  assertEqual(env.doc._elements["detail-fields"].children.length, 0, "detail cleared");
});

/* ------------------------------------------------------------------ */
/* Audit corrections (v1.0.1)                                           */
/* ------------------------------------------------------------------ */

test("A1 bite: a completed-intake lead is labeled as intake completion", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  env.data.queue("listLeads", { ok: true, data: { total: 1, limit: 25,
    offset: 0, leads: [leadFixture({ lead_status: "completed" })] } });
  env.doc._elements["nav-leads"].trigger("click");
  await flush();
  const meta = env.doc._elements["leads-list"].children[0].children[0]
    .children.filter((c) => c.className === "portal-lead-meta")[0];
  assert(meta.textContent.indexOf("Intake completed") === 0,
    "row shows 'Intake completed', got: " + meta.textContent);
  assert(meta.textContent.toLowerCase().indexOf("completed  ") !== 0 ||
    meta.textContent.indexOf("Intake completed") === 0,
    "raw ambiguous 'completed' must not be shown alone");

  env.data.queue("getLeadDetail", { ok: true, data: Object.assign(
    leadFixture({ lead_status: "completed" }),
    { messages: [], messages_total: 0, messages_truncated: false }) });
  env.doc._elements["leads-list"].children[0].children[0].trigger("click");
  await flush();
  const fields = env.doc._elements["detail-fields"].children;
  let statusValue = null;
  for (let i = 0; i < fields.length - 1; i += 2) {
    if (fields[i].textContent === "Status") { statusValue = fields[i + 1].textContent; }
  }
  assertEqual(statusValue, "Intake completed", "detail status labeled honestly");
});

test("A2 bite: a truncated transcript shows the explicit partial notice", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  env.data.queue("listLeads", { ok: true, data: { total: 1, limit: 25,
    offset: 0, leads: [leadFixture()] } });
  env.doc._elements["nav-leads"].trigger("click");
  await flush();
  env.data.queue("getLeadDetail", { ok: true, data: Object.assign(
    leadFixture(), { messages: [
      { role: "user", content: "m1", created_at: "2026-08-10T14:00:00+00:00" },
      { role: "user", content: "m2", created_at: "2026-08-10T14:01:00+00:00" }
    ], messages_total: 9, messages_truncated: true }) });
  env.doc._elements["leads-list"].children[0].children[0].trigger("click");
  await flush();
  assertEqual(env.doc._elements["detail-transcript-note"].textContent,
    "Showing the first 2 of 9 messages.", "explicit truncation notice");

  /* And a full transcript clears the notice. */
  env.data.queue("getLeadDetail", { ok: true, data: Object.assign(
    leadFixture(), { messages: [
      { role: "user", content: "m1", created_at: "2026-08-10T14:00:00+00:00" }
    ], messages_total: 1, messages_truncated: false }) });
  env.doc._elements["leads-list"].children[0].children[0].trigger("click");
  await flush();
  assertEqual(env.doc._elements["detail-transcript-note"].textContent, "",
    "no notice when the transcript is complete");
});

test("A3: an invalid_response outcome shows its message, never a stuck loading state", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: false, state: "invalid_response" });
  env.pages.enter();
  await flush();
  assert(env.doc._elements["dashboard-state"].textContent
    .indexOf("unexpected response") !== -1, "honest invalid-response message");
  assertEqual(env.doc._elements["dashboard-counts"].hidden, true, "no counts");
});

/* ------------------------------------------------------------------ */
/* Pure helpers                                                         */
/* ------------------------------------------------------------------ */

test("pure helpers: pager arithmetic, badges, timestamps, empty wording", () => {
  const helpers = loadPagesFactory().helpers;
  const first = helpers.pagerModel(60, 25, 0);
  assertEqual(first.label, "Showing 1-25 of 60", "first page label");
  assertEqual(first.prevDisabled, true, "prev disabled on first page");
  assertEqual(first.nextOffset, 25, "next offset");
  const last = helpers.pagerModel(60, 25, 50);
  assertEqual(last.label, "Showing 51-60 of 60", "last page label");
  assertEqual(last.nextDisabled, true, "next disabled on last page");
  assertEqual(last.prevOffset, 25, "prev offset");
  assertEqual(helpers.pagerModel(0, 25, 0).label, "0 leads", "empty label");

  assertEqual(helpers.leadBadges(leadFixture({ lead_is_emergency: true,
    lead_is_outside_hours: true })).join(","), "Emergency,After hours",
    "badge order");
  assertEqual(helpers.formatTimestamp("not-a-date"), "", "invalid time is empty");
  assert(helpers.formatTimestamp("2026-08-10T15:00:00+00:00").length > 0,
    "valid time renders");
  assertEqual(helpers.emptyLeadsMessage(true), "No leads match these filters.",
    "filtered wording");
  assertEqual(helpers.statusLabel("completed"), "Intake completed",
    "A1: completed labels as intake completion, never office follow-up");
  assertEqual(helpers.statusLabel("new"), "Intake not completed",
    "A1-R1: 'new' labels as unfinished intake, never as a staff state");
  assertEqual(helpers.statusLabel("someday-status"), "someday-status",
    "unknown status renders raw, never guessed");
});

/* ------------------------------------------------------------------ */

(async () => {
  const summary = await h.runRegisteredTests("test_portal_pages");
  process.exitCode = summary.failed === 0 ? 0 : 1;
})();


/* ------------------------------------------------------------------ */
/* P3-B2: office workflow controls on the Lead Detail page              */
/* ------------------------------------------------------------------ */

function detailBodyFixture(overrides) {
  return Object.assign(leadFixture(), {
    messages: [], messages_total: 0, messages_truncated: false,
    office_status: null, office_status_updated_at: null,
    office_note: null, office_note_updated_at: null
  }, overrides || {});
}

function workflowBodyFixture(overrides) {
  return Object.assign({
    lead_id: leadFixture().lead_id,
    office_status: "contacted",
    office_status_updated_at: "2026-08-12T03:00:00Z",
    office_note: null,
    office_note_updated_at: null
  }, overrides || {});
}

/* Open the detail page the way an office user does: enter, go to Leads,
 * click the one row (the house row-click technique). */
async function openDetailWith(env, body) {
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  env.data.queue("listLeads", { ok: true, data: { total: 1, limit: 25,
    offset: 0, leads: [leadFixture()] } });
  env.doc._elements["nav-leads"].trigger("click");
  await flush();
  env.data.queue("getLeadDetail", { ok: true, data: body });
  env.doc._elements["leads-list"].children[0].children[0].trigger("click");
  await flush();
}

test("detail renders the office slice and a save sends the observed token", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture({
    office_status: "booked",
    office_status_updated_at: "2026-08-12T02:00:00Z"
  }));
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "select shows persisted status");
  env.doc._elements["detail-office-status"].value = "closed";
  env.data.queue("putLeadStatus", { ok: true,
    data: workflowBodyFixture({ office_status: "closed",
      office_status_updated_at: "2026-08-12T03:10:00Z" }) });
  env.doc._elements["detail-status-save"].trigger("click");
  await flush();
  const sent = env.data.calls.putLeadStatus[0];
  assertEqual(sent.status, "closed", "requested value sent");
  assertEqual(sent.token, "2026-08-12T02:00:00Z",
    "the token last OBSERVED is what travels");
  assertEqual(env.doc._elements["detail-status-feedback"].textContent,
    "Status saved.", "validated success only after the response");
  /* A second save must carry the FRESH token from the response. */
  env.data.queue("putLeadStatus", { ok: true,
    data: workflowBodyFixture({ office_status: "closed",
      office_status_updated_at: "2026-08-12T03:11:00Z" }) });
  env.doc._elements["detail-status-save"].trigger("click");
  await flush();
  assertEqual(env.data.calls.putLeadStatus[1].token,
    "2026-08-12T03:10:00Z", "token advanced client-side after success");
});

test("duplicate status submits are blocked while one is in flight", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture());
  const deferred = env.data.queueDeferred("putLeadStatus");
  env.doc._elements["detail-status-save"].trigger("click");
  env.doc._elements["detail-status-save"].trigger("click"); /* busy */
  assertEqual(env.data.calls.putLeadStatus.length, 1,
    "exactly ONE request left the page");
  assert(env.doc._elements["detail-status-save"].disabled === true,
    "save button disabled while in flight");
  deferred.resolve({ ok: true, data: workflowBodyFixture() });
  await flush();
  assert(env.doc._elements["detail-status-save"].disabled === false,
    "button re-enabled after settle");
});

test("duplicate note submits are blocked while one is in flight", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture());
  env.doc._elements["detail-office-note"].value = "call back";
  const deferred = env.data.queueDeferred("putLeadNote");
  env.doc._elements["detail-note-save"].trigger("click");
  env.doc._elements["detail-note-save"].trigger("click");   /* busy */
  env.doc._elements["detail-note-clear"].trigger("click");  /* also busy */
  assertEqual(env.data.calls.putLeadNote.length, 1,
    "exactly ONE request left the page");
  deferred.resolve({ ok: true, data: workflowBodyFixture({
    office_note: "call back",
    office_note_updated_at: "2026-08-12T03:20:00Z" }) });
  await flush();
  assert(env.doc._elements["detail-note-save"].disabled === false,
    "note buttons re-enabled after settle");
});

test("a stale mutation response can never overwrite newer UI state", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture());
  const deferred = env.data.queueDeferred("putLeadStatus");
  env.doc._elements["detail-status-save"].trigger("click"); /* in flight */
  env.pages.reset();                        /* session wiped meanwhile */
  deferred.resolve({ ok: true, data: workflowBodyFixture() });
  await flush();
  assertEqual(env.doc._elements["detail-status-feedback"].textContent, "",
    "superseded response drew NOTHING");
  assertEqual(env.doc._elements["detail-office-status"].value, "",
    "wiped select stays wiped");
});

test("409 shows the conflict message and refreshes authoritative detail", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture());
  env.data.queue("putLeadStatus", { ok: false, state: "conflict" });
  env.data.queue("getLeadDetail", { ok: true, data: detailBodyFixture({
    office_status: "booked",
    office_status_updated_at: "2026-08-12T04:00:00Z"
  }) });
  env.doc._elements["detail-status-save"].trigger("click");
  await flush();
  assertEqual(env.doc._elements["detail-status-feedback"].textContent,
    "This lead was updated somewhere else. Showing the latest state.",
    "conflict wording shown");
  assertEqual(env.data.calls.getLeadDetail.length, 2,
    "authoritative re-fetch happened");
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "latest persisted state rendered");
});

test("a failed mutation never displays success", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture());
  env.data.queue("putLeadNote", { ok: false, state: "unavailable" });
  env.doc._elements["detail-office-note"].value = "call back";
  env.doc._elements["detail-note-save"].trigger("click");
  await flush();
  const feedback = env.doc._elements["detail-note-feedback"].textContent;
  assert(feedback.indexOf("saved") === -1, "no success wording: " + feedback);
  assert(feedback.length > 0, "an explicit failure message is shown");
});

test("whitespace-only note is stopped locally; Clear note sends null", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture({
    office_note: "old text", office_note_updated_at: "2026-08-12T01:00:00Z"
  }));
  env.doc._elements["detail-office-note"].value = "   ";
  env.doc._elements["detail-note-save"].trigger("click");
  await flush();
  assertEqual(env.data.calls.putLeadNote.length, 0,
    "no request for a whitespace-only save");
  env.data.queue("putLeadNote", { ok: true, data: workflowBodyFixture({
    office_status: null, office_status_updated_at: null,
    office_note: null, office_note_updated_at: "2026-08-12T05:00:00Z" }) });
  env.doc._elements["detail-note-clear"].trigger("click");
  await flush();
  assertEqual(env.data.calls.putLeadNote[0].note, null,
    "clear sends an explicit null");
  assertEqual(env.data.calls.putLeadNote[0].token, "2026-08-12T01:00:00Z",
    "clear carries the observed note token");
  assertEqual(env.doc._elements["detail-note-feedback"].textContent,
    "Note cleared.", "clear reported only after the response");
});

test("reset wipes every office workflow value (tenant-data wipe)", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture({
    office_status: "contacted",
    office_status_updated_at: "2026-08-12T02:00:00Z",
    office_note: "sensitive tenant text",
    office_note_updated_at: "2026-08-12T02:30:00Z"
  }));
  env.pages.reset();
  assertEqual(env.doc._elements["detail-office-status"].value, "", "select wiped");
  assertEqual(env.doc._elements["detail-office-note"].value, "", "note wiped");
  assertEqual(env.doc._elements["detail-status-meta"].textContent, "", "meta wiped");
  assertEqual(env.doc._elements["detail-note-meta"].textContent, "", "meta wiped");
});

/* ------------------------------------------------------------------ */
/* P3-B2 v1.0.1: audit-correction regression proofs                     */
/* ------------------------------------------------------------------ */

const LEAD_B_ID = "22222222-2222-2222-2222-222222222222";

/* Open the detail page from a TWO-row list (Lead A then Lead B), landing
 * on Lead A - the cross-lead proof then clicks row B directly. */
async function openDetailWithTwoLeads(env, bodyA) {
  env.data.queue("getDashboard", { ok: false, state: "unavailable" });
  env.pages.enter();
  await flush();
  env.data.queue("listLeads", { ok: true, data: { total: 2, limit: 25,
    offset: 0, leads: [leadFixture(),
      leadFixture({ lead_id: LEAD_B_ID, lead_name: "Sam Alvarez" })] } });
  env.doc._elements["nav-leads"].trigger("click");
  await flush();
  env.data.queue("getLeadDetail", { ok: true, data: bodyA });
  env.doc._elements["leads-list"].children[0].children[0].trigger("click");
  await flush();
}

test("v1.0.1 F2 bite: a delayed Lead A mutation response never renders into Lead B", async () => {
  const env = makePages();
  await openDetailWithTwoLeads(env, detailBodyFixture());
  const deferred = env.data.queueDeferred("putLeadStatus");
  env.doc._elements["detail-office-status"].value = "closed";
  env.doc._elements["detail-status-save"].trigger("click"); /* A in flight */
  /* Navigate to Lead B before Lead A's response returns. */
  env.data.queue("getLeadDetail", { ok: true, data: detailBodyFixture({
    lead_id: LEAD_B_ID, office_status: "booked",
    office_status_updated_at: "2026-08-12T06:00:00Z" }) });
  env.doc._elements["leads-list"].children[1].children[0].trigger("click");
  await flush();
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "Lead B's slice is on screen");
  assert(env.doc._elements["detail-status-save"].disabled === false,
    "navigation handed Lead B enabled controls");
  /* Lead A's delayed response lands AFTER Lead B is authoritative. */
  deferred.resolve({ ok: true, data: workflowBodyFixture({
    office_status: "closed",
    office_status_updated_at: "2026-08-12T06:05:00Z" }) });
  await flush();
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "Lead A's response drew NOTHING into Lead B");
  assertEqual(env.doc._elements["detail-status-feedback"].textContent, "",
    "no success message from the foreign-lead response");
  /* The next save must target Lead B with Lead B's observed token,
   * proving neither the lead nor the token was poisoned. */
  env.data.queue("putLeadStatus", { ok: true, data: workflowBodyFixture({
    lead_id: LEAD_B_ID, office_status: "booked",
    office_status_updated_at: "2026-08-12T06:10:00Z" }) });
  env.doc._elements["detail-status-save"].trigger("click");
  await flush();
  const sent = env.data.calls.putLeadStatus[1];
  assertEqual(sent.leadId, LEAD_B_ID, "the save targets Lead B");
  assertEqual(sent.token, "2026-08-12T06:00:00Z",
    "the token observed on Lead B is what travels");
});

test("v1.0.1 F3 bite: status completion never re-enables note controls still in flight", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture());
  env.doc._elements["detail-office-note"].value = "call back";
  const noteDeferred = env.data.queueDeferred("putLeadNote");
  env.doc._elements["detail-note-save"].trigger("click");   /* note in flight */
  const statusDeferred = env.data.queueDeferred("putLeadStatus");
  env.doc._elements["detail-office-status"].value = "contacted";
  env.doc._elements["detail-status-save"].trigger("click"); /* both in flight */
  assert(env.doc._elements["detail-note-save"].disabled === true &&
    env.doc._elements["detail-status-save"].disabled === true,
    "both controls disabled while both requests are in flight");
  statusDeferred.resolve({ ok: true, data: workflowBodyFixture({
    office_status: "contacted",
    office_status_updated_at: "2026-08-12T07:00:00Z" }) });
  await flush();
  assert(env.doc._elements["detail-status-save"].disabled === false,
    "status control settled and re-enabled itself");
  assert(env.doc._elements["detail-note-save"].disabled === true,
    "note save STAYS disabled: its own request is still in flight");
  assert(env.doc._elements["detail-note-clear"].disabled === true,
    "note clear STAYS disabled too");
  noteDeferred.resolve({ ok: true, data: workflowBodyFixture({
    office_note: "call back",
    office_note_updated_at: "2026-08-12T07:05:00Z" }) });
  await flush();
  assert(env.doc._elements["detail-note-save"].disabled === false,
    "note controls re-enabled only by their OWN settle");
  assertEqual(env.doc._elements["detail-note-feedback"].textContent,
    "Note saved.", "the pending note reported its own success");
});

test("v1.0.1 F4 bite: an older status response cannot roll back a newer note result", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture({
    office_note: "old note",
    office_note_updated_at: "2026-08-12T01:00:00Z" }));
  const statusDeferred = env.data.queueDeferred("putLeadStatus");
  env.doc._elements["detail-office-status"].value = "contacted";
  env.doc._elements["detail-status-save"].trigger("click"); /* status FIRST */
  env.doc._elements["detail-office-note"].value = "new note text";
  env.data.queue("putLeadNote", { ok: true, data: workflowBodyFixture({
    office_status: null, office_status_updated_at: null,
    office_note: "new note text",
    office_note_updated_at: "2026-08-12T04:00:00Z" }) });
  env.doc._elements["detail-note-save"].trigger("click");   /* note SECOND */
  await flush();                                     /* note settles first */
  assertEqual(env.doc._elements["detail-office-note"].value, "new note text",
    "newer note result on screen");
  /* The OLDER status response returns afterward carrying the OLD note
   * snapshot - it must update ONLY the status slice. */
  statusDeferred.resolve({ ok: true, data: workflowBodyFixture({
    office_status: "contacted",
    office_status_updated_at: "2026-08-12T05:00:00Z",
    office_note: "old note",
    office_note_updated_at: "2026-08-12T01:00:00Z" }) });
  await flush();
  assertEqual(env.doc._elements["detail-office-status"].value, "contacted",
    "status slice applied from the status response");
  assertEqual(env.doc._elements["detail-office-note"].value, "new note text",
    "note value NOT rolled back by the older status response");
  /* Token proof: the next note save must carry the NEWER note token. */
  env.data.queue("putLeadNote", { ok: true, data: workflowBodyFixture({
    office_note: "new note text",
    office_note_updated_at: "2026-08-12T05:30:00Z" }) });
  env.doc._elements["detail-note-save"].trigger("click");
  await flush();
  assertEqual(env.data.calls.putLeadNote[1].token, "2026-08-12T04:00:00Z",
    "note token NOT rolled back by the older status response");
});

test("v1.0.1 F4 bite: an older note response cannot roll back a newer status result", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture());
  env.doc._elements["detail-office-note"].value = "first note";
  const noteDeferred = env.data.queueDeferred("putLeadNote");
  env.doc._elements["detail-note-save"].trigger("click");   /* note FIRST */
  env.data.queue("putLeadStatus", { ok: true, data: workflowBodyFixture({
    office_status: "booked",
    office_status_updated_at: "2026-08-12T06:00:00Z",
    office_note: null, office_note_updated_at: null }) });
  env.doc._elements["detail-office-status"].value = "booked";
  env.doc._elements["detail-status-save"].trigger("click"); /* status SECOND */
  await flush();                                   /* status settles first */
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "newer status result on screen");
  noteDeferred.resolve({ ok: true, data: workflowBodyFixture({
    office_status: null, office_status_updated_at: null,
    office_note: "first note",
    office_note_updated_at: "2026-08-12T06:30:00Z" }) });
  await flush();
  assertEqual(env.doc._elements["detail-office-note"].value, "first note",
    "note slice applied from the note response");
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "status value NOT rolled back by the older note response");
  env.data.queue("putLeadStatus", { ok: true, data: workflowBodyFixture({
    office_status: "booked",
    office_status_updated_at: "2026-08-12T07:00:00Z" }) });
  env.doc._elements["detail-status-save"].trigger("click");
  await flush();
  assertEqual(env.data.calls.putLeadStatus[1].token, "2026-08-12T06:00:00Z",
    "status token NOT rolled back by the older note response");
});

test("v1.0.1 F4 bite: conflict refresh never disturbs the sibling's in-flight request", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture());
  env.doc._elements["detail-office-note"].value = "pending note";
  const noteDeferred = env.data.queueDeferred("putLeadNote");
  env.doc._elements["detail-note-save"].trigger("click");   /* note in flight */
  env.data.queue("putLeadStatus", { ok: false, state: "conflict" });
  env.data.queue("getLeadDetail", { ok: true, data: detailBodyFixture({
    office_status: "booked",
    office_status_updated_at: "2026-08-12T08:00:00Z",
    office_note: "server note",
    office_note_updated_at: "2026-08-12T02:00:00Z" }) });
  env.doc._elements["detail-office-status"].value = "contacted";
  env.doc._elements["detail-status-save"].trigger("click");
  await flush();
  assertEqual(env.doc._elements["detail-status-feedback"].textContent,
    "This lead was updated somewhere else. Showing the latest state.",
    "conflict wording shown on the conflicted control");
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "refresh applied the authoritative status");
  assertEqual(env.doc._elements["detail-office-note"].value, "pending note",
    "refresh did NOT overwrite the note being saved");
  assert(env.doc._elements["detail-note-save"].disabled === true,
    "refresh did NOT re-enable the in-flight note controls");
  noteDeferred.resolve({ ok: true, data: workflowBodyFixture({
    office_note: "pending note",
    office_note_updated_at: "2026-08-12T08:30:00Z" }) });
  await flush();
  assertEqual(env.doc._elements["detail-note-feedback"].textContent,
    "Note saved.", "the pending note settled normally after the refresh");
  env.data.queue("putLeadNote", { ok: true, data: workflowBodyFixture({
    office_note: "pending note",
    office_note_updated_at: "2026-08-12T09:00:00Z" }) });
  env.doc._elements["detail-note-save"].trigger("click");
  await flush();
  assertEqual(env.data.calls.putLeadNote[1].token, "2026-08-12T08:30:00Z",
    "the note token comes from the note's OWN response, not the refresh");
});

/* ------------------------------------------------------------------ */
/* P3-B2 v1.0.2: conflict-refresh residual-race proofs                  */
/* ------------------------------------------------------------------ */

test("v1.0.2 F4 bite A: a stale conflict refresh cannot roll back a note that settled after the refresh began", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture({
    office_note: "old note",
    office_note_updated_at: "2026-08-12T01:00:00Z" }));
  /* 1. Note mutation begins and stays in flight. */
  env.doc._elements["detail-office-note"].value = "newer note";
  const noteDeferred = env.data.queueDeferred("putLeadNote");
  env.doc._elements["detail-note-save"].trigger("click");
  /* 2-3. Status mutation returns 409; its conflict refresh GET is itself
   * deferred so we control when it arrives relative to the note settle. */
  env.data.queue("putLeadStatus", { ok: false, state: "conflict" });
  const refreshDeferred = env.data.queueDeferred("getLeadDetail");
  env.doc._elements["detail-office-status"].value = "contacted";
  env.doc._elements["detail-status-save"].trigger("click");
  await flush();  /* status 409 handled; refresh GET now in flight; note pending */
  assert(env.doc._elements["detail-note-save"].disabled === true,
    "note is still in flight when the refresh has begun");
  /* 4. The note settles with a NEWER value + token, BEFORE the refresh
   * returns - its busy flag clears here. */
  noteDeferred.resolve({ ok: true, data: workflowBodyFixture({
    office_status: null, office_status_updated_at: null,
    office_note: "newer note",
    office_note_updated_at: "2026-08-12T04:00:00Z" }) });
  await flush();
  assertEqual(env.doc._elements["detail-office-note"].value, "newer note",
    "the newer note is applied locally before the refresh returns");
  assert(env.doc._elements["detail-note-save"].disabled === false,
    "the note settled - its busy flag is now clear");
  /* 5-6. The stale refresh arrives carrying the OLD note snapshot. */
  refreshDeferred.resolve({ ok: true, data: detailBodyFixture({
    office_status: "booked",
    office_status_updated_at: "2026-08-12T08:00:00Z",
    office_note: "old note",
    office_note_updated_at: "2026-08-12T01:00:00Z" }) });
  await flush();
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "the conflicted status control still refreshes to authoritative state");
  assertEqual(env.doc._elements["detail-office-note"].value, "newer note",
    "the stale refresh did NOT roll back the newer note value");
  /* Token proof: the next note save must carry the NEWER note token. */
  env.data.queue("putLeadNote", { ok: true, data: workflowBodyFixture({
    office_note: "newer note",
    office_note_updated_at: "2026-08-12T08:30:00Z" }) });
  env.doc._elements["detail-note-save"].trigger("click");
  await flush();
  assertEqual(env.data.calls.putLeadNote[1].token, "2026-08-12T04:00:00Z",
    "the note token was NOT rolled back by the stale refresh");
});

test("v1.0.2 F4 bite B: a stale conflict refresh cannot roll back a status that settled after the refresh began", async () => {
  const env = makePages();
  await openDetailWith(env, detailBodyFixture({
    office_status: "contacted",
    office_status_updated_at: "2026-08-12T01:00:00Z" }));
  /* 1. Status mutation begins and stays in flight. */
  env.doc._elements["detail-office-status"].value = "booked";
  const statusDeferred = env.data.queueDeferred("putLeadStatus");
  env.doc._elements["detail-status-save"].trigger("click");
  /* 2-3. Note mutation returns 409; its refresh GET is deferred. */
  env.doc._elements["detail-office-note"].value = "note text";
  env.data.queue("putLeadNote", { ok: false, state: "conflict" });
  const refreshDeferred = env.data.queueDeferred("getLeadDetail");
  env.doc._elements["detail-note-save"].trigger("click");
  await flush();  /* note 409 handled; refresh in flight; status pending */
  assert(env.doc._elements["detail-status-save"].disabled === true,
    "status is still in flight when the refresh has begun");
  /* 4. Status settles with a NEWER value + token before the refresh returns. */
  statusDeferred.resolve({ ok: true, data: workflowBodyFixture({
    office_status: "booked",
    office_status_updated_at: "2026-08-12T04:00:00Z",
    office_note: null, office_note_updated_at: null }) });
  await flush();
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "the newer status is applied locally before the refresh returns");
  assert(env.doc._elements["detail-status-save"].disabled === false,
    "the status settled - its busy flag is now clear");
  /* 5-6. Stale refresh arrives carrying the OLD status snapshot. */
  refreshDeferred.resolve({ ok: true, data: detailBodyFixture({
    office_status: "contacted",
    office_status_updated_at: "2026-08-12T01:00:00Z",
    office_note: "server note",
    office_note_updated_at: "2026-08-12T08:00:00Z" }) });
  await flush();
  assertEqual(env.doc._elements["detail-office-note"].value, "server note",
    "the conflicted note control still refreshes to authoritative state");
  assertEqual(env.doc._elements["detail-office-status"].value, "booked",
    "the stale refresh did NOT roll back the newer status value");
  /* Token proof: the next status save must carry the NEWER status token. */
  env.data.queue("putLeadStatus", { ok: true, data: workflowBodyFixture({
    office_status: "booked",
    office_status_updated_at: "2026-08-12T08:30:00Z" }) });
  env.doc._elements["detail-status-save"].trigger("click");
  await flush();
  assertEqual(env.data.calls.putLeadStatus[1].token, "2026-08-12T04:00:00Z",
    "the status token was NOT rolled back by the stale refresh");
});

/* ------------------------------------------------------------------ */
/* Portal Appointments v1: read-only appointments page                  */
/* ------------------------------------------------------------------ */

function appointmentFixture(overrides) {
  return Object.assign({
    appointment_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    patient_name: "Kevin Alvarado",
    patient_phone: "516-555-1234",
    patient_email: null,
    new_or_returning: "new",
    reason: "cleaning",
    urgency: "routine",
    start_datetime: "2026-07-16T14:00:00+00:00",
    end_datetime: "2026-07-16T14:45:00+00:00",
    status: "pending",
    confirmed_at: null,
    source: "mia_widget",
    notification_outcome: "pending"
  }, overrides || {});
}

function appointmentsBodyFixture(overrides) {
  return Object.assign({
    timezone_name: "America/New_York",
    start_day: "2026-07-16",
    end_day: "2026-07-22",
    appointments: [appointmentFixture()]
  }, overrides || {});
}

test("clicking Appointments loads and renders rows with a loading state first", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "Test Dental", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();

  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture() });
  env.doc._elements["nav-appointments"].trigger("click");
  assertEqual(env.doc._elements["page-appointments"].hidden, false,
    "appointments page shown");
  assertEqual(env.doc._elements["page-dashboard"].hidden, true,
    "dashboard hidden");
  assertEqual(env.doc._elements["appointments-state"].textContent, "Loading...",
    "loading state visible first");
  await flush();
  assertEqual(env.doc._elements["appointments-state"].textContent, "",
    "loading cleared on success");
  assertEqual(env.doc._elements["appointments-list"].children.length, 1,
    "one appointment row rendered");
});

test("the default appointments request sends NO bounds (backend default)", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture() });
  env.doc._elements["nav-appointments"].trigger("click");
  await flush();
  const params = env.data.calls.getAppointments[0];
  assertEqual(params.start_day, undefined, "no start_day on default view");
  assertEqual(params.end_day, undefined, "no end_day on default view");
});

test("an empty appointments range says so explicitly", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture({ appointments: [] }) });
  env.doc._elements["nav-appointments"].trigger("click");
  await flush();
  assert(env.doc._elements["appointments-state"].textContent
    .indexOf("No appointments") === 0, "explicit empty message");
  assertEqual(env.doc._elements["appointments-list"].children.length, 0,
    "no rows");
});

test("an unavailable appointments load shows the honest error, no rows", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();
  env.data.queue("getAppointments", { ok: false, state: "unavailable" });
  env.doc._elements["nav-appointments"].trigger("click");
  await flush();
  assert(env.doc._elements["appointments-state"].textContent
    .indexOf("temporarily unavailable") !== -1, "honest error message");
  assertEqual(env.doc._elements["appointments-list"].children.length, 0,
    "no rows on error");
});

test("a session-loss appointments outcome hands back through onSessionLost", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();
  env.data.queue("getAppointments", { ok: false, state: "unauthorized" });
  env.doc._elements["nav-appointments"].trigger("click");
  await flush();
  assertEqual(env.sessionLost[env.sessionLost.length - 1], "unauthorized",
    "session loss routed to onSessionLost");
});

test("Next week sends explicit +7 bounds relative to the default start", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();
  /* Default view establishes defaultStart = 2026-07-16. */
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture() });
  env.doc._elements["nav-appointments"].trigger("click");
  await flush();
  /* Next week: start = default + 7, end = start + 6. */
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture({ start_day: "2026-07-23",
      end_day: "2026-07-29", appointments: [] }) });
  env.doc._elements["appt-next"].trigger("click");
  await flush();
  const params = env.data.calls.getAppointments[1];
  assertEqual(params.start_day, "2026-07-23", "next start = default + 7");
  assertEqual(params.end_day, "2026-07-29", "next end = start + 6");
});

test("Previous then Next returns to the default (offset arithmetic)", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture() });
  env.doc._elements["nav-appointments"].trigger("click");
  await flush();
  /* Previous week: offset -1 -> start = default - 7. */
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture({ start_day: "2026-07-09",
      end_day: "2026-07-15", appointments: [] }) });
  env.doc._elements["appt-prev"].trigger("click");
  await flush();
  assertEqual(env.data.calls.getAppointments[1].start_day, "2026-07-09",
    "previous start = default - 7");
  /* Next week: offset back to 0 -> NO bounds (default). */
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture() });
  env.doc._elements["appt-next"].trigger("click");
  await flush();
  assertEqual(env.data.calls.getAppointments[2].start_day, undefined,
    "returning to offset 0 sends no bounds");
});

test("a stale appointments response never overwrites a newer one", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();
  /* First load is deferred (in-flight); a second click supersedes it. */
  const slow = env.data.queueDeferred("getAppointments");
  env.doc._elements["nav-appointments"].trigger("click");
  /* nav re-entry resets to offset 0 and reloads; queue the fresh result. */
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture({ appointments: [
      appointmentFixture({ appointment_id: "newer", patient_name: "Newer" })
    ] }) });
  env.doc._elements["nav-appointments"].trigger("click");
  await flush();
  /* Now resolve the STALE first request with different content. */
  slow.resolve({ ok: true, data: appointmentsBodyFixture({ appointments: [
    appointmentFixture({ appointment_id: "stale", patient_name: "Stale" })
  ] }) });
  await flush();
  /* The rendered row must be the NEWER one, not the late stale response. */
  const list = env.doc._elements["appointments-list"];
  assertEqual(list.children.length, 1, "one row");
  /* The row's name span is the first child of the row div. */
  const row = list.children[0].children[0];
  assertEqual(row.children[0].textContent, "Newer",
    "the newer response won; the stale one was dropped");
});

test("session-loss wipe clears rendered appointment content", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture() });
  env.doc._elements["nav-appointments"].trigger("click");
  await flush();
  assertEqual(env.doc._elements["appointments-list"].children.length, 1,
    "a row is present before reset");
  env.pages.reset();
  assertEqual(env.doc._elements["appointments-list"].children.length, 0,
    "appointment rows wiped on reset");
  assertEqual(env.doc._elements["appt-range-label"].textContent, "",
    "range label wiped");
  assertEqual(env.doc._elements["appt-timezone-note"].textContent, "",
    "timezone note wiped");
});

test("F3 regression: re-entry with a pending default GET blocks stale-anchor navigation", async () => {
  const env = makePages();
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.pages.enter();
  await flush();

  /* First visit: establish the OLD anchor (defaultStart = 2026-07-16). */
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture() });
  env.doc._elements["nav-appointments"].trigger("click");
  await flush();
  assertEqual(env.doc._elements["appt-next"].disabled, false,
    "nav enabled once the first default resolved");

  /* Navigate away (Dashboard). */
  env.data.queue("getDashboard", { ok: true, data: {
    practice_name: "T", total_conversations: 0, total_leads: 0,
    urgent_leads: 0, leads_last_7_days: 0, recent_leads: [] } });
  env.doc._elements["nav-dashboard"].trigger("click");
  await flush();

  /* Re-enter Appointments; hold the fresh default GET PENDING (unresolved). */
  const pending = env.data.queueDeferred("getAppointments");
  env.doc._elements["nav-appointments"].trigger("click");
  /* At this instant the fresh default has NOT resolved: nav must be disabled
   * and the old anchor cleared. */
  assertEqual(env.doc._elements["appt-next"].disabled, true,
    "Next disabled while the fresh default is in flight");
  assertEqual(env.doc._elements["appt-prev"].disabled, true,
    "Previous disabled while the fresh default is in flight");

  /* Click Next NOW (the reproduction step). It must be a no-op: no new
   * getAppointments call, and certainly none computed from the old anchor. */
  const callsBefore = env.data.calls.getAppointments.length;
  env.doc._elements["appt-next"].trigger("click");
  assertEqual(env.data.calls.getAppointments.length, callsBefore,
    "a Next click before the fresh anchor resolves triggers NO request");

  /* Resolve the fresh default with a DIFFERENT anchor than the old visit. */
  pending.resolve({ ok: true, data: appointmentsBodyFixture({
    start_day: "2026-09-14", end_day: "2026-09-20", appointments: [] }) });
  await flush();
  assertEqual(env.doc._elements["appt-next"].disabled, false,
    "nav re-enabled after the fresh default resolved");

  /* Now Next computes from the NEW anchor (2026-09-14), never the old one. */
  env.data.queue("getAppointments", { ok: true,
    data: appointmentsBodyFixture({ start_day: "2026-09-21",
      end_day: "2026-09-27", appointments: [] }) });
  env.doc._elements["appt-next"].trigger("click");
  await flush();
  const last = env.data.calls.getAppointments[
    env.data.calls.getAppointments.length - 1];
  assertEqual(last.start_day, "2026-09-21",
    "Next is computed from the FRESH anchor (+7), never the stale 2026-07-16");
  assert(last.start_day !== "2026-07-23",
    "the stale anchor's next week (2026-07-23) is never produced");
});

/* ------------------------------------------------------------------ */
/* Appointments pure helpers                                            */
/* ------------------------------------------------------------------ */

test("formatInTimeZone renders in the OFFICE timezone, not device time", () => {
  const env = makePages();
  const helpers = env.helpers;
  /* 2026-07-16T14:00:00Z is 10:00 AM in America/New_York (EDT, -4). */
  const ny = helpers.formatInTimeZone("2026-07-16T14:00:00+00:00",
    "America/New_York");
  assert(ny.indexOf("America/New_York") !== -1,
    "the office timezone is named in the output");
  assert(ny.indexOf("10:00") !== -1, "10:00 AM local to New York");
});

test("formatInTimeZone falls back to explicit UTC, never device time", () => {
  const env = makePages();
  const helpers = env.helpers;
  const bad = helpers.formatInTimeZone("2026-07-16T14:00:00+00:00",
    "Not/AZone");
  assert(bad.indexOf("UTC") !== -1,
    "an unsupported timezone falls back to explicit UTC");
});

test("formatInTimeZone returns empty for missing/unparseable input", () => {
  const env = makePages();
  const helpers = env.helpers;
  assertEqual(helpers.formatInTimeZone("", "America/New_York"), "",
    "empty input -> empty");
  assertEqual(helpers.formatInTimeZone("not-a-date", "America/New_York"), "",
    "unparseable input -> empty, never Invalid Date");
});

test("shiftLocalDay shifts calendar dates purely and validates input", () => {
  const env = makePages();
  const helpers = env.helpers;
  assertEqual(helpers.shiftLocalDay("2026-07-16", 7), "2026-07-23", "+7");
  assertEqual(helpers.shiftLocalDay("2026-07-16", -7), "2026-07-09", "-7");
  assertEqual(helpers.shiftLocalDay("2026-07-16", 6), "2026-07-22", "+6");
  assertEqual(helpers.shiftLocalDay("2026-03-08", 1), "2026-03-09",
    "crosses a DST date without slipping");
  assertEqual(helpers.shiftLocalDay("bad", 1), "", "malformed -> empty");
});

test("appointmentStatusLabel and notificationOutcomeLabel map the vocabularies", () => {
  const env = makePages();
  const helpers = env.helpers;
  assertEqual(helpers.appointmentStatusLabel("pending"), "Pending", "status");
  assertEqual(helpers.appointmentStatusLabel("no_show"), "No-show", "status");
  assertEqual(helpers.appointmentStatusLabel("weird"), "weird",
    "unknown status renders as itself");
  assertEqual(helpers.notificationOutcomeLabel("sent"), "Office notified",
    "outcome sent");
  assertEqual(helpers.notificationOutcomeLabel("failed"),
    "Notification failed", "outcome failed");
  assertEqual(helpers.notificationOutcomeLabel("pending"),
    "Notification pending", "outcome pending");
});
