/*
 * test_portal_schedule_page.js - P4-A Schedule page proofs (contract v1.2
 * SS6 / SS8.14, Correction C4).
 *
 * Drives the REAL portal-pages.js in a Node vm over a fake document and a
 * scripted fake data layer (the test_portal_pages.js technique) and proves
 * the mutation-generation contract a mutation-capable page needs:
 *
 *   c. STALE-READ ROLLBACK BITE: a window GET issued BEFORE a mutation
 *      begins, resolving AFTER the mutation's authoritative refresh, is
 *      discarded (generation mismatch) - rendered state cannot roll back.
 *   d. REFRESH-ORDERING BITE: an older refresh result cannot overwrite a
 *      newer refresh (request sequence enforced).
 *   e. DUPLICATE-SUBMIT BITES: Publish, per-slot Block, per-slot Unblock,
 *      and Block-All each suppress duplicate submission while in flight,
 *      and one slot's busy state never disables another slot's control.
 *   f. SESSION-LOSS BITE: session loss invalidates all outstanding read /
 *      mutation generations and wipes every rendered schedule value.
 *
 * Plus: rendering (office-timezone times, status labels, per-status
 * action buttons), the authoritative post-mutation refresh (optimistic
 * state is never rendered), day-required guards, the booked-remaining
 * rendering, and the bulk-action copy audit (no day-shutting vocabulary
 * in ANY rendered schedule text).
 *
 * Run: node tests/portal/test_portal_schedule_page.js
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
/* Fixtures (the test_portal_pages.js technique, schedule-complete)     */
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

/* Every id portal-pages.js reads (index.html contract). */
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

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

/* Structurally-valid schedule bodies (the data-layer validators would have
 * rejected anything less, so the pages only ever see these shapes). */
function slotFixture(overrides) {
  return Object.assign({
    slot_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    start_datetime: "2026-08-21T13:00:00Z",
    end_datetime: "2026-08-21T14:00:00Z",
    status: "available",
    provider_name: null,
    service_key: null
  }, overrides || {});
}

function scheduleBody(slots, overrides) {
  return Object.assign({
    timezone_name: "America/New_York",
    start_day: "2026-08-21",
    end_day: "2026-08-27",
    slots: slots
  }, overrides || {});
}

function openSchedule(fixture) {
  fixture.doc._elements["nav-schedule"].trigger("click");
}

function renderedRowTexts(doc) {
  return doc._elements["schedule-list"].children.map((item) => {
    const row = item.children[0];
    return row.children.map((child) => child.textContent).join(" | ");
  });
}

/* ------------------------------------------------------------------ */
/* Rendering                                                            */
/* ------------------------------------------------------------------ */

test("schedule: opening loads the default window and renders rows", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture(),
    slotFixture({ slot_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      start_datetime: "2026-08-21T14:00:00Z",
      end_datetime: "2026-08-21T15:00:00Z", status: "blocked" })
  ]) });
  openSchedule(f);
  await flush();

  assertEqual(f.data.calls.getSchedule.length, 1, "one default GET");
  assert(JSON.stringify(f.data.calls.getSchedule[0]) === "{}",
    "the default window sends NO bounds");
  assert(f.doc._elements["page-schedule"].hidden === false,
    "schedule page visible");
  const rows = renderedRowTexts(f.doc);
  assertEqual(rows.length, 2, "two rendered rows");
  assert(rows[0].indexOf("Open") !== -1, "available renders as Open");
  assert(rows[1].indexOf("Blocked") !== -1, "blocked renders as Blocked");
  assert(f.doc._elements["schedule-timezone-note"].textContent
    .indexOf("America/New_York") !== -1, "office timezone note rendered");
  assert(f.doc._elements["schedule-prev"].disabled === false &&
    f.doc._elements["schedule-next"].disabled === false,
    "navigation enabled once the default anchor exists");
});

test("schedule: action buttons follow the status vocabulary", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ status: "available" }),
    slotFixture({ slot_id: "b", status: "held" }),
    slotFixture({ slot_id: "c", status: "blocked" }),
    slotFixture({ slot_id: "d", status: "booked" }),
    slotFixture({ slot_id: "e", status: "cancelled" })
  ]) });
  openSchedule(f);
  await flush();

  const items = f.doc._elements["schedule-list"].children;
  const buttonLabel = (index) => {
    const row = items[index].children[0];
    const last = row.children[row.children.length - 1];
    return last.tagName === "BUTTON" ? last.textContent : null;
  };
  assertEqual(buttonLabel(0), "Block", "available -> Block");
  assertEqual(buttonLabel(1), "Block", "held -> Block");
  assertEqual(buttonLabel(2), "Unblock", "blocked -> Unblock");
  assertEqual(buttonLabel(3), null, "booked -> no action button");
  assertEqual(buttonLabel(4), null, "cancelled -> no action button");
});

