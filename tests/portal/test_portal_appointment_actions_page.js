/*
 * test_portal_appointment_actions_page.js - P5-A Appointments ACTIONS page
 * proofs (Portal Appointment Actions v1, contract v1.1; corrections C4/C5).
 *
 * Drives the REAL portal-pages.js in a Node vm over a fake document and a
 * scripted fake data layer (the test_portal_schedule_page.js technique) and
 * proves the mutation-lifecycle contract the now-actionable Appointments page
 * needs:
 *   - RENDER: a pending row offers Confirm + Cancel, a confirmed row offers
 *     only Cancel, and every terminal status offers no control.
 *   - AUTHORITATIVE-ONLY: a Confirm/Cancel success renders NO optimistic
 *     row; it re-GETs and renders the server's truth, and shows the success
 *     line.
 *   - TWO-CLICK CANCEL (C4): the first Cancel click arms THIS appointment
 *     (no network), the second performs it; arming never calls the data layer.
 *   - DUPLICATE-SUBMIT (C5): a second submit while an appointment's action is
 *     in flight is suppressed, BOTH that row's controls are disabled, and one
 *     row's busy state never disables another row's controls.
 *   - STALE-READ ROLLBACK (C4): a window GET issued BEFORE a mutation,
 *     resolving AFTER the mutation's refresh, is discarded - rendered rows
 *     never roll back.
 *   - STALE LIFECYCLE (C4): a mutation resolving after page re-entry renders
 *     nothing and triggers no GET.
 *   - SESSION LOSS: a mutation resolving signed_out/unauthorized wipes every
 *     rendered value and hands back to the sign-in flow.
 *   - FAILURE REFRESH: conflict / not_found / unavailable each show the
 *     honest line AND re-GET authoritative state.
 *   - ARMED-CLEAR: an authoritative refresh disarms a previously-armed row.
 *
 * Every bite here FAILS against untouched fd967de: the Appointments page has
 * no action controls, no data.confirmAppointment/cancelAppointment, and no
 * mutation lifecycle there.
 *
 * Run: node tests/portal/test_portal_appointment_actions_page.js
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
/* Fixtures (the test_portal_schedule_page.js technique)               */
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

/* Every id portal-pages.js reads (index.html contract), including the P5-A
 * appt-action-feedback line. */
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
    publishScheduleDay: (d, o, c, m) =>
      next("publishScheduleDay", { d, o, c, m }),
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

let APPT_SEQ = 0;
function apptMember(overrides) {
  APPT_SEQ += 1;
  return Object.assign({
    appointment_id: "id-" + APPT_SEQ,
    patient_name: "Kevin Alvarado",
    patient_phone: "516-555-1234",
    patient_email: null,
    new_or_returning: "new",
    reason: "cleaning",
    urgency: "routine",
    start_datetime: "2026-07-16T14:00:00Z",
    end_datetime: "2026-07-16T14:45:00Z",
    status: "pending",
    confirmed_at: null,
    source: "mia_widget",
    notification_outcome: "pending",
    internal_note: null   /* 4B1: part of the exact approved member */
  }, overrides || {});
}

function apptBody(appointments, overrides) {
  return Object.assign({
    timezone_name: "America/New_York",
    start_day: "2026-07-16",
    end_day: "2026-07-22",
    appointments: appointments
  }, overrides || {});
}

function openAppointments(f) {
  f.doc._elements["nav-appointments"].trigger("click");
}

/* The action buttons of one rendered row (the last child of the row div is
 * the portal-appt-actions group). Returns [] when the row has no controls. */
function rowButtons(f, index) {
  const item = f.doc._elements["appointments-list"].children[index];
  const row = item.children[0];
  const group = row.children[row.children.length - 1];
  if (!group || group.className !== "portal-appt-actions") { return []; }
  return group.children;
}

function buttonByLabel(buttons, label) {
  for (let i = 0; i < buttons.length; i++) {
    if (buttons[i].textContent === label) { return buttons[i]; }
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* Rendering                                                            */
/* ------------------------------------------------------------------ */

test("appointments: controls follow the status allow-list", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ status: "pending" }),
    apptMember({ status: "confirmed" }),
    apptMember({ status: "cancelled" }),
    apptMember({ status: "completed" }),
    apptMember({ status: "no_show" })
  ]) });
  openAppointments(f);
  await flush();

  const labels = (i) => rowButtons(f, i).map((b) => b.textContent);
  assertEqual(labels(0).join(","), "Confirm,Cancel", "pending: Confirm+Cancel");
  assertEqual(labels(1).join(","), "Cancel", "confirmed: Cancel only");
  assertEqual(labels(2).length, 0, "cancelled: no controls");
  assertEqual(labels(3).length, 0, "completed: no controls");
  assertEqual(labels(4).length, 0, "no_show: no controls");
});

