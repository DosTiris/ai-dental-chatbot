/*
 * test_portal_data.js - P3-B1 portal-data.js proofs.
 *
 * Loads the REAL portal-core.js AND portal-data.js into one Node vm
 * context (no browser, no network) and proves, via the harness's
 * ScriptedFetch (which FAILS on any unscripted request):
 *   - every data request carries the Bearer token from the session core
 *     owns, against the exact allow-listed endpoint;
 *   - the list query serializes ONLY the closed parameter vocabulary,
 *     URI-encoded, and omits empty values - an unknown parameter name can
 *     never become a request channel;
 *   - the lead id is URI-encoded into a single path segment;
 *   - no session means NO request at all (signed_out);
 *   - a near-expiry token refreshes BEFORE the data request;
 *   - a 401 triggers exactly ONE refresh-and-retry; a second 401 is a
 *     final "unauthorized"; a rejected refresh is "unauthorized" with the
 *     dead session cleared by core;
 *   - network failures and 5xx map to "unavailable", 404 to "not_found",
 *     400 to "bad_request", and a 200 with an unusable body to
 *     "invalid_response" (fail closed - never rendered as success);
 *   - A3 bite: a 200 whose body is {} or structurally wrong for its
 *     endpoint (wrong array types, missing required fields) resolves to
 *     "invalid_response" - never ok, so it can never throw in the pages.
 *
 * Run: node tests/portal/test_portal_data.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const h = require("./portal_test_harness.js");
const { test, assert, assertEqual } = h;

const PORTAL_DIR = path.join(__dirname, "..", "..", "static", "portal");
const CORE_PATH = path.join(PORTAL_DIR, "portal-core.js");
const DATA_PATH = path.join(PORTAL_DIR, "portal-data.js");

/* Epoch used by every test: FakeClock in ms, token expiry in seconds. */
const NOW_MS = 1700000000000;
const NOW_SEC = Math.floor(NOW_MS / 1000);

/*
 * Build fresh core + data instances wired to fresh fakes, with BOTH real
 * source files executed in one vm context (the makeCore technique).
 */
function makeData(options) {
  options = options || {};
  const sandboxWindow = {};
  const context = { window: sandboxWindow, URLSearchParams: URLSearchParams };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(CORE_PATH, "utf8"), context,
    { filename: "portal-core.js" });
  vm.runInContext(fs.readFileSync(DATA_PATH, "utf8"), context,
    { filename: "portal-data.js" });

  const storage = options.storage || h.FakeStorage();
  const clock = options.clock || h.FakeClock(NOW_MS);
  const fetchImpl = options.fetch || h.ScriptedFetch();
  const core = sandboxWindow.createMiaPortalCore({
    fetchImpl: fetchImpl,
    storage: storage,
    nowFn: clock,
    windowOrigin: "https://portal.test"
  });
  const data = sandboxWindow.createMiaPortalData({
    core: core,
    fetchImpl: fetchImpl
  });
  return { core, data, storage, clock, fetch: fetchImpl };
}

/* Minimal STRUCTURALLY-VALID bodies per endpoint (A3): the shape
 * validators require these fields, so every success-path test scripts a
 * body the real backend would send. */
function validDashboardBody(overrides) {
  return Object.assign({
    practice_name: "Test Dental",
    total_conversations: 7,
    total_leads: 2,
    urgent_leads: 1,
    leads_last_7_days: 1,
    recent_leads: []
  }, overrides || {});
}

function validListBody(overrides) {
  return Object.assign({ total: 0, limit: 25, offset: 0, leads: [] },
    overrides || {});
}

function validDetailBody(overrides) {
  return Object.assign({
    lead_id: "11111111-1111-1111-1111-111111111111",
    lead_name: "X",
    messages: [],
    messages_total: 0,
    messages_truncated: false,
    /* P3-B2: the office workflow slice is part of the approved detail
     * contract (nulls = the office never touched the lead). */
    office_status: null,
    office_status_updated_at: null,
    office_note: null,
    office_note_updated_at: null
  }, overrides || {});
}

/* Persist a session the way core stores it. */
function seedSession(env, overrides) {
  const session = Object.assign({
    accessToken: "tok-a",
    refreshToken: "refresh-a",
    expiresAtSeconds: NOW_SEC + 3600
  }, overrides || {});
  env.storage.setItem(env.core.SESSION_STORAGE_KEY, JSON.stringify(session));
  return session;
}

/* ------------------------------------------------------------------ */
/* Request shape                                                       */
/* ------------------------------------------------------------------ */

test("dashboard request carries the session Bearer token to the exact endpoint", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/dashboard", method: "GET",
      headerEquals: { "Authorization": "Bearer tok-a" } },
    { status: 200, json: validDashboardBody() }
  );
  const outcome = await env.data.getDashboard();
  assert(outcome.ok, "expected ok outcome");
  assertEqual(outcome.data.practice_name, "Test Dental", "body passthrough");
  assertEqual(env.fetch.remaining(), 0, "no leftover expectations");
});

test("list query serializes ONLY the closed parameter vocabulary, encoded", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/leads?status=new&q=A%26B%20%25&days=30&limit=25&offset=50",
      method: "GET" },
    { status: 200, json: validListBody({ offset: 50 }) }
  );
  const outcome = await env.data.listLeads({
    status: "new", q: "A&B %", days: 30, limit: 25, offset: 50,
    evil: "1", other: "nope"      /* not in the vocabulary: never sent */
  });
  assert(outcome.ok, "expected ok outcome");
  assertEqual(env.fetch.remaining(), 0, "exactly one request");
});

test("buildLeadsQuery omits empty values and unknown names (pure)", () => {
  const env = makeData();
  assertEqual(env.data.buildLeadsQuery({}), "", "no params -> no query");
  assertEqual(env.data.buildLeadsQuery({ q: "", status: null, days: undefined }),
    "", "empty values omitted");
  assertEqual(env.data.buildLeadsQuery({ offset: 0, limit: 25 }),
    "?limit=25&offset=0", "zero offset is a real value, fixed order");
});

test("lead id is URI-encoded into a single path segment", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/leads/abc%2F..%2Fdef", method: "GET" },
    { status: 200, json: validDetailBody() }
  );
  const outcome = await env.data.getLeadDetail("abc/../def");
  assert(outcome.ok, "expected ok outcome");
});

/* ------------------------------------------------------------------ */
/* Session and token lifecycle                                         */
/* ------------------------------------------------------------------ */

test("no session -> signed_out with ZERO network requests", async () => {
  const env = makeData();
  const outcome = await env.data.getDashboard();
  assertEqual(outcome.ok, false, "not ok");
  assertEqual(outcome.state, "signed_out", "signed_out state");
  assertEqual(env.fetch.seen().length, 0, "no request was made");
});

test("near-expiry token refreshes BEFORE the data request", async () => {
  const env = makeData();
  seedSession(env, { expiresAtSeconds: NOW_SEC + 10 }); /* inside skew */
  h.expectConfigLoad(env.fetch);
  env.fetch.expect(
    { urlIncludes: "grant_type=refresh_token", method: "POST" },
    { status: 200, json: { access_token: "tok-new", refresh_token: "r2",
        expires_at: NOW_SEC + 3600 } }
  );
  env.fetch.expect(
    { urlEquals: "/portal/leads", method: "GET",
      headerEquals: { "Authorization": "Bearer tok-new" } },
    { status: 200, json: validListBody() }
  );
  const outcome = await env.data.listLeads({});
  assert(outcome.ok, "expected ok outcome");
  assertEqual(env.fetch.remaining(), 0, "refresh happened first");
});

test("a 401 triggers exactly ONE refresh-and-retry with the new token", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/dashboard", method: "GET",
      headerEquals: { "Authorization": "Bearer tok-a" } },
    { status: 401, json: { detail: "Invalid portal credentials." } }
  );
  h.expectConfigLoad(env.fetch);
  env.fetch.expect(
    { urlIncludes: "grant_type=refresh_token", method: "POST" },
    { status: 200, json: { access_token: "tok-b", refresh_token: "r2",
        expires_at: NOW_SEC + 3600 } }
  );
  env.fetch.expect(
    { urlEquals: "/portal/dashboard", method: "GET",
      headerEquals: { "Authorization": "Bearer tok-b" } },
    { status: 200, json: validDashboardBody() }
  );
  const outcome = await env.data.getDashboard();
  assert(outcome.ok, "retry succeeded");
  assertEqual(env.fetch.remaining(), 0, "exactly one retry");
});

