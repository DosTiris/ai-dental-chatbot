// tests/test_widget_confirm_decline_reentry.js
//
// CONFIRMATION-DECLINE RE-ENTRY regression (owner-reported production defect
// at dbc1d03): after capture-first intake -> date -> exact time ->
// confirmation, clicking "No - pick another time" re-signals the date stage
// INSIDE the decline action's own success render, while
// structuredActionInFlight is still true. fetchPickerMonth's synchronous
// entry guard refused that signal-driven initial load, so the rebuilt picker
// deadlocked as a permanently empty shell: "Next 7 days" label + "See full
// calendar" toggle with NO chips, NO availability-preview fetch, and an
// EMPTY hidden grid (revealing it showed nothing).
//
// The fix marks renderDatePicker's ONE signal-driven initial load with
// fromSignal === true at the entry guard only. Month navigation, Retry, and
// clamp-jump callers keep the full guard; the pickerSubmitted freeze and all
// post-await stale/supersession checks apply to signal loads unchanged.
//
// Executes the REAL inline script from static/chat.html in a Node `vm`
// sandbox (same technique as tests/test_widget_date_strip.js).
//
// Run:
//   node tests/test_widget_confirm_decline_reentry.js
// or:
//   MIA_CHAT_HTML=/path/to/static/chat.html node tests/test_widget_confirm_decline_reentry.js

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CHAT_HTML = process.env.MIA_CHAT_HTML ||
  path.join(__dirname, "..", "static", "chat.html");

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
  "Friday", "Saturday"];

// --------------------------------------------------------------------------
// Mock DOM element (faithful to tests/test_widget_date_strip.js).
// --------------------------------------------------------------------------
function makeClassList() {
  const values = new Set();
  return {
    add: (...items) => items.forEach((i) => values.add(i)),
    remove: (...items) => items.forEach((i) => values.delete(i)),
    toggle: (item, force) => {
      const wanted = force === undefined ? !values.has(item) : !!force;
      if (wanted) values.add(item); else values.delete(item);
      return wanted;
    },
    contains: (item) => values.has(item),
  };
}

function makeElement(tag) {
  const element = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    parent: null,
    listeners: {},
    attributes: {},
    style: { setProperty: () => {} },
    textContent: "",
    value: "",
    placeholder: "",
    disabled: false,
    hidden: false,
    scrollTop: 0,
    scrollHeight: 0,
    scrollLeft: 0,
    clientWidth: 0,
    scrollWidth: 0,
    id: "",
    type: "",
    className: "",
  };
  let innerHTMLValue = "";
  Object.defineProperty(element, "innerHTML", {
    get: () => innerHTMLValue,
    set: (value) => {
      innerHTMLValue = String(value);
      if (innerHTMLValue === "") {
        element.children.forEach((c) => { c.parent = null; });
        element.children = [];
      }
    },
  });
  Object.defineProperty(element, "parentElement", { get: () => element.parent });
  element.classList = makeClassList();
  element.appendChild = (child) => {
    child.parent = element; element.children.push(child); return child;
  };
  element.remove = () => {
    if (!element.parent) return;
    element.parent.children =
      element.parent.children.filter((c) => c !== element);
    element.parent = null;
  };
  element.addEventListener = (name, handler) => {
    (element.listeners[name] = element.listeners[name] || []).push(handler);
  };
  element.click = () => (element.listeners.click || []).forEach((h) => h());
  element.setAttribute = (name, value) => { element.attributes[name] = value; };
  element.focus = () => {};
  element.querySelectorAll = () => [];
  element.scrollIntoViewCalls = [];
  element.scrollIntoView = (opts) => element.scrollIntoViewCalls.push(opts || null);
  Object.defineProperty(element, "isConnected", {
    get: () => {
      let node = element;
      while (node.parent) node = node.parent;
      return node.tagName === "BODY";
    },
  });
  return element;
}

function collect(element, out) {
  out.push(element);
  element.children.forEach((c) => collect(c, out));
  return out;
}

function successfulJson(payload) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
}

