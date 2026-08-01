// tests/test_calendar_picker_prototype_b.js
//
// Mia Calendar Prototype B — B3 standalone read-only review page:
// frontend contract tests.
//
// The REAL <script> from static/admin/calendar-picker-prototype-b.html runs
// inside a Node `vm` sandbox with a minimal DOM (scaffolding modeled on
// tests/test_calendar_portal.js so the suites share harness behavior).
//
// Proves: the page is inert on load; the only Calendar endpoint it can reach
// is GET /admin/calendar/availability-preview; the credential rides ONLY in
// the X-Admin-Key header of the request and never appears in a URL, in page
// text, or in diagnostics; no browser storage API exists in the source; a
// blank service key is omitted while a supplied one is trimmed with its case
// preserved; the 7-day action makes exactly one request spanning start_day
// plus six local days; the month view is lazy and uses one range request of
// at most 31 inclusive days; only the four locked day states render and any
// other state is a visible review error; no daily counts and no slot
// identifiers appear; selecting an open day issues exactly one request on
// the same active range; clicking a time makes zero requests and shows the
// locked preview-only statement; a newer request outdates an older one; the
// five failure classes are distinguishable; and frozen Prototype A remains
// byte-for-byte unchanged.
//
// Run:  node tests/test_calendar_picker_prototype_b.js   (from the repo root)
//       or MIA_PICKER_B_HTML=/path/to/calendar-picker-prototype-b.html \
//          MIA_PROTOTYPE_A_HTML=/path/to/calendar-picker-prototype.html \
//          node tests/test_calendar_picker_prototype_b.js

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PICKER_B_HTML = process.env.MIA_PICKER_B_HTML ||
  path.join(__dirname, "..", "static", "admin", "calendar-picker-prototype-b.html");
const PROTOTYPE_A_HTML = process.env.MIA_PROTOTYPE_A_HTML ||
  path.join(__dirname, "..", "static", "admin", "calendar-picker-prototype.html");

// The owner-frozen Prototype A content hash (B3 design gate / handoff).
const PROTOTYPE_A_SHA256 =
  "16b2f76c62ae377ea7dadbf21a965640ff7e828934197b4eb91e32c93e1e7570";

// --------------------------------------------------------------------------
// Minimal DOM (same shape as tests/test_calendar_portal.js)
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
  el.setAttribute = (k, v) => { el.attributes[k] = String(v); };
  el.getAttribute = (k) => (k in el.attributes ? el.attributes[k] : null);
  el.focus = () => {};
  return el;
}

function collect(el, out) {
  out.push(el);
  el.children.forEach((c) => collect(c, out));
  return out;
}

// --------------------------------------------------------------------------
// Deterministic contract fixtures. The fetch mock synthesizes an exact
// AvailabilityPreviewResponse from the REQUESTED query parameters, so week,
// month, and adjacent-month requests all get contract-true payloads.
// --------------------------------------------------------------------------
const CID = "11111111-2222-3333-4444-555555555555";
const RAW_KEY = "mia_cal_disposable-test-credential-000000000000";

const WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday",
  "Thursday", "Friday", "Saturday"];
// Deterministic repeating pattern; index 1 of any range is always "open".
const STATE_PATTERN = ["past", "open", "full", "unavailable"];

function pad2(n) { return (n < 10 ? "0" : "") + n; }

function addDays(isoDate, days) {
  const p = isoDate.split("-");
  const d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2] + days));
  return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" +
         pad2(d.getUTCDate());
}

function weekdayName(isoDate) {
  const p = isoDate.split("-");
  return WEEKDAY_NAMES[new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])).getUTCDay()];
}

function inclusiveDays(startDay, endDay) {
  const days = [];
  let d = startDay;
  for (let guard = 0; guard < 62 && d <= endDay; guard += 1) {
    days.push(d);
    d = addDays(d, 1);
  }
  return days;
}

