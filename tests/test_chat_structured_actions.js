// tests/test_chat_structured_actions.js
//
// C1-B structured /chat action-transport regression tests.
//
// Executes the real inline script from static/chat.html in a Node `vm`
// sandbox. C1-B proves transport and rendering only: no availability, hold,
// booking, notification, or reset behavior is activated.
//
// Run:
//   node tests/test_chat_structured_actions.js
// or:
//   MIA_CHAT_HTML=/path/to/static/chat.html node tests/test_chat_structured_actions.js

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CHAT_HTML = process.env.MIA_CHAT_HTML ||
  path.join(__dirname, "..", "static", "chat.html");

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
    innerHTML: "",
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
  const fetchCalls = [];
  let chatResponder = options.chatResponder || (() => successfulJson({
    reply: "Okay.",
    conversation_id: "conv-1",
    meta: {},
  }));

  const sandbox = {
    console,
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
      getItem: (key) => (key in storage ? storage[key] : null),
      setItem: (key, value) => { storage[key] = String(value); },
      removeItem: (key) => { delete storage[key]; },
    },
    URLSearchParams,
    URL,
    setTimeout,
    clearTimeout,
    fetch: (url, requestOptions) => {
      fetchCalls.push({ url, options: requestOptions || {} });

      if (String(url).includes("/chat/config")) {
        return successfulJson({});
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

  return {
    context,
    body,
    elementsById,
    fetchCalls,
    html,
    setChatResponder: (responder) => { chatResponder = responder; },
  };
}

function run(context, source) {
  return vm.runInContext(source, context);
}

function quickReplyButtons(elementsById) {
  const messages = elementsById.messages;
  const buttons = [];
  messages.children
    .filter((child) => child.className === "quick-replies")
    .forEach((row) => row.children.forEach((child) => {
      if (child.className === "quick-reply") buttons.push(child);
    }));
  return buttons;
}

function chatPosts(fetchCalls) {
  return fetchCalls.filter((call) =>
    String(call.url).endsWith("/chat") &&
    call.options &&
    call.options.method === "POST"
  );
}

function parsedBody(call) {
  return JSON.parse(call.options.body);
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
  // Legacy normalization remains available.
  {
    const sb = buildSandbox();
    const value = JSON.parse(run(
      sb.context,
      'JSON.stringify(normalizeQuickReplyOption(" Morning "))'
    ));
    ok(
      "legacy string quick reply preserves label and message",
      value.label === "Morning" &&
      value.message === "Morning" &&
      value.action === null
    );
  }

  // Existing configured service objects retain their prior behavior.
  {
    const sb = buildSandbox();
    const value = JSON.parse(run(
      sb.context,
      'JSON.stringify(normalizeQuickReplyOption({' +
      'key:"cleaning_checkup",label:"Cleaning / Checkup",' +
      'message:"I need a cleaning or checkup"}))'
    ));
    ok(
      "configured service object remains text-only",
      value.label === "Cleaning / Checkup" &&
      value.message === "I need a cleaning or checkup" &&
      value.action === null
    );
  }

  // Valid action normalization.
  {
    const sb = buildSandbox();
    const value = JSON.parse(run(
      sb.context,
      'JSON.stringify(normalizeChatAction({' +
      'type:"calendar_choice",choice_id:"  opaque-123  "}))'
    ));
    ok(
      "valid calendar action is normalized",
      value.type === "calendar_choice" &&
      value.choice_id === "opaque-123"
    );
  }

  {
    const sb = buildSandbox();
    ok(
      "unknown action type is rejected",
      run(
        sb.context,
        'normalizeChatAction({type:"book_now",choice_id:"opaque"}) === null'
      )
    );
    ok(
      "blank action choice is rejected",
      run(
        sb.context,
        'normalizeChatAction({' +
        'type:"calendar_choice",choice_id:"   "}) === null'
      )
    );
    ok(
      "extra action field is rejected",
      run(
        sb.context,
        'normalizeChatAction({' +
        'type:"calendar_choice",choice_id:"opaque",slot_id:"raw"}) === null'
      )
    );
    ok(
      "oversized choice is rejected",
      run(
        sb.context,
        'normalizeChatAction({' +
        'type:"calendar_choice",choice_id:"x".repeat(201)}) === null'
      )
    );
    ok(
      "non-string choice is rejected",
      run(
        sb.context,
        'normalizeChatAction({' +
        'type:"calendar_choice",choice_id:123}) === null'
      )
    );
  }

  // Structured quick replies render, while malformed ones do not.
  {
    const sb = buildSandbox();
    run(
      sb.context,
      'renderQuickReplies([{' +
      'label:"Sat, Aug 1",message:"Saturday, August 1",' +
      'action:{type:"calendar_choice",choice_id:"choice-a"}}])'
    );
    const buttons = quickReplyButtons(sb.elementsById);
    ok(
      "valid structured quick reply renders",
      buttons.length === 1 &&
      buttons[0].textContent === "Sat, Aug 1"
    );
  }

  {
    const sb = buildSandbox();
    run(
      sb.context,
      'renderQuickReplies([{' +
      'label:"Unsafe",message:"Unsafe",' +
      'action:{type:"calendar_choice",choice_id:"",slot_id:"raw"}}])'
    );
    ok(
      "malformed structured quick reply does not render",
      quickReplyButtons(sb.elementsById).length === 0
    );
  }

  // Message-only path stays byte-shape compatible: no action property.
  {
    const sb = buildSandbox();
    run(sb.context, 'conversationId = "conv-existing"');
    run(sb.context, 'document.getElementById("input").value = "hello"');
    await run(sb.context, "sendMessage()");
    const posts = chatPosts(sb.fetchCalls);
    const body = parsedBody(posts[0]);
    ok("message-only request still posts once", posts.length === 1);
    ok(
      "message-only request has no action field",
      !Object.prototype.hasOwnProperty.call(body, "action")
    );
    ok(
      "message-only request preserves existing fields",
      body.message === "hello" &&
      body.client_key === "test-client" &&
      body.conversation_id === "conv-existing"
    );
  }

  // Action path carries only the opaque action contract.
  {
    const sb = buildSandbox();
    run(sb.context, 'conversationId = "conv-existing"');
    run(
      sb.context,
      'document.getElementById("input").value = "Saturday, August 1"'
    );
    await run(
      sb.context,
      'sendMessage({type:"calendar_choice",choice_id:"choice-123"})'
    );
    const posts = chatPosts(sb.fetchCalls);
    const body = parsedBody(posts[0]);
    ok("structured action request posts once", posts.length === 1);
    ok(
      "structured action request carries human-readable message",
      body.message === "Saturday, August 1"
    );
    ok(
      "structured action request carries opaque choice only",
      JSON.stringify(body.action) === JSON.stringify({
        type: "calendar_choice",
        choice_id: "choice-123",
      })
    );
    ok(
      "structured action request contains no raw backend identifier fields",
      !("slot_id" in body.action) &&
      !("appointment_id" in body.action) &&
      !("hold_id" in body.action)
    );
    ok(
      "patient widget action uses only POST /chat",
      posts[0].url === "http://localhost/chat"
    );
  }

  // No action can be sent without the active conversation.
  {
    const sb = buildSandbox();
    run(sb.context, "conversationId = null");
    run(sb.context, 'document.getElementById("input").value = "Date"');
    await run(
      sb.context,
      'sendMessage({type:"calendar_choice",choice_id:"choice-a"})'
    );
    ok(
      "structured action without conversation is not sent",
      chatPosts(sb.fetchCalls).length === 0
    );
  }

  // Invalid direct action calls fail closed.
  {
    const sb = buildSandbox();
    run(sb.context, 'conversationId = "conv-existing"');
    run(sb.context, 'document.getElementById("input").value = "Date"');
    await run(
      sb.context,
      'sendMessage({type:"calendar_choice",choice_id:"",slot_id:"raw"})'
    );
    ok(
      "invalid direct action is not sent",
      chatPosts(sb.fetchCalls).length === 0
    );
  }

  // In-flight lock prevents duplicate structured requests.
  {
    let resolveChat;
    const pending = new Promise((resolve) => { resolveChat = resolve; });
    const sb = buildSandbox({
      chatResponder: () => pending,
    });
    run(sb.context, 'conversationId = "conv-existing"');
    run(sb.context, 'document.getElementById("input").value = "Date"');
    const first = run(
      sb.context,
      'sendMessage({type:"calendar_choice",choice_id:"choice-a"})'
    );
    run(sb.context, 'document.getElementById("input").value = "Date"');
    const second = run(
      sb.context,
      'sendMessage({type:"calendar_choice",choice_id:"choice-a"})'
    );

    ok(
      "second structured action is blocked while first is in flight",
      chatPosts(sb.fetchCalls).length === 1
    );

    resolveChat({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        reply: "Okay.",
        conversation_id: "conv-existing",
        meta: {},
      }),
    });
    await Promise.all([first, second]);

    ok(
      "structured action in-flight lock clears after completion",
      run(sb.context, "structuredActionInFlight === false")
    );
  }

  // Server meta.calendar_actions invokes the real render call site.
  {
    const sb = buildSandbox({
      chatResponder: () => successfulJson({
        reply: "Choose a date.",
        conversation_id: "conv-existing",
        meta: {
          calendar_actions: [{
            label: "Sat, Aug 1",
            message: "Saturday, August 1",
            action: {
              type: "calendar_choice",
              choice_id: "choice-date",
            },
          }],
        },
      }),
    });
    run(sb.context, 'conversationId = "conv-existing"');
    run(sb.context, 'document.getElementById("input").value = "show dates"');
    await run(sb.context, "sendMessage()");
    const buttons = quickReplyButtons(sb.elementsById);
    ok(
      "meta.calendar_actions renders through sendMessage",
      buttons.length === 1 &&
      buttons[0].textContent === "Sat, Aug 1"
    );
  }

  // Calendar actions take priority over the existing service-menu flag.
  {
    const sb = buildSandbox({
      chatResponder: () => successfulJson({
        reply: "Choose.",
        conversation_id: "conv-existing",
        meta: {
          show_service_menu: true,
          calendar_actions: [{
            label: "Calendar option",
            message: "Calendar option",
            action: {
              type: "calendar_choice",
              choice_id: "choice-priority",
            },
          }],
        },
      }),
    });
    run(sb.context, 'conversationId = "conv-existing"');
    run(sb.context, 'document.getElementById("input").value = "choose"');
    await run(sb.context, "sendMessage()");
    const labels = quickReplyButtons(sb.elementsById).map(
      (button) => button.textContent
    );
    ok(
      "calendar actions take priority over service menu",
      labels.length === 1 && labels[0] === "Calendar option"
    );
  }

  // Malformed Calendar controls cannot suppress the legacy service menu.
  {
    const sb = buildSandbox({
      chatResponder: () => successfulJson({
        reply: "Choose a service.",
        conversation_id: "conv-existing",
        meta: {
          show_service_menu: true,
          calendar_actions: [{
            label: "Malformed",
            message: "Malformed",
            action: {
              type: "calendar_choice",
              choice_id: "",
              slot_id: "raw",
            },
          }],
        },
      }),
    });
    run(sb.context, 'conversationId = "conv-existing"');
    run(sb.context, 'document.getElementById("input").value = "services"');
    await run(sb.context, "sendMessage()");
    const labels = quickReplyButtons(sb.elementsById).map(
      (button) => button.textContent
    );
    ok(
      "malformed calendar actions fall back to service menu",
      labels.includes("Cleaning / Checkup") &&
      !labels.includes("Malformed")
    );
  }

  // ------------------------------------------------------------------
  // C1-C: structured-action failure handling (locked decisions 15-17).
  // ------------------------------------------------------------------

  function rejectedJson(status, payload) {
    return Promise.resolve({
      ok: false,
      status: status,
      json: () => Promise.resolve(payload),
    });
  }

  function botMessages(sb) {
    return sb.elementsById.messages.children
      .filter((child) => String(child.className).indexOf("msg bot") === 0)
      .map((child) => child.textContent);
  }

  async function sendAction(sb) {
    run(sb.context, 'conversationId = "conv-existing"');
    run(sb.context, 'document.getElementById("input").value = "2:00 PM"');
    await run(
      sb.context,
      'sendMessage({type:"calendar_choice",choice_id:"choice-a"})'
    );
  }

  // Recognized STALE_CHOICE with a replacement set renders the server
  // message and ONLY the recognized replacement buttons.
  {
    const sb = buildSandbox({
      chatResponder: () => rejectedJson(409, {
        detail: {
          code: "STALE_CHOICE",
          message: "That time is gone. Fresh options:",
          calendar_actions: [{
            label: "10:00 AM", message: "10:00 AM",
            action: { type: "calendar_choice", choice_id: "slot-2" },
          }],
        },
      }),
    });
    await sendAction(sb);
    const buttons = quickReplyButtons(sb.elementsById);
    ok(
      "stale choice renders replacement buttons from detail.calendar_actions",
      buttons.length === 1 && buttons[0].textContent === "10:00 AM"
    );
    ok(
      "stale choice shows the server-owned message",
      botMessages(sb).indexOf("That time is gone. Fresh options:") !== -1
    );
    ok(
      "stale choice does not retry the request",
      chatPosts(sb.fetchCalls).length === 1
    );
    ok(
      "in-flight lock clears after a recognized 409",
      run(sb.context, "structuredActionInFlight === false")
    );
  }

  // Recognized STALE_CHOICE WITHOUT a replacement set falls back to the
  // legacy service menu (never re-renders the submitted group).
  {
    const sb = buildSandbox({
      chatResponder: () => rejectedJson(409, {
        detail: { code: "STALE_CHOICE", message: "Please type instead." },
      }),
    });
    await sendAction(sb);
    const buttons = quickReplyButtons(sb.elementsById);
    ok(
      "stale choice without replacement falls back to service menu",
      buttons.length > 0 &&
      buttons.every((b) => b.textContent !== "choice-a")
    );
  }

  // Recognized ACTION_NOT_ACTIVE falls back to the service menu.
  {
    const sb = buildSandbox({
      chatResponder: () => rejectedJson(409, {
        detail: { code: "ACTION_NOT_ACTIVE", message: "Please type a message." },
      }),
    });
    await sendAction(sb);
    ok(
      "not-active shows server message and service menu",
      botMessages(sb).indexOf("Please type a message.") !== -1 &&
      quickReplyButtons(sb.elementsById).length > 0
    );
  }

  // Recognized CONVERSATION_LOCKED disables further typing.
  {
    const sb = buildSandbox({
      chatResponder: () => rejectedJson(409, {
        detail: { code: "CONVERSATION_LOCKED", message: "This chat is closed." },
      }),
    });
    await sendAction(sb);
    ok(
      "locked conversation disables the input and send button",
      run(sb.context, "document.getElementById(\"input\").disabled === true") &&
      run(sb.context, "document.getElementById(\"sendBtn\").disabled === true")
    );
  }

  // Unknown 409 machine code: generic wording, NO replacement rendering,
  // NO retry (fail-closed, locked decision 15).
  {
    const sb = buildSandbox({
      chatResponder: () => rejectedJson(409, {
        detail: { code: "SOMETHING_NEW", message: "??", calendar_actions: [{
          label: "X", message: "X",
          action: { type: "calendar_choice", choice_id: "x" } }] },
      }),
    });
    await sendAction(sb);
    const msgs = botMessages(sb);
    ok(
      "unknown 409 code uses the generic unprocessable wording",
      msgs.length === 1 &&
      msgs[0] === run(sb.context, "ACTION_MSG_UNPROCESSABLE")
    );
    ok(
      "unknown 409 code renders no buttons and does not retry",
      quickReplyButtons(sb.elementsById).length === 0 &&
      chatPosts(sb.fetchCalls).length === 1
    );
  }

  // HTTP 422 keeps the same fail-closed generic path.
  {
    const sb = buildSandbox({
      chatResponder: () => rejectedJson(422, { detail: [{ msg: "invalid" }] }),
    });
    await sendAction(sb);
    ok(
      "422 uses the generic unprocessable wording with no buttons",
      botMessages(sb)[0] === run(sb.context, "ACTION_MSG_UNPROCESSABLE") &&
      quickReplyButtons(sb.elementsById).length === 0
    );
  }

  // Malformed (unparseable) body on an HTTP 200 action response is a
  // failure, not a fake bot reply.
  {
    const sb = buildSandbox({
      chatResponder: () => Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.reject(new Error("bad json")),
      }),
    });
    await sendAction(sb);
    ok(
      "malformed 200 action response uses the generic unprocessable wording",
      botMessages(sb)[0] === run(sb.context, "ACTION_MSG_UNPROCESSABLE")
    );
    ok(
      "in-flight lock clears after a malformed response",
      run(sb.context, "structuredActionInFlight === false")
    );
  }

  // HTTP 5xx gets its own distinct wording.
  {
    const sb = buildSandbox({
      chatResponder: () => rejectedJson(500, { detail: "boom" }),
    });
    await sendAction(sb);
    ok(
      "500 uses the server-error wording",
      botMessages(sb)[0] === run(sb.context, "ACTION_MSG_SERVER_ERROR")
    );
  }

  // Genuine fetch/network failure gets its own distinct wording, and the
  // in-flight lock still clears.
  {
    const sb = buildSandbox({
      chatResponder: () => Promise.reject(new Error("net down")),
    });
    await sendAction(sb);
    ok(
      "network failure uses the network wording",
      botMessages(sb)[0] === run(sb.context, "ACTION_MSG_NETWORK")
    );
    ok(
      "in-flight lock clears after a network failure",
      run(sb.context, "structuredActionInFlight === false")
    );
  }

  // The four failure classes stay pairwise distinct (locked decision 17).
  {
    const sb = buildSandbox();
    const wordings = [
      run(sb.context, "ACTION_MSG_SERVER_ERROR"),
      run(sb.context, "ACTION_MSG_UNPROCESSABLE"),
      run(sb.context, "ACTION_MSG_NETWORK"),
      "That time is gone. Fresh options:",
    ];
    ok(
      "failure wordings are pairwise distinct",
      new Set(wordings).size === wordings.length
    );
  }

  // Legacy message-only sends keep the existing generic connection
  // wording on failure (byte-compatible behavior).
  {
    const sb = buildSandbox({
      chatResponder: () => rejectedJson(500, { detail: "boom" }),
    });
    run(sb.context, 'conversationId = "conv-existing"');
    run(sb.context, 'document.getElementById("input").value = "hello"');
    await run(sb.context, "sendMessage()");
    const msgs = botMessages(sb);
    ok(
      "legacy message failure keeps the existing connection wording",
      msgs.length === 1 &&
      msgs[0].indexOf("having trouble connecting") !== -1
    );
  }

  // ------------------------------------------------------------------
  // V2 (C1-C audit item 6): recognized CONVERSATION_UNAVAILABLE and
  // SAFETY_BLOCKED envelopes. Grounded in the widget's REAL capabilities:
  // the server-owned message renders; NO replacement buttons render (that
  // branch is STALE_CHOICE-only, so an illegitimate calendar_actions
  // payload on these codes is ignored); nothing retries; typing stays
  // ENABLED (only CONVERSATION_LOCKED disables input); the submitted
  // action group cleared at send is never re-rendered; and startOver()
  // remains the recovery path. There is NO call-office button renderer in
  // this widget \u2014 "call the office" exists only as message/placeholder
  // text \u2014 so no such control is asserted.
  // ------------------------------------------------------------------

  for (const failureCode of ["CONVERSATION_UNAVAILABLE", "SAFETY_BLOCKED"]) {
    const serverMessage = "Server-owned message for " + failureCode + ".";
    const sb = buildSandbox({
      chatResponder: () => rejectedJson(409, {
        detail: {
          code: failureCode,
          message: serverMessage,
          // Illegitimate payload on a non-STALE code: must be ignored.
          calendar_actions: [{
            label: "X", message: "X",
            action: { type: "calendar_choice", choice_id: "x" },
          }],
        },
      }),
    });
    // Pre-render a live action group so the clear-at-send contract is
    // observable for this code too.
    run(sb.context, 'renderQuickReplies([{label:"10:00 AM",' +
      'message:"10:00 AM",' +
      'action:{type:"calendar_choice",choice_id:"slot-1"}}])');
    await sendAction(sb);
    ok(
      failureCode + " shows the server-owned message",
      botMessages(sb).indexOf(serverMessage) !== -1
    );
    ok(
      failureCode + " renders no buttons and ignores non-stale calendar_actions",
      quickReplyButtons(sb.elementsById).length === 0
    );
    ok(
      failureCode + " does not retry the request",
      chatPosts(sb.fetchCalls).length === 1
    );
    ok(
      failureCode + " leaves typing enabled (no input lockout)",
      run(sb.context, 'document.getElementById("input").disabled === false') &&
      run(sb.context, 'document.getElementById("sendBtn").disabled === false')
    );
    ok(
      failureCode + " never re-renders the cleared submitted action group",
      quickReplyButtons(sb.elementsById)
        .every((b) => b.textContent !== "10:00 AM")
    );
    ok(
      failureCode + " clears the in-flight lock",
      run(sb.context, "structuredActionInFlight === false")
    );
    ok(
      failureCode + " keeps the startOver recovery path available",
      run(sb.context, 'typeof startOver === "function"') &&
      sb.html.includes('onclick="startOver()"')
    );
  }

  // ------------------------------------------------------------------
  // V3 (audit item 2): parsed-but-malformed structured-action HTTP 200
  // bodies. The widget must validate the success shape BEFORE the normal
  // rendering path: a non-null object with non-empty string reply and
  // conversation_id, and meta (when present) an object. Failures use the
  // SAME class as an unparseable body: ACTION_MSG_UNPROCESSABLE, no
  // retry, no buttons, and never the legacy fallback help reply.
  // ------------------------------------------------------------------

  const malformedSuccessBodies = [
    ["parsed empty object", {}],
    ["missing reply", { conversation_id: "conv-1", meta: {} }],
    ["non-string reply", { reply: 42, conversation_id: "conv-1", meta: {} }],
    ["missing conversation_id", { reply: "Okay.", meta: {} }],
    ["malformed meta", { reply: "Okay.", conversation_id: "conv-1",
                         meta: "not-an-object" }],
    // V4 (audit item 2): non-empty means non-BLANK after trimming.
    ["whitespace-only reply", { reply: "   \u00a0 ", conversation_id: "conv-1",
                                meta: {} }],
    ["whitespace-only conversation_id", { reply: "Okay.",
                                          conversation_id: "   ", meta: {} }],
  ];
  for (const [label, body] of malformedSuccessBodies) {
    const sb = buildSandbox({
      chatResponder: () => successfulJson(body),
    });
    await sendAction(sb);
    const msgs = botMessages(sb);
    ok(
      "malformed 200 action body (" + label + ") uses the generic " +
        "unprocessable wording, never the legacy fallback",
      msgs.length === 1 &&
      msgs[0] === run(sb.context, "ACTION_MSG_UNPROCESSABLE") &&
      msgs[0].indexOf("appointments, services, insurance") === -1
    );
    ok(
      "malformed 200 action body (" + label + ") renders no buttons, " +
        "does not retry, and clears the in-flight lock",
      quickReplyButtons(sb.elementsById).length === 0 &&
      chatPosts(sb.fetchCalls).length === 1 &&
      run(sb.context, "structuredActionInFlight === false")
    );
  }

  // A VALID final_closed HTTP 200 action response still renders normally
  // through the new shape validation (regression guard for the pinned
  // final_closed 200 contract).
  {
    const finalClosedReply =
      "This conversation has ended. Please tap Start over to begin again.";
    const sb = buildSandbox({
      chatResponder: () => successfulJson({
        reply: finalClosedReply,
        conversation_id: "conv-existing",
        meta: { final_closed: true },
      }),
    });
    await sendAction(sb);
    ok(
      "valid final_closed 200 action response renders the server reply",
      botMessages(sb).indexOf(finalClosedReply) !== -1 &&
      botMessages(sb).indexOf(
        run(sb.context, "ACTION_MSG_UNPROCESSABLE")
      ) === -1
    );
    ok(
      "valid final_closed 200 keeps typing available and clears in-flight",
      run(sb.context, 'document.getElementById("input").disabled === false') &&
      run(sb.context, "structuredActionInFlight === false")
    );
  }

  // Static boundaries.
  {
    const sb = buildSandbox();
    ok(
      "production Render URL is preserved",
      sb.html.includes("https://ai-dental-chatbot.onrender.com")
    );
    ok(
      "staging same-origin behavior is preserved",
      sb.html.includes(
        'const STAGING_HOSTNAME = "ai-dental-chatbot-staging.onrender.com";'
      )
    );
    ok(
      "patient widget contains no admin availability-preview route",
      !sb.html.includes("/admin/calendar/availability-preview")
    );
    ok(
      "patient widget contains no admin credential field",
      !sb.html.includes("X-Admin-Key") &&
      !sb.html.includes("ADMIN_API_KEY")
    );
    ok(
      "C1-B widget adds no direct hold or booking endpoint",
      !sb.html.includes("/calendar/hold") &&
      !sb.html.includes("/calendar/book") &&
      !sb.html.includes("/appointments")
    );
    ok(
      "existing map action renderer remains present",
      sb.html.includes("renderMapActionButton")
    );
  }

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
