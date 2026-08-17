/*
 * portal-calendar.js - Mia Visual Calendar Phase 1: read-only Week view
 * geometry and rendering (time-axis refinement).
 *
 * OWNERSHIP (Constitution 5 / Rule 3): this file is a PURE presentation
 * module. It owns time-axis geometry, visual bucketing, band consolidation,
 * overlap lanes, and DOM fragment construction - and nothing else.
 *
 * It deliberately owns NONE of the following, and the static audit proves
 * the absence of the first group:
 *   - network access of any kind (no request API, no endpoint literal)
 *   - authentication, tokens, storage
 *   - tenant identity
 *   - booking policy, availability policy, or slot eligibility
 *   - the authoritative date range or timezone (both arrive on the server
 *     envelopes and are never recomputed here)
 *
 * REUSE, NEVER DUPLICATION: formatInTimeZone, scheduleSlotStatusLabel,
 * appointmentStatusLabel, notificationOutcomeLabel and shiftLocalDay are
 * INJECTED by the caller (portal-pages.js owns them). This module
 * re-implements none of them.
 *
 * SERVER IS AUTHORITATIVE: start_day, end_day and timezone_name come from
 * the responses. This module never derives "today", never reads device time
 * as a scheduling source, and never invents inventory.
 *
 * THE ONE LOCAL COMPUTATION is PRESENTATION GEOMETRY: converting each
 * authoritative UTC instant into the office-local wall clock so a block can
 * be POSITIONED on a time axis. It is done through Intl with the OFFICE
 * timezone the server declared (never the device timezone) in ONE place -
 * localParts below - and the same conversion supplies both the vertical
 * position and the block's short time caption, so position and caption can
 * never disagree. Because it is a real timezone conversion rather than an
 * ISO-string slice, it stays correct across DST transitions (for
 * America/New_York: 2026-03-08 and 2026-11-01).
 *
 * The long, fully-qualified rendering (date + time + timezone) is still
 * produced by the injected formatInTimeZone, and now appears only in the
 * detail drawer and once at calendar level - never repeated inside every
 * block, which is what made the first resting view unreadable.
 *
 * FAIL CLOSED: if the two responses disagree about start_day, end_day or
 * timezone_name, buildGrid refuses and returns ok:false with no element.
 * Mixed authoritative ranges are never rendered (Constitution 14).
 *
 * PRESENTATION FILTERING (Phase 1 scope): cancelled slots and cancelled /
 * completed / no_show appointments are omitted from the resting calendar.
 * This filters the VIEW only - no request parameter changes, no source data
 * is altered, and the backend is untouched. Consolidating adjacent open
 * slots into one availability band is likewise presentation only: every
 * underlying slot row remains individually authoritative and is counted on
 * the band, never merged away.
 */