/* ------------------------------------------------------------------ */
/* Confirm - authoritative refresh, never optimistic                    */
/* ------------------------------------------------------------------ */

test("appointments: Confirm re-GETs authoritative state and shows success", async () => {
  const f = makePages();
  const appt = apptMember({ appointment_id: "aptX", status: "pending" });
  f.data.queue("getAppointments", { ok: true, data: apptBody([appt]) });
  openAppointments(f);
  await flush();

  f.data.queue("confirmAppointment", { ok: true,
    data: apptMember({ appointment_id: "aptX", status: "confirmed",
      confirmed_at: "2026-07-16T14:01:00Z" }) });
  /* the authoritative re-GET after the action */
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptX", status: "confirmed",
      confirmed_at: "2026-07-16T14:01:00Z" }) ]) });

  buttonByLabel(rowButtons(f, 0), "Confirm").trigger("click");
  await flush();

  assertEqual(f.data.calls.confirmAppointment.length, 1, "one confirm call");
  assertEqual(f.data.calls.confirmAppointment[0], "aptX", "confirm by id");
  assertEqual(f.data.calls.getAppointments.length, 2,
    "an authoritative re-GET followed the action");
  assertEqual(f.doc._elements["appt-action-feedback"].textContent,
    "Appointment confirmed.", "success line shown");
  assertEqual(rowButtons(f, 0).map((b) => b.textContent).join(","), "Cancel",
    "the re-GET row (confirmed) offers only Cancel");
});

/* ------------------------------------------------------------------ */
/* Two-click Cancel (C4)                                                */
/* ------------------------------------------------------------------ */

test("appointments: Cancel arms on the first click, performs on the second", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptC", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  const cancelFirst = buttonByLabel(rowButtons(f, 0), "Cancel");
  cancelFirst.trigger("click");           /* ARM only - no network */
  assertEqual(f.data.calls.cancelAppointment.length, 0,
    "arming performs NO cancel");
  assertEqual(cancelFirst.textContent, "Confirm cancel", "button re-labels");
  assertEqual(f.doc._elements["appt-action-feedback"].textContent,
    "Click Cancel again to confirm.", "armed prompt shown");

  f.data.queue("cancelAppointment", { ok: true,
    data: apptMember({ appointment_id: "aptC", status: "cancelled" }) });
  f.data.queue("getAppointments", { ok: true, data: apptBody([]) });

  cancelFirst.trigger("click");           /* PERFORM */
  await flush();
  assertEqual(f.data.calls.cancelAppointment.length, 1, "one cancel call");
  assertEqual(f.data.calls.cancelAppointment[0], "aptC", "cancel by id");
  assertEqual(f.doc._elements["appt-action-feedback"].textContent,
    "Appointment cancelled.", "success line shown");
});

/* ------------------------------------------------------------------ */
/* Duplicate-submit and per-row independence (C5)                       */
/* ------------------------------------------------------------------ */

test("appointments: a second submit while in flight is suppressed; both row controls disabled", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptD", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  const buttons = rowButtons(f, 0);
  const confirmBtn = buttonByLabel(buttons, "Confirm");
  const cancelBtn = buttonByLabel(buttons, "Cancel");
  const deferred = f.data.queueDeferred("confirmAppointment");

  confirmBtn.trigger("click");            /* in flight */
  assert(confirmBtn.disabled === true && cancelBtn.disabled === true,
    "BOTH row controls disabled while in flight (C5)");
  confirmBtn.trigger("click");            /* duplicate - suppressed */
  cancelBtn.trigger("click");             /* sibling - suppressed, not armed */
  assertEqual(f.data.calls.confirmAppointment.length, 1,
    "no duplicate confirm submitted");
  assertEqual(f.data.calls.cancelAppointment.length, 0,
    "the sibling Cancel neither armed nor submitted while busy");

  deferred.resolve({ ok: true,
    data: apptMember({ appointment_id: "aptD", status: "confirmed",
      confirmed_at: "2026-07-16T14:01:00Z" }) });
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptD", status: "confirmed",
      confirmed_at: "2026-07-16T14:01:00Z" }) ]) });
  await flush();
  assertEqual(f.data.calls.getAppointments.length, 2, "one authoritative re-GET");
});

