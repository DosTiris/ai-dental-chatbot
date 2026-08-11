/*
 * portal-reset.js - Mia Office Portal page glue for reset.html (P3-A).
 *
 * Handles BOTH flows that arrive by email link with a token fragment:
 *   type=recovery - forgot-password completion
 *   type=invite   - invitation activation for the invite-only portal
 * The set-password rules live in portal-core.js; this file is DOM glue.
 *
 * SECURITY-CRITICAL STEP: the link token arrives in location.hash. It is
 * captured once and the fragment is immediately replaced in the address
 * bar so the token does not linger in the visible URL, and the token is
 * never written to storage. After a successful password set the user is
 * sent to the normal sign-in page - the portal is only ever entered
 * through the standard verified /portal/me path.
 */
(function () {
  "use strict";

  var MESSAGES = {
    config_missing: "The portal is not configured yet. Please contact Dos Tiris support.",
    config_invalid: "The portal configuration is invalid. Please contact Dos Tiris support.",
    link_invalid: "This link is invalid or has expired. Please request a new one from the sign-in page.",
    link_expired: "This link has expired. Please request a new one from the sign-in page.",
    auth_unreachable: "The service could not be reached. Please check your connection and try again.",
    auth_error: "The service is temporarily unavailable. Please try again shortly.",
    password_mismatch: "The two passwords do not match.",
    password_short: "Please choose a password of at least 8 characters.",
    success: "Your password has been set. You can now sign in."
  };

  /* Minimum length shown to the user before the request is sent. Supabase
   * remains the authority on password policy; a server rejection is shown
   * via the weak_password path. Named setting, not a magic value. */
  var MIN_PASSWORD_LENGTH = 8;

  var core = null;

  /* The link token lives only in this closure variable for the lifetime
   * of the page. It is never persisted. */
  var linkAccessToken = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function setStatus(text) {
    byId("reset-status").textContent = text || "";
  }

  function showForm(visible) {
    byId("reset-form").hidden = !visible;
  }

  function showDone(visible) {
    byId("reset-done").hidden = !visible;
  }

  /*
   * Purpose: classify the arrival and prepare the page.
   * State transitions: recovery/invite -> form shown; link_error,
   * unsupported, empty -> explanatory message only, no form (there is
   * nothing safe the page could do without a valid link token).
   */
  function boot() {
    var parsed = core.parseRecoveryHash(window.location.hash);

    /* Strip the fragment immediately, whatever it contained, so tokens or
     * error details are not left sitting in the address bar or history. */
    window.history.replaceState(null, "", window.location.pathname);

    showDone(false);
    if (parsed.kind === "recovery" || parsed.kind === "invite") {
      linkAccessToken = parsed.accessToken;
      byId("reset-heading").textContent = parsed.kind === "invite"
        ? "Activate your account"
        : "Set a new password";
      byId("reset-intro").textContent = parsed.kind === "invite"
        ? "Welcome to the Mia Office Portal. Choose a password to activate your account."
        : "Choose a new password for your Mia Office Portal account.";
      showForm(true);
      setStatus("");
      return;
    }
    showForm(false);
    if (parsed.kind === "link_error") {
      setStatus(parsed.message || MESSAGES.link_invalid);
      return;
    }
    /* "empty" and "unsupported" both mean: no usable link material. */
    setStatus(MESSAGES.link_invalid);
  }

  function onSubmit(event) {
    event.preventDefault();
    var password = byId("reset-password").value;
    var confirm = byId("reset-password-confirm").value;
    if (password.length < MIN_PASSWORD_LENGTH) {
      setStatus(MESSAGES.password_short);
      return;
    }
    if (password !== confirm) {
      setStatus(MESSAGES.password_mismatch);
      return;
    }
    setStatus("");
    byId("reset-submit").disabled = true;
    core.completePasswordSet(linkAccessToken, password).then(function (result) {
      byId("reset-submit").disabled = false;
      if (result.ok) {
        /* Clear the fields and the closure token: the link is spent. */
        byId("reset-password").value = "";
        byId("reset-password-confirm").value = "";
        linkAccessToken = null;
        showForm(false);
        showDone(true);
        setStatus(MESSAGES.success);
        return;
      }
      if (result.reason === "weak_password" && result.message) {
        setStatus(result.message);
        return;
      }
      setStatus(MESSAGES[result.reason] || MESSAGES.auth_error);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    core = window.createMiaPortalCore({
      fetchImpl: window.fetch.bind(window),
      storage: window.localStorage,
      nowFn: Date.now,
      windowOrigin: window.location.origin
    });
    byId("reset-form").addEventListener("submit", onSubmit);
    boot();
  });

}());