(function (globalScope) {
  "use strict";

  /* Hard bound on rendered columns, mirroring the backend's 31-day read cap
   * so a malformed or reversed range can never spin or render unbounded. */
  var MAX_DAY_COLUMNS = 31;

  /* Time-axis geometry, named in one reviewable place (Rule 4). One hour of
   * office-local wall clock is HOUR_HEIGHT_PX tall, so a 30-minute block is
   * exactly half the height of a 60-minute block. */
  var HOUR_HEIGHT_PX = 48;
  var MINUTES_PER_DAY = 1440;
  /* The resting viewport when nothing forces it wider. It is a VIEWPORT
   * choice only - it never hides an item, because the range always expands
   * to contain every rendered entry. */
  var DEFAULT_FIRST_HOUR = 8;
  var DEFAULT_LAST_HOUR = 18;
  /* A block shorter than this still gets this much height so it stays
   * readable and clickable; the caption always states the true time. */
  var MIN_BLOCK_MINUTES = 18;
  /* Below this duration a block drops its secondary line and renders on one
   * compact row. */
  var COMPACT_BLOCK_MINUTES = 45;
  var MIN_VISIBLE_HISTORY_MINUTES = MIN_BLOCK_MINUTES;

  var WEEKDAY_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /* The closed set of slot statuses the resting calendar displays.
   * "cancelled" is deliberately absent (presentation filtering). */
  var VISIBLE_SLOT_STATUSES = ["available", "held", "blocked"];

  /* Appointment statuses split into two presentation roles.
   *
   * ACTIVE occupies time: it is laid out in overlap lanes, it competes for
   * the office's attention, and it means a patient is expected.
   *
   * HISTORY does NOT occupy time. A cancelled appointment is context the
   * office may want (who was booked here, so we can follow up) but it is
   * emphatically not occupancy: whether the time is open now is a question
   * only the authoritative Schedule response answers, and this module never
   * infers availability from an appointment row.
   *
   * completed and no_show stay hidden: neither is follow-up context, and
   * nothing in the current source argues for surfacing them. */
  var ACTIVE_APPOINTMENT_STATUSES = ["pending", "confirmed"];
  var HISTORY_APPOINTMENT_STATUSES = ["cancelled"];
  var VISIBLE_APPOINTMENT_STATUSES =
    ACTIVE_APPOINTMENT_STATUSES.concat(HISTORY_APPOINTMENT_STATUSES);

  /* The compact horizontal history strip: the affordance that keeps a
   * cancelled appointment reachable when a live one is drawn over it.
   * It is only ever rendered when the ghost itself has become unreadable.
   *
   * HISTORY_STRIP_HEIGHT_PX is one thin line, sat on the BOTTOM edge of the
   * occluded region. That placement is deliberate: a live block renders its
   * time and patient on its first lines, so a strip along the bottom covers
   * only its least important line and the live appointment stays dominant.
   *
   * MIN_VISIBLE_HISTORY_MINUTES is how much clear air the TOP of a ghost
   * needs before it counts as readable on its own. It reuses the minimum
   * block height the renderer already guarantees every entry - roughly one
   * line of text - rather than inventing a second magic number. */
  var HISTORY_STRIP_HEIGHT_PX = 14;

  var KIND_SLOT = "slot";
  var KIND_APPOINTMENT = "appointment";

  /* Refusal reasons (closed vocabulary, Constitution 4.5). */
  var REASON_OK = "ok";
  var REASON_RANGE_MISMATCH = "range_mismatch";
  var REASON_RANGE_UNUSABLE = "range_unusable";

  /*
   * Purpose: is this a REAL calendar date in YYYY-MM-DD form? Rejects both
   * malformed text and impossible dates (2026-02-30) by round-tripping
   * through UTC, so a bad bound can never silently become a column. Pure.
   */
  function isRealCalendarDay(text) {
    if (typeof text !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(text)) {
      return false;
    }
    var year = Number(text.slice(0, 4));
    var month = Number(text.slice(5, 7));
    var day = Number(text.slice(8, 10));
    if (month < 1 || month > 12 || day < 1 || day > 31) {
      return false;
    }
    var probe = new Date(Date.UTC(year, month - 1, day));
    return probe.getUTCFullYear() === year &&
      probe.getUTCMonth() === month - 1 &&
      probe.getUTCDate() === day;
  }

  /*
   * Purpose: the inclusive list of day columns between the server's echoed
   * bounds, walked with the INJECTED shiftLocalDay (no second date-shift
   * implementation).
   * Returns: an array of YYYY-MM-DD strings, or [] when the bounds are
   *   malformed, reversed, non-advancing, or exceed MAX_DAY_COLUMNS.
   */
  function dayColumns(startDay, endDay, shiftLocalDay) {
    if (!isRealCalendarDay(startDay) || !isRealCalendarDay(endDay)) {
      return [];
    }
    if (typeof shiftLocalDay !== "function") {
      return [];
    }
    var columns = [];
    var cursor = startDay;
    while (columns.length < MAX_DAY_COLUMNS) {
      columns.push(cursor);
      if (cursor === endDay) {
        return columns;
      }
      var stepped = shiftLocalDay(cursor, 1);
      if (!isRealCalendarDay(stepped) || stepped <= cursor) {
        return [];
      }
      cursor = stepped;
    }
    return [];
  }

  /*
   * Purpose: the two-part column header for one server-supplied day string.
   * Pure UTC calendar math on a label the server already resolved - never
   * Intl and never device time.
   * Returns: { weekday: "Mon", date: "Aug 17" } or { weekday: "", date: "" }.
   */
  function dayHeaderParts(dayText) {
    if (!isRealCalendarDay(dayText)) {
      return { weekday: "", date: "" };
    }
    var year = Number(dayText.slice(0, 4));
    var month = Number(dayText.slice(5, 7));
    var day = Number(dayText.slice(8, 10));
    var probe = new Date(Date.UTC(year, month - 1, day));
    return {
      weekday: WEEKDAY_SHORT[probe.getUTCDay()],
      date: MONTH_SHORT[month - 1] + " " + day
    };
  }

  /* Two-digit pad for minute captions. Pure. */
  function pad2(value) {
    return value < 10 ? "0" + value : String(value);
  }

  /*
   * Purpose: the SHORT office-local caption for a wall-clock hour/minute.
   * Pure 12-hour arithmetic on parts already converted in the office
   * timezone - it introduces no second timezone decision, only a shorter
   * rendering of the same converted instant.
   * Returns: e.g. "9:00 AM", "12:30 PM".
   */
  function clockCaption(hour, minute) {
    var suffix = hour < 12 ? "AM" : "PM";
    var display = hour % 12 === 0 ? 12 : hour % 12;
    return display + ":" + pad2(minute) + " " + suffix;
  }

  /* The hour-rail caption, same arithmetic without minutes: "8 AM", "12 PM". */
  function hourCaption(hour) {
    var normalized = ((hour % 24) + 24) % 24;
    var suffix = normalized < 12 ? "AM" : "PM";
    var display = normalized % 12 === 0 ? 12 : normalized % 12;
    return display + " " + suffix;
  }

  /*
   * Purpose: extract office-local wall-clock parts for one instant.
   * Returns null when the timezone is unsupported so the caller can fall
   * back EXPLICITLY (the formatInTimeZone discipline: never a silent slide
   * into device time).
   */
  function wallClockParts(dateValue, timeZone) {
    try {
      var pieces = new Intl.DateTimeFormat("en-US", {
        timeZone: timeZone,
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hourCycle: "h23"
      }).formatToParts(dateValue);
      var found = { year: "", month: "", day: "", hour: "", minute: "" };
      for (var i = 0; i < pieces.length; i++) {
        if (Object.prototype.hasOwnProperty.call(found, pieces[i].type)) {
          found[pieces[i].type] = pieces[i].value;
        }
      }
      if (found.year === "" || found.month === "" || found.day === "" ||
          found.hour === "" || found.minute === "") {
        return null;
      }
      return found;
    } catch (err) {
      return null;
    }
  }

  /*
   * Purpose: THE single conversion from an authoritative UTC instant to the
   * office-local presentation values every part of the grid needs: which day
   * column it belongs to, where it sits on the time axis, and how its short
   * caption reads. Doing all three from ONE conversion means a block's
   * position and its printed time can never disagree.
   *
   * This is a real timezone conversion, NOT an ISO-string slice, which is
   * exactly what makes it survive DST: on 2026-11-01 (America/New_York, a
   * 25-hour local day) the instant 2026-11-02T03:00:00Z is 22:00 local on
   * NOVEMBER 1 and lands in the November 1 column at the 22:00 position; a
   * string slice would wrongly file it under November 2.
   *
   * A missing/blank/unsupported timezone falls back to EXPLICIT UTC rather
   * than the device timezone (there is no device-time path in this file).
   * Returns: { day, minutes, caption } or null for an unusable instant.
   */
  function localParts(isoText, timeZone) {
    if (typeof isoText !== "string" || isoText === "") {
      return null;
    }
    var parsed = new Date(isoText);
    if (isNaN(parsed.getTime())) {
      return null;
    }
    var zone = (typeof timeZone === "string" && timeZone !== "")
      ? timeZone : "UTC";
    var parts = wallClockParts(parsed, zone);
    if (parts === null) {
      parts = wallClockParts(parsed, "UTC");
    }
    if (parts === null) {
      return null;
    }
    var hour = Number(parts.hour) % 24;
    var minute = Number(parts.minute);
    return {
      day: parts.year + "-" + parts.month + "-" + parts.day,
      minutes: hour * 60 + minute,
      caption: clockCaption(hour, minute)
    };
  }

  /* Retained for callers that only need the column: the same one conversion. */
  function localDayOf(isoText, timeZone) {
    var parts = localParts(isoText, timeZone);
    return parts === null ? "" : parts.day;
  }

  /* Presentation filters (Phase 1 scope). Unknown statuses are NOT shown - a
   * speculative render of an unrecognised vocabulary value would be a guess;
   * the Schedule and Appointments pages remain the complete views. */
  function isVisibleSlotStatus(status) {
    return VISIBLE_SLOT_STATUSES.indexOf(status) !== -1;
  }

  function isVisibleAppointmentStatus(status) {
    return VISIBLE_APPOINTMENT_STATUSES.indexOf(status) !== -1;
  }

  function isActiveAppointmentStatus(status) {
    return ACTIVE_APPOINTMENT_STATUSES.indexOf(status) !== -1;
  }

  /* A rendered appointment that is HISTORY rather than occupancy. */
  function isHistoryAppointmentStatus(status) {
    return HISTORY_APPOINTMENT_STATUSES.indexOf(status) !== -1;
  }

  /*
   * Purpose: the RESPONSE CONSISTENCY GUARD. The grid combines two
   * independent authoritative reads; it may only do so when both describe
   * the SAME window in the SAME timezone. Any disagreement (or a malformed
   * envelope) refuses. Pure.
   */
  function rangesAgree(scheduleBody, appointmentsBody) {
    if (scheduleBody === null || typeof scheduleBody !== "object" ||
        appointmentsBody === null || typeof appointmentsBody !== "object") {
      return false;
    }
    if (typeof scheduleBody.timezone_name !== "string" ||
        scheduleBody.timezone_name === "") {
      return false;
    }
    if (!isRealCalendarDay(scheduleBody.start_day) ||
        !isRealCalendarDay(scheduleBody.end_day)) {
      return false;
    }
    return scheduleBody.start_day === appointmentsBody.start_day &&
      scheduleBody.end_day === appointmentsBody.end_day &&
      scheduleBody.timezone_name === appointmentsBody.timezone_name;
  }

  /*
   * Purpose: build ONE positioned entry from an authoritative row.
   * End geometry: the office-local wall-clock end is used, so a block's
   * height matches the clock the office reads. An end at or before the start
   * (midnight rollover, or a DST artefact) is clamped to the end of the local
   * day rather than rendered inverted or dropped.
   * Returns null when the start instant is unusable.
   */
  function positionEntry(kind, status, startIso, endIso, timeZone, payload) {
    var start = localParts(startIso, timeZone);
    if (start === null) {
      return null;
    }
    var end = localParts(endIso, timeZone);
    var endMinutes;
    if (end === null || end.day !== start.day || end.minutes <= start.minutes) {
      endMinutes = MINUTES_PER_DAY;
    } else {
      endMinutes = end.minutes;
    }
    if (endMinutes > MINUTES_PER_DAY) {
      endMinutes = MINUTES_PER_DAY;
    }
    return {
      kind: kind,
      status: status,
      day: start.day,
      startMinutes: start.minutes,
      endMinutes: endMinutes,
      durationMinutes: endMinutes - start.minutes,
      caption: start.caption,
      endCaption: end === null ? "" : end.caption,
      historical: false,
      payload: payload || null
    };
  }

  /*
   * Purpose: flatten both server payloads into positioned entries, applying
   * the Phase 1 presentation filters. Pure.
   */
  function collectEntries(scheduleBody, appointmentsBody, timeZone) {
    var entries = [];
    var slots = Array.isArray(scheduleBody.slots) ? scheduleBody.slots : [];
    for (var i = 0; i < slots.length; i++) {
      var slot = slots[i];
      if (!slot || !isVisibleSlotStatus(slot.status)) {
        continue;
      }
      /* SLICE 3: the slot ROW rides along as the entry payload (exactly
       * what appointment entries already do), so a consolidated band can
       * hand back the REAL underlying inventory. Presentation still
       * decides nothing: the row is the server's, unmodified. */
      var slotEntry = positionEntry(KIND_SLOT, slot.status,
        slot.start_datetime, slot.end_datetime, timeZone, slot);
      if (slotEntry !== null) {
        entries.push(slotEntry);
      }
    }
    var appointments = Array.isArray(appointmentsBody.appointments)
      ? appointmentsBody.appointments : [];
    for (var j = 0; j < appointments.length; j++) {
      var appointment = appointments[j];
      if (!appointment || !isVisibleAppointmentStatus(appointment.status)) {
        continue;
      }
      var apptEntry = positionEntry(KIND_APPOINTMENT, appointment.status,
        appointment.start_datetime, appointment.end_datetime, timeZone,
        appointment);
      if (apptEntry !== null) {
        /* Decided ONCE, here, from the authoritative status - so the layer
         * an entry lands in and the lane maths it is excluded from can
         * never disagree. */
        apptEntry.historical = isHistoryAppointmentStatus(appointment.status);
        entries.push(apptEntry);
      }
    }
    return entries;
  }

  /*
   * Purpose: does this entry share any minute with an ACTIVE appointment?
   * Half-open on both sides, so entries that merely touch end-to-start do
   * NOT count as overlapping. Pure.
   * Used only to decide whether a cancelled entry needs a marker: a ghost
   * that nothing is drawn over stays perfectly visible on its own.
   */
  function overlapsAnyActive(entry, activeEntries) {
    for (var i = 0; i < activeEntries.length; i++) {
      var active = activeEntries[i];
      if (entry.startMinutes < active.endMinutes &&
          active.startMinutes < entry.endMinutes) {
        return true;
      }
    }
    return false;
  }

  /*
   * Purpose: the LARGEST contiguous run of minutes anywhere inside this
   * entry that no active appointment covers.
   *
   * "Anywhere" is the point. An earlier rule looked only at the entry's top,
   * which failed the real case the office reported: a cancelled 11:30-12:30
   * sitting under an active 11:00-12:00 has its first line covered, yet the
   * whole 12:00-12:30 tail is exposed and reads perfectly well as
   * "Aisha Khan / CANCELLED". A ghost like that is its own affordance and
   * must not be given a second one.
   *
   * Overlapping actives are clipped to this entry, merged, and the gaps
   * between them measured - including the gap before the first and after the
   * last. Pure; reads nothing but the minutes already computed for layout.
   */
  function largestVisibleSpanMinutes(entry, activeEntries) {
    var covered = [];
    for (var i = 0; i < activeEntries.length; i++) {
      var active = activeEntries[i];
      var from = Math.max(active.startMinutes, entry.startMinutes);
      var to = Math.min(active.endMinutes, entry.endMinutes);
      if (from < to) {
        covered.push({ from: from, to: to });
      }
    }
    covered.sort(function (left, right) { return left.from - right.from; });

    var largest = 0;
    var cursor = entry.startMinutes;
    for (var c = 0; c < covered.length; c++) {
      if (covered[c].from > cursor && covered[c].from - cursor > largest) {
        largest = covered[c].from - cursor;
      }
      if (covered[c].to > cursor) {
        cursor = covered[c].to;
      }
    }
    if (entry.endMinutes - cursor > largest) {
      largest = entry.endMinutes - cursor;
    }
    return largest;
  }

  /*
   * Purpose: THE rule that decides whether a cancelled ghost still speaks
   * for itself, or needs a separate affordance.
   *   no overlap at all              -> never occluded (Case 1)
   *   a meaningful exposed span left -> not occluded, the ghost IS the
   *                                     affordance and nothing extra is
   *                                     drawn, wherever that span sits
   *                                     within the entry (Case 2)
   *   nothing meaningful exposed     -> effectively occluded, so a compact
   *                                     horizontal history strip is drawn
   *                                     (Case 3)
   * Presentation only: it changes no geometry and reads no inventory. Pure.
   */
  function isHistoryOccluded(entry, activeEntries) {
    if (!overlapsAnyActive(entry, activeEntries)) {
      return false;
    }
    return largestVisibleSpanMinutes(entry, activeEntries) <
      MIN_VISIBLE_HISTORY_MINUTES;
  }

  /*
   * Purpose (SLICE 4C - Bug A): does this history entry share any minute
   * with an OPEN (available) consolidated band? Half-open on both sides,
   * exactly the overlapsAnyActive rule. Pure.
   *
   * Why it exists: a cancelled ghost paints ABOVE the availability bands
   * (history layer z1 over the base bands layer), and the ghost is a real
   * button (pointer-events: auto per the 7496573 hotfix) - so a full-height
   * ghost sitting over a green Open band swallowed every click meant for
   * the band, and the office could not book the reusable time underneath.
   * When THIS predicate is true, the renderer DEMOTES the ghost to the
   * existing compact history strip (its own thin, deterministic hit target
   * on the bottom edge) and draws NO full ghost block, so the Open band's
   * button is reachable everywhere else. Two real controls, two distinct
   * hit areas - never an invisible overlapping click ambiguity. Held and
   * blocked bands are deliberately NOT considered: they are inert regions
   * with no click target for a ghost to steal.
   */
  function overlapsAnyAvailableBand(entry, bands) {
    for (var i = 0; i < bands.length; i++) {
      if (bands[i].status !== "available") {
        continue;
      }
      if (entry.startMinutes < bands[i].endMinutes &&
          bands[i].startMinutes < entry.endMinutes) {
        return true;
      }
    }
    return false;
  }

  /*
   * Purpose: consolidate adjacent same-status slot entries into bands, so a
   * full day of thirty-minute openings reads as one calm availability region
   * instead of sixteen stacked cards.
   *
   * PRESENTATION ONLY: nothing is merged away. Each band carries the exact
   * COUNT of authoritative slot rows it covers - and, SLICE 3, the rows
   * THEMSELVES (references to the server response, never copies or
   * derivations), so a caller can act on the real inventory behind the
   * band. The individual rows remain the backend's own inventory, and the
   * Schedule page continues to show them one by one with their per-slot
   * controls.
   *
   * Bands merge only when the statuses match and the next entry starts at or
   * before the current band's end (contiguous or overlapping). Pure.
   */
  function consolidateBands(slotEntries) {
    var ordered = slotEntries.slice().sort(function (left, right) {
      if (left.status !== right.status) {
        return left.status < right.status ? -1 : 1;
      }
      return left.startMinutes - right.startMinutes;
    });
    var bands = [];
    for (var i = 0; i < ordered.length; i++) {
      var entry = ordered[i];
      var last = bands.length ? bands[bands.length - 1] : null;
      if (last !== null && last.status === entry.status &&
          entry.startMinutes <= last.endMinutes) {
        if (entry.endMinutes > last.endMinutes) {
          last.endMinutes = entry.endMinutes;
        }
        last.slotCount += 1;
        last.slots.push(entry.payload);
        continue;
      }
      bands.push({
        status: entry.status,
        startMinutes: entry.startMinutes,
        endMinutes: entry.endMinutes,
        slotCount: 1,
        /* The authoritative rows behind this band (SLICE 3). */
        slots: [entry.payload],
        caption: entry.caption
      });
    }
    return bands.sort(function (left, right) {
      return left.startMinutes - right.startMinutes;
    });
  }

  /*
   * Purpose: assign side-by-side lanes to overlapping appointment blocks, so
   * two appointments at the same hour are both readable instead of hidden
   * behind one another. Lanes are computed per overlap CLUSTER, so one long
   * overlap elsewhere in the day never narrows unrelated blocks. Pure.
   * Mutates and returns the entries with lane / laneCount attached.
   */
  function assignLanes(appointmentEntries) {
    var ordered = appointmentEntries.slice().sort(function (left, right) {
      if (left.startMinutes !== right.startMinutes) {
        return left.startMinutes - right.startMinutes;
      }
      return left.endMinutes - right.endMinutes;
    });
    var cluster = [];
    var clusterEnd = -1;

    function closeCluster() {
      var laneCount = 0;
      for (var c = 0; c < cluster.length; c++) {
        if (cluster[c].lane + 1 > laneCount) {
          laneCount = cluster[c].lane + 1;
        }
      }
      for (var d = 0; d < cluster.length; d++) {
        cluster[d].laneCount = laneCount;
      }
      cluster = [];
      clusterEnd = -1;
    }

    for (var i = 0; i < ordered.length; i++) {
      var entry = ordered[i];
      if (cluster.length && entry.startMinutes >= clusterEnd) {
        closeCluster();
      }
      var laneEnds = [];
      for (var j = 0; j < cluster.length; j++) {
        var occupied = cluster[j];
        while (laneEnds.length <= occupied.lane) {
          laneEnds.push(-1);
        }
        if (occupied.endMinutes > laneEnds[occupied.lane]) {
          laneEnds[occupied.lane] = occupied.endMinutes;
        }
      }
      var lane = 0;
      while (lane < laneEnds.length && laneEnds[lane] > entry.startMinutes) {
        lane += 1;
      }
      entry.lane = lane;
      entry.laneCount = 1;
      cluster.push(entry);
      if (entry.endMinutes > clusterEnd) {
        clusterEnd = entry.endMinutes;
      }
    }
    if (cluster.length) {
      closeCluster();
    }
    return ordered;
  }

  /*
   * Purpose: the visible hour window. It starts from the resting default and
   * EXPANDS to contain every rendered entry, so a viewport preference can
   * never hide authoritative data. Pure.
   * Returns { firstHour, lastHour } with lastHour > firstHour.
   */
  function hourWindow(entries) {
    var firstHour = DEFAULT_FIRST_HOUR;
    var lastHour = DEFAULT_LAST_HOUR;
    for (var i = 0; i < entries.length; i++) {
      var startHour = Math.floor(entries[i].startMinutes / 60);
      var endHour = Math.ceil(entries[i].endMinutes / 60);
      if (startHour < firstHour) {
        firstHour = startHour;
      }
      if (endHour > lastHour) {
        lastHour = endHour;
      }
    }
    if (firstHour < 0) {
      firstHour = 0;
    }
    if (lastHour > 24) {
      lastHour = 24;
    }
    if (lastHour <= firstHour) {
      lastHour = firstHour + 1;
    }
    return { firstHour: firstHour, lastHour: lastHour };
  }

  /*
   * Purpose: THE geometry rule - office-local wall-clock minutes to pixels.
   * A 30-minute entry is exactly half the height of a 60-minute entry
   * because both are (minutes / 60) * HOUR_HEIGHT_PX. Pure.
   * Returns { top, height } in pixels; height respects MIN_BLOCK_MINUTES so
   * a very short entry stays readable and clickable.
   */
  function geometryFor(startMinutes, endMinutes, firstHour) {
    var top = ((startMinutes - firstHour * 60) / 60) * HOUR_HEIGHT_PX;
    var minutes = endMinutes - startMinutes;
    if (minutes < MIN_BLOCK_MINUTES) {
      minutes = MIN_BLOCK_MINUTES;
    }
    return { top: top, height: (minutes / 60) * HOUR_HEIGHT_PX };
  }

  /* ------------------------------------------------------------------ */
  /* Factory                                                             */
  /* ------------------------------------------------------------------ */

  /*
   * Purpose: build the calendar renderer.
   * Inputs (deps, all REQUIRED - no permissive defaults, Rule 4):
   *   documentRef              the document (createElement only)
   *   formatInTimeZone         the portal's ONE fully-qualified time renderer
   *   scheduleSlotStatusLabel  the portal's ONE slot status vocabulary
   *   appointmentStatusLabel   the portal's ONE appointment status vocabulary
   *   notificationOutcomeLabel the portal's ONE notification vocabulary
   *   shiftLocalDay            the portal's ONE pure day-shift helper
   *   onAppointmentSelect      READ-ONLY callback invoked with the already
   *                            loaded appointment row when a block is
   *                            activated. No request is made here or there.
   *   onOpenBandSelect         SLICE 3 callback invoked with the ALREADY
   *                            loaded authoritative slot rows behind an
   *                            Open (available) band when it is activated.
   *                            This module makes no request and applies no
   *                            policy; everything after the click is the
   *                            orchestrator's.
   * Returns: { buildGrid, appointmentDetailFields }
   * Failures: throws on a wiring mistake by the caller (a programming error,
   *   never a user-facing state).
   */
  function createMiaPortalCalendar(deps) {
    if (!deps || !deps.documentRef ||
        typeof deps.formatInTimeZone !== "function" ||
        typeof deps.scheduleSlotStatusLabel !== "function" ||
        typeof deps.appointmentStatusLabel !== "function" ||
        typeof deps.notificationOutcomeLabel !== "function" ||
        typeof deps.shiftLocalDay !== "function" ||
        typeof deps.onAppointmentSelect !== "function" ||
        typeof deps.onOpenBandSelect !== "function") {
      throw new Error("createMiaPortalCalendar: missing injected dependencies");
    }
    var doc = deps.documentRef;
    var formatInTimeZone = deps.formatInTimeZone;
    var scheduleSlotStatusLabel = deps.scheduleSlotStatusLabel;
    var appointmentStatusLabel = deps.appointmentStatusLabel;
    var notificationOutcomeLabel = deps.notificationOutcomeLabel;
    var shiftLocalDay = deps.shiftLocalDay;
    var onAppointmentSelect = deps.onAppointmentSelect;
    var onOpenBandSelect = deps.onOpenBandSelect;

    /* Render-local registry of the appointment blocks in the CURRENT grid,
     * paired with the row each one was drawn from. It exists only so the
     * open detail panel can keep its own block visibly selected.
     *
     * This is FRONTEND SELECTION STATE ONLY: it is discarded and rebuilt by
     * every buildGrid call, it holds no authority, and it can neither
     * change nor infer anything about slots, appointments or eligibility. */
    var renderedBlocks = [];

    function span(className, text) {
      var element = doc.createElement("span");
      element.className = className;
      element.textContent = text;
      return element;
    }

    /* One consolidated availability / held / blocked band. The status WORD is
     * always rendered, so the distinction is never carried by colour alone
     * (accessibility). The slot count states plainly how many authoritative
     * rows the band covers - it is a summary of them, not a replacement. */
    function buildBand(band, firstHour) {
      /* SLICE 3: an OPEN (available) band is the receptionist's entry point
       * to booking, so it renders as a real button and, when activated,
       * hands the ALREADY-LOADED authoritative slot rows to the injected
       * callback. Held and blocked bands stay inert regions. Nothing is
       * decided here: which rows exist came from the server response, and
       * everything after the click is owned by the orchestrator. */
      var interactive = band.status === "available";
      var element = doc.createElement(interactive ? "button" : "div");
      if (interactive) {
        element.type = "button";
      }
      element.className = "portal-calendar-band portal-calendar-band-" +
        band.status;
      var box = geometryFor(band.startMinutes, band.endMinutes, firstHour);
      element.style.top = box.top + "px";
      element.style.height = box.height + "px";
      /* The resting calendar prints the STATUS WORD only. Repeating a
       * slot count on every band ("Open (10)", "Open (12)") was pure
       * noise competing with the appointments that actually need
       * attention. The count is not lost: consolidation still tracks it
       * exactly, it remains on the band model for any caller, and it is
       * surfaced on the hover title - it simply is not shouted in the
       * default view. */
      var label = scheduleSlotStatusLabel(band.status);
      element.appendChild(span("portal-calendar-band-label", label));
      element.title = band.slotCount > 1
        ? label + " from " + band.caption + " (" + band.slotCount + " slots)"
        : label + " from " + band.caption;
      if (interactive) {
        element.addEventListener("click", function () {
          /* A defensive copy: the caller may sort or annotate for display,
           * and the band model must stay exactly what consolidation
           * computed from the server rows. */
          onOpenBandSelect(band.slots.slice());
        });
      }
      return element;
    }

    /* One compact appointment block: start time, patient name, service or
     * reason, plus a short status word. The fully-qualified date/timezone
     * rendering deliberately does NOT appear here - it belongs once at
     * calendar level and in the detail drawer. */
    function buildAppointmentBlock(entry, firstHour) {
      var element = doc.createElement("button");
      element.type = "button";
      var compact = entry.durationMinutes < COMPACT_BLOCK_MINUTES;
      /* A history block is DEMOTED: it never joins a lane, never shows the
       * service line, and carries its own class so the styling can subdue
       * it without touching any active treatment. */
      var historical = entry.historical === true;
      /* A laned block is only a fraction of the column wide, so it drops
       * the secondary line and truncates with an ellipsis rather than
       * wrapping into unreadable fragments. The patient name is the
       * highest-priority line and is always kept; the complete record is
       * one click away in the detail panel, and the title carries it on
       * hover. The grid is never widened to fit text. */
      var narrow = !historical && entry.laneCount > 1;
      element.className = "portal-calendar-block portal-calendar-appointment-" +
        entry.status + (compact ? " portal-calendar-block-compact" : "") +
        (narrow ? " portal-calendar-block-narrow" : "") +
        (historical ? " portal-calendar-block-history" : "");
      var box = geometryFor(entry.startMinutes, entry.endMinutes, firstHour);
      element.style.top = box.top + "px";
      element.style.height = box.height + "px";
      /* History spans the full column: it took no part in the active lane
       * assignment, so it has no lane of its own to occupy. */
      var laneCount = (!historical && entry.laneCount > 0)
        ? entry.laneCount : 1;
      var lane = historical ? 0 : entry.lane;
      var width = 100 / laneCount;
      element.style.left = (lane * width) + "%";
      element.style.width = width + "%";

      var statusWord = appointmentStatusLabel(entry.status);
      var name = (entry.payload && entry.payload.patient_name) || "";
      var service = (entry.payload && entry.payload.reason) || "";

      element.appendChild(span("portal-calendar-block-time", entry.caption));
      element.appendChild(span("portal-calendar-block-name", name));
      /* History omits the service line - the drawer carries the full
       * record, and less ink is exactly the point. */
      if (!compact && !historical && service !== "") {
        element.appendChild(span("portal-calendar-block-service", service));
      }
      /* The status word ships in the markup on every block, so the visual
       * treatment is never the only carrier of meaning. */
      element.appendChild(span("portal-calendar-block-status", statusWord));
      /* The hover title carries everything a truncated block had to drop,
       * so nothing is only-visible-if-it-fits. */
      element.title = entry.caption + " " + statusWord +
        (name === "" ? "" : " - " + name) +
        (service === "" ? "" : " - " + service);

      element.addEventListener("click", function () {
        onAppointmentSelect(entry.payload);
      });
      renderedBlocks.push({ payload: entry.payload, element: element });
      return element;
    }

    function buildHourRail(window) {
      var rail = doc.createElement("div");
      rail.className = "portal-calendar-rail";
      var spacer = doc.createElement("div");
      spacer.className = "portal-calendar-rail-corner";
      rail.appendChild(spacer);
      for (var hour = window.firstHour; hour < window.lastHour; hour++) {
        var cell = doc.createElement("div");
        cell.className = "portal-calendar-hour";
        cell.style.height = HOUR_HEIGHT_PX + "px";
        cell.appendChild(span("portal-calendar-hour-label", hourCaption(hour)));
        rail.appendChild(cell);
      }
      return rail;
    }

    /*
     * Purpose: the HISTORY STRIP - a compact horizontal badge drawn ABOVE the
     * live appointments, keeping a cancelled appointment reachable when a
     * live one has rendered its ghost unreadable.
     *
     * Why it exists: history is deliberately painted BENEATH live work, so a
     * live appointment covering the ghost's first line leaves it neither
     * readable nor clickable, and the office loses the follow-up context this
     * phase exists to keep. Insetting the history layer cannot help - that
     * whole layer sits below.
     *
     * Why it is only sometimes drawn: when the ghost still shows a readable
     * top of its own, it IS the affordance and nothing is added. A second
     * control beside a perfectly visible ghost is clutter.
     *
     * Why it sits on the BOTTOM edge: a live block renders its time and
     * patient on its first lines, so a thin strip along the bottom covers
     * only its least important line. The live appointment stays dominant and
     * no padding has to be reserved on blocks that have no history at all.
     *
     * It is a real button: keyboard reachable, opening exactly the same
     * read-only cancelled drawer the ghost opens. Patient, status word and
     * original time are all present in its text and on the hover title.
     */
    function buildHistoryStrip(entry, firstHour) {
      var element = doc.createElement("button");
      element.type = "button";
      element.className = "portal-calendar-history-strip";
      var box = geometryFor(entry.startMinutes, entry.endMinutes, firstHour);
      var top = box.top + box.height - HISTORY_STRIP_HEIGHT_PX;
      if (top < box.top) {
        top = box.top;
      }
      element.style.top = top + "px";
      element.style.height = HISTORY_STRIP_HEIGHT_PX + "px";

      var statusWord = appointmentStatusLabel(entry.status);
      var name = (entry.payload && entry.payload.patient_name) || "";
      element.appendChild(span("portal-calendar-history-strip-name", name));
      element.appendChild(span("portal-calendar-history-strip-status",
        statusWord));
      /* Visually clipped, so the strip stays compact while the ORIGINAL time
       * remains part of the control's accessible name. */
      element.appendChild(span("portal-calendar-history-strip-time",
        "at " + entry.caption));
      element.title = statusWord + (name === "" ? "" : " - " + name) +
        " at " + entry.caption;

      element.addEventListener("click", function () {
        onAppointmentSelect(entry.payload);
      });
      renderedBlocks.push({ payload: entry.payload, element: element });
      return element;
    }

    function buildColumn(dayText, dayEntries, window) {
      var column = doc.createElement("div");
      column.className = "portal-calendar-col";

      var head = doc.createElement("div");
      head.className = "portal-calendar-dayhead";
      var parts = dayHeaderParts(dayText);
      head.appendChild(span("portal-calendar-dayhead-weekday", parts.weekday));
      head.appendChild(span("portal-calendar-dayhead-date", parts.date));
      column.appendChild(head);

      var canvas = doc.createElement("div");
      canvas.className = "portal-calendar-canvas";
      canvas.style.height =
        ((window.lastHour - window.firstHour) * HOUR_HEIGHT_PX) + "px";

      /* Hour lines first, so everything else layers above them. */
      var lines = doc.createElement("div");
      lines.className = "portal-calendar-lines";
      for (var hour = window.firstHour; hour < window.lastHour; hour++) {
        var line = doc.createElement("div");
        line.className = "portal-calendar-line";
        line.style.height = HOUR_HEIGHT_PX + "px";
        lines.appendChild(line);
      }
      canvas.appendChild(lines);

      var slotEntries = [];
      var activeEntries = [];
      var historyEntries = [];
      for (var i = 0; i < dayEntries.length; i++) {
        if (dayEntries[i].kind !== KIND_APPOINTMENT) {
          slotEntries.push(dayEntries[i]);
        } else if (dayEntries[i].historical) {
          historyEntries.push(dayEntries[i]);
        } else {
          activeEntries.push(dayEntries[i]);
        }
      }

      var bandLayer = doc.createElement("div");
      bandLayer.className = "portal-calendar-bands";
      var bands = consolidateBands(slotEntries);
      for (var b = 0; b < bands.length; b++) {
        bandLayer.appendChild(buildBand(bands[b], window.firstHour));
      }
      canvas.appendChild(bandLayer);

      /* History sits in its OWN layer, between the availability bands and
       * the active appointments. Two consequences, both deliberate:
       *   - an active appointment always paints ON TOP of a cancelled one,
       *     so the office is never asked which of two patients is real;
       *   - history is excluded from assignLanes entirely, so a cancelled
       *     row can never squeeze a live Pending or Confirmed appointment
       *     into a half-width lane. */
      var historyLayer = doc.createElement("div");
      historyLayer.className = "portal-calendar-history";
      var orderedHistory = historyEntries.slice().sort(function (l, r) {
        return l.startMinutes - r.startMinutes;
      });
      for (var h = 0; h < orderedHistory.length; h++) {
        /* SLICE 4C (Bug A): a ghost that overlaps an OPEN band is demoted
         * to the compact strip ONLY (built in the strips layer below), so
         * the Open band's button keeps a full, deterministic hit area and
         * the cancelled history stays visible AND clickable on its own
         * thin strip. Ghosts over non-open time render exactly as before. */
        if (overlapsAnyAvailableBand(orderedHistory[h], bands)) {
          continue;
        }
        historyLayer.appendChild(buildAppointmentBlock(orderedHistory[h],
          window.firstHour));
      }
      canvas.appendChild(historyLayer);

      var blockLayer = doc.createElement("div");
      blockLayer.className = "portal-calendar-blocks";
      var laned = assignLanes(activeEntries);
      for (var a = 0; a < laned.length; a++) {
        blockLayer.appendChild(buildAppointmentBlock(laned[a],
          window.firstHour));
      }
      canvas.appendChild(blockLayer);

      /* The ONLY thing drawn above the live appointments, and ONLY for the
       * ghosts that a live appointment has actually rendered unreadable.
       * A ghost that still shows a readable top - overlapped or not - speaks
       * for itself and gets nothing extra. */
      var stripLayer = doc.createElement("div");
      stripLayer.className = "portal-calendar-history-strips";
      for (var m = 0; m < orderedHistory.length; m++) {
        /* SLICE 4C (Bug A): a strip is drawn for a ghost demoted off an
         * OPEN band (its only remaining affordance) as well as for the
         * existing case of a ghost a live appointment rendered unreadable.
         * The two conditions are OR-ed over ONE loop, so an entry can never
         * receive two strips. */
        if (overlapsAnyAvailableBand(orderedHistory[m], bands) ||
            isHistoryOccluded(orderedHistory[m], activeEntries)) {
          stripLayer.appendChild(buildHistoryStrip(orderedHistory[m],
            window.firstHour));
        }
      }
      canvas.appendChild(stripLayer);

      column.appendChild(canvas);
      return column;
    }

    return {
      /*
       * Purpose: build the detached time-axis week element from the two
       * authoritative responses.
       * Returns (closed shape):
       *   { ok: true,  reason: "ok", element, dayCount, visibleCount,
       *     outsideCount, bandCount, appointmentCount, firstHour, lastHour }
       *   { ok: false, reason: "range_mismatch" | "range_unusable", ... }
       * The caller renders NOTHING on ok:false (fail closed).
       * Network effects: none. This module performs no I/O.
       */
      buildGrid: function (scheduleBody, appointmentsBody) {
        var refusal = {
          ok: false, reason: REASON_RANGE_MISMATCH, element: null,
          dayCount: 0, visibleCount: 0, outsideCount: 0,
          bandCount: 0, appointmentCount: 0, historyCount: 0,
          firstHour: 0, lastHour: 0
        };
        if (!rangesAgree(scheduleBody, appointmentsBody)) {
          renderedBlocks = [];
          return refusal;
        }
        /* Every build replaces the rendered blocks, so the selection
         * registry is discarded here - a stale element can never keep a
         * selected look after a re-render, week change or reload. */
        renderedBlocks = [];
        var timeZone = scheduleBody.timezone_name;
        var columns = dayColumns(scheduleBody.start_day, scheduleBody.end_day,
          shiftLocalDay);
        if (columns.length === 0) {
          refusal.reason = REASON_RANGE_UNUSABLE;
          return refusal;
        }

        var byDay = {};
        for (var c = 0; c < columns.length; c++) {
          byDay[columns[c]] = [];
        }
        var entries = collectEntries(scheduleBody, appointmentsBody, timeZone);
        var outsideCount = 0;
        var visibleCount = 0;
        var kept = [];
        for (var i = 0; i < entries.length; i++) {
          if (!Object.prototype.hasOwnProperty.call(byDay, entries[i].day)) {
            /* The backend should not return rows outside the window it
             * echoed. If it ever does, COUNT them - never drop silently. */
            outsideCount += 1;
            continue;
          }
          byDay[entries[i].day].push(entries[i]);
          kept.push(entries[i]);
          visibleCount += 1;
        }
        var window = hourWindow(kept);

        var grid = doc.createElement("div");
        grid.className = "portal-calendar-grid-inner";
        var frame = doc.createElement("div");
        frame.className = "portal-calendar-frame";
        frame.appendChild(buildHourRail(window));

        var days = doc.createElement("div");
        days.className = "portal-calendar-days";
        var bandCount = 0;
        var appointmentCount = 0;
        var historyCount = 0;
        for (var d = 0; d < columns.length; d++) {
          var dayEntries = byDay[columns[d]];
          days.appendChild(buildColumn(columns[d], dayEntries, window));
          for (var e = 0; e < dayEntries.length; e++) {
            if (dayEntries[e].kind === KIND_APPOINTMENT) {
              appointmentCount += 1;
              if (dayEntries[e].historical) {
                historyCount += 1;
              }
            }
          }
          bandCount += consolidateBands(dayEntries.filter(function (entry) {
            return entry.kind === KIND_SLOT;
          })).length;
        }
        frame.appendChild(days);
        grid.appendChild(frame);

        return {
          ok: true, reason: REASON_OK, element: grid,
          dayCount: columns.length,
          visibleCount: visibleCount,
          outsideCount: outsideCount,
          bandCount: bandCount,
          appointmentCount: appointmentCount,
          historyCount: historyCount,
          firstHour: window.firstHour,
          lastHour: window.lastHour
        };
      },

      /*
       * Purpose: mark exactly one rendered appointment block as selected,
       * so the receptionist can see which block the open detail panel
       * belongs to. Passing null (or a row that is not on screen) clears
       * the selection entirely.
       * Matching is by OBJECT IDENTITY against the row the block was drawn
       * from, so it needs no id field and cannot mis-select a lookalike.
       * This changes appearance only: no authoritative state, no request,
       * no mutation.
       * Returns: the number of blocks now selected (0 or 1).
       */
      applySelection: function (appointment) {
        var selected = 0;
        for (var i = 0; i < renderedBlocks.length; i++) {
          var isMatch = appointment !== null && appointment !== undefined &&
            renderedBlocks[i].payload === appointment;
          renderedBlocks[i].element.classList.toggle(
            "portal-calendar-block-selected", isMatch);
          if (isMatch) {
            selected += 1;
          }
        }
        return selected;
      },

      /*
       * Purpose: the READ-ONLY detail field list for one already-loaded
       * appointment row. Every value comes from the response the calendar
       * ALREADY holds - opening the drawer makes no request of any kind.
       * The fully-qualified date/time rendering (injected formatInTimeZone)
       * lives here, where there is room for it.
       * Returns: [{ label, value }], values already display-safe strings.
       */
      appointmentDetailFields: function (appointment, timeZone, missingText) {
        var absent = typeof missingText === "string" ? missingText : "";
        function present(value) {
          return (typeof value === "string" && value !== "") ? value : absent;
        }
        if (!appointment || typeof appointment !== "object") {
          return [];
        }
        var fields = [
          { label: "Patient", value: present(appointment.patient_name) },
          { label: "Phone", value: present(appointment.patient_phone) },
          { label: "Email", value: present(appointment.patient_email) },
          { label: "Service", value: present(appointment.reason) },
          { label: "Patient type", value: present(appointment.new_or_returning) },
          { label: "Urgency", value: present(appointment.urgency) },
          { label: "Starts",
            value: formatInTimeZone(appointment.start_datetime, timeZone) },
          { label: "Ends",
            value: formatInTimeZone(appointment.end_datetime, timeZone) },
          { label: "Status",
            value: appointmentStatusLabel(appointment.status) }
        ];
        /* SLICE 4A: the receptionist reads WHO booked, not a storage enum.
         * portal_staff is the ONE source with an owner-approved human
         * label; the stored value is untouched (projection only) and every
         * other source keeps its existing raw display unchanged. */
        var staffCreated = appointment.source === "portal_staff";
        fields.push(staffCreated
          ? { label: "Booked by", value: "Office staff" }
          : { label: "Source", value: present(appointment.source) });
        /* confirmed_at is null for an appointment that was never STAFF
         * confirmed - including one auto-confirmed by the backend - so it is
         * reported as its own fact and never used to infer the status.
         * SLICE 4A: a staff-created appointment is born CONFIRMED with
         * confirmed_at deliberately NULL and sends NO booking notification
         * (both frozen backend contracts), so for portal_staff these two
         * rows could only ever read as permanent "missing"/"pending" noise
         * - they are hidden for that one source, per the owner-approved
         * direction. Every other source keeps both rows exactly as before,
         * including visible failures (Rule 16). If a future slice ever
         * writes confirmed_at or sends notifications for staff bookings,
         * this projection must be revisited in that slice's contract. */
        if (!staffCreated) {
          fields.push({
            label: "Staff confirmed",
            value: (typeof appointment.confirmed_at === "string" &&
              appointment.confirmed_at !== "")
              ? formatInTimeZone(appointment.confirmed_at, timeZone)
              : absent
          });
          fields.push({
            label: "Office notification",
            value: notificationOutcomeLabel(appointment.notification_outcome)
          });
        }
        return fields;
      }
    };
  }

  /* Pure helpers exported for the Node suite (no DOM required), matching the
   * createMiaPortalPages.helpers convention. */
  createMiaPortalCalendar.helpers = {
    isRealCalendarDay: isRealCalendarDay,
    dayColumns: dayColumns,
    dayHeaderParts: dayHeaderParts,
    localParts: localParts,
    localDayOf: localDayOf,
    clockCaption: clockCaption,
    hourCaption: hourCaption,
    isVisibleSlotStatus: isVisibleSlotStatus,
    isVisibleAppointmentStatus: isVisibleAppointmentStatus,
    isActiveAppointmentStatus: isActiveAppointmentStatus,
    isHistoryAppointmentStatus: isHistoryAppointmentStatus,
    rangesAgree: rangesAgree,
    positionEntry: positionEntry,
    collectEntries: collectEntries,
    consolidateBands: consolidateBands,
    overlapsAnyActive: overlapsAnyActive,
    largestVisibleSpanMinutes: largestVisibleSpanMinutes,
    isHistoryOccluded: isHistoryOccluded,
    assignLanes: assignLanes,
    hourWindow: hourWindow,
    geometryFor: geometryFor,
    MAX_DAY_COLUMNS: MAX_DAY_COLUMNS,
    HOUR_HEIGHT_PX: HOUR_HEIGHT_PX,
    MIN_BLOCK_MINUTES: MIN_BLOCK_MINUTES,
    COMPACT_BLOCK_MINUTES: COMPACT_BLOCK_MINUTES,
    DEFAULT_FIRST_HOUR: DEFAULT_FIRST_HOUR,
    DEFAULT_LAST_HOUR: DEFAULT_LAST_HOUR,
    VISIBLE_SLOT_STATUSES: VISIBLE_SLOT_STATUSES,
    VISIBLE_APPOINTMENT_STATUSES: VISIBLE_APPOINTMENT_STATUSES,
    ACTIVE_APPOINTMENT_STATUSES: ACTIVE_APPOINTMENT_STATUSES,
    HISTORY_APPOINTMENT_STATUSES: HISTORY_APPOINTMENT_STATUSES
  };

  /* Export for both the browser (window) and the Node test harness. */
  globalScope.createMiaPortalCalendar = createMiaPortalCalendar;

}(typeof window !== "undefined" ? window : this));
