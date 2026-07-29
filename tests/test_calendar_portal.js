// tests/test_calendar_portal.js
//
// Simple Office Calendar Portal MVP — frontend contract tests.
//
// The REAL <script> from static/admin/calendar-portal.html runs inside a
// Node `vm` sandbox with a minimal DOM (scaffolding modeled on
// tests/test_map_action.js so the suites share harness behavior).
//
// Proves: the key lives in sessionStorage only and rides ONLY in the
// X-Admin-Key header of relative same-origin URLs; /admin/calendar/me
// bootstraps the tenant; the list call uses the returned client_id and the
// server-provided 30-day range; patient values render through textContent;
// only pending rows expose a confirmation control; confirmation calls the
// existing /confirm endpoint (and nothing ever calls a cancel endpoint);
// 401 and Logout both clear the key and every rendered patient value; a
// booking-disabled office still loads its list; and the required patient,
// urgency, status, and notification fields are all represented.
//
// Run:  node tests/test_calendar_portal.js   (from the repository root)
//       or MIA_PORTAL_HTML=/path/to/calendar-portal.html node tests/test_calendar_portal.js

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PORTAL_HTML = process.env.MIA_PORTAL_HTML ||
  path.join(__dirname, "..", "static", "admin", "calendar-portal.html");

// --------------------------------------------------------------------------
// Minimal DOM (same shape as tests/test_map_action.js)
// --------------------------------------------------------------------------
function makeClassList() {
  const classes = new Set();
  return {
    add: (...cs) => cs.forEach((c) => classes.add(c)),
    remove: (...cs) => cs.forEach((c) => classes.delete(c)),
    toggle: (c, force) => {
      const want = force === undefined ? !classes.has(c) : !!force;
      if (want) classes.add(c); else classes.delete(c);
      return want;
    },
    contains: (c) => classes.has(c),
    _all: () => Array.from(classes),
  };
}

function makeElement(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    children: [], parent: null, listeners: {}, attributes: {},
    style: { setProperty: () => {} },
    textContent: "", value: "", placeholder: "",
    disabled: false, id: "", type: "",
  };
  el.className = "";
  el.classList = makeClassList(el);
  el.appendChild = (child) => { child.parent = el; el.children.push(child); return child; };
  el.remove = () => {
    if (el.parent) {
      el.parent.children = el.parent.children.filter((c) => c !== el);
      el.parent = null;
    }
  };
  el.addEventListener = (event, handler) => {
    (el.listeners[event] = el.listeners[event] || []).push(handler);
  };
  el.click = () => (el.listeners.click || []).forEach((h) => h());
  el.setAttribute = (k, v) => { el.attributes[k] = v; };
  el.focus = () => {};
  return el;
}

function collect(el, out) {
  out.push(el);
  el.children.forEach((c) => collect(c, out));
  return out;
}