function isoAdd(iso, days) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}
// Pinned "today": Monday, so the owner-reported Friday sits inside next-7.
const TODAY = "2026-08-10";
function weekdayOf(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
}
function makeFakeDate(iso) {
  const RealDate = Date;
  const [Y, M, D] = iso.split("-").map(Number);
  return class extends RealDate {
    constructor(...args) {
      if (args.length === 0) super(Y, M - 1, D);
      else super(...args);
    }
    static now() { return new RealDate(Y, M - 1, D).getTime(); }
  };
}

// Public-preview payload for the requested window (C2-A.1 shape).
function previewPayloadFor(url, options = {}) {
  const parsed = new URL(String(url));
  const start = parsed.searchParams.get("start_day");
  const end = parsed.searchParams.get("end_day");
  const openDays = options.openDays || [];
  const days = [];
  let cursor = start;
  while (cursor <= end) {
    days.push({ local_date: cursor, weekday: weekdayOf(cursor),
      state: openDays.indexOf(cursor) !== -1 ? "open" : "unavailable" });
    cursor = isoAdd(cursor, 1);
  }
  return {
    timezone: "America/New_York",
    requested_start_day: start,
    requested_end_day: end,
    earliest_bookable_day: options.earliest || TODAY,
    latest_bookable_day: options.latest || isoAdd(TODAY, 40),
    days,
  };
}

function buildSandbox(options = {}) {
  const elementsById = {};
  const body = makeElement("body");
  ["messages", "input", "sendBtn", "miaHeaderTitle", "miaHeaderSubtitle",
    "main-menu", "service-menu", "consentModal", "agreeBtn"].forEach((id) => {
    const el = makeElement("div"); el.id = id; elementsById[id] = el;
    body.appendChild(el);
  });
  const inputRow = makeElement("div");
  inputRow.className = "chat-input-row";
  body.appendChild(inputRow);

  const styleMap = {};
  const rootStyle = {
    setProperty: (k, v) => { styleMap[k] = v; },
    getPropertyValue: (k) => (k in styleMap ? styleMap[k] : ""),
    removeProperty: (k) => { delete styleMap[k]; },
  };

  const documentStub = {
    documentElement: { style: rootStyle },
    body,
    getElementById: (id) => {
      if (!elementsById[id]) {
        const el = makeElement("div"); el.id = id; elementsById[id] = el;
        body.appendChild(el);
      }
      return elementsById[id];
    },
    createElement: (tag) => makeElement(tag),
    querySelector: (sel) => (sel === ".chat-input-row" ? inputRow : makeElement("div")),
    querySelectorAll: (sel) => {
      const all = collect(body, []);
      const classes = sel.split(",").map((s) => s.trim().replace(/^\./, ""));
      return all.filter((el) =>
        classes.some((name) =>
          el.classList.contains(name) ||
          String(el.className).split(/\s+/).includes(name)));
    },
  };

  const fetchCalls = [];
  const rafQueue = [];
  let chatResponder =
    () => successfulJson({ reply: "Okay.", conversation_id: "conv-1", meta: {} });
  let previewResponder =
    (url) => successfulJson(previewPayloadFor(url, options.preview || {}));

  const sandbox = {
    console,
    requestAnimationFrame: (cb) => { rafQueue.push(cb); return rafQueue.length; },
    getComputedStyle: (el) => el.style,
    document: documentStub,
    window: {
      location: { search: "?client_key=test-client", hostname: "localhost",
        origin: "http://localhost" },
      matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    },
    URLSearchParams, URL, Date: makeFakeDate(TODAY),
    setTimeout, clearTimeout,
    localStorage: {
      getItem: () => null, setItem: () => {}, removeItem: () => {},
    },
    fetch: (url, requestOptions) => {
      fetchCalls.push({ url, options: requestOptions || {} });
      if (String(url).includes("/chat/config")) return successfulJson({});
      if (String(url).includes("/chat/calendar/availability-preview"))
        return previewResponder(String(url));
      return chatResponder(url, requestOptions || {});
    },
  };
  sandbox.window.document = documentStub;
  sandbox.window.localStorage = sandbox.localStorage;
  sandbox.globalThis = sandbox;

  const html = fs.readFileSync(CHAT_HTML, "utf8");
  const match = html.match(/<script>([\s\S]*)<\/script>/);
  if (!match) throw new Error("No inline script found in static/chat.html");
  const context = vm.createContext(sandbox);
  vm.runInContext(match[1], context, { filename: "static/chat.html<script>" });

  return {
    context, body, elementsById, fetchCalls,
    setChatResponder: (r) => { chatResponder = r; },
  };
}

