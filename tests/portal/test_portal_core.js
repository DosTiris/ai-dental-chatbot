/*
 * test_portal_core.js - behavioral suite for static/portal/portal-core.js.
 *
 * Coverage map to the P3-A requirements:
 *   (1) login + session bootstrap        -> signIn / restore tests
 *   (2) /portal/me token use             -> bootstrap tests (exact header,
 *                                           exact URL, no query string)
 *   (3) logout                           -> signOut tests
 *   (4) no public registration           -> every network call is scripted;
 *                                           an unscripted call (e.g. signup)
 *                                           fails the test; plus the static
 *                                           source audit suite
 *   (5) failed / unbound / inactive auth -> fail-closed bootstrap tests
 *   (6) reset flow                       -> recover + completePasswordSet
 *   (7) invite / activation flow         -> parseRecoveryHash invite +
 *                                           completePasswordSet
 *   (8) no browser-authoritative tenant  -> no-client_id assertions here
 *                                           plus the static source audit
 *  (10) auth-system separation           -> exact-request proofs (no admin
 *                                           key header, no calendar paths)
 * Run: node tests/portal/test_portal_core.js
 */
"use strict";

const h = require("./portal_test_harness.js");
const { test, assert, assertEqual, makeCore, expectConfigLoad, SUPABASE } = h;

const GOOD_TOKENS = {
  access_token: "ACCESS_A",
  refresh_token: "REFRESH_A",
  expires_in: 3600
};

function seedSession(env, overrides) {
  const base = {
    accessToken: "ACCESS_SEED",
    refreshToken: "REFRESH_SEED",
    expiresAtSeconds: Math.floor(env.clock() / 1000) + 3600
  };
  const session = Object.assign(base, overrides || {});
  env.storage.setItem(env.core.SESSION_STORAGE_KEY, JSON.stringify(session));
  return session;
}

/* ------------------------------------------------------------------ */
/* Runtime configuration                                               */
/* ------------------------------------------------------------------ */

test("config: backend 503 (not configured) fails visibly (no fallback)", async () => {
  const env = makeCore();
  env.fetch.expect({ urlEquals: "/portal/config" },
    { status: 503, json: { detail: "portal configuration unavailable" } });
  const result = await env.core.signIn("a@b.com", "pw");
  assertEqual(result.ok, false, "signIn must fail");
  assertEqual(result.reason, "config_missing", "reason");
  assertEqual(env.fetch.remaining(), 0, "no extra requests");
});

test("config: non-https supabase_url is rejected as invalid", async () => {
  const env = makeCore();
  env.fetch.expect({ urlEquals: "/portal/config" },
    { status: 200, json: { supabase_url: "http://plain.example", supabase_publishable_key: "k" } });
  const result = await env.core.signIn("a@b.com", "pw");
  assertEqual(result.reason, "config_invalid", "http url must be refused");
});

test("config: empty publishable key is rejected as invalid", async () => {
  const env = makeCore();
  env.fetch.expect({ urlEquals: "/portal/config" },
    { status: 200, json: { supabase_url: "https://x.supabase.co", supabase_publishable_key: "" } });
  const result = await env.core.signIn("a@b.com", "pw");
  assertEqual(result.reason, "config_invalid", "empty key must be refused");
});

test("config: loaded once and cached for subsequent calls", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  const first = await env.core.loadConfig();
  const second = await env.core.loadConfig();
  assertEqual(first.supabaseUrl, SUPABASE, "url normalized");
  assertEqual(second, first, "cached object reused");
  assertEqual(env.fetch.remaining(), 0, "exactly one config request");
});

/* ------------------------------------------------------------------ */
/* Sign-in                                                             */
/* ------------------------------------------------------------------ */

