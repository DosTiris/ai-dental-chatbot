// tests/test_widget_time_stage.js
//
// C2-A.3 — patient-widget visual TIME stages, frontend proofs.
//
// Executes the REAL inline script from static/chat.html in a Node `vm`
// sandbox (the same technique as tests/test_widget_date_picker.js and
// tests/test_chat_structured_actions.js). Proves the 42 frozen-contract
// behaviors: the two stage triggers (time_preference / slot_selection),
// typed-parity ordinary-message preference transport, the existing
// slot-UUID calendar_choice submission path, the RETAINED in-flight
// lifecycle (submitted row stays attached / selected / disabled /
// visible until the authoritative response boundary), exact-element
// cleanup, row-level duplicate protection with UNCHANGED global
// input-lock semantics, Start Over and chatEpoch invalidation, the
// visual-slot-panel rendering of recognized 409 STALE_CHOICE
// replacements, loop-free no-replacement stales, transport-specific
// network wording, strict widget-side gating on stage values, C2-A.2
// date-picker non-interference, and the accessibility attributes.
//
// Run:
//   node tests/test_widget_time_stage.js
// or:
//   MIA_CHAT_HTML=/path/to/static/chat.html node tests/test_widget_time_stage.js

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

async function main() {
  // =========================================================================
  console.log("— time-preference row —");
  // =========================================================================
  {
    const sb = buildSandbox();
    const rows = await openPreferenceRow(sb);
    check("P1 stage time_preference renders exactly one preference row",
      rows.length === 1);
    const btns = rows.length ? buttonsOf(rows[0]) : [];
    check("P2 buttons are exactly Morning and Afternoon in order",
      btns.length === 2 && btns[0].textContent === "Morning" &&
      btns[1].textContent === "Afternoon");
    check("P13 typed input stays usable while the row is shown",
      run(sb.context, "inputEl.disabled") === false);

    // Hold the click's request unresolved.
    let resolveChat = null;
    sb.setChatResponder(() => new Promise((r) => { resolveChat = r; }));
    const before = chatPosts(sb).length;
    btns[0].click();
    await flush();
    const during = chatPosts(sb);
    const clickPost = during[during.length - 1];
    check("P3 click submits ONE ordinary message (no action field)",
      during.length === before + 1 &&
      parsedBody(clickPost).message === "Morning" &&
      parsedBody(clickPost).action === undefined);
    check("P4 submitted row stays attached during the unresolved request",
      rowsOf(sb, "time-pref-row").length === 1 &&
      rowsOf(sb, "time-pref-row")[0].classList.contains("tp-submitting"));
    check("P5 selected button carries aria-pressed=true and tp-selected",
      btns[0].attributes["aria-pressed"] === "true" &&
      btns[0].classList.contains("tp-selected"));
    check("P6 every preference button is disabled during the request",
      btns[0].disabled === true && btns[1].disabled === true);
    btns[0].click();
    btns[1].click();
    await flush();
    check("P7 double/second clicks produce no additional POST",
      chatPosts(sb).length === before + 1);
    sb.flushRaf();
    check("P8 bounded rAF visibility restoration targeted the selection",
      btns[0].scrollIntoViewCalls.length === 1 &&
      btns[0].scrollIntoViewCalls[0] &&
      btns[0].scrollIntoViewCalls[0].block === "nearest");

    // Authoritative boundary: resolve, row is removed (exact element).
    resolveChat({ ok: true, status: 200,
      json: () => Promise.resolve(stageReply(null)) });
    await flush(); await flush();
    check("P9 row is removed only at the authoritative response boundary",
      rowsOf(sb, "time-pref-row").length === 0);
  }
  {
    // Start Over removes the retained row immediately; the abandoned
    // epoch's completion restores nothing.
    const sb = buildSandbox();
    const rows = await openPreferenceRow(sb);
    const btns = buttonsOf(rows[0]);
    let resolveChat = null;
    sb.setChatResponder(() => new Promise((r) => { resolveChat = r; }));
    btns[1].click();
    await flush();
    run(sb.context, "startOver()");
    check("P10 Start Over removes the retained submitting row immediately",
      rowsOf(sb, "time-pref-row").length === 0);
    // The abandoned epoch's completion must surface NOTHING: no restored
    // row and none of ITS OWN reply text. (Start Over legitimately replays
    // the opening sequence, so total-message counts are not the invariant;
    // the abandoned reply's specific text is.)
    resolveChat({ ok: true, status: 200,
      json: () => Promise.resolve(stageReply("time_preference")) });
    await flush(); await flush();
    check("P11 abandoned-epoch completion restores nothing (no row, no reply)",
      rowsOf(sb, "time-pref-row").length === 0 &&
      botMessages(sb).every((m) => m.textContent !== "Server reply."));
  }
  {
    // Ordinary-message network failure keeps the legacy wording.
    const sb = buildSandbox();
    const rows = await openPreferenceRow(sb);
    const btns = buttonsOf(rows[0]);
    sb.setChatResponder(() => Promise.reject(new Error("offline")));
    btns[0].click();
    await flush(); await flush();
    const bots = botMessages(sb);
    const last = bots[bots.length - 1];
    check("P12 preference network failure shows the ordinary-message wording",
      last && last.textContent.indexOf("having trouble connecting") !== -1);
    check("N1 handled failure removed the retained row (typed recovery open)",
      rowsOf(sb, "time-pref-row").length === 0 &&
      run(sb.context, "inputEl.disabled") === false);
  }

  // =========================================================================
  console.log("— exact-slot panel —");
  // =========================================================================
  {
    const sb = buildSandbox();
    const rows = await openSlotPanel(sb);
    check("S1 stage slot_selection with actions renders the slot panel",
      rows.length === 1 &&
      rowsOf(sb, "quick-replies").length === 0);
    const chips = rows.length ? buttonsOf(rows[0]) : [];
    check("S2 chips reuse validation and render server labels in order",
      chips.length === 2 && chips[0].textContent === "10:00 AM" &&
      chips[1].textContent === "1:30 PM");
    const allText = collect(rows[0], [])
      .map((el) => el.textContent).join(" ");
    check("S3 slot UUIDs are never displayed",
      allText.indexOf("1111") === -1 && allText.indexOf("2222") === -1);

    let resolveChat = null;
    sb.setChatResponder(() => new Promise((r) => { resolveChat = r; }));
    const before = chatPosts(sb).length;
    chips[1].click();
    await flush();
    const during = chatPosts(sb);
    const clickPost = during[during.length - 1];
    const body = parsedBody(clickPost);
    check("S4 click submits the EXISTING slot-UUID calendar_choice action",
      during.length === before + 1 &&
      body.action && body.action.type === "calendar_choice" &&
      body.action.choice_id === "22222222-2222-4222-8222-222222222222" &&
      body.message === "1:30 PM");
    check("S5 submitted panel stays attached during the unresolved action",
      rowsOf(sb, "slot-panel-row").length === 1 &&
      rowsOf(sb, "slot-panel-row")[0].classList.contains("sp-submitting"));
    check("S6 selected chip carries aria-pressed=true and sp-selected",
      chips[1].attributes["aria-pressed"] === "true" &&
      chips[1].classList.contains("sp-selected"));
    check("S7 every slot option is disabled during the request",
      chips[0].disabled === true && chips[1].disabled === true);
    chips[0].click();
    chips[1].click();
    await flush();
    check("S8 double/second clicks produce no additional action POST",
      chatPosts(sb).length === before + 1);
    resolveChat({ ok: true, status: 200,
      json: () => Promise.resolve(stageReply(null)) });
    await flush(); await flush();
    check("S9 panel is removed only at the authoritative response boundary",
      rowsOf(sb, "slot-panel-row").length === 0);
  }
  {
    const sb = buildSandbox();
    const rows = await openSlotPanel(sb);
    const chips = buttonsOf(rows[0]);
    let resolveChat = null;
    sb.setChatResponder(() => new Promise((r) => { resolveChat = r; }));
    chips[0].click();
    await flush();
    run(sb.context, "startOver()");
    check("S10 Start Over removes the retained submitting panel immediately",
      rowsOf(sb, "slot-panel-row").length === 0);
    resolveChat({ ok: true, status: 200,
      json: () => Promise.resolve(
        stageReply("slot_selection", { calendar_actions: SLOT_ACTIONS })) });
    await flush(); await flush();
    check("S11 abandoned-epoch completion cannot restore the panel",
      rowsOf(sb, "slot-panel-row").length === 0 &&
      botMessages(sb).every((m) => m.textContent !== "Server reply."));
  }
  {
    // Owner amendment (Option B): a 409 STALE_CHOICE whose failed
    // submission DEMONSTRABLY originated from the visual slot panel (the
    // chip click below is that origin) renders its validated replacements
    // through the dedicated slot-panel renderer. Generic-origin stales
    // are proven unchanged by the byte-identical existing harnesses.
    const sb = buildSandbox();
    const rows = await openSlotPanel(sb);
    const chips = buttonsOf(rows[0]);
    sb.setChatResponder(() => failedJson(409, {
      detail: {
        code: "STALE_CHOICE",
        message: "That time was just taken.",
        calendar_actions: SLOT_ACTIONS,
      },
    }));
    const fetchesBefore = sb.fetchCalls.length;
    chips[0].click();
    await flush(); await flush();
    const replacement = rowsOf(sb, "slot-panel-row");
    const replacementText = replacement.length
      ? collect(replacement[0], []).map((el) => el.textContent).join(" ")
      : "";
    check("S12 slot-panel-origin 409 replacement renders the visual slot " +
      "panel (Available-times group, no UUIDs, no quick-reply row, one POST)",
      replacement.length === 1 &&
      replacement[0].children[0].attributes["aria-label"] === "Available times" &&
      replacementText.indexOf("1111") === -1 &&
      replacementText.indexOf("2222") === -1 &&
      rowsOf(sb, "quick-replies").length === 0 &&
      sb.fetchCalls.length === fetchesBefore + 1);
  }
  {
    // A 409 without validated replacements stays loop-free: the server
    // message shows once, nothing re-renders, nothing refetches, and
    // typed input remains recoverable.
    const sb = buildSandbox();
    const rows = await openSlotPanel(sb);
    const chips = buttonsOf(rows[0]);
    sb.setChatResponder(() => failedJson(409, {
      detail: { code: "STALE_CHOICE", message: "That time was just taken." },
    }));
    const fetchesBefore = sb.fetchCalls.length;
    chips[0].click();
    await flush(); await flush();
    const staleMessages = botMessages(sb).filter((m) =>
      m.textContent === "That time was just taken.");
    check("S13 no-replacement 409 is loop-free (message once, no refetch, " +
      "no panel, typed input recoverable)",
      staleMessages.length === 1 &&
      sb.fetchCalls.length === fetchesBefore + 1 &&
      rowsOf(sb, "slot-panel-row").length === 0 &&
      run(sb.context, "inputEl.disabled") === false &&
      run(sb.context, "structuredActionInFlight") === false);
  }
  {
    // Slot-action network failure uses ACTION_MSG_NETWORK.
    const sb = buildSandbox();
    const rows = await openSlotPanel(sb);
    const chips = buttonsOf(rows[0]);
    sb.setChatResponder(() => Promise.reject(new Error("offline")));
    chips[0].click();
    await flush(); await flush();
    const bots = botMessages(sb);
    const last = bots[bots.length - 1];
    check("S14 slot network failure shows ACTION_MSG_NETWORK wording",
      last && last.textContent.indexOf("couldn’t reach the chat service") !== -1);
  }

  // =========================================================================
  console.log("— widget-side gating —");
  // =========================================================================
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson(stageReply("time")));
    await typeAndSend(sb, "hello");
    check("G1 unknown stage value renders neither row",
      rowsOf(sb, "time-pref-row").length === 0 &&
      rowsOf(sb, "slot-panel-row").length === 0);
  }
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson({
      reply: "x", conversation_id: "conv-1",
      meta: { calendar_picker: ["time_preference"] },
    }));
    await typeAndSend(sb, "hello");
    check("G2 array-typed calendar_picker is ignored",
      rowsOf(sb, "time-pref-row").length === 0);
  }
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson({
      reply: "x", conversation_id: "conv-1",
      meta: { calendar_picker: "time_preference" },
    }));
    await typeAndSend(sb, "hello");
    check("G3 string-typed calendar_picker is ignored",
      rowsOf(sb, "time-pref-row").length === 0);
  }
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson(stageReply("slot_selection")));
    await typeAndSend(sb, "hello");
    check("G4 slot_selection WITHOUT actions renders nothing",
      rowsOf(sb, "slot-panel-row").length === 0 &&
      rowsOf(sb, "quick-replies").length === 0);
  }
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson(stageReply("Time_Preference")));
    await typeAndSend(sb, "hello");
    check("G5 stage comparison is exact-string (wrong case renders nothing)",
      rowsOf(sb, "time-pref-row").length === 0);
  }
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson({
      reply: "x", conversation_id: "conv-1",
      meta: { calendar_actions: SLOT_ACTIONS },
    }));
    await typeAndSend(sb, "hello");
    check("G6 actions WITHOUT the stage keep the generic quick-reply row",
      rowsOf(sb, "quick-replies").length === 1 &&
      rowsOf(sb, "slot-panel-row").length === 0);
  }

  // =========================================================================
  console.log("— C2-A.2 date-picker non-interference —");
  // =========================================================================
  {
    const sb = buildSandbox();
    sb.setChatResponder(() => successfulJson(stageReply("date")));
    await typeAndSend(sb, "I need an appointment");
    await flush();
    check("D1 stage date still renders the C2-A.2 date picker",
      rowsOf(sb, "date-picker-row").length === 1);
    check("D2 C2-A.2 choice prefix constant is unchanged",
      run(sb.context, "DATE_SELECT_CHOICE_PREFIX") === "pick-date:");
    run(sb.context, "startOver()");
    check("D3 Start Over still removes every date-picker row",
      rowsOf(sb, "date-picker-row").length === 0);
  }
  {
    // clearActionRows still spares dp-submitting rows (existing rule) and
    // now spares tp-/sp-submitting rows the same way.
    const sb = buildSandbox();
    run(sb.context, `
      (function () {
        const mk = (cls, tag) => {
          const el = document.createElement("div");
          el.className = cls;
          if (tag) el.classList.add(tag);
          messagesEl.appendChild(el);
        };
        mk("date-picker-row", "dp-submitting");
        mk("date-picker-row", null);
        mk("time-pref-row", "tp-submitting");
        mk("time-pref-row", null);
        mk("slot-panel-row", "sp-submitting");
        mk("slot-panel-row", null);
        clearActionRows();
      })();
    `);
    check("D4 clearActionRows spares exactly the submitting rows of all three stages",
      rowsOf(sb, "date-picker-row").length === 1 &&
      rowsOf(sb, "time-pref-row").length === 1 &&
      rowsOf(sb, "slot-panel-row").length === 1);
  }

  // =========================================================================
  console.log("— accessibility —");
  // =========================================================================
  {
    const sb = buildSandbox();
    const prefRows = await openPreferenceRow(sb);
    check("A1 preference row is an ARIA group labeled 'Time of day'",
      prefRows[0].attributes.role === "group" &&
      prefRows[0].attributes["aria-label"] === "Time of day");
    const btns = buttonsOf(prefRows[0]);
    check("A2 unselected preference buttons carry aria-pressed=false",
      btns.every((b) => b.attributes["aria-pressed"] === "false"));
  }
  {
    const sb = buildSandbox();
    const rows = await openSlotPanel(sb);
    const panel = rows[0].children[0];
    check("A3 slot panel is an ARIA group labeled 'Available times'",
      panel.attributes.role === "group" &&
      panel.attributes["aria-label"] === "Available times");
    const chips = buttonsOf(rows[0]);
    check("A4 chips are real buttons in DOM order matching the actions order",
      chips.every((c) => c.type === "button") &&
      chips.map((c) => c.textContent).join("|") === "10:00 AM|1:30 PM");
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
