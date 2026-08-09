// tests/test_widget_slot_pagination.js
//
// UX-A - slot-page navigation chips in the patient widget, frontend
// proofs. ADDITIVE harness: tests/test_widget_time_stage.js (42) is
// carried byte-identical and keeps owning the base slot-panel contract.
//
// Executes the REAL inline script from static/chat.html in a Node `vm`
// sandbox (loader below mirrored verbatim from
// tests/test_widget_time_stage.js, the established per-file pattern).
// Proves: the server-issued "See later times" / "Back to earlier times"
// entries render as ordinary slot-panel chips in SERVER ORDER; a nav tap
// submits ONE calendar_choice POST carrying the fixed literal id
// (slots-later / slots-earlier) with the FULL label as the message
// (never shortened to "Back"); the retained sp-submitting lifecycle,
// whole-row disable sweep, and double-click guard cover nav chips
// exactly like slot chips; the authoritative response boundary swaps the
// panel to the next page; Start Over sweeps a nav-submitting panel and
// the abandoned epoch restores nothing; a recognized 409 STALE_CHOICE
// replacement that carries nav entries re-renders them; and a panel
// without nav entries renders zero extra chips (server-driven only).
//
// Run:
//   node tests/test_widget_slot_pagination.js
// or:
//   MIA_CHAT_HTML=/path/to/static/chat.html node tests/test_widget_slot_pagination.js

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CHAT_HTML = process.env.MIA_CHAT_HTML ||
  path.join(__dirname, "..", "static", "chat.html");

// ---------------------------------------------------------------------------
// Minimal DOM stub (mirrors tests/test_widget_date_picker.js).
// ---------------------------------------------------------------------------

function makeClassList(element) {
  const values = new Set();
  return {
    add: (...items) => items.forEach((item) => values.add(item)),
    remove: (...items) => items.forEach((item) => values.delete(item)),
    toggle: (item, force) => {
      const wanted = force === undefined ? !values.has(item) : !!force;
      if (wanted) values.add(item); else values.delete(item);
      return wanted;
    },
    contains: (item) =>
      values.has(item) ||
      String(element.className).split(/\s+/).includes(item),
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
    className: "",
  };
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
  element.classList = makeClassList(element);
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
    element.attributes[name] = String(value);
  };
  element.focus = () => {};
  element.querySelectorAll = () => [];
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

function failedJson(status, payload) {
  return Promise.resolve({
    ok: false,
    status: status,
    json: () => Promise.resolve(payload),
  });
}

function buildSandbox(options = {}) {
  const elementsById = {};
  const body = makeElement("body");

  ["messages", "input", "sendBtn", "miaHeaderTitle", "miaHeaderSubtitle",
   "main-menu", "service-menu", "consentModal", "agreeBtn"].forEach((id) => {
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
        classes.some((name) => element.classList.contains(name))
      );
    },
  };

  const storage = {};
  const fetchCalls = [];
  const rafQueue = [];
  let chatResponder = options.chatResponder || (() => successfulJson({
    reply: "Okay.",
    conversation_id: "conv-1",
    meta: {},
  }));
  let previewResponder = options.previewResponder || (() => successfulJson({
    timezone: "America/New_York",
    requested_start_day: "2099-01-01",
    requested_end_day: "2099-01-31",
    earliest_bookable_day: "2099-01-02",
    latest_bookable_day: "2099-01-30",
    days: [],
  }));

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
      matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    },
    localStorage: {
      getItem: (key) => (key in storage ? storage[key] : null),
      setItem: (key, value) => { storage[key] = String(value); },
      removeItem: (key) => { delete storage[key]; },
    },
    URLSearchParams,
    URL,
    Date,
    setTimeout,
    clearTimeout,
    fetch: (url, requestOptions) => {
      fetchCalls.push({ url, options: requestOptions || {} });
      if (String(url).includes("/chat/config")) return successfulJson({});
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
  vm.runInContext(match[1], context, { filename: "static/chat.html<script>" });

  return {
    context,
    body,
    elementsById,
    fetchCalls,
    flushRaf: () => {
      rafQueue.splice(0, rafQueue.length).forEach((cb) => cb());
    },
    setChatResponder: (responder) => { chatResponder = responder; },
  };
}