test("a second 401 after the refresh is a FINAL unauthorized", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/dashboard" },
    { status: 401, json: {} });
  h.expectConfigLoad(env.fetch);
  env.fetch.expect(
    { urlIncludes: "grant_type=refresh_token", method: "POST" },
    { status: 200, json: { access_token: "tok-b", refresh_token: "r2",
        expires_at: NOW_SEC + 3600 } }
  );
  env.fetch.expect({ urlEquals: "/portal/dashboard" },
    { status: 401, json: {} });
  const outcome = await env.data.getDashboard();
  assertEqual(outcome.state, "unauthorized", "final unauthorized");
  assertEqual(env.fetch.remaining(), 0, "no third attempt");
});

test("a REJECTED refresh is unauthorized and core drops the dead session", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/leads" }, { status: 401, json: {} });
  h.expectConfigLoad(env.fetch);
  env.fetch.expect(
    { urlIncludes: "grant_type=refresh_token", method: "POST" },
    { status: 401, json: {} }
  );
  const outcome = await env.data.listLeads({});
  assertEqual(outcome.state, "unauthorized", "unauthorized state");
  assertEqual(env.core.readSession(), null, "session cleared by core");
});

/* ------------------------------------------------------------------ */
/* Failure mapping (closed outcome vocabulary)                          */
/* ------------------------------------------------------------------ */

test("network failure maps to unavailable", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/dashboard" }, { networkError: true });
  const outcome = await env.data.getDashboard();
  assertEqual(outcome.state, "unavailable", "unavailable on network failure");
});

test("a 5xx maps to unavailable", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/dashboard" },
    { status: 503, json: { detail: "down" } });
  const outcome = await env.data.getDashboard();
  assertEqual(outcome.state, "unavailable", "unavailable on 5xx");
});

test("404 maps to not_found and 400 maps to bad_request", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 404, json: { detail: "Lead not found." } });
  const missing = await env.data.getLeadDetail("0000");
  assertEqual(missing.state, "not_found", "404 -> not_found");

  env.fetch.expect({ urlIncludes: "/portal/leads?" },
    { status: 400, json: { detail: "limit must be between 1 and 100" } });
  const invalid = await env.data.listLeads({ limit: 101 });
  assertEqual(invalid.state, "bad_request", "400 -> bad_request");
});

test("a 200 with an unusable body is invalid_response, never success", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/dashboard" },
    { status: 200, json: "not-an-object" });
  const outcome = await env.data.getDashboard();
  assertEqual(outcome.ok, false, "not ok");
  assertEqual(outcome.state, "invalid_response", "fail closed on bad body");
});

test("A3 bite: a 200 {} fails every endpoint's shape check", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/dashboard" }, { status: 200, json: {} });
  assertEqual((await env.data.getDashboard()).state, "invalid_response",
    "empty dashboard body rejected");
  env.fetch.expect({ urlEquals: "/portal/leads" }, { status: 200, json: {} });
  assertEqual((await env.data.listLeads({})).state, "invalid_response",
    "empty list body rejected");
  env.fetch.expect({ urlIncludes: "/portal/leads/" }, { status: 200, json: {} });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "empty detail body rejected");
});

test("A3 bite: wrong array types are rejected per endpoint", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/dashboard" },
    { status: 200, json: validDashboardBody({ recent_leads: "nope" }) });
  assertEqual((await env.data.getDashboard()).state, "invalid_response",
    "recent_leads must be an array");
  env.fetch.expect({ urlEquals: "/portal/leads" },
    { status: 200, json: validListBody({ leads: {} }) });
  assertEqual((await env.data.listLeads({})).state, "invalid_response",
    "leads must be an array");
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({ messages: null }) });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "messages must be an array");
});

test("A3-R1 bite: [null] array members are rejected on every endpoint", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/dashboard" },
    { status: 200, json: validDashboardBody({ recent_leads: [null] }) });
  assertEqual((await env.data.getDashboard()).state, "invalid_response",
    "recent_leads [null] rejected");
  env.fetch.expect({ urlEquals: "/portal/leads" },
    { status: 200, json: validListBody({ total: 1, leads: [null] }) });
  assertEqual((await env.data.listLeads({})).state, "invalid_response",
    "leads [null] rejected");
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({ messages: [null],
      messages_total: 1 }) });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "messages [null] rejected");
});

test("A3-R1 bite: primitive array members are rejected", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/dashboard" },
    { status: 200, json: validDashboardBody({ recent_leads: ["lead"] }) });
  assertEqual((await env.data.getDashboard()).state, "invalid_response",
    "string lead member rejected");
  env.fetch.expect({ urlEquals: "/portal/leads" },
    { status: 200, json: validListBody({ total: 1, leads: [42] }) });
  assertEqual((await env.data.listLeads({})).state, "invalid_response",
    "numeric lead member rejected");
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({ messages: ["hi"],
      messages_total: 1 }) });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "string message member rejected");
});

test("A3-R1 bite: malformed lead members are rejected, nullable display fields stay permissive", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/leads" },
    { status: 200, json: validListBody({ total: 1,
      leads: [{ lead_name: "No Id" }] }) });
  assertEqual((await env.data.listLeads({})).state, "invalid_response",
    "lead member without lead_id rejected");
  env.fetch.expect({ urlEquals: "/portal/leads" },
    { status: 200, json: validListBody({ total: 1,
      leads: [{ lead_id: 123 }] }) });
  assertEqual((await env.data.listLeads({})).state, "invalid_response",
    "non-string lead_id rejected");
  env.fetch.expect({ urlEquals: "/portal/leads" },
    { status: 200, json: validListBody({ total: 1,
      leads: [{ lead_id: "" }] }) });
  assertEqual((await env.data.listLeads({})).state, "invalid_response",
    "empty lead_id rejected");
  /* Permissiveness proof: the backend legitimately nulls display fields;
   * a member with ONLY a usable lead_id must still pass. */
  env.fetch.expect({ urlEquals: "/portal/leads" },
    { status: 200, json: validListBody({ total: 1,
      leads: [{ lead_id: "lead-1", lead_name: null, lead_phone: null,
        last_lead_at: null }] }) });
  const permissive = await env.data.listLeads({});
  assertEqual(permissive.ok, true, "nullable display fields remain valid");
});

test("A3-R1 bite: malformed message members are rejected", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({
      messages: [{ role: "user" }], messages_total: 1 }) });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "message without content rejected");
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({
      messages: [{ role: 5, content: "hi" }], messages_total: 1 }) });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "non-string role rejected");
  /* created_at stays permissive: a null timestamp is legitimate. */
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({
      messages: [{ role: "user", content: "hi", created_at: null }],
      messages_total: 1 }) });
  assertEqual((await env.data.getLeadDetail("x")).ok, true,
    "null created_at remains valid");
});

test("A3-R1 bite: inconsistent transcript totals are rejected", async () => {
  const env = makeData();
  seedSession(env);
  const oneMessage = [{ role: "user", content: "hi",
    created_at: "2026-08-10T14:00:00+00:00" }];
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({ messages: oneMessage,
      messages_total: 0, messages_truncated: false }) });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "total below delivered length rejected");
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({ messages: oneMessage,
      messages_total: 2, messages_truncated: false }) });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "untruncated total must equal delivered length");
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({ messages: oneMessage,
      messages_total: 1, messages_truncated: true }) });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "truncated total must exceed delivered length");
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: validDetailBody({ messages: oneMessage,
      messages_total: 1.5, messages_truncated: true }) });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "non-integer total rejected");
});

test("A3 bite: missing required fields are rejected", async () => {
  const env = makeData();
  seedSession(env);
  const noCounts = validDashboardBody();
  delete noCounts.urgent_leads;
  env.fetch.expect({ urlEquals: "/portal/dashboard" },
    { status: 200, json: noCounts });
  assertEqual((await env.data.getDashboard()).state, "invalid_response",
    "dashboard without urgent_leads rejected");

  const noTotal = validListBody();
  delete noTotal.total;
  env.fetch.expect({ urlEquals: "/portal/leads" },
    { status: 200, json: noTotal });
  assertEqual((await env.data.listLeads({})).state, "invalid_response",
    "list without total rejected");

  const noTrunc = validDetailBody();
  delete noTrunc.messages_truncated;
  env.fetch.expect({ urlIncludes: "/portal/leads/" },
    { status: 200, json: noTrunc });
  assertEqual((await env.data.getLeadDetail("x")).state, "invalid_response",
    "detail without messages_truncated rejected");
});

