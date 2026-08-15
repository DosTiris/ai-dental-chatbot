/*
 * test_portal_recurring_schedule_page.js - P4-B Recurring Schedule page proofs
 * (v1.0.1: previewed-token pinning, authoritative-GET-after-Save, 409 recovery,
 * closure ranges, per-day Preview/Apply breakdowns).
 *
 * Drives the REAL portal-pages.js recurring lifecycle in Node over a fake
 * document and a scripted fake data layer. Proves:
 *   - open loads config, renders 7 rows + slot_minutes + closures; Apply is
 *     DISABLED until a Preview (F2 - a config token is NOT enough);
 *   - Save PUTs collected config with the token VERBATIM, then follows the P6-A
 *     lifecycle (authoritative GET is the final rendered state/token) and
 *     invalidates any prior Preview;
 *   - a duplicate Save while one is in flight is suppressed;
 *   - a 409 on Save refreshes authoritatively and warns;
 *   - Preview POSTs {} , pins the returned token, enables Apply, and renders a
 *     per-day outcome breakdown (F3);
 *   - editing a config field after Preview invalidates the previewed token so
 *     Apply is disabled again (F2);
 *   - Apply with no current Preview performs NO data call (F2);
 *   - Apply sends the PREVIEWED token VERBATIM, renders per-day outcomes +
 *     totals (F3), then invalidates the Preview (a further Apply needs another);
 *   - an Apply stale 409 refreshes authoritatively and requires a fresh Preview
 *     (recoverable - no permanent stale loop);
 *   - a single-date closure adds {date}; a start+end closure adds {start,end} (F3);
 *   - reset wipes every rendered recurring value incl. the previewed token (D10);
 *   - a GET resolving AFTER reset renders nothing (generation guard, D10).
 *
 * Run: node tests/portal/test_portal_recurring_schedule_page.js
 */
"use strict";

const path = require("path");
const h = require("./portal_test_harness.js");
const { test, assert, assertEqual } = h;

const { createMiaPortalPages } = require(
  path.join(__dirname, "..", "..", "static", "portal", "portal-pages.js"));

function makeClassList() {
  const set = new Set();
  return {
    toggle(name, force) {
      if (force === undefined) { set.has(name) ? set.delete(name) : set.add(name); }
      else if (force) { set.add(name); } else { set.delete(name); }
    },
    contains(name) { return set.has(name); }
  };
}

function makeElement(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    children: [], listeners: {}, classList: makeClassList(),
    textContent: "", className: "", value: "", type: "",
    hidden: false, disabled: false, checked: false, attrs: {},
    get firstChild() { return el.children.length ? el.children[0] : null; },
    appendChild(c) { el.children.push(c); return c; },
    removeChild(c) { const i = el.children.indexOf(c); if (i !== -1) el.children.splice(i, 1); return c; },
    setAttribute(k, v) { el.attrs[k] = v; },
    getAttribute(k) { return el.attrs[k]; },
    addEventListener(t, fn) { (el.listeners[t] = el.listeners[t] || []).push(fn); },
    trigger(t) { (el.listeners[t] || []).forEach((fn) => fn({})); }
  };
  return el;
}

function makeDocument() {
  const byId = {};
  return {
    _els: byId,
    getElementById(id) { if (!byId[id]) byId[id] = makeElement("div"); return byId[id]; },
    createElement(tag) { return makeElement(tag); }
  };
}

function deferred() { let resolve; const p = new Promise((r) => { resolve = r; }); return { p, resolve }; }
function flush() { return new Promise((r) => setTimeout(r, 0)); }

function makeData() {
  const calls = { get: [], put: [], preview: [], apply: [] };
  const q = { get: [], put: [], preview: [], apply: [] };
  function take(kind) {
    const d = q[kind].shift() || (function () { const x = deferred();
      x.resolve({ ok: false, state: "unavailable" }); return x; })();
    return d.p;
  }
  return {
    _calls: calls, _q: q,
    getRecurringSchedule() { calls.get.push(true); return take("get"); },
    putRecurringSchedule(weekly, minutes, closures, token) {
      calls.put.push({ weekly, minutes, closures, token }); return take("put"); },
    previewRecurringSchedule() { calls.preview.push(true); return take("preview"); },
    applyRecurringSchedule(token) { calls.apply.push({ token }); return take("apply"); },
    getDashboard() { return Promise.resolve({ ok: true, data: {} }); }
  };
}

