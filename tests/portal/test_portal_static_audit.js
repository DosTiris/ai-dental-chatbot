/*
 * test_portal_static_audit.js - static source audit of the shipped portal
 * frontend files (P3-A requirements 4, 8, 9, 10).
 *
 * These checks run against the real bytes that ship, so a future edit that
 * introduces a secret, a signup path, a browser-authoritative tenant id,
 * or a third-party script fails the suite immediately.
 *
 * Run: node tests/portal/test_portal_static_audit.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const h = require("./portal_test_harness.js");
const { test, assert, assertEqual } = h;

const PORTAL_DIR = path.join(__dirname, "..", "..", "static", "portal");

/* The exact production file set this audit covers. A new portal file must
 * be added here deliberately so it comes under audit (closed list). */
const PORTAL_FILES = [
  "index.html",
  "reset.html",
  "portal.css",
  "portal-core.js",
  "portal-app.js",
  "portal-reset.js",
  "portal-data.js",   /* P3-B1: read-only data access */
  "portal-pages.js",  /* P3-B1: dashboard/leads DOM glue */
  "portal-calendar.js" /* Visual Calendar Phase 1: pure Week-view render */
];

function read(name) {
  return fs.readFileSync(path.join(PORTAL_DIR, name), "utf8");
}

function readBytes(name) {
  return fs.readFileSync(path.join(PORTAL_DIR, name));
}

/* ------------------------------------------------------------------ */
/* Requirement 9: secret and credential leakage audit                  */
/* ------------------------------------------------------------------ */

test("audit: no secret material or backend credential names in any portal file", () => {
  /* Case-insensitive forbidden markers. These are the credential families
   * that exist in the Mia system; none may ever reach browser code. */
  const forbidden = [
    "service_role", "sb_secret", "supabase_service",
    "admin_api_key", "x-admin-key", "mia_cal_",
    "database_url", "openai_api_key", "twilio", "resend_api",
    "jwt_secret", "postgres://", "postgresql://",
    "eyjhbgcioi" /* base64 of a real JWT header: no live token literals */
  ];
  for (const name of PORTAL_FILES) {
    const content = read(name).toLowerCase();
    for (const marker of forbidden) {
      assert(content.indexOf(marker) === -1,
        name + " must not contain '" + marker + "'");
    }
  }
});

test("audit: config comes only from the backend /portal/config endpoint", () => {
  /* F-P3A-1: the deploy-blocking static JSON config file is gone. No portal
   * file may reference a portal-config.json (example or real), and the
   * config URL literal in portal-core.js must be the backend endpoint. */
  for (const name of PORTAL_FILES) {
    assert(read(name).indexOf("portal-config") === -1,
      name + " must not reference the removed static config file");
  }
  assert(read("portal-core.js").indexOf('"/portal/config"') !== -1,
    "portal-core.js must load config from the backend /portal/config");
});

test("audit: no static config JSON ships with the portal frontend", () => {
  assert(!fs.existsSync(path.join(PORTAL_DIR, "portal-config.json")),
    "portal-config.json must not exist in the payload");
  assert(!fs.existsSync(path.join(PORTAL_DIR, "portal-config.example.json")),
    "portal-config.example.json was removed by F-P3A-1 and must not return");
});

/* ------------------------------------------------------------------ */
/* Requirement 4: no public signup surface                             */
/* ------------------------------------------------------------------ */

test("audit: no signup vocabulary anywhere in the portal frontend", () => {
  const forbidden = ["signup", "sign-up", "sign_up", "create account", "create an account"];
  for (const name of PORTAL_FILES) {
    const content = read(name).toLowerCase();
    for (const marker of forbidden) {
      assert(content.indexOf(marker) === -1,
        name + " must not contain '" + marker + "'");
    }
  }
});