/* ------------------------------------------------------------------ */
/* P4-A: schedule requests (contract v1.2 SS6 / SS8.14a-b, C5)          */
/* ------------------------------------------------------------------ */

/* A structurally-valid schedule slot / envelope per the P4-A backend. */
function validScheduleSlot(overrides) {
  return Object.assign({
    slot_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    start_datetime: "2026-08-21T13:00:00Z",
    end_datetime: "2026-08-21T14:00:00Z",
    status: "available",
    provider_name: null,
    service_key: null
  }, overrides || {});
}

function validScheduleBody(overrides) {
  return Object.assign({
    timezone_name: "America/New_York",
    start_day: "2026-08-21",
    end_day: "2026-08-27",
    slots: [validScheduleSlot()]
  }, overrides || {});
}

test("schedule query serializes ONLY the closed vocabulary, URI-encoded", async () => {
  const env = makeData();
  assertEqual(env.data.buildScheduleQuery({}), "", "empty params -> no query");
  assertEqual(env.data.buildScheduleQuery(null), "", "null params -> no query");
  assertEqual(
    env.data.buildScheduleQuery({ start_day: "2026-08-21",
      end_day: "2026-08-27" }),
    "?start_day=2026-08-21&end_day=2026-08-27", "both bounds serialized");
  assertEqual(
    env.data.buildScheduleQuery({ start_day: "2026-08-21", end_day: "",
      client_id: "smuggled", limit: 99, anything: "x" }),
    "?start_day=2026-08-21",
    "unknown names (incl. client_id) NEVER serialize; empty values omitted");
  assertEqual(
    env.data.buildScheduleQuery({ start_day: "a b", end_day: "c&d" }),
    "?start_day=a%20b&end_day=c%26d", "values are URI-encoded");
});

test("getSchedule hits the exact endpoint with the Bearer token", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule", method: "GET",
      headerEquals: { "Authorization": "Bearer tok-a" } },
    { status: 200, json: validScheduleBody() }
  );
  const outcome = await env.data.getSchedule({});
  assert(outcome.ok, "valid schedule body accepted");
  assertEqual(env.fetch.remaining(), 0, "exactly one request");
});

test("publishScheduleDay POSTs EXACTLY the three approved body fields", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/days/2026-08-21/publish", method: "POST",
      headerEquals: { "Authorization": "Bearer tok-a" },
      bodyJson: { open_time: "09:00", close_time: "17:00",
        slot_minutes: 30 } },
    { status: 200, json: [validScheduleSlot()] }
  );
  const outcome = await env.data.publishScheduleDay(
    "2026-08-21", "09:00", "17:00", 30);
  assert(outcome.ok, "created slots accepted");
  assertEqual(env.fetch.remaining(), 0, "exactly one request");
});

test("publish 409 maps to the conflict outcome (never silent success)", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/days/2026-08-21/publish", method: "POST" },
    { status: 409, json: { detail: "One or more requested slots overlap existing slots on that day." } }
  );
  const outcome = await env.data.publishScheduleDay(
    "2026-08-21", "09:00", "17:00", 30);
  assert(!outcome.ok, "409 is not ok");
  assertEqual(outcome.state, "conflict", "409 -> conflict");
});

test("per-slot block/unblock POST to URI-encoded slot paths", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/slots/s%20x/block", method: "POST" },
    { status: 200, json: validScheduleSlot({ status: "blocked" }) }
  );
  assert((await env.data.blockScheduleSlot("s x")).ok,
    "block: encoded path, valid SlotView accepted");
  env.fetch.expect(
    { urlEquals: "/portal/schedule/slots/abc/unblock", method: "POST" },
    { status: 404, json: { detail: "Slot not found." } }
  );
  assertEqual((await env.data.unblockScheduleSlot("abc")).state, "not_found",
    "unblock 404 -> not_found");
});

test("blockAllOpenSlots POSTs to the day path and validates the result", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/days/2026-08-21/block-all-open",
      method: "POST",
      headerEquals: { "Authorization": "Bearer tok-a" } },
    { status: 200, json: { day: "2026-08-21", blocked_count: 3,
      booked_remaining: [{ start_datetime: "2026-08-21T16:00:00Z",
        end_datetime: "2026-08-21T17:00:00Z" }] } }
  );
  const outcome = await env.data.blockAllOpenSlots("2026-08-21");
  assert(outcome.ok, "valid bulk body accepted");
  assertEqual(outcome.data.blocked_count, 3, "count delivered");
});

test("A3 bite: malformed 200 schedule bodies resolve to invalid_response", async () => {
  const cases = [
    /* envelope: missing slots array */
    { json: { timezone_name: "America/New_York", start_day: "2026-08-21",
      end_day: "2026-08-27" } },
    /* envelope: slot missing slot_id */
    { json: validScheduleBody({ slots: [
      { start_datetime: "2026-08-21T13:00:00Z",
        end_datetime: "2026-08-21T14:00:00Z", status: "available" }] }) },
    /* envelope: instant without the Z designator (device-time hazard) */
    { json: validScheduleBody({ slots: [
      validScheduleSlot({ start_datetime: "2026-08-21T13:00:00" })] }) },
    /* C5 bite: an IMPOSSIBLE date JS Date would silently normalize */
    { json: validScheduleBody({ slots: [
      validScheduleSlot({ start_datetime: "2026-02-30T10:00:00Z" })] }) }
  ];
  for (const testCase of cases) {
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/schedule", method: "GET" },
      { status: 200, json: testCase.json }
    );
    const outcome = await env.data.getSchedule({});
    assert(!outcome.ok, "malformed 200 is never ok");
    assertEqual(outcome.state, "invalid_response", "fails closed");
  }
});

test("A3 bite: malformed bulk result bodies resolve to invalid_response", async () => {
  const cases = [
    /* impossible local date for day */
    { day: "2026-02-30", blocked_count: 1, booked_remaining: [] },
    /* non-integer count */
    { day: "2026-08-21", blocked_count: 1.5, booked_remaining: [] },
    /* negative count */
    { day: "2026-08-21", blocked_count: -1, booked_remaining: [] },
    /* booked_remaining with an impossible instant (C5: the SAME strict
     * validator judges booked_remaining) */
    { day: "2026-08-21", blocked_count: 0, booked_remaining: [
      { start_datetime: "2026-02-30T10:00:00Z",
        end_datetime: "2026-08-21T17:00:00Z" }] }
  ];
  for (const body of cases) {
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/schedule/days/2026-08-21/block-all-open",
        method: "POST" },
      { status: 200, json: body }
    );
    const outcome = await env.data.blockAllOpenSlots("2026-08-21");
    assert(!outcome.ok, "malformed bulk 200 is never ok");
    assertEqual(outcome.state, "invalid_response", "fails closed");
  }
});

/* ------------------------------------------------------------------ */
/* F2 bites: EXACT field sets - an otherwise-valid body carrying ANY   */
/* unexpected property fails closed (leak prevention at the browser).  */
/* ------------------------------------------------------------------ */

test("F2 bite: an envelope with ANY extra field resolves to invalid_response", async () => {
  const extras = [
    { client_id: "11111111-1111-1111-1111-111111111111" },
    { settings: {} },
    { total: 1 }
  ];
  for (const extra of extras) {
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/schedule", method: "GET" },
      { status: 200, json: Object.assign(validScheduleBody(), extra) }
    );
    const outcome = await env.data.getSchedule({});
    assert(!outcome.ok, "extra envelope field is never ok");
    assertEqual(outcome.state, "invalid_response",
      "envelope extra " + Object.keys(extra)[0] + " fails closed");
  }
});

