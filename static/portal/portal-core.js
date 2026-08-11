/*
 * portal-core.js - Mia Office Portal frontend auth/session core (P3-A).
 *
 * SINGLE OWNER (Constitution 5): this file is the only owner of browser-side
 * portal session state, Supabase Auth (GoTrue) REST calls, and the
 * server-authoritative tenant bootstrap call to GET /portal/me.
 * portal-app.js and portal-reset.js are DOM glue only and contain no
 * auth rules.
 *
 * ARCHITECTURE (P2 constraints, frozen):
 *  - Supabase Auth owns passwords and sessions. This file never sends a
 *    password anywhere except Supabase's own /auth/v1 endpoints.
 *  - The Mia backend never sees a password and holds no service-role key.
 *  - Tenant identity comes ONLY from the verified /portal/me response.
 *    Nothing in this file reads, writes, or transmits any tenant or
 *    office identifier of its own.
 *  - Only the PUBLIC Supabase URL and PUBLIC publishable key are used in
 *    the browser. They are loaded at runtime from GET /portal/config,
 *    which is created server-side and is never committed with real values.
 *
 * TESTABILITY: createMiaPortalCore() takes injected dependencies
 * (fetchImpl, storage, nowFn, windowOrigin) so the Node test harness can
 * drive every path without a browser. No DOM access occurs in this file.
 */