const SELECTED_DAY_SLOTS = [
  {
    start_utc: "2026-08-01T14:00:00+00:00",
    end_utc: "2026-08-01T14:45:00+00:00",
    local_start_time: "10:00 AM",
    local_end_time: "10:45 AM",
    accessible_date_label: "Saturday, August 1",
    accessible_time_label: "10:00 AM to 10:45 AM",
    time_of_day: "morning",
    selectable: true,
  },
  {
    start_utc: "2026-08-01T15:00:00+00:00",
    end_utc: "2026-08-01T15:30:00+00:00",
    local_start_time: "11:00 AM",
    local_end_time: "11:30 AM",
    accessible_date_label: "Saturday, August 1",
    accessible_time_label: "11:00 AM to 11:30 AM",
    time_of_day: "morning",
    selectable: true,
  },
  {
    start_utc: "2026-08-01T17:30:00+00:00",
    end_utc: "2026-08-01T18:15:00+00:00",
    local_start_time: "1:30 PM",
    local_end_time: "2:15 PM",
    accessible_date_label: "Saturday, August 1",
    accessible_time_label: "1:30 PM to 2:15 PM",
    time_of_day: "afternoon",
    selectable: true,
  },
];

function buildPreviewPayload(query, scenario) {
  const startDay = query.get("start_day");
  const endDay = query.get("end_day");
  const selectedDay = query.get("selected_day");
  const days = inclusiveDays(startDay, endDay).map((localDate, i) => {
    let state = STATE_PATTERN[i % STATE_PATTERN.length];
    if (scenario.unknownStateAt && scenario.unknownStateAt.index === i) {
      state = scenario.unknownStateAt.value;
    }
    return {
      local_date: localDate,
      weekday: weekdayName(localDate),
      state,
      selectable: state === "open",
    };
  });
  return {
    client_id: CID,
    practice_name: scenario.practiceName || "Test Dental",
    timezone_name: "America/New_York",
    booking_enabled: scenario.bookingEnabled === true,
    range_start: startDay,
    range_end: endDay,
    generated_at: "2026-07-31T15:00:00+00:00",
    days,
    selected_day: selectedDay || null,
    slots: selectedDay ? JSON.parse(JSON.stringify(SELECTED_DAY_SLOTS)) : [],
  };
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  };
}

// --------------------------------------------------------------------------
// Sandbox
// --------------------------------------------------------------------------
function buildSandbox(scenario) {
  const elementsById = {};
  const body = makeElement("body");

  const INPUT_IDS = ["apiBaseInput", "credentialInput", "clientIdInput",
    "serviceKeyInput", "startDateInput"];
  const BUTTON_IDS = ["clearCredentialBtn", "loadWeekBtn", "openMonthBtn",
    "prevMonthBtn", "nextMonthBtn"];
  const HIDDEN_AT_START = ["errorState", "resultView", "bookingPausedBadge",
    "monthNav", "slotsPanel", "slotSummary"];
  const OTHER_IDS = ["devBanner", "statusLive", "errorState", "devDetails",
    "resultView", "practiceName", "bookingPausedBadge", "rangeLabel",
    "viewModeLabel", "monthNav", "monthNameLabel", "dayGrid",
    "slotsPanel", "slotsHeading", "slotsList", "slotSummary"];

  INPUT_IDS.forEach((id) => {
    const el = makeElement("input"); el.id = id;
    elementsById[id] = el; body.appendChild(el);
  });
  BUTTON_IDS.forEach((id) => {
    const el = makeElement("button"); el.id = id;
    elementsById[id] = el; body.appendChild(el);
  });
  OTHER_IDS.forEach((id) => {
    if (elementsById[id]) return;
    const el = makeElement("div"); el.id = id;
    elementsById[id] = el; body.appendChild(el);
  });
  HIDDEN_AT_START.forEach((id) => elementsById[id].classList.add("hidden"));

  const fetchCalls = [];
  const manualDeferreds = [];
  const abortTracker = { count: 0 };

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

  function FakeAbortController() {
    const self = this;
    this.signal = { aborted: false };
    this.abort = function () {
      self.signal.aborted = true;
      abortTracker.count += 1;
    };
  }

  const sandbox = {
    document: documentStub,
    Date,
    URLSearchParams,
    AbortController: FakeAbortController,
    fetch: (url, opts) => {
      const call = { url: String(url), opts: opts || {} };
      fetchCalls.push(call);
      if (scenario.manual) {
        let resolveFn;
        const promise = new Promise((res) => { resolveFn = res; });
        manualDeferreds.push({ call, resolve: resolveFn });
        return promise;
      }
      if (scenario.networkReject) {
        return Promise.reject(new Error("network unreachable in test"));
      }
      if (scenario.status && scenario.status !== 200) {
        return Promise.resolve(jsonResponse(
          scenario.status, { detail: scenario.detail || "test detail" }));
      }
      const query = new URLSearchParams(call.url.split("?")[1] || "");
      return Promise.resolve(jsonResponse(200, buildPreviewPayload(query, scenario)));
    },
  };
  sandbox.globalThis = sandbox;

  const context = vm.createContext(sandbox);
  const html = fs.readFileSync(PICKER_B_HTML, "utf8");
  const match = html.match(/<script>([\s\S]*)<\/script>/);
  if (!match) throw new Error("No <script> block found in calendar-picker-prototype-b.html");
  const scriptSource = match[1];
  vm.runInContext(scriptSource, context,
    { filename: "calendar-picker-prototype-b.html<script>" });

  return {
    context, elementsById, fetchCalls, manualDeferreds, abortTracker,
    body, html, scriptSource, sandbox,
  };
}