function run(context, source) { return vm.runInContext(source, context); }
function flush() { return new Promise((r) => setTimeout(r, 0)); }

// --------------------------------------------------------------------------
// Locators.
// --------------------------------------------------------------------------
function hasClass(el, name) {
  return el.classList.contains(name) ||
    String(el.className).split(/\s+/).includes(name);
}
function rowsOf(sb, cls) {
  return sb.elementsById.messages.children.filter((c) => hasClass(c, cls));
}
function lastPickerRow(sb) {
  const r = rowsOf(sb, "date-picker-row");
  return r[r.length - 1] || null;
}
function findByClass(root, name) {
  return collect(root, []).find((el) => hasClass(el, name)) || null;
}
function findAllByClass(root, name) {
  return collect(root, []).filter((el) => hasClass(el, name));
}
function stripOf(sb) { const r = lastPickerRow(sb); return r ? findByClass(r, "dp-strip") : null; }
function moreOf(sb) { const r = lastPickerRow(sb); return r ? findByClass(r, "dp-more") : null; }
function gridOf(sb) { const r = lastPickerRow(sb); return r ? findByClass(r, "date-picker") : null; }
function stripChips(sb) {
  const strip = stripOf(sb);
  return strip ? strip.children.filter((c) => hasClass(c, "dp-chip")) : [];
}
function gridDays(sb) {
  const grid = gridOf(sb);
  return grid ? collect(grid, []).filter((c) => hasClass(c, "dp-day")) : [];
}
function chatPosts(sb) {
  return sb.fetchCalls.filter((c) =>
    String(c.url).endsWith("/chat") && c.options && c.options.method === "POST");
}
function previewFetches(sb) {
  return sb.fetchCalls.filter((c) =>
    String(c.url).includes("/chat/calendar/availability-preview"));
}
function chipByLabel(sb, monDay) {
  return stripChips(sb).find((c) => {
    const num = findByClass(c, "dp-chip-num");
    return num && num.textContent === monDay;
  }) || null;
}

// --------------------------------------------------------------------------
let passed = 0, failed = 0;
function ok(name, condition) {
  if (condition) { passed += 1; console.log("ok - " + name); }
  else { failed += 1; console.log("NOT OK - " + name); }
}

const OPEN_DAYS = ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"];
const SLOT_ACTIONS_FIRST = [
  { label: "10:00 AM", message: "10:00 AM",
    action: { type: "calendar_choice",
      choice_id: "11111111-1111-4111-8111-111111111111" } },
  { label: "1:30 PM", message: "1:30 PM",
    action: { type: "calendar_choice",
      choice_id: "22222222-2222-4222-8222-222222222222" } },
  { label: "3:00 PM", message: "3:00 PM",
    action: { type: "calendar_choice",
      choice_id: "33333333-3333-4333-8333-333333333333" } },
];
const SLOT_ACTIONS_ALTERNATE = [
  { label: "11:15 AM", message: "11:15 AM",
    action: { type: "calendar_choice",
      choice_id: "44444444-4444-4444-8444-444444444444" } },
];
const CONFIRM_ACTIONS = [
  { label: "Yes, book it", message: "Yes",
    action: { type: "calendar_choice", choice_id: "confirm-yes" } },
  { label: "No \u2014 pick another time", message: "No \u2014 pick another time",
    action: { type: "calendar_choice", choice_id: "confirm-no" } },
];

