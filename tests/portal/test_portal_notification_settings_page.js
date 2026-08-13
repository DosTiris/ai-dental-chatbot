/*
 * test_portal_notification_settings_page.js - P6-A Settings page proofs.
 *
 * Drives the REAL portal-pages.js in a Node vm over a fake document and a
 * scripted fake data layer (the test_portal_schedule_page.js technique) and
 * proves the notification-settings page contract:
 *
 *   - opening Settings loads the office's own destinations authoritatively
 *     and renders them plus the DERIVED read-only status lines;
 *   - Save sends the trimmed destinations, blanks as null, and the OPAQUE
 *     token VERBATIM; it applies ONLY the authoritative response (never the
 *     optimistic input) and shows the saved wording;
 *   - the both-empty guard blocks the request locally (no data call);
 *   - a duplicate Save while one is in flight is suppressed;
 *   - a 409 conflict refreshes authoritative state and shows the conflict
 *     line;
 *   - a Save resolving AFTER an independent reset renders nothing and cannot
 *     repopulate the wiped page (generation bite);
 *   - a Save resolving signed_out wipes every rendered value and hands back;
 *   - reset wipes every rendered settings value (shared-computer rule D10).
 *
 * Run: node tests/portal/test_portal_notification_settings_page.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const h = require("./portal_test_harness.js");
const { test, assert, assertEqual } = h;

const PAGES_PATH = path.join(__dirname, "..", "..", "static", "portal",
  "portal-pages.js");

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

/* Every id portal-pages.js reads (index.html contract, settings-complete). */
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
    "unblockScheduleSlot", "blockAllOpenSlots",
    "getNotificationSettings", "putNotificationSettings"];
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
    getNotificationSettings: () => next("getNotificationSettings", null),
    putNotificationSettings: (email, phone, token) =>
      next("putNotificationSettings", { email, phone, token }),
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
  return { pages, doc, data, sessionLost };
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

const TOKEN = "2026-08-13T12:00:00.123456Z";

function settingsBody(overrides) {
  return Object.assign({
    notification_email: "office@example.com",
    notification_phone: "+15550001111",
    notification_settings_updated_at: TOKEN
  }, overrides || {});
}

function openSettings(f) {
  f.doc._elements["nav-settings"].trigger("click");
}

/* ------------------------------------------------------------------ */
/* Rendering / load                                                    */
/* ------------------------------------------------------------------ */

test("settings: opening loads destinations and renders derived status", async () => {
  const f = makePages();
  f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
  openSettings(f);
  await flush();

  assertEqual(f.data.calls.getNotificationSettings.length, 1, "one GET");
  assert(f.doc._elements["page-settings"].hidden === false,
    "settings page visible");
  assertEqual(f.doc._elements["settings-email"].value, "office@example.com",
    "email input populated");
  assertEqual(f.doc._elements["settings-phone"].value, "+15550001111",
    "phone input populated");
  assert(f.doc._elements["settings-email-status"].textContent
    .indexOf("configured") !== -1, "email status derived");
  assert(f.doc._elements["settings-sms-status"].textContent
    .indexOf("configured") !== -1, "sms status derived");
  assertEqual(f.doc._elements["settings-state"].textContent, "",
    "state line cleared on success");
});

test("settings: null destinations render as empty inputs + 'not configured'",
  async () => {
    const f = makePages();
    f.data.queue("getNotificationSettings", { ok: true, data: settingsBody(
      { notification_email: null, notification_phone: null,
        notification_settings_updated_at: null }) });
    openSettings(f);
    await flush();
    assertEqual(f.doc._elements["settings-email"].value, "", "email empty");
    assertEqual(f.doc._elements["settings-phone"].value, "", "phone empty");
    assert(f.doc._elements["settings-email-status"].textContent
      .indexOf("not configured") !== -1, "email not configured");
  });

/* ------------------------------------------------------------------ */
/* Save                                                                */
/* ------------------------------------------------------------------ */

