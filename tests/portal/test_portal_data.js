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
    notification_outcome: "pending"
  }, overrides || {});
}

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
