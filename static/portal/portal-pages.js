/*
 * portal-pages.js - Mia Office Portal page glue for the authenticated
 * shell (P3-B1: read-only Dashboard + Leads + Lead detail).
 *
 * OWNERSHIP (Constitution 5): this file only connects DOM elements to
 * portal-data.js results. It performs NO network requests of its own,
 * reads NO tokens or storage, and contains no auth or tenant rule -
 * portal-core.js owns sessions, portal-data.js owns data requests, and
 * the backend alone decides WHICH office's data is returned.
 *
 * RENDERING RULE: every server- or patient-supplied string reaches the
 * page through textContent only (never markup), so captured lead text can
 * never inject HTML into the office's browser.
 *
 * PAGE STATES (closed set, one visible at a time inside the shell):
 *   page-dashboard | page-leads | page-lead-detail
 * Every data load ends in exactly one of: rendered content, an explicit
 * empty message, or an explicit error message - a page is never left
 * silently blank (Constitution 14: failure must be visible).
 *
 * STALE-RESPONSE GUARD: each page keeps a request counter; a response is
 * applied only if it is still the NEWEST request for that page, so a slow
 * older response can never overwrite a newer one (Constitution 10).
 */
(function (globalScope) {
  "use strict";

  /* The leads page size. One named setting, matching the backend default
   * (portal_leads_service.LIST_LIMIT_DEFAULT). */
  var LIST_PAGE_LIMIT = 25;

  /* User-facing wording for every data outcome, in one reviewable place.
   * "signed_out"/"unauthorized" never render here - they hand control
   * back to the sign-in flow via onSessionLost. */
  var MESSAGES = {
    loading: "Loading...",
    unavailable: "The portal service is temporarily unavailable. Please try again shortly.",
    bad_request: "Those filters could not be applied. Please adjust them and try again.",
    invalid_response: "The portal returned an unexpected response. Please try again shortly.",
    lead_not_found: "That lead could not be found. It may have been removed.",
    dashboard_empty: "No leads yet. New leads will appear here as soon as Mia captures them.",
    value_missing: "Not provided",
    workflow_conflict: "This lead was updated somewhere else. Showing the latest state.",
    workflow_saved_status: "Status saved.",
    workflow_saved_note: "Note saved.",
    workflow_cleared_note: "Note cleared.",
    workflow_note_empty: "Enter a note before saving, or use Clear note.",
    workflow_failed: "The change was not saved. Please try again.",
    appointments_empty: "No appointments in this range.",
    appointments_tz_note_prefix: "Times shown in the office timezone: ",
    /* P5-A appointment action wording. Office-facing, closed set. */
    appointment_confirmed: "Appointment confirmed.",
    appointment_cancelled: "Appointment cancelled.",
    appointment_cancel_arm: "Click Cancel again to confirm.",
    appointment_action_conflict:
      "That appointment changed somewhere else. Showing the latest appointments.",
    appointment_action_failed:
      "The change was not saved. Showing the latest appointments.",
    appointment_gone:
      "That appointment could not be found. Showing the latest appointments.",
    /* P4-A schedule wording. NEVER the words "close"/"closed"/"closure"
     * for the bulk action (contract v1.2 SS5-E / D3): it is a SLOT
     * operation over the rows existing right now - publishing later
     * reopens the day. */
    schedule_empty: "No slots in this range.",
    schedule_day_required: "Choose a day first.",
    schedule_publish_conflict:
      "Those hours overlap existing slots on that day. Showing the latest schedule.",
    schedule_publish_rejected:
      "Those hours could not be published. Adjust the times or slot length and try again.",
    schedule_action_conflict:
      "That slot changed somewhere else. Showing the latest schedule.",
    schedule_bulk_note:
      "Blocks every slot that is currently open on this day. Publishing new slots later will reopen the day.",
    schedule_booked_remaining_prefix: "Booked appointments remain at: ",
    schedule_slot_gone: "That slot could not be found. Showing the latest schedule."
  };

  /* ------------------------------------------------------------------ */
  /* Pure presentation helpers (exported for the Node suite)             */
  /* ------------------------------------------------------------------ */

  /*
   * Purpose: one office-facing label per slot status (P4-A). The closed
   * backend vocabulary maps to closed wording; an unknown value renders AS
   * ITSELF (through textContent - safe) so a vocabulary drift is visible,
   * never hidden (Constitution 14).
   */
  var SCHEDULE_STATUS_LABELS = {
    available: "Open",
    held: "On hold",
    booked: "Booked",
    blocked: "Blocked",
    cancelled: "Cancelled"
  };

  function scheduleSlotStatusLabel(status) {
    if (Object.prototype.hasOwnProperty.call(SCHEDULE_STATUS_LABELS, status)) {
      return SCHEDULE_STATUS_LABELS[status];
    }
    return String(status || "");
  }

  /*
   * Purpose (P5-A): the office actions offered for one appointment status.
   * The SERVER (booking_service) is authoritative; these are UI hints that
   * mirror the lifecycle owner's allow-lists so a control never appears
   * where the backend would only refuse it: pending offers Confirm and
   * Cancel, a confirmed appointment offers only Cancel, and every terminal
   * status (cancelled/completed/no_show) offers none. An unknown status
   * offers none - fail safe, never a speculative button. Pure.
   */
  function appointmentActionsFor(status) {
    if (status === "pending") { return ["confirm", "cancel"]; }
    if (status === "confirmed") { return ["cancel"]; }
    return [];
  }

  /*
   * Purpose: turn an ISO timestamp into local display text.
   * Returns: a locale string, or "" for missing/unparseable input (an
   * absent time renders as absent, never as "Invalid Date").
   */
  function formatTimestamp(isoText) {
    if (typeof isoText !== "string" || isoText === "") {
      return "";
    }
    var parsed = new Date(isoText);
    if (isNaN(parsed.getTime())) {
      return "";
    }
    return parsed.toLocaleString();
  }

  /* The urgency badges for one lead, in fixed display order. */
  function leadBadges(lead) {
    var badges = [];
    if (lead && lead.lead_is_emergency) { badges.push("Emergency"); }
    if (lead && lead.lead_is_priority) { badges.push("Priority"); }
    if (lead && lead.lead_is_outside_hours) { badges.push("After hours"); }
    return badges;
  }

  /* Display labels for the closed lead_status vocabulary (audit findings
   * A1 + A1-R1). Both system-written values are labeled by what they
   * actually mean in the intake lifecycle: "new" only means Mia has not
   * finished capturing the request, and "completed" only means it HAS -
   * neither may ever read as a staff-handling state. Unknown values (a
   * future vocabulary change) render as their raw text, never guessed. */
  var STATUS_LABELS = {
    "new": "Intake not completed",
    "contacted": "Contacted",
    "booked": "Booked",
    "completed": "Intake completed",
    "closed": "Closed"
  };

  function statusLabel(status) {
    if (typeof status !== "string" || status === "") {
      return "";
    }
    return STATUS_LABELS[status] || status;
  }

  /*
   * Purpose: pagination arithmetic in one testable place.
   * Inputs: the FILTERED total the backend reported, plus the page bounds
   * that were actually used.
   * Returns: { label, prevDisabled, nextDisabled, prevOffset, nextOffset }.
   */
  function pagerModel(total, limit, offset) {
    var from = total === 0 ? 0 : offset + 1;
    var to = Math.min(offset + limit, total);
    return {
      label: total === 0 ? "0 leads" :
        "Showing " + from + "-" + to + " of " + total,
      prevDisabled: offset <= 0,
      nextDisabled: offset + limit >= total,
      prevOffset: Math.max(0, offset - limit),
      nextOffset: offset + limit
    };
  }

  /* Honest empty wording: an empty FILTERED list is not "no leads". */
  function emptyLeadsMessage(filtersActive) {
    return filtersActive
      ? "No leads match these filters."
      : "No leads yet. New leads will appear here.";
  }

  /* ------------------------------------------------------------------ */
  /* Appointments pure presentation helpers (exported for the Node suite)*/
  /* ------------------------------------------------------------------ */

  /* Appointment lifecycle labels (the backend AppointmentStatus vocabulary).
   * An unknown value renders as itself rather than being hidden. */
  var APPOINTMENT_STATUS_LABELS = {
    "pending": "Pending",
    "confirmed": "Confirmed",
    "cancelled": "Cancelled",
    "completed": "Completed",
    "no_show": "No-show"
  };

  function appointmentStatusLabel(status) {
    if (typeof status !== "string" || status === "") {
      return "";
    }
    return APPOINTMENT_STATUS_LABELS[status] || status;
  }

  /* The safe derived notification outcome (backend closed vocabulary:
   * sent | failed | pending). Office-facing wording; an unknown value
   * renders as itself so nothing is silently dropped. */
  var NOTIFICATION_OUTCOME_LABELS = {
    "sent": "Office notified",
    "failed": "Notification failed",
    "pending": "Notification pending"
  };

  function notificationOutcomeLabel(outcome) {
    if (typeof outcome !== "string" || outcome === "") {
      return "";
    }
    return NOTIFICATION_OUTCOME_LABELS[outcome] || outcome;
  }

  /*
   * Purpose: format a UTC ISO instant in a SPECIFIC IANA timezone - the
   * office timezone the backend returns, NEVER the browser/device timezone.
   * This is the single reason appointment times are trustworthy for staff
   * in another timezone.
   * Inputs: isoText (a UTC instant string) and timeZone (an IANA name).
   * Returns: a formatted local-to-the-office string, or "" for missing or
   * unparseable input (an absent time renders as absent, never as
   * "Invalid Date"). If the timezone is unsupported by the runtime, falls
   * back to a UTC-suffixed render rather than silently using device time -
   * a wrong-timezone time would mislead staff about when to expect a
   * patient (Constitution 4/16: failure is visible, never hidden).
   */
  function formatInTimeZone(isoText, timeZone) {
    if (typeof isoText !== "string" || isoText === "") {
      return "";
    }
    var parsed = new Date(isoText);
    if (isNaN(parsed.getTime())) {
      return "";
    }
    var options = {
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit"
    };
    if (typeof timeZone === "string" && timeZone !== "") {
      try {
        return new Intl.DateTimeFormat(undefined,
          Object.assign({ timeZone: timeZone }, options)).format(parsed) +
          " (" + timeZone + ")";
      } catch (err) {
        /* Unsupported timezone name: fall back to an explicit UTC render so
         * the value is never silently shown in device time. */
        return new Intl.DateTimeFormat(undefined,
          Object.assign({ timeZone: "UTC" }, options)).format(parsed) +
          " (UTC)";
      }
    }
    /* No timezone supplied: explicit UTC, never implicit device time. */
    return new Intl.DateTimeFormat(undefined,
      Object.assign({ timeZone: "UTC" }, options)).format(parsed) + " (UTC)";
  }

  /*
   * Purpose: shift an ISO local-day string (YYYY-MM-DD) by a whole number
   * of days, purely (no timezone math - these are calendar-date labels the
   * office navigates by, and the backend re-derives DST-safe UTC windows
   * from them). Returns a YYYY-MM-DD string, or "" for malformed input.
   */
  function shiftLocalDay(dayText, deltaDays) {
    if (typeof dayText !== "string" ||
        !/^\d{4}-\d{2}-\d{2}$/.test(dayText)) {
      return "";
    }
    /* Anchor at UTC noon so a +/- day shift can never cross a day boundary
     * through the Date object's own timezone handling. */
    var base = new Date(dayText + "T12:00:00Z");
    if (isNaN(base.getTime())) {
      return "";
    }
    base.setUTCDate(base.getUTCDate() + deltaDays);
    var y = base.getUTCFullYear();
    var m = String(base.getUTCMonth() + 1).padStart(2, "0");
    var d = String(base.getUTCDate()).padStart(2, "0");
    return y + "-" + m + "-" + d;
  }

  /*
   * Purpose: the range label for the appointments week view, in one
   * testable place.
   * Returns: e.g. "Jul 16 - Jul 22, 2026", or "" when either bound is
   * missing.
   */
  function appointmentsRangeLabel(startDay, endDay) {
    if (typeof startDay !== "string" || startDay === "" ||
        typeof endDay !== "string" || endDay === "") {
      return "";
    }
    return startDay + "  \u2192  " + endDay;
  }

  /* ------------------------------------------------------------------ */
  /* Factory                                                             */
  /* ------------------------------------------------------------------ */

  function createMiaPortalPages(deps) {
    if (!deps || !deps.data || !deps.documentRef ||
        typeof deps.onSessionLost !== "function") {
      /* Wiring error by the caller, not a user-facing state. Fail loudly. */
      throw new Error(
        "createMiaPortalPages: data, documentRef and onSessionLost are required");
    }

    var data = deps.data;
    var doc = deps.documentRef;
    var onSessionLost = deps.onSessionLost;

    /* Current leads query (closed parameter set; tenant is NEVER part of
     * a query - the backend derives it from the token). */
    var leadsQuery = { status: "", q: "", limit: LIST_PAGE_LIMIT, offset: 0 };

    /* Stale-response guards: one monotonically increasing id per page. */
    var requestIds = { dashboard: 0, leads: 0, detail: 0, appointments: 0,
      schedule: 0 };

    /* P4-A Schedule page state (contract v1.2 SS6 / Correction C4).
     * The Schedule page MUTATES, so the read-page sequence guard alone is
     * not enough: generation is a mutation-generation counter, incremented
     * the moment ANY schedule mutation begins. A window GET response is
     * applied only when BOTH its request id is still current AND the
     * generation captured at issue time equals the current generation - a
     * read issued before a mutation can therefore never roll the rendered
     * schedule back after the mutation's authoritative refresh.
     * Busy flags are INDEPENDENT duplicate-submit guards: slotBusy tracks
     * per-slot in-flight actions by slot_id (one slot's action never
     * disables another slot's control), publishBusy guards the publish
     * form, bulkBusy guards the bulk action. Week navigation mirrors the
     * appointments anchor discipline (F3).
     * lifecycle (audit F3) is the page/session lifecycle token: bumped by
     * resetContent (sign-out / independent reset) and by openSchedule
     * (page re-entry). EVERY mutation captures it at start; a mutation
     * response whose captured lifecycle is stale may clear its own busy
     * flag but must not touch the DOM and must not trigger a schedule
     * GET - so a late-resolving mutation can never repopulate a wiped
     * page or fire a post-reset request. */
    var schedule = {
      weekOffset: 0,
      defaultStart: null,
      currentStart: null,
      currentEnd: null,
      generation: 0,
      lifecycle: 0,
      publishBusy: false,
      bulkBusy: false,
      slotBusy: {}
    };

    /* Appointments week-navigation state. weekOffset 0 = the backend
     * DEFAULT seven-day range (both bounds omitted, so the backend anchors
     * "today" in the OFFICE timezone). A non-zero offset navigates whole
     * seven-day windows relative to the last-known default start returned by
     * the backend; explicit bounds are then sent. currentRange holds the
     * bounds the backend actually echoed, so navigation is always relative
     * to real, DST-safe local dates (never a browser-computed "today"). */
    var appointments = {
      weekOffset: 0,
      defaultStart: null,   /* the office-local "today" the backend anchored */
      currentStart: null,   /* the start_day the backend echoed for this view */
      currentEnd: null,     /* the end_day the backend echoed for this view */
      /* P5-A: the Appointments page now MUTATES, so it mirrors the P4-A
       * schedule discipline. generation is a mutation-generation counter
       * (a window GET is applied only when its captured generation still
       * equals the current one, so a read issued before a mutation can
       * never roll the list back behind the mutation's authoritative
       * refresh). lifecycle is the page/session token bumped by resetContent
       * and openAppointments; a mutation whose captured lifecycle is stale
       * may clear its own busy flag but must not touch the DOM or trigger a
       * GET. actionBusy is the per-appointment duplicate-submit guard: it
       * maps an appointment id to the UNIQUE token of the mutation that owns
       * it (F4). Presence of a token means busy - BOTH that row's controls
       * are disabled - and a completing mutation clears the entry ONLY when
       * it still owns the token, so a stale mutation resolving after a reset
       * can never clear a NEWER same-appointment mutation's ownership.
       * actionSeq is the monotonic token source (NEVER reset, so tokens can
       * never collide across page re-entries). armed holds the C4 inline
       * two-click Cancel confirmation state per appointment. */
      generation: 0,
      lifecycle: 0,
      actionSeq: 0,
      actionBusy: {},
      armed: {}
    };

    /* P3-B2 office workflow state: the AUTHORITATIVE lead (set the
     * moment a lead is opened - every mutation response is bound to it
     * and is discarded once another lead is authoritative, v1.0.1), the
     * two SERVER concurrency tokens last observed, independent in-flight
     * flags (duplicate-submit guards; each control's busy lifecycle is
     * fully independent of its sibling's), and independent sequence
     * counters so a superseded mutation response can NEVER overwrite
     * newer UI state (the requestIds pattern, per control). */
    var workflow = {
      leadId: null,
      statusToken: null,
      noteToken: null,
      statusBusy: false,
      noteBusy: false,
      statusSeq: 0,
      noteSeq: 0
    };

    function byId(id) {
      return doc.getElementById(id);
    }

    function setText(id, text) {
      byId(id).textContent = text || "";
    }

    function clearChildren(element) {
      while (element.firstChild) {
        element.removeChild(element.firstChild);
      }
    }

    /* Show exactly one page section; hide the rest (single-meaning state). */
    function showPage(pageId) {
      var pages = ["page-dashboard", "page-leads", "page-lead-detail",
        "page-appointments", "page-schedule"];
      for (var i = 0; i < pages.length; i++) {
        byId(pages[i]).hidden = pages[i] !== pageId;
      }
      /* The nav highlights the SECTION the visible page belongs to; the
       * detail page belongs to Leads, and Appointments and Schedule are
       * their own sections. */
      var isAppointments = pageId === "page-appointments";
      var isSchedule = pageId === "page-schedule";
      var isDashboard = pageId === "page-dashboard";
      var isLeads = !isAppointments && !isSchedule && !isDashboard;
      byId("nav-dashboard").classList.toggle("portal-nav-active", isDashboard);
      byId("nav-leads").classList.toggle("portal-nav-active", isLeads);
      byId("nav-appointments").classList.toggle("portal-nav-active",
        isAppointments);
      byId("nav-schedule").classList.toggle("portal-nav-active", isSchedule);
    }

    /*
     * Purpose: route one FAILED data outcome to the right visible result.
     * Session outcomes clear the rendered tenant content FIRST (nothing
     * office-specific may linger behind the login view) and then hand
     * control back to the app glue. Every other failure renders its
     * honest message into the given state line.
     */
    function handleFailure(outcome, stateElementId) {
      if (outcome.state === "signed_out" || outcome.state === "unauthorized") {
        resetContent();
        onSessionLost(outcome.state);
        return;
      }
      var message = MESSAGES[outcome.state] || MESSAGES.invalid_response;
      if (outcome.state === "not_found") {
        message = MESSAGES.lead_not_found;
      }
      setText(stateElementId, message);
    }

    /* ---------------------------------------------------------------- */
    /* Dashboard                                                         */
    /* ---------------------------------------------------------------- */

    function renderCounts(body) {
      setText("count-conversations", String(body.total_conversations));
      setText("count-leads", String(body.total_leads));
      setText("count-urgent-leads", String(body.urgent_leads));
      setText("count-recent-leads", String(body.leads_last_7_days));
      byId("dashboard-counts").hidden = false;
    }

    function renderRecentLeads(leads) {
      var list = byId("dashboard-recent");
      clearChildren(list);
      if (!leads || leads.length === 0) {
        setText("dashboard-state", MESSAGES.dashboard_empty);
        return;
      }
      for (var i = 0; i < leads.length; i++) {
        list.appendChild(buildLeadRow(leads[i]));
      }
    }

    function loadDashboard() {
      var requestId = ++requestIds.dashboard;
      setText("dashboard-state", MESSAGES.loading);
      byId("dashboard-counts").hidden = true;
      clearChildren(byId("dashboard-recent"));
      data.getDashboard().then(function (outcome) {
        if (requestId !== requestIds.dashboard) {
          return; /* superseded by a newer request - never overwrite */
        }
        if (!outcome.ok) {
          handleFailure(outcome, "dashboard-state");
          return;
        }
        setText("dashboard-state", "");
        renderCounts(outcome.data);
        renderRecentLeads(outcome.data.recent_leads);
      });
    }

    /* ---------------------------------------------------------------- */
    /* Leads list                                                        */
    /* ---------------------------------------------------------------- */

    /*
     * One lead row: a button (keyboard-accessible) carrying the captured
     * name, contact, status, badges, and last-activity time. All values go
     * through textContent.
     */
    function buildLeadRow(lead) {
      var item = doc.createElement("li");
      item.className = "portal-lead-item";
      var button = doc.createElement("button");
      button.type = "button";
      button.className = "portal-lead-row";

      var name = doc.createElement("span");
      name.className = "portal-lead-name";
      name.textContent = lead.lead_name || "(no name captured)";
      button.appendChild(name);

      var badges = leadBadges(lead);
      for (var i = 0; i < badges.length; i++) {
        var badge = doc.createElement("span");
        badge.className = "portal-badge";
        badge.textContent = badges[i];
        button.appendChild(badge);
      }

      var contact = doc.createElement("span");
      contact.className = "portal-lead-contact";
      contact.textContent = [lead.lead_phone, lead.lead_email]
        .filter(function (value) { return !!value; }).join("  ");
      button.appendChild(contact);

      var meta = doc.createElement("span");
      meta.className = "portal-lead-meta";
      var when = formatTimestamp(lead.last_lead_at || lead.created_at);
      meta.textContent = [statusLabel(lead.lead_status), when]
        .filter(function (value) { return !!value; }).join("  ");
      button.appendChild(meta);

      button.addEventListener("click", function () {
        openLeadDetail(lead.lead_id);
      });
      item.appendChild(button);
      return item;
    }

    function renderLeadsPage(body) {
      var list = byId("leads-list");
      clearChildren(list);
      var filtersActive = leadsQuery.status !== "" || leadsQuery.q !== "";
      if (body.leads.length === 0) {
        setText("leads-state", emptyLeadsMessage(filtersActive));
      } else {
        setText("leads-state", "");
        for (var i = 0; i < body.leads.length; i++) {
          list.appendChild(buildLeadRow(body.leads[i]));
        }
      }
      var pager = pagerModel(body.total, body.limit, body.offset);
      setText("leads-page-label", pager.label);
      byId("leads-prev").disabled = pager.prevDisabled;
      byId("leads-next").disabled = pager.nextDisabled;
    }

    function loadLeads() {
      var requestId = ++requestIds.leads;
      setText("leads-state", MESSAGES.loading);
      data.listLeads({
        status: leadsQuery.status,
        q: leadsQuery.q,
        limit: leadsQuery.limit,
        offset: leadsQuery.offset
      }).then(function (outcome) {
        if (requestId !== requestIds.leads) {
          return; /* superseded - a stale page must never render */
        }
        if (!outcome.ok) {
          handleFailure(outcome, "leads-state");
          return;
        }
        renderLeadsPage(outcome.data);
      });
    }

    /* Filter submit: read the inputs, restart at the FIRST page (an offset
     * kept from an old filter would silently skip results), reload. */
    function onFiltersSubmit(event) {
      event.preventDefault();
      leadsQuery.q = (byId("leads-search").value || "").trim();
      leadsQuery.status = byId("leads-status").value || "";
      leadsQuery.offset = 0;
      loadLeads();
    }

    function onPagerPrev() {
      leadsQuery.offset = pagerModel(0, leadsQuery.limit,
        leadsQuery.offset).prevOffset;
      loadLeads();
    }

    function onPagerNext() {
      leadsQuery.offset = leadsQuery.offset + leadsQuery.limit;
      loadLeads();
    }

    /* ---------------------------------------------------------------- */
    /* Lead detail                                                       */
    /* ---------------------------------------------------------------- */

    function appendDetailField(fields, label, value) {
      var term = doc.createElement("dt");
      term.textContent = label;
      var definition = doc.createElement("dd");
      definition.textContent = value || MESSAGES.value_missing;
      fields.appendChild(term);
      fields.appendChild(definition);
    }

    function renderDetail(body, allow) {
      setText("detail-name", body.lead_name || "(no name captured)");
      setText("detail-badges", leadBadges(body).join("  "));

      var fields = byId("detail-fields");
      clearChildren(fields);
      appendDetailField(fields, "Phone", body.lead_phone);
      appendDetailField(fields, "Email", body.lead_email);
      appendDetailField(fields, "Reason", body.lead_reason);
      appendDetailField(fields, "Status", statusLabel(body.lead_status));
      appendDetailField(fields, "Patient type", body.lead_patient_type);
      appendDetailField(fields, "Preferred time", body.lead_time_window);
      if (body.lead_is_outside_hours) {
        appendDetailField(fields, "After-hours note",
          body.lead_outside_hours_note);
      }
      appendDetailField(fields, "Email opt-out",
        body.lead_email_opt_out ? "Yes" : "No");
      appendDetailField(fields, "Captured", formatTimestamp(body.created_at));
      appendDetailField(fields, "Last activity",
        formatTimestamp(body.last_lead_at));

      /* A2: the transcript is bounded server-side; when it was cut, say
       * so explicitly - the office must never silently read a partial
       * conversation as the whole one. */
      if (body.messages_truncated) {
        setText("detail-transcript-note", "Showing the first " +
          body.messages.length + " of " + body.messages_total +
          " messages.");
      } else {
        setText("detail-transcript-note", "");
      }

      var transcript = byId("detail-messages");
      clearChildren(transcript);
      for (var i = 0; i < body.messages.length; i++) {
        var line = doc.createElement("li");
        line.className = "portal-message portal-message-" +
          (body.messages[i].role === "user" ? "patient" : "assistant");
        var speaker = doc.createElement("span");
        speaker.className = "portal-message-role";
        speaker.textContent =
          body.messages[i].role === "user" ? "Patient" : "Mia";
        var text = doc.createElement("span");
        text.className = "portal-message-content";
        text.textContent = body.messages[i].content;
        line.appendChild(speaker);
        line.appendChild(text);
        transcript.appendChild(line);
      }
      renderWorkflow(body, allow);
      byId("detail-body").hidden = false;
    }

    /* Apply ONE control's authoritative value + token, touching NOTHING
     * that belongs to the sibling control (v1.0.1: a status response also
     * carries a note snapshot that may be OLDER than a note result the
     * office already received, and vice versa - a mutation response may
     * only ever apply its own control's slice). Values are assigned via
     * .value / textContent ONLY - office notes are plain text, never HTML
     * (the task's no-interpretation rule). */
    function applyStatusSlice(body) {
      workflow.statusToken = body.office_status_updated_at;
      byId("detail-office-status").value = body.office_status || "";
      setText("detail-status-meta", body.office_status_updated_at ?
        "Updated " + formatTimestamp(body.office_status_updated_at) : "");
    }

    function applyNoteSlice(body) {
      workflow.noteToken = body.office_note_updated_at;
      byId("detail-office-note").value = body.office_note || "";
      setText("detail-note-meta", body.office_note_updated_at ?
        "Updated " + formatTimestamp(body.office_note_updated_at) : "");
    }

    /* Render the office workflow from an authoritative DETAIL body (page
     * load or conflict refresh). A control whose OWN mutation is still in
     * flight is SKIPPED entirely: its bound response handler owns its
     * value, token, feedback and busy lifecycle (v1.0.1 - a refresh must
     * never re-enable or roll back an in-flight sibling). renderWorkflow
     * no longer touches ANY busy state: busy is owned solely by the
     * submit handlers, their bound response handlers, navigation
     * (openLeadDetail) and the tenant wipe (resetContent). */
    function renderWorkflow(body, allow) {
      workflow.leadId = body.lead_id;
      /* allow.status / allow.note are the caller's per-control verdict.
       * Navigation passes its arrival-time busy check; a conflict refresh
       * passes a verdict computed from the state SNAPSHOTTED when the
       * refresh began (v1.0.2), so a sibling that settled between refresh
       * start and this response can never be rolled back here. */
      if (allow.status) {
        applyStatusSlice(body);
        setText("detail-status-feedback", "");
      }
      if (allow.note) {
        applyNoteSlice(body);
        setText("detail-note-feedback", "");
      }
    }

    function setWorkflowBusy(kind, busy) {
      if (kind === "status") {
        workflow.statusBusy = busy;
        byId("detail-status-save").disabled = busy;
      } else {
        workflow.noteBusy = busy;
        byId("detail-note-save").disabled = busy;
        byId("detail-note-clear").disabled = busy;
      }
    }

    /* One outcome handler for both mutations. Success is claimed ONLY from
     * a validated 200 (no optimistic claims); a 409 refreshes authoritative
     * detail and says the lead changed elsewhere; session loss keeps the
     * existing wipe behavior via handleFailure. */
    function handleWorkflowOutcome(kind, outcome, successMessage) {
      var feedbackId = kind === "status" ?
        "detail-status-feedback" : "detail-note-feedback";
      setWorkflowBusy(kind, false);
      if (outcome.ok) {
        /* Apply ONLY the mutated control's authoritative value + token.
         * The response body also carries the sibling control's snapshot,
         * which may predate a newer sibling result already on screen -
         * it must never be applied from here (v1.0.1). */
        if (kind === "status") {
          applyStatusSlice(outcome.data);
        } else {
          applyNoteSlice(outcome.data);
        }
        setText(feedbackId, successMessage);
        return;
      }
      if (outcome.state === "conflict") {
        /* Refresh FIRST (renderWorkflow clears the non-busy feedback
         * lines), then say the lead changed elsewhere - so the notice
         * survives the redraw and sits beside the authoritative latest
         * state. refreshLeadDetail - NOT openLeadDetail - keeps the
         * sibling control's in-flight request fully protected (v1.0.1). */
        var refreshedLead = workflow.leadId;
        refreshLeadDetail().then(function () {
          if (workflow.leadId === refreshedLead) {
            setText(feedbackId, MESSAGES.workflow_conflict);
          }
        });
        return;
      }
      if (outcome.state === "signed_out" ||
          outcome.state === "unauthorized") {
        handleFailure(outcome, feedbackId); /* existing wipe behavior */
        return;
      }
      var message = MESSAGES[outcome.state] || MESSAGES.workflow_failed;
      if (outcome.state === "bad_request") {
        message = MESSAGES.workflow_failed;
      }
      if (outcome.state === "not_found") {
        message = MESSAGES.lead_not_found;
      }
      setText(feedbackId, message);
    }

    function onStatusSave() {
      if (workflow.statusBusy || workflow.leadId === null) {
        return;                            /* duplicate submit blocked */
      }
      var raw = byId("detail-office-status").value;
      var requested = raw === "" ? null : raw;   /* portal clear = null */
      setWorkflowBusy("status", true);
      setText("detail-status-feedback", "");
      var seq = ++workflow.statusSeq;
      /* The response is BOUND to the lead that was authoritative when the
       * request left (v1.0.1): once another lead is opened, this response
       * must be ignored no matter what the sequence says. */
      var leadAtRequest = workflow.leadId;
      data.putLeadStatus(leadAtRequest, requested, workflow.statusToken)
        .then(function (outcome) {
          if (seq !== workflow.statusSeq ||
              workflow.leadId !== leadAtRequest) {
            return;                        /* superseded - never overwrite */
          }
          handleWorkflowOutcome("status", outcome,
            MESSAGES.workflow_saved_status);
        });
    }

    function submitNote(noteValue, successMessage) {
      if (workflow.noteBusy || workflow.leadId === null) {
        return;                            /* duplicate submit blocked */
      }
      setWorkflowBusy("note", true);
      setText("detail-note-feedback", "");
      var seq = ++workflow.noteSeq;
      /* Same lead binding as the status writer (v1.0.1). */
      var leadAtRequest = workflow.leadId;
      data.putLeadNote(leadAtRequest, noteValue, workflow.noteToken)
        .then(function (outcome) {
          if (seq !== workflow.noteSeq ||
              workflow.leadId !== leadAtRequest) {
            return;                        /* superseded - never overwrite */
          }
          handleWorkflowOutcome("note", outcome, successMessage);
        });
    }

    function onNoteSave() {
      var text = byId("detail-office-note").value;
      if (text.replace(/^\s+|\s+$/g, "") === "") {
        /* Whitespace-only is not a save; clearing is the explicit button.
         * The server enforces the same rule authoritatively. */
        setText("detail-note-feedback", MESSAGES.workflow_note_empty);
        return;
      }
      submitNote(text, MESSAGES.workflow_saved_note);
    }

    function onNoteClear() {
      submitNote(null, MESSAGES.workflow_cleared_note);
    }

    function openLeadDetail(leadId) {
      showPage("page-lead-detail");
      var requestId = ++requestIds.detail;
      /* The lead being OPENED is authoritative from this moment (v1.0.1).
       * Any mutation still in flight belongs to a lead no longer shown:
       * invalidate its response (sequence bump + the per-request lead
       * binding) and hand the new lead enabled controls. Navigation - not
       * renderWorkflow - owns this reset. */
      workflow.leadId = leadId;
      workflow.statusSeq += 1;
      workflow.noteSeq += 1;
      setWorkflowBusy("status", false);
      setWorkflowBusy("note", false);
      setText("detail-state", MESSAGES.loading);
      byId("detail-body").hidden = true;
      return data.getLeadDetail(leadId).then(function (outcome) {
        if (requestId !== requestIds.detail) {
          return; /* superseded */
        }
        if (!outcome.ok) {
          handleFailure(outcome, "detail-state");
          return;
        }
        setText("detail-state", "");
        renderDetail(outcome.data, {
          status: !workflow.statusBusy,
          note: !workflow.noteBusy
        });
      });
    }

    /* Re-fetch the authoritative detail for the lead CURRENTLY shown,
     * after a concurrency conflict (v1.0.1). Unlike navigation this must
     * not disturb the sibling control's in-flight request: no sequence is
     * invalidated and no busy flag is touched here - renderWorkflow skips
     * any control whose own mutation is still pending. */
    function refreshLeadDetail() {
      var leadId = workflow.leadId;
      var requestId = ++requestIds.detail;
      /* Per-control state SNAPSHOT at refresh initiation (v1.0.2 F4
       * residual-race fix). The v1.0.1 gate read only the busy flag when
       * the refresh RESPONSE arrived; a sibling mutation that was in
       * flight when the refresh STARTED could settle (clearing its busy
       * flag and applying a newer value + token) before this response
       * arrived, and then be rolled back by this now-stale snapshot. A
       * control's refresh slice is applied ONLY when, from refresh start
       * to response arrival, that control (a) had NO mutation in flight
       * at the start, (b) did NOT advance - no new mutation was submitted
       * (its generation counter is unchanged), and (c) has no mutation in
       * flight now. The conflicted control itself cleared its busy flag
       * BEFORE this refresh began, so it still refreshes to authoritative
       * state; only a genuinely newer sibling result is protected. */
      var statusBusyAtStart = workflow.statusBusy;
      var noteBusyAtStart = workflow.noteBusy;
      var statusSeqAtStart = workflow.statusSeq;
      var noteSeqAtStart = workflow.noteSeq;
      setText("detail-state", MESSAGES.loading);
      byId("detail-body").hidden = true;
      return data.getLeadDetail(leadId).then(function (outcome) {
        if (requestId !== requestIds.detail ||
            workflow.leadId !== leadId) {
          return; /* superseded or another lead became authoritative */
        }
        if (!outcome.ok) {
          handleFailure(outcome, "detail-state");
          return;
        }
        setText("detail-state", "");
        renderDetail(outcome.data, {
          status: !statusBusyAtStart &&
                  workflow.statusSeq === statusSeqAtStart &&
                  !workflow.statusBusy,
          note: !noteBusyAtStart &&
                workflow.noteSeq === noteSeqAtStart &&
                !workflow.noteBusy
        });
      });
    }

    function onDetailBack() {
      showPage("page-leads");
    }

    /* ---------------------------------------------------------------- */
    /* Appointments (read-only)                                          */
    /* ---------------------------------------------------------------- */

    /*
     * Purpose: build ONE appointment list row via textContent only (never
     * markup), so captured patient text can never inject HTML into the
     * office's browser (the buildLeadRow convention). Times are formatted in
     * the office timezone the backend returned, NOT the device timezone.
     */
    function buildAppointmentRow(appointment, timezoneName) {
      var item = doc.createElement("li");
      item.className = "portal-lead-item";
      var row = doc.createElement("div");
      row.className = "portal-lead-row";

      var name = doc.createElement("span");
      name.className = "portal-lead-name";
      name.textContent = appointment.patient_name || "(no name)";
      row.appendChild(name);

      /* When (start) in the OFFICE timezone. */
      var when = doc.createElement("span");
      when.className = "portal-lead-meta";
      when.textContent = formatInTimeZone(appointment.start_datetime,
        timezoneName);
      row.appendChild(when);

      /* Contact (phone, optional email). */
      var contact = doc.createElement("span");
      contact.className = "portal-lead-contact";
      contact.textContent = [appointment.patient_phone,
        appointment.patient_email]
        .filter(function (value) { return !!value; }).join("  ");
      row.appendChild(contact);

      /* Status + patient type + urgency + notification outcome badges. */
      var badges = [
        appointmentStatusLabel(appointment.status),
        appointment.new_or_returning || "",
        appointment.urgency || "",
        notificationOutcomeLabel(appointment.notification_outcome)
      ].filter(function (value) { return !!value; });
      for (var i = 0; i < badges.length; i++) {
        var badge = doc.createElement("span");
        badge.className = "portal-badge";
        badge.textContent = badges[i];
        row.appendChild(badge);
      }

      /* Reason/service context, when present. */
      if (appointment.reason) {
        var reason = doc.createElement("span");
        reason.className = "portal-lead-contact";
        reason.textContent = appointment.reason;
        row.appendChild(reason);
      }

      /* P5-A: per-appointment action controls, only where the lifecycle
       * owner would accept them. Both this row's controls share ONE
       * rowButtons list so a mutation in flight disables BOTH (C5), never
       * another row's. Cancel is a deliberate inline two-click (C4): the
       * first click arms THIS appointment, the second performs it. */
      var apptId = appointment.appointment_id;
      var actions = appointmentActionsFor(appointment.status);
      if (actions.length > 0) {
        var busy = appointments.actionBusy[apptId] !== undefined;
        var group = doc.createElement("span");
        group.className = "portal-appt-actions";
        var rowButtons = [];
        if (actions.indexOf("confirm") !== -1) {
          var confirmBtn = doc.createElement("button");
          confirmBtn.type = "button";
          confirmBtn.className = "portal-button portal-button-secondary";
          confirmBtn.textContent = "Confirm";
          confirmBtn.disabled = busy;
          confirmBtn.addEventListener("click", function () {
            onAppointmentAction(apptId, data.confirmAppointment, rowButtons,
              MESSAGES.appointment_confirmed);
          });
          rowButtons.push(confirmBtn);
          group.appendChild(confirmBtn);
        }
        if (actions.indexOf("cancel") !== -1) {
          var armed = appointments.armed[apptId] === true;
          var cancelBtn = doc.createElement("button");
          cancelBtn.type = "button";
          cancelBtn.className = "portal-button portal-button-secondary";
          cancelBtn.textContent = armed ? "Confirm cancel" : "Cancel";
          cancelBtn.disabled = busy;
          cancelBtn.addEventListener("click", function () {
            onAppointmentCancelClick(apptId, cancelBtn, rowButtons);
          });
          rowButtons.push(cancelBtn);
          group.appendChild(cancelBtn);
        }
        row.appendChild(group);
      }

      item.appendChild(row);
      return item;
    }

    function renderAppointmentsPage(body) {
      /* P5-A: an authoritative refresh (this render) DISARMS every row's
       * inline Cancel confirmation - stale two-click state must never
       * survive a reload (C4). actionBusy is NOT cleared here: a mutation in
       * flight during a concurrent refresh keeps its per-row controls
       * disabled (their disabled state is read from actionBusy at build). */
      appointments.armed = {};

      /* Record the bounds the backend actually used, so week navigation is
       * always relative to real DST-safe local dates. On the default view
       * (weekOffset 0) the echoed start_day IS the office-local "today". */
      appointments.currentStart = body.start_day;
      appointments.currentEnd = body.end_day;
      if (appointments.weekOffset === 0) {
        appointments.defaultStart = body.start_day;
      }

      setText("appt-timezone-note",
        MESSAGES.appointments_tz_note_prefix + body.timezone_name);
      setText("appt-range-label",
        appointmentsRangeLabel(body.start_day, body.end_day));

      var list = byId("appointments-list");
      clearChildren(list);
      if (!body.appointments || body.appointments.length === 0) {
        setText("appointments-state", MESSAGES.appointments_empty);
      } else {
        setText("appointments-state", "");
        for (var i = 0; i < body.appointments.length; i++) {
          list.appendChild(
            buildAppointmentRow(body.appointments[i], body.timezone_name));
        }
      }
      /* F3: navigation is only safe once an AUTHORITATIVE default start is
       * established by a resolved default (weekOffset 0) response. Both
       * controls stay disabled until then, so a Next/Previous click can
       * never compute an explicit range from a stale or absent anchor. Once
       * an anchor exists they are enabled (the office may look back or
       * ahead). Re-entry clears the anchor (openAppointments), so a fresh
       * visit re-disables until its own default resolves. */
      var haveAnchor = appointments.defaultStart !== null;
      byId("appt-prev").disabled = !haveAnchor;
      byId("appt-next").disabled = !haveAnchor;
    }

    /*
     * Purpose: load the appointments for the current week offset. Offset 0
     * sends NO bounds (the backend default, office-anchored). A non-zero
     * offset sends explicit start_day/end_day computed by shifting the
     * known default start by whole weeks - so navigation never depends on a
     * browser-computed "today".
     * Stale-response guard (C4): a superseded response, OR a read issued
     * BEFORE a mutation began (mutation-generation mismatch), is dropped -
     * so a stale read can never roll the list back behind a mutation's
     * authoritative refresh.
     */
    function loadAppointments() {
      var requestId = ++requestIds.appointments;
      var generationAtIssue = appointments.generation;
      setText("appointments-state", MESSAGES.loading);

      var params = {};
      if (appointments.weekOffset !== 0) {
        /* F3: a non-zero offset REQUIRES an authoritative anchor. If none is
         * established (should be unreachable - the controls are disabled and
         * the handlers guard - but defended here too), fall back to the
         * default week rather than computing a range from a null anchor. */
        if (appointments.defaultStart === null) {
          appointments.weekOffset = 0;
        } else {
          var start = shiftLocalDay(appointments.defaultStart,
            appointments.weekOffset * 7);
          var end = shiftLocalDay(start, 6);
          if (start !== "" && end !== "") {
            params = { start_day: start, end_day: end };
          } else {
            /* A malformed anchor can never drive a request. */
            appointments.weekOffset = 0;
          }
        }
      }

      data.getAppointments(params).then(function (outcome) {
        if (requestId !== requestIds.appointments ||
            generationAtIssue !== appointments.generation) {
          return; /* superseded or pre-mutation - never applied (C4) */
        }
        if (!outcome.ok) {
          handleFailure(outcome, "appointments-state");
          return;
        }
        renderAppointmentsPage(outcome.data);
      });
    }

    /*
     * Purpose (P5-A): the FIRST click of Cancel arms THIS appointment
     * (no network, no window.confirm); the SECOND explicit click performs
     * it. A mutation already in flight for the same appointment suppresses
     * both arming and performing (C5). Arming is per-appointment, so one
     * row's armed state never affects another's.
     */
    function onAppointmentCancelClick(appointmentId, cancelButton, rowButtons) {
      if (appointments.actionBusy[appointmentId] !== undefined) {
        return; /* a mutation is in flight for this appointment (C5) */
      }
      if (appointments.armed[appointmentId] !== true) {
        appointments.armed[appointmentId] = true;
        cancelButton.textContent = "Confirm cancel";
        setText("appt-action-feedback", MESSAGES.appointment_cancel_arm);
        return;
      }
      onAppointmentAction(appointmentId, data.cancelAppointment, rowButtons,
        MESSAGES.appointment_cancelled);
    }

    /*
     * Purpose (P5-A): perform ONE appointment mutation (Confirm or the
     * armed Cancel) and settle it authoritatively. Guards: a duplicate
     * submit while this appointment is in flight is suppressed (C5); BOTH
     * of this row's controls are disabled for the duration; a new mutation
     * generation is opened so any window read issued before now is
     * discarded (C4). Optimistic state is NEVER rendered - only the
     * authoritative re-GET the settler triggers.
     * External effects: one POST via the data layer; then one GET on settle.
     */
    function onAppointmentAction(appointmentId, call, rowButtons, successMessage) {
      if (appointments.actionBusy[appointmentId] !== undefined) {
        return; /* duplicate submit while in flight (C5) */
      }
      /* F4: this mutation OWNS the row via a unique token, not an unowned
       * boolean. A later same-appointment mutation stores a different token,
       * so this mutation's late completion can only clear ITS OWN ownership. */
      var token = ++appointments.actionSeq;
      appointments.actionBusy[appointmentId] = token;
      appointments.generation += 1;   /* C4: a mutation begins */
      var lifecycleAtIssue = appointments.lifecycle;
      var generationAtIssue = appointments.generation;
      for (var i = 0; i < rowButtons.length; i++) {
        rowButtons[i].disabled = true;   /* C5: both row controls disabled */
      }
      setText("appt-action-feedback", "");
      call(appointmentId).then(function (outcome) {
        /* Release ownership ONLY if this mutation still owns the row (F4).
         * If a reset cleared it and a newer mutation took the row, its token
         * differs and we must NOT delete the newer owner's entry. */
        if (appointments.actionBusy[appointmentId] === token) {
          delete appointments.actionBusy[appointmentId];
        }
        settleAppointmentMutation(lifecycleAtIssue, generationAtIssue,
          outcome, successMessage);
      });
    }

    /*
     * Purpose (P5-A): THE single settling point for an appointment mutation
     * response (the P4-A settleScheduleMutation discipline). Guard order:
     *   1. STALE LIFECYCLE (sign-out / reset / page re-entry happened while
     *      in flight): render NOTHING and trigger NO GET - the wipe stands.
     *   2. Session-loss outcome: wipe rendered tenant content and hand back
     *      to the sign-in flow.
     *   3. STALE GENERATION (a newer mutation already owns the surface): a
     *      SUCCESSFUL older commit still changed the server, so fetch the
     *      current truth; a failed older attempt is simply dropped.
     *   4. Current: show the honest message and ALWAYS re-GET authoritative
     *      state (never optimistic) - on success AND on every failure, so a
     *      conflict/not_found/unavailable refreshes to the real list.
     */
    function settleAppointmentMutation(lifecycleAtIssue, generationAtIssue,
        outcome, successMessage) {
      if (lifecycleAtIssue !== appointments.lifecycle) {
        return; /* an independent reset/logout/re-entry won - stay wiped */
      }
      if (!outcome.ok && (outcome.state === "signed_out" ||
          outcome.state === "unauthorized")) {
        resetContent();
        onSessionLost(outcome.state);
        return;
      }
      if (generationAtIssue !== appointments.generation) {
        /* A newer mutation owns feedback and rows now. */
        if (outcome.ok) {
          loadAppointments(); /* the older commit changed the server */
        }
        return;
      }
      if (!outcome.ok) {
        var message;
        if (outcome.state === "conflict") {
          message = MESSAGES.appointment_action_conflict;
        } else if (outcome.state === "not_found") {
          message = MESSAGES.appointment_gone;
        } else {
          message = MESSAGES[outcome.state] || MESSAGES.appointment_action_failed;
        }
        setText("appt-action-feedback", message);
        loadAppointments(); /* refresh to authoritative state */
        return;
      }
      setText("appt-action-feedback", successMessage);
      loadAppointments(); /* authoritative state only - never optimistic */
    }

    function onApptPrev() {
      /* F3 guard: refuse to navigate from a stale/absent anchor. The
       * control is disabled until an anchor exists, and this guard makes the
       * handler itself safe even if a click races the disable. */
      if (appointments.defaultStart === null) {
        return;
      }
      appointments.weekOffset -= 1;
      loadAppointments();
    }

    function onApptNext() {
      if (appointments.defaultStart === null) {
        return;
      }
      appointments.weekOffset += 1;
      loadAppointments();
    }

    function openAppointments() {
      /* F3: re-enter at the default week AND clear the previous visit's
       * anchor and echoed bounds, so navigation is impossible from a stale
       * defaultStart while the fresh default GET is still in flight. The
       * bumped request id (in loadAppointments) plus the disabled controls
       * (rendered only once the fresh default resolves) close the ordering
       * hole ChatGPT reproduced. */
      appointments.lifecycle += 1;   /* P5-A: page re-entry - a mutation
       * still in flight from the PREVIOUS visit may no longer render
       * feedback or trigger a GET when it resolves. */
      appointments.weekOffset = 0;
      appointments.defaultStart = null;
      appointments.currentStart = null;
      appointments.currentEnd = null;
      appointments.actionBusy = {};
      appointments.armed = {};
      setText("appt-action-feedback", "");
      byId("appt-prev").disabled = true;
      byId("appt-next").disabled = true;
      showPage("page-appointments");
      loadAppointments();
    }

    /* ---------------------------------------------------------------- */
    /* Schedule (P4-A - Portal Slot Schedule Controls v1)                */
    /* ---------------------------------------------------------------- */

    /*
     * Purpose: THE single settling point for every schedule MUTATION
     * response (audit F3). The caller has already cleared its own busy
     * flag (always safe). Guard order:
     *   1. STALE LIFECYCLE (an independent sign-out / reset / page
     *      re-entry happened while the mutation was in flight): render
     *      NOTHING, trigger NO schedule GET - the wipe must stand.
     *   2. Session-loss outcome: wipe and hand control back (the shared
     *      rule) - the session is genuinely dead regardless of ordering.
     *   3. STALE GENERATION (a NEWER mutation began after this one): this
     *      response's BODY and feedback no longer own the UI - no feedback
     *      is written and nothing renders from the response itself. BUT
     *      (audit F5) if the older mutation completed SUCCESSFULLY it DID
     *      change the server truth, and a refresh rendered before it
     *      committed may now be stale - so one fresh authoritative
     *      loadSchedule() is triggered at the CURRENT generation, which
     *      the request-sequence + generation guards allow to supersede any
     *      earlier read. A still-current older-generation FAILURE changed
     *      nothing that its own error wording would describe, so it stays
     *      fully suppressed (no unnecessary behavior).
     *   4. Failure: honest wording; every non-session failure that may
     *      reflect a server-side change also triggers the authoritative
     *      refresh.
     *   5. Success: the caller's onSuccess renders its feedback, then the
     *      authoritative refresh - the mutation response body is never
     *      applied to the grid optimistically.
     */
    function settleScheduleMutation(lifecycleAtIssue, generationAtIssue,
        outcome, feedbackId, messages, onSuccess) {
      if (lifecycleAtIssue !== schedule.lifecycle) {
        return; /* F3: reset/logout while in flight - nothing may render */
      }
      if (!outcome.ok && (outcome.state === "signed_out" ||
          outcome.state === "unauthorized")) {
        resetContent();
        onSessionLost(outcome.state);
        return;
      }
      if (generationAtIssue !== schedule.generation) {
        /* A newer mutation owns feedback and rows. F5: a SUCCESSFUL older
         * commit still changed the server, so fetch the current truth. */
        if (outcome.ok) {
          loadSchedule();
        }
        return;
      }
      if (!outcome.ok) {
        var message;
        if (outcome.state === "conflict") {
          message = messages.conflict;
        } else if (outcome.state === "not_found") {
          message = MESSAGES.schedule_slot_gone;
        } else if (outcome.state === "bad_request") {
          message = messages.bad_request;
        } else {
          message = MESSAGES[outcome.state] || MESSAGES.invalid_response;
        }
        setText(feedbackId, message);
        loadSchedule();
        return;
      }
      onSuccess(outcome);
      loadSchedule(); /* authoritative state only */
    }

    /* One slot row: the local time range (formatted in the OFFICE timezone
     * from the envelope - never device time), the status label, and - only
     * where the backend would accept it - ONE action button (Block for
     * open/held rows, Unblock for blocked rows; booked and cancelled rows
     * get no button). Every value goes through textContent. */
    function buildScheduleRow(slot, timezoneName) {
      var item = doc.createElement("li");
      item.className = "portal-lead-item";
      var row = doc.createElement("div");
      row.className = "portal-schedule-row";

      var when = doc.createElement("span");
      when.className = "portal-schedule-when";
      when.textContent = formatInTimeZone(slot.start_datetime, timezoneName) +
        " - " + formatInTimeZone(slot.end_datetime, timezoneName);
      row.appendChild(when);

      var status = doc.createElement("span");
      status.className = "portal-schedule-status portal-muted";
      status.textContent = scheduleSlotStatusLabel(slot.status);
      row.appendChild(status);

      var action = null;
      if (slot.status === "available" || slot.status === "held") {
        action = { label: "Block", call: data.blockScheduleSlot };
      } else if (slot.status === "blocked") {
        action = { label: "Unblock", call: data.unblockScheduleSlot };
      }
      if (action !== null) {
        var button = doc.createElement("button");
        button.type = "button";
        button.className = "portal-button portal-button-secondary";
        button.textContent = action.label;
        /* Per-slot duplicate-submit guard: ONLY this slot's control is
         * disabled while its action is in flight (C4 independence). */
        button.disabled = schedule.slotBusy[slot.slot_id] === true;
        button.addEventListener("click", function () {
          onSlotAction(slot.slot_id, action.call, button);
        });
        row.appendChild(button);
      }

      item.appendChild(row);
      return item;
    }

    function renderSchedulePage(body) {
      schedule.currentStart = body.start_day;
      schedule.currentEnd = body.end_day;
      if (schedule.weekOffset === 0) {
        schedule.defaultStart = body.start_day;
      }
      setText("schedule-timezone-note",
        MESSAGES.appointments_tz_note_prefix + body.timezone_name);
      setText("schedule-range-label",
        appointmentsRangeLabel(body.start_day, body.end_day));

      var list = byId("schedule-list");
      clearChildren(list);
      if (!body.slots || body.slots.length === 0) {
        setText("schedule-state", MESSAGES.schedule_empty);
      } else {
        setText("schedule-state", "");
        for (var i = 0; i < body.slots.length; i++) {
          list.appendChild(buildScheduleRow(body.slots[i],
            body.timezone_name));
        }
      }
      var haveAnchor = schedule.defaultStart !== null;
      byId("schedule-prev").disabled = !haveAnchor;
      byId("schedule-next").disabled = !haveAnchor;
    }

    /*
     * Purpose: load the schedule window for the current week offset under
     * the DUAL guard (Correction C4): the response is applied only when its
     * request id is still current AND the mutation generation captured at
     * issue time still equals the current generation. A GET issued before a
     * mutation began - however late it resolves - is discarded silently, so
     * rendered state can never roll back behind a mutation's authoritative
     * refresh. Offset semantics mirror the appointments page (F3 anchor).
     */
    function loadSchedule() {
      var requestId = ++requestIds.schedule;
      var generationAtIssue = schedule.generation;
      setText("schedule-state", MESSAGES.loading);

      var params = {};
      if (schedule.weekOffset !== 0) {
        if (schedule.defaultStart === null) {
          schedule.weekOffset = 0;
        } else {
          var start = shiftLocalDay(schedule.defaultStart,
            schedule.weekOffset * 7);
          var end = shiftLocalDay(start, 6);
          if (start !== "" && end !== "") {
            params = { start_day: start, end_day: end };
          } else {
            schedule.weekOffset = 0;
          }
        }
      }

      data.getSchedule(params).then(function (outcome) {
        if (requestId !== requestIds.schedule ||
            generationAtIssue !== schedule.generation) {
          return; /* superseded or pre-mutation - never applied (C4) */
        }
        if (!outcome.ok) {
          handleFailure(outcome, "schedule-state");
          return;
        }
        renderSchedulePage(outcome.data);
      });
    }

    function onSchedulePrev() {
      if (schedule.defaultStart === null) {
        return;
      }
      schedule.weekOffset -= 1;
      loadSchedule();
    }

    function onScheduleNext() {
      if (schedule.defaultStart === null) {
        return;
      }
      schedule.weekOffset += 1;
      loadSchedule();
    }

    /*
     * Purpose: one per-slot mutation (Block or Unblock). Marks the mutation
     * generation FIRST (invalidating every in-flight window read), guards
     * duplicate submission for THIS slot only, and on completion renders
     * ONLY the authoritative refresh - the response body is never applied
     * to the grid optimistically.
     */
    function onSlotAction(slotId, call, button) {
      if (schedule.slotBusy[slotId] === true) {
        return; /* duplicate submit while in flight */
      }
      schedule.slotBusy[slotId] = true;
      schedule.generation += 1;   /* C4: mutation begins */
      var lifecycleAtIssue = schedule.lifecycle;      /* F3 capture */
      var generationAtIssue = schedule.generation;
      button.disabled = true;
      setText("schedule-action-feedback", "");
      call(slotId).then(function (outcome) {
        delete schedule.slotBusy[slotId];  /* busy clearing is always safe */
        settleScheduleMutation(lifecycleAtIssue, generationAtIssue, outcome,
          "schedule-action-feedback", {
            conflict: MESSAGES.schedule_action_conflict,
            bad_request: MESSAGES.schedule_action_conflict
          }, function () { /* nothing beyond the refresh to render */ });
      });
    }

    /*
     * Purpose: publish one local day's hours. Reads the three form values
     * (slot length defaults to 30 in the markup - the contract's portal
     * default), refuses an empty day locally, and follows the same
     * mutation-generation + authoritative-refresh discipline.
     */
    function onSchedulePublish() {
      if (schedule.publishBusy) {
        return;
      }
      var day = byId("schedule-day").value;
      if (!day) {
        setText("schedule-publish-feedback", MESSAGES.schedule_day_required);
        return;
      }
      var openTime = byId("schedule-open").value;
      var closeTime = byId("schedule-end").value;
      var slotMinutes = parseInt(byId("schedule-minutes").value, 10);
      if (!isFinite(slotMinutes)) {
        slotMinutes = 0; /* a non-numeric length is refused by the backend */
      }
      schedule.publishBusy = true;
      schedule.generation += 1;   /* C4: mutation begins */
      var lifecycleAtIssue = schedule.lifecycle;      /* F3 capture */
      var generationAtIssue = schedule.generation;
      byId("schedule-publish").disabled = true;
      setText("schedule-publish-feedback", "");
      data.publishScheduleDay(day, openTime, closeTime, slotMinutes)
        .then(function (outcome) {
          schedule.publishBusy = false;  /* busy clearing is always safe */
          if (lifecycleAtIssue === schedule.lifecycle) {
            byId("schedule-publish").disabled = false;
          } /* after a reset the control was already reset by the wipe */
          settleScheduleMutation(lifecycleAtIssue, generationAtIssue,
            outcome, "schedule-publish-feedback", {
              conflict: MESSAGES.schedule_publish_conflict,
              bad_request: MESSAGES.schedule_publish_rejected
            }, function (settled) {
              setText("schedule-publish-feedback",
                "Published " + settled.data.length + " slots.");
            });
        });
    }

    /*
     * Purpose: block every currently open slot on the selected day (a SLOT
     * operation - the permanent note beside the control states that
     * publishing later reopens the day; the words close/closed/closure are
     * deliberately absent). Renders the blocked count and the still-booked
     * windows from the response, then the authoritative refresh.
     */
    function onScheduleBlockAll() {
      if (schedule.bulkBusy) {
        return;
      }
      var day = byId("schedule-day").value;
      if (!day) {
        setText("schedule-bulk-feedback", MESSAGES.schedule_day_required);
        return;
      }
      schedule.bulkBusy = true;
      schedule.generation += 1;   /* C4: mutation begins */
      var lifecycleAtIssue = schedule.lifecycle;      /* F3 capture */
      var generationAtIssue = schedule.generation;
      byId("schedule-block-all").disabled = true;
      setText("schedule-bulk-feedback", "");
      setText("schedule-booked-remaining", "");
      data.blockAllOpenSlots(day).then(function (outcome) {
        schedule.bulkBusy = false;     /* busy clearing is always safe */
        if (lifecycleAtIssue === schedule.lifecycle) {
          byId("schedule-block-all").disabled = false;
        } /* after a reset the control was already reset by the wipe */
        settleScheduleMutation(lifecycleAtIssue, generationAtIssue, outcome,
          "schedule-bulk-feedback", {
            conflict: MESSAGES.schedule_action_conflict,
            bad_request: MESSAGES.schedule_action_conflict
          }, function (settled) {
            setText("schedule-bulk-feedback",
              "Blocked " + settled.data.blocked_count + " slots.");
            var remaining = settled.data.booked_remaining;
            if (remaining.length > 0) {
              var tz = byId("schedule-timezone-note").textContent
                .slice(MESSAGES.appointments_tz_note_prefix.length) || "UTC";
              var windows = [];
              for (var i = 0; i < remaining.length; i++) {
                windows.push(
                  formatInTimeZone(remaining[i].start_datetime, tz));
              }
              setText("schedule-booked-remaining",
                MESSAGES.schedule_booked_remaining_prefix +
                windows.join("; "));
            }
          });
      });
    }

    function openSchedule() {
      /* Re-enter at the default week AND clear the previous visit's anchor
       * (the frozen appointments discipline), so navigation is impossible
       * from a stale anchor while the fresh default GET is in flight.
       * Audit F3: re-entry is a page reset - bump the lifecycle so any
       * mutation still in flight from the PREVIOUS visit can no longer
       * render feedback or trigger a schedule GET when it resolves. */
      schedule.lifecycle += 1;
      schedule.weekOffset = 0;
      schedule.defaultStart = null;
      schedule.currentStart = null;
      schedule.currentEnd = null;
      byId("schedule-prev").disabled = true;
      byId("schedule-next").disabled = true;
      setText("schedule-publish-feedback", "");
      setText("schedule-action-feedback", "");
      setText("schedule-bulk-feedback", "");
      setText("schedule-booked-remaining", "");
      showPage("page-schedule");
      loadSchedule();
    }

    /* ---------------------------------------------------------------- */
    /* Entry, reset, wiring                                              */
    /* ---------------------------------------------------------------- */

    /*
     * Purpose: wipe every rendered tenant-specific value and return the
     * pages to their pre-entry state. Called on sign-out and before any
     * hand-back to the login view, so no office data can linger on a
     * shared front-desk computer.
     */
    function resetContent() {
      requestIds.dashboard += 1;   /* invalidate any in-flight responses */
      requestIds.leads += 1;
      requestIds.detail += 1;
      requestIds.appointments += 1;
      /* P5-A (C4): session loss invalidates BOTH appointment counters and
       * clears every in-flight per-row guard/armed state, so no in-flight
       * window read NOR mutation result may apply afterwards. */
      appointments.generation += 1;
      appointments.lifecycle += 1;
      appointments.actionBusy = {};
      appointments.armed = {};
      /* P4-A (C4): session loss invalidates BOTH schedule counters - no
       * in-flight window read NOR any in-flight mutation result may apply
       * afterwards - and wipes every rendered schedule value. */
      requestIds.schedule += 1;
      schedule.generation += 1;
      schedule.lifecycle += 1;   /* F3: in-flight mutations may not render */
      schedule.weekOffset = 0;
      schedule.defaultStart = null;
      schedule.currentStart = null;
      schedule.currentEnd = null;
      schedule.publishBusy = false;
      schedule.bulkBusy = false;
      schedule.slotBusy = {};
      setText("schedule-timezone-note", "");
      setText("schedule-range-label", "");
      setText("schedule-state", "");
      setText("schedule-publish-feedback", "");
      setText("schedule-action-feedback", "");
      setText("schedule-bulk-feedback", "");
      setText("schedule-booked-remaining", "");
      clearChildren(byId("schedule-list"));
      byId("schedule-day").value = "";
      byId("schedule-open").value = "";
      byId("schedule-end").value = "";
      byId("schedule-minutes").value = "30";
      byId("schedule-publish").disabled = false;
      byId("schedule-block-all").disabled = false;
      byId("schedule-prev").disabled = true;
      byId("schedule-next").disabled = true;
      leadsQuery = { status: "", q: "", limit: LIST_PAGE_LIMIT, offset: 0 };
      /* Appointments week-navigation state and rendered content wipe: no
       * office's appointment times or patient contact may linger behind the
       * login view on a shared front-desk computer. */
      appointments.weekOffset = 0;
      appointments.defaultStart = null;
      appointments.currentStart = null;
      appointments.currentEnd = null;
      setText("appointments-state", "");
      setText("appt-range-label", "");
      setText("appt-timezone-note", "");
      clearChildren(byId("appointments-list"));
      setText("appt-action-feedback", "");
      setText("dashboard-state", "");
      byId("dashboard-counts").hidden = true;
      setText("count-conversations", "");
      setText("count-leads", "");
      setText("count-urgent-leads", "");
      setText("count-recent-leads", "");
      clearChildren(byId("dashboard-recent"));
      setText("leads-state", "");
      clearChildren(byId("leads-list"));
      setText("leads-page-label", "");
      byId("leads-search").value = "";
      byId("leads-status").value = "";
      setText("detail-state", "");
      setText("detail-name", "");
      setText("detail-badges", "");
      setText("detail-transcript-note", "");
      clearChildren(byId("detail-fields"));
      clearChildren(byId("detail-messages"));
      workflow.leadId = null;              /* P3-B2 tenant-data wipe */
      workflow.statusToken = null;
      workflow.noteToken = null;
      workflow.statusBusy = false;
      workflow.noteBusy = false;
      workflow.statusSeq += 1;             /* invalidate in-flight writes */
      workflow.noteSeq += 1;
      byId("detail-office-status").value = "";
      byId("detail-office-note").value = "";
      setText("detail-status-meta", "");
      setText("detail-note-meta", "");
      setText("detail-status-feedback", "");
      setText("detail-note-feedback", "");
      byId("detail-status-save").disabled = false;
      byId("detail-note-save").disabled = false;
      byId("detail-note-clear").disabled = false;
      byId("detail-body").hidden = true;
    }

    /* Enter (or re-enter after a fresh sign-in): always lands on a fresh
     * Dashboard - nothing is trusted from a previous session's render. */
    function enter() {
      resetContent();
      showPage("page-dashboard");
      loadDashboard();
    }

    function wireEvents() {
      byId("nav-dashboard").addEventListener("click", function () {
        showPage("page-dashboard");
        loadDashboard();
      });
      byId("nav-leads").addEventListener("click", function () {
        showPage("page-leads");
        loadLeads();
      });
      byId("nav-appointments").addEventListener("click", function () {
        openAppointments();
      });
      byId("nav-schedule").addEventListener("click", function () {
        openSchedule();
      });
      byId("appt-prev").addEventListener("click", onApptPrev);
      byId("appt-next").addEventListener("click", onApptNext);
      byId("schedule-prev").addEventListener("click", onSchedulePrev);
      byId("schedule-next").addEventListener("click", onScheduleNext);
      byId("schedule-publish").addEventListener("click", onSchedulePublish);
      byId("schedule-block-all").addEventListener("click",
        onScheduleBlockAll);
      byId("leads-filter-form").addEventListener("submit", onFiltersSubmit);
      byId("leads-prev").addEventListener("click", onPagerPrev);
      byId("leads-next").addEventListener("click", onPagerNext);
      byId("detail-back").addEventListener("click", onDetailBack);
      byId("detail-status-save").addEventListener("click", onStatusSave);
      byId("detail-note-save").addEventListener("click", onNoteSave);
      byId("detail-note-clear").addEventListener("click", onNoteClear);
    }

    wireEvents();

    return {
      enter: enter,
      reset: resetContent
    };
  }

  /* Pure helpers exported for the Node suite (no DOM required). */
  createMiaPortalPages.helpers = {
    formatTimestamp: formatTimestamp,
    leadBadges: leadBadges,
    statusLabel: statusLabel,
    pagerModel: pagerModel,
    emptyLeadsMessage: emptyLeadsMessage,
    appointmentStatusLabel: appointmentStatusLabel,
    notificationOutcomeLabel: notificationOutcomeLabel,
    formatInTimeZone: formatInTimeZone,
    shiftLocalDay: shiftLocalDay,
    appointmentsRangeLabel: appointmentsRangeLabel,
    scheduleSlotStatusLabel: scheduleSlotStatusLabel,
    appointmentActionsFor: appointmentActionsFor
  };

  /* Export for both the browser (window) and the Node test harness. */
  globalScope.createMiaPortalPages = createMiaPortalPages;

}(typeof window !== "undefined" ? window : this));