function run(context, source) {
  return vm.runInContext(source, context);
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function rowsOf(sb, className) {
  return sb.elementsById.messages.children.filter((child) =>
    child.classList.contains(className));
}

function buttonsOf(row) {
  return collect(row, []).filter((el) => el.tagName === "BUTTON");
}

function chatPosts(sb) {
  return sb.fetchCalls.filter((call) =>
    String(call.url).endsWith("/chat") &&
    call.options && call.options.method === "POST");
}

function parsedBody(call) {
  return JSON.parse(call.options.body);
}

function botMessages(sb) {
  return sb.elementsById.messages.children.filter((child) =>
    String(child.className).indexOf("msg bot") === 0);
}

const SLOT_ACTIONS = [
  { label: "10:00 AM", message: "10:00 AM",
    action: { type: "calendar_choice",
              choice_id: "11111111-1111-4111-8111-111111111111" } },
  { label: "1:30 PM", message: "1:30 PM",
    action: { type: "calendar_choice",
              choice_id: "22222222-2222-4222-8222-222222222222" } },
];

function stageReply(stage, extraMeta) {
  const meta = Object.assign({}, extraMeta || {});
  if (stage) meta.calendar_picker = { stage: stage };
  return { reply: "Server reply.", conversation_id: "conv-1", meta: meta };
}

async function typeAndSend(sb, text) {
  run(sb.context, `inputEl.value = ${JSON.stringify(text)};`);
  const promise = run(sb.context, "sendMessage()");
  await Promise.resolve(promise);
  await flush();
}

async function openPreferenceRow(sb) {
  sb.setChatResponder(() => successfulJson(stageReply("time_preference")));
  await typeAndSend(sb, "tomorrow");
  return rowsOf(sb, "time-pref-row");
}

async function openSlotPanel(sb) {
  sb.setChatResponder(() => successfulJson(
    stageReply("slot_selection", { calendar_actions: SLOT_ACTIONS })));
  await typeAndSend(sb, "afternoon");
  return rowsOf(sb, "slot-panel-row");
}

let passed = 0;
let failed = 0;

function check(name, condition) {
  if (condition) {
    passed += 1;
    console.log(`  PASS  ${name}`);
  } else {
    failed += 1;
    console.log(`  FAIL  ${name}`);
  }
}

const LATER_NAV = {
  label: "See later times", message: "See later times",
  action: { type: "calendar_choice", choice_id: "slots-later" } };
const EARLIER_NAV = {
  label: "Back to earlier times", message: "Back to earlier times",
  action: { type: "calendar_choice", choice_id: "slots-earlier" } };
const PAGE2_ACTIONS = [
  { label: "3:00 PM", message: "3:00 PM",
    action: { type: "calendar_choice",
              choice_id: "33333333-3333-4333-8333-333333333333" } },
  { label: "4:00 PM", message: "4:00 PM",
    action: { type: "calendar_choice",
              choice_id: "44444444-4444-4444-8444-444444444444" } },
  EARLIER_NAV,
];

async function openPanelWith(sb, actions) {
  sb.setChatResponder(() => successfulJson(
    stageReply("slot_selection", { calendar_actions: actions })));
  await typeAndSend(sb, "tomorrow");
  return rowsOf(sb, "slot-panel-row");
}

function labelsOf(row) {
  return buttonsOf(row).map((b) => b.textContent).join("|");
}

async function main() {
  // =========================================================================
  console.log("- first page: See later times chip -");
  // =========================================================================
  {
    const sb = buildSandbox();
    const rows = await openPanelWith(sb, SLOT_ACTIONS.concat([LATER_NAV]));
    check("G1 first-page panel renders slot chips then the nav chip in " +
      "SERVER order",
      rows.length === 1 &&
      labelsOf(rows[0]) === "10:00 AM|1:30 PM|See later times");
    const btns = buttonsOf(rows[0]);
    const navBtn = btns[2];
    check("G2 nav chip carries the FULL owner label",
      navBtn.textContent === "See later times");

    let resolveChat = null;
    sb.setChatResponder(() => new Promise((r) => { resolveChat = r; }));
    const before = chatPosts(sb).length;
    navBtn.click();
    await flush();
    const posts = chatPosts(sb);
    const post = posts[posts.length - 1];
    const body = parsedBody(post);
    check("G3 nav tap submits ONE calendar_choice POST with the fixed " +
      "literal id and the full label as the message",
      posts.length === before + 1 &&
      body.message === "See later times" &&
      body.action && body.action.type === "calendar_choice" &&
      body.action.choice_id === "slots-later");
    check("G4 submitted panel is RETAINED sp-submitting and the tapped nav " +
      "chip is sp-selected aria-pressed",
      rowsOf(sb, "slot-panel-row").length === 1 &&
      rowsOf(sb, "slot-panel-row")[0].classList.contains("sp-submitting") &&
      navBtn.classList.contains("sp-selected") &&
      navBtn.attributes["aria-pressed"] === "true");
    check("G5 the disable sweep covers EVERY chip - slots and nav alike",
      btns.every((b) => b.disabled === true));
    navBtn.click();
    btns[0].click();
    await flush();
    check("G6 double/second clicks during flight produce no additional POST",
      chatPosts(sb).length === before + 1);

    resolveChat({ ok: true, status: 200,
      json: () => Promise.resolve(
        stageReply("slot_selection", { calendar_actions: PAGE2_ACTIONS })) });
    await flush(); await flush();
    const after = rowsOf(sb, "slot-panel-row");
    check("G7 authoritative boundary swaps the panel to the LATER page " +
      "(old row removed, new page renders with its Back chip)",
      after.length === 1 &&
      labelsOf(after[0]) === "3:00 PM|4:00 PM|Back to earlier times");

    let resolveBack = null;
    sb.setChatResponder(() => new Promise((r) => { resolveBack = r; }));
    const backBefore = chatPosts(sb).length;
    buttonsOf(after[0])[2].click();
    await flush();
    const backPosts = chatPosts(sb);
    const backBody = parsedBody(backPosts[backPosts.length - 1]);
    check("G8 Back tap submits slots-earlier with the FULL label - never " +
      "shortened to 'Back'",
      backPosts.length === backBefore + 1 &&
      backBody.message === "Back to earlier times" &&
      backBody.action.choice_id === "slots-earlier");
    resolveBack({ ok: true, status: 200,
      json: () => Promise.resolve(stageReply(null)) });
    await flush(); await flush();
  }

  // =========================================================================
  console.log("- middle page: both directions -");
  // =========================================================================
  {
    const sb = buildSandbox();
    const middle = SLOT_ACTIONS.concat([EARLIER_NAV, LATER_NAV]);
    const rows = await openPanelWith(sb, middle);
    check("G9 middle page renders Back FIRST then See later, after the " +
      "slot chips, exactly as the server ordered",
      rows.length === 1 &&
      labelsOf(rows[0]) ===
        "10:00 AM|1:30 PM|Back to earlier times|See later times");
  }

  // =========================================================================
  console.log("- Start Over sweep -");
  // =========================================================================
  {
    const sb = buildSandbox();
    const rows = await openPanelWith(sb, SLOT_ACTIONS.concat([LATER_NAV]));
    const navBtn = buttonsOf(rows[0])[2];
    let resolveChat = null;
    sb.setChatResponder(() => new Promise((r) => { resolveChat = r; }));
    navBtn.click();
    await flush();
    run(sb.context, "startOver()");
    check("G10 Start Over removes the nav-submitting slot panel immediately",
      rowsOf(sb, "slot-panel-row").length === 0);
    resolveChat({ ok: true, status: 200,
      json: () => Promise.resolve(
        stageReply("slot_selection", { calendar_actions: PAGE2_ACTIONS })) });
    await flush(); await flush();
    check("G11 the abandoned epoch's page restores nothing (no row, no " +
      "abandoned reply text)",
      rowsOf(sb, "slot-panel-row").length === 0 &&
      botMessages(sb).every((m) => m.textContent !== "Server reply."));
  }

  // =========================================================================
  console.log("- 409 replacement carrying nav -");
  // =========================================================================
  {
    const sb = buildSandbox();
    const rows = await openPanelWith(sb, SLOT_ACTIONS.concat([LATER_NAV]));
    sb.setChatResponder(() => failedJson(409, {
      detail: {
        code: "STALE_CHOICE",
        message: "That time was just taken.",
        calendar_actions: PAGE2_ACTIONS,
      },
    }));
    const fetchesBefore = sb.fetchCalls.length;
    buttonsOf(rows[0])[0].click();
    await flush(); await flush();
    const replacement = rowsOf(sb, "slot-panel-row");
    check("G12 slot-panel-origin stale replacement re-renders nav chips " +
      "from the validated replacement set (one POST, no loop)",
      replacement.length === 1 &&
      labelsOf(replacement[0]) ===
        "3:00 PM|4:00 PM|Back to earlier times" &&
      sb.fetchCalls.length === fetchesBefore + 1);
  }

  // =========================================================================
  console.log("- server-driven only + accessibility -");
  // =========================================================================
  {
    const sb = buildSandbox();
    const rows = await openPanelWith(sb, SLOT_ACTIONS);
    check("G13 a panel WITHOUT nav entries renders zero extra chips - the " +
      "widget never invents navigation",
      rows.length === 1 && labelsOf(rows[0]) === "10:00 AM|1:30 PM");
  }
  {
    const sb = buildSandbox();
    const rows = await openPanelWith(sb, SLOT_ACTIONS.concat([LATER_NAV]));
    const btns = buttonsOf(rows[0]);
    check("G14 nav chips are real type=button members of the Available-" +
      "times ARIA group",
      rows[0].children[0].attributes["aria-label"] === "Available times" &&
      btns.every((b) => b.type === "button"));
  }

  console.log("");
  console.log(`RESULT: ${passed} passed, ${failed} failed, ` +
    `${passed + failed} total assertions`);
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