function cfg(overrides) {
  return Object.assign({
    weekly_hours: {
      mon: { open: true, start: "09:00", end: "17:00" },
      tue: { open: true, start: "09:00", end: "17:00" },
      wed: { open: false, start: null, end: null },
      thu: { open: true, start: "09:00", end: "17:00" },
      fri: { open: true, start: "09:00", end: "17:00" },
      sat: { open: false, start: null, end: null },
      sun: { open: false, start: null, end: null }
    },
    slot_minutes: 30,
    closures: [{ date: "2026-12-25" }],
    schedule_config_updated_at: "2026-08-14T12:00:00.000000Z"
  }, overrides || {});
}

function previewData(overrides) {
  return Object.assign({
    schedule_config_updated_at: "2026-08-14T12:00:00.000000Z",
    start_day: "2026-08-14", end_day: "2026-09-13",
    days: [{ day: "2026-08-17", outcome: "would_publish" },
           { day: "2026-08-19", outcome: "existing_inventory" },
           { day: "2026-08-16", outcome: "weekly_closed_empty" }]
  }, overrides || {});
}

function applyData(overrides) {
  return Object.assign({
    schedule_config_updated_at: "2026-08-14T12:00:00.000000Z",
    start_day: "2026-08-14", end_day: "2026-09-13",
    days: [{ day: "2026-08-17", outcome: "published" },
           { day: "2026-08-19", outcome: "existing_inventory_skipped" }],
    totals: { published_days: 20, closure_blocked_days: 1, existing_inventory_skipped_days: 2 }
  }, overrides || {});
}

function build() {
  const doc = makeDocument();
  const data = makeData();
  const sessionLost = [];
  const pages = createMiaPortalPages({
    data: data, documentRef: doc, onSessionLost: (s) => sessionLost.push(s) });
  return { doc, data, pages, sessionLost };
}

function open(f, outcome) {
  const d = deferred(); f.data._q.get.push(d);
  f.doc.getElementById("nav-recurring").trigger("click");
  d.resolve(outcome);
  return flush();
}

async function openAndPreview(f) {
  await open(f, { ok: true, data: cfg() });
  const pv = deferred(); f.data._q.preview.push(pv);
  f.doc.getElementById("recurring-preview").trigger("click");
  pv.resolve({ ok: true, data: previewData() });
  await flush();
}

test("recurring: open renders 7 rows + slot_minutes; Apply DISABLED until a Preview (F2)", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg() });
  assertEqual(f.doc._els["recurring-hours"].children.length, 7, "7 weekday rows");
  assertEqual(f.doc._els["recurring-slot-minutes"].value, "30", "slot_minutes rendered");
  assert(f.doc._els["recurring-apply"].disabled === true,
    "Apply disabled after open - a config token alone is not a Preview (F2)");
  assert(f.doc._els["recurring-closure-warning"].textContent.indexOf("stay blocked") !== -1,
    "closure-removal warning shown");
});

test("recurring: Preview pins the returned token, enables Apply, and renders a per-day breakdown (F3)", async () => {
  const f = build();
  await openAndPreview(f);
  assert(f.doc._els["recurring-apply"].disabled === false, "Apply enabled after Preview");
  const out = f.doc._els["recurring-preview-output"].textContent;
  assert(out.indexOf("2026-08-17 - would publish") !== -1, "per-DAY row: date + outcome");
  assert(out.indexOf("2026-08-19 - existing inventory") !== -1, "per-DAY row for existing inventory");
  assert(out.indexOf("2026-08-16 - weekly closed") !== -1, "per-DAY row for weekly closed");
  assert(out.indexOf("not one transaction") !== -1, "does not imply an atomic horizon");
});

