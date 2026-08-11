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
  "detail-transcript-note"
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
  const queues = { getDashboard: [], listLeads: [], getLeadDetail: [] };
  const calls = { getDashboard: [], listLeads: [], getLeadDetail: [] };
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
