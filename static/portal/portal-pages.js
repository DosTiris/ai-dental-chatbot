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
    appointments_tz_note_prefix: "Times shown in the office timezone: "
  };

  /* ------------------------------------------------------------------ */
  /* Pure presentation helpers (exported for the Node suite)             */
  /* ------------------------------------------------------------------ */

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
    var requestIds = { dashboard: 0, leads: 0, detail: 0, appointments: 0 };

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
      currentEnd: null      /* the end_day the backend echoed for this view */
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
        "page-appointments"];
      for (var i = 0; i < pages.length; i++) {
        byId(pages[i]).hidden = pages[i] !== pageId;
      }
      /* The nav highlights the SECTION the visible page belongs to; the
       * detail page belongs to Leads, and Appointments is its own section. */
      var isAppointments = pageId === "page-appointments";
      var isDashboard = pageId === "page-dashboard";
      var isLeads = !isAppointments && !isDashboard;
      byId("nav-dashboard").classList.toggle("portal-nav-active", isDashboard);
      byId("nav-leads").classList.toggle("portal-nav-active", isLeads);
      byId("nav-appointments").classList.toggle("portal-nav-active",
        isAppointments);
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

      item.appendChild(row);
      return item;
    }

    function renderAppointmentsPage(body) {
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
     * Stale-response guard: a superseded response is dropped.
     */
    function loadAppointments() {
      var requestId = ++requestIds.appointments;
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
        if (requestId !== requestIds.appointments) {
          return; /* superseded - a stale page must never render */
        }
        if (!outcome.ok) {
          handleFailure(outcome, "appointments-state");
          return;
        }
        renderAppointmentsPage(outcome.data);
      });
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
      appointments.weekOffset = 0;
      appointments.defaultStart = null;
      appointments.currentStart = null;
      appointments.currentEnd = null;
      byId("appt-prev").disabled = true;
      byId("appt-next").disabled = true;
      showPage("page-appointments");
      loadAppointments();
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
      byId("appt-prev").addEventListener("click", onApptPrev);
      byId("appt-next").addEventListener("click", onApptNext);
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
    appointmentsRangeLabel: appointmentsRangeLabel
  };

  /* Export for both the browser (window) and the Node test harness. */
  globalScope.createMiaPortalPages = createMiaPortalPages;

}(typeof window !== "undefined" ? window : this));
