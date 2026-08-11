/*
 * portal-app.js - Mia Office Portal page glue for index.html (P3-A).
 *
 * OWNERSHIP (Constitution 5): this file only connects DOM elements to
 * portal-core.js. Every auth, session, and tenant rule lives in
 * portal-core.js; nothing here reads tokens, storage keys, or tenant
 * fields directly.
 *
 * VIEW STATES (closed set, one visible at a time):
 *   view-loading  - restoring a session on page load
 *   view-login    - email + password sign-in
 *   view-forgot   - request a password reset email
 *   view-shell    - the authenticated portal shell (practice name + logout)
 * Transitions are driven exclusively by portal-core outcomes; no view is
 * ever entered from browser state alone.
 */
(function () {
  "use strict";

  /* User-facing messages in one place so wording stays reviewable.
   * Sign-in failures are deliberately generic (no account probing). */
  var MESSAGES = {
    config_missing: "The portal is not configured yet. Please contact Dos Tiris support.",
    config_invalid: "The portal configuration is invalid. Please contact Dos Tiris support.",
    invalid_credentials: "That email and password combination was not accepted.",
    auth_unreachable: "The sign-in service could not be reached. Please check your connection and try again.",
    auth_error: "Sign-in is temporarily unavailable. Please try again shortly.",
    unauthorized: "This account is not authorized for the Mia Office Portal.",
    unavailable: "The portal service is temporarily unavailable. Please try again shortly.",
    bootstrap_invalid: "The portal could not verify this account. Please sign in again.",
    signed_out: "You have been signed out.",
    reset_sent: "If an account exists for that email, a password reset link has been sent.",
    session_expired: "Your session has expired. Please sign in again."
  };

  var core = null;

  function byId(id) {
    return document.getElementById(id);
  }

  /* Show exactly one view; hide the rest (single-meaning state). */
  function showView(viewId) {
    var views = ["view-loading", "view-login", "view-forgot", "view-shell"];
    for (var i = 0; i < views.length; i++) {
      byId(views[i]).hidden = views[i] !== viewId;
    }
  }

  /* One status line per page area; text only (never HTML) so server or
   * user supplied strings cannot inject markup. */
  function setStatus(elementId, text) {
    byId(elementId).textContent = text || "";
  }

  function setBusy(buttonId, busy) {
    var el = byId(buttonId);
    el.disabled = !!busy;
  }

  /*
   * Purpose: route a portal-core bootstrap outcome to a view.
   * "authorized" is the ONLY outcome that enters the shell; every other
   * outcome lands on the login view with an honest message (fail closed).
   */
  function routeBootstrap(outcome, loginMessage) {
    if (outcome.state === "authorized") {
      byId("shell-practice-name").textContent = outcome.practiceName;
      setStatus("shell-status", "");
      showView("view-shell");
      return;
    }
    showView("view-login");
    if (outcome.state === "signed_out") {
      setStatus("login-status", loginMessage || "");
      return;
    }
    setStatus("login-status", MESSAGES[outcome.state] || MESSAGES.auth_error);
  }

  /*
   * Purpose: page-load session restoration.
   * A stored session is verified against /portal/me before ANY portal
   * content is shown; the shell is never rendered from cached data.
   */
  function boot() {
    showView("view-loading");
    core.loadConfig().then(function (cfg) {
      if (cfg.error) {
        showView("view-login");
        setStatus("login-status", MESSAGES[cfg.error]);
        /* Without configuration the form cannot work; disable it so the
         * failure is visible instead of a dead submit button mystery. */
        setBusy("login-submit", true);
        return;
      }
      core.fetchPortalMe().then(function (outcome) {
        routeBootstrap(outcome, "");
      });
    });
  }

  /* Sign-in submit: verify credentials with Supabase, then bootstrap the
   * tenant through /portal/me. A valid password with no active office
   * binding still lands back on login (fail closed). */
  function onLoginSubmit(event) {
    event.preventDefault();
    var email = byId("login-email").value.trim();
    var password = byId("login-password").value;
    if (email === "" || password === "") {
      setStatus("login-status", "Please enter your email and password.");
      return;
    }
    setStatus("login-status", "");
    setBusy("login-submit", true);
    core.signIn(email, password).then(function (result) {
      if (!result.ok) {
        setBusy("login-submit", false);
        setStatus("login-status", MESSAGES[result.reason] || MESSAGES.auth_error);
        return;
      }
      core.fetchPortalMe().then(function (outcome) {
        setBusy("login-submit", false);
        byId("login-password").value = "";
        routeBootstrap(outcome, "");
      });
    });
  }

  /* Forgot-password submit: always shows the same generic confirmation on
   * any Supabase response (anti-enumeration rule lives in portal-core). */
  function onForgotSubmit(event) {
    event.preventDefault();
    var email = byId("forgot-email").value.trim();
    if (email === "") {
      setStatus("forgot-status", "Please enter your email address.");
      return;
    }
    setBusy("forgot-submit", true);
    core.requestPasswordReset(email).then(function (result) {
      setBusy("forgot-submit", false);
      if (!result.ok) {
        setStatus("forgot-status", MESSAGES[result.reason] || MESSAGES.auth_error);
        return;
      }
      setStatus("forgot-status", MESSAGES.reset_sent);
    });
  }

  /* Logout: local session death is guaranteed by portal-core before the
   * best-effort server revocation; the UI returns to login either way. */
  function onLogoutClick() {
    setBusy("shell-logout", true);
    core.signOut().then(function () {
      setBusy("shell-logout", false);
      byId("login-password").value = "";
      showView("view-login");
      setStatus("login-status", MESSAGES.signed_out);
    });
  }

  function wireEvents() {
    byId("login-form").addEventListener("submit", onLoginSubmit);
    byId("forgot-form").addEventListener("submit", onForgotSubmit);
    byId("shell-logout").addEventListener("click", onLogoutClick);
    byId("login-forgot-link").addEventListener("click", function (event) {
      event.preventDefault();
      setStatus("forgot-status", "");
      showView("view-forgot");
    });
    byId("forgot-back-link").addEventListener("click", function (event) {
      event.preventDefault();
      setStatus("login-status", "");
      showView("view-login");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    core = window.createMiaPortalCore({
      fetchImpl: window.fetch.bind(window),
      storage: window.localStorage,
      nowFn: Date.now,
      windowOrigin: window.location.origin
    });
    wireEvents();
    boot();
  });

}());
