/*
 * run_portal_ui_tests.js - runs both P3-A portal UI suites as child
 * processes and exits nonzero if either fails. Kept as separate processes
 * so one suite's registry cannot bleed into the other.
 *
 * Run: node tests/portal/run_portal_ui_tests.js
 */
"use strict";

const { spawnSync } = require("child_process");
const path = require("path");

const suites = [
  "test_portal_core.js",
  "test_portal_static_audit.js",
  "test_portal_data.js",    /* P3-B1 */
  "test_portal_pages.js",   /* P3-B1 */
  "test_portal_schedule_page.js",  /* P4-A */
  "test_portal_appointment_actions_page.js",  /* P5-A */
  "test_portal_notification_settings_page.js",  /* P6-A */
  "test_portal_recurring_schedule_page.js",  /* P4-B */
  "test_portal_calendar_page.js"  /* Visual Calendar Phase 1 */
];

let failed = 0;
for (const suite of suites) {
  const suitePath = path.join(__dirname, suite);
  console.log("== " + suite + " ==");
  const result = spawnSync(process.execPath, [suitePath], { stdio: "inherit" });
  if (result.status !== 0) {
    failed += 1;
  }
}

if (failed > 0) {
  console.log("PORTAL UI TESTS: FAILED (" + failed + " suite(s) failed)");
  process.exitCode = 1;
} else {
  console.log("PORTAL UI TESTS: ALL SUITES PASSED");
}