(function (globalScope) {
  "use strict";

  /* Storage key for the persisted session. Versioned so a future format
   * change can migrate or discard old sessions explicitly instead of
   * misreading them (Constitution 10: invalid stored state is defined). */
  var SESSION_STORAGE_KEY = "mia_portal_session_v1";

  /* The one field of the /portal/me response used for the shell heading.
   * CONFIRMED CONTRACT (D1 resolved by independent audit against the
   * frozen P2 baseline): PortalMeView.practice_name.
   * If the field is absent the core still FAILS CLOSED (no shell entry). */
  var PRACTICE_NAME_FIELD = "practice_name";

  /* Refresh the access token when it has less than this many seconds of
   * life left. Named setting, not a magic value (Constitution 4.5). */
  var TOKEN_REFRESH_SKEW_SECONDS = 60;

  /* Where the runtime public browser config is served from: the backend
   * endpoint GET /portal/config (F-P3A-1). It returns exactly the two
   * public values and fails closed with 503 when the server environment
   * is not configured - there is no static config file. */
  var PORTAL_CONFIG_URL = "/portal/config";

  /* Backend bootstrap endpoint (P2, frozen). Relative URL: the portal is
   * served by the same FastAPI app, so the call is same-origin by design.
   * Serving the portal from another origin is explicitly out of scope for
   * P3-A (would require a CORS and base-URL decision). */
  var PORTAL_ME_URL = "/portal/me";

  /* Closed vocabulary of link types accepted on the reset page
   * (Constitution 4.5 / closed vocabularies): anything else is rejected
   * as "unsupported", never guessed at. */
  var SUPPORTED_LINK_TYPES = { recovery: true, invite: true };

  function createMiaPortalCore(deps) {
    if (!deps || typeof deps.fetchImpl !== "function" ||
        !deps.storage || typeof deps.nowFn !== "function") {
      /* Wiring error by the caller, not a user-facing state. Fail loudly. */
      throw new Error("createMiaPortalCore: fetchImpl, storage and nowFn are required");
    }

    var fetchImpl = deps.fetchImpl;
    var storage = deps.storage;          /* localStorage-shaped: getItem/setItem/removeItem */
    var nowFn = deps.nowFn;              /* returns epoch milliseconds */
    var windowOrigin = deps.windowOrigin || ""; /* e.g. https://beta.dostiris.com */

    /* Loaded once by loadConfig(); every Supabase call requires it. */
    var runtimeConfig = null;

    /* Single-flight guard so concurrent callers share one refresh request
     * instead of racing Supabase with the same refresh token
     * (Constitution 10: duplicate execution guarded). */
    var refreshInFlight = null;

    /* ------------------------------------------------------------------ */
    /* Runtime public configuration                                        */
    /* ------------------------------------------------------------------ */

    /*
     * Purpose: load and validate the PUBLIC browser configuration.
     * Inputs: none (fetches PORTAL_CONFIG_URL).
     * Returns: { supabaseUrl, supabasePublishableKey } on success.
     * Failure: returns { error: "config_missing" | "config_invalid" }.
     *   There is no fallback configuration on purpose: a missing or
     *   malformed backend configuration (503) must be visible, never
     *   silently guessed
     *   (Constitution 4.5).
     * External effects: one same-origin GET; nothing stored.
     */
    function loadConfig() {
      if (runtimeConfig) {
        return Promise.resolve(runtimeConfig);
      }
      return fetchImpl(PORTAL_CONFIG_URL, { method: "GET", cache: "no-store" })
        .then(function (res) {
          if (!res || res.status !== 200) {
            return { error: "config_missing" };
          }
          return res.json().then(function (body) {
            var url = body && typeof body.supabase_url === "string"
              ? body.supabase_url.trim() : "";
            var key = body && typeof body.supabase_publishable_key === "string"
              ? body.supabase_publishable_key.trim() : "";
            /* The Supabase URL must be https to guarantee credentials are
             * never sent over plaintext from the login form. */
            if (url.indexOf("https://") !== 0 || key === "") {
              return { error: "config_invalid" };
            }
            /* Normalize: no trailing slash so path joins are exact. */
            while (url.charAt(url.length - 1) === "/") {
              url = url.slice(0, -1);
            }
            runtimeConfig = { supabaseUrl: url, supabasePublishableKey: key };
            return runtimeConfig;
          }).catch(function () {
            return { error: "config_invalid" };
          });
        })
        .catch(function () {
          return { error: "config_missing" };
        });
    }

    /* ------------------------------------------------------------------ */
    /* Session persistence                                                 */
    /* ------------------------------------------------------------------ */

    /*
     * Purpose: read the persisted session.
     * Returns: { accessToken, refreshToken, expiresAtSeconds } or null.
     * Failure behavior: a malformed stored value is CLEARED and treated as
     *   signed-out. Corrupt state must not trap the portal
     *   (Constitution 10: invalid stored state).
     */
    function readSession() {
      var raw = storage.getItem(SESSION_STORAGE_KEY);
      if (!raw) {
        return null;
      }
      var parsed = null;
      try {
        parsed = JSON.parse(raw);
      } catch (e) {
        parsed = null;
      }
      if (!parsed ||
          typeof parsed.accessToken !== "string" || parsed.accessToken === "" ||
          typeof parsed.refreshToken !== "string" || parsed.refreshToken === "" ||
          typeof parsed.expiresAtSeconds !== "number" ||
          !isFinite(parsed.expiresAtSeconds)) {
        /* Explicitly discard the unreadable session rather than acting on it. */
        clearSession();
        return null;
      }
      return parsed;
    }

    /* Persist a validated session. Only tokens and expiry are stored -
     * never the practice name, role, or any tenant identifier, so nothing
     * tenant-authoritative can ever be replayed from browser storage. */
    function writeSession(session) {
      storage.setItem(SESSION_STORAGE_KEY, JSON.stringify({
        accessToken: session.accessToken,
        refreshToken: session.refreshToken,
        expiresAtSeconds: session.expiresAtSeconds
      }));
    }

    function clearSession() {
      storage.removeItem(SESSION_STORAGE_KEY);
    }

    /*
     * Purpose: turn a GoTrue token response into a stored session.
     * Business rule: expiry derivation is an explicit ordered choice, not a
     * silent fallback - prefer the server's absolute expires_at (seconds),
     * otherwise compute now + expires_in. If neither exists the response
     * is malformed and the session is REJECTED (fail closed).
     */
    function sessionFromTokenResponse(body) {
      if (!body || typeof body.access_token !== "string" ||
          typeof body.refresh_token !== "string") {
        return null;
      }
      var expiresAtSeconds = null;
      if (typeof body.expires_at === "number" && isFinite(body.expires_at)) {
        expiresAtSeconds = body.expires_at;
      } else if (typeof body.expires_in === "number" && isFinite(body.expires_in)) {
        expiresAtSeconds = Math.floor(nowFn() / 1000) + body.expires_in;
      }
      if (expiresAtSeconds === null) {
        return null;
      }
      return {
        accessToken: body.access_token,
        refreshToken: body.refresh_token,
        expiresAtSeconds: expiresAtSeconds
      };
    }

    /* ------------------------------------------------------------------ */
    /* Supabase Auth (GoTrue) REST calls                                   */
    /* ------------------------------------------------------------------ */

    /*
     * Purpose: one owner for every Supabase Auth request.
     * Inputs: cfg (validated config), path under /auth/v1, options:
     *   method, bodyObject (JSON-encoded), accessToken (optional Bearer).
     * Returns: { status, body } where body is parsed JSON or null.
     * Failure: network-level failure resolves to { status: 0, body: null }
     *   so callers can distinguish "could not reach Supabase" from an
     *   auth rejection and show an honest message (Constitution 14).
     * External effects: one HTTPS request to the office's Supabase project.
     */
    function gotrueRequest(cfg, path, options) {
      var headers = {
        "apikey": cfg.supabasePublishableKey,
        "Content-Type": "application/json"
      };
      if (options.accessToken) {
        headers["Authorization"] = "Bearer " + options.accessToken;
      }
      var init = { method: options.method, headers: headers, cache: "no-store" };
      if (options.bodyObject !== undefined) {
        init.body = JSON.stringify(options.bodyObject);
      }
      return fetchImpl(cfg.supabaseUrl + "/auth/v1" + path, init)
        .then(function (res) {
          if (!res) {
            return { status: 0, body: null };
          }
          return res.json().then(
            function (parsed) { return { status: res.status, body: parsed }; },
            function () { return { status: res.status, body: null }; }
          );
        })
        .catch(function () {
          return { status: 0, body: null };
        });
    }

    /*
     * Purpose: email + password sign-in.
     * Returns: { ok: true } after persisting the session, or
     *   { ok: false, reason: "config_missing" | "config_invalid" |
     *     "invalid_credentials" | "auth_unreachable" | "auth_error" }.
     * Business rule: credential rejections (400/401/403) all collapse to
     *   ONE generic reason so the login form cannot be used to probe which
     *   emails exist or which part of a credential was wrong.
     * External effects: POST to Supabase token endpoint; session storage
     *   write on success. The password is never stored or logged.
     */
    function signIn(email, password) {
      return loadConfig().then(function (cfg) {
        if (cfg.error) {
          return { ok: false, reason: cfg.error };
        }
        return gotrueRequest(cfg, "/token?grant_type=password", {
          method: "POST",
          bodyObject: { email: email, password: password }
        }).then(function (result) {
          if (result.status === 200) {
            var session = sessionFromTokenResponse(result.body);
            if (!session) {
              /* 200 with an unusable body: treat as an auth service error,
               * never as a signed-in state (fail closed). */
              return { ok: false, reason: "auth_error" };
            }
            writeSession(session);
            return { ok: true };
          }
          if (result.status === 0) {
            return { ok: false, reason: "auth_unreachable" };
          }
          if (result.status === 400 || result.status === 401 ||
              result.status === 403) {
            return { ok: false, reason: "invalid_credentials" };
          }
          return { ok: false, reason: "auth_error" };
        });
      });
    }

    /*
     * Purpose: exchange the stored refresh token for a new session.
     * Single-flight: concurrent callers await the same request.
     * Failure: ANY non-200 clears the session (a refresh token that
     *   Supabase rejected is dead; keeping it would hide the failure).
     *   A pure network failure (status 0) keeps the session so a transient
     *   outage does not sign the office out, and reports "auth_unreachable".
     */
    function refreshSession() {
      if (refreshInFlight) {
        return refreshInFlight;
      }
      refreshInFlight = loadConfig().then(function (cfg) {
        if (cfg.error) {
          return { ok: false, reason: cfg.error };
        }
        var session = readSession();
        if (!session) {
          return { ok: false, reason: "signed_out" };
        }
        return gotrueRequest(cfg, "/token?grant_type=refresh_token", {
          method: "POST",
          bodyObject: { refresh_token: session.refreshToken }
        }).then(function (result) {
          if (result.status === 200) {
            var next = sessionFromTokenResponse(result.body);
            if (!next) {
              clearSession();
              return { ok: false, reason: "session_expired" };
            }
            writeSession(next);
            return { ok: true };
          }
          if (result.status === 0) {
            return { ok: false, reason: "auth_unreachable" };
          }
          clearSession();
          return { ok: false, reason: "session_expired" };
        });
      }).then(function (outcome) {
        refreshInFlight = null;
        return outcome;
      }, function (err) {
        refreshInFlight = null;
        throw err;
      });
      return refreshInFlight;
    }

    /*
     * Purpose: return a usable access token, refreshing first when the
     * stored token is within TOKEN_REFRESH_SKEW_SECONDS of expiry.
     * Returns: { token } or { error: <refresh failure reason> } or
     *   { error: "signed_out" } when no session exists.
     */
    function ensureFreshAccessToken() {
      var session = readSession();
      if (!session) {
        return Promise.resolve({ error: "signed_out" });
      }
      var secondsLeft = session.expiresAtSeconds - Math.floor(nowFn() / 1000);
      if (secondsLeft > TOKEN_REFRESH_SKEW_SECONDS) {
        return Promise.resolve({ token: session.accessToken });
      }
      return refreshSession().then(function (outcome) {
        if (!outcome.ok) {
          return { error: outcome.reason };
        }
        var refreshed = readSession();
        if (!refreshed) {
          return { error: "signed_out" };
        }
        return { token: refreshed.accessToken };
      });
    }

    /* ------------------------------------------------------------------ */
    /* Server-authoritative tenant bootstrap                               */
    /* ------------------------------------------------------------------ */

    /*
     * Purpose: verify the session against the Mia backend and obtain the
     * ONLY trusted tenant identity: the /portal/me response (P2 contract).
     * Returns one of the closed set of states:
     *   { state: "authorized", practiceName, me }  - enter the shell
     *   { state: "signed_out" }                    - show login
     *   { state: "unauthorized" }                  - session cleared; login.
     *       Covers invalid tokens and valid tokens that are unbound or
     *       inactive. The states are DELIBERATELY indistinguishable here,
     *       mirroring the backend's fail-closed posture, so the login page
     *       cannot be used to probe which accounts are provisioned.
     *   { state: "bootstrap_invalid" }             - 200 but unusable body;
     *       session cleared; login. Never enter the shell on a response
     *       that cannot be displayed honestly (fail closed).
     *   { state: "unavailable" }                   - backend unreachable or
     *       5xx; the SESSION IS KEPT (an outage must not sign offices out)
     *       but the shell is NOT entered.
     * External effects: GET /portal/me (same origin) with Bearer token;
     *   at most one automatic refresh + retry on a 401.
     */
    function fetchPortalMe() {
      return ensureFreshAccessToken().then(function (tokenResult) {
        if (tokenResult.error === "signed_out") {
          return { state: "signed_out" };
        }
        if (tokenResult.error === "auth_unreachable") {
          return { state: "unavailable" };
        }
        if (tokenResult.error) {
          return { state: "unauthorized" };
        }
        return requestPortalMeOnce(tokenResult.token).then(function (first) {
          if (first.status !== 401) {
            return interpretPortalMe(first);
          }
          /* One refresh-and-retry: the token may have expired between the
           * skew check and the request. A second 401 is final. */
          return refreshSession().then(function (outcome) {
            if (!outcome.ok) {
              if (outcome.reason === "auth_unreachable") {
                return { state: "unavailable" };
              }
              clearSession();
              return { state: "unauthorized" };
            }
            var refreshed = readSession();
            if (!refreshed) {
              return { state: "unauthorized" };
            }
            return requestPortalMeOnce(refreshed.accessToken)
              .then(interpretPortalMe);
          });
        });
      });
    }

    /* One raw GET /portal/me. Network failure resolves to status 0. */
    function requestPortalMeOnce(accessToken) {
      return fetchImpl(PORTAL_ME_URL, {
        method: "GET",
        cache: "no-store",
        headers: { "Authorization": "Bearer " + accessToken }
      }).then(function (res) {
        if (!res) {
          return { status: 0, body: null };
        }
        return res.json().then(
          function (parsed) { return { status: res.status, body: parsed }; },
          function () { return { status: res.status, body: null }; }
        );
      }).catch(function () {
        return { status: 0, body: null };
      });
    }

    /* Map a /portal/me result onto the closed state vocabulary above. */
    function interpretPortalMe(result) {
      if (result.status === 200) {
        var name = result.body ? result.body[PRACTICE_NAME_FIELD] : undefined;
        if (typeof name !== "string" || name.trim() === "") {
          /* Fail closed: do not enter the shell without the practice
           * identity the shell exists to display. */
          clearSession();
          return { state: "bootstrap_invalid" };
        }
        return { state: "authorized", practiceName: name.trim(), me: result.body };
      }
      if (result.status === 0 || result.status >= 500) {
        return { state: "unavailable" };
      }
      /* 401 (after retry), 403, 404 and every other client status:
       * fail closed, drop the session, show login. */
      clearSession();
      return { state: "unauthorized" };
    }

    /* ------------------------------------------------------------------ */
    /* Sign-out                                                            */
    /* ------------------------------------------------------------------ */

    /*
     * Purpose: sign the office user out.
     * Business rule: the LOCAL session is cleared FIRST and unconditionally,
     * so sign-out can never be blocked by a network failure. The Supabase
     * server-side revocation is then attempted best-effort and its outcome
     * is reported honestly (serverLogout flag) rather than hidden.
     * External effects: session storage removal; one POST /logout attempt.
     */
    function signOut() {
      var session = readSession();
      clearSession();
      if (!session) {
        return Promise.resolve({ serverLogout: false });
      }
      return loadConfig().then(function (cfg) {
        if (cfg.error) {
          return { serverLogout: false };
        }
        return gotrueRequest(cfg, "/logout", {
          method: "POST",
          accessToken: session.accessToken
        }).then(function (result) {
          return { serverLogout: result.status === 204 || result.status === 200 };
        });
      });
    }

    /* ------------------------------------------------------------------ */
    /* Password reset initiation                                           */
    /* ------------------------------------------------------------------ */

    /*
     * Purpose: ask Supabase to send a recovery email.
     * Business rule (anti-enumeration): every Supabase response - success,
     * unknown email, or rate limit - resolves to { ok: true } with the same
     * generic outcome, so this form cannot reveal whether an email has an
     * account. Only a transport failure ("could not reach the sign-in
     * service") is reported differently, because hiding it would leave the
     * user waiting for an email that was never requested (Constitution 14:
     * failure must be visible).
     * External effects: POST /recover with redirect_to pointing at the
     * reset page. The redirect URL must be allow-listed in Supabase URL
     * Configuration (documented in the runbook).
     */
    function requestPasswordReset(email) {
      return loadConfig().then(function (cfg) {
        if (cfg.error) {
          return { ok: false, reason: cfg.error };
        }
        var redirectTo = windowOrigin + "/static/portal/reset.html";
        return gotrueRequest(cfg,
          "/recover?redirect_to=" + encodeURIComponent(redirectTo), {
            method: "POST",
            bodyObject: { email: email }
          }).then(function (result) {
            if (result.status === 0) {
              return { ok: false, reason: "auth_unreachable" };
            }
            return { ok: true };
          });
      });
    }

    /* ------------------------------------------------------------------ */
    /* Recovery / invitation link handling (reset.html)                    */
    /* ------------------------------------------------------------------ */

    /*
     * Purpose: classify the URL fragment Supabase redirects to reset.html.
     * Inputs: the raw location.hash string (may be "" or "#...").
     * Returns one of the closed set:
     *   { kind: "recovery" | "invite", accessToken }  - show the form
     *   { kind: "link_error", message }               - Supabase-reported
     *       error (e.g. expired link); message is a short sanitized string.
     *   { kind: "unsupported" } - token present but type outside the closed
     *       vocabulary; refused, never guessed (Constitution 4.5).
     *   { kind: "empty" }       - no token material; page opened directly.
     * External effects: none. The caller is responsible for stripping the
     * fragment from the address bar immediately (tokens must not linger in
     * history); that DOM action lives in portal-reset.js.
     */
    function parseRecoveryHash(hashString) {
      var raw = typeof hashString === "string" ? hashString : "";
      if (raw.charAt(0) === "#") {
        raw = raw.slice(1);
      }
      if (raw === "") {
        return { kind: "empty" };
      }
      var params;
      try {
        params = new URLSearchParams(raw);
      } catch (e) {
        return { kind: "empty" };
      }
      var errorCode = params.get("error") || params.get("error_code");
      if (errorCode) {
        var description = params.get("error_description") || "";
        /* Sanitize: cap length and strip angle brackets; the value is
         * rendered as text, but defense in depth costs one line. */
        description = description.replace(/[<>]/g, "").slice(0, 200);
        return {
          kind: "link_error",
          message: description || "This link is invalid or has expired."
        };
      }
      var accessToken = params.get("access_token");
      var type = params.get("type");
      if (!accessToken) {
        return { kind: "empty" };
      }
      if (!SUPPORTED_LINK_TYPES[type]) {
        return { kind: "unsupported" };
      }
      return { kind: type, accessToken: accessToken };
    }

    /*
     * Purpose: complete a recovery or invitation by setting the password.
     * Inputs: the access token taken from the link fragment (NOT from the
     *   stored session) and the new password.
     * Returns: { ok: true } or { ok: false, reason: "link_expired" |
     *   "weak_password" | "auth_unreachable" | "auth_error" | config errors,
     *   message? } - message is only ever Supabase's password-policy text
     *   (safe to show; it contains no account information).
     * Business rule: the link session is used for exactly this one PUT and
     *   is never persisted; after success the user signs in normally, so
     *   the portal is entered only through the standard verified path.
     * External effects: PUT /user with Bearer link token.
     */
    function completePasswordSet(linkAccessToken, newPassword) {
      return loadConfig().then(function (cfg) {
        if (cfg.error) {
          return { ok: false, reason: cfg.error };
        }
        return gotrueRequest(cfg, "/user", {
          method: "PUT",
          accessToken: linkAccessToken,
          bodyObject: { password: newPassword }
        }).then(function (result) {
          if (result.status === 200) {
            return { ok: true };
          }
          if (result.status === 0) {
            return { ok: false, reason: "auth_unreachable" };
          }
          if (result.status === 401 || result.status === 403) {
            return { ok: false, reason: "link_expired" };
          }
          if (result.status === 422) {
            var message = result.body && typeof result.body.msg === "string"
              ? result.body.msg.replace(/[<>]/g, "").slice(0, 200)
              : "";
            return { ok: false, reason: "weak_password", message: message };
          }
          return { ok: false, reason: "auth_error" };
        });
      });
    }

    /* Public surface. Internal helpers stay private. */
    return {
      loadConfig: loadConfig,
      readSession: readSession,
      clearSession: clearSession,
      signIn: signIn,
      refreshSession: refreshSession,
      ensureFreshAccessToken: ensureFreshAccessToken,
      fetchPortalMe: fetchPortalMe,
      signOut: signOut,
      requestPasswordReset: requestPasswordReset,
      parseRecoveryHash: parseRecoveryHash,
      completePasswordSet: completePasswordSet,
      SESSION_STORAGE_KEY: SESSION_STORAGE_KEY,
      PRACTICE_NAME_FIELD: PRACTICE_NAME_FIELD
    };
  }

  /* Export for both the browser (window) and the Node test harness. */
  globalScope.createMiaPortalCore = createMiaPortalCore;

}(typeof window !== "undefined" ? window : this));