test("audit: portal-core references only the four allow-listed GoTrue paths", () => {
  const content = read("portal-core.js");
  const allowList = ["/token?grant_type=password", "/token?grant_type=refresh_token",
    "/logout", "/recover?redirect_to=", "/user"];
  /* Every string literal beginning with a slash that is passed to the
   * GoTrue request owner must be one of the allow-listed paths. */
  const literalPattern = /gotrueRequest\(cfg,\s*\r?\n?\s*?"([^"]+)"/g;
  const inline = /gotrueRequest\(cfg,\s*"([^"]+)"/g;
  const found = [];
  let match;
  while ((match = inline.exec(content)) !== null) {
    found.push(match[1]);
  }
  /* Also catch the recover call built by concatenation. */
  if (content.indexOf('"/recover?redirect_to=" + encodeURIComponent') !== -1) {
    found.push("/recover?redirect_to=");
  }
  assert(found.length >= 4, "expected the known GoTrue call sites, found " + found.length);
  for (const p of found) {
    assert(allowList.indexOf(p) !== -1, "unexpected GoTrue path '" + p + "'");
  }
  assert(content.indexOf("/auth/v1/signup") === -1, "no signup endpoint literal");
  void literalPattern;
});

/* ------------------------------------------------------------------ */
/* Requirement 8: no browser-authoritative tenant identity             */
/* ------------------------------------------------------------------ */

test("audit: no client identifier vocabulary in any portal file", () => {
  const forbidden = ["client_id", "clientid", "client_key", "clientkey", "tenant_id", "tenantid"];
  for (const name of PORTAL_FILES) {
    const content = read(name).toLowerCase();
    for (const marker of forbidden) {
      assert(content.indexOf(marker) === -1,
        name + " must not contain '" + marker + "'");
    }
  }
});

test("audit: portal JS never reads the query string (no tenant/params channel)", () => {
  for (const name of ["portal-core.js", "portal-app.js", "portal-reset.js",
    "portal-data.js", "portal-pages.js", "portal-calendar.js"]) {
    const content = read(name);
    assert(content.indexOf("location.search") === -1,
      name + " must not read location.search");
  }
});

/* ------------------------------------------------------------------ */
/* Requirement 10 (frontend half): auth-system separation              */
/* ------------------------------------------------------------------ */

test("audit: portal JS references no operator or Calendar admin routes", () => {
  for (const name of ["portal-core.js", "portal-app.js", "portal-reset.js",
    "portal-data.js", "portal-pages.js", "portal-calendar.js"]) {
    const content = read(name);
    assert(content.indexOf("/admin/") === -1, name + " must not call /admin/ routes");
    assert(content.indexOf("/chat") === -1, name + " must not call the patient chat API");
  }
});

/* P3-B1 + Portal Appointments v1: the data layer's OWN backend allow-list -
 * exactly the read-only endpoint literals, nothing else, and the required
 * ones must exist. Appointments is a READ-ONLY GET like dashboard/leads. */
test("audit: portal-data calls only the allow-listed read endpoints", () => {
  const content = read("portal-data.js");
  const portalCalls = content.match(/"\/portal\/[a-z-]*"/g) || [];
  /* P4-A: "/portal/schedule" added DELIBERATELY (the closed-list growth
   * mechanism this audit documents) for the approved schedule surface;
   * every derived action path is built from that one literal with
   * URI-encoded segments, so no second literal may appear. */
  const allowed = ['"/portal/dashboard"', '"/portal/leads"',
    '"/portal/appointments"', '"/portal/schedule"',
    '"/portal/notification-settings"'];
  for (const call of portalCalls) {
    assert(allowed.indexOf(call) !== -1,
      call + " is not an allowed portal data endpoint");
  }
  assert(portalCalls.indexOf('"/portal/dashboard"') !== -1,
    "dashboard endpoint literal must exist");
  assert(portalCalls.indexOf('"/portal/leads"') !== -1,
    "leads endpoint literal must exist");
  assert(portalCalls.indexOf('"/portal/appointments"') !== -1,
    "appointments endpoint literal must exist");
  assert(portalCalls.indexOf('"/portal/schedule"') !== -1,
    "schedule endpoint literal must exist (P4-A)");
  assert(portalCalls.indexOf('"/portal/notification-settings"') !== -1,
    "notification-settings endpoint literal must exist (P6-A)");
});