test("schedule: no rendered schedule text uses day-shutting vocabulary", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ status: "blocked" })
  ]) });
  openSchedule(f);
  await flush();
  /* Bulk result copy included. */
  f.doc._elements["schedule-day"].value = "2026-08-21";
  f.data.queue("blockAllOpenSlots", { ok: true, data: {
    day: "2026-08-21", blocked_count: 0,
    booked_remaining: [{ start_datetime: "2026-08-21T17:00:00Z",
      end_datetime: "2026-08-21T18:00:00Z" }] } });
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["schedule-block-all"].trigger("click");
  await flush();

  const texts = [];
  for (const id of ["schedule-state", "schedule-publish-feedback",
    "schedule-bulk-feedback", "schedule-booked-remaining",
    "schedule-action-feedback", "schedule-range-label",
    "schedule-timezone-note"]) {
    texts.push(f.doc._elements[id].textContent);
  }
  for (const row of renderedRowTexts(f.doc)) { texts.push(row); }
  const joined = texts.join(" ").toLowerCase();
  for (const word of ["close", "closed", "closure"]) {
    assert(joined.indexOf(word) === -1,
      "rendered schedule text must not contain '" + word + "'");
  }
});

/* ------------------------------------------------------------------ */
/* C4 bite (c): stale read cannot roll back a mutation's refresh        */
/* ------------------------------------------------------------------ */

test("schedule: a pre-mutation GET resolving late is discarded (generation)", async () => {
  const f = makePages();
  /* 1. Open: authoritative render with one blocked slot (anchor set). */
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ status: "blocked" })
  ]) });
  openSchedule(f);
  await flush();

  /* 2. A window GET is issued (Next week) and left IN FLIGHT. Its captured
   * generation is the pre-mutation value. */
  const staleRead = f.data.queueDeferred("getSchedule");
  f.doc._elements["schedule-next"].trigger("click");
  await flush();

  /* 3. A mutation begins and completes; its authoritative refresh renders
   * the new truth (slot now available after Unblock). */
  f.data.queue("unblockScheduleSlot", { ok: true, data:
    slotFixture({ status: "available" }) });
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ status: "available" })
  ]) });
  const row = f.doc._elements["schedule-list"].children[0].children[0];
  row.children[row.children.length - 1].trigger("click");
  await flush();
  assert(renderedRowTexts(f.doc)[0].indexOf("Open") !== -1,
    "authoritative refresh rendered the unblocked slot");

  /* 4. NOW the stale pre-mutation read resolves with the OLD truth. It
   * must be discarded: the rendered state may not roll back. */
  staleRead.resolve({ ok: true, data: scheduleBody([
    slotFixture({ status: "blocked" })
  ], { start_day: "2026-08-28", end_day: "2026-09-03" }) });
  await flush();
  assert(renderedRowTexts(f.doc)[0].indexOf("Open") !== -1,
    "the stale read did NOT overwrite the post-mutation render");
  assert(f.doc._elements["schedule-range-label"].textContent
    .indexOf("2026-08-28") === -1, "the stale range label never rendered");
});

/* ------------------------------------------------------------------ */
/* C4 bite (d): an older refresh cannot overwrite a newer refresh       */
/* ------------------------------------------------------------------ */

test("schedule: an older window response cannot overwrite a newer one", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  openSchedule(f);
  await flush();

  /* Two navigations; the OLDER request resolves LAST. */
  const older = f.data.queueDeferred("getSchedule");
  f.doc._elements["schedule-next"].trigger("click");
  const newer = f.data.queueDeferred("getSchedule");
  f.doc._elements["schedule-next"].trigger("click");

  newer.resolve({ ok: true, data: scheduleBody([
    slotFixture({ status: "available" })
  ], { start_day: "2026-09-04", end_day: "2026-09-10" }) });
  await flush();
  assertEqual(renderedRowTexts(f.doc).length, 1, "newer response rendered");

  older.resolve({ ok: true, data: scheduleBody([
    slotFixture({ status: "blocked" }),
    slotFixture({ slot_id: "b", status: "blocked" })
  ], { start_day: "2026-08-28", end_day: "2026-09-03" }) });
  await flush();
  assertEqual(renderedRowTexts(f.doc).length, 1,
    "the superseded older response never rendered");
  assert(f.doc._elements["schedule-range-label"].textContent
    .indexOf("2026-09-04") !== -1, "the NEWER range label stands");
});

