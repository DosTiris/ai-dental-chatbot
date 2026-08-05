// tests/test_widget_date_picker.js
//
// C2-A.2 — patient-widget visual DATE picker, frontend proofs.
//
// Executes the REAL inline script from static/chat.html in a Node `vm`
// sandbox (the same technique as tests/test_chat_structured_actions.js).
// Proves the 25 plan-required behaviors: signal-gated rendering, the
// public-preview-only network surface, locked day states, single
// submission through the existing POST /chat action lane, supersession,
// visible failure + retry, Start Over cleanup, and the absence of any
// admin surface, browser storage, slot identifier, or time-stage UI.
// V6 (owner audit) added the retained-clamp retry proofs. V7 (owner
// audit) replaces the allowJump Boolean with an EXPLICIT CLAMP BUDGET
// and proves: a patient-initiated request (open, Retry, navigation)
// consumes at most ONE automatic server-directed jump; a zero-budget
// response whose newly returned nonempty bounds still exclude the
// requested month renders NOTHING and fails visibly with Retry; each
// Retry is a fresh bounded attempt; superseded responses can render
// neither a grid nor a failure; no automatic request loop exists.
// V8 (owner audit) adds the targeted visibility-preservation contract
// (mock call-level only; real layout geometry is proven by the
// package's Chromium probe, which is NOT part of the repository).
//
// Run:
//   node tests/test_widget_date_picker.js
// or:
//   MIA_CHAT_HTML=/path/to/static/chat.html node tests/test_widget_date_picker.js

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CHAT_HTML = process.env.MIA_CHAT_HTML ||
  path.join(__dirname, "..", "static", "chat.html");

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
  "Friday", "Saturday"];

function makeClassList() {
  const values = new Set();
  return {
    add: (...items) => items.forEach((item) => values.add(item)),
    remove: (...items) => items.forEach((item) => values.delete(item)),
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
    scrollTop: 0,
    scrollHeight: 0,
    id: "",
    type: "",
    href: "",
    target: "",
    rel: "",
    className: "",
  };

  // The picker clears its panel with `innerHTML = ""` (real-DOM child
  // clearing); mirror that contract in the stub.
  let innerHTMLValue = "";
  Object.defineProperty(element, "innerHTML", {
    get: () => innerHTMLValue,
    set: (value) => {
      innerHTMLValue = String(value);
      if (innerHTMLValue === "") {
        element.children.forEach((child) => { child.parent = null; });
        element.children = [];
      }
    },
  });

  Object.defineProperty(element, "parentElement", {
    get: () => element.parent,
  });

  element.classList = makeClassList();
  element.appendChild = (child) => {
    child.parent = element;
    element.children.push(child);
    return child;
  };
  element.remove = () => {
    if (!element.parent) return;
    element.parent.children = element.parent.children.filter(
      (child) => child !== element
    );
    element.parent = null;
  };
  element.addEventListener = (name, handler) => {
    (element.listeners[name] = element.listeners[name] || []).push(handler);
  };
  element.click = () => {
    (element.listeners.click || []).forEach((handler) => handler());
  };
  element.setAttribute = (name, value) => {
    element.attributes[name] = value;
  };
  element.focus = () => {};
  element.querySelectorAll = () => [];
  // V8: record targeted visibility-restoration calls (the real layout
  // proof lives in the package's Chromium probe; the mock only proves
  // the call contract).
  element.scrollIntoViewCalls = [];
  element.scrollIntoView = (opts) => {
    element.scrollIntoViewCalls.push(opts || null);
  };
  Object.defineProperty(element, "isConnected", {
    get: () => {
      let node = element;
      while (node.parent) node = node.parent;
      return node.tagName === "BODY";
    },
  });
  return element;
}

function collect(element, output) {
  output.push(element);
  element.children.forEach((child) => collect(child, output));
  return output;
}

function successfulJson(payload) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(payload),
  });
}

// ---------------------------------------------------------------------------
// Default preview responder: parses start_day/end_day from the requested
// URL and answers with the C2-A.1 public contract shape. `openDays` are
// ISO dates reported "open"; everything else is "unavailable".
// ---------------------------------------------------------------------------

function isoAdd(iso, days) {
  const [y, m, d] = iso.split("-").map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + days));
  return t.toISOString().slice(0, 10);
}

const FIXED_TODAY = "2026-08-04";
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
const MIA_FAKE_DATE = makeFakeDate(FIXED_TODAY);
function todayIso() { return FIXED_TODAY; }

function weekdayOf(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
}

function previewPayloadFor(url, options = {}) {
  const parsed = new URL(String(url));
  const start = parsed.searchParams.get("start_day");
  const end = parsed.searchParams.get("end_day");
  const openDays = options.openDays || [];
  const earliest = options.earliest || isoAdd(todayIso(), 1);
  const latest = options.latest || isoAdd(todayIso(), 40);
  const days = [];
  let cursor = start;
  while (cursor <= end) {
    days.push({
      local_date: cursor,
      weekday: weekdayOf(cursor),
      state: openDays.indexOf(cursor) !== -1 ? "open" : "unavailable",
    });
    cursor = isoAdd(cursor, 1);
  }
  if (options.mutateDays) options.mutateDays(days);
  const payload = {
    timezone: "America/New_York",
    requested_start_day: start,
    requested_end_day: end,
    earliest_bookable_day: earliest,
    latest_bookable_day: latest,
    days: days,
  };
  if (options.mutatePayload) options.mutatePayload(payload);
  return payload;
}