test("signIn: success stores session and sends exactly the password grant", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  env.fetch.expect(
    {
      urlEquals: SUPABASE + "/auth/v1/token?grant_type=password",
      method: "POST",
      headerEquals: { apikey: "sb_publishable_TEST_ONLY" },
      bodyJson: { email: "office@example.com", password: "pw123456" }
    },
    { status: 200, json: GOOD_TOKENS }
  );
  const result = await env.core.signIn("office@example.com", "pw123456");
  assertEqual(result.ok, true, "sign-in ok");
  const stored = env.core.readSession();
  assertEqual(stored.accessToken, "ACCESS_A", "access token stored");
  assertEqual(stored.refreshToken, "REFRESH_A", "refresh token stored");
  assertEqual(stored.expiresAtSeconds,
    Math.floor(env.clock() / 1000) + 3600, "expiry derived from expires_in");
  assertEqual(env.fetch.remaining(), 0, "no extra requests");
});

test("signIn: absolute expires_at is preferred over expires_in", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlIncludes: "grant_type=password" },
    { status: 200, json: { access_token: "A", refresh_token: "R", expires_in: 3600, expires_at: 1900000123 } });
  await env.core.signIn("a@b.com", "pw");
  assertEqual(env.core.readSession().expiresAtSeconds, 1900000123, "expires_at wins");
});

test("signIn: 400/401/403 all collapse to one generic invalid_credentials", async () => {
  for (const status of [400, 401, 403]) {
    const env = makeCore();
    expectConfigLoad(env.fetch);
    env.fetch.expect({ urlIncludes: "grant_type=password" },
      { status: status, json: { error: "server detail that must not leak" } });
    const result = await env.core.signIn("a@b.com", "bad");
    assertEqual(result.reason, "invalid_credentials", "status " + status + " is generic");
    assertEqual(env.core.readSession(), null, "no session on failure");
  }
});

test("signIn: network failure reports auth_unreachable, no session", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlIncludes: "grant_type=password" }, { networkError: true });
  const result = await env.core.signIn("a@b.com", "pw");
  assertEqual(result.reason, "auth_unreachable", "transport failure is named");
  assertEqual(env.core.readSession(), null, "no session on failure");
});

test("signIn: 200 with malformed token body is refused (fail closed)", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlIncludes: "grant_type=password" },
    { status: 200, json: { access_token: "only-half" } });
  const result = await env.core.signIn("a@b.com", "pw");
  assertEqual(result.reason, "auth_error", "malformed 200 is an error");
  assertEqual(env.core.readSession(), null, "no session stored");
});

/* ------------------------------------------------------------------ */
/* /portal/me bootstrap: the ONLY tenant authority                     */
/* ------------------------------------------------------------------ */

test("bootstrap: fresh session sends bare GET /portal/me with Bearer token only", async () => {
  const env = makeCore();
  seedSession(env);
  env.fetch.expect(
    {
      urlEquals: "/portal/me",
      method: "GET",
      headerEquals: { Authorization: "Bearer ACCESS_SEED" }
    },
    { status: 200, json: { practice_name: "Bright Smile Dental", role: "office_admin" } }
  );
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "authorized", "authorized");
  assertEqual(outcome.practiceName, "Bright Smile Dental", "practice name from server");
  const seen = env.fetch.seen();
  assertEqual(seen.length, 1, "exactly one request for a fresh session");
  assert(seen[0].url.indexOf("?") === -1, "no query string on /portal/me");
  assert(seen[0].body === undefined, "no body on /portal/me");
});

test("bootstrap: no session at all resolves signed_out with zero requests", async () => {
  const env = makeCore();
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "signed_out", "signed out");
  assertEqual(env.fetch.seen().length, 0, "no network traffic");
});

test("bootstrap: 403 (unbound or inactive user) fails closed and clears the session", async () => {
  const env = makeCore();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/me" }, { status: 403, json: { detail: "forbidden" } });
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "unauthorized", "unauthorized");
  assertEqual(env.core.readSession(), null, "session cleared");
});

test("bootstrap: 404 (tenant-opaque not-found) is indistinguishable from 403", async () => {
  const env = makeCore();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/me" }, { status: 404, json: { detail: "not found" } });
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "unauthorized", "same closed outcome as 403");
  assertEqual(env.core.readSession(), null, "session cleared");
});