/* ------------------------------------------------------------------ */
/* C4 bite (e): duplicate suppression, independent busy states          */
/* ------------------------------------------------------------------ */

test("schedule: duplicate Publish submits are suppressed while in flight", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  openSchedule(f);
  await flush();

  f.doc._elements["schedule-day"].value = "2026-08-21";
  f.doc._elements["schedule-open"].value = "09:00";
  f.doc._elements["schedule-end"].value = "17:00";
  f.doc._elements["schedule-minutes"].value = "30";
  const inflight = f.data.queueDeferred("publishScheduleDay");
  f.doc._elements["schedule-publish"].trigger("click");
  f.doc._elements["schedule-publish"].trigger("click");
  f.doc._elements["schedule-publish"].trigger("click");
  assertEqual(f.data.calls.publishScheduleDay.length, 1,
    "exactly ONE publish call despite three clicks");
  assertEqual(f.data.calls.publishScheduleDay[0].slotMinutes, 30,
    "the portal default slot length is sent");

  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture()
  ]) });
  inflight.resolve({ ok: true, data: [slotFixture()] });
  await flush();
  assertEqual(renderedRowTexts(f.doc).length, 1,
    "the authoritative refresh rendered - never the optimistic body");
  assert(f.doc._elements["schedule-publish"].disabled === false,
    "publish re-enabled after completion");
});

test("schedule: per-slot busy is per slot; Bulk and Publish stay independent", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ slot_id: "s1", status: "blocked" }),
    slotFixture({ slot_id: "s2", status: "blocked",
      start_datetime: "2026-08-21T15:00:00Z",
      end_datetime: "2026-08-21T16:00:00Z" })
  ]) });
  openSchedule(f);
  await flush();

  const items = f.doc._elements["schedule-list"].children;
  const buttonOf = (index) => {
    const row = items[index].children[0];
    return row.children[row.children.length - 1];
  };
  const inflight = f.data.queueDeferred("unblockScheduleSlot");
  buttonOf(0).trigger("click");
  buttonOf(0).trigger("click");
  assertEqual(f.data.calls.unblockScheduleSlot.length, 1,
    "duplicate per-slot submits suppressed");
  assert(buttonOf(0).disabled === true, "the acting slot's control is busy");
  assert(buttonOf(1).disabled === false,
    "the OTHER slot's control stays enabled (independent busy)");
  assert(f.doc._elements["schedule-publish"].disabled === false &&
    f.doc._elements["schedule-block-all"].disabled === false,
    "Publish and Block-All stay enabled during a per-slot action");

  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  inflight.resolve({ ok: true, data: slotFixture({ slot_id: "s1",
    status: "available" }) });
  await flush();
});

test("schedule: duplicate Block-All submits are suppressed while in flight", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  openSchedule(f);
  await flush();

  f.doc._elements["schedule-day"].value = "2026-08-21";
  const inflight = f.data.queueDeferred("blockAllOpenSlots");
  f.doc._elements["schedule-block-all"].trigger("click");
  f.doc._elements["schedule-block-all"].trigger("click");
  assertEqual(f.data.calls.blockAllOpenSlots.length, 1,
    "exactly ONE bulk call despite two clicks");

  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  inflight.resolve({ ok: true, data: { day: "2026-08-21", blocked_count: 3,
    booked_remaining: [] } });
  await flush();
  assert(f.doc._elements["schedule-bulk-feedback"].textContent
    .indexOf("3") !== -1, "the blocked count is reported");
});

test("schedule: publish and bulk refuse an empty day locally", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  openSchedule(f);
  await flush();

  f.doc._elements["schedule-publish"].trigger("click");
  f.doc._elements["schedule-block-all"].trigger("click");
  assertEqual(f.data.calls.publishScheduleDay.length, 0,
    "no publish request without a day");
  assertEqual(f.data.calls.blockAllOpenSlots.length, 0,
    "no bulk request without a day");
  assert(f.doc._elements["schedule-publish-feedback"].textContent !== "",
    "the publish day-required message rendered");
  assert(f.doc._elements["schedule-bulk-feedback"].textContent !== "",
    "the bulk day-required message rendered");
});

