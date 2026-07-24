// tests/test_map_action.js
//
// S2 Maps backport widget regression tests.
//
// The REAL <script> from chat.html runs inside a Node `vm` sandbox with the
// same minimal DOM used by tests/test_quick_replies.js (scaffolding reused
// from that file so both suites exercise identical harness behavior).
//
// Proves: approved hosts render the "Open in Google Maps" button through
// the existing safe external-link renderer; non-HTTPS, lookalike, arbitrary,
// and malformed URLs are rejected; the real sendMessage meta.map_action call
// site renders the button; the staging backend URL is untouched; and the S1
// quick-reply objects remain present.
//
// Run:  node tests/test_map_action.js   (from the folder containing chat.html)
//       or MIA_CHAT_HTML=/path/to/chat.html node tests/test_map_action.js

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CHAT_HTML = process.env.MIA_CHAT_HTML ||
  path.join(__dirname, "..", "chat.html");

// --------------------------------------------------------------------------
// Minimal DOM (same shape as tests/test_quick_replies.js)
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
    textContent: "", innerHTML: "", value: "", placeholder: "",
    disabled: false, scrollTop: 0, scrollHeight: 0,
    id: "", type: "", href: "", target: "", rel: "",
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
  el.querySelectorAll = () => [];
  return el;
}

function collect(el, out) {
  out.push(el);
  el.children.forEach((c) => collect(c, out));
  return out;
}

function buildSandbox(fetchMeta) {
  const elementsById = {};
  const body = makeElement("body");

  ["messages", "input", "sendBtn", "miaHeaderTitle", "miaHeaderSubtitle",
   "main-menu", "service-menu", "consentModal", "consentAccept"].forEach((id) => {
    const el = makeElement("div");
    el.id = id;
    elementsById[id] = el;
    body.appendChild(el);
  });

  const fetchCalls = [];

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
    querySelector: () => makeElement("div"),
    querySelectorAll: (sel) => {
      const all = collect(body, []);
      const classes = sel.split(",").map((s) => s.trim().replace(/^\./, ""));
      return all.filter((e) =>
        classes.some(
          (cls) =>
            e.classList.contains(cls) ||
            String(e.className).split(/\s+/).includes(cls)
        )
      );
    },
  };

  const storage = {};
  const sandbox = {
    console,
    document: documentStub,
    window: {
      location: { search: "", hostname: "localhost", origin: "http://localhost" },
      matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    },
    localStorage: {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
      removeItem: (k) => { delete storage[k]; },
    },
    URLSearchParams,
    URL,
    setTimeout,
    clearTimeout,
    fetch: (url, opts) => {
      fetchCalls.push({ url, opts });
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          reply: "We are located at 123 Main Street.",
          response: "We are located at 123 Main Street.",
          conversation_id: "conv-1",
          meta: fetchMeta || {},
        }),
      });
    },
  };
  sandbox.window.document = documentStub;
  sandbox.window.localStorage = sandbox.localStorage;
  sandbox.globalThis = sandbox;

  const context = vm.createContext(sandbox);
  const html = fs.readFileSync(CHAT_HTML, "utf8");
  const match = html.match(/<script>([\s\S]*)<\/script>/);
  if (!match) throw new Error("No <script> block found in chat.html");
  vm.runInContext(match[1], context, { filename: "chat.html<script>" });

  return { context, elementsById, fetchCalls, body, html };
}

function run(context, code) {
  return vm.runInContext(code, context);
}

function bookingLinkButtons(elementsById) {
  const messages = elementsById["messages"];
  const rows = messages.children.filter((c) => c.className === "booking-link-row");
  const links = [];
  rows.forEach((r) => r.children.forEach((c) => {
    if (c.className === "booking-link-button") links.push(c);
  }));
  return links;
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

function renderCase(action) {
  const { context, elementsById } = buildSandbox();
  run(context, "renderMapActionButton(" + JSON.stringify(action) + ");");
  return bookingLinkButtons(elementsById);
}

// 1. Approved hosts render through the existing safe renderer.
{
  const links = renderCase({ url: "https://maps.app.goo.gl/AbC123", label: "Open in Google Maps" });
  ok("approved maps.app.goo.gl renders Open in Google Maps button",
     links.length === 1 &&
     links[0].href === "https://maps.app.goo.gl/AbC123" &&
     links[0].textContent === "Open in Google Maps" &&
     links[0].target === "_blank" &&
     links[0].rel === "noopener noreferrer");
}
{
  const links = renderCase({ url: "https://maps.google.com/?cid=99" });
  ok("approved maps.google.com renders with default label",
     links.length === 1 && links[0].textContent === "Open in Google Maps");
}
{
  const links = renderCase({ url: "https://www.google.com/maps/place/x", label: "Open in Google Maps" });
  ok("google.com /maps path renders", links.length === 1);
}

// 2. Rejections.
ok("google.com bare homepage rejected", renderCase({ url: "https://www.google.com/" }).length === 0);
ok("google.com /mapsearch lookalike path rejected", renderCase({ url: "https://www.google.com/mapsearch" }).length === 0);
ok("non-HTTPS http URL rejected", renderCase({ url: "http://maps.app.goo.gl/AbC123" }).length === 0);
ok("javascript: URL rejected", renderCase({ url: "javascript:alert(1)" }).length === 0);
ok("lookalike host maps.google.com.evil.example rejected", renderCase({ url: "https://maps.google.com.evil.example/x" }).length === 0);
ok("legacy goo.gl host rejected (not in allowlist)", renderCase({ url: "https://goo.gl/maps/abc" }).length === 0);
ok("arbitrary host rejected", renderCase({ url: "https://example.com/maps" }).length === 0);
ok("malformed URL rejected", renderCase({ url: "not a url" }).length === 0);
ok("null action rejected", renderCase(null).length === 0);
ok("action without url rejected", renderCase({ label: "Open in Google Maps" }).length === 0);

// 3. Allowlist is exactly the verified production set.
{
  const { context } = buildSandbox();
  const hosts = run(context, "JSON.stringify(APPROVED_MAP_HOSTS)");
  ok("widget allowlist matches verified production set",
     hosts === JSON.stringify({
       "maps.app.goo.gl": true,
       "maps.google.com": true,
       "www.google.com": "/maps",
       "google.com": "/maps",
     }));
}

// 4. The real sendMessage meta.map_action call site renders the button.
{
  const meta = {
    map_action: {
      type: "external_link",
      url: "https://maps.app.goo.gl/MetaPath1",
      label: "Open in Google Maps",
      target: "_blank",
      rel: "noopener noreferrer",
    },
  };
  const sb = buildSandbox(meta);
  run(sb.context, 'document.getElementById("input").value = "what is your address";');
  run(sb.context, "sendMessage();");
  setTimeout(() => {
    const links = bookingLinkButtons(sb.elementsById);
    ok("sendMessage meta.map_action path renders the Maps button",
       links.length === 1 && links[0].href === "https://maps.app.goo.gl/MetaPath1");

    // 5. Static file assertions.
    ok("staging Render URL unchanged",
       sb.html.indexOf("https://ai-dental-chatbot-staging.onrender.com") !== -1);
    ok("production Render URL not introduced",
       sb.html.indexOf('"https://ai-dental-chatbot.onrender.com"') === -1);
    ok("S1 quick-reply functions still present",
       sb.html.indexOf("normalizeQuickReplyOption") !== -1 &&
       sb.html.indexOf("getServiceReplyOptions") !== -1);

    console.log("\n" + passed + " passed, " + failed + " failed");
    process.exit(failed === 0 ? 0 : 1);
  }, 20);
}