async function driveToConfirmation(sb) {
  const say = (reply, meta) => sb.setChatResponder(() =>
    successfulJson(Object.assign({ reply, conversation_id: "conv-1" }, meta)));

  // Capture-first intake reaches the date stage on an ORDINARY message.
  say("What day would work best?", { meta: {
    calendar_picker: { stage: "date", submit: "message" } } });
  run(sb.context, 'inputEl.value = "Kev";');
  await run(sb.context, "sendMessage()");
  await flush(); await flush();

  // Friday from the strip (capture-first ordinary message) -> time stage.
  say("Morning or afternoon?", { meta: {
    calendar_picker: { stage: "time_preference" } } });
  chipByLabel(sb, "AUG 14").click();
  await flush(); await flush();

  // Morning -> exact times.
  say("Here are the times.", { meta: {
    calendar_picker: { stage: "slot_selection" },
    calendar_actions: SLOT_ACTIONS_FIRST } });
  findAllByClass(rowsOf(sb, "time-pref-row")[0], "time-pref-btn")[0].click();
  await flush(); await flush();

  // 10:00 AM (structured action) -> confirmation chips.
  say("To confirm: Kev on Friday, August 14 at 10:00 AM. Is that correct?",
    { meta: { calendar_actions: CONFIRM_ACTIONS } });
  findAllByClass(rowsOf(sb, "slot-panel-row")[0], "slot-chip")[0].click();
  await flush(); await flush();
  return say;
}

function declineButton(sb) {
  const qr = rowsOf(sb, "quick-replies")[0];
  return collect(qr, []).find((el) =>
    el.tagName === "BUTTON" && /pick another time/.test(el.textContent)) || null;
}