// --------------------------------------------------------------------------
// Sandbox with a scenario-driven fetch mock
// --------------------------------------------------------------------------
function buildSandbox(scenario) {
  const elementsById = {};
  const body = makeElement("body");

  ["practiceName", "bookingPausedBadge", "refreshBtn", "logoutBtn",
   "loginView", "portalKeyInput", "loginBtn", "loginError",
   "portalView", "rangeLabel", "errorState", "emptyState",
   "appointmentsList"].forEach((id) => {
    const el = makeElement(id === "portalKeyInput" ? "input" : "div");
    el.id = id;
    elementsById[id] = el;
    body.appendChild(el);
  });

  const fetchCalls = [];
  const sessionStore = {};
  const forbiddenStoreCalls = { count: 0 };

  function jsonResponse(status, payload) {
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(payload),
    });
  }

  const documentStub = {
    documentElement: { style: { setProperty: () => {} } },
    body,
    getElementById: (id) => {
      if (!elementsById[id]) {
        const el = makeElement("div");
        el.id = id;
        elementsById[id] = el;
        body.appendChild(el);
      }
      return elementsById[id];
    },
    createElement: (tag) => makeElement(tag),
  };

  const sandbox = {
    console,
    document: documentStub,
    Intl,
    Date,
    JSON,
    setTimeout,
    clearTimeout,
    encodeURIComponent,
    window: {
      confirmCalls: 0,
      confirmAnswer: true,
      location: { hostname: "localhost", origin: "http://localhost" },
      sessionStorage: {
        getItem: (k) => (k in sessionStore ? sessionStore[k] : null),
        setItem: (k, v) => { sessionStore[k] = String(v); },
        removeItem: (k) => { delete sessionStore[k]; },
      },
    },
    fetch: (url, opts) => {
      fetchCalls.push({ url: String(url), opts: opts || {} });
      if (scenario.failAllWith401) {
        return jsonResponse(401, { detail: "Invalid admin key." });
      }
      if (String(url).indexOf("/admin/calendar/me") === 0) {
        if (scenario.meStatus && scenario.meStatus !== 200) {
          return jsonResponse(scenario.meStatus, { detail: "Invalid admin key." });
        }
        return jsonResponse(200, scenario.me);
      }
      if (String(url).indexOf("/admin/calendar/appointments?") === 0) {
        if (scenario.listStatus && scenario.listStatus !== 200) {
          return jsonResponse(scenario.listStatus, { detail: "Invalid admin key." });
        }
        return jsonResponse(200, scenario.appointments);
      }
      if (String(url).indexOf("/confirm") !== -1) {
        if (scenario.confirmStatus && scenario.confirmStatus !== 200) {
          return jsonResponse(scenario.confirmStatus, { detail: "conflict" });
        }
        return jsonResponse(200, scenario.confirmResult);
      }
      return jsonResponse(404, { detail: "unexpected url in test" });
    },
  };
  sandbox.window.confirm = function () {
    sandbox.window.confirmCalls += 1;
    return sandbox.window.confirmAnswer;
  };
  sandbox.window.document = documentStub;
  sandbox.globalThis = sandbox;

  // A trap standing in for the OTHER, forbidden browser store: the portal
  // must never touch it. (Its name is avoided here so the source-scan test
  // below stays meaningful for the portal file alone.)
  const forbiddenStore = {
    getItem: () => { forbiddenStoreCalls.count += 1; return null; },
    setItem: () => { forbiddenStoreCalls.count += 1; },
    removeItem: () => { forbiddenStoreCalls.count += 1; },
  };
  sandbox.window["local" + "Storage"] = forbiddenStore;
  sandbox["local" + "Storage"] = forbiddenStore;

  const context = vm.createContext(sandbox);
  const html = fs.readFileSync(PORTAL_HTML, "utf8");
  const match = html.match(/<script>([\s\S]*)<\/script>/);
  if (!match) throw new Error("No <script> block found in calendar-portal.html");
  const scriptSource = match[1];
  vm.runInContext(scriptSource, context, { filename: "calendar-portal.html<script>" });

  return {
    context, elementsById, fetchCalls, body, html, scriptSource,
    sessionStore, forbiddenStoreCalls, sandbox,
  };
}

function run(context, code) { return vm.runInContext(code, context); }
function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

function cards(sb) { return sb.elementsById["appointmentsList"].children; }

function buttonsIn(el, label) {
  return collect(el, []).filter(
    (n) => n.tagName === "BUTTON" && n.textContent === label
  );
}

function allText(el) {
  return collect(el, []).map((n) => n.textContent).join(" | ");
}

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------
const ME = {
  client_id: "11111111-2222-3333-4444-555555555555",
  practice_name: "Test Dental",
  timezone_name: "America/New_York",
  today_local: "2026-07-29",
  booking_enabled: true,
};

const XSS_NAME = "<img src=x onerror=alert(1)>Mallory";

