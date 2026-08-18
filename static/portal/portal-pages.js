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
    /* P6-A notification-settings wording (office-facing, closed set). */
    settings_saved: "Notification settings saved.",
    settings_conflict:
      "Notification settings were updated somewhere else. Showing the latest state.",
    settings_both_empty:
      "Enter at least one destination (email or phone) before saving.",
    settings_invalid:
      "That email or phone number was not accepted. Please check and try again.",
    settings_failed: "The change was not saved. Please try again.",
    settings_email_configured: "Email alerts: configured.",
    settings_email_unconfigured: "Email alerts: not configured.",
    settings_sms_configured: "SMS alerts: configured.",
    settings_sms_unconfigured: "SMS alerts: not configured.",
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
    schedule_slot_gone: "That slot could not be found. Showing the latest schedule.",
    /* Visual Calendar Phase 1 wording (read-only Week view). Deliberately
     * NOT prefixed schedule_* : the Schedule page owns that vocabulary and
     * its audited no-day-shutting rule; these are a separate page. */
    calendar_empty: "Nothing scheduled in this range.",
    calendar_range_mismatch:
      "The portal returned two different date ranges. Nothing is shown; please refresh.",
    calendar_unavailable_module:
      "The calendar view could not be loaded. Please reload the portal.",
    calendar_outside_prefix: "Entries outside this range were not shown: ",
    /* P2-A: the drawer now offers the EXISTING P5-A actions, so the
     * outcome wording is the P5-A wording verbatim - one vocabulary for
     * one capability, whichever page invoked it. Only the "nothing to
     * offer" line is new, because the Appointments page shows an empty
     * control group where the drawer needs a sentence. */
    calendar_drawer_no_actions:
      "No actions are available for this appointment.",
    calendar_drawer_untitled: "Appointment",
    /* PHASE 3A Slice 3: receptionist booking from an Open band.
     * Deliberately NOT prefixed schedule_* (that vocabulary is separately
     * audited) and carrying NO channel claim of any kind: no patient or
     * office notification is sent by a staff booking, so none is promised.
     * booking_uncertain deliberately never claims failure: a response lost
     * in transport can arrive AFTER the booking committed (Rule 16). */
    booking_choose_time: "Choose a time:",
    booking_fields_required: "Patient name and phone are required.",
    booking_success: "Appointment booked.",
    booking_conflict:
      "That time is no longer available. Showing the latest calendar.",
    booking_slot_gone:
      "That time could not be found. Showing the latest calendar.",
    booking_rejected:
      "The portal could not accept those patient details. Please review them and try again.",
    booking_uncertain:
      "The portal did not confirm whether the booking went through. Showing the latest calendar - please check it before trying again.",
    /* PHASE 3A Slice 4D-A: one-off availability from the Calendar. No
     * channel claims of any kind (creating availability notifies no one).
     * avail_rejected stays GENERIC (the booking_rejected convention - raw
     * backend detail never renders through this surface), and
     * avail_uncertain deliberately never claims failure: a response lost
     * in transport can arrive AFTER the availability committed (Rule 16). */
    avail_intro:
      "Open one appointment time. Times use the office timezone.",
    avail_fields_required: "Date and start time are required.",
    avail_created: "Availability added.",
    avail_conflict:
      "That time overlaps existing schedule slots. Showing the latest calendar.",
    avail_rejected:
      "The portal could not accept that availability. Please review the date and time and try again.",
    avail_uncertain:
      "The portal did not confirm whether the availability was added. Showing the latest calendar - please check it before trying again.",
    /* PHASE 3A Slice 4B2: office-internal notes. No channel claims (a note
     * is never sent anywhere) and no patient-visibility ambiguity. */
    note_empty: "No internal notes",
    note_saved: "Note saved.",
    note_rejected:
      "The portal could not accept that note. It must be 2000 characters or fewer.",
    note_gone:
      "That appointment could not be found. Showing the latest calendar.",
    note_uncertain:
      "The portal did not confirm whether the note was saved. Showing the latest calendar - please check it before trying again.",
    /* PHASE 3A Slice 4C: cancelled-appointment recovery + rescheduling.
     * Office-facing, closed set. No channel claim of any kind: restoring
     * or moving an appointment sends NO patient or office notification, so
     * none is promised. The conflict sentence reuses the booking panel's
     * honest shape ("that time is no longer available") because a refused
     * restore/reschedule is the SAME real-world fact. */
    appointment_restored: "Appointment restored.",
    appointment_rescheduled: "Appointment time changed.",
    reschedule_choose_time: "Choose a new time:",
    reschedule_none_available:
      "No open times in the week shown. Open the calendar week you want and try again, or publish availability first.",
    reschedule_time_required: "Choose a new time first.",
    reschedule_conflict:
      "That time is no longer available for this appointment. Showing the latest calendar."
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
   * Purpose (SLICE 4C): the CALENDAR DRAWER's action set for one
   * appointment status. It DELEGATES to the frozen appointmentActionsFor
   * matrix above (which the Appointments page continues to consume
   * unchanged - that surface gains nothing here) and adds only the two
   * Slice 4C capabilities the drawer now offers:
   *   pending / confirmed  -> + "reschedule"  (Change time)
   *   cancelled            -> "restore" + "reschedule"
   *                           (Restore original time / Choose another time)
   * Terminal completed / no_show and unknown statuses still offer nothing
   * (fail safe - never a speculative button). The SERVER lifecycle owner
   * remains authoritative; these stay UI hints mirroring its allow-lists.
   * Pure.
   */
  function calendarDrawerActionsFor(status) {
    var actions = appointmentActionsFor(status).slice();
    if (status === "pending" || status === "confirmed") {
      actions.push("reschedule");
    }
    if (status === "cancelled") {
      actions.push("restore");
      actions.push("reschedule");
    }
    return actions;
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
    /* SLICE 4C.1 (Defect 1): the ONE clock the reschedule picker consults,
     * injectable for deterministic tests and defaulting to the real clock.
     * It returns an EPOCH-MS INSTANT - never a local-clock string - so the
     * comparison against the slots' aware UTC start instants is the same
     * on every device in every timezone (instants are timezone-free; only
     * their RENDERING is local, and rendering plays no part here). */
    var nowProvider = (deps && typeof deps.nowProvider === "function")
      ? deps.nowProvider : Date.now;

    /* Current leads query (closed parameter set; tenant is NEVER part of
     * a query - the backend derives it from the token). */
    var leadsQuery = { status: "", q: "", limit: LIST_PAGE_LIMIT, offset: 0 };

    /* Stale-response guards: one monotonically increasing id per page. */
    var requestIds = { dashboard: 0, leads: 0, detail: 0, appointments: 0,
      schedule: 0, calendar: 0 };

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

    /* Visual Calendar Phase 1 state. Week navigation mirrors the frozen
     * schedule/appointments discipline exactly: weekOffset 0 means the
     * BACKEND default range (both bounds omitted, so the backend anchors
     * "today" in the OFFICE timezone), and any non-zero offset navigates
     * whole seven-day windows relative to the last default start the
     * backend echoed - never a browser-computed today.
     *
     * GUARDS: requestIds.calendar is the supersession counter (only the
     * newest issued load may render) and lifecycle is the page/session
     * token bumped by resetContent and by page re-entry (a load issued
     * before a wipe or re-entry may never render afterwards). There is
     * deliberately NO mutation-generation counter here: Phase 1 is
     * strictly read-only and owns no mutation, so a generation counter
     * would guard nothing. It belongs with the first calendar write
     * action, not before it (Constitution 12/17: no premature scaffolding). */
    var calendar = {
      weekOffset: 0,
      defaultStart: null,
      currentStart: null,
      currentEnd: null,
      /* The office timezone from the AGREED envelopes, kept so the
       * detail panel renders in the same zone the grid was laid out in.
       * It is only ever adopted from a consistency-checked pair. */
      timezoneName: "",
      /* The appointment row the detail panel is showing, or null. It is a
       * reference to an ALREADY loaded row - never separately fetched. */
      selected: null,
      /* P2-A: the appointment_id the panel is showing. The panel is
       * reopened after an authoritative refresh by THIS ID, never by the
       * old object reference - after a refresh the row is a NEW object,
       * and matching on the stale reference would silently fail to
       * reopen (or worse, show pre-mutation values). */
      selectedId: null,
      /* P2-A mutation ownership, mirroring P5-A exactly rather than
       * inventing a second scheme:
       *   generation  - bumped the moment ANY calendar mutation begins.
       *                 A week GET is applied only when the generation it
       *                 captured still equals this one, so a read issued
       *                 BEFORE a mutation can never roll the calendar
       *                 back behind that mutation's authoritative refresh.
       *   actionBusy  - appointment_id -> the UNIQUE token of the mutation
       *                 that owns it. Presence means busy, which both
       *                 suppresses duplicate submits and disables BOTH of
       *                 that appointment's controls. A completing mutation
       *                 releases the entry only when the token is still
       *                 its own, so it can never free a newer owner.
       *   armed       - the two-step cancel guard, scoped per appointment.
       *   actionSeq   - the monotonic source of those tokens.
       * lifecycle (already present) remains the page/session token. */
      generation: 0,
      actionBusy: {},
      armed: {},
      actionSeq: 0,
      /* F1: lifecycle alone cannot distinguish the two very different
       * events that bump it. resetContent (sign-out / independent reset)
       * means "this surface was WIPED - stay wiped and fire nothing".
       * openCalendar (page re-entry) means "the office is looking at this
       * page again" - and a mutation that succeeds afterwards MUST NOT
       * leave that visible page showing pre-mutation state.
       *   wipeEpoch - bumped ONLY by resetContent. A mutation whose captured
       *               epoch is stale was wiped: it renders nothing and
       *               triggers no request, ever.
       *   active    - whether the Calendar page is the one currently shown.
       *               Owned by showPage, so there is exactly one writer.
       *               A re-entry refresh happens only when the office is
       *               actually on the Calendar - never as background work
       *               behind another page. */
      wipeEpoch: 0,
      active: false,
      /* F6: a mutation is settled, but the authoritative refresh it started
       * has not arrived yet. Until it does, the Calendar does NOT know the
       * new truth, so the drawer keeps its controls disabled and refuses new
       * actions. Without this there is a window - the length of the combined
       * GET - in which Confirm/Cancel could be pressed again against a row
       * that is already stale. Cleared the moment authoritative state lands
       * (renderCalendarPage) or that read fails, so it can never stick. */
      settling: 0,
      /* A settled mutation message that must survive the authoritative
       * re-render which immediately follows it. */
      pendingFeedback: "",
      /* PHASE 3A Slice 3 - booking panel state. bookSlots holds REFERENCES
       * to slot rows the week read already returned (never fetched
       * separately, never derived from pixels); bookSelectedId is the ONE
       * server slot id the office chose. Both are UI state scoped to one
       * panel and are wiped with it. */
      bookSlots: [],
      bookSelectedId: null,
      /* v1.0.1 F1 - the BOOKING-WIDE in-flight owner: the unique token of
       * the ONE receptionist booking POST currently unresolved, or null.
       * Per-slot ownership alone let a SECOND booking start for a
       * DIFFERENT slot while the first was still in flight - and two
       * different-slot bookings can both legitimately commit server-side,
       * which no later refresh can undo. So at most ONE staff booking may
       * be in flight from this surface at a time, whatever the slot_id.
       * It follows the F4 principle exactly: page re-entry does not make
       * a real network request cease to exist, so re-entry does NOT clear
       * it; only the promise holding the token may release it, and only
       * resetContent (sign-out / independent reset - the one event
       * entitled to declare the request irrelevant) may wipe it. It is
       * DEDICATED state: Confirm/Cancel actionBusy semantics are
       * untouched. */
      bookBusy: null,
      /* SLICE 4B2 - the note-save single-flight owner: the token of the
       * ONE internal-note PUT currently unresolved, or null. Dedicated
       * state (a note save is neither an appointment action nor a staff
       * booking); released only by the owning promise; wiped only by
       * resetContent. */
      noteBusy: null,
      /* SLICE 4D-A - the one-off availability single-flight owner: the
       * token of the ONE availability POST currently unresolved, or null.
       * Dedicated state (creating inventory is neither a booking nor a
       * note); released only by the owning promise; wiped only by
       * resetContent (the F4 principle - exactly the bookBusy/noteBusy
       * rule). */
      availBusy: null,
      /* SLICE 4C - reschedule picker state. scheduleSlots holds REFERENCES
       * to the slot rows of the LAST consistency-checked week read (the
       * same authoritative response the grid was built from - never
       * fetched separately, never derived from pixels); adopted only by
       * renderCalendarPage on an agreed range, cleared on a range refusal
       * and by resetContent. rescheduleOpen / rescheduleSelectedId /
       * rescheduleMode are UI state scoped to the drawer's slot picker and
       * are wiped with the drawer. The chosen value is always a REAL
       * server slot_id. rescheduleMode (v1.0.1 mode pin F1) records WHICH
       * command the user issued when the picker opened - "active" (Change
       * time) or "cancelled" (Choose another time) - so Save submits that
       * SAME server command; the backend independently enforces the legal
       * starting status under its row lock and refuses a stale command. */
      scheduleSlots: [],
      rescheduleOpen: false,
      rescheduleSelectedId: null,
      rescheduleMode: null,
      lifecycle: 0
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

    /* P6-A notification-settings page state. The page MUTATES (full-
     * replacement PUT), so it mirrors the P3-B2 workflow discipline: token
     * is the last-observed SERVER concurrency token (OPAQUE - stored and
     * echoed verbatim, never parsed, contract C4); saveBusy is the
     * duplicate-submit guard; saveSeq is the monotonic sequence so a
     * superseded load/save response can never overwrite newer UI state; and
     * generation is bumped by resetContent so an in-flight settings GET/PUT
     * resolving after a reset can never repopulate the wiped page. */
    var settings = {
      token: null,
      loaded: false,
      saveBusy: false,
      saveSeq: 0,
      generation: 0
    };

    /* P4-B recurring-schedule page state. token is the OPAQUE server config
     * token (echoed VERBATIM, never parsed - A1); busy is the duplicate-submit
     * guard; generation is bumped by resetContent so an in-flight GET/PUT/
     * preview/apply resolving after a tenant wipe can never repopulate the
     * cleared page (D10); rows maps each weekday to its live input elements. */
    var recurring = {
      token: null, loaded: false, busy: false, generation: 0,
      saveSeq: 0, previewSeq: 0, previewedToken: null, closures: [], rows: {}
    };

    /* Visual Calendar Phase 1: the PURE rendering module (portal-calendar.js).
     * It is OPTIONAL at construction - the null-tolerant convention P4-B
     * established for the recurring panel - so a context that loads only
     * portal-pages.js (every frozen page suite) constructs exactly as
     * before and every other page is bit-for-bit unaffected.
     *
     * The four presentation helpers are INJECTED rather than reimplemented,
     * so the portal keeps ONE time formatter and ONE status vocabulary
     * (Rule 3). The calendar module performs no request of its own; this
     * file remains the only caller of the data owner. */
    var calendarRenderer = null;
    if (typeof globalScope.createMiaPortalCalendar === "function") {
      calendarRenderer = globalScope.createMiaPortalCalendar({
        documentRef: doc,
        formatInTimeZone: formatInTimeZone,
        scheduleSlotStatusLabel: scheduleSlotStatusLabel,
        appointmentStatusLabel: appointmentStatusLabel,
        notificationOutcomeLabel: notificationOutcomeLabel,
        shiftLocalDay: shiftLocalDay,
        /* READ-ONLY selection. The row handed back is the one ALREADY
         * loaded by the week read, so opening details issues no request
         * of any kind and cannot mutate anything. */
        onAppointmentSelect: function (appointment) {
          openCalendarDrawer(appointment);
        },
        /* SLICE 3: an Open band click hands over the ALREADY-LOADED
         * authoritative slot rows. Everything after the click - choose
         * time, the form, the mutation lifecycle - is owned HERE; the
         * renderer stays pure. */
        onOpenBandSelect: function (slots) {
          openCalendarBook(slots);
        }
      });
    }

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
        "page-appointments", "page-schedule", "page-settings", "page-recurring",
        "page-calendar"];
      for (var i = 0; i < pages.length; i++) {
        /* P4-B: null-tolerant so a suite without the page-recurring node
         * (the frozen notification-settings suite) never throws here. */
        var pgEl = byId(pages[i]); if (pgEl) pgEl.hidden = pages[i] !== pageId;
      }
      /* The nav highlights the SECTION the visible page belongs to; the
       * detail page belongs to Leads, and Appointments, Schedule and
       * Settings are their own sections. */
      var isAppointments = pageId === "page-appointments";
      var isSchedule = pageId === "page-schedule";
      var isDashboard = pageId === "page-dashboard";
      var isSettings = pageId === "page-settings";
      var isRecurring = pageId === "page-recurring";
      /* Visual Calendar Phase 1: Calendar is its own section. It MUST be
       * excluded here, otherwise the Leads-default fallback would light up
       * the Leads nav while the Calendar page is visible. */
      var isCalendar = pageId === "page-calendar";
      var isLeads = !isAppointments && !isSchedule && !isDashboard &&
        !isSettings && !isRecurring && !isCalendar;
      byId("nav-dashboard").classList.toggle("portal-nav-active", isDashboard);
      byId("nav-leads").classList.toggle("portal-nav-active", isLeads);
      byId("nav-appointments").classList.toggle("portal-nav-active",
        isAppointments);
      byId("nav-schedule").classList.toggle("portal-nav-active", isSchedule);
      byId("nav-settings").classList.toggle("portal-nav-active", isSettings);
      var navRecurringEl = byId("nav-recurring");
      if (navRecurringEl) navRecurringEl.classList.toggle("portal-nav-active", isRecurring);
      var navCalendarEl = byId("nav-calendar");
      if (navCalendarEl) navCalendarEl.classList.toggle("portal-nav-active", isCalendar);
      /* F1: one writer for "is the Calendar the visible page". Every page
       * switch routes through here, so this can never drift. */
      calendar.active = isCalendar;
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

      /* Status + patient type + urgency + notification outcome badges.
       * SLICE 4A: a staff-created appointment sends no booking
       * notification BY DESIGN (frozen backend contract; its derived
       * outcome is the permanent no-attempt "pending"), so its badge would
       * be noise, not information - it is suppressed for portal_staff
       * ONLY. Every other source keeps its badge exactly as before,
       * including visible failures (Rule 16). Same rationale, same
       * revisit-rule as the drawer projection in portal-calendar.js. */
      var badges = [
        appointmentStatusLabel(appointment.status),
        appointment.new_or_returning || "",
        appointment.urgency || "",
        appointment.source === "portal_staff"
          ? ""
          : notificationOutcomeLabel(appointment.notification_outcome)
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
    /* Visual Calendar Phase 1 - READ-ONLY Week view (time axis)         */
    /* ---------------------------------------------------------------- */

    /* Null-tolerant text setter for the calendar ids (the setRecurringText
     * convention), so a suite whose fake DOM predates these ids never
     * throws inside shared code paths such as resetContent. */
    function setCalendarText(id, text) {
      var el = byId(id);
      if (el) el.textContent = text || "";
    }

    function setCalendarNavDisabled(disabled) {
      var prevEl = byId("calendar-prev");
      if (prevEl) prevEl.disabled = disabled;
      var nextEl = byId("calendar-next");
      if (nextEl) nextEl.disabled = disabled;
    }

    /*
     * Purpose (P2-A): render the action controls for ONE appointment at the
     * bottom of the drawer.
     *
     * The offered set comes from appointmentActionsFor - the SAME frozen
     * status/action matrix the Appointments page uses. No second matrix is
     * defined here, so a status that offers nothing there offers nothing
     * here, and a control can never appear where the lifecycle owner would
     * only refuse it.
     *
     * Both controls share ONE button list, so a mutation in flight disables
     * both (the P5-A C5 rule) - Confirm and Cancel can never overlap on the
     * same appointment.
     */
    function renderCalendarDrawerActions(appointment) {
      var host = byId("calendar-drawer-actions");
      if (!host) {
        return;
      }
      clearChildren(host);
      var appointmentId = appointment.appointment_id;
      /* SLICE 4C: the drawer offers the calendar action set - the frozen
       * P5-A matrix plus restore / reschedule. The Appointments page keeps
       * consuming the frozen matrix unchanged. */
      var actions = calendarDrawerActionsFor(appointment.status);
      if (actions.length === 0) {
        setCalendarText("calendar-drawer-actions-note",
          MESSAGES.calendar_drawer_no_actions);
        return;
      }
      setCalendarText("calendar-drawer-actions-note", "");
      /* Busy means EITHER this appointment has a request in flight, OR the
       * Calendar is still waiting for the authoritative state that a settled
       * mutation asked for (F6). */
      var busy = calendar.actionBusy[appointmentId] !== undefined ||
        calendar.settling > 0;
      var buttons = [];
      if (actions.indexOf("confirm") !== -1) {
        var confirmBtn = doc.createElement("button");
        confirmBtn.type = "button";
        confirmBtn.className = "portal-button portal-button-secondary";
        confirmBtn.textContent = "Confirm";
        confirmBtn.disabled = busy;
        confirmBtn.addEventListener("click", function () {
          onCalendarAppointmentAction(appointmentId, data.confirmAppointment,
            buttons, MESSAGES.appointment_confirmed);
        });
        buttons.push(confirmBtn);
        host.appendChild(confirmBtn);
      }
      if (actions.indexOf("cancel") !== -1) {
        var armed = calendar.armed[appointmentId] === true;
        var cancelBtn = doc.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "portal-button portal-button-secondary";
        /* The armed wording is the P5-A wording: the second click is an
         * explicit, differently-labelled confirmation, never a repeat of
         * the same label. */
        cancelBtn.textContent = armed ? "Confirm cancel" : "Cancel appointment";
        cancelBtn.disabled = busy;
        cancelBtn.addEventListener("click", function () {
          onCalendarCancelClick(appointmentId, cancelBtn, buttons);
        });
        buttons.push(cancelBtn);
        host.appendChild(cancelBtn);
      }
      /* SLICE 4C: Restore original time - a cancelled appointment only.
       * One click performs the action through the SAME mutation discipline
       * Confirm uses (token ownership, generation bump, F6 settle): the
       * BACKEND re-verifies the original slot under its row locks, so a
       * single click here can never double-book - and the P5-A rule that a
       * mutation in flight disables EVERY control in this list holds. */
      if (actions.indexOf("restore") !== -1) {
        var restoreBtn = doc.createElement("button");
        restoreBtn.type = "button";
        restoreBtn.className = "portal-button portal-button-secondary";
        restoreBtn.textContent = "Restore original time";
        restoreBtn.disabled = busy;
        restoreBtn.addEventListener("click", function () {
          onCalendarAppointmentAction(appointmentId, data.restoreAppointment,
            buttons, MESSAGES.appointment_restored,
            MESSAGES.reschedule_conflict);
        });
        buttons.push(restoreBtn);
        host.appendChild(restoreBtn);
      }
      /* SLICE 4C: Change time (active) / Choose another time (cancelled).
       * The click opens the drawer's slot picker - NO request is made here
       * or there: the choices are the REAL open slot rows of the already
       * loaded week read, and the only scheduling value that will ever
       * leave the browser is the chosen server slot_id. */
      if (actions.indexOf("reschedule") !== -1) {
        var reschedBtn = doc.createElement("button");
        reschedBtn.type = "button";
        reschedBtn.className = "portal-button portal-button-secondary";
        reschedBtn.textContent = appointment.status === "cancelled"
          ? "Choose another time" : "Change time";
        reschedBtn.disabled = busy;
        reschedBtn.addEventListener("click", function () {
          /* v1.0.1 (F1): the mode is fixed by the command the user is
           * issuing HERE - the button they see - never re-derived later. */
          openCalendarReschedule(appointmentId,
            appointment.status === "cancelled" ? "cancelled" : "active");
        });
        buttons.push(reschedBtn);
        host.appendChild(reschedBtn);
      }
    }

    /* ---------------------------------------------------------------- */
    /* PHASE 3A Slice 4C: drawer slot picker (Change time / Choose       */
    /* another time). Authoritative real slots only - no second          */
    /* availability engine, no typed datetimes, no pixel-derived times.  */
    /* ---------------------------------------------------------------- */

    /*
     * Purpose: close AND WIPE the picker (the booking-panel rule). The
     * selection and rendered time choices are cleared so nothing stale can
     * be submitted after a drawer close, a re-render, or a reset; the
     * static Save/Cancel controls are re-enabled for the next open.
     * No request, no calendar state beyond the picker's own.
     */
    function closeCalendarReschedule() {
      calendar.rescheduleOpen = false;
      calendar.rescheduleSelectedId = null;
      calendar.rescheduleMode = null;
      var section = byId("calendar-drawer-reschedule");
      if (section) { section.hidden = true; }
      var times = byId("calendar-drawer-reschedule-times");
      if (times) { clearChildren(times); }
      setCalendarText("calendar-drawer-reschedule-note", "");
      setCalendarText("calendar-drawer-reschedule-when", "");
      var save = byId("calendar-drawer-reschedule-save");
      if (save) { save.disabled = false; }
      var cancelBtn = byId("calendar-drawer-reschedule-cancel");
      if (cancelBtn) { cancelBtn.disabled = false; }
    }

    /*
     * Purpose: the picker's choices - the OPEN (available) slot rows of the
     * last consistency-checked week read, earliest first. References into
     * the authoritative response, never copies or derivations; no request
     * is made and no availability rule is applied here (the server already
     * produced these rows, and it re-judges the chosen one under lock at
     * mutation time - a same-day slot that has since started is refused
     * there, exactly as an Open band click already behaves). Pure read.
     */
    function availableRescheduleSlots() {
      var out = [];
      /* SLICE 4C.1 (Defect 1): one instant, read once per render, so every
       * candidate is judged against the SAME now. */
      var nowMs = nowProvider();
      for (var i = 0; i < calendar.scheduleSlots.length; i++) {
        var slot = calendar.scheduleSlots[i];
        if (!slot || slot.status !== "available") { continue; }
        /* SLICE 4C.1 (Defect 1): never OFFER a slot whose start is at or
         * before the current instant - the exact mirror of the backend's
         * "start not strictly in the future -> slot_started" refusal, so
         * a choice the server would certainly reject (the 4:00 PM slot
         * still listed after 4:00 PM office time) is not presented. The
         * comparison is instant-vs-instant: Date.parse of the slot's
         * aware UTC ISO start against the epoch-ms now - no local-clock
         * strings, no device-timezone dependence. An unparsable start
         * fails CLOSED (not offered). The backend's own judgement under
         * its row lock remains the final authority for everything this
         * presentation filter cannot see (clock skew, races, holds). */
        var startMs = Date.parse(slot.start_datetime);
        if (!isFinite(startMs) || startMs <= nowMs) { continue; }
        out.push(slot);
      }
      /* ISO-8601 UTC instants compare lexicographically in time order. */
      out.sort(function (left, right) {
        if (left.start_datetime < right.start_datetime) { return -1; }
        if (left.start_datetime > right.start_datetime) { return 1; }
        return 0;
      });
      return out;
    }

    /* Render one time-choice button per open slot (the booking panel's
     * renderBookTimes pattern): each button is bound to its OWN
     * authoritative slot row, so the selection can never be positional
     * guesswork, and the chosen one carries the selected treatment. */
    function renderRescheduleTimes() {
      var times = byId("calendar-drawer-reschedule-times");
      if (!times) { return; }
      clearChildren(times);
      var choices = availableRescheduleSlots();
      for (var i = 0; i < choices.length; i++) {
        (function (slot) {
          var button = doc.createElement("button");
          button.type = "button";
          button.className = "portal-button portal-button-secondary " +
            "portal-book-time" +
            (calendar.rescheduleSelectedId === slot.slot_id
              ? " portal-book-time-selected" : "");
          /* The portal's ONE formatter, in the OFFICE timezone the agreed
           * envelopes declared - never the device timezone. */
          button.textContent = formatInTimeZone(slot.start_datetime,
            calendar.timezoneName);
          button.addEventListener("click", function () {
            selectRescheduleSlot(slot);
          });
          times.appendChild(button);
        }(choices[i]));
      }
    }

    /* Adopt ONE chosen slot: its server slot_id becomes the ONLY
     * scheduling authority the browser will send, and its full
     * office-local time is echoed so the receptionist confirms a real
     * instant, never a pixel (the booking panel's selectBookSlot rule). */
    function selectRescheduleSlot(slot) {
      calendar.rescheduleSelectedId = slot.slot_id;
      setCalendarText("calendar-drawer-reschedule-when",
        formatInTimeZone(slot.start_datetime, calendar.timezoneName));
      renderRescheduleTimes();
    }

    /*
     * Purpose: open the picker for the appointment the drawer is showing.
     * No request is made: the choices are the loaded week's open slots. An
     * empty week states so plainly (and disables Save) instead of offering
     * an unusable form. Refused while authoritative state is settling or a
     * mutation owns this appointment - the F6 / actionBusy rules the other
     * drawer entry points already follow.
     */
    function openCalendarReschedule(appointmentId, mode) {
      if (calendar.settling > 0) {
        return; /* F6: authoritative state is in flight */
      }
      if (calendar.actionBusy[appointmentId] !== undefined) {
        return; /* a mutation owns this appointment */
      }
      if (mode !== "active" && mode !== "cancelled") {
        return; /* v1.0.1 (F1): no recognizable command, no picker */
      }
      closeCalendarReschedule();
      var section = byId("calendar-drawer-reschedule");
      if (section) { section.hidden = false; }
      calendar.rescheduleOpen = true;
      calendar.rescheduleMode = mode;
      var choices = availableRescheduleSlots();
      if (choices.length === 0) {
        setCalendarText("calendar-drawer-reschedule-note",
          MESSAGES.reschedule_none_available);
        var save = byId("calendar-drawer-reschedule-save");
        if (save) { save.disabled = true; }
        return;
      }
      setCalendarText("calendar-drawer-reschedule-note",
        MESSAGES.reschedule_choose_time);
      renderRescheduleTimes();
    }

    /*
     * Purpose: submit the ONE atomic reschedule for the appointment the
     * drawer is showing, carrying ONLY the chosen real server slot_id -
     * through the EXISTING onCalendarAppointmentAction discipline, so
     * duplicate-submit suppression (actionBusy token ownership), the
     * mutation generation bump, lifecycle/wipe capture, control disabling,
     * and F6 settling are all inherited verbatim rather than re-invented.
     * A cancelled appointment is restored AND moved by the backend in the
     * same transaction (one request - never restore-then-reschedule).
     * External effects: one POST via the data owner; then the existing
     * combined authoritative re-read on settle.
     */
    function onCalendarRescheduleSave() {
      if (calendar.selected === null || calendar.selectedId === null) {
        return; /* no drawer context - nothing to move */
      }
      var appointmentId = calendar.selectedId;
      if (calendar.settling > 0) {
        return; /* F6 */
      }
      if (calendar.actionBusy[appointmentId] !== undefined) {
        return; /* duplicate submit while in flight */
      }
      var slotId = calendar.rescheduleSelectedId;
      if (slotId === null) {
        setCalendarText("calendar-drawer-feedback",
          MESSAGES.reschedule_time_required);
        return;
      }
      var mode = calendar.rescheduleMode;
      if (mode !== "active" && mode !== "cancelled") {
        return; /* v1.0.1 (F1): the picker was wiped - no command to send */
      }
      /* EVERY interactive control of the drawer - the action buttons the
       * builder rendered, the picker's time choices, and the picker's own
       * Save/Cancel - is disabled together for the flight (the P5-A "one
       * button list" rule, extended over the picker). */
      var controls = [];
      var actionsHost = byId("calendar-drawer-actions");
      if (actionsHost) {
        for (var i = 0; i < actionsHost.children.length; i++) {
          controls.push(actionsHost.children[i]);
        }
      }
      var times = byId("calendar-drawer-reschedule-times");
      if (times) {
        for (var t = 0; t < times.children.length; t++) {
          controls.push(times.children[t]);
        }
      }
      var save = byId("calendar-drawer-reschedule-save");
      if (save) { controls.push(save); }
      var cancelBtn = byId("calendar-drawer-reschedule-cancel");
      if (cancelBtn) { controls.push(cancelBtn); }
      /* v1.0.1 (F1): submit the SAME server command the user issued when
       * the picker opened. "Choose another time" (cancelled) and "Change
       * time" (active) are DIFFERENT server-owned routes; the browser
       * never sends a status and the backend re-checks the legal starting
       * status under its own row lock - a stale command is refused there,
       * surfaces as the conflict outcome, and settles with the
       * authoritative re-read. */
      onCalendarAppointmentAction(appointmentId, function (id) {
        return mode === "cancelled"
          ? data.restoreAppointmentToSlot(id, slotId)
          : data.rescheduleAppointment(id, slotId);
      }, controls, MESSAGES.appointment_rescheduled,
        MESSAGES.reschedule_conflict);
    }

    /*
     * Purpose (F4): refresh ONLY the drawer's action controls, from the row
     * ALREADY on screen. No request, no grid re-render, no second action
     * architecture - it re-runs the very builder the drawer already uses.
     * It exists for ONE narrow recovery case: an active, re-entered drawer
     * whose mutation failed in a way that changed nothing on the server, so
     * no authoritative refresh is warranted and nothing else would ever
     * re-enable the controls.
     *
     * It must NEVER run on a path that is about to refresh (F6): rebuilding
     * from the stale row would re-enable Confirm/Cancel before the Calendar
     * knows the authoritative state. Every precondition is therefore checked
     * here as well as at the call site - this is the one function that can
     * un-disable an action control, so it fails closed.
     */
    function refreshCalendarDrawerActions(appointmentId) {
      if (!calendar.active) {
        return; /* never hidden DOM work behind another page */
      }
      if (calendar.settling > 0) {
        return; /* authoritative state is in flight - stay disabled */
      }
      if (calendar.actionBusy[appointmentId] !== undefined) {
        return; /* a NEWER mutation owns this appointment - still busy */
      }
      if (calendar.selected === null ||
          calendar.selectedId !== appointmentId) {
        return; /* the panel no longer represents this appointment */
      }
      renderCalendarDrawerActions(calendar.selected);
    }

    /*
     * Purpose: open (or re-open) the appointment detail panel for one row the
     * week read ALREADY returned. No request is made here: every value comes
     * from the loaded response, so the panel can never be a second data
     * pathway.
     * The panel is one element; CSS alone decides whether it presents as a
     * right-hand drawer (desktop) or a near-full-screen sheet (mobile).
     */
    function openCalendarDrawer(appointment) {
      /* SLICE 3: one surface at a time - opening appointment details
       * closes (and wipes) the booking panel, exactly as opening the
       * booking panel closes the drawer. */
      closeCalendarBook();
      closeCalendarAvail();   /* 4D-A: the same one-surface rule */
      if (!appointment || calendarRenderer === null) {
        return;
      }
      var panel = byId("calendar-drawer");
      if (!panel) {
        return;
      }
      calendar.selected = appointment;
      calendar.selectedId = appointment.appointment_id;
      setCalendarText("calendar-drawer-title",
        appointment.patient_name || MESSAGES.calendar_drawer_untitled);
      setCalendarText("calendar-drawer-status",
        appointmentStatusLabel(appointment.status));

      var fields = byId("calendar-drawer-fields");
      if (fields) {
        clearChildren(fields);
        var rows = calendarRenderer.appointmentDetailFields(
          appointment, calendar.timezoneName, MESSAGES.value_missing);
        for (var i = 0; i < rows.length; i++) {
          var term = doc.createElement("dt");
          term.textContent = rows[i].label;
          var value = doc.createElement("dd");
          /* textContent only: patient-supplied text can never inject markup. */
          value.textContent = rows[i].value;
          fields.appendChild(term);
          fields.appendChild(value);
        }
      }
      renderCalendarDrawerActions(appointment);
      /* SLICE 4C: every drawer open starts with the picker closed - a
       * fresh open always renders fresh authoritative choices on demand,
       * never a leftover selection from another appointment. */
      closeCalendarReschedule();
      /* SLICE 4B2: every drawer open resets the note editor and renders
       * the display state from the row the week read returned. */
      closeCalendarNoteEditor();
      renderCalendarDrawerNote(appointment);
      setCalendarText("calendar-drawer-note-feedback", "");
      /* Keep the originating block visibly selected while the panel is open,
       * so it is obvious which appointment is being read. Appearance only.
       * A cancelled appointment has no block in the resting calendar, so
       * this simply selects nothing - the panel still shows its true state. */
      calendarRenderer.applySelection(appointment);
      panel.hidden = false;
    }

    /* Close and WIPE. The panel holds patient contact details, so closing
     * clears them rather than merely hiding them - nothing may linger on a
     * shared front-desk computer behind a hidden element. Closing also
     * disarms any pending cancel: an arm never survives the panel it was
     * made in, so re-opening always requires the two clicks again. */
    function closeCalendarDrawer() {
      /* SLICE 4C: the picker's selection dies with the drawer it belongs
       * to - a late response can never submit against a closed panel. */
      closeCalendarReschedule();
      /* SLICE 4B2: the note editor may hold TYPED office-private text -
       * close AND wipe it with the drawer (the booking-panel rule), and
       * clear the displayed note + feedback so nothing lingers hidden. */
      closeCalendarNoteEditor();
      setCalendarText("calendar-drawer-note-text", "");
      setCalendarText("calendar-drawer-note-feedback", "");
      calendar.selected = null;
      calendar.selectedId = null;
      calendar.armed = {};
      /* Clearing the selection is part of closing: no block may keep a
       * selected look once the panel it belonged to is gone. Guarded because
       * resetContent also runs in contexts where no renderer was built. */
      if (calendarRenderer !== null) {
        calendarRenderer.applySelection(null);
      }
      var panel = byId("calendar-drawer");
      if (panel) panel.hidden = true;
      setCalendarText("calendar-drawer-title", "");
      setCalendarText("calendar-drawer-status", "");
      setCalendarText("calendar-drawer-actions-note", "");
      setCalendarText("calendar-drawer-feedback", "");
      var actionsHost = byId("calendar-drawer-actions");
      if (actionsHost) clearChildren(actionsHost);
      var fields = byId("calendar-drawer-fields");
      if (fields) clearChildren(fields);
    }

    /* Find one appointment in an authoritative response BY ID. Used to
     * re-open the panel after a refresh: the refreshed row is a new object,
     * so identity must be the id the backend owns, never a stale reference. */
    function findAppointmentById(appointmentsBody, appointmentId) {
      if (appointmentId === null || !appointmentsBody ||
          !Array.isArray(appointmentsBody.appointments)) {
        return null;
      }
      for (var i = 0; i < appointmentsBody.appointments.length; i++) {
        var row = appointmentsBody.appointments[i];
        if (row && row.appointment_id === appointmentId) {
          return row;
        }
      }
      return null;
    }

    /* Every control the note editor owns (Rule 4: named once). */
    var NOTE_CONTROL_IDS = ["calendar-drawer-note-edit",
      "calendar-drawer-note-input", "calendar-drawer-note-save",
      "calendar-drawer-note-cancel"];

    /* ---------------------------------------------------------------- */
    /* PHASE 3A Slice 4B2: office-internal note in the drawer            */
    /* ---------------------------------------------------------------- */

    /* Render the note DISPLAY state from the server-authoritative row.
     * textContent only (never innerHTML): a note that LOOKS like markup
     * stays literal text, and CSS pre-wrap preserves its real line
     * breaks. The section shows for EVERY appointment - any source, any
     * status - because a Mia-created or cancelled appointment may carry
     * a private office note too. */
    function renderCalendarDrawerNote(appointment) {
      var text = byId("calendar-drawer-note-text");
      if (!text) { return; }
      var note = appointment ? appointment.internal_note : null;
      /* Full className assignment (the codebase convention): the state of
       * this element is exactly one of two declared appearances. */
      if (typeof note === "string" && note !== "") {
        text.textContent = note;
        text.className = "portal-drawer-note-text";
      } else {
        text.textContent = MESSAGES.note_empty;
        text.className = "portal-drawer-note-text portal-muted";
      }
    }

    /* Leave edit mode. The typed value is WIPED (an internal note is
     * office-private data - the booking-panel rule) and the display,
     * which was never touched during editing, simply shows again. */
    function closeCalendarNoteEditor() {
      var input = byId("calendar-drawer-note-input");
      if (input) { input.value = ""; input.hidden = true; input.disabled = false; }
      var save = byId("calendar-drawer-note-save");
      if (save) { save.hidden = true; save.disabled = false; }
      var cancel = byId("calendar-drawer-note-cancel");
      if (cancel) { cancel.hidden = true; cancel.disabled = false; }
      var text = byId("calendar-drawer-note-text");
      if (text) { text.hidden = false; }
      var editBtn = byId("calendar-drawer-note-edit");
      if (editBtn) { editBtn.hidden = false; editBtn.disabled = false; }
    }

    /* Enter edit mode, prefilled with the CURRENT server-authoritative
     * value from the cached row (opening the editor issues no request -
     * the drawer rule). Refused while a save is in flight. */
    function openCalendarNoteEditor() {
      if (calendar.noteBusy !== null) {
        return; /* single-flight: the owning save settles first */
      }
      var appointment = calendar.selected;
      if (!appointment || calendar.selectedId === null) { return; }
      var input = byId("calendar-drawer-note-input");
      if (input) {
        input.value = typeof appointment.internal_note === "string"
          ? appointment.internal_note : "";
        input.hidden = false;
      }
      var save = byId("calendar-drawer-note-save");
      if (save) { save.hidden = false; }
      var cancel = byId("calendar-drawer-note-cancel");
      if (cancel) { cancel.hidden = false; }
      var text = byId("calendar-drawer-note-text");
      if (text) { text.hidden = true; }
      var editBtn = byId("calendar-drawer-note-edit");
      if (editBtn) { editBtn.hidden = true; }
      setCalendarText("calendar-drawer-note-feedback", "");
    }

    function setNoteControlsDisabled(disabled) {
      for (var i = 0; i < NOTE_CONTROL_IDS.length; i++) {
        var el = byId(NOTE_CONTROL_IDS[i]);
        if (el) { el.disabled = disabled; }
      }
    }

    /*
     * Purpose (Slice 4B2): submit ONE note save through the EXISTING data
     * owner under the established mutation discipline: a dedicated
     * single-flight owner (calendar.noteBusy - a note save is distinct
     * from Confirm/Cancel and from a staff booking, so it entangles
     * neither actionBusy nor bookBusy), a new mutation generation so any
     * week read issued before now can never roll the cached note back,
     * lifecycle/wipe capture, and F6 settling. Blank input is translated
     * to the contract's EXPLICIT null clear; otherwise the typed text is
     * sent verbatim and the SERVER-normalized value comes back.
     */
    function onCalendarNoteSave() {
      if (calendar.settling > 0) {
        return; /* F6: authoritative state is in flight */
      }
      if (calendar.noteBusy !== null) {
        return; /* single-flight */
      }
      var appointmentId = calendar.selectedId;
      if (appointmentId === null) {
        return;
      }
      var input = byId("calendar-drawer-note-input");
      var raw = String(input ? input.value : "");
      var note = raw.trim() === "" ? null : raw;

      var token = ++calendar.actionSeq;
      calendar.noteBusy = token;
      calendar.generation += 1;   /* a mutation begins */
      var lifecycleAtIssue = calendar.lifecycle;
      var generationAtIssue = calendar.generation;
      var wipeEpochAtIssue = calendar.wipeEpoch;
      setNoteControlsDisabled(true);
      setCalendarText("calendar-drawer-note-feedback", "");
      data.setAppointmentInternalNote(appointmentId, note)
        .then(function (outcome) {
          /* Only the promise that OWNS the token releases it. */
          if (calendar.noteBusy === token) {
            calendar.noteBusy = null;
          }
          settleCalendarNote(lifecycleAtIssue, generationAtIssue,
            wipeEpochAtIssue, outcome, appointmentId);
        });
    }

    /*
     * Purpose (Slice 4B2): THE settling point for a note save - the
     * settleCalendarBooking guard order applied to the note editor.
     *   1. WIPED -> nothing (the wipe stands).
     *   2. Session loss -> wipe and hand back (F9).
     *   3. NOT ON SCREEN (F3) -> nothing now; openCalendar reads
     *      authoritatively on the next visit, so a committed save is
     *      seen then.
     *   4. STALE lifecycle or generation -> a superseded attempt writes
     *      no feedback; when the server MAY have moved (success or ANY
     *      ambiguous outcome - a response can be lost AFTER the save
     *      committed) the visible truth is corrected by ONE authoritative
     *      re-read. A proven no-op is dropped.
     *   5. Current, ok: adopt the RETURNED server-normalized value - the
     *      cached row is patched (so reopening the drawer shows the truth
     *      without a re-read) and, if the drawer still shows THIS
     *      appointment, the editor closes over the fresh display.
     *   6. Current, bad_request (proven no-op): editor stays open with
     *      the typed text for correction and retry; nothing on the
     *      server moved, so nothing else changes.
     *   7. Current, not_found: the appointment vanished or was never this
     *      office's (indistinguishable by design) - honest message, close
     *      the drawer path via the settle refresh.
     *   8. Current, anything else: AMBIGUOUS - the save may have
     *      committed, so failure is never claimed; honest wording plus
     *      the authoritative re-read (Rule 16 / F7).
     */
    function settleCalendarNote(lifecycleAtIssue, generationAtIssue,
        wipeEpochAtIssue, outcome, appointmentId) {
      if (wipeEpochAtIssue !== calendar.wipeEpoch) {
        return;
      }
      if (isSessionLossOutcome(outcome)) {
        resetContent();
        onSessionLost(outcome.state);
        return;
      }
      if (!calendar.active) {
        return; /* F3 */
      }
      if (lifecycleAtIssue !== calendar.lifecycle ||
          generationAtIssue !== calendar.generation) {
        if (mutationMayHaveChangedState(outcome)) {
          startCalendarSettleRefresh();
        }
        return;
      }
      if (outcome.ok) {
        if (calendar.selectedId === appointmentId &&
            calendar.selected !== null) {
          /* The drawer still shows THIS appointment: adopt the RETURNED
           * server-normalized note onto the LIVE row object - the same
           * reference the rendered grid holds - so the display is the
           * committed truth and reopening later re-reads nothing. Even if
           * an interleaved refresh re-rendered and reopened this drawer,
           * calendar.selected is that render's live row, so the newest
           * committed value always wins the display. */
          calendar.selected.internal_note = outcome.data.internal_note;
          closeCalendarNoteEditor();
          renderCalendarDrawerNote(calendar.selected);
          setCalendarText("calendar-drawer-note-feedback",
            MESSAGES.note_saved);
          return;
        }
        /* The office moved on (drawer closed or showing ANOTHER
         * appointment): the committed note now exists only server-side,
         * so the ONE existing correction system re-reads the truth -
         * never a write into someone else's drawer context, and never a
         * second cache or stale-response system. */
        startCalendarSettleRefresh();
        return;
      }
      if (outcome.state === "bad_request") {
        if (calendar.selectedId === appointmentId) {
          setNoteControlsDisabled(false);
          setCalendarText("calendar-drawer-note-feedback",
            MESSAGES.note_rejected);
        }
        return;
      }
      if (outcome.state === "not_found") {
        calendar.pendingFeedback = MESSAGES.note_gone;
        startCalendarSettleRefresh();
        return;
      }
      calendar.pendingFeedback = MESSAGES.note_uncertain;
      startCalendarSettleRefresh();
    }

    /* ---------------------------------------------------------------- */
    /* PHASE 3A Slice 3: receptionist booking from an Open band          */
    /* ---------------------------------------------------------------- */

    /* Every input the booking panel owns. Named once so open/close/disable
     * can never drift apart on which fields exist (Rule 4). */
    var BOOK_INPUT_IDS = ["calendar-book-name", "calendar-book-phone",
      "calendar-book-email", "calendar-book-patient-type",
      "calendar-book-reason",
      "calendar-book-note" /* 4B2: wiped and disabled with the rest */];

    function setBookControlsDisabled(disabled) {
      for (var i = 0; i < BOOK_INPUT_IDS.length; i++) {
        var el = byId(BOOK_INPUT_IDS[i]);
        if (el) { el.disabled = disabled; }
      }
      var submitEl = byId("calendar-book-submit");
      if (submitEl) { submitEl.disabled = disabled; }
      var times = byId("calendar-book-times");
      if (times) {
        for (var t = 0; t < times.children.length; t++) {
          times.children[t].disabled = disabled;
        }
      }
    }

    /*
     * Purpose (Slice 3): close AND WIPE the booking panel. The form carries
     * patient contact details, so - exactly the drawer rule - it is never
     * merely hidden: every typed value, the chosen slot, the rendered time
     * choices and the feedback are all cleared, so nothing can linger on a
     * shared front-desk computer or leak into the next booking.
     */
    function closeCalendarBook() {
      calendar.bookSlots = [];
      calendar.bookSelectedId = null;
      var panel = byId("calendar-book");
      if (panel) { panel.hidden = true; }
      for (var i = 0; i < BOOK_INPUT_IDS.length; i++) {
        var el = byId(BOOK_INPUT_IDS[i]);
        if (el) { el.value = ""; el.disabled = false; }
      }
      var submitEl = byId("calendar-book-submit");
      if (submitEl) { submitEl.disabled = false; }
      var times = byId("calendar-book-times");
      if (times) { clearChildren(times); }
      setCalendarText("calendar-book-times-note", "");
      setCalendarText("calendar-book-when", "");
      setCalendarText("calendar-book-feedback", "");
    }

    /* Render one time-choice button per underlying slot (only when there is
     * a real choice to make). Each button is bound to its OWN authoritative
     * slot row, so the selection can never be positional guesswork. */
    function renderBookTimes() {
      var times = byId("calendar-book-times");
      if (!times) { return; }
      clearChildren(times);
      if (calendar.bookSlots.length < 2) { return; }
      for (var i = 0; i < calendar.bookSlots.length; i++) {
        (function (slot) {
          var button = doc.createElement("button");
          button.type = "button";
          button.className = "portal-button portal-button-secondary " +
            "portal-book-time" +
            (calendar.bookSelectedId === slot.slot_id
              ? " portal-book-time-selected" : "");
          /* The portal's ONE formatter, in the OFFICE timezone the agreed
           * envelopes declared - never the device timezone. */
          button.textContent = formatInTimeZone(slot.start_datetime,
            calendar.timezoneName);
          button.addEventListener("click", function () {
            selectBookSlot(slot);
          });
          times.appendChild(button);
        }(calendar.bookSlots[i]));
      }
    }

    /* Adopt ONE chosen slot: its server slot_id becomes the ONLY scheduling
     * authority the browser will send, and its full office-local time is
     * echoed so the receptionist confirms a real instant, never a pixel. */
    function selectBookSlot(slot) {
      calendar.bookSelectedId = slot.slot_id;
      setCalendarText("calendar-book-when",
        formatInTimeZone(slot.start_datetime, calendar.timezoneName));
      renderBookTimes();
    }

    /*
     * Purpose (Slice 3): open the booking panel for the REAL slots behind a
     * clicked Open band. The rows are references into the week read's
     * authoritative response, so opening this panel issues NO request (the
     * drawer rule). Exactly one slot skips Choose Time; several render one
     * time button each. Pixels never become datetimes: the only scheduling
     * value that will ever leave the browser is a server slot_id.
     */
    function openCalendarBook(slots) {
      if (calendar.settling > 0) {
        return; /* F6: authoritative state is in flight - not even a panel */
      }
      if (calendar.bookBusy !== null) {
        /* v1.0.1 F1: a receptionist booking POST is still unresolved.
         * Opening (and thereby wiping/re-arming) the shared form for a
         * DIFFERENT slot would allow a second booking to start before the
         * first settles - and both could commit. The panel opens again
         * the moment the owning request settles. */
        return;
      }
      if (!Array.isArray(slots) || slots.length === 0) {
        return;
      }
      /* One surface at a time: the drawer and the booking panel are
       * mutually exclusive over the same grid (and 4D-A adds the
       * availability panel to the same exclusion). */
      closeCalendarDrawer();
      closeCalendarBook();
      closeCalendarAvail();
      /* ISO-8601 UTC instants compare lexicographically in time order, so
       * the choices always read earliest-first whatever the band held. */
      var ordered = slots.slice().sort(function (left, right) {
        if (left.start_datetime < right.start_datetime) { return -1; }
        if (left.start_datetime > right.start_datetime) { return 1; }
        return 0;
      });
      calendar.bookSlots = ordered;
      var panel = byId("calendar-book");
      if (panel) { panel.hidden = false; }
      if (ordered.length === 1) {
        selectBookSlot(ordered[0]);
      } else {
        setCalendarText("calendar-book-times-note",
          MESSAGES.booking_choose_time);
        renderBookTimes();
      }
    }

    /*
     * Purpose (Slice 3): submit ONE receptionist booking through the
     * EXISTING data owner, under the SAME mutation discipline the drawer
     * actions use: F4 token ownership (v1.0.1: held in the DEDICATED
     * booking-wide calendar.bookBusy owner - at most ONE staff booking in
     * flight from this surface, whatever the slot_id - minted by the same
     * calendar.actionSeq source), a
     * new mutation generation so any week read issued before now can never
     * roll the calendar back, lifecycle/wipe capture, and F6 settling. The
     * body carries ONLY the patient-entered fields - blank optionals are
     * omitted entirely - and the required-field precheck mirrors the
     * schedule page's day_required convention: a plainly incomplete form
     * never leaves the browser, while everything beyond presence stays the
     * backend's judgment (no booking-policy duplication).
     */
    function onCalendarBookSubmit() {
      if (calendar.settling > 0) {
        return; /* F6: a settled mutation is still awaiting the truth */
      }
      var slotId = calendar.bookSelectedId;
      if (slotId === null) {
        setCalendarText("calendar-book-feedback",
          MESSAGES.booking_choose_time);
        return;
      }
      if (calendar.bookBusy !== null) {
        /* v1.0.1 F1: at most ONE staff booking may be in flight from this
         * surface, whatever the slot_id - a duplicate submit for the SAME
         * slot and a fresh submit for a DIFFERENT slot are refused by the
         * same booking-wide owner. */
        return;
      }
      var nameEl = byId("calendar-book-name");
      var phoneEl = byId("calendar-book-phone");
      var name = String(nameEl ? nameEl.value : "").trim();
      var phone = String(phoneEl ? phoneEl.value : "").trim();
      if (name === "" || phone === "") {
        setCalendarText("calendar-book-feedback",
          MESSAGES.booking_fields_required);
        return;
      }
      var fields = { patient_name: name, patient_phone: phone };
      var emailEl = byId("calendar-book-email");
      var email = String(emailEl ? emailEl.value : "").trim();
      if (email !== "") { fields.patient_email = email; }
      var typeEl = byId("calendar-book-patient-type");
      var patientType = String(typeEl ? typeEl.value : "").trim();
      if (patientType !== "") { fields.new_or_returning = patientType; }
      var reasonEl = byId("calendar-book-reason");
      var reason = String(reasonEl ? reasonEl.value : "").trim();
      if (reason !== "") { fields.reason = reason; }
      /* SLICE 4B2: the optional office-internal note rides the SAME
       * booking request - the frozen 4B1 transaction creates the
       * appointment and its note atomically, so there is never a second
       * note-save call. Blank is OMITTED (the Slice 3 optional-field
       * convention; the 4B1 booking contract reads an absent field as
       * "no note"). */
      var noteEl = byId("calendar-book-note");
      var bookNote = String(noteEl ? noteEl.value : "").trim();
      if (bookNote !== "") { fields.internal_note = bookNote; }

      /* v1.0.1 F1: acquire the booking-wide ownership BEFORE the request
       * is issued. The token still comes from the ONE monotonic actionSeq
       * source (never reset, so tokens can never collide), but it is held
       * in the DEDICATED bookBusy owner - Confirm/Cancel per-appointment
       * ownership is a different capability and keeps its own semantics. */
      var token = ++calendar.actionSeq;
      calendar.bookBusy = token;
      calendar.generation += 1;   /* a mutation begins */
      var lifecycleAtIssue = calendar.lifecycle;
      var generationAtIssue = calendar.generation;
      var wipeEpochAtIssue = calendar.wipeEpoch;
      setBookControlsDisabled(true);
      setCalendarText("calendar-book-feedback", "");
      data.bookScheduleSlot(slotId, fields).then(function (outcome) {
        /* Only the promise that OWNS the booking token may release it: a
         * stale completion after a reset (which wiped bookBusy to null)
         * or after a wholesale wipe-and-reacquire can never free a newer
         * owner (the F4 release rule, applied to the dedicated owner). */
        if (calendar.bookBusy === token) {
          calendar.bookBusy = null;
        }
        settleCalendarBooking(lifecycleAtIssue, generationAtIssue,
          wipeEpochAtIssue, outcome, slotId);
      });
    }

    /*
     * Purpose (Slice 3): THE settling point for a receptionist booking -
     * the settleCalendarMutation guard order applied to the booking panel.
     *   1. WIPED while in flight: render nothing, fire nothing.
     *   2. Session loss (the F9 classifier): wipe and hand back.
     *   3. NOT ON SCREEN (F3): nothing now - openCalendar reads
     *      authoritatively on the next visit, so a committed booking is
     *      seen then. No background work behind another page.
     *   4. STALE lifecycle or generation (F1/F5): a superseded attempt
     *      never writes feedback, but when the server MAY have moved (F7:
     *      success, conflict, disappearance, or ANY ambiguous transport
     *      outcome - a response can be lost AFTER the booking committed)
     *      the visible calendar is corrected with one authoritative
     *      re-read. A proven no-op is simply dropped.
     *   5. Current, PROVEN no-op (bad_request - the strict transport model
     *      or the patient-data owner refused before any transition, the F7
     *      allow-list): nothing on the server is stale, so the form stays
     *      open with its typed values and says so inline. If the panel was
     *      closed meanwhile, there is nothing to correct and nothing to say.
     *   6. Every other current outcome closes the panel, records the honest
     *      message, and re-reads the week authoritatively: success because
     *      the calendar must show the new appointment; conflict / gone /
     *      ambiguity because the truth may have moved - and a lost response
     *      may still mean a committed booking, so failure is NEVER claimed
     *      with certainty (Rule 16).
     */
    function settleCalendarBooking(lifecycleAtIssue, generationAtIssue,
        wipeEpochAtIssue, outcome, slotId) {
      if (wipeEpochAtIssue !== calendar.wipeEpoch) {
        return; /* wiped by reset/sign-out - stay wiped */
      }
      if (isSessionLossOutcome(outcome)) {
        resetContent();
        onSessionLost(outcome.state);
        return;
      }
      if (!calendar.active) {
        return; /* F3: no background work behind another page */
      }
      if (lifecycleAtIssue !== calendar.lifecycle ||
          generationAtIssue !== calendar.generation) {
        if (mutationMayHaveChangedState(outcome)) {
          startCalendarSettleRefresh();
        }
        return;
      }
      if (!outcome.ok && outcome.state === "bad_request") {
        if (calendar.bookSelectedId === slotId) {
          setBookControlsDisabled(false);
          setCalendarText("calendar-book-feedback",
            MESSAGES.booking_rejected);
        }
        return;
      }
      var message;
      if (outcome.ok) {
        message = MESSAGES.booking_success;
      } else if (outcome.state === "conflict") {
        message = MESSAGES.booking_conflict;
      } else if (outcome.state === "not_found") {
        message = MESSAGES.booking_slot_gone;
      } else {
        message = MESSAGES.booking_uncertain;
      }
      closeCalendarBook();
      calendar.pendingFeedback = message;
      startCalendarSettleRefresh();
    }


    /* -----------------------------------------------------------------
     * PHASE 3A SLICE 4D-A - Calendar-native one-off availability.
     *
     * The receptionist affordance for an empty (or any) week: open ONE
     * future time from the Calendar itself, then book it through the
     * EXISTING Slice 3 staff booking panel. The panel follows every
     * established Calendar mutation discipline verbatim: a DEDICATED
     * single-flight owner (calendar.availBusy, minted from the ONE
     * monotonic actionSeq source), a new mutation generation so any week
     * read issued before now can never roll the calendar back, lifecycle /
     * wipe capture, F6 settling, and the F9 session-loss classifier.
     *
     * AUTHORITY BOUNDARIES (the four 4D-A GO constraints):
     *   1. Eligibility is SERVER-established only. The browser sends the
     *      typed office-local date, "HH:MM" start, and a duration from the
     *      fixed select - never a clock, never a tenant, never a raw
     *      datetime instant. Required-field presence is the ONLY precheck
     *      here (the booking panel's day_required convention); validity,
     *      DST, strictly-future, overlap, and tenancy are all backend
     *      judgments.
     *   2/3. Tenancy and time normalization live entirely behind the
     *      verified credential on the server; this surface cannot express
     *      either.
     *   4. NOTHING is synthesized into frontend state on success: the
     *      panel closes and startCalendarSettleRefresh() re-reads the
     *      authoritative Schedule + Appointments pair - the new Open slot
     *      renders ONLY from that response, and booking it then sends the
     *      real server slot_id through the unchanged booking flow.
     * ----------------------------------------------------------------- */

    var AVAIL_INPUT_IDS = ["calendar-avail-day", "calendar-avail-start",
      "calendar-avail-duration"];
    /* The prefilled slot length (mirrors the backend's named
     * PORTAL_DEFAULT_SLOT_MINUTES - Rule 4: one reviewable value here). */
    var AVAIL_DEFAULT_DURATION = "30";

    function setAvailControlsDisabled(disabled) {
      for (var i = 0; i < AVAIL_INPUT_IDS.length; i++) {
        var el = byId(AVAIL_INPUT_IDS[i]);
        if (el) { el.disabled = disabled; }
      }
      var submitEl = byId("calendar-avail-submit");
      if (submitEl) { submitEl.disabled = disabled; }
    }

    /*
     * Purpose (4D-A): close AND WIPE the availability panel (the drawer /
     * booking-panel rule): every typed value, the note, and the feedback
     * are cleared and the controls re-enabled, so the panel always opens
     * fresh and nothing lingers on a shared front-desk computer.
     */
    function closeCalendarAvail() {
      var panel = byId("calendar-avail");
      if (panel) { panel.hidden = true; }
      var dayEl = byId("calendar-avail-day");
      if (dayEl) { dayEl.value = ""; dayEl.disabled = false; }
      var startEl = byId("calendar-avail-start");
      if (startEl) { startEl.value = ""; startEl.disabled = false; }
      var durationEl = byId("calendar-avail-duration");
      if (durationEl) {
        durationEl.value = AVAIL_DEFAULT_DURATION;
        durationEl.disabled = false;
      }
      var submitEl = byId("calendar-avail-submit");
      if (submitEl) { submitEl.disabled = false; }
      setCalendarText("calendar-avail-note", "");
      setCalendarText("calendar-avail-feedback", "");
    }

    /*
     * Purpose (4D-A): open the availability panel fresh. Guards mirror
     * openCalendarBook: not while a settled mutation awaits authoritative
     * state (F6), and not while an availability POST is unresolved (the
     * single-flight owner). One surface at a time over the same grid: the
     * drawer and the booking panel close first. The date prefills to the
     * FIRST DAY OF THE DISPLAYED WEEK from the backend-derived anchor
     * (calendar.defaultStart shifted by the week offset - never the device
     * date), with the office-local today as a browser-assistance min; both
     * are convenience only - the strictly-future rule is server-enforced.
     */
    function openCalendarAvail() {
      if (calendar.settling > 0) {
        return; /* F6: authoritative state is in flight - not even a panel */
      }
      if (calendar.availBusy !== null) {
        return; /* an availability POST is still unresolved */
      }
      closeCalendarDrawer();
      closeCalendarBook();
      closeCalendarAvail();   /* always opens FRESH (the wipe-on-open rule) */
      var panel = byId("calendar-avail");
      if (panel) { panel.hidden = false; }
      setCalendarText("calendar-avail-note", MESSAGES.avail_intro);
      var dayEl = byId("calendar-avail-day");
      if (dayEl && calendar.defaultStart !== null) {
        var weekStart = calendar.weekOffset === 0
          ? calendar.defaultStart
          : shiftLocalDay(calendar.defaultStart, calendar.weekOffset * 7);
        if (weekStart !== "") {
          dayEl.value = weekStart;
          /* min is ASSISTANCE only (constraint 1): the office-local today
           * the backend default week declared. Server rules decide. */
          dayEl.min = calendar.defaultStart;
        }
      }
    }

    /*
     * Purpose (4D-A): submit ONE one-off availability POST through the
     * EXISTING data owner, under the SAME mutation discipline the booking
     * panel uses. The body carries ONLY the three typed fields; the
     * required-presence precheck mirrors booking_fields_required (a
     * plainly incomplete form never leaves the browser) while every rule
     * beyond presence stays the backend's judgment - no availability-policy
     * duplication in the browser.
     */
    function onCalendarAvailSubmit() {
      if (calendar.settling > 0) {
        return; /* F6: a settled mutation is still awaiting the truth */
      }
      if (calendar.availBusy !== null) {
        return; /* at most ONE availability POST in flight */
      }
      var dayEl = byId("calendar-avail-day");
      var startEl = byId("calendar-avail-start");
      var durationEl = byId("calendar-avail-duration");
      var day = String(dayEl ? dayEl.value : "").trim();
      var start = String(startEl ? startEl.value : "").trim();
      if (day === "" || start === "") {
        setCalendarText("calendar-avail-feedback",
          MESSAGES.avail_fields_required);
        return;
      }
      var duration = parseInt(
        String(durationEl ? durationEl.value : "").trim(), 10);
      if (!isFinite(duration)) {
        /* A broken select is refused loudly, never silently defaulted
         * (Rule 4: no hidden fallback values). */
        setCalendarText("calendar-avail-feedback",
          MESSAGES.avail_fields_required);
        return;
      }
      var token = ++calendar.actionSeq;
      calendar.availBusy = token;
      calendar.generation += 1;   /* a mutation begins */
      var lifecycleAtIssue = calendar.lifecycle;
      var generationAtIssue = calendar.generation;
      var wipeEpochAtIssue = calendar.wipeEpoch;
      setAvailControlsDisabled(true);
      setCalendarText("calendar-avail-feedback", "");
      data.createOneOffAvailability(day, start, duration)
        .then(function (outcome) {
          /* Only the promise that OWNS the token may release it (the F4
           * release rule, applied to the dedicated owner). */
          if (calendar.availBusy === token) {
            calendar.availBusy = null;
          }
          settleCalendarAvail(lifecycleAtIssue, generationAtIssue,
            wipeEpochAtIssue, outcome);
        });
    }

    /*
     * Purpose (4D-A): THE settling point for a one-off availability POST -
     * the settleCalendarBooking guard order applied to this panel.
     *   1. WIPED while in flight: render nothing, fire nothing.
     *   2. Session loss (the F9 classifier): wipe and hand back.
     *   3. NOT ON SCREEN (F3): nothing now - openCalendar reads
     *      authoritatively on the next visit.
     *   4. STALE lifecycle or generation: a superseded attempt never
     *      writes feedback, but when the server MAY have moved (F7) the
     *      visible calendar is corrected with one authoritative re-read.
     *   5. Current, PROVEN no-op (bad_request - the strict transport model
     *      or the service prevalidation refused before any insert): the
     *      form stays open with its typed values and says so GENERICALLY
     *      (raw backend detail never renders). If the panel was closed
     *      meanwhile, there is nothing to correct and nothing to say.
     *   6. Every other current outcome closes the panel, records the
     *      honest message, and re-reads the week authoritatively: success
     *      because the new Open slot must arrive through the REAL data
     *      source (constraint 4 - never synthesized); conflict / ambiguity
     *      because the truth may have moved - and a lost response may
     *      still mean a committed insert, so failure is NEVER claimed with
     *      certainty (Rule 16).
     */
    function settleCalendarAvail(lifecycleAtIssue, generationAtIssue,
        wipeEpochAtIssue, outcome) {
      if (wipeEpochAtIssue !== calendar.wipeEpoch) {
        return; /* wiped by reset/sign-out - stay wiped */
      }
      if (isSessionLossOutcome(outcome)) {
        resetContent();
        onSessionLost(outcome.state);
        return;
      }
      if (!calendar.active) {
        return; /* F3: no background work behind another page */
      }
      if (lifecycleAtIssue !== calendar.lifecycle ||
          generationAtIssue !== calendar.generation) {
        if (mutationMayHaveChangedState(outcome)) {
          startCalendarSettleRefresh();
        }
        return;
      }
      if (!outcome.ok && outcome.state === "bad_request") {
        var panel = byId("calendar-avail");
        if (panel && !panel.hidden) {
          setAvailControlsDisabled(false);
          setCalendarText("calendar-avail-feedback",
            MESSAGES.avail_rejected);
        }
        return;
      }
      var message;
      if (outcome.ok) {
        message = MESSAGES.avail_created;
      } else if (outcome.state === "conflict") {
        message = MESSAGES.avail_conflict;
      } else {
        message = MESSAGES.avail_uncertain;
      }
      closeCalendarAvail();
      calendar.pendingFeedback = message;
      startCalendarSettleRefresh();
    }

    /*
     * Purpose (P2-A): the FIRST click of Cancel arms THIS appointment (no
     * network, no window.confirm); the SECOND explicit click performs it.
     * This is the P5-A guard reused verbatim - the Calendar must never offer
     * a weaker cancellation path than the Appointments page. A mutation
     * already in flight for the same appointment suppresses both.
     */
    function onCalendarCancelClick(appointmentId, cancelButton, buttons) {
      if (calendar.settling > 0) {
        return; /* F6: authoritative state is in flight - not even an arm */
      }
      if (calendar.actionBusy[appointmentId] !== undefined) {
        return; /* a mutation is in flight for this appointment */
      }
      if (calendar.armed[appointmentId] !== true) {
        calendar.armed[appointmentId] = true;
        cancelButton.textContent = "Confirm cancel";
        setCalendarText("calendar-drawer-feedback",
          MESSAGES.appointment_cancel_arm);
        return;
      }
      onCalendarAppointmentAction(appointmentId, data.cancelAppointment,
        buttons, MESSAGES.appointment_cancelled);
    }

    /*
     * Purpose (P2-A): perform ONE appointment mutation from the drawer and
     * settle it authoritatively, through the EXISTING P5-A data methods.
     * Guards: a duplicate submit while this appointment is in flight is
     * suppressed; BOTH drawer controls are disabled for the duration; a new
     * mutation generation is opened so any week read issued before now is
     * discarded. Optimistic state is NEVER rendered - only the authoritative
     * combined re-read the settler triggers.
     * External effects: one POST via the data layer; then the existing
     * combined Schedule + Appointments GET on settle.
     */
    function onCalendarAppointmentAction(appointmentId, call, buttons,
        successMessage, conflictMessage) {
      if (calendar.settling > 0) {
        /* F6: a settled mutation is still waiting for authoritative state.
         * Acting now would act against a row the Calendar already knows is
         * out of date. */
        return;
      }
      if (calendar.actionBusy[appointmentId] !== undefined) {
        return; /* duplicate submit while in flight */
      }
      /* This mutation OWNS the appointment via a unique token, not an
       * unowned boolean, so its late completion can only release its own
       * ownership and never a newer mutation's. */
      var token = ++calendar.actionSeq;
      calendar.actionBusy[appointmentId] = token;
      calendar.generation += 1;   /* a mutation begins */
      var lifecycleAtIssue = calendar.lifecycle;
      var generationAtIssue = calendar.generation;
      var wipeEpochAtIssue = calendar.wipeEpoch;
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].disabled = true;
      }
      setCalendarText("calendar-drawer-feedback", "");
      call(appointmentId).then(function (outcome) {
        /* The request itself is over, so its ownership is released here -
         * but ONLY the settler decides whether the controls may be rebuilt
         * (F6). Rebuilding here would re-enable them from the stale row
         * while the authoritative refresh is still in flight. */
        if (calendar.actionBusy[appointmentId] === token) {
          delete calendar.actionBusy[appointmentId];
        }
        settleCalendarMutation(lifecycleAtIssue, generationAtIssue,
          wipeEpochAtIssue, outcome, successMessage, appointmentId,
          conflictMessage);
      });
    }

    /*
     * Purpose (P2-A): THE single settling point for a calendar appointment
     * mutation response (the P5-A settleAppointmentMutation discipline).
     * Guard order:
     *   1. WIPED (sign-out or an independent reset happened while in
     *      flight): render NOTHING and trigger NO GET - the wipe stands and
     *      no request may repopulate a signed-out screen.
     *   2. Session-loss outcome: wipe rendered tenant content and hand back
     *      to the sign-in flow.
     *   3. NOT ON SCREEN (F3): the office navigated to another page and has
     *      not come back. Rendering or fetching here would be invisible
     *      background work behind the page they are actually using, and it
     *      is never needed - openCalendar performs a fresh authoritative
     *      read on the next visit, so nothing can be stale by the time it
     *      is seen again. Session loss is deliberately handled ABOVE this
     *      gate: a lost session must wipe and hand back whichever page is
     *      visible.
     *   4. STALE LIFECYCLE without a wipe - i.e. PAGE RE-ENTRY (F1). The
     *      re-entry GET may well have been answered BEFORE this mutation
     *      committed, so the visible calendar can be showing pre-mutation
     *      state. Returning silently here would leave it stale until a
     *      manual refresh. A SUCCESSFUL commit therefore triggers the
     *      smallest safe correction: one authoritative re-read of the
     *      CURRENT window through the existing combined path (the on-screen
     *      gate above already applies). No message is shown and no drawer is
     *      reopened: that mutation's UI context is gone, and only the stale
     *      pixels are being corrected. A FAILED older attempt changed
     *      nothing and is simply dropped.
     *   5. STALE GENERATION (a newer mutation already owns the surface): a
     *      SUCCESSFUL older commit still changed the server, so fetch the
     *      current truth; a failed older attempt is simply dropped.
     *   6. Current: record the honest message and ALWAYS re-read the week
     *      authoritatively - on success AND on every failure - so a
     *      conflict, a not-found or an unavailable settles to real state.
     * The message is held in pendingFeedback because the refresh rebuilds
     * (and briefly closes) the panel; the settler never writes the final
     * visual state itself.
     */
    /*
     * Purpose (F9): is this outcome a lost session? THE single classifier -
     * every calendar path that must distinguish "the credential is dead"
     * from "this request merely failed" asks this one function, so the two
     * values can never drift apart between the read path and the mutation
     * path. Pure.
     */
    function isSessionLossOutcome(outcome) {
      return !outcome.ok && (outcome.state === "signed_out" ||
        outcome.state === "unauthorized");
    }

    /* F7: the outcomes that PROVE a mutation never took effect. This is an
     * allow-list, not a deny-list, because the safe default for a POST is
     * "the server may have moved".
     *
     * From the data owner's closed vocabulary, only bad_request (400/422)
     * qualifies: the request was rejected before any lifecycle transition
     * could occur. signed_out and unauthorized are also no-ops, but they
     * never reach this predicate - the wipe path handles them first.
     *
     * Everything else is at best UNCERTAIN and must fail safe toward
     * reading the truth:
     *   ok               - it committed.
     *   conflict         - a 409 reports the appointment changed SOMEWHERE
     *                      ELSE, so the Calendar is likely the stale party.
     *   not_found        - it is gone from the server's point of view.
     *   unavailable      - a network failure or 5xx can happen AFTER the
     *                      server committed and merely lose the response.
     *   invalid_response - a 200 whose body failed validation still means
     *                      the transition very likely occurred.
     * A future vocabulary value falls through to "may have changed", which
     * costs one extra read and can never leave the office looking at a
     * stale appointment. */
    var MUTATION_PROVEN_NO_EFFECT = ["bad_request"];

    /*
     * Purpose (F5/F7): may authoritative state have changed on the server?
     * Returns false ONLY for an outcome that proves the mutation never
     * applied; every other result - success, conflict, disappearance, and
     * every ambiguous transport failure - returns true.
     */
    function mutationMayHaveChangedState(outcome) {
      if (outcome.ok) {
        return true;
      }
      return MUTATION_PROVEN_NO_EFFECT.indexOf(outcome.state) === -1;
    }

    /* Begin the authoritative refresh a settled mutation requires. The
     * settling marker is raised BEFORE the read is issued, so the drawer is
     * already frozen by the time control returns to the office (F6). */
    function startCalendarSettleRefresh() {
      calendar.settling += 1;
      loadCalendar();
    }

    function settleCalendarMutation(lifecycleAtIssue, generationAtIssue,
        wipeEpochAtIssue, outcome, successMessage, appointmentId,
        conflictMessage) {
      if (wipeEpochAtIssue !== calendar.wipeEpoch) {
        return; /* wiped by reset/sign-out - stay wiped, fire nothing */
      }
      if (isSessionLossOutcome(outcome)) {
        resetContent();
        onSessionLost(outcome.state);
        return;
      }
      if (!calendar.active) {
        /* F3: the Calendar is not the page on screen. Do nothing at all -
         * no read, no render, no feedback into a panel nobody is looking
         * at. The next openCalendar reads authoritatively anyway. */
        return;
      }
      if (lifecycleAtIssue !== calendar.lifecycle) {
        /* Page re-entry, not a wipe (F1). The re-entry read may well have
         * been answered before this mutation resolved, so if the server
         * state may have moved (F5: committed, conflicted, or vanished) the
         * visible page must be corrected. No message is written: an older
         * mutation never overwrites a newer one's feedback. */
        if (mutationMayHaveChangedState(outcome)) {
          startCalendarSettleRefresh();
        } else {
          refreshCalendarDrawerActions(appointmentId);
        }
        return;
      }
      if (generationAtIssue !== calendar.generation) {
        /* A newer mutation owns the surface now - so again, no feedback
         * from this one. It still fetches when the server may have moved. */
        if (mutationMayHaveChangedState(outcome)) {
          startCalendarSettleRefresh();
        } else {
          refreshCalendarDrawerActions(appointmentId);
        }
        return;
      }
      if (!outcome.ok) {
        var message;
        if (outcome.state === "conflict") {
          /* SLICE 4C: an action may declare its own honest conflict
           * sentence (a refused reschedule is "that time is taken", not
           * "the appointment changed"); absent one, the frozen P5-A
           * wording applies unchanged. */
          message = conflictMessage || MESSAGES.appointment_action_conflict;
        } else if (outcome.state === "not_found") {
          message = MESSAGES.appointment_gone;
        } else {
          message = MESSAGES[outcome.state] || MESSAGES.appointment_action_failed;
        }
        /* Never success wording on a failure, and never an optimistic edit
         * to the visible appointment. */
        calendar.pendingFeedback = message;
        startCalendarSettleRefresh();
        return;
      }
      calendar.pendingFeedback = successMessage;
      /* Authoritative state only - never optimistic. The drawer stays
       * frozen until this read lands and rebuilds it from the NEW row. */
      startCalendarSettleRefresh();
    }

    /*
     * Purpose: render ONE week from the two authoritative responses.
     * FAIL CLOSED (the response consistency guard): the pure module refuses
     * whenever the Schedule and Appointments envelopes disagree on
     * start_day / end_day / timezone_name, or the range is unusable. On a
     * refusal NOTHING is rendered - no grid, no range label, no timezone
     * note - and the office is told plainly. Two authoritative ranges are
     * never blended (Constitution 14: failure must be visible).
     */
    function renderCalendarPage(scheduleBody, appointmentsBody) {
      var gridEl = byId("calendar-grid");
      if (gridEl) clearChildren(gridEl);
      /* A re-render replaces every block element, so any open drawer would
       * be pointing at a detached row: close it first. The id is captured
       * BEFORE the close (which clears it) so the panel can be reopened
       * from the REFRESHED row below. */
      /* Authoritative state has arrived: whatever a settled mutation was
       * waiting for is now known, so the drawer may be rebuilt live again. */
      calendar.settling = 0;
      var reopenId = calendar.selectedId;
      closeCalendarDrawer();
      /* SLICE 3: a re-render replaces the week's rows, so any open booking
       * panel would hold references to detached inventory. Close and wipe
       * it; a settled booking's message survives through pendingFeedback. */
      closeCalendarBook();

      var result = calendarRenderer.buildGrid(scheduleBody, appointmentsBody);
      if (!result.ok) {
        calendar.currentStart = null;
        calendar.currentEnd = null;
        calendar.timezoneName = "";
        /* SLICE 4C: a refused pair is never a picker source either. */
        calendar.scheduleSlots = [];
        setCalendarText("calendar-range-label", "");
        setCalendarText("calendar-timezone-note", "");
        setCalendarText("calendar-state", MESSAGES.calendar_range_mismatch);
        setCalendarNavDisabled(true);
        return;
      }

      /* Only an AGREED range is ever adopted as navigation state. */
      calendar.currentStart = scheduleBody.start_day;
      calendar.currentEnd = scheduleBody.end_day;
      calendar.timezoneName = scheduleBody.timezone_name;
      /* SLICE 4C: the picker's choices are exactly this agreed week read's
       * slot rows - the same authoritative inventory the grid painted. */
      calendar.scheduleSlots = Array.isArray(scheduleBody.slots)
        ? scheduleBody.slots : [];
      if (calendar.weekOffset === 0) {
        calendar.defaultStart = scheduleBody.start_day;
      }
      /* The office timezone is stated ONCE, at calendar level - never
       * repeated inside every block (that repetition is exactly what made
       * the first resting view unreadable). */
      setCalendarText("calendar-timezone-note",
        MESSAGES.appointments_tz_note_prefix + scheduleBody.timezone_name);
      setCalendarText("calendar-range-label",
        appointmentsRangeLabel(scheduleBody.start_day, scheduleBody.end_day));
      if (gridEl) gridEl.appendChild(result.element);

      if (result.outsideCount > 0) {
        /* The backend should not return rows outside the window it echoed.
         * If it ever does, say so rather than dropping them silently. */
        setCalendarText("calendar-state",
          MESSAGES.calendar_outside_prefix + result.outsideCount);
      } else if (result.visibleCount === 0) {
        setCalendarText("calendar-state", MESSAGES.calendar_empty);
      } else {
        setCalendarText("calendar-state", "");
      }
      setCalendarNavDisabled(calendar.defaultStart === null);

      /* P2-A: reopen the panel from the AUTHORITATIVE refreshed row, found
       * by appointment_id. If the backend no longer returns it - which is
       * expected after a cancellation once it leaves the window - the
       * panel stays closed rather than continuing to display a row the
       * server did not send. No history is fabricated. */
      var refreshed = findAppointmentById(appointmentsBody, reopenId);
      if (refreshed !== null) {
        openCalendarDrawer(refreshed);
      }
      /* A settled mutation message survives its own refresh. It is shown
       * inside the panel when the panel is open, and at calendar level
       * when the appointment is gone - so the outcome is never lost. */
      if (calendar.pendingFeedback !== "") {
        if (refreshed !== null) {
          setCalendarText("calendar-drawer-feedback",
            calendar.pendingFeedback);
        } else {
          setCalendarText("calendar-state", calendar.pendingFeedback);
        }
        calendar.pendingFeedback = "";
      }
    }

    /*
     * Purpose: load one calendar week through the EXISTING data owner -
     * exactly the two frozen read methods, with the same closed parameter
     * vocabulary the Schedule and Appointments pages already send. No new
     * endpoint, no new request pathway.
     * Guards: the response is applied only when its request id is still the
     * newest AND the lifecycle captured at issue still holds (a load issued
     * before a sign-out wipe or a page re-entry may never render).
     * Both responses must succeed; the first failure is reported through the
     * shared handleFailure rule, so session loss wipes and hands back.
     */
    function loadCalendar() {
      if (calendarRenderer === null) {
        return;
      }
      var requestId = ++requestIds.calendar;
      var lifecycleAtIssue = calendar.lifecycle;
      /* P2-A: the week read is also generation-guarded now that the page
       * mutates. A read issued BEFORE a mutation began can never be
       * applied afterwards, so a stale pre-mutation GET cannot overwrite
       * post-mutation authoritative state. */
      var generationAtIssue = calendar.generation;
      setCalendarText("calendar-state", MESSAGES.loading);

      var params = {};
      if (calendar.weekOffset !== 0) {
        if (calendar.defaultStart === null) {
          calendar.weekOffset = 0;
        } else {
          var start = shiftLocalDay(calendar.defaultStart,
            calendar.weekOffset * 7);
          var end = shiftLocalDay(start, 6);
          if (start !== "" && end !== "") {
            params = { start_day: start, end_day: end };
          } else {
            calendar.weekOffset = 0;
          }
        }
      }

      Promise.all([
        data.getSchedule(params),
        data.getAppointments(params)
      ]).then(function (outcomes) {
        if (requestId !== requestIds.calendar ||
            lifecycleAtIssue !== calendar.lifecycle ||
            generationAtIssue !== calendar.generation) {
          return; /* superseded, wiped/re-entered, or pre-mutation */
        }
        var scheduleOutcome = outcomes[0];
        var appointmentsOutcome = outcomes[1];
        if (!scheduleOutcome.ok || !appointmentsOutcome.ok) {
          /* F8: the read FAILED, so authoritative state never arrived. The
           * settling marker is deliberately LEFT SET. After a mutation whose
           * effect is committed or uncertain, the last row the Calendar
           * holds is no longer safe to act on - and clearing the marker here
           * would let the office re-enable Confirm/Cancel simply by closing
           * and reopening the panel, acting against data the Calendar itself
           * knows is unresolved.
           * Nothing is fabricated: the honest read-failure message is shown,
           * and recovery is a NEW authoritative read - manual Refresh, week
           * navigation, or ordinary page re-entry - each of which clears the
           * marker only by actually landing new truth. A sign-out or reset
           * still wipes normally through handleFailure. */
          /* F9: the Calendar reads TWO windows concurrently, so a failure
           * selector that always preferred the Schedule half could hide a
           * dead session behind an ordinary transport failure on the other
           * half - leaving patient and appointment data on screen after the
           * credential had already been rejected.
           * Session loss therefore DOMINATES every ordinary failure,
           * whichever half reports it, and the order of the two is
           * irrelevant. Exactly ONE outcome reaches handleFailure, so the
           * wipe and the hand-back happen exactly once. Neither response is
           * partially rendered on any failure path. */
          var failure;
          if (isSessionLossOutcome(scheduleOutcome)) {
            failure = scheduleOutcome;
          } else if (isSessionLossOutcome(appointmentsOutcome)) {
            failure = appointmentsOutcome;
          } else {
            failure = scheduleOutcome.ok ? appointmentsOutcome
              : scheduleOutcome;
          }
          handleFailure(failure, "calendar-state");
          return;
        }
        renderCalendarPage(scheduleOutcome.data, appointmentsOutcome.data);
      });
    }

    /* Changing week closes the panel FIRST, so a mutation still in flight
     * cannot reopen a drawer for an appointment belonging to the week the
     * office just left, and no arm survives the move. */
    function onCalendarPrev() {
      if (calendar.defaultStart === null) {
        return;
      }
      closeCalendarDrawer();
      closeCalendarBook();   /* Slice 3: no panel survives leaving the week */
      closeCalendarAvail();  /* 4D-A: its date default belongs to this week */
      calendar.weekOffset -= 1;
      loadCalendar();
    }

    function onCalendarNext() {
      if (calendar.defaultStart === null) {
        return;
      }
      closeCalendarDrawer();
      closeCalendarBook();   /* Slice 3: no panel survives leaving the week */
      closeCalendarAvail();  /* 4D-A: its date default belongs to this week */
      calendar.weekOffset += 1;
      loadCalendar();
    }

    /* The visible manual Refresh control: re-read the CURRENT week from the
     * server. There is no polling and no timer in Phase 1 - a refresh only
     * ever happens because the office asked for one or re-entered the page. */
    function onCalendarRefresh() {
      loadCalendar();
    }

    /* Re-entry is a page reset (the frozen openSchedule discipline): bump the
     * lifecycle so any load still in flight from the previous visit can no
     * longer render, drop the stale anchor, and reload from the backend
     * default week. */
    function openCalendar() {
      calendar.lifecycle += 1;   /* page re-entry: a mutation still in
                                  * flight may no longer own the surface */
      /* F4: actionBusy is deliberately NOT cleared here. A page visit does
       * not stop an outstanding network request from existing, and clearing
       * the lock would re-enable Confirm/Cancel for an appointment whose
       * mutation is still in flight, allowing a second submit for the SAME
       * appointment. Ownership is released by the completing mutation
       * itself, or wiped wholesale by resetContent (sign-out / reset) -
       * the only event entitled to declare the request irrelevant.
       * The arm is UI state scoped to one panel visit, so it DOES clear. */
      calendar.armed = {};
      calendar.pendingFeedback = "";
      calendar.settling = 0;
      calendar.weekOffset = 0;
      calendar.defaultStart = null;
      calendar.currentStart = null;
      calendar.currentEnd = null;
      calendar.timezoneName = "";
      closeCalendarDrawer();
      closeCalendarBook();   /* Slice 3: re-entry is a page reset */
      closeCalendarAvail();  /* 4D-A: the same page-reset rule */
      setCalendarNavDisabled(true);
      setCalendarText("calendar-range-label", "");
      setCalendarText("calendar-timezone-note", "");
      var gridEl = byId("calendar-grid");
      if (gridEl) clearChildren(gridEl);
      showPage("page-calendar");
      loadCalendar();
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
    /* ---------------------------------------------------------------- */
    /* Notification settings (P6-A)                                      */
    /* ---------------------------------------------------------------- */

    /* Derived read-only helper text (D10): NOT an enable/disable toggle - it
     * only reflects whether each destination currently holds a value. */
    function renderSettingsStatus(email, phone) {
      setText("settings-email-status", email ?
        MESSAGES.settings_email_configured :
        MESSAGES.settings_email_unconfigured);
      setText("settings-sms-status", phone ?
        MESSAGES.settings_sms_configured :
        MESSAGES.settings_sms_unconfigured);
    }

    /* Apply an AUTHORITATIVE settings body to the page: the inputs, the
     * derived status lines, and the stored OPAQUE token (echoed verbatim on
     * the next save - never parsed, contract C4). */
    function applySettings(body) {
      var email = body.notification_email;
      var phone = body.notification_phone;
      byId("settings-email").value = email === null ? "" : email;
      byId("settings-phone").value = phone === null ? "" : phone;
      settings.token = body.notification_settings_updated_at;
      renderSettingsStatus(email, phone);
    }

    /* Load (or reload) the office's own destinations authoritatively. The
     * generation + saveSeq guards discard a response the page has moved past
     * (reset or a newer save/load), so a late GET can never repopulate a
     * wiped page or overwrite newer state.
     * successMessage (F2): when this GET is the authoritative refresh that
     * FOLLOWS a successful PUT, the saved wording is written to the feedback
     * line ONLY after this GET applies - so the confirmation is bound to the
     * post-mutation authoritative state, never to the PUT body. A plain
     * (nav-entry) load passes no message and clears the feedback line. */
    function loadSettings(successMessage) {
      var generation = settings.generation;
      var requestId = ++settings.saveSeq;
      settings.loaded = false;
      if (!successMessage) {
        setText("settings-feedback", "");
      }
      setText("settings-state", MESSAGES.loading);
      data.getNotificationSettings().then(function (outcome) {
        if (generation !== settings.generation ||
            requestId !== settings.saveSeq) {
          return;                            /* superseded / wiped */
        }
        if (!outcome.ok) {
          handleFailure(outcome, "settings-state");
          return;
        }
        setText("settings-state", "");
        settings.loaded = true;
        applySettings(outcome.data);
        if (successMessage) {
          setText("settings-feedback", successMessage);
        }
      });
    }

    function onSettingsSave() {
      if (settings.saveBusy || !settings.loaded) {
        return;                              /* duplicate submit blocked */
      }
      var email = byId("settings-email").value.trim();
      var phone = byId("settings-phone").value.trim();
      /* D6 mirrored locally for fast UX; the backend stays authoritative. */
      if (email === "" && phone === "") {
        setText("settings-feedback", MESSAGES.settings_both_empty);
        return;
      }
      settings.saveBusy = true;
      byId("settings-save").disabled = true;
      setText("settings-feedback", "");
      var generation = settings.generation;
      var seq = ++settings.saveSeq;
      /* Blanks are sent as null (clear that channel); the token is echoed
       * VERBATIM (opaque - never parsed, contract C4). */
      data.putNotificationSettings(
        email === "" ? null : email,
        phone === "" ? null : phone,
        settings.token
      ).then(function (outcome) {
        if (generation !== settings.generation) {
          return;                            /* wiped by reset - drop it */
        }
        settings.saveBusy = false;
        byId("settings-save").disabled = false;
        if (seq !== settings.saveSeq) {
          return;                            /* superseded - never overwrite */
        }
        if (outcome.ok) {
          /* F2 (contract v1.1): the validated PUT response is only an
           * INTERMEDIATE success signal - the post-mutation authoritative GET
           * is the frontend's FINAL authority for both the rendered
           * destinations and the stored token. The PUT body is NOT applied
           * here (no optimistic state). loadSettings' generation + saveSeq
           * guards mean this refresh cannot overwrite a newer save/load and
           * renders nothing if a reset/session-loss happened first; the saved
           * wording appears only once the GET applies. */
          loadSettings(MESSAGES.settings_saved);
          return;
        }
        if (outcome.state === "conflict") {
          /* Refresh authoritative state FIRST, then say it changed elsewhere
           * so the notice survives the redraw (the workflow pattern). */
          loadSettings();
          setText("settings-feedback", MESSAGES.settings_conflict);
          return;
        }
        if (outcome.state === "signed_out" ||
            outcome.state === "unauthorized") {
          handleFailure(outcome, "settings-feedback");
          return;
        }
        if (outcome.state === "bad_request") {
          setText("settings-feedback", MESSAGES.settings_invalid);
          return;
        }
        setText("settings-feedback",
          MESSAGES[outcome.state] || MESSAGES.settings_failed);
      });
    }

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
      /* P4-B (D10): invalidate any in-flight recurring request and wipe the
       * rendered panel; guarded so absent ids never throw. */
      recurring.generation += 1;
      recurring.busy = false;
      recurring.token = null;
      recurring.previewedToken = null;
      recurring.saveSeq += 1;
      recurring.previewSeq += 1;
      recurring.loaded = false;
      recurring.closures = [];
      recurring.rows = {};
      setRecurringText("recurring-state", "");
      setRecurringText("recurring-save-feedback", "");
      setRecurringText("recurring-preview-output", "");
      setRecurringText("recurring-apply-output", "");
      var recHoursEl = byId("recurring-hours"); if (recHoursEl) clearChildren(recHoursEl);
      var recClosuresEl = byId("recurring-closures"); if (recClosuresEl) clearChildren(recClosuresEl);
      var recMinutesEl = byId("recurring-slot-minutes"); if (recMinutesEl) recMinutesEl.value = "";
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
      /* P6-A: session loss / reset wipes the notification-settings page.
       * Bump generation to discard any in-flight settings GET/PUT, bump
       * saveSeq to supersede in-flight responses, drop the stored token and
       * the rendered destinations (no office contact may linger on a shared
       * front-desk computer, D10), and re-enable the control. */
      settings.generation += 1;
      settings.saveSeq += 1;
      settings.token = null;
      settings.loaded = false;
      settings.saveBusy = false;
      byId("settings-email").value = "";
      byId("settings-phone").value = "";
      setText("settings-email-status", "");
      setText("settings-sms-status", "");
      setText("settings-state", "");
      setText("settings-feedback", "");
      byId("settings-save").disabled = false;
      /* Visual Calendar Phase 1: session loss / reset invalidates any
       * in-flight calendar load (BOTH the request id and the lifecycle) and
       * wipes every rendered value - no office\u2019s appointment or slot times
       * may linger behind the login view on a shared front-desk computer.
       * Every access is null-tolerant so the frozen suites are unaffected. */
      requestIds.calendar += 1;
      calendar.generation += 1;
      calendar.lifecycle += 1;
      calendar.wipeEpoch += 1;   /* F1: a WIPE, not a re-entry */
      calendar.active = false;
      calendar.actionBusy = {};
      /* v1.0.1 F1: the wipe is the ONE event entitled to declare the
       * in-flight booking request irrelevant (the actionBusy rule). */
      calendar.bookBusy = null;
      /* 4B2 v1.0.1 F1: the SAME rule for the note-save owner - v1.0.0
       * documented this wipe but never performed it, which could strand a
       * freshly authenticated session behind the prior wiped session's
       * unresolved note PUT. Only THIS true wipe/reset boundary clears it:
       * drawer Close and Calendar re-entry continue to respect a real
       * unresolved request from the same session (the F4 principle), and a
       * late completion from the wiped session can neither clear a newer
       * owner (the token guard) nor render anything (the wipeEpoch guard
       * settles it first). actionSeq stays monotonic and is never reset,
       * so old and new tokens can never collide. */
      calendar.noteBusy = null;
      /* 4D-A: the SAME true-wipe rule for the availability owner - only
       * this reset boundary may declare the in-flight POST irrelevant. */
      calendar.availBusy = null;
      /* SLICE 4C: the wipe clears the picker source and selection - no
       * office's open-slot times may linger behind the login view, and a
       * post-reset drawer can never submit a pre-reset slot id. */
      calendar.scheduleSlots = [];
      calendar.rescheduleOpen = false;
      calendar.rescheduleSelectedId = null;
      calendar.rescheduleMode = null;
      calendar.armed = {};
      calendar.pendingFeedback = "";
      calendar.settling = 0;
      calendar.weekOffset = 0;
      calendar.defaultStart = null;
      calendar.currentStart = null;
      calendar.currentEnd = null;
      calendar.timezoneName = "";
      setCalendarText("calendar-state", "");
      setCalendarText("calendar-range-label", "");
      setCalendarText("calendar-timezone-note", "");
      var calendarGridEl = byId("calendar-grid");
      if (calendarGridEl) clearChildren(calendarGridEl);
      setCalendarNavDisabled(true);
      /* The detail panel carries patient contact details: close AND wipe
       * it, never merely hide it. */
      closeCalendarDrawer();
      /* SLICE 3: the booking panel carries TYPED patient contact details -
       * the same rule applies, with the same urgency. */
      closeCalendarBook();
      /* 4D-A: the availability panel carries no patient data, but a wipe
       * clears EVERY typed value and open surface - nothing may linger
       * behind the login view on a shared front-desk computer. */
      closeCalendarAvail();
    }

    /* Enter (or re-enter after a fresh sign-in): always lands on a fresh
     * Dashboard - nothing is trusted from a previous session's render. */
    function enter() {
      resetContent();
      showPage("page-dashboard");
      loadDashboard();
    }

    var RECURRING_ORDER = ["mon","tue","wed","thu","fri","sat","sun"];
    var RECURRING_CLOSURE_WARNING = "Removing a closure only updates your recurring configuration. Any slots a previous Apply blocked for those dates stay blocked. To make specific dates bookable again, reopen individual slots with Unblock on the Schedule page.";

    function setRecurringText(id, t){ var el = byId(id); if (el) el.textContent = t; }

    /* F2: a valid Apply requires a CURRENT Preview. Any config edit, a fresh
     * load, a successful Save, or an Apply conflict invalidates the previewed
     * token so Apply is disabled until the office Previews again - no stale
     * previewed token can ever be applied, and there is no permanent stale
     * loop (re-Preview always re-arms Apply against the latest token). */
    function invalidatePreview(){
      recurring.previewedToken = null;
      recurring.previewSeq += 1;   /* T3: supersede any in-flight Preview response */
      var applyBtn = byId("recurring-apply"); if (applyBtn) applyBtn.disabled = true;
    }

    /* Render one editable weekday row; editing any field invalidates Preview. */
    function buildRecurringRow(wd, row){
      var wrap = doc.createElement("div"); wrap.setAttribute("data-weekday", wd);
      var open = doc.createElement("input"); open.type = "checkbox"; open.checked = !!row.open;
      var start = doc.createElement("input"); start.type = "text";
      start.value = row.open ? (row.start || "") : "";
      var end = doc.createElement("input"); end.type = "text";
      end.value = row.open ? (row.end || "") : "";
      open.addEventListener("change", invalidatePreview);
      start.addEventListener("change", invalidatePreview);
      end.addEventListener("change", invalidatePreview);
      wrap.appendChild(open); wrap.appendChild(start); wrap.appendChild(end);
      recurring.rows[wd] = { open: open, start: start, end: end };
      return wrap;
    }

    /* Render the AUTHORITATIVE config (never echoed input). Rendering a fresh
     * config invalidates any prior Preview (F2). */
    function renderRecurring(cfg){
      recurring.token = cfg.schedule_config_updated_at;   /* opaque, verbatim */
      recurring.closures = Array.isArray(cfg.closures) ? cfg.closures.slice() : [];
      recurring.rows = {};
      var minutes = byId("recurring-slot-minutes");
      if (minutes) minutes.value = String(cfg.slot_minutes);
      var host = byId("recurring-hours");
      if (host){ clearChildren(host);
        for (var i=0;i<RECURRING_ORDER.length;i++){ var wd=RECURRING_ORDER[i];
          var row=(cfg.weekly_hours && cfg.weekly_hours[wd]) || {open:false};
          host.appendChild(buildRecurringRow(wd, row)); } }
      renderClosures();
      invalidatePreview();   /* F2: a new config requires a fresh Preview */
      setRecurringText("recurring-closure-warning", RECURRING_CLOSURE_WARNING);
    }

    /* Render closures: single-date {date} OR inclusive range {start,end} (F3). */
    function renderClosures(){
      var list = byId("recurring-closures"); if (!list) return; clearChildren(list);
      for (var i=0;i<recurring.closures.length;i++){ (function(idx){
        var c=recurring.closures[idx]; var li=doc.createElement("li");
        li.textContent = c && c.date ? c.date
          : (c && c.start ? (c.start + " to " + c.end) : "");
        var rm=doc.createElement("button"); rm.textContent="Remove";
        rm.addEventListener("click", function(){
          recurring.closures.splice(idx,1); renderClosures(); invalidatePreview(); });
        li.appendChild(rm); list.appendChild(li); })(i); }
    }

    function collectWeeklyHours(){
      var out={};
      for (var i=0;i<RECURRING_ORDER.length;i++){ var wd=RECURRING_ORDER[i];
        var r=recurring.rows[wd];
        if (r && r.open.checked){ out[wd]={open:true,start:r.start.value,end:r.end.value}; }
        else { out[wd]={open:false,start:null,end:null}; } }
      return out;
    }

    /* F3: add a single-date closure (end blank) OR an inclusive start..end
     * range. Backend keeps MAX_CLOSURES / range-length authority. */
    function onRecurringClosureAdd(){
      var startEl=byId("recurring-closure-date"); if(!startEl) return;
      var endEl=byId("recurring-closure-end");
      var start=(startEl.value||"").trim();
      var end=endEl ? (endEl.value||"").trim() : "";
      if(start===""){ return; }
      if(end===""){ recurring.closures.push({date:start}); }
      else { recurring.closures.push({start:start, end:end}); }
      startEl.value=""; if(endEl) endEl.value="";
      renderClosures(); invalidatePreview();
    }

    function openRecurring(){
      var gen=recurring.generation; showPage("page-recurring");
      setRecurringText("recurring-state","Loading recurring schedule...");
      data.getRecurringSchedule().then(function(outcome){
        if (gen!==recurring.generation) return;   /* superseded by reset (D10) */
        if (!outcome.ok){ handleFailure(outcome,"recurring-state"); return; }
        recurring.loaded=true; renderRecurring(outcome.data); setRecurringText("recurring-state","");
      });
    }

    /* F2: authoritative refresh - GET, render the GET body as the final state
     * and token. Guarded by generation (reset) and saveSeq (superseding). */
    function authoritativeRefresh(feedbackId, message){
      var gen=recurring.generation; var seq=(recurring.saveSeq += 1);
      data.getRecurringSchedule().then(function(outcome){
        if (gen!==recurring.generation || seq!==recurring.saveSeq) return;
        if (!outcome.ok){ handleFailure(outcome, feedbackId); return; }
        renderRecurring(outcome.data);   /* final authoritative state + token */
        if (message) setRecurringText(feedbackId, message);
      });
    }

    /* F2: Save follows the P6-A lifecycle - PUT success -> authoritative GET is
     * the final rendered state/token; a 409 conflict refreshes authoritatively;
     * Save always invalidates any prior Preview. */
    function onRecurringSave(){
      if (recurring.busy) return; recurring.busy=true;
      invalidatePreview();   /* V1: supersede any current/in-flight Preview BEFORE the PUT */
      var gen=recurring.generation;
      var minutesEl=byId("recurring-slot-minutes");
      var minutes=minutesEl?parseInt(minutesEl.value,10):NaN;
      setRecurringText("recurring-save-feedback","Saving...");
      data.putRecurringSchedule(collectWeeklyHours(), minutes, recurring.closures.slice(),
        recurring.token).then(function(outcome){
        recurring.busy=false; if (gen!==recurring.generation) return;
        if (!outcome.ok){
          if (outcome.state==="conflict"){
            authoritativeRefresh("recurring-save-feedback",
              "Settings changed elsewhere; showing the latest saved state.");
          } else { handleFailure(outcome,"recurring-save-feedback"); }
          return; }
        /* P6-A lifecycle: the authoritative GET (not the PUT body) is final. */
        authoritativeRefresh("recurring-save-feedback","Recurring schedule saved.");
      });
    }

    /* R2: human labels so each returned DAY shows its actual outcome (not just
     * an aggregate). Booked windows/counts are surfaced so staff can see a
     * closure will NOT cancel existing appointments. */
    function previewDayLabel(d){
      var o=d.outcome, s;
      if (o==="would_publish"){ s="would publish"+(d.would_publish_count!=null?" ("+d.would_publish_count+")":""); }
      else if (o==="existing_inventory"){ s="existing inventory"; }
      else if (o==="weekly_closed_empty"){ s="weekly closed"; }
      else if (o==="would_block"){ s="closure would block "+(d.would_block_available_held||0); }
      else if (o==="closure_empty"){ s="closure (no open slots)"; }
      else if (o==="dst_invalid"){ s="DST invalid - skipped"; }
      else { s=o; }
      if (Array.isArray(d.booked_windows) && d.booked_windows.length){
        s += " - booked preserved: "+d.booked_windows.length; }
      return s;
    }
    function applyDayLabel(d){
      var o=d.outcome, s;
      if (o==="published"){ s="published"+(d.published_count!=null?" ("+d.published_count+")":""); }
      else if (o==="existing_inventory_skipped"){ s="existing inventory - skipped"; }
      else if (o==="weekly_closed"){ s="weekly closed"; }
      else if (o==="closure_blocked"){ s="closure blocked "+(d.blocked_count||0); }
      else if (o==="closure_empty"){ s="closure (nothing to block)"; }
      else if (o==="dst_skipped"){ s="DST skipped"; }
      else { s=o; }
      if (Array.isArray(d.booked_remaining) && d.booked_remaining.length){
        s += " - booked preserved: "+d.booked_remaining.length; }
      return s;
    }
    /* Genuine per-day rendering: one "DATE - outcome" line per returned day. */
    function renderDayLines(header, days, labelFn){
      var lines=[header];
      for (var i=0;i<days.length;i++){ lines.push(days[i].day+" - "+labelFn(days[i])); }
      return lines.join("\n");
    }

    /* R1 + R2: Preview pins the returned token verbatim, enables Apply ONLY when
     * that token is non-null (a pre-first-Save Preview leaves Apply disabled),
     * and renders each day's actual outcome. */
    function onRecurringPreview(){
      if (recurring.busy) return;   /* V1: reject new Preview while Save/Apply is in flight */
      invalidatePreview();          /* V1: clear token + disable Apply + supersede older Preview... */
      var gen=recurring.generation; var seq=recurring.previewSeq;   /* ...THEN capture this request */
      setRecurringText("recurring-preview-output","Computing preview...");
      data.previewRecurringSchedule().then(function(outcome){
        /* T3: ignore a stale Preview response - superseded by reset (generation),
         * or by any newer Preview / Save / edit / Apply-conflict (previewSeq). */
        if (gen!==recurring.generation || seq!==recurring.previewSeq) return;
        if (!outcome.ok){ invalidatePreview(); handleFailure(outcome,"recurring-preview-output"); return; }
        var d=outcome.data;
        recurring.previewedToken = d.schedule_config_updated_at;   /* pin exact token (may be null) */
        var applyBtn=byId("recurring-apply");
        if (applyBtn) applyBtn.disabled = (recurring.previewedToken === null);   /* R1 */
        var note = recurring.previewedToken===null
          ? " Save the schedule before it can be applied." : "";
        var header="Preview "+d.start_day+" to "+d.end_day+" ("+d.days.length+
          " days); applied day by day, not one transaction."+note;
        setRecurringText("recurring-preview-output", renderDayLines(header, d.days, previewDayLabel));
      });
    }

    /* R1 + R2: Apply sends the PREVIEWED token verbatim (only possible when it is
     * non-null), renders each day's actual outcome plus totals, then invalidates
     * the Preview; a stale conflict refreshes and requires a fresh Preview. */
    function onRecurringApply(){
      var applyToken = recurring.previewedToken;   /* V1: capture the exact token first */
      if (applyToken===null) return;               /* R1: never fire without a current Preview */
      if (recurring.busy) return; recurring.busy=true;
      invalidatePreview();   /* V1: consume - one-shot per Preview; Apply disables immediately;
                             * a late older Preview cannot re-arm Apply while this runs. */
      var gen=recurring.generation;
      setRecurringText("recurring-apply-output","Applying...");
      data.applyRecurringSchedule(applyToken).then(function(outcome){
        recurring.busy=false; if (gen!==recurring.generation) return;
        if (!outcome.ok){
          if (outcome.state==="conflict"){
            authoritativeRefresh("recurring-apply-output",
              "Configuration changed; Preview again before applying.");
            return; }
          handleFailure(outcome,"recurring-apply-output"); return; }
        var d=outcome.data; var t=d.totals;
        recurring.token = d.schedule_config_updated_at;   /* refresh authoritative token
           (the Preview was already consumed/invalidated at Apply start - V1) */
        var header="Applied "+d.start_day+" to "+d.end_day+" (day by day):";
        var body=renderDayLines(header, d.days, applyDayLabel)+
          "\nTotals - published days: "+t.published_days+
          ", closure-blocked days: "+t.closure_blocked_days+
          ", inventory-skipped days: "+t.existing_inventory_skipped_days+".";
        setRecurringText("recurring-apply-output", body);
      });
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
      byId("nav-settings").addEventListener("click", function () {
        showPage("page-settings");
        loadSettings();
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
      byId("settings-save").addEventListener("click", onSettingsSave);
      /* P4-B: null-tolerant wiring so a page suite WITHOUT the recurring ids
       * (e.g. the frozen notification-settings suite) never breaks at
       * construction; the recurring id-contract suites include these ids. */
      var navRec = byId("nav-recurring");
      if (navRec) navRec.addEventListener("click", openRecurring);
      var recSave = byId("recurring-save");
      if (recSave) recSave.addEventListener("click", onRecurringSave);
      var recPrev = byId("recurring-preview");
      if (recPrev) recPrev.addEventListener("click", onRecurringPreview);
      var recApply = byId("recurring-apply");
      if (recApply) recApply.addEventListener("click", onRecurringApply);
      var recAddClosure = byId("recurring-closure-add");
      if (recAddClosure) recAddClosure.addEventListener("click", onRecurringClosureAdd);
      var recMinutesInput = byId("recurring-slot-minutes");
      if (recMinutesInput) recMinutesInput.addEventListener("change", invalidatePreview);
      /* Visual Calendar Phase 1: same null-tolerant convention, so a suite
       * whose fake DOM predates these ids constructs without throwing. */
      var navCalendar = byId("nav-calendar");
      if (navCalendar) navCalendar.addEventListener("click", openCalendar);
      var calPrev = byId("calendar-prev");
      if (calPrev) calPrev.addEventListener("click", onCalendarPrev);
      var calNext = byId("calendar-next");
      if (calNext) calNext.addEventListener("click", onCalendarNext);
      var calRefresh = byId("calendar-refresh");
      if (calRefresh) calRefresh.addEventListener("click", onCalendarRefresh);
      var calDrawerClose = byId("calendar-drawer-close");
      if (calDrawerClose) {
        calDrawerClose.addEventListener("click", closeCalendarDrawer);
      }
      /* PHASE 3A Slice 3: booking panel controls (null-tolerant, so frozen
       * suites whose fake DOM predates these ids construct unchanged). */
      var calBookSubmit = byId("calendar-book-submit");
      if (calBookSubmit) {
        calBookSubmit.addEventListener("click", onCalendarBookSubmit);
      }
      var calBookClose = byId("calendar-book-close");
      if (calBookClose) {
        calBookClose.addEventListener("click", closeCalendarBook);
      }
      /* PHASE 3A Slice 4D-A: one-off availability panel controls
       * (null-tolerant, the frozen-suite construction rule). */
      var calAvailOpen = byId("calendar-avail-open");
      if (calAvailOpen) {
        calAvailOpen.addEventListener("click", openCalendarAvail);
      }
      var calAvailClose = byId("calendar-avail-close");
      if (calAvailClose) {
        calAvailClose.addEventListener("click", closeCalendarAvail);
      }
      var calAvailSubmit = byId("calendar-avail-submit");
      if (calAvailSubmit) {
        calAvailSubmit.addEventListener("click", onCalendarAvailSubmit);
      }
      /* PHASE 3A Slice 4C: drawer reschedule picker controls
       * (null-tolerant, the frozen-suite construction rule). */
      var reschedSave = byId("calendar-drawer-reschedule-save");
      if (reschedSave) {
        reschedSave.addEventListener("click", onCalendarRescheduleSave);
      }
      var reschedCancel = byId("calendar-drawer-reschedule-cancel");
      if (reschedCancel) {
        reschedCancel.addEventListener("click", closeCalendarReschedule);
      }
      /* PHASE 3A Slice 4B2: drawer note controls (null-tolerant). */
      var noteEdit = byId("calendar-drawer-note-edit");
      if (noteEdit) {
        noteEdit.addEventListener("click", openCalendarNoteEditor);
      }
      var noteSave = byId("calendar-drawer-note-save");
      if (noteSave) {
        noteSave.addEventListener("click", onCalendarNoteSave);
      }
      var noteCancel = byId("calendar-drawer-note-cancel");
      if (noteCancel) {
        /* Cancel is LOCAL: it touches the network never, wipes the typed
         * text, and the untouched display simply shows again. */
        noteCancel.addEventListener("click", closeCalendarNoteEditor);
      }
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
    appointmentActionsFor: appointmentActionsFor,
    /* SLICE 4C: the calendar drawer's action set, exported so the suite
     * can pin its terminal-offers-nothing rule as a pure function. */
    calendarDrawerActionsFor: calendarDrawerActionsFor
  };

  /* Export for both the browser (window) and the Node test harness. */
  globalScope.createMiaPortalPages = createMiaPortalPages;

}(typeof window !== "undefined" ? window : this));