test("recurring: editing a config field after Preview invalidates it (Apply disabled again) (F2)", async () => {
  const f = build();
  await openAndPreview(f);
  assert(f.doc._els["recurring-apply"].disabled === false, "enabled after Preview");
  f.doc.getElementById("recurring-slot-minutes").trigger("change");   /* edit */
  assert(f.doc._els["recurring-apply"].disabled === true, "editing invalidates the Preview (F2)");
});

test("recurring: Apply with no current Preview performs NO data call (F2)", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg() });   /* token non-null but no Preview */
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply.length, 0, "no apply call without a current Preview");
});

test("recurring: Apply sends the PREVIEWED token VERBATIM, renders per-day + totals, then re-requires Preview (F2/F3)", async () => {
  const f = build();
  await openAndPreview(f);
  const ap = deferred(); f.data._q.apply.push(ap);
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply[0].token, "2026-08-14T12:00:00.000000Z",
    "the PREVIEWED token is echoed verbatim to Apply");
  ap.resolve({ ok: true, data: applyData() });
  await flush();
  const out = f.doc._els["recurring-apply-output"].textContent;
  assert(out.indexOf("published days: 20") !== -1, "totals rendered");
  assert(out.indexOf("2026-08-17 - published") !== -1, "per-DAY row: date + outcome");
  assert(out.indexOf("2026-08-19 - existing inventory - skipped") !== -1, "per-DAY skipped row");
  assert(out.indexOf("day by day") !== -1, "does not imply an atomic horizon");
  assert(f.doc._els["recurring-apply"].disabled === true,
    "a further Apply needs a fresh Preview");
});

test("recurring: Save PUTs verbatim token, then the authoritative GET is final; Preview is invalidated (F2)", async () => {
  const f = build();
  await openAndPreview(f);
  assert(f.doc._els["recurring-apply"].disabled === false, "Apply armed by Preview");
  const putD = deferred(); f.data._q.put.push(putD);
  const getD = deferred(); f.data._q.get.push(getD);   /* authoritative GET after PUT */
  f.doc.getElementById("recurring-save").trigger("click");
  const sent = f.data._calls.put[0];
  assertEqual(sent.token, "2026-08-14T12:00:00.000000Z", "current token echoed verbatim on Save");
  putD.resolve({ ok: true, data: cfg({ schedule_config_updated_at: "IGNORED-PUT-BODY" }) });
  await flush();
  getD.resolve({ ok: true, data: cfg({ schedule_config_updated_at: "2026-08-14T13:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-save-feedback"].textContent.indexOf("saved") !== -1, "saved message");
  assert(f.doc._els["recurring-apply"].disabled === true,
    "Save invalidates the prior Preview (Apply disabled until re-Preview)");
});

test("recurring: a duplicate Save while one is in flight is suppressed", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg() });
  const putD = deferred(); f.data._q.put.push(putD);
  f.doc.getElementById("recurring-save").trigger("click");
  f.doc.getElementById("recurring-save").trigger("click");
  assertEqual(f.data._calls.put.length, 1, "only one PUT in flight");
  const getD = deferred(); f.data._q.get.push(getD);
  putD.resolve({ ok: true, data: cfg() });
  await flush();
  getD.resolve({ ok: true, data: cfg() });
  await flush();
});

test("recurring: a 409 on Save refreshes authoritatively and warns", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg() });
  const putD = deferred(); f.data._q.put.push(putD);
  const getD = deferred(); f.data._q.get.push(getD);
  f.doc.getElementById("recurring-save").trigger("click");
  putD.resolve({ ok: false, state: "conflict" });
  await flush();
  getD.resolve({ ok: true, data: cfg({ slot_minutes: 60 }) });   /* authoritative latest */
  await flush();
  assert(f.doc._els["recurring-save-feedback"].textContent.indexOf("changed elsewhere") !== -1,
    "conflict warning shown");
  assertEqual(f.doc._els["recurring-slot-minutes"].value, "60", "authoritative latest rendered");
});