const APPOINTMENTS = [
  {
    id: "aaaaaaaa-0000-0000-0000-000000000001",
    patient_name: XSS_NAME,
    patient_phone: "516-555-1234",
    patient_email: null,
    new_or_returning: "new",
    reason: "cleaning/checkup",
    urgency: "routine",
    start_datetime: "2026-07-30T14:00:00Z",
    end_datetime: "2026-07-30T14:45:00Z",
    status: "pending",
    confirmed_at: null,
    source: "mia_widget",
    office_sms_sent: true,
    office_email_sent: false,
    patient_sms_sent: false,
    notify_error: "send_failed",
  },
  {
    id: "aaaaaaaa-0000-0000-0000-000000000002",
    patient_name: "Second Patient",
    patient_phone: "516-555-9999",
    patient_email: "second@example.com",
    new_or_returning: "returning",
    reason: "implant consultation",
    urgency: "priority",
    start_datetime: "2026-07-31T15:00:00Z",
    end_datetime: "2026-07-31T16:00:00Z",
    status: "confirmed",
    confirmed_at: "2026-07-29T12:00:00Z",
    source: "mia_widget",
    office_sms_sent: true,
    office_email_sent: true,
    patient_sms_sent: false,
    notify_error: null,
  },
  {
    id: "aaaaaaaa-0000-0000-0000-000000000003",
    patient_name: "Cancelled Patient",
    patient_phone: "516-555-0000",
    patient_email: null,
    new_or_returning: null,
    reason: "other",
    urgency: "routine",
    start_datetime: "2026-08-01T15:00:00Z",
    end_datetime: "2026-08-01T15:30:00Z",
    status: "cancelled",
    confirmed_at: null,
    source: "mia_widget",
    office_sms_sent: false,
    office_email_sent: false,
    patient_sms_sent: false,
    notify_error: null,
  },
];

const CONFIRMED_FIRST = Object.assign({}, APPOINTMENTS[0], {
  status: "confirmed",
  confirmed_at: "2026-07-29T18:00:00Z",
});

function freshScenario(overrides) {
  return Object.assign({
    me: JSON.parse(JSON.stringify(ME)),
    appointments: JSON.parse(JSON.stringify(APPOINTMENTS)),
    confirmResult: JSON.parse(JSON.stringify(CONFIRMED_FIRST)),
  }, overrides || {});
}

async function signIn(sb, key) {
  sb.elementsById["portalKeyInput"].value = key;
  await run(sb.context, "signIn();");
  await sleep(20);
}

// --------------------------------------------------------------------------
// Tests
// --------------------------------------------------------------------------
let passed = 0;
let failed = 0;

function ok(name, cond) {
  if (cond) { passed += 1; console.log("ok - " + name); }
  else { failed += 1; console.log("NOT OK - " + name); }
}