test("schedule: booked-remaining windows render after a bulk block", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  openSchedule(f);
  await flush();

  f.doc._elements["schedule-day"].value = "2026-08-21";
  f.data.queue("blockAllOpenSlots", { ok: true, data: {
    day: "2026-08-21", blocked_count: 2,
    booked_remaining: [
      { start_datetime: "2026-08-21T16:00:00Z",
        end_datetime: "2026-08-21T17:00:00Z" },
      { start_datetime: "2026-08-21T17:00:00Z",
        end_datetime: "2026-08-21T18:00:00Z" }
    ] } });
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  f.doc._elements["schedule-block-all"].trigger("click");
  await flush();
  const remaining = f.doc._elements["schedule-booked-remaining"].textContent;
  assert(remaining.indexOf("Booked appointments remain") !== -1,
    "the booked-remaining line rendered");
});

/* ------------------------------------------------------------------ */
/* C4 bite (f): session loss invalidates everything and wipes           */
/* ------------------------------------------------------------------ */

test("schedule: session loss wipes rendered data and outstanding reads never apply", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ status: "blocked" })
  ]) });
  openSchedule(f);
  await flush();
  assertEqual(renderedRowTexts(f.doc).length, 1, "a row rendered pre-loss");

  /* An in-flight window read AND an in-flight mutation... */
  const pendingRead = f.data.queueDeferred("getSchedule");
  f.doc._elements["schedule-next"].trigger("click");
  const row = f.doc._elements["schedule-list"].children[0].children[0];
  const pendingMutation = f.data.queueDeferred("unblockScheduleSlot");
  row.children[row.children.length - 1].trigger("click");

  /* ...then the mutation resolves as SIGNED OUT: reset + hand-back. */
  pendingMutation.resolve({ ok: false, state: "signed_out" });
  await flush();
  assertEqual(f.sessionLost.length, 1, "session loss handed back once");
  assertEqual(f.doc._elements["schedule-list"].children.length, 0,
    "the rendered schedule rows were wiped");
  assertEqual(f.doc._elements["schedule-timezone-note"].textContent, "",
    "the timezone note was wiped");
  assertEqual(f.doc._elements["schedule-day"].value, "",
    "the day input was wiped");

  /* The outstanding pre-loss read resolves LAST - both its request id and
   * its captured generation are stale, so NOTHING may render. */
  pendingRead.resolve({ ok: true, data: scheduleBody([
    slotFixture(), slotFixture({ slot_id: "b" })
  ]) });
  await flush();
  assertEqual(f.doc._elements["schedule-list"].children.length, 0,
    "the post-loss stale read rendered nothing");
  assertEqual(f.doc._elements["schedule-state"].textContent, "",
    "no state text reappeared after the wipe");
});

/* ------------------------------------------------------------------ */
/* Failure wording + authoritative refresh on conflict                  */
/* ------------------------------------------------------------------ */

test("schedule: a publish conflict renders honest wording and refreshes", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([]) });
  openSchedule(f);
  await flush();

  f.doc._elements["schedule-day"].value = "2026-08-21";
  f.doc._elements["schedule-open"].value = "09:00";
  f.doc._elements["schedule-end"].value = "17:00";
  f.data.queue("publishScheduleDay", { ok: false, state: "conflict" });
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ status: "booked" })
  ]) });
  f.doc._elements["schedule-publish"].trigger("click");
  await flush();
  assert(f.doc._elements["schedule-publish-feedback"].textContent
    .indexOf("overlap") !== -1, "the overlap wording rendered");
  assertEqual(f.data.calls.getSchedule.length, 2,
    "the conflict triggered an authoritative refresh");
  assertEqual(renderedRowTexts(f.doc).length, 1,
    "the refreshed truth rendered");
});

test("schedule: pure helper maps the closed status vocabulary", () => {
  const f = makePages();
  const label = f.helpers.scheduleSlotStatusLabel;
  assertEqual(label("available"), "Open", "available");
  assertEqual(label("held"), "On hold", "held");
  assertEqual(label("booked"), "Booked", "booked");
  assertEqual(label("blocked"), "Blocked", "blocked");
  assertEqual(label("cancelled"), "Cancelled", "cancelled");
  assertEqual(label("weird"), "weird", "unknown renders as itself");
});