test("F2 bite: a SlotView with ANY extra field resolves to invalid_response", async () => {
  const extras = [
    { held_until: "2026-08-21T13:05:00Z" },
    { held_by_conversation_id: "22222222-2222-2222-2222-222222222222" },
    { client_id: "11111111-1111-1111-1111-111111111111" },
    { patient_name: "Kevin Alvarado" }
  ];
  for (const extra of extras) {
    /* Once through the envelope's slot array... */
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/schedule", method: "GET" },
      { status: 200, json: validScheduleBody({
        slots: [Object.assign(validScheduleSlot(), extra)] }) }
    );
    const outcome = await env.data.getSchedule({});
    assertEqual(outcome.state, "invalid_response",
      "envelope slot extra " + Object.keys(extra)[0] + " fails closed");
    /* ...and once through the single-SlotView block response. */
    const env2 = makeData();
    seedSession(env2);
    env2.fetch.expect(
      { urlEquals: "/portal/schedule/slots/abc/block", method: "POST" },
      { status: 200, json: Object.assign(
        validScheduleSlot({ status: "blocked" }), extra) }
    );
    const blockOutcome = await env2.data.blockScheduleSlot("abc");
    assertEqual(blockOutcome.state, "invalid_response",
      "block SlotView extra " + Object.keys(extra)[0] + " fails closed");
  }
});

test("F2 bite: a booked_remaining member with ANY extra field fails closed", async () => {
  const extras = [
    { patient_name: "Kevin Alvarado" },
    { slot_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" },
    { held_until: null }
  ];
  for (const extra of extras) {
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/schedule/days/2026-08-21/block-all-open",
        method: "POST" },
      { status: 200, json: { day: "2026-08-21", blocked_count: 1,
        booked_remaining: [Object.assign({
          start_datetime: "2026-08-21T16:00:00Z",
          end_datetime: "2026-08-21T17:00:00Z" }, extra)] } }
    );
    const outcome = await env.data.blockAllOpenSlots("2026-08-21");
    assertEqual(outcome.state, "invalid_response",
      "booked_remaining extra " + Object.keys(extra)[0] + " fails closed");
  }
  /* And the bulk envelope itself. */
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/days/2026-08-21/block-all-open",
      method: "POST" },
    { status: 200, json: { day: "2026-08-21", blocked_count: 1,
      booked_remaining: [], client_id: "x" } }
  );
  assertEqual((await env.data.blockAllOpenSlots("2026-08-21")).state,
    "invalid_response", "bulk envelope extra client_id fails closed");
});

test("F2 bite: status outside the closed vocabulary and non-string provider fail closed", async () => {
  const badSlots = [
    validScheduleSlot({ status: "weird" }),
    validScheduleSlot({ status: "" }),
    validScheduleSlot({ provider_name: 7 }),
    validScheduleSlot({ service_key: {} })
  ];
  for (const slot of badSlots) {
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/schedule", method: "GET" },
      { status: 200, json: validScheduleBody({ slots: [slot] }) }
    );
    assertEqual((await env.data.getSchedule({})).state, "invalid_response",
      "closed vocabulary / typing enforced");
  }
});

test("no session means NO schedule request at all", async () => {
  const env = makeData();  /* no seedSession */
  const outcome = await env.data.blockAllOpenSlots("2026-08-21");
  assert(!outcome.ok, "no session -> not ok");
  assertEqual(outcome.state, "signed_out", "signed_out with zero requests");
  assertEqual(env.fetch.remaining(), 0, "no request was made");
});

/* ------------------------------------------------------------------ */
/* P4-B: recurring-schedule data layer (GET/PUT/preview/apply)          */
/* ------------------------------------------------------------------ */

function validRecurringBody(overrides) {
  return Object.assign({
    weekly_hours: { mon: { open: true, start: "09:00", end: "17:00" } },
    slot_minutes: 30,
    closures: [],
    schedule_config_updated_at: "2026-08-14T12:00:00.000000Z"
  }, overrides || {});
}
function validPreviewBody(overrides) {
  return Object.assign({
    schedule_config_updated_at: "2026-08-14T12:00:00.000000Z",
    start_day: "2026-08-14", end_day: "2026-09-13", days: []
  }, overrides || {});
}
function validApplyBody(overrides) {
  return Object.assign({
    schedule_config_updated_at: "2026-08-14T12:00:00.000000Z",
    start_day: "2026-08-14", end_day: "2026-09-13",
    days: [], totals: { published_days: 0, closure_blocked_days: 0,
      existing_inventory_skipped_days: 0 }
  }, overrides || {});
}

test("P4-B: GET recurring carries the Bearer token to the exact endpoint", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/recurring", method: "GET",
      headerEquals: { "Authorization": "Bearer tok-a" } },
    { status: 200, json: validRecurringBody() });
  const outcome = await env.data.getRecurringSchedule();
  assert(outcome.ok, "ok"); assertEqual(env.fetch.remaining(), 0, "no leftover");
});

test("P4-B: PUT recurring sends EXACTLY the four-key body with the token VERBATIM", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/recurring", method: "PUT",
      bodyJson: { weekly_hours: { mon: { open: true, start: "09:00", end: "17:00" } },
                  slot_minutes: 30, closures: [],
                  expected_schedule_config_updated_at: "2026-08-14T12:00:00.000000Z" } },
    { status: 200, json: validRecurringBody() });
  const outcome = await env.data.putRecurringSchedule(
    { mon: { open: true, start: "09:00", end: "17:00" } }, 30, [], "2026-08-14T12:00:00.000000Z");
  assert(outcome.ok, "ok"); assertEqual(env.fetch.remaining(), 0, "exact body matched");
});

test("P4-B: PUT recurring 409 maps to conflict; 422 maps to bad_request", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/schedule/recurring", method: "PUT" },
    { status: 409, json: {} });
  const c = await env.data.putRecurringSchedule({}, 30, [], "2026-08-14T12:00:00.000000Z");
  assertEqual(c.state, "conflict", "409 -> conflict");
  env.fetch.expect({ urlEquals: "/portal/schedule/recurring", method: "PUT" },
    { status: 422, json: {} });
  const b = await env.data.putRecurringSchedule({}, 30, [], "2026-08-14T12:00:00.000000Z");
  assertEqual(b.state, "bad_request", "422 -> bad_request");
});

test("P4-B: Preview POSTs a body of EXACTLY {} (F2)", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/recurring/preview", method: "POST",
      bodyJson: {} },
    { status: 200, json: validPreviewBody() });
  const outcome = await env.data.previewRecurringSchedule();
  assert(outcome.ok, "ok"); assertEqual(env.fetch.remaining(), 0, "preview body was exactly {}");
});

test("P4-B: Apply POSTs the expected token VERBATIM and returns totals", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/recurring/apply", method: "POST",
      bodyJson: { expected_schedule_config_updated_at: "2026-08-14T12:00:00.000000Z" } },
    { status: 200, json: validApplyBody() });
  const outcome = await env.data.applyRecurringSchedule("2026-08-14T12:00:00.000000Z");
  assert(outcome.ok, "ok"); assertEqual(env.fetch.remaining(), 0, "apply sent the verbatim token");
});

test("P4-B: Apply 409 maps to conflict", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/schedule/recurring/apply", method: "POST" },
    { status: 409, json: {} });
  const outcome = await env.data.applyRecurringSchedule("2026-08-14T12:00:00.000000Z");
  assertEqual(outcome.state, "conflict", "409 -> conflict");
});

test("P4-B: no session means NO recurring request at all", async () => {
  const env = makeData();  /* no seedSession */
  assertEqual((await env.data.getRecurringSchedule()).state, "signed_out", "GET signed_out");
  assertEqual((await env.data.putRecurringSchedule({}, 30, [], null)).state, "signed_out", "PUT signed_out");
  assertEqual((await env.data.previewRecurringSchedule()).state, "signed_out", "preview signed_out");
  assertEqual((await env.data.applyRecurringSchedule(null)).state, "signed_out", "apply signed_out");
  assertEqual(env.fetch.seen().length, 0, "no request was made");
});

test("P4-B/F6: a Preview 200 missing schedule_config_updated_at fails closed as invalid_response", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/schedule/recurring/preview", method: "POST" },
    { status: 200, json: { start_day: "2026-08-14", end_day: "2026-09-13", days: [] } });
  const outcome = await env.data.previewRecurringSchedule();
  assertEqual(outcome.state, "invalid_response", "malformed Preview 200 -> invalid_response");
});

test("P4-B/F6: a Preview 200 missing start_day/end_day/days fails closed", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/schedule/recurring/preview", method: "POST" },
    { status: 200, json: { schedule_config_updated_at: "2026-08-14T12:00:00.000000Z" } });
  const outcome = await env.data.previewRecurringSchedule();
  assertEqual(outcome.state, "invalid_response", "missing horizon/days -> invalid_response");
});

