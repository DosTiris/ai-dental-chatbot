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

/* P2-A: the calendar drawer now performs the EXISTING P5-A appointment
 * actions, but the pure renderer must stay entirely out of it. Mutation
 * orchestration, the action vocabulary and the data methods all live in
 * portal-pages.js; portal-calendar.js may not even name them. */
test("audit: portal-calendar owns no mutation logic", () => {
  const content = read("portal-calendar.js");
  for (const name of ["confirmAppointment", "cancelAppointment",
    "appointmentActionsFor", "actionBusy", "generation", "armed"]) {
    assert(content.indexOf(name) === -1,
      "portal-calendar.js must not reference " + name);
  }
});

/* P2-A: the two action methods the drawer uses must be the ALREADY
 * allow-listed P5-A ones. This is the byte-level proof that no new
 * endpoint or network owner was introduced alongside the new capability. */
test("audit: the drawer actions reuse the existing data-layer methods", () => {
  const pages = read("portal-pages.js");
  assert(pages.indexOf("data.confirmAppointment") !== -1,
    "Confirm must go through the existing data owner");
  assert(pages.indexOf("data.cancelAppointment") !== -1,
    "Cancel must go through the existing data owner");
  /* SLICE 4B2 / SLICE 4C - DELIBERATE allow-list growth (the documented
   * closed-list mechanism): the data owner now also declares the reviewed
   * 4B1 internal-note endpoint and the reviewed 4C restore / reschedule /
   * restore-to-slot actions (v1.0.1 mode pin F1: Change time and Choose
   * another time are DIFFERENT server commands). Confirm, cancel,
   * internal-note, restore, reschedule, and restore-to-slot are the ONLY
   * appointment mutations; anything else remains a loud failure. */
  const dataFile = read("portal-data.js");
  const posts = dataFile.match(/\/portal\/appointments\/[^"\x27]*/g) || [];
  for (const path of posts) {
    assert(path.indexOf("confirm") !== -1 || path.indexOf("cancel") !== -1 ||
      path.indexOf("internal-note") !== -1 ||
      path.indexOf("restore") !== -1 || path.indexOf("reschedule") !== -1 ||
      path.indexOf("{") !== -1 || path === "/portal/appointments",
      "unexpected appointment endpoint in the data owner: " + path);
  }
  assert(pages.indexOf("data.setAppointmentInternalNote") !== -1,
    "the note save must go through the existing data owner");
  /* SLICE 4C: the drawer's restore and reschedule must also go through
   * the ONE data owner - never a second request pathway. */
  assert(pages.indexOf("data.restoreAppointment") !== -1,
    "Restore must go through the existing data owner");
  assert(pages.indexOf("data.rescheduleAppointment") !== -1,
    "Reschedule must go through the existing data owner");
  /* v1.0.1 (F1): the cancelled-recovery move is its OWN server command
   * and must ALSO go through the one data owner. */
  assert(pages.indexOf("data.restoreAppointmentToSlot") !== -1,
    "Choose another time must go through the existing data owner");
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
/* PHASE 3A SLICE 3 - DELIBERATE closed-list amendment (the same growth
 * mechanism the endpoint allow-list documents): the owner-approved Slice 3
 * contract authorizes EXACTLY ONE new capability on this surface - booking
 * an Open band's real slot through the receptionist panel - so the wording
 * "Book appointment" and the panel's two markup buttons (close + submit)
 * are now authorized. Everything else stays forbidden: reschedule and
 * duplicate remain out of scope, and appointment CREATION language beyond
 * booking an EXISTING authoritative slot remains banned, because the
 * browser must never imply it can invent inventory. The control count
 * stays an EXACT pin so an unreviewed button can never slip in. */
test("audit: the calendar surface renders no unauthorized action vocabulary", () => {
  const html = read("index.html");
  const start = html.indexOf('id="page-calendar"');
  assert(start !== -1, "index.html must contain the page-calendar section");
  const end = html.indexOf("</section>", start);
  assert(end !== -1, "page-calendar section must be closed");
  const section = html.slice(start, end);
  for (const word of ["Reschedule", "Duplicate", "New appointment",
    "Create appointment"]) {
    assert(section.indexOf(word) === -1,
      "page-calendar markup must not offer " + word);
  }
  /* Exactly the FOURTEEN reviewed controls (SLICE 4D-A amendment, the same
   * closed-list mechanism): week prev/next, refresh, the drawer close,
   * the booking panel's close + submit, the drawer note section's
   * Edit / Save / Cancel, the 4C reschedule picker's Save new time /
   * Cancel, and the 4D-A availability panel's open ("Add availability"
   * toolbar button) + close + submit. The 4D-A affordance opens
   * AVAILABILITY (authoritative inventory) - never an appointment row
   * directly - which is why the forbidden-word pin above still refuses
   * "New appointment" / "Create appointment" over this section. The
   * action buttons themselves (Confirm, Cancel appointment, Restore
   * original time, Change time / Choose another time) are rendered by the
   * reviewed drawer builder from the calendar action set, never declared
   * in markup. Still an exact pin - an unreviewed button can never slip
   * in. */
  /* Slice 4D-B adds three reviewed controls: the "Close day" toolbar
   * button and the Close/Reopen panel's close + submit. The submit's
   * resting label "Close or reopen" names a DAY-state action (the panel
   * mutates closure state and never an appointment), so the forbidden
   * appointment-vocabulary pin above remains satisfied. */
  /* Slice 4D-C adds three reviewed controls: the "Weekly schedule" toolbar
   * button and the read-only panel's close + "Open Recurring settings"
   * navigation CTA. None of them mutates anything. */
  const buttons = section.match(/<button/g) || [];
  assert(buttons.length === 20,
    "expected exactly the twenty reviewed controls, found " + buttons.length);
});

/* PHASE 3A Slice 3: the booking submit must go through the existing data
 * owner; the pure renderer may not even name the method; and the booking
 * path must stay DERIVED from the one schedule literal - no new endpoint
 * literal may appear (the multi-segment shape would evade the single-
 * segment allow-list regex above, so it is pinned here explicitly). */
/* v1.0.2 HOTFIX: the history and blocks layers span the whole canvas
 * ABOVE the availability bands, and a full-canvas positioned layer wins
 * real-browser hit-testing even where it is fully transparent - Node
 * .click() dispatch cannot model that, which is how v1.0.1 shipped Open
 * bands a mouse could not reach. This bite pins the invariant: every
 * full-canvas FOREGROUND layer must opt out of hit-testing, every real
 * control inside one must opt back in, and the background bands layer
 * keeps default hit-testing (its buttons need no re-enable, and blank
 * canvas stays inert because the layer itself has no handler). */
test("audit: foreground calendar layers never consume clicks meant for layers beneath", () => {
  const css = read("portal.css");
  function ruleOf(selector) {
    const at = css.indexOf(selector);
    assert(at !== -1, selector + " rule must exist");
    return css.slice(at, css.indexOf("}", at));
  }
  for (const layer of [".portal-calendar-history {",
    ".portal-calendar-blocks {", ".portal-calendar-history-strips {"]) {
    assert(ruleOf(layer).indexOf("pointer-events: none") !== -1,
      layer + " is a full-canvas foreground layer and must not hit-test");
  }
  assert(ruleOf(".portal-calendar-block {").indexOf("pointer-events: auto") !== -1,
    "live appointments AND cancelled ghosts share this class and must re-enable clicks");
  assert(ruleOf(".portal-calendar-history-strip {").indexOf("pointer-events: auto") !== -1,
    "the history strip control must stay clickable (frozen pattern)");
  assert(ruleOf(".portal-calendar-bands {").indexOf("pointer-events") === -1,
    "the BACKGROUND bands layer keeps default hit-testing - making it " +
    "none would orphan the Open-band buttons unless they re-enabled, " +
    "and making the whole canvas clickable is equally forbidden");
  /* The z-order priority is a stated rule, not an accident: the fix must
   * never be a re-stack. */
  assert(ruleOf(".portal-calendar-history {").indexOf("z-index: 1") !== -1,
    "history stays above bands");
  assert(ruleOf(".portal-calendar-blocks {").indexOf("z-index: 2") !== -1,
    "live appointments stay above history");
  assert(ruleOf(".portal-calendar-history-strips {").indexOf("z-index: 3") !== -1,
    "strips stay above live appointments");
});

/* v1.0.1 F2: turning the Open band into a <button> added a (0,1,1) reset
 * with border: 0, which silently out-specified the (0,1,0) availability
 * rule and erased the approved thin left accent. The availability
 * treatment now carries BOTH band classes (0,2,0), so the reset can never
 * win again. This bite pins the pair: the strengthened selector must
 * exist WITH its accent, and the button reset must keep border: 0 (a
 * bare UA button border on the calendar canvas is its own regression). */
test("audit: the Open band keeps the approved availability accent as a button", () => {
  const css = read("portal.css");
  const availableAt = css.indexOf(".portal-calendar-band.portal-calendar-band-available {");
  assert(availableAt !== -1,
    "the availability treatment must carry BOTH band classes (0,2,0)");
  const availableRule = css.slice(availableAt, css.indexOf("}", availableAt));
  assert(availableRule.indexOf("border-left: 2px solid") !== -1,
    "the thin left accent must survive on the strengthened selector");
  const resetAt = css.indexOf("button.portal-calendar-band {");
  assert(resetAt !== -1, "the button reset must exist");
  const resetRule = css.slice(resetAt, css.indexOf("}", resetAt));
  assert(resetRule.indexOf("border: 0") !== -1,
    "the reset must keep suppressing the UA button border");
  assert(resetRule.indexOf("border-left") === -1,
    "the accent has ONE owner - the availability rule, never the reset");
  const weakAvailable = css.indexOf("\n.portal-calendar-band-available {");
  assert(weakAvailable === -1,
    "no weak (0,1,0) availability rule may return and lose to the reset");
});

test("audit: the booking panel reuses the existing data owner", () => {
  const pages = read("portal-pages.js");
  assert(pages.indexOf("data.bookScheduleSlot") !== -1,
    "booking must go through the existing data owner");
  const calendarSource = read("portal-calendar.js");
  assert(calendarSource.indexOf("bookScheduleSlot") === -1,
    "portal-calendar.js must not name the booking method");
  const dataFile = read("portal-data.js");
  const bookingLiterals = dataFile.match(/"\/portal\/schedule\/slots[a-z/-]*"/g) || [];
  assertEqual(bookingLiterals.length, 0,
    "the booking path is concatenated from the one schedule literal");
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
  /* 4C: portal-calendar.js is now version-tokened as well (its ghost
   * hit-target fix must never pair with a stale cached orchestrator);
   * the ORDER rule is unchanged. */
  const calendarAt = content.indexOf('src="/static/portal/portal-calendar.js?');
  /* 4B2: portal-pages.js is version-tokened; the ORDER rule is unchanged. */
  const pagesAt = content.indexOf('src="/static/portal/portal-pages.js?');
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

/* Slice 4B1 F2 established the rule; SLICE 4B2 amends the token set
 * DELIBERATELY (the same mechanism): pages, data, and the stylesheet all
 * change together in 4B2, so each carries the SAME deterministic token -
 * production can never mix old and new UI code. Untouched assets (core,
 * calendar, app) must stay token-free, and no nondeterministic
 * (timestamp-like) token is ever permitted. A future incompatible change
 * cannot ship without consciously re-facing this exact pin. */
/* SLICE 4B2: internal notes are OFFICE-ONLY, so their inputs are pinned:
 * both textareas carry the 2000-character UX assist (the backend limit
 * stays authoritative), and the note travels ONLY in the authenticated
 * request body - portal-pages may never place it in a URL, a DOM data
 * attribute, or browser storage. */
test("audit: internal-note inputs are bounded and the note stays out of URLs/storage", () => {
  const html = read("index.html");
  for (const id of ["calendar-book-note", "calendar-drawer-note-input"]) {
    const at = html.indexOf('id="' + id + '"');
    assert(at !== -1, id + " must exist");
    const tagStart = html.lastIndexOf("<textarea", at);
    const tagEnd = html.indexOf(">", at);
    const tag = html.slice(tagStart, tagEnd);
    assert(tagStart !== -1 && tag.indexOf('maxlength="2000"') !== -1,
      id + " must be a textarea with maxlength 2000");
    assert(tag.indexOf("data-") === -1,
      id + " must carry no data attributes");
  }
  const pages = read("portal-pages.js");
  for (const banned of ["localStorage", "sessionStorage",
    "console.log", "internal_note="]) {
    assert(pages.indexOf(banned) === -1,
      "portal-pages.js must not contain '" + banned +
      "' (notes live only in the authenticated runtime and request body)");
  }
});

test("audit: the 4D-A assets carry the exact deterministic cache-bust tokens", () => {
  const html = read("index.html");
  /* 4D-A: one shared token for the versioned asset set. This slice changed
   * data (createOneOffAvailability), pages (the availability panel), and
   * index.html markup; portal-calendar.js and portal.css are byte-unchanged
   * but stay pinned WITH the bundle (the 4C rule: a stale cached member of
   * this set must never pair with a fresh one across the deployment
   * boundary). */
  const TOKEN = "4dc-weekly-view-v1";
  for (const versioned of [
    '<script src="/static/portal/portal-data.js?v=' + TOKEN + '"></script>',
    '<script src="/static/portal/portal-calendar.js?v=' + TOKEN + '"></script>',
    '<script src="/static/portal/portal-pages.js?v=' + TOKEN + '"></script>',
    '<link rel="stylesheet" href="/static/portal/portal.css?v=' + TOKEN + '" />'
  ]) {
    assert(html.indexOf(versioned) !== -1,
      "a 4C-modified asset must ship with the exact token: " + versioned);
  }
  const scripts = html.match(/<script[^>]*src="([^"]+)"/g) || [];
  for (const tag of scripts) {
    if (tag.indexOf("portal-data.js") !== -1 ||
        tag.indexOf("portal-calendar.js") !== -1 ||
        tag.indexOf("portal-pages.js") !== -1) { continue; }
    assert(tag.indexOf("?") === -1,
      "an untouched asset must not carry a query token: " + tag);
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

/* ==========================================================================
 * PHASE 3A SLICE 4D-B.1 - closed-day polish pins.
 * ======================================================================== */

test("audit 4D-B.1: the day toolbar control says exactly Close/reopen day", () => {
  const html = read("index.html");
  assert(html.includes('id="calendar-close-open"'),
    "the day toolbar control exists");
  const match = html.match(
    /id="calendar-close-open"[^>]*>([^<]*)<\/button>/);
  assert(match !== null, "the control has readable label markup");
  assert(match[1].trim() === "Close/reopen day",
    "label must be exactly 'Close/reopen day', found '" +
    (match ? match[1].trim() : "") + "'");
});

test("audit 4D-B.1: the closed-day styling stays neutral - no danger palette", () => {
  const css = read("portal.css");
  const start = css.indexOf("SLICE 4D-B.1");
  assert(start !== -1, "the 4D-B.1 styling block exists");
  const block = css.slice(start);
  for (const forbidden of ["red", "#d9534f", "#dc3545", "#c00", "crimson",
    "danger"]) {
    assert(!block.toLowerCase().includes(forbidden),
      "closed-day styling must not use danger styling: " + forbidden);
  }
});

/* ==========================================================================
 * PHASE 3A SLICE 4D-C - Weekly schedule (read-only) pins.
 * ======================================================================== */

test("audit 4D-C: the toolbar control says exactly Weekly schedule", () => {
  const html = read("index.html");
  const match = html.match(
    /id="calendar-weekly-open"[^>]*>([^<]*)<\/button>/);
  assert(match !== null, "the Weekly schedule control exists");
  assert(match[1].trim() === "Weekly schedule",
    "label must be exactly 'Weekly schedule', found '" +
    match[1].trim() + "'");
});

test("audit 4D-C: the planned-closure terminology and Save/Apply copy are pinned verbatim", () => {
  const html = read("index.html");
  assert(html.includes("Planned closures &mdash; take effect on Apply"),
    "the exact planned-closures heading is present");
  assert(html.includes(
    "Saving updates the recurring plan only. It does not change the times offered for booking. Apply attempts to materialize the saved plan across the booking horizon. Days that already contain scheduled inventory may be skipped, and booked appointments are never changed."),
    "the Save/Apply explanation is present verbatim");
  assert(html.includes(
    "Planned closures are different from Close/reopen day. Close/reopen day takes effect immediately."),
    "the 4D-B distinction is present verbatim");
});

test("audit 4D-C: the Weekly schedule panel reproduces NO mutation controls", () => {
  const html = read("index.html");
  const start = html.indexOf('id="calendar-weekly"');
  assert(start !== -1, "the panel exists");
  const end = html.indexOf("</aside>", start);
  const block = html.slice(start, end);
  const buttons = block.match(/<button/g) || [];
  assert(buttons.length === 2,
    "exactly two controls (Close + navigation CTA), found " + buttons.length);
  for (const forbidden of [">Save<", ">Preview<", ">Apply<",
    'id="recurring-save"', 'id="recurring-preview"', 'id="recurring-apply"']) {
    assert(!block.includes(forbidden),
      "the read-only panel must not reproduce: " + forbidden);
  }
});