/* P4-B: the recurring-schedule surface is a MULTI-segment path the
 * single-segment allow-list regex above cannot capture, so it is brought
 * under the closed list here: exactly ONE base literal must appear, and
 * /preview + /apply are built by concatenation (no second literal). */
test("audit: portal-data recurring surface uses exactly the one approved base literal", () => {
  const content = read("portal-data.js");
  assert(content.indexOf('"/portal/schedule/recurring"') !== -1,
    "recurring base endpoint literal must exist (P4-B)");
  const recurringLiterals = content.match(/"\/portal\/schedule\/recurring[a-z/-]*"/g) || [];
  assertEqual(recurringLiterals.length, 1,
    "recurring surface must use exactly one base literal; /preview and /apply are concatenated");
  assertEqual(recurringLiterals[0], '"/portal/schedule/recurring"',
    "the one recurring literal must be the approved base");
});

/* ------------------------------------------------------------------ */
/* P4-A: schedule surface audits                                       */
/* ------------------------------------------------------------------ */

/* Contract v1.2 SS5-E / D3: the bulk action is a SLOT operation. The
 * shipped schedule MARKUP (the page-schedule section of index.html) must
 * never word it as shutting the day. */
test("audit: schedule markup never uses day-shutting vocabulary", () => {
  const content = read("index.html");
  const start = content.indexOf('id="page-schedule"');
  assert(start !== -1, "index.html must contain the page-schedule section");
  const end = content.indexOf("</section>", start);
  assert(end !== -1, "page-schedule section must be closed");
  const section = content.slice(start, end).toLowerCase();
  for (const word of ["close", "closed", "closure"]) {
    assert(section.indexOf(word) === -1,
      "page-schedule markup must not contain '" + word + "'");
  }
  assert(section.indexOf("block all open slots") !== -1,
    "the bulk control must be worded 'Block all open slots'");
});

/* The schedule USER-FACING WORDING in portal-pages.js (the schedule_*
 * MESSAGES values and the rendered button labels) must not use the
 * day-shutting vocabulary either. String literals only - code comments
 * legitimately DISCUSS the rule by quoting the words. */
test("audit: schedule wording in portal-pages.js never shuts the day", () => {
  const content = read("portal-pages.js");
  const pattern = /schedule_[a-z_]+:\s*\r?\n?\s*"([^"]*)"/g;
  const values = [];
  let match;
  while ((match = pattern.exec(content)) !== null) {
    values.push(match[1]);
  }
  assert(values.length >= 5, "the schedule MESSAGES entries must exist");
  for (const value of values) {
    const lowered = value.toLowerCase();
    for (const word of ["close", "closed", "closure"]) {
      assert(lowered.indexOf(word) === -1,
        "schedule message wording must not contain '" + word + "': " + value);
    }
  }
});

/* P3-B1: portal-pages is DOM glue ONLY - it owns no network surface at
 * all, so a future edit cannot quietly grow a second request pathway. */
test("audit: portal-pages performs no network requests of its own", () => {
  const content = read("portal-pages.js");
  assert(content.indexOf("fetch(") === -1,
    "portal-pages.js must not call fetch");
  assert(content.indexOf("XMLHttpRequest") === -1,
    "portal-pages.js must not use XMLHttpRequest");
  assert(content.indexOf('"/portal/') === -1,
    "portal-pages.js must not hold backend endpoint literals");
});

/* Visual Calendar Phase 1: the renderer is a PURE presentation module.
 * This is the byte-level proof that it never became a second network
 * owner - the single highest architectural risk of adding a page. */