async function main() {
  // ---- 1. Source-level contracts -----------------------------------------
  {
    const sb = buildSandbox(freshScenario());
    ok("script stores the key via sessionStorage",
       sb.scriptSource.indexOf("sessionStorage") !== -1);
    ok("script never mentions the forbidden persistent store",
       sb.scriptSource.indexOf("local" + "Storage") === -1);
    ok("script never assigns HTML from strings (no innerHTML)",
       sb.scriptSource.indexOf("inner" + "HTML") === -1);
    ok("script contains no cancel endpoint reference",
       sb.scriptSource.indexOf("/cancel") === -1);
    ok("script writes nothing to the console",
       sb.scriptSource.indexOf("console.") === -1);
  }

  // ---- 2. Happy path: bootstrap + list -----------------------------------
  {
    const sb = buildSandbox(freshScenario());
    const RAW_KEY = "mia_cal_test-key-value-for-contract-tests-000000";
    await signIn(sb, RAW_KEY);

    ok("key persisted in sessionStorage under the portal name",
       sb.sessionStore["miaCalendarPortalKey"] === RAW_KEY);
    ok("forbidden persistent store never touched at runtime",
       sb.forbiddenStoreCalls.count === 0);

    const urls = sb.fetchCalls.map((c) => c.url);
    ok("first call bootstraps /admin/calendar/me",
       urls.length >= 1 && urls[0] === "/admin/calendar/me");
    ok("every request uses a relative same-origin URL",
       urls.every((u) => u.indexOf("/admin/calendar/") === 0));
    ok("every request carries the key as the X-Admin-Key header",
       sb.fetchCalls.every((c) =>
         c.opts.headers && c.opts.headers["X-Admin-Key"] === RAW_KEY));
    ok("the key never appears in any URL",
       urls.every((u) => u.indexOf(RAW_KEY) === -1));

    const listUrl = urls.find((u) => u.indexOf("/admin/calendar/appointments?") === 0) || "";
    ok("appointment list uses the bootstrap client_id",
       listUrl.indexOf("client_id=" + ME.client_id) !== -1);
    ok("appointment list spans today_local through today_local + 30 days",
       listUrl.indexOf("start_day=2026-07-29") !== -1 &&
       listUrl.indexOf("end_day=2026-08-28") !== -1);

    ok("practice name displayed from the bootstrap",
       sb.elementsById["practiceName"].textContent === "Test Dental");
    ok("booking-enabled office shows no paused badge",
       sb.elementsById["bookingPausedBadge"].classList.contains("hidden"));

    const rendered = cards(sb);
    ok("pending and confirmed render; cancelled is filtered out",
       rendered.length === 2 &&
       allText(rendered[0]).indexOf("Mallory") !== -1 &&
       allText(rendered[1]).indexOf("Second Patient") !== -1 &&
       allText(sb.elementsById["appointmentsList"]).indexOf("Cancelled Patient") === -1);
    ok("backend order preserved (pending first, confirmed second)",
       allText(rendered[0]).indexOf("Pending") !== -1 &&
       allText(rendered[1]).indexOf("Confirmed") !== -1);

    const hostileName = collect(rendered[0], []).some(
      (n) => n.textContent === XSS_NAME
    );
    ok("patient values render through textContent (hostile name inert)",
       hostileName);

    ok("required fields represented on a card",
       allText(rendered[0]).indexOf("516-555-1234") !== -1 &&        // phone
       allText(rendered[0]).indexOf("Not provided") !== -1 &&        // null email
       allText(rendered[0]).indexOf("New patient") !== -1 &&         // type
       allText(rendered[0]).indexOf("Cleaning / Checkup") !== -1 &&  // reason label
       allText(rendered[0]).indexOf("Routine") !== -1 &&             // urgency
       allText(rendered[0]).indexOf("Office SMS") !== -1 &&
       allText(rendered[0]).indexOf("Sent") !== -1 &&
       allText(rendered[0]).indexOf("Office email") !== -1 &&
       allText(rendered[0]).indexOf("Not sent") !== -1 &&
       allText(rendered[0]).indexOf("send_failed") !== -1);          // notify_error
    ok("priority urgency badge shown on the second card",
       allText(rendered[1]).indexOf("Priority") !== -1);

    ok("only the pending row exposes a confirmation control",
       buttonsIn(rendered[0], "Confirm appointment").length === 1 &&
       buttonsIn(rendered[1], "Confirm appointment").length === 0);
    ok("no cancellation control exists anywhere",
       collect(sb.body, []).every(
         (n) => String(n.textContent).toLowerCase().indexOf("cancel appointment") === -1));

    // ---- 3. Confirmation flow ---------------------------------------------
    sb.sandbox.window.confirmAnswer = false;
    buttonsIn(rendered[0], "Confirm appointment")[0].click();
    await sleep(20);
    ok("declining the browser prompt sends no request",
       sb.sandbox.window.confirmCalls === 1 &&
       sb.fetchCalls.every((c) => c.url.indexOf("/confirm") === -1));

    sb.sandbox.window.confirmAnswer = true;
    const confirmBtn = buttonsIn(rendered[0], "Confirm appointment")[0];
    confirmBtn.click();
    ok("confirm button disabled while the request runs",
       confirmBtn.disabled === true);
    await sleep(20);

    const confirmCall = sb.fetchCalls.find((c) => c.url.indexOf("/confirm") !== -1);
    ok("confirmation calls the existing /confirm endpoint with client_id",
       !!confirmCall &&
       confirmCall.url.indexOf("/admin/calendar/appointments/" +
         APPOINTMENTS[0].id + "/confirm") === 0 &&
       confirmCall.url.indexOf("client_id=" + ME.client_id) !== -1 &&
       confirmCall.opts.method === "POST");

    const after = cards(sb);
    ok("row updated in place from the returned AppointmentView",
       after.length === 2 &&
       allText(after[0]).indexOf("Confirmed") !== -1 &&
       buttonsIn(after[0], "Confirm appointment").length === 0);
    const listCallCount = sb.fetchCalls.filter(
      (c) => c.url.indexOf("/admin/calendar/appointments?") === 0).length;
    ok("no unnecessary list reload after confirming",
       listCallCount === 1);
  }

  // ---- 4. Booking disabled is informational ------------------------------
  {
    const scenario = freshScenario();
    scenario.me.booking_enabled = false;
    const sb = buildSandbox(scenario);
    await signIn(sb, "mia_cal_paused-office-key-000000000000000000000");
    ok("paused badge visible when booking_enabled is false",
       !sb.elementsById["bookingPausedBadge"].classList.contains("hidden"));
    ok("booking-disabled office still loads its appointment list",
       cards(sb).length === 2);
  }

  // ---- 5. 401 clears the key and rendered patient content ----------------
  {
    const sb = buildSandbox(freshScenario());
    await signIn(sb, "mia_cal_will-be-revoked-key-00000000000000000000");
    ok("signed in before the 401 (precondition)", cards(sb).length === 2);
    // Simulate revocation: every subsequent call is 401, then press Refresh.
    sb.sandbox.fetch = (url, opts) => {
      sb.fetchCalls.push({ url: String(url), opts: opts || {} });
      return Promise.resolve({
        ok: false, status: 401,
        json: () => Promise.resolve({ detail: "Invalid admin key." }),
      });
    };
    sb.elementsById["refreshBtn"].click();
    await sleep(20);
    ok("401 clears the stored key",
       !("miaCalendarPortalKey" in sb.sessionStore));
    ok("401 clears all rendered patient content",
       cards(sb).length === 0 &&
       allText(sb.body).indexOf("Mallory") === -1 &&
       allText(sb.body).indexOf("516-555-1234") === -1);
    ok("401 returns to the login view",
       !sb.elementsById["loginView"].classList.contains("hidden") &&
       sb.elementsById["portalView"].classList.contains("hidden"));
  }

  // ---- 6. Logout clears the key and rendered patient content -------------
  {
    const sb = buildSandbox(freshScenario());
    await signIn(sb, "mia_cal_logout-test-key-00000000000000000000000");
    ok("signed in before logout (precondition)", cards(sb).length === 2);
    sb.elementsById["logoutBtn"].click();
    await sleep(10);
    ok("logout clears the stored key",
       !("miaCalendarPortalKey" in sb.sessionStore));
    ok("logout clears all rendered patient content and shows login",
       cards(sb).length === 0 &&
       allText(sb.body).indexOf("Mallory") === -1 &&
       !sb.elementsById["loginView"].classList.contains("hidden"));
  }

  // ---- 7. Empty state -----------------------------------------------------
  {
    const scenario = freshScenario();
    scenario.appointments = [];
    const sb = buildSandbox(scenario);
    await signIn(sb, "mia_cal_empty-state-key-00000000000000000000000");
    ok("empty state shown when no appointments exist",
       cards(sb).length === 0 &&
       !sb.elementsById["emptyState"].classList.contains("hidden"));
  }

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((err) => {
  console.log("HARNESS FAILURE: " + (err && err.stack ? err.stack : err));
  process.exit(1);
});