test("settings: Save sends trimmed values + verbatim token, then the "
  + "authoritative GET (not the PUT body) is the final state and token (F2)",
  async () => {
    const f = makePages();
    f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
    openSettings(f);
    await flush();

    /* Edit the inputs (with surrounding whitespace to prove trimming). */
    f.doc._elements["settings-email"].value = "  new@example.com  ";
    f.doc._elements["settings-phone"].value = "  516-555-7777  ";
    /* The PUT reports value/token A ... */
    const putBodyA = settingsBody({
      notification_email: "new@example.com",
      notification_phone: "516-555-7777",
      notification_settings_updated_at: "2026-08-13T12:34:56.111111Z"
    });
    f.data.queue("putNotificationSettings", { ok: true, data: putBodyA });
    /* ... but the authoritative post-mutation GET reports value/token B. The
     * final rendered/stored state MUST be B (F2: the GET is the authority). */
    const getBodyB = settingsBody({
      notification_email: "canonical@example.com",
      notification_phone: "516-555-0000",
      notification_settings_updated_at: "2026-08-13T12:34:56.999999Z"
    });
    f.data.queue("getNotificationSettings", { ok: true, data: getBodyB });

    f.doc._elements["settings-save"].trigger("click");
    await flush();

    const putCall = f.data.calls.putNotificationSettings[0];
    assertEqual(putCall.email, "new@example.com", "email trimmed");
    assertEqual(putCall.phone, "516-555-7777", "phone trimmed");
    assertEqual(putCall.token, TOKEN, "the opaque token is echoed verbatim");
    /* A successful PUT triggered an authoritative GET. */
    assertEqual(f.data.calls.getNotificationSettings.length, 2,
      "the successful PUT performed a post-mutation authoritative GET");
    /* Final state/token come from the GET (B), never the PUT body (A). */
    assertEqual(f.doc._elements["settings-email"].value,
      "canonical@example.com", "final email is from the authoritative GET");
    assertEqual(f.doc._elements["settings-phone"].value, "516-555-0000",
      "final phone is from the authoritative GET");
    assertEqual(f.doc._elements["settings-feedback"].textContent,
      "Notification settings saved.", "saved wording after the GET applies");

    /* The next save uses token B (from the GET), not token A (from the PUT). */
    const putBodyC = settingsBody(
      { notification_settings_updated_at: "2026-08-13T13:00:00.000001Z" });
    f.data.queue("putNotificationSettings", { ok: true, data: putBodyC });
    f.data.queue("getNotificationSettings", { ok: true, data: putBodyC });
    f.doc._elements["settings-save"].trigger("click");
    await flush();
    assertEqual(f.data.calls.putNotificationSettings[1].token,
      "2026-08-13T12:34:56.999999Z",
      "the next save uses the token from the authoritative GET (B), not the PUT");
  });

test("settings: both-empty Save is blocked locally with no data call",
  async () => {
    const f = makePages();
    f.data.queue("getNotificationSettings", { ok: true, data: settingsBody(
      { notification_email: null, notification_phone: null,
        notification_settings_updated_at: null }) });
    openSettings(f);
    await flush();

    f.doc._elements["settings-email"].value = "   ";
    f.doc._elements["settings-phone"].value = "";
    f.doc._elements["settings-save"].trigger("click");
    await flush();

    assertEqual(f.data.calls.putNotificationSettings.length, 0,
      "no PUT was issued");
    assert(f.doc._elements["settings-feedback"].textContent
      .indexOf("at least one") !== -1, "both-empty wording shown");
  });

test("settings: a duplicate Save while one is in flight is suppressed",
  async () => {
    const f = makePages();
    f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
    openSettings(f);
    await flush();

    const deferred = f.data.queueDeferred("putNotificationSettings");
    f.doc._elements["settings-save"].trigger("click");
    await flush();
    f.doc._elements["settings-save"].trigger("click");   /* while in flight */
    await flush();
    assertEqual(f.data.calls.putNotificationSettings.length, 1,
      "only one PUT while the first is in flight");

    /* Resolving the in-flight PUT now triggers the F2 authoritative GET. */
    f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
    deferred.resolve({ ok: true, data: settingsBody() });
    await flush();
    assertEqual(f.data.calls.getNotificationSettings.length, 2,
      "the single successful PUT performed exactly one authoritative GET");
  });

test("settings: a 409 conflict refreshes authoritative state and warns",
  async () => {
    const f = makePages();
    f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
    openSettings(f);
    await flush();

    f.doc._elements["settings-email"].value = "changed@example.com";
    f.data.queue("putNotificationSettings", { ok: false, state: "conflict" });
    /* the conflict handler re-GETs authoritative state */
    f.data.queue("getNotificationSettings", { ok: true, data: settingsBody(
      { notification_email: "server@example.com" }) });
    f.doc._elements["settings-save"].trigger("click");
    await flush();

    assertEqual(f.data.calls.getNotificationSettings.length, 2,
      "conflict triggered an authoritative refresh");
    assertEqual(f.doc._elements["settings-email"].value, "server@example.com",
      "the authoritative value replaced the optimistic edit");
    assert(f.doc._elements["settings-feedback"].textContent
      .indexOf("updated somewhere else") !== -1, "conflict wording survives");
  });

/* ------------------------------------------------------------------ */
/* Lifecycle bites                                                      */
/* ------------------------------------------------------------------ */