async function main() {
  // ------------------------------------------------------------------
  // 1. Owner-reported sequence: decline must cleanly re-enter the date
  //    stage with a WORKING strip and a WORKING full calendar.
  // ------------------------------------------------------------------
  {
    const sb = buildSandbox({ preview: { openDays: OPEN_DAYS } });
    const say = await driveToConfirmation(sb);

    ok("initial picker rendered 7 strip chips from one preview fetch",
      previewFetches(sb).length === 1);

    const postsBefore = chatPosts(sb).length;
    const previewsBefore = previewFetches(sb).length;

    say("No problem \u2014 what day would work better?", { meta: {
      calendar_picker: { stage: "date", submit: "message" } } });
    declineButton(sb).click();
    await flush(); await flush(); await flush();

    const declineBody = JSON.parse(
      chatPosts(sb)[chatPosts(sb).length - 1].options.body);
    ok("decline sent EXACTLY one /chat POST carrying the decline choice " +
       "(no booking-side traffic from navigating backward)",
      chatPosts(sb).length === postsBefore + 1 &&
      declineBody.action &&
      declineBody.action.type === "calendar_choice" &&
      declineBody.action.choice_id === "confirm-no");

    const row = lastPickerRow(sb);
    const label = row ? findByClass(row, "dp-strip-label") : null;
    ok("re-entry rebuilt ONE fresh picker row with the Next 7 days shell",
      rowsOf(sb, "date-picker-row").length === 1 &&
      !!label && label.textContent === "Next 7 days" && !!moreOf(sb));

    ok("re-entry fetched the availability preview exactly once " +
       "(the defect fetched ZERO times)",
      previewFetches(sb).length === previewsBefore + 1);

    ok("NEXT 7 DAYS strip rendered 7 date chips again",
      stripChips(sb).length === 7);

    const altChip = chipByLabel(sb, "AUG 12");
    ok("an alternate open date chip is present, enabled, and clickable",
      !!altChip && hasClass(altChip, "dp-open") && altChip.disabled !== true &&
      (altChip.listeners.click || []).length === 1);
    if (!altChip) {
      // Defect state: the strip never rendered, so the rest of the flow
      // cannot be exercised. Fail the remaining flow checks explicitly and
      // finish cleanly instead of throwing.
      failed += 1;
      console.log("NOT OK - (flow aborted: no alternate chip rendered to continue the re-entry flow)");
      console.log(`\n${passed} passed, ${failed} failed`);
      process.exit(1);
    }

    const postsAfterReentry = chatPosts(sb).length;
    moreOf(sb).click();
    ok("See full calendar OPENS and the grid actually rendered day cells",
      gridOf(sb).hidden === false &&
      moreOf(sb).attributes["aria-expanded"] === "true" &&
      gridDays(sb).length > 0);
    ok("re-entry rendering and the full-calendar reveal made no extra " +
       "/chat POSTs and no extra preview fetches",
      chatPosts(sb).length === postsAfterReentry &&
      previewFetches(sb).length === previewsBefore + 1);

    // Alternate date, then another exact time.
    say("Morning or afternoon?", { meta: {
      calendar_picker: { stage: "time_preference" } } });
    altChip.click();
    await flush(); await flush();
    ok("alternate date submits through the SAME capture-first path " +
       "(one more ordinary /chat POST, no action field)",
      chatPosts(sb).length === postsAfterReentry + 1 &&
      JSON.parse(chatPosts(sb)[chatPosts(sb).length - 1].options.body)
        .action === undefined);

    say("Here are the times.", { meta: {
      calendar_picker: { stage: "slot_selection" },
      calendar_actions: SLOT_ACTIONS_ALTERNATE } });
    findAllByClass(rowsOf(sb, "time-pref-row")[0], "time-pref-btn")[0].click();
    await flush(); await flush();
    const altSlot = findAllByClass(rowsOf(sb, "slot-panel-row")[0], "slot-chip")[0];
    ok("another exact time is offered and selectable after re-entry",
      !!altSlot && altSlot.textContent === "11:15 AM" &&
      (altSlot.listeners.click || []).length === 1);
  }

  // ------------------------------------------------------------------
  // 2. Guard preservation on the REBUILT picker: the in-flight lock
  //    still gates user-driven submission and month navigation.
  // ------------------------------------------------------------------
  {
    const sb = buildSandbox({ preview: { openDays: OPEN_DAYS } });
    const say = await driveToConfirmation(sb);
    say("No problem \u2014 what day would work better?", { meta: {
      calendar_picker: { stage: "date", submit: "message" } } });
    declineButton(sb).click();
    await flush(); await flush(); await flush();

    const posts = chatPosts(sb).length;
    const previews = previewFetches(sb).length;
    run(sb.context, "structuredActionInFlight = true;");
    chipByLabel(sb, "AUG 12").click();
    const navs = findAllByClass(gridOf(sb), "dp-nav");
    if (navs.length) navs[navs.length - 1].click();
    await flush();
    ok("with a NEW structured action in flight, chip submission AND month " +
       "navigation on the rebuilt picker still refuse (protections intact)",
      chatPosts(sb).length === posts && previewFetches(sb).length === previews);
    run(sb.context, "structuredActionInFlight = false;");
  }

  // ------------------------------------------------------------------
  // 3. Static pins (EOL-normalized): the exception is EXACTLY the one
  //    signal-driven call; every other fetchPickerMonth caller keeps the
  //    full guard.
  // ------------------------------------------------------------------
  {
    const html = fs.readFileSync(CHAT_HTML, "utf8").replace(/\r\n/g, "\n");
    ok("static: entry guard blocks in-flight starts EXCEPT the marked " +
       "signal-driven initial load",
      html.includes(
        "if (pickerSubmitted || (structuredActionInFlight && fromSignal !== true)) return;"));
    ok("static: exactly ONE call site passes fromSignal=true (renderDatePicker's " +
       "initial load); navigation/Retry/clamp callers stay fully guarded",
      (html.match(/PICKER_PATIENT_JUMP_BUDGET, true\)/g) || []).length === 1 &&
      (html.match(/PICKER_NO_JUMP_BUDGET, true/g) || []).length === 0 &&
      (html.match(/fetchPickerMonth\(/g) || []).length >= 6);
    ok("static: the pickerSubmitted freeze still applies unconditionally " +
       "at the entry guard",
      /if \(pickerSubmitted \|\| \(structuredActionInFlight/.test(html));
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
}

main();