test("audit: portal-calendar performs no network requests of its own", () => {
  const content = read("portal-calendar.js");
  assert(content.indexOf("fetch") === -1,
    "portal-calendar.js must not reference fetch at all");
  assert(content.indexOf("XMLHttpRequest") === -1,
    "portal-calendar.js must not use XMLHttpRequest");
  assert(content.indexOf("/portal/") === -1,
    "portal-calendar.js must hold no backend endpoint path at all");
  assert(content.indexOf("EventSource") === -1,
    "portal-calendar.js must not open a server stream");
  assert(content.indexOf("WebSocket") === -1,
    "portal-calendar.js must not open a socket");
});

/* Visual Calendar Phase 1: the four presentation helpers are INJECTED,
 * never redeclared, so the portal keeps exactly one time formatter, one
 * day-shift helper, and one status vocabulary per entity (Rule 3). */
test("audit: portal-calendar reimplements none of the injected helpers", () => {
  const content = read("portal-calendar.js");
  for (const name of ["formatInTimeZone", "shiftLocalDay",
    "scheduleSlotStatusLabel", "appointmentStatusLabel",
    "notificationOutcomeLabel"]) {
    assert(content.indexOf("function " + name) === -1,
      "portal-calendar.js must not declare its own " + name);
  }
  /* And it must genuinely take them from the caller. */
  assert(content.indexOf("deps.formatInTimeZone") !== -1,
    "formatInTimeZone must be injected");
  assert(content.indexOf("deps.shiftLocalDay") !== -1,
    "shiftLocalDay must be injected");
  assert(content.indexOf("deps.scheduleSlotStatusLabel") !== -1,
    "scheduleSlotStatusLabel must be injected");
  assert(content.indexOf("deps.appointmentStatusLabel") !== -1,
    "appointmentStatusLabel must be injected");
  assert(content.indexOf("deps.notificationOutcomeLabel") !== -1,
    "notificationOutcomeLabel must be injected");
});

/* Time-axis refinement: geometry is applied through CSSOM property
 * assignment (element.style.top = ...), which the baseline CSP permits.
 * A style attribute parsed from a string would be blocked by style-src
 * 'self', so the grid would silently collapse in production. */
test("audit: portal-calendar sets geometry via CSSOM, never a style attribute", () => {
  const content = read("portal-calendar.js");
  assert(content.indexOf("setAttribute") === -1,
    "portal-calendar.js must not use setAttribute (CSP style-src)");
  assert(content.indexOf("innerHTML") === -1,
    "portal-calendar.js must not use innerHTML");
  assert(content.indexOf(".style.top") !== -1,
    "vertical position must be applied through CSSOM");
  assert(content.indexOf(".style.height") !== -1,
    "block height must be applied through CSSOM");
});

/* Final polish: the calendar must not grow its own vertical scroll box.
 * The page scrolls; a nested vertical scroller would hide the early and
 * late hours the expanding window exists to reveal. */
test("audit: the calendar grid introduces no internal vertical scrolling", () => {
  const css = read("portal.css");
  const start = css.indexOf(".portal-calendar-grid {");
  assert(start !== -1, "the calendar grid rule must exist");
  const rule = css.slice(start, css.indexOf("}", start));
  assert(rule.indexOf("overflow-x: auto") !== -1,
    "day columns scroll HORIZONTALLY");
  assert(rule.indexOf("overflow-y: auto") === -1 &&
    rule.indexOf("overflow-y: scroll") === -1,
    "the calendar must not become its own vertical scroll container");
  assert(css.indexOf(".portal-calendar-canvas {") !== -1,
    "the canvas rule must exist");
  const canvas = css.slice(css.indexOf(".portal-calendar-canvas {"),
    css.indexOf("}", css.indexOf(".portal-calendar-canvas {")));
  assert(canvas.indexOf("overflow-y") === -1,
    "the day canvas must not scroll vertically either");
});