test("appointments: one row's busy state never disables another row's controls", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptA", status: "pending" }),
    apptMember({ appointment_id: "aptB", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  const aButtons = rowButtons(f, 0);
  const bButtons = rowButtons(f, 1);
  const deferred = f.data.queueDeferred("confirmAppointment");
  buttonByLabel(aButtons, "Confirm").trigger("click");   /* row A busy */

  assert(aButtons[0].disabled === true, "row A control disabled");
  assert(bButtons[0].disabled === false && bButtons[1].disabled === false,
    "row B controls stay enabled (independence)");

  deferred.resolve({ ok: true,
    data: apptMember({ appointment_id: "aptA", status: "confirmed" }) });
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptA", status: "confirmed" }),
    apptMember({ appointment_id: "aptB", status: "pending" }) ]) });
  await flush();
});

/* ------------------------------------------------------------------ */
/* Stale-read rollback (C4)                                             */
/* ------------------------------------------------------------------ */

test("appointments: a pre-mutation window GET resolving late is discarded", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "old", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  /* A window navigation GET issued BEFORE any mutation (captures gen 0). */
  const staleGet = f.data.queueDeferred("getAppointments");
  f.doc._elements["appt-next"].trigger("click");

  /* Now a mutation whose authoritative refresh renders the NEW truth. */
  f.data.queue("confirmAppointment", { ok: true,
    data: apptMember({ appointment_id: "old", status: "confirmed" }) });
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "fresh", status: "confirmed" }) ]) });
  buttonByLabel(rowButtons(f, 0), "Confirm").trigger("click");
  await flush();

  const freshName = f.doc._elements["appointments-list"].children[0]
    .children[0].children[0].textContent;
  /* Resolve the STALE window GET LAST - it must be discarded. */
  staleGet.resolve({ ok: true, data: apptBody([
    apptMember({ appointment_id: "stale", patient_name: "STALE ROLLBACK",
      status: "pending" }) ]) });
  await flush();

  const afterName = f.doc._elements["appointments-list"].children[0]
    .children[0].children[0].textContent;
  assertEqual(afterName, freshName,
    "the stale window GET did not roll the rendered list back");
  assert(afterName.indexOf("STALE ROLLBACK") === -1, "stale row not rendered");
});

/* ------------------------------------------------------------------ */
/* Stale lifecycle (C4)                                                 */
/* ------------------------------------------------------------------ */

test("appointments: a mutation resolving after page re-entry renders nothing", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptL", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  const deferred = f.data.queueDeferred("confirmAppointment");
  buttonByLabel(rowButtons(f, 0), "Confirm").trigger("click");  /* in flight */

  /* Page re-entry bumps the lifecycle; its own default GET resolves. */
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptL", status: "pending" }) ]) });
  openAppointments(f);
  await flush();
  const getsAfterReentry = f.data.calls.getAppointments.length;

  deferred.resolve({ ok: true,
    data: apptMember({ appointment_id: "aptL", status: "confirmed" }) });
  await flush();

  assertEqual(f.data.calls.getAppointments.length, getsAfterReentry,
    "the stale mutation triggered NO authoritative GET (lifecycle mismatch)");
  assertEqual(f.doc._elements["appt-action-feedback"].textContent, "",
    "the stale mutation rendered no feedback");
});

/* ------------------------------------------------------------------ */
/* Session loss                                                         */
/* ------------------------------------------------------------------ */

test("appointments: a mutation resolving signed_out wipes and hands back", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptS", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  f.data.queue("confirmAppointment", { ok: false, state: "signed_out" });
  buttonByLabel(rowButtons(f, 0), "Confirm").trigger("click");
  await flush();

  assertEqual(f.sessionLost.length, 1, "session loss handed back once");
  assertEqual(f.sessionLost[0], "signed_out", "the reason propagated");
  assertEqual(f.doc._elements["appointments-list"].children.length, 0,
    "rendered rows wiped");
});

/* ------------------------------------------------------------------ */
/* Failure refresh                                                      */
/* ------------------------------------------------------------------ */

test("appointments: a 409 conflict shows the conflict line and re-GETs", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptK", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  f.data.queue("confirmAppointment", { ok: false, state: "conflict" });
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptK", status: "cancelled" }) ]) });
  buttonByLabel(rowButtons(f, 0), "Confirm").trigger("click");
  await flush();

  assert(f.doc._elements["appt-action-feedback"].textContent
    .indexOf("changed somewhere else") !== -1, "conflict line shown");
  assertEqual(f.data.calls.getAppointments.length, 2, "authoritative re-GET");
});

/* ------------------------------------------------------------------ */
/* Armed-clear on authoritative refresh                                 */
/* ------------------------------------------------------------------ */