test("bootstrap: 200 without a usable practice name fails closed (no shell entry)", async () => {
  const env = makeCore();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/me" }, { status: 200, json: { role: "office_admin" } });
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "bootstrap_invalid", "unusable body is refused");
  assertEqual(env.core.readSession(), null, "session cleared");
});

test("bootstrap: backend 500 keeps the session but does not enter the shell", async () => {
  const env = makeCore();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/me" }, { status: 500, json: { detail: "boom" } });
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "unavailable", "outage is named, not authorized");
  assert(env.core.readSession() !== null, "session preserved through outage");
});

test("bootstrap: backend unreachable keeps the session, state unavailable", async () => {
  const env = makeCore();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/me" }, { networkError: true });
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "unavailable", "transport failure named");
  assert(env.core.readSession() !== null, "session preserved");
});

test("bootstrap: 401 triggers exactly one refresh and retry, then succeeds", async () => {
  const env = makeCore();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/me" }, { status: 401, json: {} });
  expectConfigLoad(env.fetch);
  env.fetch.expect(
    { urlEquals: SUPABASE + "/auth/v1/token?grant_type=refresh_token",
      method: "POST", bodyJson: { refresh_token: "REFRESH_SEED" } },
    { status: 200, json: GOOD_TOKENS }
  );
  env.fetch.expect(
    { urlEquals: "/portal/me", headerEquals: { Authorization: "Bearer ACCESS_A" } },
    { status: 200, json: { practice_name: "Bright Smile Dental" } }
  );
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "authorized", "retry path authorized");
  assertEqual(env.fetch.remaining(), 0, "exact request sequence consumed");
});

test("bootstrap: 401 then failed refresh signs the user out (fail closed)", async () => {
  const env = makeCore();
  seedSession(env);
  env.fetch.expect({ urlEquals: "/portal/me" }, { status: 401, json: {} });
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlIncludes: "grant_type=refresh_token" }, { status: 401, json: {} });
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "unauthorized", "dead refresh token ends the session");
  assertEqual(env.core.readSession(), null, "session cleared");
});

test("restore after refresh: expired stored token is refreshed before /portal/me", async () => {
  const env = makeCore();
  seedSession(env, { expiresAtSeconds: Math.floor(env.clock() / 1000) - 10 });
  expectConfigLoad(env.fetch);
  env.fetch.expect(
    { urlIncludes: "grant_type=refresh_token", bodyJson: { refresh_token: "REFRESH_SEED" } },
    { status: 200, json: GOOD_TOKENS }
  );
  env.fetch.expect(
    { urlEquals: "/portal/me", headerEquals: { Authorization: "Bearer ACCESS_A" } },
    { status: 200, json: { practice_name: "Bright Smile Dental" } }
  );
  const outcome = await env.core.fetchPortalMe();
  assertEqual(outcome.state, "authorized", "restored");
  assertEqual(env.core.readSession().accessToken, "ACCESS_A", "rotated tokens stored");
});

test("refresh: concurrent callers share one in-flight request (single flight)", async () => {
  const env = makeCore();
  seedSession(env);
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlIncludes: "grant_type=refresh_token" },
    { status: 200, json: GOOD_TOKENS });
  const [a, b] = await Promise.all([env.core.refreshSession(), env.core.refreshSession()]);
  assertEqual(a.ok, true, "first ok");
  assertEqual(b.ok, true, "second ok");
  assertEqual(env.fetch.remaining(), 0, "exactly one refresh request served both");
});

/* ------------------------------------------------------------------ */
/* Corrupt stored state                                                */
/* ------------------------------------------------------------------ */

test("corrupt session JSON is cleared and treated as signed out", async () => {
  const env = makeCore();
  env.storage.setItem(env.core.SESSION_STORAGE_KEY, "{not json");
  assertEqual(env.core.readSession(), null, "unreadable -> null");
  assertEqual(env.storage.getItem(env.core.SESSION_STORAGE_KEY), null, "cleared");
});