test("recurring: an Apply stale 409 refreshes and requires a fresh Preview (recoverable, no stale loop)", async () => {
  const f = build();
  await openAndPreview(f);
  const ap = deferred(); f.data._q.apply.push(ap);
  const getD = deferred(); f.data._q.get.push(getD);
  f.doc.getElementById("recurring-apply").trigger("click");
  ap.resolve({ ok: false, state: "conflict" });
  await flush();
  getD.resolve({ ok: true, data: cfg({ schedule_config_updated_at: "2026-08-14T14:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-apply-output"].textContent.indexOf("Preview again") !== -1,
    "asks for a fresh Preview");
  assert(f.doc._els["recurring-apply"].disabled === true, "Apply disabled pending re-Preview");
  /* Recovery: Preview again re-arms Apply against the refreshed token. */
  const pv = deferred(); f.data._q.preview.push(pv);
  f.doc.getElementById("recurring-preview").trigger("click");
  pv.resolve({ ok: true, data: previewData(
    { schedule_config_updated_at: "2026-08-14T14:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === false, "re-Preview re-arms Apply (no stale loop)");
});

test("recurring: closures support a single date and an inclusive range (F3)", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg({ closures: [] }) });
  /* single date */
  f.doc.getElementById("recurring-closure-date").value = "2026-11-26";
  f.doc.getElementById("recurring-closure-end").value = "";
  f.doc.getElementById("recurring-closure-add").trigger("click");
  /* range */
  f.doc.getElementById("recurring-closure-date").value = "2026-12-24";
  f.doc.getElementById("recurring-closure-end").value = "2026-12-26";
  f.doc.getElementById("recurring-closure-add").trigger("click");
  const items = f.doc._els["recurring-closures"].children.map((li) => li.textContent);
  assert(items.some((t) => t.indexOf("2026-11-26") !== -1 && t.indexOf("to") === -1),
    "single-date closure rendered");
  assert(items.some((t) => t.indexOf("2026-12-24 to 2026-12-26") !== -1),
    "range closure rendered");
});

test("recurring: reset wipes every rendered recurring value incl. the previewed token (D10)", async () => {
  const f = build();
  await openAndPreview(f);
  f.pages.reset();
  assertEqual(f.doc._els["recurring-hours"].children.length, 0, "rows cleared");
  assertEqual(f.doc._els["recurring-slot-minutes"].value, "", "slot_minutes cleared");
  /* previewed token wiped -> Apply would perform no call */
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply.length, 0, "no apply after reset (previewed token wiped)");
});

test("recurring: a GET resolving AFTER reset renders nothing (generation guard, D10)", async () => {
  const f = build();
  const getD = deferred(); f.data._q.get.push(getD);
  f.doc.getElementById("nav-recurring").trigger("click");
  f.pages.reset();
  getD.resolve({ ok: true, data: cfg() });
  await flush();
  assertEqual(f.doc._els["recurring-hours"].children.length, 0, "late GET did not repopulate");
});

test("recurring: a pre-first-Save Preview (token null) leaves Apply DISABLED and fires no Apply (R1)", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg({ schedule_config_updated_at: null }) });  /* no Save yet */
  const pv = deferred(); f.data._q.preview.push(pv);
  f.doc.getElementById("recurring-preview").trigger("click");
  pv.resolve({ ok: true, data: previewData({ schedule_config_updated_at: null }) });  /* token null */
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === true,
    "Apply stays disabled while the authoritative token is null (R1)");
  assert(f.doc._els["recurring-preview-output"].textContent.indexOf("Save the schedule") !== -1,
    "Preview tells staff to Save before applying");
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply.length, 0, "clicking a disabled Apply fires NO request (R1)");
  /* After a real Save -> authoritative GET non-null token -> fresh Preview -> Apply enabled. */
  const putD = deferred(); f.data._q.put.push(putD);
  const getD = deferred(); f.data._q.get.push(getD);
  f.doc.getElementById("recurring-save").trigger("click");
  putD.resolve({ ok: true, data: cfg() });
  await flush();
  getD.resolve({ ok: true, data: cfg({ schedule_config_updated_at: "2026-08-14T13:00:00.000000Z" }) });
  await flush();
  const pv2 = deferred(); f.data._q.preview.push(pv2);
  f.doc.getElementById("recurring-preview").trigger("click");
  pv2.resolve({ ok: true, data: previewData({ schedule_config_updated_at: "2026-08-14T13:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === false, "a non-null Preview arms Apply");
});

test("recurring: Preview surfaces preserved booked windows on a closure day (R2)", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg() });
  const pv = deferred(); f.data._q.preview.push(pv);
  f.doc.getElementById("recurring-preview").trigger("click");
  pv.resolve({ ok: true, data: previewData({ days: [
    { day: "2026-12-25", outcome: "closure_empty", would_block_available_held: 0,
      booked_windows: [ { start_utc: "2026-12-25T14:00:00Z", end_utc: "2026-12-25T14:30:00Z" },
                        { start_utc: "2026-12-25T15:00:00Z", end_utc: "2026-12-25T15:30:00Z" } ] } ] }) });
  await flush();
  const out = f.doc._els["recurring-preview-output"].textContent;
  assert(out.indexOf("2026-12-25 - closure (no open slots) - booked preserved: 2") !== -1,
    "closure day shows preserved booked-window count so staff sees they are NOT cancelled");
});