function buildSandbox(options = {}) {
  const elementsById = {};
  const body = makeElement("body");

  [
    "messages",
    "input",
    "sendBtn",
    "miaHeaderTitle",
    "miaHeaderSubtitle",
    "main-menu",
    "service-menu",
    "consentModal",
    "agreeBtn",
  ].forEach((id) => {
    const element = makeElement("div");
    element.id = id;
    elementsById[id] = element;
    body.appendChild(element);
  });

  const inputRow = makeElement("div");
  inputRow.className = "chat-input-row";
  body.appendChild(inputRow);

  const documentStub = {
    documentElement: { style: { setProperty: () => {} } },
    body,
    getElementById: (id) => {
      if (!elementsById[id]) {
        const element = makeElement("div");
        element.id = id;
        elementsById[id] = element;
        body.appendChild(element);
      }
      return elementsById[id];
    },
    createElement: (tag) => makeElement(tag),
    querySelector: (selector) => {
      if (selector === ".chat-input-row") return inputRow;
      return makeElement("div");
    },
    querySelectorAll: (selector) => {
      const all = collect(body, []);
      const classes = selector
        .split(",")
        .map((item) => item.trim().replace(/^\./, ""));
      return all.filter((element) =>
        classes.some((name) =>
          element.classList.contains(name) ||
          String(element.className).split(/\s+/).includes(name)
        )
      );
    },
  };

  const storage = {};
  const storageOps = [];
  const fetchCalls = [];
  // V8: deterministic rAF — callbacks run only when a test flushes
  // them, mirroring "one bounded frame later".
  const rafQueue = [];
  let chatResponder = options.chatResponder || (() => successfulJson({
    reply: "Okay.",
    conversation_id: "conv-1",
    meta: {},
  }));
  let previewResponder = options.previewResponder ||
    ((url) => successfulJson(previewPayloadFor(url, options.preview || {})));

  const sandbox = {
    console,
    requestAnimationFrame: (cb) => { rafQueue.push(cb); return rafQueue.length; },
    document: documentStub,
    window: {
      location: {
        search: "?client_key=test-client",
        hostname: "localhost",
        origin: "http://localhost",
      },
      matchMedia: () => ({
        matches: false,
        addEventListener: () => {},
      }),
    },
    localStorage: {
      getItem: (key) => {
        storageOps.push({ op: "get", key });
        return key in storage ? storage[key] : null;
      },
      setItem: (key, value) => {
        storageOps.push({ op: "set", key });
        storage[key] = String(value);
      },
      removeItem: (key) => {
        storageOps.push({ op: "remove", key });
        delete storage[key];
      },
    },
    URLSearchParams,
    URL,
    Date: MIA_FAKE_DATE,
    setTimeout,
    clearTimeout,
    fetch: (url, requestOptions) => {
      fetchCalls.push({ url, options: requestOptions || {} });

      if (String(url).includes("/chat/config")) {
        return successfulJson({});
      }
      if (String(url).includes("/chat/calendar/availability-preview")) {
        return previewResponder(String(url));
      }
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
  vm.runInContext(match[1], context, {
    filename: "static/chat.html<script>",
  });

  const flushRaf = () => {
    const pending = rafQueue.splice(0, rafQueue.length);
    pending.forEach((cb) => cb());
  };
  return {
    context,
    body,
    elementsById,
    fetchCalls,
    storageOps,
    html,
    rafQueue,
    flushRaf,
    setChatResponder: (responder) => { chatResponder = responder; },
    setPreviewResponder: (responder) => { previewResponder = responder; },
  };
}

function run(context, source) {
  return vm.runInContext(source, context);
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function pickerRows(elementsById) {
  return elementsById.messages.children.filter(
    (child) => child.className === "date-picker-row"
  );
}

function panelOf(row) {
  // G1: the row now holds [strip, "See full calendar", month panel]; locate
  // the .date-picker month panel by class rather than by child position.
  return (row.children || []).find((c) =>
    String(c.className).split(/\s+/).includes("date-picker")) || null;
}

function pickerPanel(elementsById) {
  const rows = pickerRows(elementsById);
  return rows.length ? panelOf(rows[rows.length - 1]) : null;
}

function panelParts(panel) {
  const all = collect(panel, []);
  const has = (element, name) =>
    element.classList.contains(name) ||
    String(element.className).split(/\s+/).includes(name);
  return {
    all,
    title: all.find((element) => has(element, "dp-title")) || null,
    navs: all.filter((element) => has(element, "dp-nav")),
    days: all.filter((element) => has(element, "dp-day")),
    openDays: all.filter((element) =>
      has(element, "dp-day") && has(element, "dp-open")),
    lockedDays: all.filter((element) =>
      has(element, "dp-day") && has(element, "dp-locked")),
    statuses: all.filter((element) => has(element, "dp-status")),
    retries: all.filter((element) => has(element, "dp-retry")),
  };
}

function PICKER_MONTH_TITLE(year, monthIndex) {
  const names = ["January","February","March","April","May","June","July",
    "August","September","October","November","December"];
  return `${names[monthIndex]} ${year}`;
}

function chatPosts(fetchCalls) {
  return fetchCalls.filter((call) =>
    String(call.url).endsWith("/chat") &&
    call.options &&
    call.options.method === "POST"
  );
}

function previewFetches(fetchCalls) {
  return fetchCalls.filter((call) =>
    String(call.url).includes("/chat/calendar/availability-preview")
  );
}

function parsedBody(call) {
  return JSON.parse(call.options.body);
}

async function openPicker(sb, openDays) {
  // Drive the REAL response path: a bot reply carrying the C2-A.2 signal.
  if (openDays) {
    sb.setPreviewResponder((url) =>
      successfulJson(previewPayloadFor(url, { openDays })));
  }
  sb.setChatResponder(() => successfulJson({
    reply: "What day would work best for your appointment?",
    conversation_id: "conv-1",
    meta: {
      mode: "booking",
      state: "waiting_for_date",
      calendar_picker: { stage: "date" },
    },
  }));
  run(sb.context, 'inputEl.value = "I need an appointment";');
  await run(sb.context, "sendMessage()");
  await flush();
  await flush();
}

let passed = 0;
let failed = 0;

function ok(name, condition) {
  if (condition) {
    passed += 1;
    console.log("ok - " + name);
  } else {
    failed += 1;
    console.log("NOT OK - " + name);
  }
}

async function main() {
  const openIso = isoAdd(todayIso(), 7);

  // 1. No signal, no picker — including a non-date stage value.
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson({
      reply: "Okay.", conversation_id: "conv-1", meta: {},
    }));
    run(sb.context, 'inputEl.value = "hello";');
    await run(sb.context, "sendMessage()");
    await flush();
    ok("picker absent without meta.calendar_picker",
      pickerRows(sb.elementsById).length === 0 &&
      previewFetches(sb.fetchCalls).length === 0);
  }
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson({
      reply: "Okay.", conversation_id: "conv-1",
      meta: { calendar_picker: { stage: "time" } },
    }));
    run(sb.context, 'inputEl.value = "hello";');
    await run(sb.context, "sendMessage()");
    await flush();
    ok("picker absent for a non-date stage (no C2-A.3 time UI)",
      pickerRows(sb.elementsById).length === 0);
  }

  // 2. Only the public preview route is fetched for availability.
  {
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    const previews = previewFetches(sb.fetchCalls);
    const nonChat = sb.fetchCalls.filter((call) =>
      !String(call.url).endsWith("/chat") &&
      !String(call.url).includes("/chat/config"));
    ok("availability comes ONLY from the public preview route",
      previews.length >= 1 &&
      nonChat.length === previews.length &&
      previews.every((call) =>
        String(call.url).includes("/chat/calendar/availability-preview") &&
        String(call.url).includes("client_key=test-client")));
    ok("preview requests carry no headers (no credential surface)",
      previews.every((call) => !call.options.headers));

    // 6/7. Day-state rendering.
    const parts = panelParts(pickerPanel(sb.elementsById));
    const openBtn = parts.openDays[0];
    ok("open day renders as an enabled button whose accessible label " +
       "identifies it as available",
      parts.openDays.length === 1 &&
      openBtn.disabled === false &&
      openBtn.tagName === "BUTTON" &&
      (openBtn.listeners.click || []).length === 1 &&
      String(openBtn.attributes["aria-label"] || "").includes(openIso) &&
      String(openBtn.attributes["aria-label"] || "").includes("available"));
    ok("non-open days are locked (disabled + aria-disabled)",
      parts.lockedDays.length > 0 &&
      parts.lockedDays.every((btn) =>
        btn.disabled === true &&
        btn.attributes["aria-disabled"] === "true" &&
        (btn.listeners.click || []).length === 0));

    // 9. Day cells show ONLY the day number — never a slot count.
    ok("no daily slot count is displayed",
      parts.days.every((btn) => /^\d{1,2}$/.test(String(btn.textContent))));

    // 10-13 + V2 defect 2. Selection keeps the picker ATTACHED to the
    // live messages DOM, visibly selected and fully disabled, for the
    // whole in-flight window; every assertion below reads the LIVE DOM
    // (never a retained detached reference).
    let releaseAction = null;
    sb.setChatResponder(() => new Promise((resolve) => {
      releaseAction = () => resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
          reply: "Got it \u2014 morning or afternoon?",
          conversation_id: "conv-1",
          meta: { mode: "booking", state: "waiting_for_time_preference" },
        }),
      });
    }));
    openBtn.click();
    await flush();
    // ---- in-flight window: interrogate the LIVE DOM only ----
    const liveRows = pickerRows(sb.elementsById);
    ok("submitting picker row remains ATTACHED to the live messages DOM",
      liveRows.length === 1 &&
      liveRows[0].parentElement === sb.elementsById.messages &&
      liveRows[0].classList.contains("dp-submitting"));
    const liveParts = panelParts(panelOf(liveRows[0]));
    const liveSelected = liveParts.days.filter((b) =>
      b.classList.contains("dp-selected"));
    ok("selected day is visibly selected IN the live DOM with " +
       "aria-pressed=true",
      liveSelected.length === 1 &&
      liveSelected[0].attributes["aria-pressed"] === "true");
    ok("every date and navigation control is disabled during submission",
      liveParts.days.every((b) => b.disabled === true) &&
      liveParts.navs.every((b) => b.disabled === true));
    liveSelected[0].click();  // duplicate via the LIVE attached button
    await flush();
    ok("duplicate submission is blocked while the action is in flight",
      chatPosts(sb.fetchCalls).length === 2);
    // ---- resolved boundary ----
    releaseAction();
    await flush(); await flush();
    ok("picker clears only at the resolved response boundary",
      pickerRows(sb.elementsById).length === 0);
    const posts = chatPosts(sb.fetchCalls);
    ok("date submitted exactly once through existing POST /chat",
      posts.length === 2 /* opener message + the date action */ &&
      parsedBody(posts[1]).action !== undefined);
    const action = parsedBody(posts[1]).action;
    ok("existing calendar_choice action type is reused",
      action.type === "calendar_choice" &&
      action.choice_id === "pick-date:" + openIso &&
      Object.keys(action).length === 2);
    ok("no slot ID or internal identifier is submitted",
      !JSON.stringify(parsedBody(posts[1])).match(/slot|hold_id|appointment/i));
    openBtn.click();          // replay after completion: flag set, row gone
    await flush();
    ok("post-completion replay is also blocked",
      chatPosts(sb.fetchCalls).length === 2 &&
      pickerRows(sb.elementsById).length === 0);

    // 23. No browser storage was used for any picker state.
    ok("no localStorage writes for picker state",
      sb.storageOps.every((op) =>
        op.op === "get" || String(op.key).startsWith("chat_conversation_id_")));
  }

  // 3/4/5/25. Static source guarantees: no admin surface, no write
  // endpoints, no storage APIs in the picker block, no time-stage UI.
  {
    const html = fs.readFileSync(CHAT_HTML, "utf8");
    ok("no admin header, credential, or admin endpoint in the widget",
      !html.includes("X-Admin-Key") &&
      !html.includes("x-admin-key") &&
      !/\/admin\//.test(html) &&
      !html.includes("service_role"));
    ok("no booking/hold/confirm/cancel/appointment endpoint in the widget",
      !/\/chat\/calendar\/(hold|book|confirm|cancel|appointment|slot)/.test(html) &&
      !/\/calendar\/appointments/.test(html));
    const start = html.indexOf("C2-A.2 — visual date picker");
    const end = html.indexOf("function resetInputPlaceholder");
    const pickerBlock = html.slice(start, end);
    ok("picker block exists and uses no browser storage API",
      start !== -1 && end > start &&
      !/localStorage|sessionStorage|document\.cookie|indexedDB|caches\.|serviceWorker/
        .test(pickerBlock));
    ok("no visual time-selection UI exists",
      !/dp-time|time-picker|renderTimePicker|pick-time:/.test(html));
    ok("picker state lives in plain in-memory variables",
      pickerBlock.includes("let pickerFetchSeq") &&
      pickerBlock.includes("let pickerSubmitted"));
  }

  // 8. Unknown day state -> visible failure with retry, nothing guessed.
  {
    const sb = buildSandbox({
      preview: {
        mutateDays: (days) => { days[2].state = "mystery"; },
      },
    });
    await openPicker(sb, null);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("unknown day state produces visible failure (fail closed)",
      parts.days.length === 0 &&
      parts.statuses.some((s) => /couldn’t load/.test(s.textContent)) &&
      parts.retries.length === 1);
  }

  // 16. Network failure and HTTP failure -> visible retry that works.
  {
    const sb = buildSandbox();
    let fail = true;
    sb.setPreviewResponder((url) => {
      if (fail) return Promise.reject(new Error("offline"));
      return successfulJson(previewPayloadFor(url, { openDays: [openIso] }));
    });
    sb.setChatResponder(() => successfulJson({
      reply: "What day works?", conversation_id: "conv-1",
      meta: { calendar_picker: { stage: "date" } },
    }));
    run(sb.context, 'inputEl.value = "book me";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();
    let parts = panelParts(pickerPanel(sb.elementsById));
    ok("network failure shows visible failure + retry",
      parts.retries.length === 1 &&
      parts.statuses.some((s) => /couldn’t load/.test(s.textContent)));
    fail = false;
    parts.retries[0].click();
    await flush(); await flush();
    parts = panelParts(pickerPanel(sb.elementsById));
    ok("retry refetches and renders the grid",
      previewFetches(sb.fetchCalls).length === 2 &&
      parts.openDays.length === 1 && parts.retries.length === 0);
  }
  {
    const sb = buildSandbox();
    sb.setPreviewResponder(() => Promise.resolve({
      ok: false, status: 500, json: () => Promise.resolve({}),
    }));
    await openPicker(sb, null);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("HTTP failure shows visible failure + retry",
      parts.retries.length === 1);
  }
  {
    const sb = buildSandbox();
    sb.setPreviewResponder(() => Promise.resolve({
      ok: true, status: 200, json: () => Promise.reject(new Error("bad json")),
    }));
    await openPicker(sb, null);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("malformed preview JSON shows visible failure + retry",
      parts.retries.length === 1);
  }
  {
    const sb = buildSandbox();
    sb.setPreviewResponder(() => successfulJson({ nonsense: true }));
    await openPicker(sb, null);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("malformed preview shape shows visible failure + retry",
      parts.retries.length === 1);
  }

  // 14/15. Supersession: the newer request wins; a stale response that
  // resolves later can never replace the newer result.
  {
    const resolvers = [];
    const sb = buildSandbox();
    sb.setPreviewResponder((url) => new Promise((resolve) => {
      resolvers.push({ url, resolve });
    }));
    sb.setChatResponder(() => successfulJson({
      reply: "What day?", conversation_id: "conv-1",
      meta: { calendar_picker: { stage: "date" } },
    }));
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush();
    // A second signal re-opens the picker: a NEWER fetch begins while the
    // first is still pending.
    run(sb.context, 'inputEl.value = "book again";');
    await run(sb.context, "sendMessage()");
    await flush();
    ok("newer preview request supersedes the older one",
      resolvers.length === 2);
    // Resolve NEWEST first with a distinctive open day, then the STALE
    // one with a different (would-be-misleading) payload.
    resolvers[1].resolve({
      ok: true, status: 200,
      json: () => Promise.resolve(
        previewPayloadFor(resolvers[1].url, { openDays: [openIso] })),
    });
    await flush(); await flush();
    resolvers[0].resolve({
      ok: true, status: 200,
      json: () => Promise.resolve(
        previewPayloadFor(resolvers[0].url, { openDays: [] })),
    });
    await flush(); await flush();
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("stale preview response cannot replace the newer result",
      pickerRows(sb.elementsById).length === 1 &&
      parts.openDays.length === 1);
  }

  // 17/24. Start Over clears everything; reopening builds a FRESH picker.
  {
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    ok("picker rendered before Start Over", pickerRows(sb.elementsById).length === 1);
    run(sb.context, "startOver()");
    ok("Start Over removes the picker and resets its in-memory state",
      pickerRows(sb.elementsById).length === 0 &&
      run(sb.context, "pickerSubmitted") === false &&
      run(sb.context, "pickerBounds") === null);
    // Reopen: fresh instance, fresh fetch, no restored selection.
    await openPicker(sb, [openIso]);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("reopening renders a fresh picker with no stale state",
      pickerRows(sb.elementsById).length === 1 &&
      parts.openDays.length === 1 &&
      !parts.openDays[0].classList.contains("dp-selected") &&
      run(sb.context, "pickerSubmitted") === false);
  }

  // Month navigation stays inside server bounds; empty window is truthful.
  {
    const sb = buildSandbox({
      preview: { earliest: isoAdd(todayIso(), 1), latest: isoAdd(todayIso(), 3) },
    });
    await openPicker(sb, null);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("month navigation is clamped by the server bounds",
      parts.navs.length === 2 &&
      parts.navs.every((nav) => nav.disabled === true));
  }
  {
    const sb = buildSandbox();
    sb.setPreviewResponder((url) => successfulJson(previewPayloadFor(url, {
      earliest: isoAdd(todayIso(), 10),
      latest: isoAdd(todayIso(), 5),
    })));
    await openPicker(sb, null);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("empty booking window shows truthful no-dates messaging",
      parts.days.length === 0 &&
      parts.statuses.some((s) => /type a day/.test(s.textContent)) &&
      parts.retries.length === 0);
  }

  // 18/19/20. Existing action-failure behavior is intact when the picker's
  // date action is rejected by the server.
  async function submitPickWith(responder) {
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    sb.setChatResponder(responder);
    const parts = panelParts(pickerPanel(sb.elementsById));
    parts.openDays[0].click();
    await flush(); await flush();
    return sb;
  }
  {
    const sb = await submitPickWith(() => Promise.resolve({
      ok: false, status: 409,
      json: () => Promise.resolve({ detail: {
        code: "CONVERSATION_UNAVAILABLE",
        message: "Please refresh and start again.",
      } }),
    }));
    const texts = sb.elementsById.messages.children.map((c) => c.textContent);
    ok("conversation-unavailable handling remains intact",
      texts.indexOf("Please refresh and start again.") !== -1);
  }
  {
    const sb = await submitPickWith(() => Promise.resolve({
      ok: false, status: 409,
      json: () => Promise.resolve({ detail: {
        code: "SAFETY_BLOCKED",
        message: "Please call the office directly.",
      } }),
    }));
    const texts = sb.elementsById.messages.children.map((c) => c.textContent);
    ok("safety-blocked handling remains intact",
      texts.indexOf("Please call the office directly.") !== -1);
  }
  {
    const sb = await submitPickWith(() => Promise.resolve({
      ok: false, status: 409,
      json: () => Promise.resolve({ detail: {
        code: "STALE_CHOICE",
        message: "That day is no longer available.",
        calendar_actions: [
          { label: "Tue 10:00 AM", message: "Tue 10:00 AM",
            action: { type: "calendar_choice", choice_id: "abc" } },
        ],
      } }),
    }));
    const texts = sb.elementsById.messages.children.map((c) => c.textContent);
    const replies = sb.elementsById.messages.children.filter(
      (c) => c.className === "quick-replies");
    ok("stale-action recovery (replacement buttons) remains intact",
      texts.indexOf("That day is no longer available.") !== -1 &&
      replies.length === 1 && replies[0].children.length === 1);
    ok("submitting picker cleared at the resolved failure boundary",
      pickerRows(sb.elementsById).length === 0);
  }

  // 21/22. Existing service menu and structured quick replies are intact.
  {
    const sb = buildSandbox();
    const menu = sb.elementsById["service-menu"];
    ok("existing service-menu rendering remains intact",
      menu.children.length >= 6 &&
      menu.children.every((btn) => btn.tagName === "BUTTON"));
  }
  {
    const sb = buildSandbox();
    run(sb.context,
      'renderQuickReplies([{label:"Tue 10:00 AM",message:"Tue 10:00 AM",' +
      'action:{type:"calendar_choice",choice_id:"slot-1"}}]);' +
      'conversationId = "conv-1";');
    const replies = sb.elementsById.messages.children.filter(
      (c) => c.className === "quick-replies");
    replies[0].children[0].click();
    await flush();
    const posts = chatPosts(sb.fetchCalls);
    ok("existing structured quick replies remain intact",
      posts.length === 1 &&
      parsedBody(posts[0]).action.choice_id === "slot-1");
  }

  // ------------------------------------------------------------------
  // V2 defect 3 — range/date binding: every malformed or misaligned
  // preview response must show the visible failure + Retry state.
  // ------------------------------------------------------------------
  async function expectPreviewFailure(name, options) {
    const sb = buildSandbox(options);
    await openPicker(sb, null);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok(name,
      parts.days.length === 0 &&
      parts.retries.length === 1 &&
      parts.statuses.some((s) => /couldn’t load/.test(s.textContent)));
  }
  await expectPreviewFailure(
    "impossible bound date (2026-02-30) fails visibly",
    { preview: { mutatePayload: (p) => {
        p.earliest_bookable_day = "2026-02-30"; } } });
  await expectPreviewFailure(
    "impossible day date (2026-13-01) fails visibly",
    { preview: { mutateDays: (d) => { d[3].local_date = "2026-13-01"; } } });
  await expectPreviewFailure(
    "mismatched requested_start_day fails visibly",
    { preview: { mutatePayload: (p) => {
        p.requested_start_day = isoAdd(p.requested_start_day, 1); } } });
  await expectPreviewFailure(
    "mismatched requested_end_day fails visibly",
    { preview: { mutatePayload: (p) => {
        p.requested_end_day = isoAdd(p.requested_end_day, -1); } } });
  await expectPreviewFailure(
    "missing date in the range fails visibly",
    { preview: { mutateDays: (d) => { d.splice(5, 1); } } });
  await expectPreviewFailure(
    "duplicate date fails visibly",
    { preview: { mutateDays: (d) => { d[6] = { ...d[5] }; } } });
  await expectPreviewFailure(
    "out-of-range date fails visibly",
    { preview: { mutateDays: (d) => {
        d.push({ local_date: isoAdd(d[d.length - 1].local_date, 1),
                 weekday: "Monday", state: "unavailable" }); } } });
  await expectPreviewFailure(
    "nonchronological date list fails visibly",
    { preview: { mutateDays: (d) => {
        const t = d[2]; d[2] = d[3]; d[3] = t; } } });

  // ------------------------------------------------------------------
  // V2 defect 4 — each locked state carries ITS OWN accessible label.
  // ------------------------------------------------------------------
  {
    const sb = buildSandbox({
      preview: { mutateDays: (d) => {
        d[1].state = "full";
        d[2].state = "past";
        // (the rest stay "unavailable"; one open added below)
        d[4].state = "open";
      } },
    });
    await openPicker(sb, null);
    const parts = panelParts(pickerPanel(sb.elementsById));
    const labelOf = (b) => String(b.attributes["aria-label"] || "");
    const byState = (word) => parts.days.filter((b) =>
      labelOf(b).includes("— " + word));
    ok("full date accessible label identifies it as full",
      byState("full").length === 1 && byState("full")[0].disabled === true);
    ok("unavailable date accessible label identifies it as unavailable",
      byState("unavailable").length >= 1 &&
      byState("unavailable").every((b) => b.disabled === true));
    ok("past date accessible label identifies it as past",
      byState("past").length === 1 && byState("past")[0].disabled === true);
    ok("open date accessible label identifies it as available",
      byState("available").length === 1 &&
      byState("available")[0].disabled === false);
  }

  // ------------------------------------------------------------------
  // V4 (owner audit) — a no-replacement STALE_CHOICE for a date pick is
  // STATE-FREE: message only. No picker, no service buttons, no
  // automatic refetch, no repeated POST. A fresh picker renders ONLY
  // from a later authoritative HTTP 200 carrying the meta signal.
  // ------------------------------------------------------------------
  async function stalePickScenario(message) {
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    sb.setChatResponder(() => Promise.resolve({
      ok: false, status: 409,
      json: () => Promise.resolve({ detail: {
        code: "STALE_CHOICE", message: message,
      } }),
    }));
    const before = {
      previews: previewFetches(sb.fetchCalls).length,
      posts: chatPosts(sb.fetchCalls).length,
    };
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush(); await flush(); await flush();
    return { sb, before };
  }
  {
    // (1) duplicate-after-success shape: same state-free envelope.
    const { sb, before } = await stalePickScenario(
      "You already picked a day — morning or afternoon?");
    const texts = sb.elementsById.messages.children.map((c) => c.textContent);
    const serviceRows = sb.elementsById.messages.children.filter(
      (c) => c.className === "quick-replies");
    ok("duplicate-after-success stale: server message visible, no picker, " +
       "no service buttons, typed input usable",
      texts.indexOf("You already picked a day — morning or afternoon?") !== -1 &&
      pickerRows(sb.elementsById).length === 0 &&
      serviceRows.length === 0 &&
      sb.elementsById.input.disabled === false);
  }
  {
    // (2) wrong-state shape: additionally prove NO refetch and NO
    // repeated POST.
    const { sb, before } = await stalePickScenario(
      "That option is no longer available.");
    const texts = sb.elementsById.messages.children.map((c) => c.textContent);
    const serviceRows = sb.elementsById.messages.children.filter(
      (c) => c.className === "quick-replies");
    ok("wrong-state stale: message visible, no picker, no service buttons",
      texts.indexOf("That option is no longer available.") !== -1 &&
      pickerRows(sb.elementsById).length === 0 &&
      serviceRows.length === 0);
    ok("wrong-state stale: no additional preview GET and no repeated POST",
      previewFetches(sb.fetchCalls).length === before.previews &&
      chatPosts(sb.fetchCalls).length === before.posts + 1);
  }
  {
    // (3) a LATER authoritative HTTP 200 with the meta signal is the
    // only path to a fresh picker.
    const { sb } = await stalePickScenario("Stale.");
    ok("no picker exists after the state-free stale rejection",
      pickerRows(sb.elementsById).length === 0);
    sb.setChatResponder(() => successfulJson({
      reply: "What day would work best for your appointment?",
      conversation_id: "conv-1",
      meta: { mode: "booking", state: "waiting_for_date",
              calendar_picker: { stage: "date" } },
    }));
    run(sb.context, 'inputEl.value = "another day please";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();
    ok("a subsequent authoritative 200 with the meta signal renders a " +
       "fresh picker",
      pickerRows(sb.elementsById).length === 1 &&
      panelParts(pickerPanel(sb.elementsById)).openDays.length === 1);
  }
  {
    // Static assertion: the no-replacement pick-date stale branch calls
    // no picker render and no availability fetch.
    const html = fs.readFileSync(CHAT_HTML, "utf8");
    const fnStart = html.indexOf("function handleActionFailureResponse");
    const fnEnd = html.indexOf("async function sendMessage");
    const failureFn = html.slice(fnStart, fnEnd);
    ok("static: handleActionFailureResponse never calls renderDatePicker " +
       "or fetchPickerMonth",
      fnStart !== -1 && fnEnd > fnStart &&
      !/renderDatePicker\s*\(/.test(failureFn) &&
      !/fetchPickerMonth\s*\(/.test(failureFn));
  }

  // ------------------------------------------------------------------
  // V4 (owner audit) — later-than-latest initial month clamp.
  // ------------------------------------------------------------------
  {
    const sb = buildSandbox({
      preview: { earliest: isoAdd(todayIso(), -70),
                 latest: isoAdd(todayIso(), -40) },
    });
    await openPicker(sb, null);
    await flush(); await flush();
    const previews = previewFetches(sb.fetchCalls);
    const parts = panelParts(pickerPanel(sb.elementsById));
    const latestIso = isoAdd(todayIso(), -40);
    const lp = latestIso.split("-").map(Number);
    const expectedTitle = PICKER_MONTH_TITLE(lp[0], lp[1] - 1);
    ok("later-than-latest: the widget jumps ONCE to the latest allowed " +
       "month (no loop, out-of-window month never final)",
      previews.length === 2 &&
      new URL(previews[1].url).searchParams.get("start_day")
        .startsWith(latestIso.slice(0, 7)) &&
      parts.title !== null &&
      parts.title.textContent === expectedTitle);
    ok("later-than-latest: navigation reflects the server bounds",
      parts.navs.length === 2 &&
      parts.navs[1].disabled === true /* next clamped at latest */ &&
      parts.navs[0].disabled === false /* prev open toward earliest */);
  }

  // ------------------------------------------------------------------
  // V6 (owner audit) — a retried INITIAL preview failure retains the one
  // bounded server-directed month clamp, in BOTH directions; a post-jump
  // retry can never jump again.
  // ------------------------------------------------------------------
  {
    // (A) EARLIER direction: the first (allowJump=true) fetch fails; the
    // retried request keeps the clamp, so a success whose browser month
    // precedes earliest_bookable_day still jumps ONCE. Bounds are 40+
    // days out so the month boundaries are date-independent.
    const earliestIso = isoAdd(todayIso(), 40);
    const latestIso = isoAdd(todayIso(), 80);
    const sb = buildSandbox();
    let failFirst = true;
    sb.setPreviewResponder((url) => {
      if (failFirst) {
        failFirst = false;
        return Promise.reject(new Error("offline"));
      }
      return successfulJson(previewPayloadFor(url, {
        earliest: earliestIso, latest: latestIso,
      }));
    });
    await openPicker(sb, null);
    let parts = panelParts(pickerPanel(sb.elementsById));
    ok("earlier-direction: initial failure shows the visible Retry",
      parts.retries.length === 1);
    parts.retries[0].click();
    await flush(); await flush(); await flush(); await flush();
    const previews = previewFetches(sb.fetchCalls);
    parts = panelParts(pickerPanel(sb.elementsById));
    const ep = earliestIso.split("-").map(Number);
    ok("earlier-direction: the RETRY retains the clamp — exactly one " +
       "bounded jump to the earliest allowed month, no loop",
      previews.length === 3 &&
      new URL(previews[2].url).searchParams.get("start_day")
        .startsWith(earliestIso.slice(0, 7)) &&
      parts.title !== null &&
      parts.title.textContent === PICKER_MONTH_TITLE(ep[0], ep[1] - 1) &&
      parts.retries.length === 0);
    ok("earlier-direction: post-retry navigation reflects the server bounds",
      parts.navs.length === 2 &&
      parts.navs[0].disabled === true /* prev clamped at earliest */ &&
      parts.navs[1].disabled === false /* next open toward latest */);
  }
  {
    // (B) LATER direction: same retained-clamp rule with the browser
    // month AFTER latest_bookable_day.
    const earliestIso = isoAdd(todayIso(), -80);
    const latestIso = isoAdd(todayIso(), -40);
    const sb = buildSandbox();
    let failFirst = true;
    sb.setPreviewResponder((url) => {
      if (failFirst) {
        failFirst = false;
        return Promise.reject(new Error("offline"));
      }
      return successfulJson(previewPayloadFor(url, {
        earliest: earliestIso, latest: latestIso,
      }));
    });
    await openPicker(sb, null);
    let parts = panelParts(pickerPanel(sb.elementsById));
    ok("later-direction: initial failure shows the visible Retry",
      parts.retries.length === 1);
    parts.retries[0].click();
    await flush(); await flush(); await flush(); await flush();
    const previews = previewFetches(sb.fetchCalls);
    parts = panelParts(pickerPanel(sb.elementsById));
    const lp = latestIso.split("-").map(Number);
    ok("later-direction: the RETRY retains the clamp — exactly one " +
       "bounded jump to the latest allowed month, no loop, and the " +
       "navigation reflects the server bounds",
      previews.length === 3 &&
      new URL(previews[2].url).searchParams.get("start_day")
        .startsWith(latestIso.slice(0, 7)) &&
      parts.title !== null &&
      parts.title.textContent === PICKER_MONTH_TITLE(lp[0], lp[1] - 1) &&
      parts.retries.length === 0 &&
      parts.navs.length === 2 &&
      parts.navs[1].disabled === true /* next clamped at latest */ &&
      parts.navs[0].disabled === false /* prev open toward earliest */);
  }
  {
    // (C) A post-jump retry re-issues the SAME month as a fresh bounded
    // attempt; with unchanged in-bounds server bounds it renders that
    // month without any further jump.
    const earliestIso = isoAdd(todayIso(), -80);
    const latestIso = isoAdd(todayIso(), -40);
    const sb = buildSandbox();
    let call = 0;
    sb.setPreviewResponder((url) => {
      call += 1;
      if (call === 2) return Promise.reject(new Error("offline"));
      return successfulJson(previewPayloadFor(url, {
        earliest: earliestIso, latest: latestIso,
      }));
    });
    await openPicker(sb, null);
    await flush(); await flush();
    let parts = panelParts(pickerPanel(sb.elementsById));
    ok("post-jump failure shows the visible Retry",
      parts.retries.length === 1);
    parts.retries[0].click();
    await flush(); await flush(); await flush(); await flush();
    const previews = previewFetches(sb.fetchCalls);
    parts = panelParts(pickerPanel(sb.elementsById));
    const lp = latestIso.split("-").map(Number);
    ok("a post-jump retry re-issues the SAME month and renders it " +
       "without another jump when it lies inside the returned bounds",
      previews.length === 3 &&
      new URL(previews[1].url).searchParams.get("start_day") ===
        new URL(previews[2].url).searchParams.get("start_day") &&
      parts.title !== null &&
      parts.title.textContent === PICKER_MONTH_TITLE(lp[0], lp[1] - 1) &&
      parts.retries.length === 0);
  }
  {
    // Static: the explicit clamp budget replaces the allowJump Boolean.
    // (EOL-normalized so the pins hold on CRLF Windows checkouts.)
    const html = fs.readFileSync(CHAT_HTML, "utf8").replace(/\r\n/g, "\n");
    const pickerBlock = html.slice(html.indexOf("function renderDatePicker"),
                                   html.indexOf("function renderPickerGrid"));
    ok("static: the ambiguous allowJump Boolean is gone; named budget " +
       "constants exist",
      !/allowJump/.test(html) &&
      html.includes("const PICKER_PATIENT_JUMP_BUDGET = 1;") &&
      html.includes("const PICKER_NO_JUMP_BUDGET = 0;"));
    ok("static: the Retry handler starts a fresh patient-initiated " +
       "attempt with its own single-jump budget",
      pickerBlock.includes("fetchPickerMonth(panel, year, monthIndex,\n" +
        "                           PICKER_PATIENT_JUMP_BUDGET)"));
    ok("static: both internal jump requests carry ZERO remaining budget",
      (html.match(/PICKER_NO_JUMP_BUDGET\n        \);/g) || []).length === 2);
  }

  // ------------------------------------------------------------------
  // V7 (owner audit) — changed bounds after the internal clamp must
  // fail visibly; Retry begins a new bounded attempt; navigation
  // failures retry cleanly; superseded responses render nothing.
  // ------------------------------------------------------------------
  {
    // OWNER PROBE SHAPE: initial failure (1) -> Retry (2, out-of-window,
    // one clamp) -> internal jump (3) whose CHANGED nonempty bounds
    // exclude the target: exactly three requests before patient
    // intervention; no second automatic jump; no title or date grid;
    // visible failure + Retry; typed input usable. Then the Retry
    // begins a new bounded attempt that clamps ONCE to the new window.
    const earliest1 = isoAdd(todayIso(), -80);
    const latest1 = isoAdd(todayIso(), -40);
    const earliest2 = isoAdd(todayIso(), -200);
    const latest2 = isoAdd(todayIso(), -160);
    const sb = buildSandbox();
    let call = 0;
    sb.setPreviewResponder((url) => {
      call += 1;
      if (call === 1) return Promise.reject(new Error("offline"));
      if (call === 2) {
        return successfulJson(previewPayloadFor(url, {
          earliest: earliest1, latest: latest1,
        }));
      }
      // From the internal jump onward the server reports the NEW window.
      return successfulJson(previewPayloadFor(url, {
        earliest: earliest2, latest: latest2,
      }));
    });
    await openPicker(sb, null);
    let parts = panelParts(pickerPanel(sb.elementsById));
    ok("owner probe: initial failure shows the visible Retry",
      parts.retries.length === 1);
    parts.retries[0].click();
    await flush(); await flush(); await flush(); await flush(); await flush();
    let previews = previewFetches(sb.fetchCalls);
    parts = panelParts(pickerPanel(sb.elementsById));
    ok("owner probe: changed bounds after the internal clamp fail " +
       "visibly with Retry — exactly three requests before patient " +
       "intervention, no second automatic jump, no title, no date grid",
      previews.length === 3 &&
      new URL(previews[2].url).searchParams.get("start_day")
        .startsWith(latest1.slice(0, 7)) &&
      parts.title === null &&
      parts.days.length === 0 &&
      parts.retries.length === 1 &&
      parts.statuses.some((s) => /couldn’t load/.test(s.textContent)) &&
      sb.elementsById.input.disabled === false);
    // Patient intervention: the Retry is a NEW bounded attempt — one
    // clamp into the new window, then the in-bounds grid renders.
    // (Guarded so a defective widget fails the NEXT assertion cleanly
    // instead of crashing the harness.)
    if (parts.retries.length === 1) parts.retries[0].click();
    await flush(); await flush(); await flush(); await flush(); await flush();
    previews = previewFetches(sb.fetchCalls);
    parts = panelParts(pickerPanel(sb.elementsById));
    const l2 = latest2.split("-").map(Number);
    ok("owner probe: clicking Retry begins a new bounded attempt — one " +
       "clamp into the new window, the in-bounds grid renders, and no " +
       "automatic loop follows",
      previews.length === 5 &&
      new URL(previews[4].url).searchParams.get("start_day")
        .startsWith(latest2.slice(0, 7)) &&
      parts.title !== null &&
      parts.title.textContent === PICKER_MONTH_TITLE(l2[0], l2[1] - 1) &&
      parts.retries.length === 0);
  }
  {
    // DIRECT changed-bounds probe (no initial failure): request (1)
    // clamps, the internal jump (2) returns bounds excluding its own
    // target -> visible failure, nothing rendered, no third request.
    const earliest1 = isoAdd(todayIso(), -80);
    const latest1 = isoAdd(todayIso(), -40);
    const sb = buildSandbox();
    let call = 0;
    sb.setPreviewResponder((url) => {
      call += 1;
      if (call === 1) {
        return successfulJson(previewPayloadFor(url, {
          earliest: earliest1, latest: latest1,
        }));
      }
      return successfulJson(previewPayloadFor(url, {
        earliest: isoAdd(todayIso(), -200), latest: isoAdd(todayIso(), -160),
      }));
    });
    await openPicker(sb, null);
    await flush(); await flush();
    const previews = previewFetches(sb.fetchCalls);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("zero-budget response outside its own returned bounds renders " +
       "nothing and fails visibly (two requests total)",
      previews.length === 2 &&
      parts.title === null &&
      parts.days.length === 0 &&
      parts.retries.length === 1);
  }
  {
    // IN-BOUNDS NAVIGATION failure followed by Retry: the requested
    // month renders normally with no unrelated jump.
    const sb = buildSandbox();
    let call = 0;
    sb.setPreviewResponder((url) => {
      call += 1;
      if (call === 2) return Promise.reject(new Error("offline"));
      return successfulJson(previewPayloadFor(url, {
        earliest: todayIso(), latest: isoAdd(todayIso(), 40),
      }));
    });
    await openPicker(sb, null);
    let parts = panelParts(pickerPanel(sb.elementsById));
    ok("navigation setup: the current month rendered with next enabled",
      parts.title !== null && parts.navs.length === 2 &&
      parts.navs[1].disabled === false);
    parts.navs[1].click();
    await flush(); await flush();
    parts = panelParts(pickerPanel(sb.elementsById));
    ok("navigation failure shows the visible Retry",
      parts.retries.length === 1);
    parts.retries[0].click();
    await flush(); await flush(); await flush();
    const previews = previewFetches(sb.fetchCalls);
    parts = panelParts(pickerPanel(sb.elementsById));
    const now = new Date();
    const nk = now.getFullYear() * 12 + now.getMonth() + 1;
    ok("in-bounds navigation Retry renders the requested month with no " +
       "unrelated jump",
      previews.length === 3 &&
      new URL(previews[1].url).searchParams.get("start_day") ===
        new URL(previews[2].url).searchParams.get("start_day") &&
      parts.title !== null &&
      parts.title.textContent ===
        PICKER_MONTH_TITLE(Math.floor(nk / 12), nk % 12) &&
      parts.retries.length === 0);
  }
  {
    // SUPERSEDED FAILURE SUPPRESSION: a stale request that FAILS after a
    // newer request already rendered can produce neither a failure
    // status, a Retry, nor a grid replacement.
    const handlers = [];
    const sb = buildSandbox();
    sb.setPreviewResponder((url) => new Promise((resolve, reject) => {
      handlers.push({ url, resolve, reject });
    }));
    sb.setChatResponder(() => successfulJson({
      reply: "What day?", conversation_id: "conv-1",
      meta: { calendar_picker: { stage: "date" } },
    }));
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush();
    run(sb.context, 'inputEl.value = "book again";');
    await run(sb.context, "sendMessage()");
    await flush();
    ok("superseded-failure setup: two preview requests are pending",
      handlers.length === 2);
    handlers[1].resolve({
      ok: true, status: 200,
      json: () => Promise.resolve(
        previewPayloadFor(handlers[1].url, { openDays: [openIso] })),
    });
    await flush(); await flush();
    handlers[0].reject(new Error("stale offline"));
    await flush(); await flush();
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("a superseded request's FAILURE renders neither a failure state, " +
       "a Retry, nor a grid replacement",
      pickerRows(sb.elementsById).length === 1 &&
      parts.openDays.length === 1 &&
      parts.retries.length === 0 &&
      !parts.statuses.some((s) => /couldn’t load/.test(s.textContent)));
  }

  // ------------------------------------------------------------------
  // V8 (owner audit) — targeted visibility preservation: sendMessage's
  // synchronous echo + typing-indicator scrolling must not leave the
  // selected date cell outside the visible #messages viewport. The
  // REAL geometric proof is the package's Chromium layout probe
  // (probes/layout_probe_date_picker.mjs); these mock assertions prove
  // only the call contract of the bounded one-frame restoration.
  // ------------------------------------------------------------------
  {
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    ok("V8 setup: no visibility-restoration is scheduled by opening the " +
       "picker or by ordinary messages (date-action-specific ownership)",
      sb.rafQueue.length === 0);
    let releaseAction = null;
    sb.setChatResponder(() => new Promise((resolve) => {
      releaseAction = () => resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
          reply: "Got it \u2014 morning or afternoon?",
          conversation_id: "conv-1",
          meta: { mode: "booking", state: "waiting_for_time_preference" },
        }),
      });
    }));
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush();
    const liveRows = pickerRows(sb.elementsById);
    const liveSelected = panelParts(panelOf(liveRows[0])).days.filter(
      (b) => b.classList.contains("dp-selected"));
    ok("V8: selecting a date schedules exactly ONE bounded " +
       "visibility-restoration frame and calls nothing synchronously",
      sb.rafQueue.length === 1 &&
      liveSelected.length === 1 &&
      liveSelected[0].scrollIntoViewCalls.length === 0);
    sb.flushRaf();
    ok("V8: the restoration frame scrolls the LIVE selected button into " +
       "view with block:'nearest' (minimal, no viewport fighting) while " +
       "the row stays attached and controls stay disabled",
      liveSelected[0].scrollIntoViewCalls.length === 1 &&
      liveSelected[0].scrollIntoViewCalls[0] !== null &&
      liveSelected[0].scrollIntoViewCalls[0].block === "nearest" &&
      pickerRows(sb.elementsById).length === 1 &&
      liveSelected[0].attributes["aria-pressed"] === "true" &&
      panelParts(panelOf(liveRows[0])).days.every(
        (b) => b.disabled === true));
    ok("V8: one frame only — no further restoration is queued afterwards",
      sb.rafQueue.length === 0);
    releaseAction();
    await flush(); await flush();
    ok("V8: the resolved boundary still clears the picker normally",
      pickerRows(sb.elementsById).length === 0);
  }
  {
    // Start Over while the date action is unresolved: the picker (and
    // its selected button) is removed immediately, so the pending
    // restoration frame finds a DETACHED button and must NOT scroll.
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    sb.setChatResponder(() => new Promise(() => {}));
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush();
    const heldSelected = panelParts(
      panelOf(pickerRows(sb.elementsById)[0])).days.filter(
      (b) => b.classList.contains("dp-selected"));
    run(sb.context, "startOver()");
    ok("V8: Start Over removes the submitting picker immediately",
      pickerRows(sb.elementsById).length === 0 &&
      heldSelected[0].isConnected === false);
    sb.flushRaf();
    ok("V8: the pending restoration frame skips the detached button " +
       "(no scroll after Start Over)",
      heldSelected[0].scrollIntoViewCalls.length === 0);
  }
  {
    // Static pin (EOL-normalized): the restoration lives in pickDate,
    // one bounded frame, connectivity-guarded, block:'nearest'.
    const html = fs.readFileSync(CHAT_HTML, "utf8").replace(/\r\n/g, "\n");
    const pickBlock = html.slice(html.indexOf("function pickDate"),
                                 html.indexOf("function resetInputPlaceholder"));
    ok("static: pickDate owns a single requestAnimationFrame " +
       "visibility restoration guarded by isConnected using " +
       "scrollIntoView block:'nearest'",
      (pickBlock.match(/requestAnimationFrame\(/g) || []).length === 1 &&
      pickBlock.includes("if (btn.isConnected) {") &&
      pickBlock.includes('btn.scrollIntoView({ block: "nearest" });'));
  }

  // ------------------------------------------------------------------
  // V3 defect 1 — Start Over invalidates every in-flight POST /chat.
  // Real held-promise tests; every assertion reads live widget state.
  // ------------------------------------------------------------------
  {
    // (1-4) Old SUCCESS resolving after Start Over: zero side effects.
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    let releaseOld = null;
    sb.setChatResponder(() => new Promise((resolve) => {
      releaseOld = () => resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
          reply: "OLD STALE REPLY",
          conversation_id: "old-conv-999",
          meta: { mode: "booking", state: "waiting_for_date",
                  calendar_picker: { stage: "date" } },
        }),
      });
    }));
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush();
    run(sb.context, "startOver()");
    await flush();
    const messagesAfterReset = sb.elementsById.messages.children.length;
    releaseOld();
    await flush(); await flush(); await flush();
    const texts = sb.elementsById.messages.children.map((c) => c.textContent);
    ok("OWNER-STYLE: old success cannot revive the abandoned conversation",
      run(sb.context, "conversationId") === null);
    // Precise storage assertion: the old ID never landed in storage.
    ok("old conversation_id is not written to localStorage after reset",
      run(sb.context,
        'localStorage.getItem("chat_conversation_id_test-client")') === null);
    ok("old bot reply is not displayed after reset",
      texts.indexOf("OLD STALE REPLY") === -1);
    ok("no old picker is rendered and no dp-submitting row survives",
      pickerRows(sb.elementsById).length === 0);
    ok("messages stay at the fresh-conversation state after the old resolve",
      sb.elementsById.messages.children.length === messagesAfterReset);
    // Start Over's own opening animation briefly disables the input by
    // design (setInputReady); wait it out, then prove the input is usable
    // and the abandoned action left no lock behind.
    await new Promise((resolve) => setTimeout(resolve, 1200));
    ok("typed input remains usable for the new conversation",
      sb.elementsById.input.disabled === false &&
      run(sb.context, "structuredActionInFlight") === false);
  }
  {
    // (5) Old 409 failure resolving after Start Over: no stale wording.
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    let releaseOld = null;
    sb.setChatResponder(() => new Promise((resolve) => {
      releaseOld = () => resolve({
        ok: false, status: 409,
        json: () => Promise.resolve({ detail: {
          code: "STALE_CHOICE", message: "OLD STALE FAILURE" } }),
      });
    }));
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush();
    run(sb.context, "startOver()");
    await flush();
    releaseOld();
    await flush(); await flush(); await flush();
    const texts = sb.elementsById.messages.children.map((c) => c.textContent);
    ok("old 409 failure after Start Over displays no stale message",
      texts.indexOf("OLD STALE FAILURE") === -1 &&
      pickerRows(sb.elementsById).length === 0);
  }
  {
    // (5b) Old NETWORK failure resolving after Start Over: silent.
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    let rejectOld = null;
    sb.setChatResponder(() => new Promise((resolve, reject) => {
      rejectOld = () => reject(new Error("offline"));
    }));
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush();
    run(sb.context, "startOver()");
    await flush();
    rejectOld();
    await flush(); await flush(); await flush();
    const texts = sb.elementsById.messages.children.map((c) => c.textContent);
    ok("old network failure after Start Over displays no stale error",
      texts.every((t) => !/couldn’t reach the chat service/.test(String(t))));
  }
  {
    // (6-9) Old-vs-new structured-action lock race.
    const sb = buildSandbox();
    await openPicker(sb, [openIso]);
    const resolvers = [];
    sb.setChatResponder(() => new Promise((resolve) => {
      resolvers.push((payload) => resolve({
        ok: true, status: 200,
        json: () => Promise.resolve(payload),
      }));
    }));
    // Old action begins, then is abandoned by Start Over.
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush();
    run(sb.context, "startOver()");
    await flush();
    ok("a newer structured action can begin after reset",
      run(sb.context, "structuredActionInFlight") === false);
    // Newer conversation: render a quick-reply action and submit it.
    run(sb.context,
      'conversationId = "conv-new";' +
      'renderQuickReplies([{label:"Tue 10:00 AM",message:"Tue 10:00 AM",' +
      'action:{type:"calendar_choice",choice_id:"new-choice"}}]);');
    const replies = sb.elementsById.messages.children.filter(
      (c) => c.className === "quick-replies");
    replies[0].children[0].click();
    await flush();
    ok("newer action is in flight with its own lock",
      run(sb.context, "structuredActionInFlight") === true &&
      resolvers.length === 2);
    // Resolve the OLDER (abandoned) request first.
    resolvers[0]({ reply: "OLD", conversation_id: "old-conv",
                   meta: {} });
    await flush(); await flush();
    ok("resolving the older request does not clear the newer action's lock",
      run(sb.context, "structuredActionInFlight") === true);
    // A duplicate click on the newer pending action stays blocked.
    const postsBefore = chatPosts(sb.fetchCalls).length;
    replies.length && sb.elementsById.messages.children
      .filter((c) => c.className === "quick-replies")
      .forEach(() => {});
    run(sb.context, 'inputEl.value = "dup";');
    await run(sb.context,
      'sendMessage({type:"calendar_choice",choice_id:"dup-choice"})');
    ok("duplicate submission on the newer pending action remains blocked",
      chatPosts(sb.fetchCalls).length === postsBefore);
    // Only the CURRENT action's own resolution clears its lock.
    resolvers[1]({ reply: "New booked.", conversation_id: "conv-new",
                   meta: {} });
    await flush(); await flush();
    const texts = sb.elementsById.messages.children.map((c) => c.textContent);
    ok("only the active action's resolution clears its own lock",
      run(sb.context, "structuredActionInFlight") === false &&
      texts.indexOf("New booked.") !== -1 &&
      texts.indexOf("OLD") === -1 &&
      run(sb.context, "conversationId") === "conv-new");
  }

  // ------------------------------------------------------------------
  // V3 defect 2 — weekday must match local_date canonically.
  // ------------------------------------------------------------------
  await expectPreviewFailure(
    "wrong weekday on an open date fails visibly",
    { preview: { mutateDays: (d) => {
        d[6].state = "open";
        d[6].weekday = d[6].weekday === "Monday" ? "Tuesday" : "Monday"; } } });
  await expectPreviewFailure(
    "wrong weekday on a locked date fails visibly",
    { preview: { mutateDays: (d) => {
        d[2].weekday = d[2].weekday === "Friday" ? "Saturday" : "Friday"; } } });
  await expectPreviewFailure(
    "blank weekday remains rejected",
    { preview: { mutateDays: (d) => { d[4].weekday = ""; } } });

  // ------------------------------------------------------------------
  // V3 defect 3 — open dates must respect the declared bounds.
  // ------------------------------------------------------------------
  await expectPreviewFailure(
    "open date before earliest_bookable_day fails closed",
    { preview: {
        earliest: isoAdd(todayIso(), 20), latest: isoAdd(todayIso(), 40),
        mutateDays: (d) => { d[1].state = "open"; } } });
  await expectPreviewFailure(
    "open date after latest_bookable_day fails closed",
    { preview: {
        earliest: isoAdd(todayIso(), -40), latest: isoAdd(todayIso(), -30),
        mutateDays: (d) => { d[3].state = "open"; } } });
  await expectPreviewFailure(
    "open date in a declared-empty window fails closed",
    { preview: {
        earliest: isoAdd(todayIso(), 10), latest: isoAdd(todayIso(), 5),
        mutateDays: (d) => { d[2].state = "open"; } } });
  {
    // Locked dates OUTSIDE the bounds are legitimate and still render.
    const sb = buildSandbox({
      preview: { earliest: isoAdd(todayIso(), 3), latest: isoAdd(todayIso(), 4) },
    });
    await openPicker(sb, null);
    const parts = panelParts(pickerPanel(sb.elementsById));
    ok("valid locked dates outside the bounds still render as locked",
      parts.days.length > 0 &&
      parts.lockedDays.length === parts.days.length &&
      parts.retries.length === 0);
  }

  // ------------------------------------------------------------------------
  // Capture-first ordinary-message submission mode
  // (meta.calendar_picker.submit === "message"): the date card submits the
  // picked date as an ordinary intake message with NO action object. The
  // native picker (no marker, or any non-"message" value) keeps the pick-date
  // calendar_choice action exactly as before.
  // ------------------------------------------------------------------------
  const CF_MONTHS = ["January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"];

  async function openCaptureFirst(sb, openDays, submit) {
    sb.setPreviewResponder((url) =>
      successfulJson(previewPayloadFor(url, { openDays })));
    let turn = 0;
    sb.setChatResponder(() => {
      turn += 1;
      if (turn === 1) {
        const picker = (submit === undefined)
          ? { stage: "date" }
          : { stage: "date", submit: submit };
        return successfulJson({
          reply: "Great\u2014thanks Kevin. What day/time window works best (e.g., Tue morning)?",
          conversation_id: "conv-1",
          meta: { mode: "bypass", show_start_over: true, calendar_picker: picker },
        });
      }
      return successfulJson({
        reply: "Got it \u2014 do you prefer morning or afternoon?",
        conversation_id: "conv-1",
        meta: { mode: "intake_time_window_capture",
                calendar_picker: { stage: "time_preference" } },
      });
    });
    run(sb.context, 'inputEl.value = "book";');
    await run(sb.context, "sendMessage()");
    await flush(); await flush();
  }

  function cfExpectedMessage(openIso) {
    const [y, mo, d] = openIso.split("-").map(Number);
    return `${weekdayOf(openIso)}, ${CF_MONTHS[mo - 1]} ${d}, ${y}`;
  }

  { // message mode: ordinary message, no action object, full month-name date
    const openIso = isoAdd(todayIso(), 2);
    const sb = buildSandbox({});
    await openCaptureFirst(sb, [openIso], "message");
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush(); await flush();
    const posts = chatPosts(sb.fetchCalls);
    const body = parsedBody(posts[posts.length - 1]);
    ok("capture-first date click posts exactly once with NO action object",
      posts.length === 2 && !("action" in body));
    ok("capture-first message is the full month-name date (never a raw ISO)",
      body.message === cfExpectedMessage(openIso) &&
      !/\d{4}-\d{2}-\d{2}/.test(String(body.message)));
  }

  { // fail-closed: any non-"message" marker keeps the native calendar_choice
    const openIso = isoAdd(todayIso(), 2);
    for (const bad of ["action", "true", "MESSAGE", ""]) {
      const sb = buildSandbox({});
      await openCaptureFirst(sb, [openIso], bad);
      panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
      await flush(); await flush();
      const body = parsedBody(chatPosts(sb.fetchCalls).slice(-1)[0]);
      ok(`submit marker ${JSON.stringify(bad)} falls closed to calendar_choice`,
        !!body.action && body.action.type === "calendar_choice" &&
        body.action.choice_id === "pick-date:" + openIso);
    }
  }

  { // native picker (no submit marker) is unchanged: pick-date calendar_choice
    const openIso = isoAdd(todayIso(), 2);
    const sb = buildSandbox({});
    await openCaptureFirst(sb, [openIso], undefined);
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush(); await flush();
    const body = parsedBody(chatPosts(sb.fetchCalls).slice(-1)[0]);
    ok("native date picker (no marker) keeps the pick-date calendar_choice",
      !!body.action && body.action.type === "calendar_choice" &&
      body.action.choice_id === "pick-date:" + openIso);
  }

  { // message-mode lifecycle: card retained + disabled in flight; no duplicate
    const openIso = isoAdd(todayIso(), 2);
    const sb = buildSandbox({});
    await openCaptureFirst(sb, [openIso], "message");
    let release = null;
    sb.setChatResponder(() => new Promise((resolve) => {
      release = () => resolve(successfulJson({
        reply: "Got it \u2014 do you prefer morning or afternoon?",
        conversation_id: "conv-1",
        meta: { mode: "intake_time_window_capture",
                calendar_picker: { stage: "time_preference" } } }));
    }));
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush();
    const liveRows = pickerRows(sb.elementsById);
    const liveParts = panelParts(panelOf(liveRows[0]));
    const selected = liveParts.days.filter((b) =>
      b.classList.contains("dp-selected"));
    ok("message-mode: submitting card retained, selected, all controls disabled",
      liveRows.length === 1 &&
      liveRows[0].classList.contains("dp-submitting") &&
      selected.length === 1 &&
      liveParts.days.every((b) => b.disabled === true) &&
      liveParts.navs.every((b) => b.disabled === true));
    const before = chatPosts(sb.fetchCalls).length;
    selected[0].click();  // duplicate via the LIVE attached button
    await flush();
    ok("message-mode: duplicate click while in flight adds no second POST",
      chatPosts(sb.fetchCalls).length === before);
    release();
    await flush(); await flush();
    ok("message-mode: the card clears only at the resolved response boundary",
      pickerRows(sb.elementsById).length === 0);
  }

  function messagesText(sb) {
    const out = [];
    collect(sb.elementsById.messages, out);
    return out.map((e) => String(e.textContent || "")).join(" | ");
  }

  { // fail-closed for NON-STRING markers: null, boolean, array, object
    const openIso = isoAdd(todayIso(), 2);
    const cases = [
      ["null", null], ["boolean true", true], ["boolean false", false],
      ["array", []], ["object", {}],
    ];
    for (const [label, marker] of cases) {
      const sb = buildSandbox({});
      await openCaptureFirst(sb, [openIso], marker);
      panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
      await flush(); await flush();
      const body = parsedBody(chatPosts(sb.fetchCalls).slice(-1)[0]);
      ok(`submit marker ${label} falls closed to calendar_choice`,
        !!body.action && body.action.type === "calendar_choice" &&
        body.action.choice_id === "pick-date:" + openIso);
    }
  }

  { // Start Over while the ordinary-message POST is unresolved
    const openIso = isoAdd(todayIso(), 2);
    const sb = buildSandbox({});
    await openCaptureFirst(sb, [openIso], "message");
    let release = null;
    sb.setChatResponder(() => new Promise((resolve) => {
      release = () => resolve(successfulJson({
        reply: "STALE_LATE_REPLY",
        conversation_id: "conv-1",
        meta: { calendar_picker: { stage: "date", submit: "message" } } }));
    }));
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush();
    ok("message-mode: card present while the POST is unresolved",
      pickerRows(sb.elementsById).length === 1);
    run(sb.context, "startOver()");
    ok("Start Over removes the in-flight capture-first picker",
      pickerRows(sb.elementsById).length === 0);
    const textBefore = messagesText(sb);
    release();
    await flush(); await flush();
    ok("late response after Start Over restores neither the picker nor the reply",
      pickerRows(sb.elementsById).length === 0 &&
      !messagesText(sb).includes("STALE_LATE_REPLY") &&
      messagesText(sb) === textBefore);
  }

  { // ordinary-message network failure: visible + recoverable
    const openIso = isoAdd(todayIso(), 2);
    const sb = buildSandbox({});
    await openCaptureFirst(sb, [openIso], "message");
    sb.setChatResponder(() => Promise.reject(new Error("offline")));
    panelParts(pickerPanel(sb.elementsById)).openDays[0].click();
    await flush(); await flush();
    ok("message-mode network failure surfaces a visible connection error",
      messagesText(sb).toLowerCase().includes("trouble connecting"));
    ok("message-mode network failure clears the card and leaves input usable",
      pickerRows(sb.elementsById).length === 0 &&
      run(sb.context, "!!inputEl.disabled") === false);
  }

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
