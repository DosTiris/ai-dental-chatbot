/*
 * portal_test_harness.js - Node vm harness for the P3-A portal frontend.
 *
 * Loads static/portal/portal-core.js into an isolated vm context (no
 * browser, no network, no real storage) and provides:
 *   - FakeStorage: localStorage-shaped in-memory store
 *   - ScriptedFetch: an ordered queue of expected requests; every real
 *     request is matched against the next expectation and answered with
 *     the scripted response. An unexpected or leftover request FAILS the
 *     test - the suite proves not only what the core does, but that it
 *     performs no unscripted network calls (no hidden behavior).
 *   - FakeClock: controllable Date.now source
 *   - a tiny async test registry with honest pass/fail counting
 *
 * No third-party packages are used, matching the repository's
 * dependency-free JS test convention.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CORE_PATH = path.join(__dirname, "..", "..", "static", "portal", "portal-core.js");

/* localStorage-shaped in-memory store. */
function FakeStorage() {
  const data = new Map();
  return {
    getItem(key) { return data.has(key) ? data.get(key) : null; },
    setItem(key, value) { data.set(key, String(value)); },
    removeItem(key) { data.delete(key); },
    _dump() { return new Map(data); }
  };
}

/* Controllable clock; timeMs is epoch milliseconds. */
function FakeClock(startMs) {
  let timeMs = startMs;
  const fn = () => timeMs;
  fn.advanceSeconds = (s) => { timeMs += s * 1000; };
  fn.set = (ms) => { timeMs = ms; };
  return fn;
}

/*
 * ScriptedFetch: expectations are consumed strictly in order.
 * expectation = {
 *   match: { urlIncludes?, urlEquals?, method?, headerEquals?: {name: value},
 *            bodyJson?: object (deep-equal on parsed JSON body) },
 *   respond: { status, json } | { networkError: true }
 * }
 */
function ScriptedFetch() {
  const queue = [];
  const seen = [];

  function deepEqual(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
  }

  const impl = function (url, init) {
    init = init || {};
    const record = {
      url: String(url),
      method: init.method || "GET",
      headers: init.headers || {},
      body: init.body
    };
    seen.push(record);
    const expectation = queue.shift();
    if (!expectation) {
      return Promise.reject(new Error(
        "ScriptedFetch: UNEXPECTED request " + record.method + " " + record.url));
    }
    const m = expectation.match || {};
    const problems = [];
    if (m.urlEquals !== undefined && record.url !== m.urlEquals) {
      problems.push("url expected == " + m.urlEquals + " got " + record.url);
    }
    if (m.urlIncludes !== undefined && record.url.indexOf(m.urlIncludes) === -1) {
      problems.push("url expected to include " + m.urlIncludes + " got " + record.url);
    }
    if (m.method !== undefined && record.method !== m.method) {
      problems.push("method expected " + m.method + " got " + record.method);
    }
    if (m.headerEquals) {
      for (const name of Object.keys(m.headerEquals)) {
        if (record.headers[name] !== m.headerEquals[name]) {
          problems.push("header " + name + " expected " + m.headerEquals[name] +
            " got " + record.headers[name]);
        }
      }
    }
    if (m.bodyJson !== undefined) {
      let parsed = null;
      try { parsed = JSON.parse(record.body); } catch (e) { parsed = "<unparseable>"; }
      if (!deepEqual(parsed, m.bodyJson)) {
        problems.push("body expected " + JSON.stringify(m.bodyJson) +
          " got " + String(record.body));
      }
    }
    if (problems.length) {
      return Promise.reject(new Error(
        "ScriptedFetch: request mismatch: " + problems.join("; ")));
    }
    const r = expectation.respond || {};
    if (r.networkError) {
      return Promise.reject(new TypeError("ScriptedFetch: simulated network failure"));
    }
    const status = r.status === undefined ? 200 : r.status;
    const jsonBody = r.json;
    return Promise.resolve({
      status: status,
      json() {
        if (jsonBody === undefined) {
          return Promise.reject(new Error("no json body scripted"));
        }
        return Promise.resolve(jsonBody);
      }
    });
  };

  impl.expect = (match, respond) => { queue.push({ match, respond }); };
  impl.remaining = () => queue.length;
  impl.seen = () => seen.slice();
  return impl;
}

/* Build a fresh core instance wired to fresh fakes. */
function makeCore(options) {
  options = options || {};
  const source = fs.readFileSync(CORE_PATH, "utf8");
  const sandboxWindow = {};
  /* URLSearchParams is a universal browser global; the vm context must
   * provide it too or hash parsing would falsely appear to fail. */
  const context = { window: sandboxWindow, URLSearchParams: URLSearchParams };
  vm.createContext(context);
  vm.runInContext(source, context, { filename: "portal-core.js" });
  const storage = options.storage || FakeStorage();
  const clock = options.clock || FakeClock(1_800_000_000_000); /* 2027-01-15ish */
  const fetchImpl = options.fetch || ScriptedFetch();
  const core = sandboxWindow.createMiaPortalCore({
    fetchImpl: fetchImpl,
    storage: storage,
    nowFn: clock,
    windowOrigin: options.windowOrigin || "https://portal.test"
  });
  return { core, storage, clock, fetch: fetchImpl };
}

/* Standard scripted config load (most flows begin with it). */
function expectConfigLoad(fetchImpl, cfg) {
  fetchImpl.expect(
    { urlEquals: "/portal/config", method: "GET" },
    { status: 200, json: cfg || {
        supabase_url: "https://example-project.supabase.co",
        supabase_publishable_key: "sb_publishable_TEST_ONLY"
      } }
  );
}

const SUPABASE = "https://example-project.supabase.co";

/* ------------------------------------------------------------------ */
/* Minimal async test registry                                         */
/* ------------------------------------------------------------------ */

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

function assert(condition, message) {
  if (!condition) {
    throw new Error("ASSERT FAILED: " + message);
  }
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error("ASSERT FAILED: " + message +
      " (expected " + JSON.stringify(expected) +
      ", got " + JSON.stringify(actual) + ")");
  }
}

async function runRegisteredTests(suiteName) {
  let passed = 0;
  const failures = [];
  for (const t of tests) {
    try {
      await t.fn();
      passed += 1;
      console.log("  PASS  " + t.name);
    } catch (err) {
      failures.push({ name: t.name, error: err });
      console.log("  FAIL  " + t.name);
      console.log("        " + String(err.message || err));
    }
  }
  console.log(suiteName + ": " + passed + " passed, " +
    failures.length + " failed, " + tests.length + " total");
  return { passed, failed: failures.length, total: tests.length };
}

module.exports = {
  FakeStorage,
  FakeClock,
  ScriptedFetch,
  makeCore,
  expectConfigLoad,
  SUPABASE,
  test,
  assert,
  assertEqual,
  runRegisteredTests
};
