/*
 * portal-data.js - Mia Office Portal read-only data access (P3-B1).
 *
 * SINGLE OWNER (Constitution 5): this file is the only owner of the
 * browser-side requests to the Mia backend's portal DATA endpoints
 * (GET /portal/dashboard, GET /portal/leads, GET /portal/leads/<id>).
 * portal-core.js remains the only owner of sessions, tokens, and
 * Supabase Auth; this file consumes core's token functions and never
 * touches storage or auth endpoints itself. portal-pages.js is DOM glue
 * only and performs no network requests of its own.
 *
 * TENANT RULE (P2/P3-A, frozen): the backend derives the office from the
 * verified Bearer token alone. Nothing in this file reads, writes, or
 * transmits any tenant or office identifier - the query parameters it can
 * send are the closed LIST_PARAM_NAMES set below, and nothing else.
 *
 * OUTCOME VOCABULARY (closed set, Constitution 4.5): every request
 * resolves to exactly one of:
 *   { ok: true,  data }                    - usable 200 body
 *   { ok: false, state: "signed_out" }     - no local session
 *   { ok: false, state: "unauthorized" }   - credential rejected (after
 *                                            one refresh-and-retry)
 *   { ok: false, state: "unavailable" }    - network failure or 5xx
 *   { ok: false, state: "not_found" }      - 404 (e.g. lead disappeared)
 *   { ok: false, state: "bad_request" }    - 400/422 (invalid filters)
 *   { ok: false, state: "invalid_response" } - 200 whose body is not an
 *       object OR fails the endpoint's shape validation below (audit
 *       finding A3)
 * Nothing is guessed: an unusable success body is REJECTED (fail closed),
 * never rendered and never allowed to throw inside the page glue or strand
 * a loading state.
 *
 * TESTABILITY: createMiaPortalData() takes injected dependencies (core,
 * fetchImpl) so the Node test harness can drive every path without a
 * browser. No DOM access occurs in this file.
 */