/* Final polish: Open availability is a background layer. A solid fill or a
 * full border would put it back in competition with the appointments. */
test("audit: Open availability keeps a subdued background treatment", () => {
  const css = read("portal.css");
  const start = css.indexOf(".portal-calendar-band-available {");
  assert(start !== -1, "the open-availability rule must exist");
  const rule = css.slice(start, css.indexOf("}", start));
  assert(rule.indexOf("rgba(") !== -1,
    "the fill must be a translucent tint, not a solid panel colour");
  assert(!/\bborder:\s/.test(rule),
    "no full border - a thin left accent only");
});

/* Time-axis refinement: the detail panel is READ-ONLY. No action control
 * and no unauthorized action vocabulary may appear on the calendar surface
 * until a separate contract approves one. */
test("audit: the calendar surface renders no unauthorized action vocabulary", () => {
  const html = read("index.html");
  const start = html.indexOf('id="page-calendar"');
  assert(start !== -1, "index.html must contain the page-calendar section");
  const end = html.indexOf("</section>", start);
  assert(end !== -1, "page-calendar section must be closed");
  const section = html.slice(start, end);
  for (const word of ["Reschedule", "Duplicate", "New appointment",
    "Book ", "Create appointment"]) {
    assert(section.indexOf(word) === -1,
      "page-calendar markup must not offer " + word);
  }
  /* The only controls in the section are week navigation, refresh and the
   * panel close: four buttons, none of them a mutation. */
  const buttons = section.match(/<button/g) || [];
  assert(buttons.length === 4,
    "expected exactly the four read-only controls, found " + buttons.length);
});

/* Visual Calendar Phase 1: the grid never advertises a slot as bookable.
 * Booking eligibility is the availability policy owner's decision and
 * depends on rules (minimum notice, horizon, service) the grid never sees;
 * an open slot therefore renders with the frozen "Open" status wording.
 *
 * SCOPED DELIBERATELY to the calendar surface: the frozen P4-B recurring
 * warning legitimately tells the office how to make dates bookable again,
 * and Phase 1 does not touch that wording (Rule 12: no unrelated change). */
test("audit: the calendar surface never claims a slot is bookable", () => {
  assert(read("portal-calendar.js").toLowerCase().indexOf("bookable") === -1,
    "portal-calendar.js must not use the word bookable");
  const html = read("index.html");
  const start = html.indexOf('id="page-calendar"');
  assert(start !== -1, "index.html must contain the page-calendar section");
  const end = html.indexOf("</section>", start);
  assert(end !== -1, "page-calendar section must be closed");
  assert(html.slice(start, end).toLowerCase().indexOf("bookable") === -1,
    "page-calendar markup must not use the word bookable");
});

/* Visual Calendar Phase 1: portal-pages.js reads the renderer at
 * construction, so the module must be defined FIRST. A reversed order
 * would silently degrade the Calendar page to its unavailable state. */
test("audit: index.html loads portal-calendar.js before portal-pages.js", () => {
  const content = read("index.html");
  const calendarAt = content.indexOf('src="/static/portal/portal-calendar.js"');
  const pagesAt = content.indexOf('src="/static/portal/portal-pages.js"');
  assert(calendarAt !== -1, "index.html must load portal-calendar.js");
  assert(pagesAt !== -1, "index.html must load portal-pages.js");
  assert(calendarAt < pagesAt,
    "portal-calendar.js must be loaded before portal-pages.js");
});

test("audit: the only backend endpoints the portal calls are /portal/config and /portal/me", () => {
  const content = read("portal-core.js");
  const portalCalls = content.match(/"\/portal\/[a-z-]*"/g) || [];
  const allowed = ['"/portal/me"', '"/portal/config"'];
  for (const c of portalCalls) {
    assert(allowed.indexOf(c) !== -1,
      c + " is not an allowed Mia backend endpoint for the portal");
  }
  assert(portalCalls.indexOf('"/portal/me"') !== -1, "bootstrap endpoint literal must exist");
  assert(portalCalls.indexOf('"/portal/config"') !== -1, "config endpoint literal must exist");
});