/* ------------------------------------------------------------------ */
/* F3 bites: in-flight MUTATIONS are invalidated by independent resets  */
/* ------------------------------------------------------------------ */

test("F3 bite: a late SUCCESSFUL Block-All after an independent reset renders nothing and fires no GET", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture()
  ]) });
  openSchedule(f);
  await flush();

  /* Block-All goes in flight... */
  f.doc._elements["schedule-day"].value = "2026-08-21";
  const inflight = f.data.queueDeferred("blockAllOpenSlots");
  f.doc._elements["schedule-block-all"].trigger("click");

  /* ...an INDEPENDENT page/session reset occurs (app-glue logout path -
   * NOT a session-loss outcome of the mutation itself)... */
  f.pages.reset();
  const getCallsAfterReset = f.data.calls.getSchedule.length;

  /* ...and the mutation then resolves ok:true WITH booked_remaining. */
  inflight.resolve({ ok: true, data: { day: "2026-08-21", blocked_count: 4,
    booked_remaining: [
      { start_datetime: "2026-08-21T16:00:00Z",
        end_datetime: "2026-08-21T17:00:00Z" }
    ] } });
  await flush();

  assertEqual(f.doc._elements["schedule-bulk-feedback"].textContent, "",
    "no blocked-count feedback repopulated the wiped page");
  assertEqual(f.doc._elements["schedule-booked-remaining"].textContent, "",
    "no booked_remaining windows repopulated the wiped page");
  assertEqual(f.doc._elements["schedule-list"].children.length, 0,
    "no schedule rows reappeared");
  assertEqual(f.data.calls.getSchedule.length, getCallsAfterReset,
    "NO post-reset authoritative GET was initiated");
  assertEqual(f.sessionLost.length, 0,
    "an ok outcome never routes through the session-loss path");
});

test("F3 bite: a late successful per-slot Block after an independent reset renders nothing and fires no GET", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture()
  ]) });
  openSchedule(f);
  await flush();

  const row = f.doc._elements["schedule-list"].children[0].children[0];
  const inflight = f.data.queueDeferred("blockScheduleSlot");
  row.children[row.children.length - 1].trigger("click");

  f.pages.reset();
  const getCallsAfterReset = f.data.calls.getSchedule.length;

  inflight.resolve({ ok: true, data: slotFixture({ status: "blocked" }) });
  await flush();

  assertEqual(f.doc._elements["schedule-action-feedback"].textContent, "",
    "no per-slot feedback repopulated the wiped page");
  assertEqual(f.doc._elements["schedule-list"].children.length, 0,
    "no schedule rows reappeared");
  assertEqual(f.data.calls.getSchedule.length, getCallsAfterReset,
    "NO post-reset authoritative GET was initiated");
});

test("F5 bite: an older successful mutation renders no body but triggers one final authoritative refresh", async () => {
  const f = makePages();
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture()
  ]) });
  openSchedule(f);
  await flush();

  /* Older mutation: per-slot Block, left in flight. */
  const row = f.doc._elements["schedule-list"].children[0].children[0];
  const older = f.data.queueDeferred("blockScheduleSlot");
  row.children[row.children.length - 1].trigger("click");

  /* Newer mutation: Publish begins (a NEWER generation now owns feedback),
   * resolves, renders its feedback, and triggers ITS refresh. */
  f.doc._elements["schedule-day"].value = "2026-08-21";
  f.doc._elements["schedule-open"].value = "09:00";
  f.doc._elements["schedule-end"].value = "10:00";
  f.data.queue("publishScheduleDay", { ok: true, data: [
    slotFixture({ slot_id: "new-1" })
  ] });
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ slot_id: "new-1" })
  ]) });
  f.doc._elements["schedule-publish"].trigger("click");
  await flush();
  const publishFeedback =
    f.doc._elements["schedule-publish-feedback"].textContent;
  assert(publishFeedback.indexOf("Published 1") !== -1,
    "the newer mutation's feedback rendered");
  assertEqual(f.data.calls.getSchedule.length, 2,
    "the newer mutation's refresh ran");

  /* The OLDER mutation resolves LAST with SUCCESS: it renders nothing of
   * its own and does not touch the newer feedback, but its commit changed
   * the server - so it MUST trigger one final authoritative refresh (F5). */
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ slot_id: "new-1" }),
    slotFixture({ slot_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      status: "blocked", start_datetime: "2026-08-21T15:00:00Z",
      end_datetime: "2026-08-21T16:00:00Z" })
  ]) });
  older.resolve({ ok: true, data: slotFixture({ status: "blocked" }) });
  await flush();
  assertEqual(f.doc._elements["schedule-action-feedback"].textContent, "",
    "the older mutation wrote no feedback");
  assertEqual(f.doc._elements["schedule-publish-feedback"].textContent,
    publishFeedback, "the newer mutation's feedback stands untouched");
  assertEqual(f.data.calls.getSchedule.length, 3,
    "the older SUCCESS triggered exactly one final authoritative refresh");
  assertEqual(renderedRowTexts(f.doc).length, 2,
    "the final refresh rendered the reconciled truth");
});