(function (globalScope) {
  "use strict";

  /* Backend endpoints (relative: the portal is served by the same FastAPI
   * app, so calls are same-origin by design - the P3-A convention). */
  var DASHBOARD_URL = "/portal/dashboard";
  var LEADS_URL = "/portal/leads";

  /* Closed vocabulary of list query parameter names. Anything not in this
   * list is NEVER serialized onto a request, so no caller mistake can turn
   * into a new request channel (Constitution 4.5). */
  var LIST_PARAM_NAMES = ["status", "q", "days", "limit", "offset"];

  function createMiaPortalData(deps) {
    if (!deps || !deps.core || typeof deps.fetchImpl !== "function") {
      /* Wiring error by the caller, not a user-facing state. Fail loudly. */
      throw new Error("createMiaPortalData: core and fetchImpl are required");
    }

    var core = deps.core;
    var fetchImpl = deps.fetchImpl;

    /*
     * Purpose: build the query string for the leads list from the closed
     * parameter vocabulary.
     * Business rules: only LIST_PARAM_NAMES are serialized, in that fixed
     * order; undefined/null/empty values are omitted entirely (the backend
     * treats an absent filter as "no filter"); every value is URI-encoded
     * so search text can never break the URL.
     * Returns: "" or "?name=value&...".
     */
    function buildLeadsQuery(params) {
      params = params || {};
      var parts = [];
      for (var i = 0; i < LIST_PARAM_NAMES.length; i++) {
        var name = LIST_PARAM_NAMES[i];
        var value = params[name];
        if (value === undefined || value === null || value === "") {
          continue;
        }
        parts.push(name + "=" + encodeURIComponent(String(value)));
      }
      return parts.length === 0 ? "" : "?" + parts.join("&");
    }

    /* ---------------------------------------------------------------
     * Response-shape validation (audit findings A3 + A3-R1): the
     * SMALLEST endpoint-specific structural checks that guarantee the
     * page glue can render the body without throwing - which means
     * validating ARRAY MEMBERS, not just the array container: the pages
     * dereference every lead row and every transcript line, so a [null]
     * or primitive member would throw exactly like a missing array.
     * Optional DISPLAY fields the backend legitimately nulls (lead_name,
     * lead_phone, timestamps...) stay permissive; only the fields the
     * pages must dereference are required. A 200 that fails any check -
     * including the internal transcript-consistency checks - resolves to
     * "invalid_response": a malformed success is a failure.
     * --------------------------------------------------------------- */

    /* A JSON count: a finite, non-negative INTEGER. */
    function isCount(value) {
      return typeof value === "number" && isFinite(value) &&
        value >= 0 && value % 1 === 0;
    }

    /* Minimum usable lead summary: a real object carrying a non-empty
     * string lead_id (the pages key every row action off it). All other
     * summary fields are nullable display data and stay permissive. */
    function isValidLeadMember(lead) {
      return lead !== null && typeof lead === "object" &&
        typeof lead.lead_id === "string" && lead.lead_id !== "";
    }

    function isValidLeadArray(list) {
      if (!Array.isArray(list)) {
        return false;
      }
      for (var i = 0; i < list.length; i++) {
        if (!isValidLeadMember(list[i])) {
          return false;
        }
      }
      return true;
    }

    /* Minimum usable transcript line: a real object with string role and
     * string content (both are dereferenced unconditionally). created_at
     * stays permissive (nullable display data). */
    function isValidMessageMember(message) {
      return message !== null && typeof message === "object" &&
        typeof message.role === "string" &&
        typeof message.content === "string";
    }

    function isValidMessageArray(list) {
      if (!Array.isArray(list)) {
        return false;
      }
      for (var i = 0; i < list.length; i++) {
        if (!isValidMessageMember(list[i])) {
          return false;
        }
      }
      return true;
    }

    function isValidDashboardBody(body) {
      return typeof body.practice_name === "string" &&
        isCount(body.total_conversations) &&
        isCount(body.total_leads) &&
        isCount(body.urgent_leads) &&
        isCount(body.leads_last_7_days) &&
        isValidLeadArray(body.recent_leads);
    }

    function isValidLeadListBody(body) {
      return isCount(body.total) &&
        isCount(body.limit) &&
        isCount(body.offset) &&
        isValidLeadArray(body.leads);
    }

    function isValidLeadDetailBody(body) {
      if (typeof body.lead_id !== "string" || body.lead_id === "" ||
          !isValidMessageArray(body.messages) ||
          !isCount(body.messages_total) ||
          typeof body.messages_truncated !== "boolean") {
        return false;
      }
      /* Internal consistency (A3-R1): the truncation claim and the true
       * total must agree with the page actually delivered - an honest
       * partial-transcript notice cannot be built from numbers that
       * contradict the payload. */
      if (body.messages_truncated === false) {
        return body.messages_total === body.messages.length;
      }
      return body.messages_total > body.messages.length;
    }

    /* One raw authenticated GET. Network failure resolves to status 0 so
     * callers can distinguish "could not reach the portal" from a
     * rejection (the portal-core requestPortalMeOnce convention). */
    function requestOnce(url, accessToken) {
      return fetchImpl(url, {
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

    /* Map one HTTP result onto the closed outcome vocabulary above.
     * isValidBody is the endpoint's shape validator (A3). */
    function interpret(result, isValidBody) {
      if (result.status === 200) {
        if (result.body === null || typeof result.body !== "object" ||
            !isValidBody(result.body)) {
          /* Fail closed: never hand the pages an unusable body. */
          return { ok: false, state: "invalid_response" };
        }
        return { ok: true, data: result.body };
      }
      if (result.status === 0 || result.status >= 500) {
        return { ok: false, state: "unavailable" };
      }
      if (result.status === 404) {
        return { ok: false, state: "not_found" };
      }
      if (result.status === 400 || result.status === 422) {
        return { ok: false, state: "bad_request" };
      }
      /* 401 (final, after the retry below), 403, and every other client
       * status: the credential was rejected - fail closed. */
      return { ok: false, state: "unauthorized" };
    }

    /*
     * Purpose: the ONE authenticated GET pathway every data call uses.
     * Flow: ensure a fresh access token (core refreshes near-expiry
     * tokens itself), perform the request, and on a 401 attempt EXACTLY
     * ONE refresh-and-retry (the token may have expired between the skew
     * check and the request - the portal-core fetchPortalMe convention).
     * A second 401 is final. Duplicate refreshes cannot race: core's
     * refreshSession is single-flight.
     * External effects: one or two same-origin GETs; possibly one token
     * refresh via core.
     */
    function authorizedGet(url, isValidBody) {
      return core.ensureFreshAccessToken().then(function (tokenResult) {
        if (tokenResult.error === "signed_out") {
          return { ok: false, state: "signed_out" };
        }
        if (tokenResult.error === "auth_unreachable") {
          return { ok: false, state: "unavailable" };
        }
        if (tokenResult.error) {
          return { ok: false, state: "unauthorized" };
        }
        return requestOnce(url, tokenResult.token).then(function (first) {
          if (first.status !== 401) {
            return interpret(first, isValidBody);
          }
          return core.refreshSession().then(function (outcome) {
            if (!outcome.ok) {
              if (outcome.reason === "auth_unreachable") {
                return { ok: false, state: "unavailable" };
              }
              return { ok: false, state: "unauthorized" };
            }
            var refreshed = core.readSession();
            if (!refreshed) {
              return { ok: false, state: "unauthorized" };
            }
            return requestOnce(url, refreshed.accessToken)
              .then(function (retried) {
                return interpret(retried, isValidBody);
              });
          });
        });
      });
    }

    /* Public surface: one function per backend endpoint, nothing else. */
    return {
      /* GET /portal/dashboard - counts + recent leads for the ONE office
       * the verified token belongs to. */
      getDashboard: function () {
        return authorizedGet(DASHBOARD_URL, isValidDashboardBody);
      },
      /* GET /portal/leads - the paginated lead list. params may carry only
       * the closed LIST_PARAM_NAMES vocabulary. */
      listLeads: function (params) {
        return authorizedGet(LEADS_URL + buildLeadsQuery(params),
          isValidLeadListBody);
      },
      /* GET /portal/leads/<id> - one lead + transcript. The id is
       * URI-encoded so it can only ever address a path segment. */
      getLeadDetail: function (leadId) {
        return authorizedGet(
          LEADS_URL + "/" + encodeURIComponent(String(leadId)),
          isValidLeadDetailBody);
      },
      /* Exported for the Node suite (pure function). */
      buildLeadsQuery: buildLeadsQuery
    };
  }

  /* Export for both the browser (window) and the Node test harness. */
  globalScope.createMiaPortalData = createMiaPortalData;

}(typeof window !== "undefined" ? window : this));
