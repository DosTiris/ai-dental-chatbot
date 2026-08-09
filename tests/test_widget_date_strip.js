// tests/test_widget_date_strip.js
//
// G1 — patient-widget "Next 7 days" DATE STRIP (approved Prototype A v2.2.1
// hierarchy) and G3 — selected-control foreground contrast, frontend proofs.
//
// Executes the REAL inline script from static/chat.html in a Node `vm`
// sandbox (same technique as tests/test_widget_date_picker.js). The strip is
// DERIVED from the SAME availability-preview payload the month grid fetches on
// signal; the month grid is the SECONDARY "See full calendar" view. Selection
// flows through the EXISTING pickDate / calendar_choice path.
//
// Real pixel geometry (mobile width, light-primary rendering) is proven by the
// delivery package's Chromium screenshots, NOT by this repository suite; these
// mock assertions prove the call/structure/derivation contracts.
//
// Run:
//   node tests/test_widget_date_strip.js
// or:
//   MIA_CHAT_HTML=/path/to/static/chat.html node tests/test_widget_date_strip.js

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CHAT_HTML = process.env.MIA_CHAT_HTML ||
  path.join(__dirname, "..", "static", "chat.html");

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
  "Friday", "Saturday"];

// --------------------------------------------------------------------------
// Mock DOM element (faithful to tests/test_widget_date_picker.js).
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
  element.dispatch = (name) =>
    (element.listeners[name] || []).forEach((h) => h());
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
function todayIso() { return new Date().toISOString().slice(0, 10); }
const STRIP_TODAY = "2026-08-04";        // pinned "today" for deterministic tests
function todayLocalIso() { return STRIP_TODAY; }
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
function weekdayOf(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
}

// Public-preview payload for the requested window (matches the C2-A.1 shape).
function previewPayloadFor(url, options = {}) {
  const parsed = new URL(String(url));
  const start = parsed.searchParams.get("start_day");
  const end = parsed.searchParams.get("end_day");
  const openDays = options.openDays || [];
  const fullDays = options.fullDays || [];
  const earliest = options.earliest || isoAdd(todayLocalIso(), 0);
  const latest = options.latest || isoAdd(todayLocalIso(), 40);
  const days = [];
  let cursor = start;
  while (cursor <= end) {
    let state = "unavailable";
    if (openDays.indexOf(cursor) !== -1) state = "open";
    else if (fullDays.indexOf(cursor) !== -1) state = "full";
    days.push({ local_date: cursor, weekday: weekdayOf(cursor), state });
    cursor = isoAdd(cursor, 1);
  }
  if (options.mutateDays) options.mutateDays(days);
  const payload = {
    timezone: "America/New_York",
    requested_start_day: start,
    requested_end_day: end,
    earliest_bookable_day: earliest,
    latest_bookable_day: latest,
    days,
  };
  if (options.mutatePayload) options.mutatePayload(payload);
  return payload;
}