test("F5 bite (mandated sequence): concurrent Block A / Block B end with the final grid reflecting BOTH commits", async () => {
  const f = makePages();
  /* 1. Slots A and B initially available. */
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ slot_id: "slot-a" }),
    slotFixture({ slot_id: "slot-b",
      start_datetime: "2026-08-21T15:00:00Z",
      end_datetime: "2026-08-21T16:00:00Z" })
  ]) });
  openSchedule(f);
  await flush();

  const items = f.doc._elements["schedule-list"].children;
  const buttonOf = (index) => {
    const rowElement = items[index].children[0];
    return rowElement.children[rowElement.children.length - 1];
  };

  /* 2. Block A starts (older generation) and remains in flight. */
  const deferredA = f.data.queueDeferred("blockScheduleSlot");
  buttonOf(0).trigger("click");
  /* 3. Block B starts afterward (newer generation). */
  const deferredB = f.data.queueDeferred("blockScheduleSlot");
  buttonOf(1).trigger("click");
  assertEqual(f.data.calls.blockScheduleSlot.length, 2,
    "both independent per-slot actions were permitted");
  assertEqual(f.data.calls.blockScheduleSlot[0], "slot-a", "A first");
  assertEqual(f.data.calls.blockScheduleSlot[1], "slot-b", "B second");

  /* 4. B commits FIRST; its authoritative GET renders A=available,
   * B=blocked (the server truth at that instant). */
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ slot_id: "slot-a" }),
    slotFixture({ slot_id: "slot-b", status: "blocked",
      start_datetime: "2026-08-21T15:00:00Z",
      end_datetime: "2026-08-21T16:00:00Z" })
  ]) });
  deferredB.resolve({ ok: true, data: slotFixture({ slot_id: "slot-b",
    status: "blocked" }) });
  await flush();
  let rows = renderedRowTexts(f.doc);
  assert(rows[0].indexOf("Open") !== -1 && rows[1].indexOf("Blocked") !== -1,
    "intermediate render: A open, B blocked");
  assertEqual(f.data.calls.getSchedule.length, 2, "B's refresh ran");

  /* 5-9. A commits LAST. Its response body renders nothing, but its
   * SUCCESS must initiate exactly ONE final authoritative GET whose
   * response - the true final server state A=blocked, B=blocked - is
   * what the office ends up seeing. */
  f.data.queue("getSchedule", { ok: true, data: scheduleBody([
    slotFixture({ slot_id: "slot-a", status: "blocked" }),
    slotFixture({ slot_id: "slot-b", status: "blocked",
      start_datetime: "2026-08-21T15:00:00Z",
      end_datetime: "2026-08-21T16:00:00Z" })
  ]) });
  deferredA.resolve({ ok: true, data: slotFixture({ slot_id: "slot-a",
    status: "blocked" }) });
  await flush();
  assertEqual(f.data.calls.getSchedule.length, 3,
    "A's completion initiated exactly one final authoritative GET");
  rows = renderedRowTexts(f.doc);
  assert(rows[0].indexOf("Blocked") !== -1 &&
    rows[1].indexOf("Blocked") !== -1,
    "the final rendered grid reflects BOTH server-side mutations");
  assertEqual(f.doc._elements["schedule-action-feedback"].textContent, "",
    "no mutation response body wrote feedback of its own");
  assertEqual(f.sessionLost.length, 0, "no session-loss path was touched");
});

h.runRegisteredTests("test_portal_schedule_page").then((result) => {
  if (result.failed > 0) { process.exitCode = 1; }
});