test("P4-B/F6: an Apply 200 missing totals fails closed as invalid_response", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/schedule/recurring/apply", method: "POST" },
    { status: 200, json: { schedule_config_updated_at: "2026-08-14T12:00:00.000000Z",
      start_day: "2026-08-14", end_day: "2026-09-13", days: [] } });
  const outcome = await env.data.applyRecurringSchedule("2026-08-14T12:00:00.000000Z");
  assertEqual(outcome.state, "invalid_response", "Apply without totals -> invalid_response");
});

test("P4-B/F6: an Apply 200 missing horizon/token fails closed", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/schedule/recurring/apply", method: "POST" },
    { status: 200, json: { days: [], totals: { published_days: 0, closure_blocked_days: 0,
      existing_inventory_skipped_days: 0 } } });
  const outcome = await env.data.applyRecurringSchedule("2026-08-14T12:00:00.000000Z");
  assertEqual(outcome.state, "invalid_response", "Apply missing token/horizon -> invalid_response");
});

test("P4-B/R2: a Preview 200 with closure booked_windows validates and passes the day through", async () => {
  const env = makeData(); seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/schedule/recurring/preview", method: "POST" },
    { status: 200, json: { schedule_config_updated_at: "2026-08-14T12:00:00.000000Z",
      start_day: "2026-08-14", end_day: "2026-09-13",
      days: [ { day: "2026-12-25", outcome: "closure_empty",
                would_block_available_held: 0,
                booked_windows: [ { start_utc: "2026-12-25T14:00:00Z",
                                    end_utc: "2026-12-25T14:30:00Z" } ] } ] } });
  const outcome = await env.data.previewRecurringSchedule();
  assert(outcome.ok, "valid preview with booked_windows is ok");
  assertEqual(outcome.data.days[0].booked_windows.length, 1, "booked_windows passed through");
});

/* ------------------------------------------------------------------ */

(async () => {
  const summary = await h.runRegisteredTests("test_portal_data");
  process.exitCode = summary.failed === 0 ? 0 : 1;
})();


/* ------------------------------------------------------------------ */
/* P3-B2: office workflow mutations                                     */
/* ------------------------------------------------------------------ */

function validWorkflowBody(overrides) {
  return Object.assign({
    lead_id: "11111111-1111-1111-1111-111111111111",
    office_status: "contacted",
    office_status_updated_at: "2026-08-12T03:00:00Z",
    office_note: null,
    office_note_updated_at: null
  }, overrides || {});
}

test("putLeadStatus sends EXACTLY the two-field body to the status path", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/leads/abc/status", method: "PUT",
      headerEquals: { "Authorization": "Bearer tok-a",
                      "Content-Type": "application/json" },
      bodyJson: { office_status: "contacted",
                  expected_office_status_updated_at: null } },
    { status: 200, json: validWorkflowBody() }
  );
  const outcome = await env.data.putLeadStatus("abc", "contacted", null);
  assert(outcome.ok, "expected ok outcome");
  assertEqual(outcome.data.office_status, "contacted", "body passthrough");
  assertEqual(env.fetch.remaining(), 0, "no leftover expectations");
});

test("putLeadNote null clears via an explicit null body value", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/leads/abc/note", method: "PUT",
      bodyJson: { office_note: null,
                  expected_office_note_updated_at: "2026-08-12T03:00:00Z" } },
    { status: 200, json: validWorkflowBody({
        office_status: null, office_status_updated_at: null,
        office_note: null, office_note_updated_at: "2026-08-12T03:05:00Z" }) }
  );
  const outcome = await env.data.putLeadNote("abc", null,
    "2026-08-12T03:00:00Z");
  assert(outcome.ok, "expected ok outcome");
  assertEqual(outcome.data.office_note_updated_at,
    "2026-08-12T03:05:00Z", "fresh token passthrough");
});

test("a 409 mutation response maps to the conflict outcome", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/leads/abc/status", method: "PUT" },
    { status: 409, json: { detail: "stale" } }
  );
  const outcome = await env.data.putLeadStatus("abc", "booked", "old-token");
  assert(!outcome.ok, "conflict is not ok");
  assertEqual(outcome.state, "conflict", "409 -> conflict state");
});

test("mutation response validation fails closed on a malformed body", async () => {
  const env = makeData();
  seedSession(env);
  /* office_status present but its token missing violates the pair rule. */
  env.fetch.expect(
    { urlEquals: "/portal/leads/abc/status", method: "PUT" },
    { status: 200, json: validWorkflowBody({
        office_status: "booked", office_status_updated_at: null }) }
  );
  const outcome = await env.data.putLeadStatus("abc", "booked", null);
  assert(!outcome.ok, "malformed body must not be ok");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("mutation response validation rejects an out-of-vocabulary status", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/leads/abc/status", method: "PUT" },
    { status: 200, json: validWorkflowBody({ office_status: "hot" }) }
  );
  const outcome = await env.data.putLeadStatus("abc", "contacted", null);
  assert(!outcome.ok, "unknown status value must not be ok");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("detail validator REJECTS a body missing the office workflow slice", async () => {
  const env = makeData();
  seedSession(env);
  const body = validDetailBody();
  delete body.office_status;
  env.fetch.expect(
    { urlIncludes: "/portal/leads/", method: "GET" },
    { status: 200, json: body }
  );
  const outcome = await env.data.getLeadDetail("abc");
  assert(!outcome.ok, "detail without the office slice must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

/* ------------------------------------------------------------------ */
/* Portal Appointments v1: read-only appointments data access          */
/* ------------------------------------------------------------------ */

function validAppointmentsBody(overrides) {
  return Object.assign({
    timezone_name: "America/New_York",
    start_day: "2026-07-16",
    end_day: "2026-07-22",
    appointments: []
  }, overrides || {});
}

function validAppointmentMember(overrides) {
  return Object.assign({
    appointment_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
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

/* ------------------------------------------------------------------ */
/* PHASE 3A Slice 4B1: internal_note joins the exact appointment       */
/* member contract. The fail-closed direction is deliberate and is     */
/* itself the deployment-order guard: a backend without the field (or  */
/* one rolled back after this ships) fails the WHOLE body closed as    */
/* invalid_response rather than rendering a half-contract.             */
/* ------------------------------------------------------------------ */

test("Slice 4B1: a member MISSING internal_note fails closed", async () => {
  const env = makeData();
  seedSession(env);
  const member = validAppointmentMember();
  delete member.internal_note;
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: { timezone_name: "America/New_York",
      start_day: "2026-07-16", end_day: "2026-07-22",
      appointments: [member] } }
  );
  const outcome = await env.data.getAppointments();
  assert(!outcome.ok, "a pre-4B1 body is not silently accepted");
  assertEqual(outcome.state, "invalid_response",
    "the exact-key contract fails the whole body closed");
});

test("Slice 4B1: internal_note must be null or a string", async () => {
  for (const bad of [7, {}, [], true]) {
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/appointments", method: "GET" },
      { status: 200, json: { timezone_name: "America/New_York",
        start_day: "2026-07-16", end_day: "2026-07-22",
        appointments: [validAppointmentMember({ internal_note: bad })] } }
    );
    const outcome = await env.data.getAppointments();
    assert(!outcome.ok, "a mis-typed note fails closed: " + typeof bad);
    assertEqual(outcome.state, "invalid_response", "invalid_response");
  }
});

test("Slice 4B1: a string note rides the approved member through", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: { timezone_name: "America/New_York",
      start_day: "2026-07-16", end_day: "2026-07-22",
      appointments: [validAppointmentMember(
        { internal_note: "gate code 4411" })] } }
  );
  const outcome = await env.data.getAppointments();
  assert(outcome.ok, "a well-typed note is accepted");
  assertEqual(outcome.data.appointments[0].internal_note, "gate code 4411",
    "and reaches the caller verbatim");
});

test("appointments default request sends NO bounds to the exact endpoint", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET",
      headerEquals: { "Authorization": "Bearer tok-a" } },
    { status: 200, json: validAppointmentsBody() }
  );
  const outcome = await env.data.getAppointments({});
  assert(outcome.ok, "expected ok outcome");
  assertEqual(outcome.data.timezone_name, "America/New_York", "tz passthrough");
  assertEqual(env.fetch.remaining(), 0, "exactly one request");
});