// --------------------------------------------------------------------------
// Sandbox: map-backed documentElement.style + getComputedStyle so G3's
// applyOnPrimaryContrast can be observed.
// --------------------------------------------------------------------------
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
  let chatResponder = options.chatResponder ||
    (() => successfulJson({ reply: "Okay.", conversation_id: "conv-1", meta: {} }));
  let previewResponder = options.previewResponder ||
    ((url) => successfulJson(previewPayloadFor(url, options.preview || {})));
  let configResponder = options.configResponder || (() => successfulJson({}));

  const sandbox = {
    console,
    requestAnimationFrame: (cb) => { rafQueue.push(cb); return rafQueue.length; },
    getComputedStyle: (el) => el.style,
    document: documentStub,
    window: {
      location: { search: "?client_key=test-client", hostname: "localhost",
        origin: "http://localhost" },
      // UX-B: options.mobile models the phone-widget environment by making
      // ONLY the "(max-width: 640px)" query match; every other query (e.g.
      // prefers-reduced-motion) keeps the existing default, so all prior
      // sandboxes remain byte-for-byte behaviorally identical.
      matchMedia: (q) => ({
        matches: !!options.mobile && String(q).includes("max-width: 640px"),
        addEventListener: () => {},
      }),
    },
    URLSearchParams, URL, Date: makeFakeDate(options.nowIso || STRIP_TODAY),
    setTimeout, clearTimeout,
    localStorage: {
      getItem: () => null, setItem: () => {}, removeItem: () => {},
    },
    fetch: (url, requestOptions) => {
      fetchCalls.push({ url, options: requestOptions || {} });
      if (String(url).includes("/chat/config")) return configResponder(String(url));
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
    context, body, elementsById, fetchCalls, html, styleMap, rafQueue,
    flushRaf: () => rafQueue.splice(0, rafQueue.length).forEach((cb) => cb()),
    setChatResponder: (r) => { chatResponder = r; },
    setPreviewResponder: (r) => { previewResponder = r; },
    setConfigResponder: (r) => { configResponder = r; },
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
function pickerRows(sb) {
  return sb.elementsById.messages.children.filter((c) =>
    hasClass(c, "date-picker-row"));
}
function lastRow(sb) { const r = pickerRows(sb); return r[r.length - 1] || null; }
function findByClass(root, name) {
  return collect(root, []).find((el) => hasClass(el, name)) || null;
}
function stripOf(sb) { const r = lastRow(sb); return r ? findByClass(r, "dp-strip") : null; }
function fadeOf(sb) { const r = lastRow(sb); return r ? findByClass(r, "dp-strip-fade") : null; }
function moreOf(sb) { const r = lastRow(sb); return r ? findByClass(r, "dp-more") : null; }
function gridOf(sb) { const r = lastRow(sb); return r ? findByClass(r, "date-picker") : null; }
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
function lastActionBody(sb) {
  const posts = chatPosts(sb);
  if (!posts.length) return null;
  try { return JSON.parse(posts[posts.length - 1].options.body); } catch (e) { return null; }
}

// Drive the date signal; the grid fetches on signal and the strip derives.
async function openStrip(sb, previewOpts) {
  if (previewOpts) sb.setPreviewResponder((url) =>
    successfulJson(previewPayloadFor(url, previewOpts)));
  sb.setChatResponder(() => successfulJson({
    reply: "What day would work best for your appointment?",
    conversation_id: "conv-1",
    meta: { mode: "booking", state: "waiting_for_date",
      calendar_picker: { stage: "date" } },
  }));
  run(sb.context, 'inputEl.value = "I need an appointment";');
  await run(sb.context, "sendMessage()");
  await flush(); await flush();
}

// --------------------------------------------------------------------------
let passed = 0, failed = 0;
function ok(name, condition) {
  if (condition) { passed += 1; console.log("ok - " + name); }
  else { failed += 1; console.log("NOT OK - " + name); }
}

// Days from today through the end of today's month (matches the strip's window).
function expectedStripCount() { return 7; }   // G1: always a true next-7-days
function nextLocalIso(days) { return isoAdd(todayLocalIso(), days); }

async function main() {
  const prefix = run(buildSandbox().context, "DATE_SELECT_CHOICE_PREFIX");
  ok("date-choice prefix is the existing 'pick-date:' contract", prefix === "pick-date:");

  const expected = expectedStripCount();
  // Open days that fall inside the strip window (today .. today+expected-1).
  const openInWindow = [];
  for (let i = 0; i < expected; i += 1) openInWindow.push(nextLocalIso(i));

  // 1. Strip renders the next up-to-7 days from the SAME single payload.
  {
    const sb = buildSandbox();
    await openStrip(sb, { openDays: openInWindow });
    ok("strip renders the next up-to-7 days (derived from the month payload)",
      stripChips(sb).length === expected && expected >= 1);
    ok("mid-month strip uses one availability-preview request",
      previewFetches(sb).length === 1);
    ok("the single preview fetch is the public availability-preview route",
      previewFetches(sb).every((c) =>
        String(c.url).includes("/chat/calendar/availability-preview") &&
        String(c.url).includes("client_key=test-client") &&
        !c.options.headers));
    // The chips carry the actual next-N ISO dates from today.
    const labels = stripChips(sb).map((c) =>
      String((c.attributes["aria-label"] || "")));
    ok("chips are dated from today forward in order",
      labels.every((lbl, i) => lbl.includes(nextLocalIso(i))));
  }

  // 2. Available / Full / Unavailable state words (never color-only).
  {
    const sb = buildSandbox();
    const openIso = nextLocalIso(0);
    const fullIso = expected > 1 ? nextLocalIso(1) : null;
    await openStrip(sb, { openDays: [openIso], fullDays: fullIso ? [fullIso] : [] });
    const chips = stripChips(sb);
    const stateText = (chip) => {
      const st = chip.children.find((c) => hasClass(c, "dp-chip-state"));
      return st ? st.textContent : "";
    };
    const openChip = chips.find((c) => hasClass(c, "dp-open"));
    ok("an open day shows 'Available', is enabled, and has a click handler",
      openChip && stateText(openChip) === "Available" &&
      openChip.disabled === false &&
      (openChip.listeners.click || []).length === 1);
    if (fullIso) {
      const fullChip = chips[1];
      ok("a full day shows 'Full', is locked (disabled + aria-disabled)",
        stateText(fullChip) === "Full" && fullChip.disabled === true &&
        fullChip.attributes["aria-disabled"] === "true" &&
        (fullChip.listeners.click || []).length === 0);
    } else {
      ok("a full day shows 'Full', is locked (skipped: month boundary)", true);
    }
    const lockedNonFull = chips.find((c) =>
      hasClass(c, "dp-locked") && stateText(c) === "Unavailable");
    ok("an unavailable day shows 'Unavailable' and is locked",
      !!lockedNonFull && lockedNonFull.disabled === true &&
      (lockedNonFull.listeners.click || []).length === 0 || expected === 1);
  }

  // 3. Selection uses the EXISTING structured calendar_choice handoff.
  {
    const sb = buildSandbox();
    const openIso = nextLocalIso(0);
    await openStrip(sb, { openDays: [openIso] });
    sb.setChatResponder(() => successfulJson({
      reply: "Got it — morning or afternoon?", conversation_id: "conv-1",
      meta: { mode: "booking", state: "waiting_for_time_preference" } }));
    const before = chatPosts(sb).length;
    stripChips(sb).find((c) => hasClass(c, "dp-open")).click();
    await flush();
    const body = lastActionBody(sb);
    ok("open chip submits a calendar_choice action via POST /chat",
      chatPosts(sb).length === before + 1 &&
      body && body.action &&
      body.action.type === "calendar_choice" &&
      body.action.choice_id === "pick-date:" + openIso);
    ok("the submitted action carries ONLY {type, choice_id}",
      Object.keys(body.action).length === 2);
  }

  // 4. Strip and full-calendar selections share the SAME action path.
  {
    // Strip selection.
    const sbA = buildSandbox();
    const openIso = nextLocalIso(0);
    await openStrip(sbA, { openDays: [openIso] });
    sbA.setChatResponder(() => successfulJson({ reply: "ok",
      conversation_id: "conv-1", meta: {} }));
    stripChips(sbA).find((c) => hasClass(c, "dp-open")).click();
    await flush();
    const stripBody = lastActionBody(sbA);

    // Grid selection (reveal secondary calendar, click the same day).
    const sbB = buildSandbox();
    await openStrip(sbB, { openDays: [openIso] });
    moreOf(sbB).click(); await flush(); await flush();
    sbB.setChatResponder(() => successfulJson({ reply: "ok",
      conversation_id: "conv-1", meta: {} }));
    const gridOpen = gridDays(sbB).find((d) => hasClass(d, "dp-open"));
    gridOpen.click();
    await flush();
    const gridBody = lastActionBody(sbB);

    ok("strip and grid produce the identical calendar_choice payload",
      stripBody && gridBody &&
      stripBody.action.type === "calendar_choice" &&
      gridBody.action.type === "calendar_choice" &&
      stripBody.action.choice_id === gridBody.action.choice_id &&
      gridBody.action.choice_id === "pick-date:" + openIso);
  }

  // 5. "See full calendar" reveals the EXISTING month grid (no extra fetch).
  {
    const sb = buildSandbox();
    await openStrip(sb, { openDays: [nextLocalIso(0)] });
    const before = previewFetches(sb).length;
    ok("month grid is hidden until asked, but already built on signal",
      gridOf(sb).hidden === true && gridDays(sb).length > 0);
    moreOf(sb).click(); await flush(); await flush();
    ok("clicking 'See full calendar' reveals the grid with NO new fetch",
      gridOf(sb).hidden === false &&
      moreOf(sb).attributes["aria-expanded"] === "true" &&
      previewFetches(sb).length === before);
    moreOf(sb).click();
    ok("the toggle can hide the grid again (show/hide, one calendar)",
      gridOf(sb).hidden === true &&
      moreOf(sb).attributes["aria-expanded"] === "false");
  }

  // 6. Typed-date fallback remains usable.
  {
    const sb = buildSandbox();
    await openStrip(sb, { openDays: [nextLocalIso(0)] });
    ok("typed input stays enabled while the strip is shown (fallback intact)",
      sb.elementsById.input.disabled === false);
    ok("static: strip failure status invites typing a day instead",
      /type a day|type one/i.test(sb.html));
  }

  // 7. Unavailable dates cannot submit.
  {
    const sb = buildSandbox();
    await openStrip(sb, { openDays: [] });   // all locked
    const before = chatPosts(sb).length;
    const locked = stripChips(sb).find((c) => hasClass(c, "dp-locked"));
    locked.click(); await flush();
    ok("clicking a locked chip submits nothing",
      locked.disabled === true && chatPosts(sb).length === before);
  }

  // 8. Duplicate clicks cannot submit twice.
  {
    const sb = buildSandbox();
    const openIso = nextLocalIso(0);
    await openStrip(sb, { openDays: [openIso] });
    sb.setChatResponder(() => new Promise(() => {}));   // hold in flight
    const before = chatPosts(sb).length;
    const chip = stripChips(sb).find((c) => hasClass(c, "dp-open"));
    chip.click(); await flush();
    chip.click(); await flush();                          // duplicate
    ok("a second click on the selected chip is blocked (single submission)",
      chatPosts(sb).length === before + 1);
  }

  // 9. Start Over resets BOTH the strip and the calendar.
  {
    const sb = buildSandbox();
    await openStrip(sb, { openDays: [nextLocalIso(0)] });
    ok("picker present before Start Over",
      pickerRows(sb).length === 1 && stripChips(sb).length >= 1);
    run(sb.context, "startOver()");
    ok("Start Over removes strip + calendar and resets in-memory state",
      pickerRows(sb).length === 0 &&
      run(sb.context, "pickerSubmitted") === false &&
      run(sb.context, "pickerBounds") === null);
  }

  // 10. Stale / superseded responses cannot repopulate the strip.
  {
    const resolvers = [];
    const sb = buildSandbox();
    sb.setPreviewResponder((url) => new Promise((resolve) =>
      resolvers.push({ url, resolve })));
    sb.setChatResponder(() => successfulJson({ reply: "day?",
      conversation_id: "conv-1", meta: { calendar_picker: { stage: "date" } } }));
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush();
    run(sb.context, 'inputEl.value = "book again";');
    await run(sb.context, "sendMessage()");
    await flush();
    ok("a newer signal supersedes the older strip fetch", resolvers.length === 2);
    // Resolve NEWEST first (open today), then the STALE one (all locked).
    resolvers[1].resolve({ ok: true, status: 200, json: () =>
      Promise.resolve(previewPayloadFor(resolvers[1].url, { openDays: [nextLocalIso(0)] })) });
    await flush(); await flush();
    resolvers[0].resolve({ ok: true, status: 200, json: () =>
      Promise.resolve(previewPayloadFor(resolvers[0].url, { openDays: [] })) });
    await flush(); await flush();
    const openChips = stripChips(sb).filter((c) => hasClass(c, "dp-open"));
    ok("the stale response cannot repopulate the strip (newest wins)",
      pickerRows(sb).length === 1 && openChips.length >= 1);
  }

  // 11. Narrow / mobile: horizontal scroll is contained; a fade cue appears.
  {
    const sb = buildSandbox();
    await openStrip(sb, { openDays: [nextLocalIso(0)] });
    const strip = stripOf(sb), fade = fadeOf(sb);
    ok("static: the strip scrolls horizontally, contained by a max-width wrap",
      /\.dp-strip\s*\{[^}]*overflow-x:\s*auto/.test(sb.html) &&
      /\.dp-strip-wrap\s*\{[^}]*max-width/.test(sb.html));
    // Simulate an overflowing strip; the scroll handler reveals the fade cue.
    strip.scrollWidth = 640; strip.clientWidth = 240; strip.scrollLeft = 0;
    strip.dispatch("scroll");
    ok("when the strip overflows, the right-edge fade cue is shown",
      !fade.classList.contains("dp-hide"));
    strip.scrollLeft = 640;   // scrolled to the end
    strip.dispatch("scroll");
    ok("at the end of the scroll the fade cue hides (no phantom overflow)",
      fade.classList.contains("dp-hide"));
  }

  // 12. Picker-gating unchanged: no signal -> no strip, no fetch.
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson({ reply: "Okay.",
      conversation_id: "conv-1", meta: {} }));
    run(sb.context, 'inputEl.value = "hello";');
    await run(sb.context, "sendMessage()");
    await flush();
    ok("picker/booking gating intact: no signal renders no strip and no fetch",
      pickerRows(sb).length === 0 && previewFetches(sb).length === 0);
  }

  // ------------------------------------------------------------------------
  // G1 — a TRUE "Next 7 days" across month/year boundaries. The cross-month
  // tail is fetched from the SAME public availability-preview endpoint; the
  // strip always shows exactly seven ordered chips, and a following-month chip
  // submits through the same calendar_choice handoff as any other date.
  // ------------------------------------------------------------------------
  async function openStripAt(nowIso) {
    const targets = [];
    for (let i = 0; i < 7; i++) targets.push(isoAdd(nowIso, i));
    const sb = buildSandbox({ nowIso });
    sb.setPreviewResponder((url) => successfulJson(previewPayloadFor(url, {
      openDays: targets, earliest: targets[0], latest: targets[6],
    })));
    sb.setChatResponder(() => successfulJson({
      reply: "What day works?", conversation_id: "conv-1",
      meta: { mode: "booking", state: "waiting_for_date",
        calendar_picker: { stage: "date" } },
    }));
    run(sb.context, 'inputEl.value = "I need an appointment";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush(); await flush();   // month + cross-month tail
    return { sb, targets };
  }
  function stripIsos(sb) {
    return stripChips(sb).map((c) => {
      const m = String(c.attributes["aria-label"] || "").match(/(\d{4}-\d{2}-\d{2})/);
      return m ? m[1] : null;
    });
  }
  const boundaryCases = [
    { name: "31-day month (Jan 30 -> Feb)",  now: "2027-01-30" },
    { name: "30-day month (Apr 28 -> May)",  now: "2027-04-28" },
    { name: "February (Feb 25 -> Mar)",      now: "2027-02-25" },
    { name: "year boundary (Dec 28 -> Jan)", now: "2026-12-28" },
  ];
  for (const bc of boundaryCases) {
    const { sb, targets } = await openStripAt(bc.now);
    const isos = stripIsos(sb);
    ok(`G1 ${bc.name}: exactly seven ordered chips spanning the boundary`,
      isos.length === 7 && isos.every((v, i) => v === targets[i]));
    ok(`G1 ${bc.name}: month + tail = two same-endpoint fetches`,
      previewFetches(sb).length === 2);
    const followIso = targets.find((iso) => iso.slice(0, 7) !== bc.now.slice(0, 7));
    const followChip = stripChips(sb).find((c) =>
      String(c.attributes["aria-label"] || "").includes(followIso));
    followChip && followChip.click();
    const body = lastActionBody(sb) || {};
    ok(`G1 ${bc.name}: following-month chip is open and submits via calendar_choice`,
      !!followChip && hasClass(followChip, "dp-open") &&
      body.action && body.action.choice_id === "pick-date:" + followIso);
  }
  {
    // Mid-month: the 7-day window stays in one month -> no tail, single fetch.
    const { sb, targets } = await openStripAt("2027-03-10");
    ok("G1 mid-month: seven chips from a single in-month fetch (no tail)",
      stripIsos(sb).length === 7 &&
      stripIsos(sb).every((v, i) => v === targets[i]) &&
      previewFetches(sb).length === 1);
  }
  {
    // Supersession: a cross-month tail still in flight must NOT repopulate an
    // abandoned picker after Start Over (shared pickerFetchSeq drops it).
    const nowIso = "2027-01-30";
    const targets = [];
    for (let i = 0; i < 7; i++) targets.push(isoAdd(nowIso, i));
    const sb = buildSandbox({ nowIso });
    const tailResolvers = [];
    sb.setPreviewResponder((url) => {
      const s = new URL(String(url)).searchParams.get("start_day");
      if (s.slice(0, 7) === nowIso.slice(0, 7)) {          // current month: now
        return successfulJson(previewPayloadFor(url, {
          openDays: targets, earliest: targets[0], latest: targets[6] }));
      }
      return new Promise((resolve) => tailResolvers.push({ url, resolve })); // tail
    });
    sb.setChatResponder(() => successfulJson({
      reply: "What day?", conversation_id: "conv-1",
      meta: { mode: "booking", state: "waiting_for_date",
        calendar_picker: { stage: "date" } },
    }));
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();               // month resolved, tail deferred
    run(sb.context, "startOver()");
    tailResolvers.forEach((t) => t.resolve(successfulJson(previewPayloadFor(t.url, {
      openDays: targets, earliest: targets[0], latest: targets[6] }))));
    await flush(); await flush();
    ok("G1 supersession: Start Over drops an in-flight cross-month tail",
      pickerRows(sb).length === 0);
  }
  {
    // A NEWER authoritative date signal (rebuilds the picker row) drops an
    // older in-flight tail: it must not complete a superseded strip.
    const nowIso = "2027-01-30";
    const targets = [];
    for (let i = 0; i < 7; i++) targets.push(isoAdd(nowIso, i));
    const sb = buildSandbox({ nowIso });
    const tailResolvers = [];
    sb.setPreviewResponder((url) => {
      const s = new URL(String(url)).searchParams.get("start_day");
      if (s.slice(0, 7) === nowIso.slice(0, 7)) {
        return successfulJson(previewPayloadFor(url, {
          openDays: targets, earliest: targets[0], latest: targets[6] }));
      }
      return new Promise((resolve) => tailResolvers.push({ url, resolve }));
    });
    sb.setChatResponder(() => successfulJson({ reply: "day?",
      conversation_id: "conv-1", meta: { mode: "booking",
        state: "waiting_for_date", calendar_picker: { stage: "date" } } }));
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();
    run(sb.context, "renderDatePicker()");     // newer authoritative signal
    tailResolvers.forEach((t) => t.resolve(successfulJson(previewPayloadFor(t.url, {
      openDays: targets, earliest: targets[0], latest: targets[6] }))));
    await flush(); await flush();
    // Only the fresh (empty) row remains; the stale tail did not add chips.
    ok("G1 supersession: a newer picker signal discards the old strip tail",
      stripChips(sb).length === 0);
  }

  // ------------------------------------------------------------------------
  // G1 (Finding 1) — a REQUIRED cross-month tail that fails/does not validate
  // must NOT render a partial strip: show a visible failure + Retry, never a
  // one-to-six-chip "Next 7 days". Retry re-attempts the same same-endpoint
  // tail; a successful Retry restores exactly seven ordered chips.
  // ------------------------------------------------------------------------
  async function openStripTail(nowIso, tailResponder) {
    const targets = [];
    for (let i = 0; i < 7; i++) targets.push(isoAdd(nowIso, i));
    const sb = buildSandbox({ nowIso });
    sb.setPreviewResponder((url) => {
      const s = new URL(String(url)).searchParams.get("start_day");
      if (s.slice(0, 7) === nowIso.slice(0, 7)) {           // current month
        return successfulJson(previewPayloadFor(url, {
          openDays: targets, earliest: targets[0], latest: targets[6] }));
      }
      return tailResponder(url, targets);                    // tail per test
    });
    sb.setChatResponder(() => successfulJson({ reply: "day?",
      conversation_id: "conv-1", meta: { mode: "booking",
        state: "waiting_for_date", calendar_picker: { stage: "date" } } }));
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush(); await flush();
    return { sb, targets };
  }
  function stripStatusEls(sb) {
    const strip = stripOf(sb);
    if (!strip) return { status: null, retry: null };
    return {
      status: strip.children.find((c) => hasClass(c, "dp-strip-status")) || null,
      retry: strip.children.find((c) => hasClass(c, "dp-retry")) || null,
    };
  }
  const failModes = [
    { name: "HTTP non-200",
      r: () => Promise.resolve({ ok: false, status: 500,
        json: () => Promise.resolve({}) }) },
    { name: "network rejection",
      r: () => Promise.reject(new Error("network down")) },
    { name: "malformed JSON",
      r: () => Promise.resolve({ ok: true, status: 200,
        json: () => Promise.reject(new Error("bad json")) }) },
    { name: "invalid response shape",
      r: () => successfulJson({ timezone: "", days: "nope" }) },
  ];
  for (const fm of failModes) {
    const { sb } = await openStripTail("2027-01-30", fm.r);
    const { status, retry } = stripStatusEls(sb);
    ok(`G1 tail ${fm.name}: visible failure + Retry, and ZERO partial chips`,
      !!status && !!retry && stripChips(sb).length === 0);
  }
  {
    // A failed Retry stays visibly failed (still no partial strip).
    const { sb } = await openStripTail("2027-01-30",
      () => Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) }));
    let r = stripStatusEls(sb).retry;
    r.click(); await flush(); await flush(); await flush();
    const after = stripStatusEls(sb);
    ok("G1 tail Retry (still failing): remains visibly failed, no partial chips",
      !!after.status && !!after.retry && stripChips(sb).length === 0);
  }
  {
    // Transient failure then success: Retry restores exactly seven ordered chips.
    let calls = 0;
    const { sb, targets } = await openStripTail("2027-01-30", (url, targets) => {
      calls += 1;
      if (calls === 1) return Promise.resolve({ ok: false, status: 500,
        json: () => Promise.resolve({}) });
      return successfulJson(previewPayloadFor(url, {
        openDays: targets, earliest: targets[0], latest: targets[6] }));
    });
    ok("G1 tail: first render failed (Retry shown, no partial)",
      !!stripStatusEls(sb).retry && stripChips(sb).length === 0);
    stripStatusEls(sb).retry.click();
    await flush(); await flush(); await flush();
    ok("G1 tail Retry (now succeeding): restores exactly seven ordered chips",
      stripChips(sb).length === 7 &&
      stripIsos(sb).every((v, i) => v === targets[i]));
  }

  // ------------------------------------------------------------------------
  // G1 (Finding 2) — ordinary full-calendar month navigation must NOT cancel
  // the primary strip's in-flight tail; resolving the tail after navigation
  // still completes the same live strip with exactly seven ordered chips.
  // ------------------------------------------------------------------------
  {
    const nowIso = "2027-01-30";
    const targets = [];
    for (let i = 0; i < 7; i++) targets.push(isoAdd(nowIso, i));
    const sb = buildSandbox({ nowIso });
    const tailResolvers = [];
    sb.setPreviewResponder((url) => {
      const u = new URL(String(url));
      const s = u.searchParams.get("start_day"), e = u.searchParams.get("end_day");
      if (s.slice(0, 7) === "2027-01") {                 // today's month
        return successfulJson(previewPayloadFor(url, {
          openDays: targets, earliest: targets[0], latest: targets[6] }));
      }
      if (s === "2027-02-01" && e === targets[6]) {      // the strip tail: defer
        return new Promise((resolve) => tailResolvers.push({ url, resolve }));
      }
      return successfulJson(previewPayloadFor(url, {       // month navigation
        openDays: [], earliest: targets[0], latest: targets[6] }));
    });
    sb.setChatResponder(() => successfulJson({ reply: "day?",
      conversation_id: "conv-1", meta: { mode: "booking",
        state: "waiting_for_date", calendar_picker: { stage: "date" } } }));
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();       // Jan resolves; strip fires deferred tail
    ok("G1 nav: strip tail deferred (no chips, no failure yet)",
      stripChips(sb).length === 0 && !stripStatusEls(sb).retry);
    moreOf(sb).click(); await flush();  // reveal the secondary full calendar
    const nextBtn = collect(gridOf(sb), []).find((el) =>
      hasClass(el, "dp-nav") &&
      String(el.attributes["aria-label"] || "").includes("Next"));
    ok("G1 nav: full calendar revealed with an enabled Next-month control",
      !!nextBtn && nextBtn.disabled === false);
    nextBtn.click(); await flush(); await flush();   // navigate (bumps fetchSeq)
    tailResolvers.forEach((t) => t.resolve(successfulJson(previewPayloadFor(t.url, {
      openDays: targets, earliest: targets[0], latest: targets[6] }))));
    await flush(); await flush();
    ok("G1 nav: tail resolved AFTER navigation still completes 7 ordered chips",
      stripChips(sb).length === 7 &&
      stripIsos(sb).every((v, i) => v === targets[i]));
  }

  // ------------------------------------------------------------------------
  // G1 (retry race) — only the NEWEST strip-tail attempt may mutate the live
  // strip. Rapid Retry must not let an older attempt (success OR failure)
  // overwrite a newer result. Per-attempt token (pickerStripAttempt).
  // ------------------------------------------------------------------------
  async function openStripDeferredTails(nowIso) {
    const targets = [];
    for (let i = 0; i < 7; i++) targets.push(isoAdd(nowIso, i));
    const sb = buildSandbox({ nowIso });
    const tails = [];   // one entry per tail fetch, in call order
    sb.setPreviewResponder((url) => {
      const s = new URL(String(url)).searchParams.get("start_day");
      if (s.slice(0, 7) === nowIso.slice(0, 7)) {
        return successfulJson(previewPayloadFor(url, {
          openDays: targets, earliest: targets[0], latest: targets[6] }));
      }
      return new Promise((resolve) => tails.push({ url, resolve }));
    });
    sb.setChatResponder(() => successfulJson({ reply: "day?",
      conversation_id: "conv-1", meta: { mode: "booking",
        state: "waiting_for_date", calendar_picker: { stage: "date" } } }));
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();   // month resolves; strip fires tails[0]
    return { sb, targets, tails };
  }
  const failRes = () => ({ ok: false, status: 500, json: () => Promise.resolve({}) });
  const okRes = (url, targets) => successfulJson(previewPayloadFor(url, {
    openDays: targets, earliest: targets[0], latest: targets[6] }));

  {
    // Rapid Retry: an OLDER attempt resolving first must not mutate; only the
    // NEWEST attempt renders.
    const { sb, targets, tails } = await openStripDeferredTails("2027-01-30");
    tails[0].resolve(failRes()); await flush(); await flush();  // initial fail
    const rbtn = stripStatusEls(sb).retry;
    rbtn.click(); await flush();          // attempt 2 -> tails[1]
    rbtn.click(); await flush();          // attempt 3 -> tails[2]
    tails[1].resolve(okRes(tails[1].url, targets));   // OLDER success first
    await flush(); await flush();
    ok("G1 race: an older retry attempt resolving first does NOT render",
      stripChips(sb).length === 0);
    tails[2].resolve(okRes(tails[2].url, targets));   // NEWEST success
    await flush(); await flush();
    ok("G1 race: the newest retry attempt renders exactly seven ordered chips",
      stripChips(sb).length === 7 &&
      stripIsos(sb).every((v, i) => v === targets[i]));
  }
  {
    // Newer SUCCESS first, older FAILURE later -> seven chips remain.
    const { sb, targets, tails } = await openStripDeferredTails("2027-01-30");
    tails[0].resolve(failRes()); await flush(); await flush();
    const rbtn = stripStatusEls(sb).retry;
    rbtn.click(); await flush();          // attempt 2 -> tails[1]
    rbtn.click(); await flush();          // attempt 3 -> tails[2]
    tails[2].resolve(okRes(tails[2].url, targets));   // newer success first
    await flush(); await flush();
    tails[1].resolve(failRes());                       // older failure later
    await flush(); await flush();
    ok("G1 race: older failure after newer success cannot overwrite (7 chips)",
      stripChips(sb).length === 7 &&
      stripIsos(sb).every((v, i) => v === targets[i]));
  }
  {
    // Newer FAILURE first, older SUCCESS later -> newer failure state remains.
    const { sb, targets, tails } = await openStripDeferredTails("2027-01-30");
    tails[0].resolve(failRes()); await flush(); await flush();
    const rbtn = stripStatusEls(sb).retry;
    rbtn.click(); await flush();          // attempt 2 -> tails[1]
    rbtn.click(); await flush();          // attempt 3 -> tails[2]
    tails[2].resolve(failRes());          // newer failure first
    await flush(); await flush();
    tails[1].resolve(okRes(tails[1].url, targets));   // older success later
    await flush(); await flush();
    const st = stripStatusEls(sb);
    ok("G1 race: older success after newer failure cannot overwrite (fail stays)",
      !!st.status && !!st.retry && stripChips(sb).length === 0);
  }
  {
    // Retry synchronously replaces the control with a loading state (no fan-out).
    const { sb, tails } = await openStripDeferredTails("2027-01-30");
    tails[0].resolve(failRes()); await flush(); await flush();
    stripStatusEls(sb).retry.click();     // do NOT flush: inspect synchronous state
    const st = stripStatusEls(sb);
    ok("G1 race: Retry synchronously shows loading (Retry removed) before await",
      !!st.status && !st.retry && stripChips(sb).length === 0);
  }
  {
    // Start Over during a retry prevents any later UI mutation.
    const { sb, targets, tails } = await openStripDeferredTails("2027-01-30");
    tails[0].resolve(failRes()); await flush(); await flush();
    stripStatusEls(sb).retry.click(); await flush();   // attempt 2 -> tails[1]
    run(sb.context, "startOver()");
    tails[1].resolve(okRes(tails[1].url, targets));
    await flush(); await flush();
    ok("G1 race: Start Over during a retry blocks the late result",
      pickerRows(sb).length === 0);
  }
  {
    // A newer authoritative picker signal during a retry prevents the old
    // attempt from mutating the new picker.
    const { sb, targets, tails } = await openStripDeferredTails("2027-01-30");
    tails[0].resolve(failRes()); await flush(); await flush();
    stripStatusEls(sb).retry.click(); await flush();   // attempt 2 -> tails[1]
    run(sb.context, "renderDatePicker()");             // newer signal -> new strip
    await flush(); await flush();                      // new month -> tails[2]
    tails[1].resolve(okRes(tails[1].url, targets));     // old attempt resolves late
    await flush(); await flush();
    ok("G1 race: a newer picker signal blocks the old retry from mutating",
      stripChips(sb).length === 0);
  }

  // ------------------------------------------------------------------------
  // G1 (Finding 2) — generic row removal (clearActionRows) ends the strip
  // lifecycle so a pending tail cannot mutate a detached strip; the retained
  // in-flight (.dp-submitting) row's strip is preserved.
  // ------------------------------------------------------------------------
  {
    const { sb, targets, tails } = await openStripDeferredTails("2027-01-30");
    run(sb.context, "clearActionRows()");   // removes non-submitting picker row
    tails[0].resolve(okRes(tails[0].url, targets));   // late tail
    await flush(); await flush();
    ok("G1 rows: clearActionRows invalidates an in-flight tail (no late update)",
      pickerRows(sb).length === 0 && stripChips(sb).length === 0);
  }
  // ------------------------------------------------------------------------
  // G1 (strip freeze) — a date submission freezes the strip-tail lifecycle: a
  // pending tail (success OR failure) cannot mutate a submitting picker, and a
  // pre-submission strip Retry is disabled during submission. The submitting
  // row stays attached until the action resolves (pickDate owns that boundary).
  // ------------------------------------------------------------------------
  async function openBoundaryTailPending(nowIso) {
    const targets = [];
    for (let i = 0; i < 7; i++) targets.push(isoAdd(nowIso, i));
    const inMonth = targets.filter((iso) => iso.slice(0, 7) === nowIso.slice(0, 7));
    const sb = buildSandbox({ nowIso });
    const tails = [];
    sb.setPreviewResponder((url) => {
      const u = new URL(String(url));
      const s = u.searchParams.get("start_day"), e = u.searchParams.get("end_day");
      if (s.slice(0, 7) === nowIso.slice(0, 7)) {
        return successfulJson(previewPayloadFor(url, {
          openDays: inMonth, earliest: targets[0], latest: targets[6] }));
      }
      if (e === targets[6]) {
        return new Promise((resolve, reject) => tails.push({ url, resolve, reject }));
      }
      return successfulJson(previewPayloadFor(url, {
        openDays: [], earliest: targets[0], latest: targets[6] }));
    });
    let chatCalls = 0;
    sb.setChatResponder(() => {
      chatCalls += 1;
      if (chatCalls === 1) return successfulJson({ reply: "day?",
        conversation_id: "conv-1", meta: { mode: "booking",
          state: "waiting_for_date", calendar_picker: { stage: "date" } } });
      return new Promise(() => {});   // hold the submission POST unresolved
    });
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();      // grid renders; strip tail pending
    moreOf(sb).click(); await flush(); // reveal the full calendar
    return { sb, targets, tails };
  }
  const submitGridDate = (sb) =>
    gridDays(sb).find((d) => hasClass(d, "dp-open")).click();

  { // A — pending tail SUCCESS cannot mutate a submitting picker
    const { sb, targets, tails } = await openBoundaryTailPending("2027-01-30");
    submitGridDate(sb); await flush();     // select grid date, POST held
    const fetches = previewFetches(sb).length, posts = chatPosts(sb).length;
    tails[0].resolve(successfulJson(previewPayloadFor(tails[0].url, {
      openDays: targets, earliest: targets[0], latest: targets[6] })));
    await flush(); await flush();
    ok("F-strip-A: late tail success creates no enabled strip chips after submission",
      stripChips(sb).length === 0 ||
      stripChips(sb).every((c) => c.disabled === true));
    ok("F-strip-A: late tail success adds no availability request or POST",
      previewFetches(sb).length === fetches && chatPosts(sb).length === posts);
    ok("F-strip-A: the submitting row stays attached until the action resolves",
      pickerRows(sb).length === 1 &&
      lastRow(sb).classList.contains("dp-submitting"));
  }

  { // B — pending tail FAILURE (4 modes) cannot create a Retry/status
    const modes = [
      { name: "HTTP failure",
        go: (t) => t.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) }) },
      { name: "network rejection", go: (t) => t.reject(new Error("net down")) },
      { name: "malformed JSON",
        go: (t) => t.resolve({ ok: true, status: 200, json: () => Promise.reject(new Error("bad")) }) },
      { name: "invalid shape",
        go: (t) => t.resolve(successfulJson({ timezone: "", days: "nope" })) },
    ];
    for (const m of modes) {
      const { sb, tails } = await openBoundaryTailPending("2027-01-30");
      submitGridDate(sb); await flush();   // select grid date, POST held
      m.go(tails[0]);
      await flush(); await flush();
      ok(`F-strip-B (${m.name}): late tail failure creates no Retry/status after submission`,
        !stripStatusEls(sb).retry && stripChips(sb).length === 0);
    }
  }

  { // C — a pre-submission strip Retry is disabled during submission
    const { sb, tails } = await openBoundaryTailPending("2027-01-30");
    tails[0].resolve({ ok: false, status: 500, json: () => Promise.resolve({}) });
    await flush(); await flush();          // tail fails BEFORE submission -> Retry
    const retry = stripStatusEls(sb).retry;
    ok("F-strip-C: a strip-tail failure before submission shows an enabled Retry",
      !!retry && retry.disabled !== true);
    submitGridDate(sb); await flush();     // select a full-calendar date, POST held
    ok("F-strip-C: the strip Retry is disabled during the date submission",
      retry.disabled === true);
    const fetches = previewFetches(sb).length, posts = chatPosts(sb).length;
    retry.click(); await flush(); await flush();
    ok("F-strip-C: activating the disabled strip Retry does nothing (no fetch/POST/rerender)",
      previewFetches(sb).length === fetches && chatPosts(sb).length === posts &&
      stripChips(sb).length === 0);
  }

  { // D — retained: a successful tail BEFORE submission still renders seven chips
    const { sb, targets, tails } = await openBoundaryTailPending("2027-01-30");
    tails[0].resolve(successfulJson(previewPayloadFor(tails[0].url, {
      openDays: targets, earliest: targets[0], latest: targets[6] })));
    await flush(); await flush();
    ok("F-strip-D: a successful tail before submission still renders seven ordered chips",
      stripChips(sb).length === 7 &&
      stripIsos(sb).every((v, i) => v === targets[i]));
  }

  // ------------------------------------------------------------------------
  // G1 (status supersession) — an authoritative current-month terminal status
  // mirrored into the strip supersedes an older pending strip tail, so a late
  // tail cannot overwrite it with chips or a Retry. Full-calendar and strip
  // messages stay consistent.
  // ------------------------------------------------------------------------
  async function openEmptyWindowTailPending(nowIso) {
    const targets = [];
    for (let i = 0; i < 7; i++) targets.push(isoAdd(nowIso, i));
    const sb = buildSandbox({ nowIso });
    const tails = [];
    // Current-month payload is a VALID shape with an EMPTY booking window
    // (earliest_bookable_day > latest_bookable_day), all days locked.
    const emptyWin = { openDays: [], earliest: "2027-12-01", latest: "2027-01-01" };
    sb.setPreviewResponder((url) => {
      const u = new URL(String(url));
      const s = u.searchParams.get("start_day"), e = u.searchParams.get("end_day");
      if (s.slice(0, 7) === nowIso.slice(0, 7)) {
        return successfulJson(previewPayloadFor(url, emptyWin));   // current month
      }
      if (e === targets[6]) {
        return new Promise((resolve, reject) => tails.push({ url, resolve, reject }));
      }
      return successfulJson(previewPayloadFor(url, emptyWin));
    });
    sb.setChatResponder(() => successfulJson({ reply: "day?",
      conversation_id: "conv-1", meta: { mode: "booking",
        state: "waiting_for_date", calendar_picker: { stage: "date" } } }));
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();     // current month: empty-window status + tail pending
    return { sb, targets, tails };
  }
  const gridStatusEl = (sb) => collect(gridOf(sb), []).find((el) => hasClass(el, "dp-status"));

  { // A — empty booking window: late tail SUCCESS cannot render chips
    const { sb, targets, tails } = await openEmptyWindowTailPending("2027-01-30");
    const before = stripStatusEls(sb);
    ok("F-status-A: empty window shows the truthful unavailable status, no chips",
      !!before.status && !before.retry && stripChips(sb).length === 0);
    const text0 = before.status.textContent;
    tails[0].resolve(successfulJson(previewPayloadFor(tails[0].url, {
      openDays: targets, earliest: targets[0], latest: targets[6] })));
    await flush(); await flush();
    const after = stripStatusEls(sb);
    ok("F-status-A: a late tail success does not replace the unavailable status with chips",
      stripChips(sb).length === 0 && !!after.status &&
      after.status.textContent === text0 && !after.retry);
  }

  { // B — empty window: late tail FAILURE variants cannot add Retry or replace
    const modes = [
      { name: "HTTP failure",
        go: (t) => t.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) }) },
      { name: "network rejection", go: (t) => t.reject(new Error("net down")) },
      { name: "malformed JSON",
        go: (t) => t.resolve({ ok: true, status: 200, json: () => Promise.reject(new Error("bad")) }) },
      { name: "invalid shape",
        go: (t) => t.resolve(successfulJson({ timezone: "", days: "nope" })) },
    ];
    for (const m of modes) {
      const { sb, tails } = await openEmptyWindowTailPending("2027-01-30");
      const text0 = stripStatusEls(sb).status.textContent;
      m.go(tails[0]);
      await flush(); await flush();
      const st = stripStatusEls(sb);
      ok(`F-status-B (${m.name}): late tail failure adds no Retry and preserves the status`,
        !st.retry && stripChips(sb).length === 0 &&
        !!st.status && st.status.textContent === text0);
    }
  }

  { // C — full-calendar and strip messages remain consistent across the late tail
    const { sb, targets, tails } = await openEmptyWindowTailPending("2027-01-30");
    ok("F-status-C: full-calendar and strip both show the unavailable status (consistent)",
      !!gridStatusEl(sb) && !!stripStatusEls(sb).status &&
      gridDays(sb).length === 0 && stripChips(sb).length === 0);
    tails[0].resolve(successfulJson(previewPayloadFor(tails[0].url, {
      openDays: targets, earliest: targets[0], latest: targets[6] })));
    await flush(); await flush();
    ok("F-status-C: a late tail keeps both surfaces consistent (still unavailable)",
      !!gridStatusEl(sb) && !!stripStatusEls(sb).status &&
      gridDays(sb).length === 0 && stripChips(sb).length === 0);
  }

  { // D — retained: a normal cross-month tail (non-empty window) still renders 7
    const { sb, targets, tails } = await openBoundaryTailPending("2027-01-30");
    tails[0].resolve(successfulJson(previewPayloadFor(tails[0].url, {
      openDays: targets, earliest: targets[0], latest: targets[6] })));
    await flush(); await flush();
    ok("F-status-D: normal cross-month happy path still renders exactly seven ordered chips",
      stripChips(sb).length === 7 && stripIsos(sb).every((v, i) => v === targets[i]));
  }

  // ------------------------------------------------------------------------
  // G1 (Finding 1) — an ASYNC cross-month strip completion must preserve the
  // live month-grid controls in panel.dpControls so a submission disables the
  // WHOLE picker; and the strip registry must replace (not accumulate) chips.
  // ------------------------------------------------------------------------
  async function openCrossMonthHeldSubmit(nowIso) {
    const targets = [];
    for (let i = 0; i < 7; i++) targets.push(isoAdd(nowIso, i));
    const sb = buildSandbox({ nowIso });
    const tails = [];
    sb.setPreviewResponder((url) => {
      const u = new URL(String(url));
      const s = u.searchParams.get("start_day"), e = u.searchParams.get("end_day");
      if (s.slice(0, 7) === nowIso.slice(0, 7)) {          // today's month
        return successfulJson(previewPayloadFor(url, {
          openDays: targets, earliest: targets[0], latest: targets[6] }));
      }
      if (e === targets[6]) {                              // the strip tail: defer
        return new Promise((resolve) => tails.push({ url, resolve }));
      }
      return successfulJson(previewPayloadFor(url, {        // month navigation
        openDays: [], earliest: targets[0], latest: targets[6] }));
    });
    let chatCalls = 0;
    sb.setChatResponder(() => {
      chatCalls += 1;
      if (chatCalls === 1) return successfulJson({ reply: "day?",
        conversation_id: "conv-1", meta: { mode: "booking",
          state: "waiting_for_date", calendar_picker: { stage: "date" } } });
      return new Promise(() => {});   // hold the submission POST unresolved
    });
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();     // month resolves; grid rendered; tail deferred
    return { sb, targets, tails };
  }
  const gridNavs = (sb) => collect(gridOf(sb), []).filter((el) => hasClass(el, "dp-nav"));
  {
    const { sb, targets, tails } = await openCrossMonthHeldSubmit("2027-01-30");
    ok("F1reg: cross-month tail deferred while the grid is already rendered",
      stripChips(sb).length === 0 && gridDays(sb).length > 0);
    moreOf(sb).click(); await flush();                    // reveal full calendar
    tails[0].resolve(successfulJson(previewPayloadFor(tails[0].url, {
      openDays: targets, earliest: targets[0], latest: targets[6] })));
    await flush(); await flush();
    ok("F1reg: tail resolves after the grid exists -> seven strip chips",
      stripChips(sb).length === 7);
    // Select an open strip chip with POST /chat held unresolved.
    stripChips(sb).find((c) => hasClass(c, "dp-open")).click();
    await flush();
    const navs = gridNavs(sb), days = gridDays(sb);
    ok("F1reg: submission disables strip chips + See full calendar + nav + all grid days",
      stripChips(sb).every((c) => c.disabled === true) &&
      moreOf(sb).disabled === true &&
      navs.length >= 2 && navs.every((n) => n.disabled === true) &&
      days.length > 0 && days.every((d) => d.disabled === true));
    // Clicking a previously-enabled nav/day during submission does nothing.
    const fetchesAfter = previewFetches(sb).length;
    const postsAfter = chatPosts(sb).length;
    const nextBtn = navs.find((n) =>
      String(n.attributes["aria-label"] || "").includes("Next"));
    if (nextBtn) nextBtn.click();
    const openDay = days.find((d) => hasClass(d, "dp-open"));
    if (openDay) openDay.click();
    await flush();
    ok("F1reg: clicking a disabled nav/day during submission adds no fetch or POST",
      previewFetches(sb).length === fetchesAfter &&
      chatPosts(sb).length === postsAfter);
  }
  {
    // Later month navigation registers the newly rendered grid controls.
    const { sb, targets, tails } = await openCrossMonthHeldSubmit("2027-01-30");
    moreOf(sb).click(); await flush();
    tails[0].resolve(successfulJson(previewPayloadFor(tails[0].url, {
      openDays: targets, earliest: targets[0], latest: targets[6] })));
    await flush(); await flush();
    gridNavs(sb).find((n) =>
      String(n.attributes["aria-label"] || "").includes("Next")).click();
    await flush(); await flush();                         // navigate to February
    const febDays = gridDays(sb);
    stripChips(sb).find((c) => hasClass(c, "dp-open")).click();  // submit
    await flush();
    ok("F1reg: later month navigation registers the new grid in the disable sweep",
      febDays.length > 0 && febDays.every((d) => d.disabled === true));
  }
  {
    // Re-rendering the strip does not accumulate detached old chip controls.
    const sb = buildSandbox();
    await openStrip(sb,
      { openDays: [0, 1, 2, 3, 4, 5, 6].map((i) => nextLocalIso(i)),
        earliest: nextLocalIso(0), latest: nextLocalIso(6) });
    const days7 = [0, 1, 2, 3, 4, 5, 6].map((i) => ({
      local_date: nextLocalIso(i), weekday: weekdayOf(nextLocalIso(i)),
      state: "open" }));
    const panel = gridOf(sb);
    const rsc = run(sb.context, "renderStripChips");   // sandbox fn reference
    rsc(panel.stripEl, panel, panel.stripFade, days7); // re-render twice on the
    rsc(panel.stripEl, panel, panel.stripFade, days7); // same live instance
    const chipCtrls = panel.dpControls.filter((c) => hasClass(c, "dp-chip"));
    ok("F1reg: repeated strip renders keep exactly seven chip controls (no accumulation)",
      stripChips(sb).length === 7 && chipCtrls.length === 7);
    ok("F1reg: dpControls still carries the See full calendar toggle + grid controls",
      panel.dpControls.some((c) => hasClass(c, "dp-more")) &&
      panel.dpControls.some((c) => hasClass(c, "dp-nav")) &&
      panel.dpControls.some((c) => hasClass(c, "dp-day")));
  }

  // ------------------------------------------------------------------------
  // G1 (month-preview freeze) — a date submission freezes the full-calendar
  // preview lifecycle: its Retry is disabled, and a late month response cannot
  // rebuild enabled controls. Two asynchronous orderings + retained behaviors.
  // ------------------------------------------------------------------------
  const aug = [0, 1, 2, 3, 4, 5, 6].map((i) => nextLocalIso(i));   // Aug 4..10
  const dpRetry = (sb) => collect(gridOf(sb), []).find((el) => hasClass(el, "dp-retry"));
  async function openAugPickerRevealed(nextMonthResponder) {
    const sb = buildSandbox();                                     // today 2026-08-04
    sb.setPreviewResponder((url) => {
      const s = new URL(String(url)).searchParams.get("start_day");
      if (s.slice(0, 7) === "2026-08") {
        return successfulJson(previewPayloadFor(url, {
          openDays: aug, earliest: aug[0], latest: "2026-09-30" }));
      }
      return nextMonthResponder(url);
    });
    let chatCalls = 0;
    sb.setChatResponder(() => {
      chatCalls += 1;
      if (chatCalls === 1) return successfulJson({ reply: "day?",
        conversation_id: "conv-1", meta: { mode: "booking",
          state: "waiting_for_date", calendar_picker: { stage: "date" } } });
      return new Promise(() => {});   // hold the submission POST unresolved
    });
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();      // August loads: strip + grid
    moreOf(sb).click(); await flush(); // reveal the full calendar
    return sb;
  }

  { // ORDERING A — failure/retry
    const sb = await openAugPickerRevealed(
      () => Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) }));
    gridNavs(sb).find((n) =>
      String(n.attributes["aria-label"] || "").includes("Next")).click();
    await flush(); await flush();      // navigate to September -> fails -> Retry
    const retry = dpRetry(sb);
    ok("F-A: month navigation failure shows a full-calendar Retry", !!retry);
    stripChips(sb).find((c) => hasClass(c, "dp-open")).click();  // submit (held)
    await flush();
    ok("F-A: the full-calendar Retry is disabled during the date submission",
      retry.disabled === true);
    const fetches = previewFetches(sb).length, posts = chatPosts(sb).length;
    const days = gridDays(sb).length;
    retry.click(); await flush(); await flush();
    ok("F-A: activating the disabled Retry does nothing (no fetch, POST, or rerender)",
      previewFetches(sb).length === fetches &&
      chatPosts(sb).length === posts && gridDays(sb).length === days);
  }

  { // ORDERING B — pending response
    const held = [];
    const sb = await openAugPickerRevealed(
      (url) => new Promise((resolve) => held.push({ url, resolve })));
    const oldNav = gridNavs(sb).find((n) =>
      String(n.attributes["aria-label"] || "").includes("Next"));
    const oldDay = gridDays(sb).find((d) => hasClass(d, "dp-open"));
    oldNav.click(); await flush(); await flush();   // September pending (loading)
    stripChips(sb).find((c) => hasClass(c, "dp-open")).click();  // submit (held)
    await flush();
    const fetches = previewFetches(sb).length, posts = chatPosts(sb).length;
    const days = gridDays(sb).length;
    held.forEach((h) => h.resolve(successfulJson(previewPayloadFor(h.url, {
      openDays: [], earliest: aug[0], latest: "2026-09-30" }))));
    await flush(); await flush();
    ok("F-B: a late month response does not render/replace the calendar",
      gridDays(sb).length === days &&
      !collect(gridOf(sb), []).some((el) =>
        (hasClass(el, "dp-day") || hasClass(el, "dp-nav") || hasClass(el, "dp-retry")) &&
        el.disabled === false));
    if (oldNav) oldNav.click();
    if (oldDay) oldDay.click();
    await flush();
    ok("F-B: clicking old captured controls causes no preview request or POST",
      previewFetches(sb).length === fetches && chatPosts(sb).length === posts);
  }

  { // RETAINED — normal month navigation before any submission still works
    const sb = await openAugPickerRevealed((url) =>
      successfulJson(previewPayloadFor(url, {
        openDays: ["2026-09-10"], earliest: aug[0], latest: "2026-09-30" })));
    gridNavs(sb).find((n) =>
      String(n.attributes["aria-label"] || "").includes("Next")).click();
    await flush(); await flush();
    const days = gridDays(sb);
    ok("F-retain: normal next-month navigation renders the new month (enabled)",
      days.length > 0 && days.some((d) => hasClass(d, "dp-open") && d.disabled === false));
  }

  { // RETAINED — Start Over invalidates a pending month preview
    const held = [];
    const sb = await openAugPickerRevealed(
      (url) => new Promise((resolve) => held.push({ url, resolve })));
    gridNavs(sb).find((n) =>
      String(n.attributes["aria-label"] || "").includes("Next")).click();
    await flush(); await flush();          // September pending
    run(sb.context, "startOver()");
    held.forEach((h) => h.resolve(successfulJson(previewPayloadFor(h.url, {
      openDays: ["2026-09-10"], earliest: aug[0], latest: "2026-09-30" }))));
    await flush(); await flush();
    ok("F-retain: Start Over invalidates a pending month preview (no late render)",
      pickerRows(sb).length === 0);
  }

  // ------------------------------------------------------------------------
  // G3 — selected-control foreground meets AA (>= 4.5:1) by the ACTUAL WCAG
  // contrast ratio of the applied foreground, for ANY tenant primary. One
  // shared --mia-on-primary value; no per-control logic.
  // ------------------------------------------------------------------------
  {
    const hexRgb = (h) => {
      h = h.replace("#", "");
      if (h.length === 3) h = h.split("").map((c) => c + c).join("");
      return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16),
        parseInt(h.slice(4, 6), 16)];
    };
    const relLum = (rgb) => {
      const f = (v) => { v /= 255;
        return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
    };
    const ratio = (a, b) => {
      const la = relLum(a), lb = relLum(b);
      const hi = Math.max(la, lb), lo = Math.min(la, lb);
      return (hi + 0.05) / (lo + 0.05);
    };
    const cases = [
      { name: "default #2563eb",       primary: "#2563eb",            rgb: hexRgb("#2563eb") },
      { name: "very light #eaf2ff",    primary: "#eaf2ff",            rgb: hexRgb("#eaf2ff") },
      { name: "mid-luminance #777777", primary: "#777777",            rgb: hexRgb("#777777") },
      { name: "boundary #767676",      primary: "#767676",            rgb: hexRgb("#767676") },
      { name: "amber #a66a00",         primary: "#a66a00",            rgb: hexRgb("#a66a00") },
      { name: "dark #0b1220",          primary: "#0b1220",            rgb: hexRgb("#0b1220") },
      { name: "rgb() light",           primary: "rgb(250, 250, 250)", rgb: [250, 250, 250] },
    ];
    const sb = buildSandbox();
    for (const c of cases) {
      run(sb.context, `applyTheme({ primary: ${JSON.stringify(c.primary)} });`);
      const fg = sb.styleMap["--mia-on-primary"];
      const r = ratio(c.rgb, hexRgb(fg));
      ok(`G3 ${c.name}: derived ${fg} meets AA (actual ${r.toFixed(2)}:1 >= 4.5)`,
        (fg === "#111827" || fg === "#000000" || fg === "#ffffff") && r >= 4.5);
    }
  }

  // ------------------------------------------------------------------------
  // G3 (Finding 3) — alpha is honored. A translucent or uninterpretable primary
  // is NOT scored as opaque; it deterministically falls back to the default
  // opaque foreground and never leaves a stale --mia-on-primary. Accepted
  // (opaque) primaries keep an actual composited contrast >= 4.5:1.
  // ------------------------------------------------------------------------
  {
    const hexRgb = (h) => {
      h = h.replace("#", "");
      if (h.length === 3) h = h.split("").map((c) => c + c).join("");
      return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16),
        parseInt(h.slice(4, 6), 16)];
    };
    const relLum = (rgb) => {
      const f = (v) => { v /= 255;
        return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
    };
    const ratio = (a, b) => {
      const la = relLum(a), lb = relLum(b);
      const hi = Math.max(la, lb), lo = Math.min(la, lb);
      return (hi + 0.05) / (lo + 0.05);
    };
    const applied = (sb) => sb.styleMap["--mia-primary"];
    const fgOf = (sb) => sb.styleMap["--mia-on-primary"];

    // Translucent primaries are rejected (not scored as opaque) -> default fg.
    {
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "rgba(255,255,255,0)" });');
      ok("G3 rgba alpha 0: rejected (not scored as opaque white) -> default #ffffff",
        fgOf(sb) === "#ffffff" && applied(sb) === undefined);
    }
    {
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "rgba(255,255,255,0.5)" });');
      ok("G3 rgba alpha 0.5: translucent rejected -> default #ffffff, not applied",
        fgOf(sb) === "#ffffff" && applied(sb) === undefined);
    }
    // Opaque rgba(...,1) IS accepted and keeps real contrast.
    {
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "rgba(11,18,32,1)" });');
      ok("G3 rgba alpha 1 (dark): accepted, on-primary #ffffff, actual >= 4.5",
        fgOf(sb) === "#ffffff" && ratio([11, 18, 32], hexRgb(fgOf(sb))) >= 4.5);
    }
    {
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "rgba(234,242,255,1)" });');
      ok("G3 rgba alpha 1 (light): accepted, on-primary #111827, actual >= 4.5",
        fgOf(sb) === "#111827" && ratio([234, 242, 255], hexRgb(fgOf(sb))) >= 4.5);
    }
    // A malformed primary AFTER a valid theme must not leave a stale foreground.
    {
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "#eaf2ff" });');
      ok("G3 setup: light theme applied first (on-primary #111827)",
        fgOf(sb) === "#111827" && applied(sb) === "#eaf2ff");
      run(sb.context, 'applyTheme({ primary: "not-a-color" });');
      ok("G3 malformed-after-theme: no stale fg, stale primary cleared (default)",
        fgOf(sb) === "#ffffff" && applied(sb) === undefined);
    }
    // Named + HSL are handled with a deterministic opaque fallback (Option B).
    {
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "navy" });');
      ok("G3 named color: deterministic opaque fallback -> default #ffffff",
        fgOf(sb) === "#ffffff" && applied(sb) === undefined);
      run(sb.context, 'applyTheme({ primary: "hsl(210, 100%, 50%)" });');
      ok("G3 hsl(): deterministic opaque fallback -> default #ffffff",
        fgOf(sb) === "#ffffff" && applied(sb) === undefined);
    }

    // ----------------------------------------------------------------------
    // G3 (Finding 2) — the color APPLIED to --mia-primary is the SAME
    // canonical color that was contrast-scored (no raw parser input, no bare
    // hex-without-# token, no decimal-vs-applied drift).
    // ----------------------------------------------------------------------
    const canonHex = (rgb) => {
      const h = (v) => Math.round(v).toString(16).padStart(2, "0");
      return "#" + h(rgb[0]) + h(rgb[1]) + h(rgb[2]);
    };
    {
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "2563eb" });');   // bare 6-hex
      ok("G3 canon: bare 6-hex canonicalized to #2563eb (valid CSS), >= 4.5",
        applied(sb) === "#2563eb" &&
        ratio([0x25, 0x63, 0xeb], hexRgb(fgOf(sb))) >= 4.5);
    }
    {
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "07f" });');      // bare 3-hex
      ok("G3 canon: bare 3-hex canonicalized to #0077ff, >= 4.5",
        applied(sb) === "#0077ff" &&
        ratio([0x00, 0x77, 0xff], hexRgb(fgOf(sb))) >= 4.5);
    }
    {
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "rgb(37.6, 99.2, 235.4)" });');
      const ch = hexRgb(applied(sb));   // channels ACTUALLY applied
      ok("G3 canon: decimal rgb applied channels == scored channels [38,99,235]",
        ch[0] === 38 && ch[1] === 99 && ch[2] === 235 &&
        ratio(ch, hexRgb(fgOf(sb))) >= 4.5);
    }
    const canonForms = [
      { p: "#2563eb", ch: [37, 99, 235] },
      { p: "#abc", ch: [0xaa, 0xbb, 0xcc] },
      { p: "rgb(37, 99, 235)", ch: [37, 99, 235] },
      { p: "rgba(11, 18, 32, 1)", ch: [11, 18, 32] },
      { p: "#eaf2ff", ch: [234, 242, 255] },
    ];
    for (const f of canonForms) {
      const sb = buildSandbox();
      run(sb.context, `applyTheme({ primary: ${JSON.stringify(f.p)} });`);
      ok(`G3 canon: ${f.p} -> ${canonHex(f.ch)} applied, actual contrast >= 4.5`,
        applied(sb) === canonHex(f.ch) &&
        ratio(f.ch, hexRgb(fgOf(sb))) >= 4.5);
    }
    {
      // Malformed/translucent following a prior theme leaves no stale value.
      const sb = buildSandbox();
      run(sb.context, 'applyTheme({ primary: "#eaf2ff" });');
      run(sb.context, 'applyTheme({ primary: "rgba(0,0,0,0)" });');
      ok("G3 canon: translucent after theme -> no stale primary or foreground",
        applied(sb) === undefined && fgOf(sb) === "#ffffff");
      run(sb.context, 'applyTheme({ primary: "#0b1220" });');
      run(sb.context, 'applyTheme({ primary: "garbage" });');
      ok("G3 canon: malformed after theme -> no stale primary or foreground",
        applied(sb) === undefined && fgOf(sb) === "#ffffff");
    }
  }
  {
    const sb = buildSandbox();
    ok("G3 static: all four selected controls consume the shared --mia-on-primary",
      /\.dp-day\.dp-selected\s*\{[^}]*color:\s*var\(--mia-on-primary/.test(sb.html) &&
      /\.dp-chip\.dp-selected\s*\{[^}]*color:\s*var\(--mia-on-primary/.test(sb.html) &&
      /\.time-pref-btn\.tp-selected\s*\{[^}]*color:\s*var\(--mia-on-primary/.test(sb.html) &&
      /\.slot-chip\.sp-selected\s*\{[^}]*color:\s*var\(--mia-on-primary/.test(sb.html));
    ok("G3 static: a single shared derivation exists (one applyOnPrimaryContrast)",
      (sb.html.match(/function applyOnPrimaryContrast/g) || []).length === 1);
  }

  // ------------------------------------------------------------------------
  // UX-B — mobile "See full calendar": revealing the grid on a short
  // scrolled viewport must restore its visibility with the SAME single
  // bounded V8 mechanism the rest of the picker uses (one frame,
  // connected-element guard, block:"nearest"). Open only; Start Over safe.
  // ------------------------------------------------------------------------
  {
    const sb = buildSandbox({ mobile: true });
    await openStrip(sb, { openDays: [nextLocalIso(0)] });
    const more = moreOf(sb);
    ok("UXB: mobile env — See full calendar is a real enabled BUTTON, grid hidden, aria-controls wired",
      run(sb.context, "mobileQuery.matches") === true &&
      more.tagName === "BUTTON" && more.type === "button" &&
      more.disabled === false && gridOf(sb).hidden === true &&
      more.attributes["aria-controls"] === "miaFullCalendar");
    const fetchesBefore = previewFetches(sb).length;
    more.click();
    ok("UXB BITE: ONE mobile tap reveals the grid AND queues exactly one bounded restoration frame (zero extra fetches, nothing synchronous)",
      gridOf(sb).hidden === false &&
      more.attributes["aria-expanded"] === "true" &&
      previewFetches(sb).length === fetchesBefore &&
      sb.rafQueue.length === 1 &&
      gridOf(sb).scrollIntoViewCalls.length === 0);
    await flush();
    ok("UXB: exactly one state transition per tap — no touch+click double-fire toggling it straight back closed",
      gridOf(sb).hidden === false && (more.listeners.click || []).length === 1);
    sb.flushRaf();
    ok("UXB BITE: the frame scrolls the LIVE revealed grid into view with block:'nearest' — one frame only, none re-queued",
      gridOf(sb).scrollIntoViewCalls.length === 1 &&
      gridOf(sb).scrollIntoViewCalls[0] !== null &&
      gridOf(sb).scrollIntoViewCalls[0].block === "nearest" &&
      sb.rafQueue.length === 0);
    more.click();
    ok("UXB: tapping again hides the grid and queues NO restoration frame (closing needs no scroll)",
      gridOf(sb).hidden === true &&
      more.attributes["aria-expanded"] === "false" &&
      sb.rafQueue.length === 0);
  }
  {
    // Submission-lock retention: with POST /chat held unresolved after a
    // strip-chip selection, the toggle is swept disabled and its handler is
    // inert — activating it reveals nothing and queues no NEW frame (the
    // single pending frame is pickDate's own V8 restoration for the chip).
    const sb = buildSandbox({ mobile: true });
    await openStrip(sb, { openDays: [nextLocalIso(0)] });
    sb.setChatResponder(() => new Promise(() => {}));
    stripChips(sb).find((c) => hasClass(c, "dp-open")).click();
    await flush();
    const more = moreOf(sb);
    const fetches = previewFetches(sb).length;
    const posts = chatPosts(sb).length;
    const framesBefore = sb.rafQueue.length;
    more.click();
    ok("UXB: during an unresolved date submission the disabled toggle stays inert — no reveal, no fetch, no POST, no new frame",
      more.disabled === true &&
      gridOf(sb).hidden === true &&
      previewFetches(sb).length === fetches &&
      chatPosts(sb).length === posts &&
      sb.rafQueue.length === framesBefore);
  }
  {
    // Start Over with the reveal's restoration frame still pending: the row
    // is removed first, so the late frame finds a DETACHED grid — it must
    // neither scroll nor reopen anything; a brand-new picker starts clean.
    const sb = buildSandbox({ mobile: true });
    await openStrip(sb, { openDays: [nextLocalIso(0)] });
    const panel = gridOf(sb);
    moreOf(sb).click();
    ok("UXB: Start Over precondition — calendar open with the restoration frame still pending",
      panel.hidden === false && sb.rafQueue.length === 1);
    run(sb.context, "startOver()");
    ok("UXB: Start Over removes the OPEN calendar and resets picker state",
      pickerRows(sb).length === 0 && panel.isConnected === false &&
      run(sb.context, "pickerSubmitted") === false);
    sb.flushRaf();
    ok("UXB: the late pending frame skips the detached grid — no scroll, no reopen after Start Over",
      panel.scrollIntoViewCalls.length === 0 && pickerRows(sb).length === 0);
    await openStrip(sb, { openDays: [nextLocalIso(0)] });
    const more2 = moreOf(sb);
    const panel2 = gridOf(sb);
    ok("UXB: a new picker after Start Over starts closed from clean state — one fresh listener, no leftover frames",
      panel2 !== panel && panel2.hidden === true &&
      more2.attributes["aria-expanded"] === "false" &&
      (more2.listeners.click || []).length === 1 &&
      sb.rafQueue.length === 0);
  }
  {
    // Desktop mechanism parity: the SAME single bounded frame runs, and
    // block:"nearest" makes it a visual no-op when the grid is already in
    // view — the existing desktop reveal contract is unchanged.
    const sb = buildSandbox();
    await openStrip(sb, { openDays: [nextLocalIso(0)] });
    const before = previewFetches(sb).length;
    moreOf(sb).click();
    ok("UXB: desktop reveal keeps the existing contract (open, truthful aria, zero extra fetches) via the same bounded mechanism",
      gridOf(sb).hidden === false &&
      moreOf(sb).attributes["aria-expanded"] === "true" &&
      previewFetches(sb).length === before &&
      sb.rafQueue.length === 1);
    sb.flushRaf();
    ok("UXB: desktop restoration is the shared minimal block:'nearest' scroll (a no-op when already visible)",
      gridOf(sb).scrollIntoViewCalls.length === 1 &&
      gridOf(sb).scrollIntoViewCalls[0].block === "nearest");
  }
  {
    // Static pin (EOL-normalized): the reveal reuses the SHARED V8 helper,
    // OPEN only, and the toggle binds click ONLY — no touch listener that
    // could double-fire a mobile tap into open-then-closed.
    const html = fs.readFileSync(CHAT_HTML, "utf8").replace(/\r\n/g, "\n");
    const block = html.slice(
      html.indexOf('more.textContent = "See full calendar"'),
      html.indexOf("panel.moreControl = more"));
    ok("UXB static: open-only shared restoreSelectedVisibility, click-only binding on the toggle",
      block.includes("if (!panel.hidden) restoreSelectedVisibility(panel);") &&
      (block.match(/addEventListener\(/g) || []).length === 1 &&
      !/touchstart|touchend|pointerdown/.test(block));
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
}

main();