test("recurring: a late Preview A response is ignored after a Save renders token B (T3)", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg() });
  /* Preview A issued (held pending). */
  const pvA = deferred(); f.data._q.preview.push(pvA);
  f.doc.getElementById("recurring-preview").trigger("click");
  /* Save succeeds -> authoritative GET renders token B; Preview is superseded. */
  const putD = deferred(); f.data._q.put.push(putD);
  const getD = deferred(); f.data._q.get.push(getD);
  f.doc.getElementById("recurring-save").trigger("click");
  putD.resolve({ ok: true, data: cfg() });
  await flush();
  getD.resolve({ ok: true, data: cfg({ schedule_config_updated_at: "2026-08-14T13:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === true, "Apply disabled after Save");
  /* Late Preview A resolves AFTER the Save -> must be ignored. */
  pvA.resolve({ ok: true, data: previewData({ schedule_config_updated_at: "2026-08-14T12:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === true,
    "stale Preview A does NOT re-arm Apply (T3)");
  assert(f.doc._els["recurring-preview-output"].textContent.indexOf("2026-08-17 - would publish") === -1,
    "stale Preview A output does NOT render (T3)");
  /* A fresh Preview B against token B re-arms Apply and pins token B. */
  const pvB = deferred(); f.data._q.preview.push(pvB);
  f.doc.getElementById("recurring-preview").trigger("click");
  pvB.resolve({ ok: true, data: previewData({ schedule_config_updated_at: "2026-08-14T13:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === false, "fresh Preview B arms Apply");
  const apD = deferred(); f.data._q.apply.push(apD);
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply[0].token, "2026-08-14T13:00:00.000000Z", "Apply uses token B, not stale A");
});

test("recurring: out-of-order Preview responses keep the latest issued preview (T3)", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg() });
  /* Preview A then Preview B issued; B resolves FIRST, A resolves later. */
  const pvA = deferred(); f.data._q.preview.push(pvA);
  f.doc.getElementById("recurring-preview").trigger("click");
  const pvB = deferred(); f.data._q.preview.push(pvB);
  f.doc.getElementById("recurring-preview").trigger("click");
  pvB.resolve({ ok: true, data: previewData({ schedule_config_updated_at: "2026-08-14T13:00:00.000000Z",
    days: [ { day: "2026-08-20", outcome: "would_publish", would_publish_count: 5 } ] }) });
  await flush();
  pvA.resolve({ ok: true, data: previewData({ schedule_config_updated_at: "2026-08-14T12:00:00.000000Z",
    days: [ { day: "2026-08-17", outcome: "would_publish", would_publish_count: 12 } ] }) });
  await flush();
  const out = f.doc._els["recurring-preview-output"].textContent;
  assert(out.indexOf("2026-08-20 - would publish (5)") !== -1, "B remains displayed (T3)");
  assert(out.indexOf("2026-08-17 - would publish (12)") === -1, "late A did not overwrite B (T3)");
  /* The pinned token is B's: Apply echoes B. */
  const apD = deferred(); f.data._q.apply.push(apD);
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply[0].token, "2026-08-14T13:00:00.000000Z", "pinned token is B's");
});

test("recurring V1: issuing Preview B immediately disables Apply until B resolves (issue boundary)", async () => {
  const f = build();
  await openAndPreview(f);                                   /* Preview A -> Apply enabled */
  assert(f.doc._els["recurring-apply"].disabled === false, "Apply enabled after Preview A");
  const pvB = deferred(); f.data._q.preview.push(pvB);
  f.doc.getElementById("recurring-preview").trigger("click"); /* Preview B issued, pending */
  assert(f.doc._els["recurring-apply"].disabled === true,
    "Apply is immediately disabled while Preview B is pending (V1)");
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply.length, 0, "clicking Apply while B pending fires nothing (V1)");
  pvB.resolve({ ok: true, data: previewData({ schedule_config_updated_at: "2026-08-14T13:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === false, "Apply enabled for B only after B resolves");
  const apD = deferred(); f.data._q.apply.push(apD);
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply[0].token, "2026-08-14T13:00:00.000000Z", "Apply uses B's token");
});

test("recurring V1: Save supersedes an in-flight Preview A during the PUT/GET window (Save race)", async () => {
  const f = build();
  await open(f, { ok: true, data: cfg() });
  const pvA = deferred(); f.data._q.preview.push(pvA);
  f.doc.getElementById("recurring-preview").trigger("click");   /* Preview A pending */
  /* Save begins -> invalidates Preview A BEFORE the PUT. */
  const putD = deferred(); f.data._q.put.push(putD);
  f.doc.getElementById("recurring-save").trigger("click");
  putD.resolve({ ok: true, data: cfg() });
  await flush();
  const getD = deferred(); f.data._q.get.push(getD);           /* authoritative GET B pending */
  /* Preview A resolves DURING the PUT-success/GET-pending window -> must be ignored. */
  pvA.resolve({ ok: true, data: previewData({ schedule_config_updated_at: "2026-08-14T12:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === true, "stale Preview A did NOT re-arm Apply (V1)");
  assert(f.doc._els["recurring-preview-output"].textContent.indexOf("2026-08-17 - would publish") === -1,
    "stale Preview A did NOT render (V1)");
  getD.resolve({ ok: true, data: cfg({ schedule_config_updated_at: "2026-08-14T13:00:00.000000Z" }) });
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === true, "config B rendered; a fresh Preview is still required (V1)");
});

test("recurring V1: Apply is one-shot and disables immediately; Preview is rejected while Apply runs (Apply boundary)", async () => {
  const f = build();
  await openAndPreview(f);                                    /* Apply armed with token A */
  const apD = deferred(); f.data._q.apply.push(apD);
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply[0].token, "2026-08-14T12:00:00.000000Z", "captured token A sent");
  assert(f.doc._els["recurring-apply"].disabled === true, "Apply disabled immediately on click (V1)");
  /* Second Apply click while the first is in flight -> one-shot: no extra call. */
  f.doc.getElementById("recurring-apply").trigger("click");
  assertEqual(f.data._calls.apply.length, 1, "Apply is one-shot per Preview (V1)");
  /* A Preview click while Apply is running is rejected. */
  f.doc.getElementById("recurring-preview").trigger("click");
  assertEqual(f.data._calls.preview.length, 1, "Preview rejected while Apply runs (V1)");
  apD.resolve({ ok: true, data: applyData() });
  await flush();
  assert(f.doc._els["recurring-apply"].disabled === true, "after Apply, a fresh Preview is required (V1)");
});

h.runRegisteredTests("test_portal_recurring_schedule_page").then((result) => {
  if (result.failed > 0) { process.exitCode = 1; }
});