test("structurally invalid session (missing refresh token) is cleared", async () => {
  const env = makeCore();
  env.storage.setItem(env.core.SESSION_STORAGE_KEY,
    JSON.stringify({ accessToken: "A", expiresAtSeconds: 999 }));
  assertEqual(env.core.readSession(), null, "invalid -> null");
  assertEqual(env.storage.getItem(env.core.SESSION_STORAGE_KEY), null, "cleared");
});

/* ------------------------------------------------------------------ */
/* Sign-out                                                            */
/* ------------------------------------------------------------------ */

test("signOut: local session is cleared before best-effort server logout", async () => {
  const env = makeCore();
  seedSession(env);
  expectConfigLoad(env.fetch);
  env.fetch.expect(
    { urlEquals: SUPABASE + "/auth/v1/logout", method: "POST",
      headerEquals: { Authorization: "Bearer ACCESS_SEED" } },
    { status: 204, json: {} }
  );
  const result = await env.core.signOut();
  assertEqual(result.serverLogout, true, "server revocation reported");
  assertEqual(env.core.readSession(), null, "session gone");
});

test("signOut: network failure still guarantees local sign-out", async () => {
  const env = makeCore();
  seedSession(env);
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlEquals: SUPABASE + "/auth/v1/logout" }, { networkError: true });
  const result = await env.core.signOut();
  assertEqual(result.serverLogout, false, "honest server outcome");
  assertEqual(env.core.readSession(), null, "local session cleared regardless");
});

test("signOut with no session makes zero network calls", async () => {
  const env = makeCore();
  const result = await env.core.signOut();
  assertEqual(result.serverLogout, false, "nothing to revoke");
  assertEqual(env.fetch.seen().length, 0, "no traffic");
});

/* ------------------------------------------------------------------ */
/* Password reset initiation                                           */
/* ------------------------------------------------------------------ */

test("reset request: POST /recover carries the email and allow-listed redirect", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  const redirect = encodeURIComponent("https://portal.test/static/portal/reset.html");
  env.fetch.expect(
    { urlEquals: SUPABASE + "/auth/v1/recover?redirect_to=" + redirect,
      method: "POST", bodyJson: { email: "office@example.com" } },
    { status: 200, json: {} }
  );
  const result = await env.core.requestPasswordReset("office@example.com");
  assertEqual(result.ok, true, "generic ok");
});

test("reset request: unknown email and rate limit both return the same generic ok", async () => {
  for (const status of [400, 429]) {
    const env = makeCore();
    expectConfigLoad(env.fetch);
    env.fetch.expect({ urlIncludes: "/auth/v1/recover" },
      { status: status, json: { error: "detail" } });
    const result = await env.core.requestPasswordReset("who@example.com");
    assertEqual(result.ok, true, "status " + status + " does not reveal account existence");
  }
});

test("reset request: transport failure is reported, not hidden", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlIncludes: "/auth/v1/recover" }, { networkError: true });
  const result = await env.core.requestPasswordReset("office@example.com");
  assertEqual(result.ok, false, "must not pretend an email was sent");
  assertEqual(result.reason, "auth_unreachable", "named failure");
});

/* ------------------------------------------------------------------ */
/* Recovery / invitation links                                         */
/* ------------------------------------------------------------------ */

test("hash parse: recovery link yields kind recovery with the token", () => {
  const env = makeCore();
  const parsed = env.core.parseRecoveryHash("#access_token=TOK123&type=recovery&refresh_token=R");
  assertEqual(parsed.kind, "recovery", "kind");
  assertEqual(parsed.accessToken, "TOK123", "token extracted");
});

test("hash parse: invite link yields kind invite (activation flow)", () => {
  const env = makeCore();
  const parsed = env.core.parseRecoveryHash("#access_token=TOK456&type=invite");
  assertEqual(parsed.kind, "invite", "kind");
  assertEqual(parsed.accessToken, "TOK456", "token extracted");
});