test("appointments: an authoritative refresh disarms a previously-armed row", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptR", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  buttonByLabel(rowButtons(f, 0), "Cancel").trigger("click");   /* arm */
  assertEqual(buttonByLabel(rowButtons(f, 0), "Confirm cancel") !== null, true,
    "row is armed");

  /* A window navigation refresh (not the cancel) rebuilds the rows. */
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "aptR", status: "pending" }) ]) });
  f.doc._elements["appt-next"].trigger("click");
  await flush();

  assertEqual(buttonByLabel(rowButtons(f, 0), "Cancel") !== null, true,
    "the rebuilt row is disarmed (Cancel again)");
  assertEqual(buttonByLabel(rowButtons(f, 0), "Confirm cancel"), null,
    "no armed control survives the refresh");
});

/* ------------------------------------------------------------------ */
/* F4 - stale mutation must not clear a newer same-row busy owner       */
/* ------------------------------------------------------------------ */

test("appointments: a stale same-row completion does not clear a newer busy owner", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "X", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  /* A1 starts on X (deferred) - takes the row's busy ownership. */
  const a1 = f.data.queueDeferred("confirmAppointment");
  buttonByLabel(rowButtons(f, 0), "Confirm").trigger("click");

  /* Page re-entry invalidates A1 (bumps lifecycle, clears actionBusy) and
   * loads its own fresh default window. */
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "X", status: "pending" }) ]) });
  openAppointments(f);
  await flush();

  /* A2 starts on the SAME appointment X (deferred) and stays pending. */
  const a2 = f.data.queueDeferred("confirmAppointment");
  buttonByLabel(rowButtons(f, 0), "Confirm").trigger("click");
  assertEqual(f.data.calls.confirmAppointment.length, 2, "A1 and A2 both submitted");

  /* An authoritative window GET happens while A2 is still in flight. */
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "X", status: "pending" }) ]) });
  f.doc._elements["appt-next"].trigger("click");
  await flush();
  const getsBeforeStale = f.data.calls.getAppointments.length;

  /* A1 resolves LATE. It must render nothing, trigger no GET (stale
   * lifecycle), AND must not clear A2's ownership. */
  a1.resolve({ ok: true,
    data: apptMember({ appointment_id: "X", status: "confirmed" }) });
  await flush();

  assertEqual(f.data.calls.getAppointments.length, getsBeforeStale,
    "the stale A1 completion triggered NO authoritative GET");
  const btns = rowButtons(f, 0);
  assert(btns.length === 2 && btns[0].disabled === true && btns[1].disabled === true,
    "A2 still owns the row: BOTH controls remain disabled after A1's stale completion");

  /* A duplicate same-row submit MUST still be suppressed (A2 owns busy). */
  const before = f.data.calls.confirmAppointment.length;
  btns[0].trigger("click");
  assertEqual(f.data.calls.confirmAppointment.length, before,
    "duplicate submit remains suppressed while A2 is in flight");

  /* A2 resolves normally -> authoritative refresh. */
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ appointment_id: "X", status: "confirmed" }) ]) });
  a2.resolve({ ok: true,
    data: apptMember({ appointment_id: "X", status: "confirmed" }) });
  await flush();
  assert(f.data.calls.getAppointments.length > getsBeforeStale,
    "A2's own completion triggered the authoritative GET");
});

(async () => {
  const summary = await h.runRegisteredTests("test_portal_appointment_actions_page");
  process.exitCode = summary.failed === 0 ? 0 : 1;
})();


/* ------------------------------------------------------------------ */
/* PHASE 3A Slice 4A: list badge - staff bookings show no permanent    */
/* "Notification pending" noise; every other source keeps its badge.   */
/* ------------------------------------------------------------------ */

function rowBadgeTexts(f, index) {
  const item = f.doc._elements["appointments-list"].children[index];
  const row = item.children[0];
  const texts = [];
  for (const child of row.children) {
    if (child.className === "portal-badge") { texts.push(child.textContent); }
  }
  return texts;
}

test("slice 4A: a staff booking's list row carries no notification badge", async () => {
  const f = makePages();
  f.data.queue("getAppointments", { ok: true, data: apptBody([
    apptMember({ source: "portal_staff", status: "confirmed",
      notification_outcome: "pending" }),
    apptMember({ source: "mia_widget", notification_outcome: "pending" })
  ]) });
  openAppointments(f);
  await flush();
  const staffBadges = rowBadgeTexts(f, 0);
  assert(staffBadges.indexOf("Notification pending") === -1,
    "no permanent pending noise for a staff booking");
  assert(staffBadges.indexOf("Confirmed") !== -1,
    "its real status badge still renders");
  const widgetBadges = rowBadgeTexts(f, 1);
  assert(widgetBadges.indexOf("Notification pending") !== -1,
    "every other source keeps its honest notification badge");
});
