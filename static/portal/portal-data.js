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
  var APPOINTMENTS_URL = "/portal/appointments";

  /* Closed vocabulary of appointments query parameter names. Like
   * LIST_PARAM_NAMES, anything not in this list is NEVER serialized, so no
   * caller mistake can turn into a new request channel (Constitution 4.5).
   * There is deliberately NO tenant parameter: tenancy is the verified
   * bearer token alone. */
  var APPOINTMENTS_PARAM_NAMES = ["start_day", "end_day"];

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

    /*
     * Purpose: build the query string for the appointments list from the
     * closed appointments parameter vocabulary (start_day, end_day only).
     * Business rules: only APPOINTMENTS_PARAM_NAMES are serialized, in that
     * fixed order; undefined/null/empty values are omitted entirely (the
     * backend treats both-absent as "the default seven-day range"); every
     * value is URI-encoded. No tenant parameter can ever be sent.
     * Returns: "" or "?start_day=...&end_day=...".
     */
    function buildAppointmentsQuery(params) {
      params = params || {};
      var parts = [];
      for (var i = 0; i < APPOINTMENTS_PARAM_NAMES.length; i++) {
        var name = APPOINTMENTS_PARAM_NAMES[i];
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

    /* A REAL calendar date in YYYY-MM-DD form (the backend's start_day /
     * end_day wire form). Structural shape is necessary but NOT sufficient:
     * "2026-99-12" and "2026-02-99" are regex-shaped but impossible, and
     * accepting them would let week navigation compute from a nonsense
     * anchor (R1). Validation is timezone-INDEPENDENT: the components are
     * parsed as integers and round-tripped through Date.UTC (UTC only, never
     * device time); a real date's UTC Y/M/D come back exactly equal, while
     * an out-of-range month or day overflows to a different date and is
     * rejected. */
    function isRealCalendarDate(value) {
      if (typeof value !== "string" ||
          !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
        return false;
      }
      var parts = value.split("-");
      var y = Number(parts[0]);
      var m = Number(parts[1]);
      var d = Number(parts[2]);
      /* Date.UTC normalizes overflow (month 99 -> a later year, day 99 ->
       * a later month), so a genuine date is the ONLY input whose UTC
       * components match the parsed numbers exactly. */
      var probe = new Date(Date.UTC(y, m - 1, d));
      return probe.getUTCFullYear() === y &&
        probe.getUTCMonth() === m - 1 &&
        probe.getUTCDate() === d;
    }

    /* A genuinely valid UTC ISO-8601 instant in EXACTLY the form this
     * frozen backend emits. Determined by inspecting the actual
     * FastAPI/Pydantic v2 serialization on baseline 0316b36c: every
     * datetime field passes through ensure_utc (aware UTC) and Pydantic
     * renders it as YYYY-MM-DDTHH:MM:SS[.ffffff]Z - always the 'T'
     * separator, always the 'Z' designator (never '+00:00'), with optional
     * fractional seconds. Two independent gates, neither depending on the
     * device timezone:
     *   1) STRICT GRAMMAR: the string must match that exact shape. This
     *      alone rejects "2026-08-12T10:00:00" (no designator, which would
     *      otherwise be parsed in DEVICE time), "2026-08-12" (date only),
     *      and "not-a-date".
     *   2) REAL CALENDAR INSTANT: the Y/M/D/h/m/s components are
     *      round-tripped through Date.UTC and must come back exactly equal,
     *      so an impossible date like "2026-02-30T10:00:00Z" - which
     *      JavaScript's Date parser silently NORMALIZES to March 2 - is
     *      rejected instead of accepted.
     * Fractional seconds are validated for shape only (any run of digits);
     * their magnitude cannot make an instant invalid. */
    var UTC_INSTANT_RE =
      /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z$/;

    function isValidUtcInstant(value) {
      if (typeof value !== "string") {
        return false;
      }
      var match = UTC_INSTANT_RE.exec(value);
      if (!match) {
        return false;
      }
      var y = Number(match[1]);
      var mo = Number(match[2]);
      var d = Number(match[3]);
      var h = Number(match[4]);
      var mi = Number(match[5]);
      var s = Number(match[6]);
      /* Reject the obvious out-of-range field values up front (Date.UTC
       * would happily roll 24:00 or 13 months over into the next unit). */
      if (mo < 1 || mo > 12 || d < 1 || d > 31 ||
          h > 23 || mi > 59 || s > 59) {
        return false;
      }
      /* Date.UTC normalizes overflow (Feb 30 -> Mar 2, etc.), so a genuine
       * instant is the ONLY input whose UTC components come back equal. All
       * arithmetic is in UTC: no device-time dependence. */
      var probe = new Date(Date.UTC(y, mo - 1, d, h, mi, s));
      return probe.getUTCFullYear() === y &&
        probe.getUTCMonth() === mo - 1 &&
        probe.getUTCDate() === d &&
        probe.getUTCHours() === h &&
        probe.getUTCMinutes() === mi &&
        probe.getUTCSeconds() === s;
    }

    /* Minimum TRUSTWORTHY appointment member: a real object carrying a
     * non-empty string appointment_id (the pages key every row off it), a
     * non-empty string status and notification_outcome (both drive a
     * rendered label), AND a genuinely parseable start_datetime AND
     * end_datetime - the appointment window. start_datetime is the TIME the
     * page formats in the office timezone; end_datetime is part of the
     * backend's required appointment wire contract, so both are pinned here
     * even though the current list UI renders only the start (R1). A member
     * without usable timing is not trustworthy to render, so it fails
     * closed. Genuinely-nullable display fields (patient_email, reason,
     * confirmed_at, ...) stay permissive, the same rule isValidLeadMember
     * follows. */
    function isValidAppointmentMember(appointment) {
      return appointment !== null && typeof appointment === "object" &&
        typeof appointment.appointment_id === "string" &&
        appointment.appointment_id !== "" &&
        typeof appointment.status === "string" &&
        appointment.status !== "" &&
        typeof appointment.notification_outcome === "string" &&
        appointment.notification_outcome !== "" &&
        isValidUtcInstant(appointment.start_datetime) &&
        isValidUtcInstant(appointment.end_datetime);
    }

    function isValidAppointmentArray(list) {
      if (!Array.isArray(list)) {
        return false;
      }
      for (var i = 0; i < list.length; i++) {
        if (!isValidAppointmentMember(list[i])) {
          return false;
        }
      }
      return true;
    }

    /* The appointments envelope: a non-empty string office timezone the
     * pages MUST format times in, the echoed local-day bounds start_day and
     * end_day (BOTH required and BOTH REAL calendar dates - the pages render
     * the range label and derive week navigation from them, so a nonsense
     * bound must fail closed, R1), and a valid appointment array. A
     * malformed success is a failure (fail closed) - never rendered. */
    function isValidAppointmentListBody(body) {
      return typeof body.timezone_name === "string" &&
        body.timezone_name !== "" &&
        isRealCalendarDate(body.start_day) &&
        isRealCalendarDate(body.end_day) &&
        isValidAppointmentArray(body.appointments);
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
      /* P3-B2: the office workflow slice is part of the approved detail
       * contract - reject a detail body that lost or malformed it. */
      if (!hasValidWorkflowFields(body)) {
        return false;
      }
      if (body.messages_truncated === false) {
        return body.messages_total === body.messages.length;
      }
      return body.messages_total > body.messages.length;
    }

    /* P3-B2: one office workflow field pair is either (null value) or a
     * value with a string token; a non-null value MUST carry its token
     * (mirrors the migration-008 CHECK), and the token itself is null only
     * while the office has never touched the field. */
    function isValidWorkflowPair(value, token, allowedValues) {
      if (token !== null && typeof token !== "string") {
        return false;
      }
      if (value === null) {
        return true;
      }
      if (typeof value !== "string" || token === null) {
        return false;
      }
      if (allowedValues) {
        for (var i = 0; i < allowedValues.length; i++) {
          if (allowedValues[i] === value) { return true; }
        }
        return false;
      }
      return true;
    }

    var OFFICE_STATUS_VALUES = ["contacted", "booked", "closed"];

    function hasValidWorkflowFields(body) {
      return isValidWorkflowPair(body.office_status,
          body.office_status_updated_at, OFFICE_STATUS_VALUES) &&
        isValidWorkflowPair(body.office_note,
          body.office_note_updated_at, null);
    }

    /* PUT /portal/leads/<id>/(status|note) response: exactly the office
     * workflow slice keyed to the lead it mutated. */
    function isValidWorkflowBody(body) {
      return typeof body.lead_id === "string" && body.lead_id !== "" &&
        hasValidWorkflowFields(body);
    }

    /* One raw authenticated GET. Network failure resolves to status 0 so
     * callers can distinguish "could not reach the portal" from a
     * rejection (the portal-core requestPortalMeOnce convention). */
    function requestOnce(url, accessToken, method, payload) {
      var init = {
        method: method || "GET",
        cache: "no-store",
        headers: { "Authorization": "Bearer " + accessToken }
      };
      if (payload !== undefined) {
        /* P3-B2 mutations: the payload NEVER carries a tenant selector -
         * tenancy is the verified bearer token alone. */
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(payload);
      }
      return fetchImpl(url, init).then(function (res) {
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
      if (result.status === 409) {
        /* P3-B2: optimistic-concurrency conflict - the lead changed
         * elsewhere. The pages must refresh authoritative state. */
        return { ok: false, state: "conflict" };
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
    function authorizedSend(method, url, payload, isValidBody) {
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
        return requestOnce(url, tokenResult.token, method, payload).then(function (first) {
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
            return requestOnce(url, refreshed.accessToken, method, payload)
              .then(function (retried) {
                return interpret(retried, isValidBody);
              });
          });
        });
      });
    }

    /* The ONE authenticated GET pathway (delegates to authorizedSend so a
     * single implementation owns token handling and the 401 retry). */
    function authorizedGet(url, isValidBody) {
      return authorizedSend("GET", url, undefined, isValidBody);
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
      /* PUT /portal/leads/<id>/status - set or clear (null) the office
       * workflow status under the expected concurrency token. */
      putLeadStatus: function (leadId, status, expectedToken) {
        return authorizedSend("PUT",
          LEADS_URL + "/" + encodeURIComponent(String(leadId)) + "/status",
          { office_status: status,
            expected_office_status_updated_at: expectedToken },
          isValidWorkflowBody);
      },
      /* PUT /portal/leads/<id>/note - save or clear (null) the office
       * note under the expected concurrency token. */
      putLeadNote: function (leadId, note, expectedToken) {
        return authorizedSend("PUT",
          LEADS_URL + "/" + encodeURIComponent(String(leadId)) + "/note",
          { office_note: note,
            expected_office_note_updated_at: expectedToken },
          isValidWorkflowBody);
      },
      /* GET /portal/appointments - the office's appointments for a local-day
       * range. params may carry ONLY the closed APPOINTMENTS_PARAM_NAMES
       * vocabulary; both omitted requests the backend default seven-day
       * range. Tenancy is the verified bearer token alone. */
      getAppointments: function (params) {
        return authorizedGet(
          APPOINTMENTS_URL + buildAppointmentsQuery(params),
          isValidAppointmentListBody);
      },
      /* Exported for the Node suite (pure functions). */
      buildLeadsQuery: buildLeadsQuery,
      buildAppointmentsQuery: buildAppointmentsQuery
    };
  }

  /* Export for both the browser (window) and the Node test harness. */
  globalScope.createMiaPortalData = createMiaPortalData;

}(typeof window !== "undefined" ? window : this));