test("hash parse: Supabase error fragment is surfaced as a sanitized link_error", () => {
  const env = makeCore();
  const parsed = env.core.parseRecoveryHash(
    "#error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid+or+has+expired");
  assertEqual(parsed.kind, "link_error", "error kind");
  assert(parsed.message.indexOf("expired") !== -1, "description surfaced");
  assert(parsed.message.indexOf("<") === -1, "no angle brackets");
});

test("hash parse: token with a type outside the closed vocabulary is refused", () => {
  const env = makeCore();
  for (const type of ["signup", "magiclink", "email_change", ""]) {
    const parsed = env.core.parseRecoveryHash("#access_token=T&type=" + type);
    assertEqual(parsed.kind, "unsupported", "type '" + type + "' refused, not guessed");
  }
});

test("hash parse: empty or absent fragment yields kind empty", () => {
  const env = makeCore();
  assertEqual(env.core.parseRecoveryHash("").kind, "empty", "empty string");
  assertEqual(env.core.parseRecoveryHash("#").kind, "empty", "bare hash");
  assertEqual(env.core.parseRecoveryHash("#foo=bar").kind, "empty", "no token material");
});

test("password set: PUT /user carries the LINK token, success requires re-sign-in", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  env.fetch.expect(
    { urlEquals: SUPABASE + "/auth/v1/user", method: "PUT",
      headerEquals: { Authorization: "Bearer LINKTOKEN" },
      bodyJson: { password: "brand-new-pass" } },
    { status: 200, json: { id: "uuid" } }
  );
  const result = await env.core.completePasswordSet("LINKTOKEN", "brand-new-pass");
  assertEqual(result.ok, true, "password set");
  assertEqual(env.core.readSession(), null, "link session never persisted");
});

test("password set: expired link token maps to link_expired", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlEquals: SUPABASE + "/auth/v1/user" }, { status: 401, json: {} });
  const result = await env.core.completePasswordSet("OLD", "brand-new-pass");
  assertEqual(result.reason, "link_expired", "named outcome");
});

test("password set: 422 surfaces only the password-policy message", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlEquals: SUPABASE + "/auth/v1/user" },
    { status: 422, json: { msg: "Password should be at least 6 characters" } });
  const result = await env.core.completePasswordSet("LINK", "short");
  assertEqual(result.reason, "weak_password", "named outcome");
  assert(result.message.indexOf("6 characters") !== -1, "policy text surfaced");
});

/* ------------------------------------------------------------------ */
/* Tenant and auth-system separation (behavioral half)                 */
/* ------------------------------------------------------------------ */

test("separation: a full login+bootstrap sends no client_id, admin key, or calendar path", async () => {
  const env = makeCore();
  expectConfigLoad(env.fetch);
  env.fetch.expect({ urlIncludes: "grant_type=password" }, { status: 200, json: GOOD_TOKENS });
  await env.core.signIn("office@example.com", "pw123456");
  env.fetch.expect({ urlEquals: "/portal/me" },
    { status: 200, json: { practice_name: "Bright Smile Dental" } });
  await env.core.fetchPortalMe();
  for (const request of env.fetch.seen()) {
    const blob = request.url + " " + JSON.stringify(request.headers) + " " + String(request.body);
    assert(blob.indexOf("client_id") === -1, "no client_id anywhere: " + request.url);
    assert(blob.toLowerCase().indexOf("x-admin-key") === -1, "no operator admin header");
    assert(blob.indexOf("mia_cal_") === -1, "no Calendar admin credential marker");
    assert(request.url.indexOf("/admin/") === -1, "no operator/Calendar admin routes");
  }
  assertEqual(env.fetch.remaining(), 0, "exact expected request set only");
});

/* ------------------------------------------------------------------ */

(async () => {
  const summary = await h.runRegisteredTests("test_portal_core");
  process.exitCode = summary.failed === 0 ? 0 : 1;
})();