test("appointments query serializes ONLY start_day/end_day, encoded", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments?start_day=2026-07-16&end_day=2026-07-22",
      method: "GET" },
    { status: 200, json: validAppointmentsBody() }
  );
  const outcome = await env.data.getAppointments({
    start_day: "2026-07-16", end_day: "2026-07-22",
    client_id: "smuggled", evil: "1"   /* not in the vocabulary: never sent */
  });
  assert(outcome.ok, "expected ok outcome");
  assertEqual(env.fetch.remaining(), 0, "exactly one request, no smuggled params");
});

test("buildAppointmentsQuery omits empty values and unknown names (pure)", () => {
  const env = makeData();
  assertEqual(env.data.buildAppointmentsQuery({}), "", "no params -> no query");
  assertEqual(env.data.buildAppointmentsQuery(
    { start_day: "", end_day: null, client_id: "x" }), "",
    "empty and unknown omitted");
  assertEqual(env.data.buildAppointmentsQuery(
    { start_day: "2026-07-16", end_day: "2026-07-22" }),
    "?start_day=2026-07-16&end_day=2026-07-22", "fixed order, both present");
});

test("appointments 200 with a valid member is ok and passes through", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody(
        { appointments: [validAppointmentMember()] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(outcome.ok, "expected ok outcome");
  assertEqual(outcome.data.appointments.length, 1, "member passthrough");
  assertEqual(outcome.data.appointments[0].notification_outcome, "pending",
    "outcome passthrough");
});

test("appointments validator fails closed on a missing timezone_name", async () => {
  const env = makeData();
  seedSession(env);
  const body = validAppointmentsBody();
  delete body.timezone_name;
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: body }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "missing timezone must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("F2 bite: appointments validator fails closed on a missing start_day", async () => {
  const env = makeData();
  seedSession(env);
  const body = validAppointmentsBody();
  delete body.start_day;
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: body }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "missing start_day must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("F2 bite: appointments validator fails closed on a malformed end_day", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ end_day: "not-a-date" }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "malformed end_day must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("F2 bite: appointments validator fails closed on a member missing start_datetime", async () => {
  const env = makeData();
  seedSession(env);
  const bad = validAppointmentMember();
  delete bad.start_datetime;   /* the appointment TIME the page formats */
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [bad] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "member without a usable time must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("F2 bite: appointments validator fails closed on a member with empty start_datetime", async () => {
  const env = makeData();
  seedSession(env);
  const bad = validAppointmentMember({ start_datetime: "" });
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [bad] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "empty start_datetime must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("R1 bite: impossible start_day (2026-99-12) fails closed", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ start_day: "2026-99-12" }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "impossible month must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("R1 bite: impossible end_day (2026-02-99) fails closed", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ end_day: "2026-02-99" }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "impossible day must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("R1 bite: non-leap invalid end_day (2027-02-29) fails closed", async () => {
  const env = makeData();
  seedSession(env);
  /* 2027 is not a leap year, so Feb 29 does not exist. */
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ end_day: "2027-02-29" }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "non-leap Feb 29 must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("R1 bite: a real leap date (2028-02-29) is accepted", async () => {
  const env = makeData();
  seedSession(env);
  /* 2028 IS a leap year - proving the check rejects impossibility, not all
   * Feb 29 dates (no false positives). */
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody(
        { start_day: "2028-02-29", end_day: "2028-03-06" }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(outcome.ok, "a real leap-day date must be accepted");
});

test("R1 bite: unparseable start_datetime (not-a-date) fails closed", async () => {
  const env = makeData();
  seedSession(env);
  const bad = validAppointmentMember({ start_datetime: "not-a-date" });
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [bad] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "unparseable start_datetime must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("R1 bite: member missing end_datetime fails closed", async () => {
  const env = makeData();
  seedSession(env);
  const bad = validAppointmentMember();
  delete bad.end_datetime;   /* required appointment-window field */
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [bad] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "missing end_datetime must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("R1 bite: member with unparseable end_datetime fails closed", async () => {
  const env = makeData();
  seedSession(env);
  const bad = validAppointmentMember({ end_datetime: "nope" });
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [bad] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "unparseable end_datetime must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

/* R1-R: start_datetime / end_datetime must be semantically-valid UTC ISO-8601
 * instants in the exact form the backend emits (Z designator). These bites
 * fail against v1.0.2 (which accepted any Date-parseable string) and pass
 * under v1.0.3. */
async function _expectMemberRejected(env, member, why) {
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [member] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, why + " must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
}

test("R1-R bite: start_datetime with NO timezone designator fails closed", async () => {
  /* "2026-08-12T10:00:00" parses in DEVICE time - device-dependent, rejected. */
  await _expectMemberRejected(makeData(),
    validAppointmentMember({ start_datetime: "2026-08-12T10:00:00" }),
    "a naive (no-designator) start_datetime");
});

test("R1-R bite: date-only start_datetime (no time) fails closed", async () => {
  /* "2026-08-12" is a date, not an instant. */
  await _expectMemberRejected(makeData(),
    validAppointmentMember({ start_datetime: "2026-08-12" }),
    "a date-only start_datetime");
});

test("R1-R bite: JS-normalized impossible instant (2026-02-30T10:00:00Z) fails closed", async () => {
  /* new Date() silently normalizes Feb 30 to Mar 2; the component round-trip
   * rejects it. */
  await _expectMemberRejected(makeData(),
    validAppointmentMember({ start_datetime: "2026-02-30T10:00:00Z" }),
    "a JS-normalized impossible instant");
});

test("R1-R bite: end_datetime with a numeric offset (not Z) fails closed", async () => {
  /* The backend on this baseline emits Z, never +00:00; an offset form is
   * outside the actual wire contract and is rejected (not broadened). */
  await _expectMemberRejected(makeData(),
    validAppointmentMember({ end_datetime: "2026-08-12T10:00:00+00:00" }),
    "an offset-form end_datetime outside the wire contract");
});

test("R1-R bite: out-of-range time field (25:00:00Z) fails closed", async () => {
  await _expectMemberRejected(makeData(),
    validAppointmentMember({ start_datetime: "2026-08-12T25:00:00Z" }),
    "an out-of-range hour");
});

test("R1-R positive: whole-second Z instant is accepted (real backend form)", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [
      validAppointmentMember({
        start_datetime: "2026-07-16T14:00:00Z",
        end_datetime: "2026-07-16T14:45:00Z" }) ] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(outcome.ok, "the real whole-second Z form must be accepted");
});

test("R1-R positive: fractional-second Z instant is accepted (real backend form)", async () => {
  const env = makeData();
  seedSession(env);
  /* Postgres timestamptz can carry microseconds; Pydantic renders .ffffffZ. */
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [
      validAppointmentMember({
        start_datetime: "2026-07-16T14:00:00.123456Z",
        end_datetime: "2026-07-16T14:45:00.007000Z" }) ] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(outcome.ok, "the real fractional-second Z form must be accepted");
});

test("R1-R positive: a real leap-day instant (2028-02-29T00:00:00Z) is accepted", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [
      validAppointmentMember({
        start_datetime: "2028-02-29T00:00:00Z",
        end_datetime: "2028-02-29T00:30:00Z" }) ] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(outcome.ok, "a real leap-day instant must be accepted (no false positive)");
});

test("appointments validator fails closed on a malformed member", async () => {
  const env = makeData();
  seedSession(env);
  const bad = validAppointmentMember();
  delete bad.notification_outcome;   /* a field the page dereferences */
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: [bad] }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "malformed member must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("appointments validator fails closed when appointments is not an array", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments", method: "GET" },
    { status: 200, json: validAppointmentsBody({ appointments: null }) }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "non-array appointments must fail closed");
  assertEqual(outcome.state, "invalid_response", "fail closed");
});

test("appointments 401 refresh-and-retry, then final unauthorized", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/appointments", method: "GET" },
    { status: 401, json: {} });
  h.expectConfigLoad(env.fetch);
  env.fetch.expect(
    { urlIncludes: "grant_type=refresh_token", method: "POST" },
    { status: 401, json: {} }
  );
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "a failed refresh after 401 is not ok");
  assertEqual(outcome.state, "unauthorized", "final unauthorized");
});

test("appointments 5xx maps to unavailable", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/appointments", method: "GET" },
    { status: 503, json: {} });
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "5xx not ok");
  assertEqual(outcome.state, "unavailable", "5xx -> unavailable");
});

