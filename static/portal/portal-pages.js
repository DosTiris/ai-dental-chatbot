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
    value_missing: "Not provided"
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
    var requestIds = { dashboard: 0, leads: 0, detail: 0 };

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
      var pages = ["page-dashboard", "page-leads", "page-lead-detail"];
      for (var i = 0; i < pages.length; i++) {
        byId(pages[i]).hidden = pages[i] !== pageId;
      }
      /* The nav highlights the SECTION the visible page belongs to; the
       * detail page belongs to Leads. */
      var leadsSection = pageId !== "page-dashboard";
      byId("nav-dashboard").classList.toggle("portal-nav-active", !leadsSection);
      byId("nav-leads").classList.toggle("portal-nav-active", leadsSection);
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

    function renderDetail(body) {
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
      byId("detail-body").hidden = false;
    }

    function openLeadDetail(leadId) {
      showPage("page-lead-detail");
      var requestId = ++requestIds.detail;
      setText("detail-state", MESSAGES.loading);
      byId("detail-body").hidden = true;
      data.getLeadDetail(leadId).then(function (outcome) {
        if (requestId !== requestIds.detail) {
          return; /* superseded */
        }
        if (!outcome.ok) {
          handleFailure(outcome, "detail-state");
          return;
        }
        setText("detail-state", "");
        renderDetail(outcome.data);
      });
    }

    function onDetailBack() {
      showPage("page-leads");
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
      leadsQuery = { status: "", q: "", limit: LIST_PAGE_LIMIT, offset: 0 };
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
      byId("leads-filter-form").addEventListener("submit", onFiltersSubmit);
      byId("leads-prev").addEventListener("click", onPagerPrev);
      byId("leads-next").addEventListener("click", onPagerNext);
      byId("detail-back").addEventListener("click", onDetailBack);
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
    emptyLeadsMessage: emptyLeadsMessage
  };

  /* Export for both the browser (window) and the Node test harness. */
  globalScope.createMiaPortalPages = createMiaPortalPages;

}(typeof window !== "undefined" ? window : this));