/* ------------------------------------------------------------------ */
/* Supply-chain and page hygiene                                       */
/* ------------------------------------------------------------------ */

test("audit: no third-party or plaintext resources; scripts are same-origin portal files", () => {
  for (const name of PORTAL_FILES) {
    const content = read(name).toLowerCase();
    for (const marker of ["cdn.", "cdnjs", "unpkg", "jsdelivr", "fonts.googleapis", "http://"]) {
      assert(content.indexOf(marker) === -1, name + " must not reference '" + marker + "'");
    }
  }
  for (const page of ["index.html", "reset.html"]) {
    const content = read(page);
    const scripts = content.match(/<script[^>]*src="([^"]+)"/g) || [];
    for (const tag of scripts) {
      assert(tag.indexOf('src="/static/portal/') !== -1,
        page + " script must be a same-origin portal file: " + tag);
    }
  }
});

test("audit: both pages are noindex and carry the baseline CSP meta", () => {
  for (const page of ["index.html", "reset.html"]) {
    const content = read(page);
    assert(content.indexOf('name="robots" content="noindex') !== -1, page + " must be noindex");
    assert(content.indexOf("Content-Security-Policy") !== -1, page + " must carry the CSP meta");
  }
});

test("audit: no inline event handlers, javascript: URLs, or console logging", () => {
  for (const name of PORTAL_FILES) {
    const content = read(name).toLowerCase();
    assert(!/\son[a-z]+\s*=\s*"/.test(content), name + " must not use inline event handlers");
    assert(content.indexOf("javascript:") === -1, name + " must not use javascript: URLs");
  }
  for (const name of ["portal-core.js", "portal-app.js", "portal-reset.js"]) {
    assert(read(name).indexOf("console.log(") === -1,
      name + " must not log (token leakage vector)");
  }
});

test("audit: password inputs declare correct autocomplete hints", () => {
  const login = read("index.html");
  assert(login.indexOf('autocomplete="current-password"') !== -1,
    "login password uses current-password");
  const reset = read("reset.html");
  const count = (reset.match(/autocomplete="new-password"/g) || []).length;
  assertEqual(count, 2, "both reset fields use new-password");
});

/* ------------------------------------------------------------------ */
/* Worktree byte convention                                            */
/* ------------------------------------------------------------------ */

test("audit: every shipped portal and portal-test file is pure CRLF (worktree convention)", () => {
  const testDir = __dirname;
  const testFiles = fs.readdirSync(testDir).filter((f) => f.endsWith(".js"));
  const targets = PORTAL_FILES.map((f) => path.join(PORTAL_DIR, f))
    .concat(testFiles.map((f) => path.join(testDir, f)));
  for (const filePath of targets) {
    const bytes = fs.readFileSync(filePath);
    let lonelyLf = 0;
    let lonelyCr = 0;
    for (let i = 0; i < bytes.length; i++) {
      if (bytes[i] === 0x0a && (i === 0 || bytes[i - 1] !== 0x0d)) {
        lonelyLf += 1;
      }
      if (bytes[i] === 0x0d && (i + 1 >= bytes.length || bytes[i + 1] !== 0x0a)) {
        lonelyCr += 1;
      }
    }
    assertEqual(lonelyLf, 0, path.basename(filePath) + " has LF without CR");
    assertEqual(lonelyCr, 0, path.basename(filePath) + " has lone CR");
  }
});

/* ------------------------------------------------------------------ */

(async () => {
  const summary = await h.runRegisteredTests("test_portal_static_audit");
  process.exitCode = summary.failed === 0 ? 0 : 1;
})();