test("no session means NO appointments request at all", async () => {
  const env = makeData();  /* no seedSession */
  const outcome = await env.data.getAppointments({});
  assert(!outcome.ok, "no session -> not ok");
  assertEqual(outcome.state, "signed_out", "signed_out with zero requests");
  assertEqual(env.fetch.remaining(), 0, "no request was made");
});

/* ==================================================================== */
/* P5-A - Portal Appointment Actions v1: confirmAppointment /            */
/* cancelAppointment data-layer proofs. Each mutation is one authorized  */
/* POST to a path DERIVED from the single appointments literal with a    */
/* URI-encoded id segment, sends NO request body (no tenant selector -   */
/* tenancy is the bearer token alone), and validates the success body to */
/* EXACTLY the approved appointment field set (C6). Every bite below     */
/* FAILS against untouched fd967de: confirmAppointment/cancelAppointment */
/* do not exist there.                                                   */
/* ==================================================================== */

test("confirmAppointment POSTs the encoded action path with the Bearer token and NO body", async () => {
  const env = makeData();
  seedSession(env);
  const id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
  env.fetch.expect(
    { urlEquals: "/portal/appointments/" + id + "/confirm", method: "POST",
      headerEquals: { "Authorization": "Bearer tok-a" } },
    { status: 200, json: validAppointmentMember({ status: "confirmed",
      confirmed_at: "2026-07-16T14:01:00Z" }) }
  );
  const outcome = await env.data.confirmAppointment(id);
  assert(outcome.ok, "a valid 200 confirm body is ok");
  assertEqual(outcome.data.status, "confirmed", "the confirmed member is returned");
  const seen = env.fetch.seen();
  assertEqual(seen[seen.length - 1].method, "POST", "confirm is a POST");
  assertEqual(seen[seen.length - 1].body, undefined,
    "confirm sends NO request body (no tenant selector)");
});

test("confirmAppointment URI-encodes the id into ONE path segment", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments/a%2Fb%20c/confirm", method: "POST" },
    { status: 200, json: validAppointmentMember({ status: "confirmed",
      confirmed_at: "2026-07-16T14:01:00Z" }) }
  );
  const outcome = await env.data.confirmAppointment("a/b c");
  assert(outcome.ok, "encoded id path is used verbatim");
});

test("cancelAppointment POSTs the encoded cancel path with NO body", async () => {
  const env = makeData();
  seedSession(env);
  const id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
  env.fetch.expect(
    { urlEquals: "/portal/appointments/" + id + "/cancel", method: "POST" },
    { status: 200, json: validAppointmentMember({ appointment_id: id,
      status: "cancelled" }) }
  );
  const outcome = await env.data.cancelAppointment(id);
  assert(outcome.ok, "a valid 200 cancel body is ok");
  assertEqual(outcome.data.status, "cancelled", "the cancelled member is returned");
  const seen = env.fetch.seen();
  assertEqual(seen[seen.length - 1].body, undefined, "cancel sends NO request body");
});

test("action success body with an EXTRA field fails closed (exact-key C6)", async () => {
  const env = makeData();
  seedSession(env);
  const leaky = validAppointmentMember({ status: "confirmed",
    confirmed_at: "2026-07-16T14:01:00Z" });
  leaky.slot_id = "should-never-be-here";  /* one extra key */
  env.fetch.expect(
    { urlEquals: "/portal/appointments/x/confirm", method: "POST" },
    { status: 200, json: leaky }
  );
  const outcome = await env.data.confirmAppointment("x");
  assert(!outcome.ok, "an extra field must fail the whole body closed");
  assertEqual(outcome.state, "invalid_response", "fail closed on extra key");
});

test("action success body MISSING a field fails closed (exact-key C6)", async () => {
  const env = makeData();
  seedSession(env);
  const partial = validAppointmentMember({ status: "confirmed",
    confirmed_at: "2026-07-16T14:01:00Z" });
  delete partial.notification_outcome;  /* one missing key */
  env.fetch.expect(
    { urlEquals: "/portal/appointments/x/confirm", method: "POST" },
    { status: 200, json: partial }
  );
  const outcome = await env.data.confirmAppointment("x");
  assert(!outcome.ok, "a missing field must fail the whole body closed");
  assertEqual(outcome.state, "invalid_response", "fail closed on missing key");
});

test("confirm 409 maps to conflict", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/appointments/x/confirm", method: "POST" },
    { status: 409, json: {} });
  const outcome = await env.data.confirmAppointment("x");
  assert(!outcome.ok, "409 not ok");
  assertEqual(outcome.state, "conflict", "409 -> conflict");
});

test("confirm 404 maps to not_found", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/appointments/x/confirm", method: "POST" },
    { status: 404, json: {} });
  const outcome = await env.data.confirmAppointment("x");
  assertEqual(outcome.state, "not_found", "404 -> not_found");
});

test("confirm 500 (fail-closed backend guardrail) maps to unavailable", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/appointments/x/confirm", method: "POST" },
    { status: 500, json: {} });
  const outcome = await env.data.confirmAppointment("x");
  assertEqual(outcome.state, "unavailable", "500 -> unavailable (honest failure)");
});

test("cancel 409 (already cancelled) maps to conflict", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/appointments/x/cancel", method: "POST" },
    { status: 409, json: {} });
  const outcome = await env.data.cancelAppointment("x");
  assertEqual(outcome.state, "conflict", "409 -> conflict");
});

test("no session means NO confirm request at all", async () => {
  const env = makeData();  /* no seedSession */
  const outcome = await env.data.confirmAppointment("x");
  assert(!outcome.ok, "no session -> not ok");
  assertEqual(outcome.state, "signed_out", "signed_out with zero requests");
  assertEqual(env.fetch.remaining(), 0, "no request was made");
});

/* ------------------------------------------------------------------ */
/* P6-A: notification-settings data layer                              */
/* ------------------------------------------------------------------ */

function validSettingsBody(overrides) {
  return Object.assign({
    notification_email: "office@example.com",
    notification_phone: "+15550001111",
    notification_settings_updated_at: "2026-08-13T12:00:00.123456Z"
  }, overrides || {});
}

test("P6-A: GET carries the Bearer token to the settings endpoint", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/notification-settings", method: "GET",
      headerEquals: { "Authorization": "Bearer tok-a" } },
    { status: 200, json: validSettingsBody() }
  );
  const outcome = await env.data.getNotificationSettings();
  assert(outcome.ok, "GET ok");
  assertEqual(outcome.data.notification_email, "office@example.com",
    "email surfaced");
  assertEqual(env.fetch.remaining(), 0, "exactly one request");
});

test("P6-A: PUT sends exactly the three keys and echoes the token verbatim",
  async () => {
    const env = makeData();
    seedSession(env);
    /* A fractional-second token: it must survive byte-for-byte (contract C4 -
     * the data layer treats it as an opaque string, never Date/parse). */
    const token = "2026-08-13T12:00:00.123456Z";
    env.fetch.expect(
      { urlEquals: "/portal/notification-settings", method: "PUT",
        headerEquals: { "Authorization": "Bearer tok-a" },
        bodyJson: { notification_email: "office@example.com",
          notification_phone: "+15550001111",
          expected_notification_settings_updated_at: token } },
      { status: 200, json: validSettingsBody() }
    );
    const outcome = await env.data.putNotificationSettings(
      "office@example.com", "+15550001111", token);
    assert(outcome.ok, "PUT ok");
    assertEqual(env.fetch.remaining(), 0, "exactly one request");
    const raw = env.fetch.seen()[0].body;
    assert(raw.indexOf(
      '"expected_notification_settings_updated_at":"' + token + '"') !== -1,
      "the fractional token is present verbatim in the PUT body");
  });

test("P6-A: PUT round-trips a null token as JSON null and clears via null",
  async () => {
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/notification-settings", method: "PUT",
        bodyJson: { notification_email: null,
          notification_phone: "516-555-7777",
          expected_notification_settings_updated_at: null } },
      { status: 200, json: validSettingsBody(
        { notification_email: null, notification_phone: "516-555-7777" }) }
    );
    const outcome = await env.data.putNotificationSettings(
      null, "516-555-7777", null);
    assert(outcome.ok, "PUT ok");
    const raw = env.fetch.seen()[0].body;
    assert(raw.indexOf(
      '"expected_notification_settings_updated_at":null') !== -1,
      "a null token is serialized as JSON null");
  });