function run(context, code) { return vm.runInContext(code, context); }
function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

function allText(el) {
  return collect(el, []).map((n) => n.textContent).join(" | ");
}

function buttonsWithAttr(container, attrName) {
  return collect(container, []).filter(
    (n) => n.tagName === "BUTTON" && n.getAttribute(attrName) !== null
  );
}

function queryOf(call) {
  return new URLSearchParams(call.url.split("?")[1] || "");
}

function fillSetup(sb, overrides) {
  const values = Object.assign({
    apiBase: "", credential: RAW_KEY, clientId: CID,
    serviceKey: "", startDate: "2026-07-31",
  }, overrides || {});
  sb.elementsById.apiBaseInput.value = values.apiBase;
  sb.elementsById.credentialInput.value = values.credential;
  sb.elementsById.clientIdInput.value = values.clientId;
  sb.elementsById.serviceKeyInput.value = values.serviceKey;
  sb.elementsById.startDateInput.value = values.startDate;
}

async function loadWeek(sb, overrides) {
  fillSetup(sb, overrides);
  sb.elementsById.loadWeekBtn.click();
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
  // ---- 1. Source-level contracts (the page's single inline script) -------
  {
    const sb = buildSandbox({});
    const src = sb.scriptSource;
    const html = sb.html;

    const previewMentions = src.split("/admin/calendar/availability-preview").length - 1;
    const calendarPathMentions = src.split("/admin/calendar/").length - 1;
    ok("the only Calendar endpoint literal is the availability preview path",
       previewMentions >= 1 && previewMentions === calendarPathMentions);
    ok("no patient chat endpoint reference exists",
       src.indexOf("/chat") === -1 && html.indexOf("/chat") === -1);
    ok("no write-capable HTTP method literal exists in the script",
       ["POST", "PUT", "PATCH", "DELETE"].every((m) => src.indexOf(m) === -1));
    ok("the credential header appears exactly once, inside a headers object",
       src.split("X-Admin-Key").length - 1 === 1 &&
       /headers:\s*\{\s*"X-Admin-Key":\s*credential\s*\}/.test(src));
    ok("no browser storage, cookie, or worker API exists in the script",
       ["local" + "Storage", "session" + "Storage", "document." + "cookie",
        "indexed" + "DB", "serviceWorker", "BackgroundSync"].every(
         (token) => src.indexOf(token) === -1));
    ok("script never assigns HTML from strings (no innerHTML)",
       src.indexOf("inner" + "HTML") === -1);
    ok("script writes nothing to the browser log",
       src.indexOf("console" + ".") === -1);
    ok("the retired time-preference request field is never mentioned",
       src.indexOf("time_" + "preference") === -1 &&
       html.indexOf("time_" + "preference") === -1);
    ok("no slot identifier field is mentioned anywhere",
       src.indexOf("slot_" + "id") === -1 && html.indexOf("slot_" + "id") === -1);
    ok("the retired fifth day state is never synthesized by the script",
       src.indexOf("\"closed\"") === -1 && src.indexOf("'closed'") === -1);
    ok("exactly one inline script block and no external script or stylesheet",
       html.split("<script").length - 1 === 1 &&
       html.indexOf("<script src") === -1 &&
       html.indexOf("<link rel=\"stylesheet\"") === -1);
    ok("development-only warning banner is present and never hidden",
       html.indexOf("Development review only. Use a disposable test credential. " +
                    "Never enter a production credential.") !== -1 &&
       /id="devBanner"[^>]*>/.test(html) &&
       !/class="[^"]*hidden[^"]*"[^>]*id="devBanner"|id="devBanner"[^>]*class="[^"]*hidden/.test(html));
    const credentialTag = (html.match(/<input[^>]*id="credentialInput"[^>]*>/) || [""])[0];
    ok("credential input is password-typed with autocomplete off and no default value",
       credentialTag.indexOf('type="password"') !== -1 &&
       credentialTag.indexOf('autocomplete="off"') !== -1 &&
       credentialTag.indexOf("value=") === -1);
  }

  // ---- 2. Inert on load ---------------------------------------------------
  {
    const sb = buildSandbox({});
    ok("no network request happens on page load", sb.fetchCalls.length === 0);
    ok("credential field starts empty",
       sb.elementsById.credentialInput.value === "");
    ok("result view starts hidden",
       sb.elementsById.resultView.classList.contains("hidden"));
  }

  // ---- 3. Seven-day preview: request shape --------------------------------
  {
    const sb = buildSandbox({});
    await loadWeek(sb);
    ok("Load 7-day preview makes exactly one request", sb.fetchCalls.length === 1);
    const call = sb.fetchCalls[0];
    ok("request targets the preview endpoint on the same origin (relative URL)",
       call.url.indexOf("/admin/calendar/availability-preview?") === 0);
    const q = queryOf(call);
    ok("request carries client_id, start_day, and end_day",
       q.get("client_id") === CID && q.get("start_day") === "2026-07-31" &&
       q.get("end_day") === "2026-08-06");
    ok("end_day is start_day plus six local calendar days",
       q.get("end_day") === "2026-08-06");
    ok("initial seven-day request sends no selected_day",
       q.get("selected_day") === null);
    ok("blank service key is omitted entirely", q.get("service_key") === null);
    ok("no retired time-preference parameter is ever sent",
       q.get("time_" + "preference") === null);
    ok("the credential rides only in the X-Admin-Key header",
       call.opts.headers && call.opts.headers["X-Admin-Key"] === RAW_KEY);
    ok("the request method is a read", call.opts.method === "GET");
    ok("the credential never appears in the request URL",
       call.url.indexOf(RAW_KEY) === -1);

    // ---- 4. Seven-day rendering -------------------------------------------
    ok("practice name renders from the server payload",
       sb.elementsById.practiceName.textContent === "Test Dental");
    ok("paused badge is shown when booking_enabled is false (informational)",
       !sb.elementsById.bookingPausedBadge.classList.contains("hidden"));
    const dayButtons = buttonsWithAttr(sb.elementsById.dayGrid, "data-local-date");
    ok("seven day buttons render for the seven-day range", dayButtons.length === 7);
    const gridText = allText(sb.elementsById.dayGrid);
    ok("all four locked states render with non-color text labels",
       gridText.indexOf("Available") !== -1 && gridText.indexOf("Full") !== -1 &&
       gridText.indexOf("Unavailable") !== -1 && gridText.indexOf("Past") !== -1);
    ok("only open days are enabled; every other state is locked",
       dayButtons.every((b) => {
         const isOpen = b.className.indexOf("state-open") !== -1;
         return isOpen ? b.disabled === false : b.disabled === true;
       }));
    ok("no daily slot count is rendered",
       !/\d+\s*(slot|time|opening)/i.test(gridText) &&
       gridText.toLowerCase().indexOf("count") === -1);
    const dev = sb.elementsById.devDetails.textContent;
    ok("developer details show status, range, and client id",
       dev.indexOf("status: 200") !== -1 &&
       dev.indexOf("2026-07-31 → 2026-08-06") !== -1 &&
       dev.indexOf(CID) !== -1);
    ok("developer details exclude the credential", dev.indexOf(RAW_KEY) === -1);

    // ---- 5. Selected day: one request on the same active range ------------
    const openDay = dayButtons.find((b) =>
      b.getAttribute("data-local-date") === "2026-08-01");
    openDay.click();
    await sleep(20);
    ok("selecting an open day makes exactly one additional request",
       sb.fetchCalls.length === 2);
    const dq = queryOf(sb.fetchCalls[1]);
    ok("selected-day request reuses the same active range",
       dq.get("start_day") === "2026-07-31" && dq.get("end_day") === "2026-08-06");
    ok("selected-day request carries selected_day",
       dq.get("selected_day") === "2026-08-01");

    const slotButtons = collect(sb.elementsById.slotsList, [])
      .filter((n) => n.tagName === "BUTTON");
    ok("the selected day's times render from the server slots",
       slotButtons.length === 3 &&
       slotButtons[0].textContent === "10:00 AM – 10:45 AM");
    const slotsText = allText(sb.elementsById.slotsList);
    ok("times are grouped under the server's morning/afternoon vocabulary",
       slotsText.indexOf("Morning") !== -1 && slotsText.indexOf("Afternoon") !== -1);
    ok("selected day is visibly marked on the grid",
       buttonsWithAttr(sb.elementsById.dayGrid, "data-local-date").some(
         (b) => b.getAttribute("data-local-date") === "2026-08-01" &&
                b.getAttribute("aria-pressed") === "true"));

    // ---- 6. Slot click: zero network, read-only summary -------------------
    const callsBeforeSlotClick = sb.fetchCalls.length;
    slotButtons[0].click();
    await sleep(10);
    ok("clicking a time makes zero network requests",
       sb.fetchCalls.length === callsBeforeSlotClick);
    const summaryText = allText(sb.elementsById.slotSummary);
    ok("read-only summary shows the server's accessible labels",
       summaryText.indexOf("Saturday, August 1") !== -1 &&
       summaryText.indexOf("10:00 AM to 10:45 AM") !== -1);
    ok("summary displays the locked preview-only statement",
       summaryText.indexOf(
         "Preview only — no appointment has been held or booked.") !== -1);
    ok("clicked time is marked pressed",
       slotButtons[0].getAttribute("aria-pressed") === "true");
    ok("no slot identifier is rendered anywhere",
       allText(sb.body).indexOf("slot_" + "id") === -1);

    // ---- 7. Month view: lazy, one range request, at most 31 days ----------
    ok("no month request happens before the month view is opened",
       sb.fetchCalls.length === 2);
    sb.elementsById.openMonthBtn.click();
    await sleep(20);
    ok("opening the month view makes exactly one request",
       sb.fetchCalls.length === 3);
    const mq = queryOf(sb.fetchCalls[2]);
    ok("month request covers the anchor calendar month",
       mq.get("start_day") === "2026-07-01" && mq.get("end_day") === "2026-07-31");
    const monthSpan =
      (Date.UTC(2026, 6, 31) - Date.UTC(2026, 6, 1)) / 86400000 + 1;
    ok("month inclusive span is no more than 31 days",
       monthSpan <= 31 && monthSpan === 31);
    const monthButtons = buttonsWithAttr(sb.elementsById.dayGrid, "data-local-date");
    ok("month grid renders one button per day of the month",
       monthButtons.length === 31);
    ok("month name label shows the loaded month",
       sb.elementsById.monthNameLabel.textContent === "July 2026");

    sb.elementsById.nextMonthBtn.click();
    await sleep(20);
    ok("next month is one additional single range request",
       sb.fetchCalls.length === 4);
    const nq = queryOf(sb.fetchCalls[3]);
    ok("next-month request covers the following calendar month",
       nq.get("start_day") === "2026-08-01" && nq.get("end_day") === "2026-08-31");
  }

  // ---- 8. Service key: omitted when blank, trimmed, case preserved --------
  {
    const sb = buildSandbox({});
    await loadWeek(sb, { serviceKey: "   " });
    ok("whitespace-only service key is omitted entirely",
       queryOf(sb.fetchCalls[0]).get("service_key") === null);

    const sb2 = buildSandbox({});
    await loadWeek(sb2, { serviceKey: "  Cleaning_Checkup  " });
    ok("supplied service key is trimmed with its case preserved",
       queryOf(sb2.fetchCalls[0]).get("service_key") === "Cleaning_Checkup");
  }

  // ---- 9. Unknown day state is a visible review error ---------------------
  {
    const sb = buildSandbox({ unknownStateAt: { index: 2, value: "closed" } });
    await loadWeek(sb);
    const errorEl = sb.elementsById.errorState;
    ok("an unknown day state produces a visible review error",
       !errorEl.classList.contains("hidden") &&
       errorEl.textContent.indexOf("unknown day state \"closed\"") !== -1);
    ok("an unknown state renders no day grid (never treated as available)",
       buttonsWithAttr(sb.elementsById.dayGrid, "data-local-date").length === 0);
  }

  // ---- 10. Error mapping: five distinguishable outcomes -------------------
  {
    const outcomes = [];
    async function errorTextFor(scenario) {
      const sb = buildSandbox(scenario);
      await loadWeek(sb);
      const text = sb.elementsById.errorState.textContent;
      ok("failure outcome is visible (" + text + ")",
         !sb.elementsById.errorState.classList.contains("hidden") && text.length > 0);
      ok("error UI and developer details exclude the credential",
         text.indexOf(RAW_KEY) === -1 &&
         sb.elementsById.devDetails.textContent.indexOf(RAW_KEY) === -1 &&
         allText(sb.body).indexOf(RAW_KEY) === -1);
      outcomes.push(text);
      return text;
    }
    ok("401 maps to the credential-rejected wording",
       (await errorTextFor({ status: 401, detail: "Invalid admin key." }))
         === "Credential rejected.");
    ok("404 maps to the client-not-found wording",
       (await errorTextFor({ status: 404, detail: "Client not found." }))
         === "Client not found.");
    ok("422 displays the stable server-provided detail verbatim",
       (await errorTextFor({ status: 422,
                             detail: "service_key is not available for preview" }))
         === "service_key is not available for preview");
    ok("network failure maps to the could-not-reach wording",
       (await errorTextFor({ networkReject: true }))
         === "Could not reach preview endpoint.");
    ok("5xx maps to the preview-service-error wording",
       (await errorTextFor({ status: 500, detail: "boom" }))
         === "Preview service error.");
    ok("all five failure classes are pairwise distinguishable",
       new Set(outcomes).size === 5);
  }

  // ---- 11. Stale-response protection --------------------------------------
  {
    const sb = buildSandbox({ manual: true });
    fillSetup(sb);
    sb.elementsById.loadWeekBtn.click();          // request A (older)
    await sleep(5);
    run(sb.context, "loadWeekPreview();");        // request B (newer)
    await sleep(5);
    ok("two in-flight requests were issued for the race",
       sb.fetchCalls.length === 2 && sb.manualDeferreds.length === 2);
    ok("starting the newer request aborts the older one",
       sb.abortTracker.count >= 1);

    const newerQ = queryOf(sb.manualDeferreds[1].call);
    const olderQ = queryOf(sb.manualDeferreds[0].call);
    // Resolve NEWER first, then let the OLDER (stale) response arrive late.
    sb.manualDeferreds[1].resolve(jsonResponse(200,
      Object.assign(buildPreviewPayload(newerQ, {}), { practice_name: "NEWER" })));
    await sleep(20);
    sb.manualDeferreds[0].resolve(jsonResponse(200,
      Object.assign(buildPreviewPayload(olderQ, {}), { practice_name: "OLDER" })));
    await sleep(20);
    ok("a stale response never replaces the newer result",
       sb.elementsById.practiceName.textContent === "NEWER");
    ok("loading state is released after the newer request settles",
       sb.elementsById.loadWeekBtn.disabled === false);
  }

  // ---- 12. Credential lifetime --------------------------------------------
  {
    const sb = buildSandbox({});
    sb.elementsById.credentialInput.value = RAW_KEY;
    sb.elementsById.clearCredentialBtn.click();
    ok("Clear credential empties the in-memory value",
       sb.elementsById.credentialInput.value === "");
    // A brand-new sandbox stands in for a page reload: with no storage API
    // in the source (proven above), nothing exists to restore a key from.
    const reloaded = buildSandbox({});
    ok("a reloaded page starts with no credential (nothing to restore from)",
       reloaded.elementsById.credentialInput.value === "");
  }

  // ---- 13. Frozen Prototype A and B3 scope guard --------------------------
  {
    const prototypeA = fs.readFileSync(PROTOTYPE_A_HTML);
    const digest = crypto.createHash("sha256").update(prototypeA).digest("hex");
    ok("Prototype A remains byte-for-byte unchanged at its frozen SHA-256",
       digest === PROTOTYPE_A_SHA256);
    const pageName = path.basename(PICKER_B_HTML);
    ok("B3 page is a NEW file distinct from every pre-existing admin page",
       pageName === "calendar-picker-prototype-b.html" &&
       ["calendar-picker-prototype.html", "calendar-portal.html",
        "dashboard.html", "demo-requests.html", "faqs.html"].indexOf(pageName) === -1);
  }

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((err) => {
  console.log("HARNESS FAILURE: " + (err && err.stack ? err.stack : err));
  process.exit(1);
});