test("settings: a Save resolving AFTER a reset renders nothing", async () => {
  const f = makePages();
  f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
  openSettings(f);
  await flush();

  f.doc._elements["settings-email"].value = "pending@example.com";
  const deferred = f.data.queueDeferred("putNotificationSettings");
  f.doc._elements["settings-save"].trigger("click");
  await flush();

  f.pages.reset();                       /* independent reset mid-flight */
  deferred.resolve({ ok: true, data: settingsBody(
    { notification_email: "late@example.com" }) });
  await flush();

  assertEqual(f.doc._elements["settings-email"].value, "",
    "the wiped input was NOT repopulated by the late save");
  assertEqual(f.doc._elements["settings-feedback"].textContent, "",
    "no late success wording appeared");
});

test("settings: a Save resolving signed_out wipes and hands back", async () => {
  const f = makePages();
  f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
  openSettings(f);
  await flush();

  f.doc._elements["settings-email"].value = "x@example.com";
  f.data.queue("putNotificationSettings", { ok: false, state: "signed_out" });
  f.doc._elements["settings-save"].trigger("click");
  await flush();

  assertEqual(f.sessionLost.length, 1, "session loss handed back to the app");
  assertEqual(f.doc._elements["settings-email"].value, "",
    "the rendered destination was wiped");
});

test("settings: reset wipes every rendered settings value (D10)", async () => {
  const f = makePages();
  f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
  openSettings(f);
  await flush();

  f.pages.reset();
  assertEqual(f.doc._elements["settings-email"].value, "", "email wiped");
  assertEqual(f.doc._elements["settings-phone"].value, "", "phone wiped");
  assertEqual(f.doc._elements["settings-email-status"].textContent, "",
    "email status wiped");
  assertEqual(f.doc._elements["settings-sms-status"].textContent, "",
    "sms status wiped");
  assertEqual(f.doc._elements["settings-feedback"].textContent, "",
    "feedback wiped");
});

test("settings: a GET failure renders an honest state line, not a throw",
  async () => {
    const f = makePages();
    f.data.queue("getNotificationSettings", { ok: false, state: "unavailable" });
    openSettings(f);
    await flush();
    assert(f.doc._elements["settings-state"].textContent
      .indexOf("temporarily unavailable") !== -1, "unavailable wording");
  });

test("settings: a post-mutation GET resolving AFTER reset renders nothing (F2)",
  async () => {
    const f = makePages();
    f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
    openSettings(f);
    await flush();

    f.doc._elements["settings-email"].value = "pending@example.com";
    /* PUT succeeds and fires the authoritative GET; that GET is deferred so we
     * can reset the page before it resolves. */
    f.data.queue("putNotificationSettings", { ok: true, data: settingsBody() });
    const deferredGet = f.data.queueDeferred("getNotificationSettings");
    f.doc._elements["settings-save"].trigger("click");
    await flush();

    f.pages.reset();                     /* independent reset mid-refresh */
    deferredGet.resolve({ ok: true, data: settingsBody(
      { notification_email: "late@example.com" }) });
    await flush();

    assertEqual(f.doc._elements["settings-email"].value, "",
      "the wiped input was NOT repopulated by the late post-mutation GET");
    assertEqual(f.doc._elements["settings-feedback"].textContent, "",
      "no late saved wording appeared after reset");
  });

test("settings: an older post-mutation GET cannot overwrite a newer load (F2)",
  async () => {
    const f = makePages();
    f.data.queue("getNotificationSettings", { ok: true, data: settingsBody() });
    openSettings(f);
    await flush();

    /* Save1 succeeds; its authoritative GET (GET-1) is deferred and STALE. */
    f.data.queue("putNotificationSettings", { ok: true, data: settingsBody() });
    const staleGet = f.data.queueDeferred("getNotificationSettings");
    f.doc._elements["settings-save"].trigger("click");
    await flush();

    /* A NEWER load starts (re-opening Settings) before GET-1 resolves; GET-2
     * is fresh and resolves first. */
    const freshGet = f.data.queueDeferred("getNotificationSettings");
    openSettings(f);
    await flush();
    freshGet.resolve({ ok: true, data: settingsBody(
      { notification_email: "fresh@example.com",
        notification_settings_updated_at: "2026-08-13T14:00:00.000000Z" }) });
    await flush();

    /* The STALE GET-1 resolves late; it must be discarded (older sequence). */
    staleGet.resolve({ ok: true, data: settingsBody(
      { notification_email: "stale@example.com",
        notification_settings_updated_at: "2026-08-13T11:00:00.000000Z" }) });
    await flush();

    assertEqual(f.doc._elements["settings-email"].value, "fresh@example.com",
      "the newer load's value stands; the older post-mutation GET was dropped");
  });

h.runRegisteredTests("test_portal_notification_settings_page").then((result) => {
  if (result.failed > 0) { process.exitCode = 1; }
});