test("P6-A: a settings 200 with extra/missing/wrong/bad-token fields fails closed",
  async () => {
    const badBodies = [
      { notification_email: "a@b.com", notification_phone: null,
        notification_settings_updated_at: "2026-08-13T12:00:00Z",
        extra: 1 },                                     /* extra key */
      { notification_email: "a@b.com", notification_phone: null },  /* missing token */
      { notification_email: 5, notification_phone: null,
        notification_settings_updated_at: null },       /* wrong type */
      { notification_email: "a@b.com", notification_phone: null,
        notification_settings_updated_at: "not-a-timestamp" }  /* bad token */
    ];
    for (const body of badBodies) {
      const env = makeData();
      seedSession(env);
      env.fetch.expect(
        { urlEquals: "/portal/notification-settings", method: "GET" },
        { status: 200, json: body });
      const outcome = await env.data.getNotificationSettings();
      assert(!outcome.ok, "unusable body rejected");
      assertEqual(outcome.state, "invalid_response", "fail closed");
    }
  });

test("P6-A: settings PUT maps 409 -> conflict and 422 -> bad_request",
  async () => {
    let env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/notification-settings", method: "PUT" },
      { status: 409, json: {} });
    let outcome = await env.data.putNotificationSettings(
      "a@b.com", "+15550001111", "2026-08-13T12:00:00Z");
    assertEqual(outcome.state, "conflict", "409 -> conflict");

    env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/notification-settings", method: "PUT" },
      { status: 422, json: { detail: "bad" } });
    outcome = await env.data.putNotificationSettings(
      "a@b.com", "+15550001111", null);
    assertEqual(outcome.state, "bad_request", "422 -> bad_request");
  });

test("P6-A: no session means NO settings request at all", async () => {
  const env = makeData();  /* no seedSession */
  const getOutcome = await env.data.getNotificationSettings();
  assertEqual(getOutcome.state, "signed_out", "GET signed_out");
  const putOutcome = await env.data.putNotificationSettings(
    "a@b.com", null, null);
  assertEqual(putOutcome.state, "signed_out", "PUT signed_out");
  assertEqual(env.fetch.seen().length, 0, "no request was made");
});


/* ------------------------------------------------------------------ */
/* PHASE 3A Slice 3: bookScheduleSlot (POST /portal/schedule/slots/     */
/* <id>/book). The path is DERIVED from the one schedule literal; the   */
/* body is the caller's patient-entered fields VERBATIM (never a        */
/* tenant, status, source, provider, service, datetime or urgency);     */
/* the success body is the SAME exact-key appointment shape the P5-A    */
/* actions return, judged by the SAME validator.                        */
/* ------------------------------------------------------------------ */

test("Slice 3: bookScheduleSlot POSTs the fields VERBATIM to the URI-encoded book path", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/slots/slot%20one/book", method: "POST",
      headerEquals: { "Authorization": "Bearer tok-a" },
      bodyJson: { patient_name: "Kevin Alvarado",
        patient_phone: "516-555-1234" } },
    { status: 200, json: validAppointmentMember({ source: "portal_staff" }) }
  );
  const outcome = await env.data.bookScheduleSlot("slot one",
    { patient_name: "Kevin Alvarado", patient_phone: "516-555-1234" });
  assert(outcome.ok, "a valid booked-appointment body is accepted");
  assertEqual(outcome.data.appointment_id,
    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "the appointment came through");
  assertEqual(env.fetch.remaining(), 0, "exactly one request");
});

test("Slice 3: optional patient fields ride along verbatim - and nothing else", async () => {
  const env = makeData();
  seedSession(env);
  const fields = { patient_name: "Kevin Alvarado",
    patient_phone: "516-555-1234", patient_email: "kevin@example.test",
    new_or_returning: "new", reason: "implant consultation" };
  env.fetch.expect(
    { urlEquals: "/portal/schedule/slots/s1/book", method: "POST",
      bodyJson: fields },
    { status: 200, json: validAppointmentMember() }
  );
  const outcome = await env.data.bookScheduleSlot("s1", fields);
  assert(outcome.ok, "accepted");
  assertEqual(env.fetch.remaining(), 0,
    "the body was EXACTLY the caller's fields - deep-equal, so a grown " +
    "tenant/status/source/urgency key would have failed the match");
});

test("Slice 3: book outcome mapping - 409 conflict, 404 not_found, 422 bad_request", async () => {
  const cases = [
    [409, "conflict", { detail: "Slot is no longer available." }],
    [404, "not_found", { detail: "Slot not found." }],
    [422, "bad_request", { detail: "patient_name and patient_phone are required." }]
  ];
  for (const [status, state, json] of cases) {
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/schedule/slots/s1/book", method: "POST" },
      { status, json }
    );
    const outcome = await env.data.bookScheduleSlot("s1",
      { patient_name: "K", patient_phone: "5" });
    assert(!outcome.ok, status + " is not ok");
    assertEqual(outcome.state, state, status + " -> " + state);
  }
});

test("Slice 3: a book success with an EXTRA key fails closed as invalid_response", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/schedule/slots/s1/book", method: "POST" },
    { status: 200,
      json: validAppointmentMember({ client_id: "tenant-leak" }) }
  );
  const outcome = await env.data.bookScheduleSlot("s1",
    { patient_name: "K", patient_phone: "5" });
  assert(!outcome.ok, "a leaked field is never rendered");
  assertEqual(outcome.state, "invalid_response",
    "the exact-key appointment contract fails the whole body closed");
});


/* ------------------------------------------------------------------ */
/* PHASE 3A Slice 4B2: setAppointmentInternalNote (PUT .../internal-   */
/* note). One required-but-nullable body key, ALWAYS sent (the frozen   */
/* 4B1 contract); the note travels ONLY in the request body - the       */
/* exact-URL assertions prove no note text ever enters the URL.         */
/* ------------------------------------------------------------------ */

test("Slice 4B2: note save PUTs exactly {internal_note} to the exact path", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments/appt%20one/internal-note",
      method: "PUT",
      headerEquals: { "Authorization": "Bearer tok-a" },
      bodyJson: { internal_note: "gate code 4411\nring twice" } },
    { status: 200, json: validAppointmentMember(
        { internal_note: "gate code 4411\nring twice" }) }
  );
  const outcome = await env.data.setAppointmentInternalNote(
    "appt one", "gate code 4411\nring twice");
  assert(outcome.ok, "the exact-key appointment body is accepted");
  assertEqual(outcome.data.internal_note, "gate code 4411\nring twice",
    "the server-normalized note comes back to the caller");
  assertEqual(env.fetch.remaining(), 0, "exactly one request");
});

test("Slice 4B2: an explicit null clear sends {internal_note:null}", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments/a1/internal-note", method: "PUT",
      bodyJson: { internal_note: null } },
    { status: 200, json: validAppointmentMember({ internal_note: null }) }
  );
  const outcome = await env.data.setAppointmentInternalNote("a1", null);
  assert(outcome.ok, "the clear round-trips");
  assertEqual(outcome.data.internal_note, null, "cleared");
});

test("Slice 4B2: note-save outcome mapping - 404, 422, 409", async () => {
  const cases = [[404, "not_found"], [422, "bad_request"],
    [409, "conflict"]];
  for (const [status, state] of cases) {
    const env = makeData();
    seedSession(env);
    env.fetch.expect(
      { urlEquals: "/portal/appointments/a1/internal-note", method: "PUT" },
      { status, json: { detail: "x" } }
    );
    const outcome = await env.data.setAppointmentInternalNote("a1", "n");
    assert(!outcome.ok, status + " is not ok");
    assertEqual(outcome.state, state, status + " -> " + state);
  }
});

test("Slice 4B2: a note-save success with an EXTRA key fails closed", async () => {
  const env = makeData();
  seedSession(env);
  env.fetch.expect(
    { urlEquals: "/portal/appointments/a1/internal-note", method: "PUT" },
    { status: 200,
      json: validAppointmentMember({ client_id: "tenant-leak" }) }
  );
  const outcome = await env.data.setAppointmentInternalNote("a1", "n");
  assert(!outcome.ok, "a leaked field is never rendered");
  assertEqual(outcome.state, "invalid_response", "fails the body closed");
});
