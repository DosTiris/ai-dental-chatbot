# app/services/booking_conversation.py
#
# OWNER OF: the booking dialog state machine (Rule 14). chat.py calls exactly
# one function here — handle_booking_message — and contains NO availability,
# hold, booking, or state-transition logic of its own (Rule 2).
#
# WHAT THIS MODULE ASSUMES (contract with chat.py):
#   - chat.py's medical-safety guard already ran for this message. As defense
#     in depth, this module STILL refuses to book emergency-flagged
#     conversations and clears its own state (Rule 10: "Can it bypass
#     emergency rules?" — no, two independent layers say no).
#   - Patient identity (name/phone) was collected by Mia's EXISTING intake
#     and lives on conversation.lead_name / lead_phone. This module never
#     re-collects it — intake has one owner and it is not the calendar
#     (Rule 3). If intake is incomplete, we return handled=False and chat.py's
#     intake flow proceeds as before.
#   - Every reply asks AT MOST one question (Mia's one-question rule).
#
# TRANSACTION HAZARD (documented per Rule 4): the hold/booking services
# commit or roll back the SHARED session. Therefore every handler below calls
# services FIRST and mutates conversation.booking_* fields AFTER, committing
# state at the end. Mutating state before a service call would let a service
# rollback silently erase it.
#
# The valid states and the complete transition table live in
# calendar_models.py next to the BookingState names.

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.calendar_models import BookingState, SlotStatus
from app.models import Conversation
# C2-A.2: the visual date picker's server-side revalidation reuses the ONE
# availability-preview owner (B1) — never a second availability computation.
from pydantic import ValidationError as PydanticValidationError
from app.schemas import AvailabilityPreviewRequest
from app.services.availability_preview_service import build_availability_preview

# PATCH 2C (Senior Audit Critical #8): how long a DISPLAYED slot menu stays
# usable before Mia refreshes it. Fixed for the MVP (not per-office) and
# owned HERE because this module owns the offer lifecycle. 30 minutes = 6x
# the 5-minute confirm-step hold: choosing among <=3 displayed times is a
# minutes-scale decision, and the TTL bounds CONVERSATION staleness
# (preference/service/settings drift) — clock rules (notice/horizon) are
# additionally re-judged live under the slot lock at hold and finalization.
BOOKING_OFFER_TTL_MINUTES = 30
from app.repositories import appointment_repository
from app.services import (
    appointment_hold_service,
    availability_service,
    booking_service,
    notification_service,
)
from app.services.appointment_intent import (
    PREF_ANY,
    match_slot_selection,
    parse_preferred_date,
    parse_time_preference,
    parse_yes_no,
)
from app.services.calendar_settings_service import (
    CalendarSettings,
    client_now,
    ensure_utc,
    load_calendar_settings,
)


@dataclass
class BookingReply:
    """What chat.py gets back. handled=False means 'not my message'."""
    handled: bool
    text: str = ""
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# C1-C structured Calendar actions — vocabulary, outcome type, and choice
# builders. This module is the SINGLE owner (Rule 3) of choice issuance and
# resolution; the browser submits only the opaque choice_id and is never
# authoritative for transition meaning, slots, labels, dates, or times.
# ---------------------------------------------------------------------------

# Wire action type. MUST equal the Literal in app/schemas.ChatAction
# ("calendar_choice"); a sync test in calendar_tests/
# test_chat_action_execution.py pins the equality (same pattern as the
# PreviewDay day-state sync test).
CALENDAR_CHOICE_ACTION_TYPE = "calendar_choice"

# Server-issued confirmation choice prefixes. The suffix is ALWAYS the
# selected-slot UUID persisted on the conversation; resolution happens HERE
# against booking_state + booking_selected_slot_id only (locked decision 3).
CONFIRM_YES_CHOICE_PREFIX = "confirm-yes:"
CONFIRM_NO_CHOICE_PREFIX = "confirm-no:"

# C2-A.2 (visual date picker): the server-DEFINED date-stage choice prefix.
# The suffix is the patient-visible ISO office-local date (YYYY-MM-DD) — a
# piece of patient data, NEVER a backend identifier — and it is resolved
# ONLY at WAITING_FOR_DATE, only while all three feature gates are strict
# true, and only after this module re-validates the date as genuinely open
# through the ONE availability-preview owner. The browser stays a display
# surface: it can propose a date, never book one.
DATE_SELECT_CHOICE_PREFIX = "pick-date:"

# UX-A (slot pagination): the two server-DEFINED slot-page navigation choice
# ids. Fixed literals - deliberately NOT UUID-shaped and NOT prefixed forms
# of any persisted identifier - so a navigation tap can never be mistaken
# for a slot selection (a UUID parse of either raises). They resolve ONLY at
# WAITING_FOR_SLOT_SELECTION, BEFORE the offered-membership check, and a
# navigation NEVER places a hold, NEVER books, NEVER notifies: it rewrites
# only the Patch 2C offer trio with a freshly REVALIDATED adjacent page.
# Paging stays server-authoritative - hidden later slot ids are never
# pre-sent to the browser; each page is recomputed from live availability.
SLOTS_LATER_CHOICE_ID = "slots-later"
SLOTS_EARLIER_CHOICE_ID = "slots-earlier"
SLOT_NAV_CHOICE_IDS = frozenset({SLOTS_LATER_CHOICE_ID, SLOTS_EARLIER_CHOICE_ID})
# Exact patient-facing labels (owner contract - never shortened to "Back").
SLOTS_LATER_LABEL = "See later times"
SLOTS_EARLIER_LABEL = "Back to earlier times"

# C2-A.3 (visual time stages): the server-defined stage-signal vocabulary
# for the EXISTING meta.calendar_picker channel introduced by C2-A.2.
# "date" (owned by _date_stage_meta, untouched) tells the widget to render
# the visual date picker; these two values tell it to render the visual
# time-preference row and the visual exact-slot panel. Old widgets ignore
# unknown stage values (locked C2-A.2 decision D-2), so both are additive:
# no already-deployed widget changes behavior.
PICKER_STAGE_TIME_PREFERENCE = "time_preference"
PICKER_STAGE_SLOT_SELECTION = "slot_selection"
# The date stage value, matching the vocabulary _date_stage_meta emits for
# the native WAITING_FOR_DATE meta. Named here so the route-level
# capture-first intake date-signal owner (intake_date_stage_signal) reuses
# the SAME closed vocabulary and the SAME triple gate as every other
# C2-A.3 signal.
PICKER_STAGE_DATE = "date"
# Closed capture-first submission mode (Rule 4). The native
# WAITING_FOR_DATE picker consumes a pick-date calendar_choice action;
# the capture-first intake picker is emitted at a route state that is
# NOT WAITING_FOR_DATE, so its date cards must submit an ORDINARY intake
# message (no ChatAction). This marker tells the widget which submission
# transport to use; the widget validates it fail-closed.
INTAKE_SUBMIT_MODE_MESSAGE = "message"


def _picker_stage_signal(settings, stage) -> Optional[dict]:
    """
    Purpose: single owner (Rule 3) of the C2-A.3 time-stage gate — the ONE
             place that decides whether a visual time-stage signal may be
             emitted, mirroring _date_stage_meta's strict triple gate.
    Inputs:  settings — the request-level CalendarSettings snapshot;
             stage — PICKER_STAGE_TIME_PREFERENCE or
             PICKER_STAGE_SLOT_SELECTION.
    Returns: {"stage": stage} when booking_enabled, calendar_actions_enabled,
             AND calendar_picker_enabled are ALL strict True; otherwise None,
             and the caller attaches nothing — the reply meta stays
             byte-identical to the pre-C2-A.3 text-only behavior.
    Database effects: none. External effects: none.
    """
    if (settings.booking_enabled is True
            and settings.calendar_actions_enabled is True
            and settings.calendar_picker_enabled is True):
        return {"stage": stage}
    return None


# C2-A.3 intake-gap fix: the ROUTE-level intake time-window path asks the
# SAME "morning or afternoon" question BEFORE the native booking state
# machine takes ownership, and production showed its response carried no
# calendar_picker signal, so the widget could not render the preference
# buttons. This constant is byte-identical to the day-only follow-up
# prompt returned by app/routes/chat.py, which imports THIS constant at
# every site (single wording owner; the raw literal no longer exists in
# chat.py - a structural test enforces that). Exact equality against the
# route-state proof: a weekday rejection, past-time correction,
# outside-hours correction, emergency reply, completion rewrite, or any
# other intake prompt produces different text and therefore never
# carries the signal.
INTAKE_TIME_PREFERENCE_PROMPT = "Got it — do you prefer morning or afternoon?"
# Same-day/today variant (Finding 2). The same-day intake owner returns
# this exact wording; centralizing it here keeps a single source of truth
# so the time-preference signal recognizes it as a closed prompt kind.
INTAKE_TIME_PREFERENCE_TODAY_PROMPT = "Got it — do you prefer today morning or afternoon?"


def intake_time_preference_stage_signal(client, reply_text) -> Optional[dict]:
    """
    Purpose: single owner (Rule 3) of the C2-A.3 stage-signal decision
             for the route-level intake time-window path. chat.py merges
             the returned dict into its existing
             intake_time_window_capture meta; the route never
             re-implements the gate, the vocabulary, or Boolean parsing.
    Inputs:  client - the tenant row (calendar settings are loaded here
             so the route stays thin); reply_text - the FINAL reply text
             about to be returned for this turn.
    Returns: {"stage": "time_preference"} only when reply_text is
             exactly the intake preference prompt AND booking_enabled,
             calendar_actions_enabled, and calendar_picker_enabled are
             ALL strict True - the same triple gate as every other
             C2-A.3 signal, decided by the same owner
             (_picker_stage_signal). Otherwise None, and the intake meta
             stays byte-identical to today.
    Database effects: none. External effects: none.
    """
    # Recognize BOTH the generic and the same-day/today time-preference
    # prompt constants (Finding 2). Exact-constant match only — never a
    # "morning or afternoon" substring — so the stage is proven by the
    # owner's emitted prompt, not by word inference.
    if reply_text not in (INTAKE_TIME_PREFERENCE_PROMPT, INTAKE_TIME_PREFERENCE_TODAY_PROMPT):
        return None
    settings = load_calendar_settings(client)
    return _picker_stage_signal(settings, PICKER_STAGE_TIME_PREFERENCE)


def calendar_intake_day_only_sufficient(client) -> bool:
    """
    Purpose: PACKAGE B — single owner (Rule 3) of the Calendar-tier
             decision behind chat.py's intake time-window SUFFICIENCY: for
             a Calendar tenant the exact-time slot offer replaces the
             part-of-day question, so a window naming a specific day is
             enough to complete intake and hand the conversation to the
             booking engine. chat.py owns the window-shape vocabulary
             (time_window_has_specific_day); this service owns the tier
             gate, exactly as intake_time_preference_stage_signal owns its
             gate — the route never re-implements capability parsing.
    Inputs:  client — the tenant row; calendar settings are loaded here so
             the route stays thin.
    Returns: True only when CalendarSettings.booking_enabled is STRICT
             True (the authoritative Calendar capability owner — never
             booking_mode, never the visual-surface flags). Any other
             value, including missing/garbage settings, returns False and
             Basic intake behavior applies unchanged.
    Database effects: none. External effects: none.
    Possible failures: none — load_calendar_settings already fails closed
             to booking_enabled=False on malformed settings.
    """
    return load_calendar_settings(client).booking_enabled is True


# The distinctive, name-INDEPENDENT tail of the S3 capture-first
# day/time-window prompt returned by receptionist_bypass_reply() in
# app/routes/chat.py. Only that standard prompt ends with this exact
# phrase: the short-symptom variant omits the "(e.g., Tue morning)"
# example, and the weekday and morning/afternoon prompts differ entirely.
# Matching on this tail identifies the capture-first day question
# regardless of the interpolated patient name WITHOUT re-implementing the
# S3 branch logic (Calendar policy stays in its owner) and WITHOUT
# refactoring that S3 owner (Rule 12: no refactor + feature in one patch).
# Documented duplication (Rule 3 note): the same phrase is present in the
# raw S3 f-string; a pinning test asserts the two stay byte-identical, and
# unifying them is recorded as deferred drift.
# UX-C: these two are now the BASIC-tier tails only. Calendar tenants
# (booking_enabled strict True — calendar_intake_day_only_sufficient)
# get the CALENDAR_* date-only tails below, and intake_date_stage_signal
# matches ONLY those — a Basic-worded reply can never carry the Calendar
# picker signal, and emitter drift fails closed (no signal).
INTAKE_DATE_WINDOW_PROMPT_TAIL = "What day/time window works best (e.g., Tue morning)?"
# Short-symptom variant (Finding 1): the short-symptom branch omits the
# "(e.g., Tue morning)" example, so its date-selection prompt ends here.
# Distinct suffix from the standard tail (which ends with the example),
# so the two kinds never cross-match.
INTAKE_DATE_WINDOW_SHORT_SYMPTOM_PROMPT_TAIL = "What day/time window works best?"

# UX-C — Calendar-tier date-only prompt tails. For Calendar tenants
# (calendar_intake_day_only_sufficient: booking_enabled strict True) the
# capture-first date question drops the time-window / part-of-day framing:
# Package B's exact-time slot offer replaces it, so the prompt asks for a
# DAY only. The wording reuses the native booking engine's house phrasing
# so both surfaces speak with one voice. The two tails are a non-suffix
# pair ("...for your appointment?" vs "...for you?"), so the two kinds can
# never cross-match via endswith. app/routes/chat.py interpolates THESE
# constants into its Calendar-branch f-strings (single source), so
# emitter/matcher drift cannot recur for the Calendar pair; the Basic pair
# above keeps its documented raw-literal duplication unchanged.
CALENDAR_INTAKE_DATE_ONLY_PROMPT_TAIL = "What day would work best for your appointment?"
# Short-symptom Calendar variant: same date-only rule, shorter frame.
CALENDAR_INTAKE_DATE_ONLY_SHORT_SYMPTOM_PROMPT_TAIL = "What day would work best for you?"


def intake_date_stage_signal(client, reply_text, entered_date_stage, date_stage_kind) -> Optional[dict]:
    """
    Purpose: single owner (Rule 3) of the C2-A.3 date-stage-signal decision
             for EVERY route-level capture-first (mode "bypass")
             day/time-window prompt — the STANDARD path and the
             SHORT-SYMPTOM path. chat.py merges the returned dict into its
             EXISTING bypass meta; the route never re-implements the gate,
             the vocabulary, or Boolean parsing. Analogous to
             intake_time_preference_stage_signal.
    Inputs:  client - tenant row (calendar settings loaded here so the
             route stays thin); reply_text - the FINAL bypass reply text
             about to be returned for this turn; entered_time_window_stage
             - a CLOSED FACT from the route: literal True only when THIS
             turn actually TRANSITIONED INTO the capture-first time_window
             stage (pre-turn bypass stage != time_window, post-turn ==
             time_window). The route establishes both stages with the
             existing bypass owner (receptionist_bypass_reply); this helper
             NEVER parses user_text and NEVER re-derives field order. It
             distinguishes ENTERING the stage (signal required) from an
             invalid/unusable date that merely RE-ASKS the identical prompt
             at the same stage (signal prohibited) - the two are
             byte-identical by reply text alone.
    Returns: {"stage": "date", "submit": "message"} only when
             entered_time_window_stage is literal True AND reply_text is the
             Calendar-tier capture-first DATE-ONLY prompt (identified by
             its name-independent CALENDAR_* tail; UX-C) AND booking_enabled,
             calendar_actions_enabled, and calendar_picker_enabled are ALL
             strict True - the same triple gate as every other C2-A.3
             signal, decided by the same owner (_picker_stage_signal).
             The submit marker tells the widget to submit the picked date
             as an ORDINARY intake message (the native _date_stage_meta
             omits it and keeps calendar_choice). Otherwise None, and the
             bypass meta stays byte-identical to today (mode, hours_hint,
             show_start_over, reply wording, and state progression
             unchanged).
    Database effects: none. External effects: none.
    """
    # Emit ONLY on a genuine transition INTO time_window (closed fact from
    # the route). An invalid date that re-asks the same prompt keeps the
    # stage unchanged and must stay signal-free.
    # entered_date_stage: closed fact from the route — True only when THIS
    # turn transitioned INTO a date-selection stage (the matching pure
    # predicate was False pre-turn, True post-turn). date_stage_kind: closed
    # enum (Rule 4) naming which authoritative stage was entered; each kind
    # maps to exactly one existing prompt tail. An invalid date that merely
    # re-asks the same prompt keeps entered_date_stage False -> no signal.
    if entered_date_stage is not True:
        return None
    # UX-C: the signal proves the CALENDAR wording. Its triple strict-True
    # gate (checked below) is a strict subset of booking_enabled, which is
    # exactly the emitter's Calendar-branch condition
    # (calendar_intake_day_only_sufficient) — so whenever this signal can
    # fire, receptionist_bypass_reply emitted the date-only tails. The old
    # Basic tails are deliberately NOT matched here: Basic tenants never
    # pass the gate, and a Basic-worded reply under Calendar gates would
    # indicate emitter drift that must fail closed (no signal).
    if date_stage_kind == "standard":
        tail = CALENDAR_INTAKE_DATE_ONLY_PROMPT_TAIL
    elif date_stage_kind == "short_symptom":
        tail = CALENDAR_INTAKE_DATE_ONLY_SHORT_SYMPTOM_PROMPT_TAIL
    else:
        # Unknown/closed-vocabulary kind: no signal (never guess).
        return None
    if not (reply_text or "").endswith(tail):
        return None
    settings = load_calendar_settings(client)
    if _picker_stage_signal(settings, PICKER_STAGE_DATE) is None:
        return None
    # Capture-first picker: the widget submits the picked date as an
    # ORDINARY intake message (this route state is not WAITING_FOR_DATE
    # and cannot consume a native pick-date calendar_choice). The submit
    # marker selects that closed transport; the native date meta
    # (_date_stage_meta) deliberately omits it and keeps calendar_choice.
    return {"stage": PICKER_STAGE_DATE, "submit": INTAKE_SUBMIT_MODE_MESSAGE}


# booking_boundary_state() return vocabulary (closed — Rule 4). The route
# and handle_booking_action BOTH call the helper (locked decision 6) so a
# state change between the two checks still lands on the correct boundary.
BOUNDARY_FINAL_CLOSED = "final_closed"
BOUNDARY_LOCKED = "locked"
BOUNDARY_SAFETY_BLOCKED = "safety_blocked"
BOUNDARY_NONE = "none"
ALL_BOUNDARY_STATES = {
    BOUNDARY_FINAL_CLOSED,
    BOUNDARY_LOCKED,
    BOUNDARY_SAFETY_BLOCKED,
    BOUNDARY_NONE,
}

# handle_booking_action outcome statuses (closed — Rule 4).
ACTION_EXECUTED = "executed"
ACTION_BOUNDARY = "boundary"
ACTION_NOT_ACTIVE = "not_active"
ACTION_STALE_CHOICE = "stale_choice"


@dataclass(frozen=True)
class ActionOutcome:
    """Explicit outcome of one structured Calendar action (C1-C).

    status vocabulary:
        executed     — a transition ran (or was idempotently restated);
                       reply and user_label are set. Transcript persistence
                       is the ROUTE's job, AFTER this returns, preserving
                       the clean-session notification entry contract
                       (locked decision 14).
        boundary     — a durable conversation boundary refused the action;
                       boundary carries booking_boundary_state's reason.
        not_active   — Calendar actions (or booking) are disabled for this
                       client; nothing beyond settings was read.
        stale_choice — the choice did not resolve against current server
                       state (forged / superseded / expired / cross-tenant
                       — indistinguishable by design). calendar_actions
                       carries the still-live replacement set when one
                       exists WITHOUT mutating state; otherwise None.
    """
    status: str
    boundary: str = BOUNDARY_NONE
    reply: Optional[BookingReply] = None
    user_label: Optional[str] = None
    calendar_actions: Optional[List[dict]] = None


def _confirm_choice_actions(selected_slot_id) -> List[dict]:
    """The two server-issued confirmation choices bound to the persisted
    selected slot. Labels are display text only; meaning lives server-side
    and is re-derived from booking_state + booking_selected_slot_id."""
    sid = str(selected_slot_id)
    return [
        {
            "label": "Yes — book it",
            "message": "Yes — book it",
            "action": {"type": CALENDAR_CHOICE_ACTION_TYPE,
                       "choice_id": CONFIRM_YES_CHOICE_PREFIX + sid},
        },
        {
            "label": "No — pick another time",
            "message": "No — pick another time",
            "action": {"type": CALENDAR_CHOICE_ACTION_TYPE,
                       "choice_id": CONFIRM_NO_CHOICE_PREFIX + sid},
        },
    ]


def _date_stage_meta(settings) -> dict:
    """The reply meta for EVERY response that leaves the conversation at
    WAITING_FOR_DATE (C2-A.2). When — and only when — booking_enabled,
    calendar_actions_enabled, and calendar_picker_enabled are ALL strict
    true, the meta additionally carries the visual-picker signal
    `calendar_picker: {"stage": "date"}`. Deliberately a NEW meta key and
    NOT a calendar_actions entry: the existing button renderer and every
    already-deployed widget ignore unknown meta keys, so old widgets keep
    the typed-date flow byte-identically (locked decision D-2)."""
    meta = {"mode": "booking", "state": BookingState.WAITING_FOR_DATE}
    if (settings.booking_enabled is True
            and settings.calendar_actions_enabled is True
            and settings.calendar_picker_enabled is True):
        meta["calendar_picker"] = {"stage": "date"}
    return meta


def _slot_choice_actions(slots, tz_name: str) -> List[dict]:
    """One tappable choice per offered slot, in display order; choice_id is
    the slot UUID (opaque to the browser, resolved here against the
    conversation's persisted offer — never trusted on its own)."""
    actions = []
    for s in slots:
        label = _fmt_time(s.start_datetime, tz_name)
        actions.append({
            "label": label,
            "message": label,
            "action": {"type": CALENDAR_CHOICE_ACTION_TYPE,
                       "choice_id": str(s.id)},
        })
    return actions


def _slot_nav_actions(has_earlier: bool, has_later: bool) -> List[dict]:
    """UX-A: the server-issued slot-page navigation choices for the CURRENT
    page - 'Back to earlier times' first, then 'See later times' (owner
    contract ordering). Issued ONLY alongside slot calendar_actions at
    WAITING_FOR_SLOT_SELECTION; the ids are the fixed non-UUID literals
    above and are navigation, never selection (no hold, no booking, no
    notification). Each flag reflects the FRESH eligible list, so a page
    that is truly first/last never advertises a direction it cannot take."""
    actions = []
    if has_earlier:
        actions.append({
            "label": SLOTS_EARLIER_LABEL,
            "message": SLOTS_EARLIER_LABEL,
            "action": {"type": CALENDAR_CHOICE_ACTION_TYPE,
                       "choice_id": SLOTS_EARLIER_CHOICE_ID},
        })
    if has_later:
        actions.append({
            "label": SLOTS_LATER_LABEL,
            "message": SLOTS_LATER_LABEL,
            "action": {"type": CALENDAR_CHOICE_ACTION_TYPE,
                       "choice_id": SLOTS_LATER_CHOICE_ID},
        })
    return actions


def _current_page_nav_actions(db, client, conversation, settings, now_utc,
                              page_rows):
    """
    Purpose: READ-ONLY truthful navigation for the CURRENT persisted page
             (round-2 audit F4). Authoritative restates and STALE_CHOICE
             replacements re-show the persisted page; this single owner
             restores the pagination controls that page truthfully has
             RIGHT NOW - so a later page always carries "Back to earlier
             times" and a middle page carries both directions - recomputed
             from CURRENT availability, never from the stored offer.
    Inputs:  live session, client row, conversation row, settings, now_utc,
             page_rows - the surviving persisted-page slot rows the caller
             is about to re-show (vanished rows already dropped).
    Returns: navigation action list from the _slot_nav_actions owner
             (possibly empty). Never slot chips, never hidden UUIDs.
    Database effects: none (availability SELECTs only). No conversation
        field, no hold, no appointment, and no notification is touched -
        the callers are non-mutating paths and stay that way.
    Possible failures: service/db errors propagate (Rule 4).
    """
    if not page_rows:
        return []
    day = _get_pref_date(conversation)
    if day is None:
        # Corrupt/missing stored date: never guess a fetch window for a
        # read-only decoration - simply omit the controls.
        return []
    eligible = availability_service.get_bookable_slots_for_day(
        db, client.id, settings, day,
        _revalidation_preference(conversation), now_utc,
        service_key=(conversation.lead_reason or None),
    )
    if not eligible:
        return []
    page_starts = [ensure_utc(r.start_datetime) for r in page_rows]
    first_start = min(page_starts)
    last_start = max(page_starts)
    # Directions are judged against CURRENT chronological availability
    # relative to the page being re-shown (the same start-time anchoring
    # rule _select_slot_page pages by).
    has_earlier = any(
        ensure_utc(s.start_datetime) < first_start for s in eligible
    )
    has_later = any(
        ensure_utc(s.start_datetime) > last_start for s in eligible
    )
    if not (has_earlier or has_later):
        return []
    return _slot_nav_actions(has_earlier, has_later)


# ---------------------------------------------------------------------------
# Formatting helpers (client-timezone display only; no logic).
# ---------------------------------------------------------------------------

def _fmt_day(d: date) -> str:
    """'Thursday, July 16' — no leading zero on the day."""
    return d.strftime("%A, %B %d").replace(" 0", " ")


def _fmt_day_list(days: Sequence[date]) -> str:
    """'Tuesday, July 28 and Wednesday, July 29' - readable day enumeration.

    Every label comes from _fmt_day, so date formatting keeps one owner.
    """
    labels = [_fmt_day(d) for d in days]
    if len(labels) <= 1:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _fmt_time(dt_utc: datetime, tz_name: str) -> str:
    """'1:30 PM' in the client's timezone."""
    local = ensure_utc(dt_utc).astimezone(ZoneInfo(tz_name))
    return local.strftime("%I:%M %p").lstrip("0")


def _slot_menu(slots: Sequence, tz_name: str) -> str:
    """'1) 10:00 AM  2) 1:30 PM  3) 3:45 PM' in display order."""
    parts = [
        f"{i + 1}) {_fmt_time(s.start_datetime, tz_name)}"
        for i, s in enumerate(slots)
    ]
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Conversation state accessors — the ONLY code that touches booking_* fields.
# ---------------------------------------------------------------------------

def _get_state(conversation) -> str:
    state = (getattr(conversation, "booking_state", None) or BookingState.NONE)
    return state if state in BookingState.ALL else BookingState.NONE


def _get_pref_date(conversation) -> Optional[date]:
    raw = getattr(conversation, "booking_preferred_date", None)
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None  # Corrupt value -> treated as unset; Mia re-asks the day.


def _get_offered_ids(conversation) -> List[str]:
    raw = getattr(conversation, "booking_offered_slot_ids", None)
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):  # SQLite test compat: JSON stored as text.
        try:
            return [str(x) for x in json.loads(raw)]
        except (ValueError, TypeError):
            return []
    return []


def _clear_booking_state(conversation) -> None:
    """Reset to NONE and wipe every flow field (Rule 14: state is cleared
    after completion, cancellation, or expiration — never left dangling)."""
    conversation.booking_state = BookingState.NONE
    conversation.booking_preferred_date = None
    conversation.booking_time_preference = None
    conversation.booking_offered_slot_ids = None
    conversation.booking_selected_slot_id = None
    conversation.booking_offer_expires_at = None            # Patch 2C
    conversation.booking_effective_time_preference = None   # Patch 2C


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handle_booking_message(
    db: Session,
    client,
    conversation,
    user_text: str,
    *,
    information_interruption: bool = False,
) -> BookingReply:
    """
    Purpose: Advance the booking dialog by exactly one step.
    Inputs:  live session, client row, conversation row, raw patient message.
             information_interruption (keyword-only, PATCH 3, default False):
             chat.py sets this True when its EXISTING office-information
             detectors (hours / location / phone / insurance / pricing / services /
             "I have a question") classified the message. The dialog then
             yields WITHOUT touching state so chat.py's information paths
             can answer; the next scheduling message resumes this exact
             state. The default False keeps every pre-Patch-3 call site
             byte-identical in behavior.
    Returns: BookingReply. handled=False means chat.py should run its normal
             flow (booking disabled, intake incomplete, emergency, or an
             information interruption).
    Database effects: at most one hold/booking transaction (via services)
        plus one conversation-state commit; see module header for ordering.
    Settings visibility (Patch 2C, documented precisely): settings are
        loaded as a fresh request-level snapshot at the beginning of each
        patient message. Patch 2C does not lock the client row or guarantee
        visibility of an admin edit occurring after that read but before
        the slot-row lock. Settings and slot state are NOT one atomic
        database snapshot; revalidation under the slot lock bounds
        staleness to a single message's handling, not to the offer's age.
    External effects: notifications fire only on the single
        WAITING_FOR_CONFIRMATION -> BOOKED transition.
    Possible failures: service/db exceptions propagate to chat.py's logged
        boundary — this module adds no broad except (Rule 4).
    """
    settings = load_calendar_settings(client)
    if not settings.booking_enabled:
        return BookingReply(handled=False)

    # Defense in depth: never book an emergency conversation, and drop any
    # in-progress booking state so a later message can't resume it.
    # PATCH 3 correction pass: use the Calendar-owned reset so a live
    # selected-slot hold is RELEASED (tenant-scoped, idempotent) rather than
    # orphaned until lazy expiry — clearing the fields alone would strand it.
    if bool(getattr(conversation, "lead_is_emergency", False)):
        if (_get_state(conversation) != BookingState.NONE
                or getattr(conversation, "booking_selected_slot_id", None) is not None):
            cancel_active_booking(db, client, conversation)
        return BookingReply(handled=False)

    # Intake owns identity. Without name+phone we do not start (Rule 3).
    if not (conversation.lead_name or "").strip() or not (conversation.lead_phone or "").strip():
        return BookingReply(handled=False)

    # PATCH 3 (Senior Audit Critical #5): information interruption.
    # The patient paused the booking dialog to ask an office-information
    # question. Yield to chat.py's existing answer paths and leave EVERY
    # booking_* field byte-unchanged — no clear, no advance, no re-ask —
    # so the next scheduling message resumes this exact state (Rule 14:
    # no unexplained state changes; the state simply persists).
    if information_interruption:
        return BookingReply(handled=False)

    now_utc = client_now(settings).astimezone(ZoneInfo("UTC"))
    state = _get_state(conversation)

    if state == BookingState.NONE:
        return _handle_start(db, client, conversation, settings, user_text, now_utc)
    if state == BookingState.WAITING_FOR_DATE:
        return _handle_date(db, client, conversation, settings, user_text, now_utc)
    if state == BookingState.WAITING_FOR_TIME_PREFERENCE:
        return _handle_time_preference(db, client, conversation, settings, user_text, now_utc)
    if state == BookingState.WAITING_FOR_SLOT_SELECTION:
        return _handle_slot_selection(db, client, conversation, settings, user_text, now_utc)
    if state == BookingState.WAITING_FOR_CONFIRMATION:
        return _handle_confirmation(db, client, conversation, settings, user_text, now_utc)

    # BOOKED (or unknown) should have been cleared already; restate + clear.
    return _restate_or_reset(db, client, conversation, settings)


def begin_booking_after_intake(
    db: Session,
    client,
    conversation,
    user_text: str,
    *,
    seed_date: Optional[date] = None,
    seed_date_text: Optional[str] = None,
    seed_time_preference: Optional[str] = None,
    seeds_are_authoritative: bool = False,
) -> BookingReply:
    """
    Purpose: PATCH 3 (Senior Audit Critical #5) — the EXPLICIT start-after-
             intake entry. chat.py calls this at the exact moment a normal
             (non-emergency) lead completes, so the Calendar dialog can
             replace the manual-callback ending. The Calendar module remains
             the single owner of its start logic: the completing message is
             passed through unchanged (never synthesized), and this module
             alone decides whether it seeds the preferred date (it does,
             via the same _handle_start parsing every start uses).
    Inputs:  live session, client row, conversation row, the raw completing
             patient message.
             seed_date / seed_date_text / seed_time_preference /
             seeds_are_authoritative (keyword-only, CHECKPOINT B rev2;
             seeds default None / False): what chat.py derived from a
             canonical time-window value. The completing message can
             contain rating/fraction tokens ("my pain is 7/10 and I can
             come in on July 28 morning") that the pure intent parser
             reads as a wrong-year date, and re-parsing raw text also
             dropped an already-answered part of day — the seeds carry the
             value the capture owner validated instead.
             seed_date: an explicit date (used as-is, still validated).
             seed_date_text: a safe day word ("tuesday", "tomorrow",
                 "today") from a stored value with no explicit date; this
                 module resolves it with the SAME appointment-intent owner
                 (parse_preferred_date) every start already uses — legacy
                 "Tuesday morning" preferences are consumed, never
                 re-asked, and no weekday arithmetic is duplicated.
             seed_time_preference: a PREF_* bucket, honored exactly the way
                 _handle_date honors "friday morning"; with no resolvable
                 date it is persisted so the day question is the ONLY
                 remaining question ("Weekday morning" -> ask the day,
                 keep morning).
             seeds_are_authoritative: True only when the caller derived
                 the seeds from THIS message safely canonicalized through
                 the rating-aware Checkpoint A pipeline; the raw-text date
                 fallback is then skipped entirely, because that fallback
                 is exactly the wrong-candidate defect vector.
             This module remains the single owner of every state decision:
             any resolved date still passes _validate_and_store_date
             (horizon and past-date rules). With no seeds, every
             pre-Checkpoint-B call site is byte-identical in behavior.
    Returns: BookingReply. handled=False means chat.py must keep today's
             lead-complete reply (booking disabled, emergency-flagged,
             intake identity missing, or a dialog is somehow already
             active — the continuation hook owns active dialogs).
    Database effects: on success, the NONE-state start transition commits
             booking_state (and preferred date when the message named one).
    Possible failures: service/db exceptions propagate to chat.py's logged
             boundary — no broad except here (Rule 4).
    """
    settings = load_calendar_settings(client)
    if not settings.booking_enabled:
        return BookingReply(handled=False)

    # Emergency leads never book (same two-layer rule as the main entry).
    # PATCH 3 correction pass: the Calendar-owned reset releases any live
    # selected-slot hold (tenant-scoped, idempotent) instead of orphaning it.
    if bool(getattr(conversation, "lead_is_emergency", False)):
        if (_get_state(conversation) != BookingState.NONE
                or getattr(conversation, "booking_selected_slot_id", None) is not None):
            cancel_active_booking(db, client, conversation)
        return BookingReply(handled=False)

    # Intake owns identity (Rule 3): without name+phone we do not start.
    if not (conversation.lead_name or "").strip() or not (conversation.lead_phone or "").strip():
        return BookingReply(handled=False)

    # Defensive: an already-active dialog belongs to the continuation hook,
    # not the start entry. Yield rather than restart (Rule 14).
    if _get_state(conversation) != BookingState.NONE:
        return BookingReply(handled=False)

    now_utc = client_now(settings).astimezone(ZoneInfo("UTC"))
    return _handle_start(
        db, client, conversation, settings, user_text, now_utc,
        seed_date=seed_date,
        seed_date_text=seed_date_text,
        seed_time_preference=seed_time_preference,
        seeds_are_authoritative=seeds_are_authoritative,
    )


def cancel_active_booking(db: Session, client, conversation) -> None:
    """
    Purpose: PATCH 3 (Senior Audit Critical #5) — the Calendar-owned reset.
             chat.py calls this when the booking dialog must stop mid-flight:
             an emergency message arrived, ownership transitioned to an
             external booking URL, or the patient explicitly ended the
             conversation. chat.py never touches booking_* fields itself.
    Inputs:  live session, client row (tenant scope), conversation row.
    Returns: None. Idempotent: calling with no active dialog and no hold is
             a safe no-op that still leaves state cleared.
    Database effects: releases this conversation's owned hold (if any) via
             appointment_hold_service.release_hold — tenant-scoped through
             client.id, atomic, commits on ownership match — then clears
             every booking_* field and commits. Ordering is deliberate:
             hold first, state second, so a failure between the two can
             leave a RELEASED hold with stale state (harmless: the next
             delegation revalidates and re-offers) but never cleared state
             with an ORPHANED hold.
    Possible failures: exceptions propagate to the caller, which must roll
             back and keep its own patient-facing reply (the emergency
             reply is never replaced by a cleanup failure).
    """
    slot_id = getattr(conversation, "booking_selected_slot_id", None)
    if slot_id is not None:
        # release_hold reports success for already-free/expired slots and
        # refuses (changing nothing) if another conversation owns the hold —
        # both outcomes leave the desired end state, so the result needs no
        # branching here (Rule 16: nothing is hidden; it logs via its result).
        appointment_hold_service.release_hold(db, client.id, slot_id, conversation.id)

    _clear_booking_state(conversation)
    db.add(conversation)
    db.commit()


# ---------------------------------------------------------------------------
# State handlers — one per state, each under ~40 lines (Rule 5).
# ---------------------------------------------------------------------------

def _handle_start(db, client, conversation, settings, user_text, now_utc,
                  seed_date=None, seed_date_text=None,
                  seed_time_preference=None,
                  seeds_are_authoritative=False) -> BookingReply:
    """NONE -> WAITING_FOR_DATE, or straight to the exact-slot offer via
    _after_date_stored when the opening message already named a day
    ('anything thursday?') — PACKAGE B removed the intermediate
    morning/afternoon stage from every date continuation.

    CHECKPOINT B (rev2): seeds derived from a canonical time window replace
    the raw-text parse. Date resolution order:
      1. seed_date — an explicit date, used as-is.
      2. seed_date_text — a safe day word from a stored value with no
         explicit date ("tuesday", "tomorrow"), resolved by the SAME
         appointment-intent owner every start uses. This is how a complete
         legacy preference ("Tuesday morning") collected earlier is
         consumed when a later answer ("returning") completes the lead.
      3. the raw message — UNLESS seeds_are_authoritative, because the raw
         completing message can contain a rating token the intent parser
         misreads as a wrong-year date (the proven cause of the false
         booking-horizon rejection on staging).
    A seed preference with a resolved date answers the morning/afternoon
    question in the same turn (mirroring how _handle_date honors 'friday
    morning'); with NO resolvable date ("Weekday morning") it is PERSISTED
    so the day question is the only remaining question — _handle_date
    already consumes a persisted preference. Every resolved date is still
    validated by _validate_and_store_date; unseeded calls behave exactly
    as before."""
    # Duplicate defense: one appointment per conversation (Rule 10).
    existing = appointment_repository.get_appointment_by_conversation(
        db, client.id, conversation.id
    )
    if existing is not None:
        return _reply_existing_appointment(existing, settings)

    today_local = client_now(settings).date()

    parsed_date = seed_date
    if parsed_date is None and seed_date_text:
        # Resolve the stored day word through the one date owner — never
        # weekday arithmetic of our own (Rule 3).
        parsed_date = parse_preferred_date(seed_date_text, today_local)
    if parsed_date is None and not seeds_are_authoritative:
        parsed_date = parse_preferred_date(user_text, today_local)

    if parsed_date is not None:
        reply = _validate_and_store_date(
            db, conversation, settings, parsed_date, today_local
        )
        if reply is not None:
            return reply

        # PACKAGE B: whether the capture owner recorded a part of day (or
        # exact-time bucket) or not, the date's ONE continuation is
        # _after_date_stored (Rule 3) — it stores a known preference and
        # offers exact slots; with no known preference it offers the whole
        # day (PREF_ANY inside _offer_slots). The former inline
        # morning/afternoon question is removed with the stage itself.
        return _after_date_stored(
            db, client, conversation, settings, parsed_date, now_utc,
            seed_time_preference,
        )

    # No resolvable date. A seeded preference ("Weekday morning", or a
    # completed lead's bare "morning" message) is PERSISTED before asking
    # for the day, so the day is the ONLY remaining question —
    # _handle_date's existing `parse_time_preference(user_text) or
    # conversation.booking_time_preference` then consumes it without ever
    # re-asking morning/afternoon.
    if seed_time_preference is not None:
        conversation.booking_time_preference = seed_time_preference

    conversation.booking_state = BookingState.WAITING_FOR_DATE
    db.add(conversation)
    db.commit()
    return BookingReply(
        True,
        "What day would work best for your appointment?",
        _date_stage_meta(settings),
    )


def _handle_date(db, client, conversation, settings, user_text, now_utc) -> BookingReply:
    """WAITING_FOR_DATE: parse the day; on success ask the one next question
    (or skip it when the same message already said 'friday morning')."""
    today_local = client_now(settings).date()
    parsed_date = parse_preferred_date(user_text, today_local)

    if parsed_date is None:
        return BookingReply(
            True,
            "Which day would you like? You can say something like "
            "\u201cThursday\u201d, \u201ctomorrow\u201d, or \u201cJuly 16\u201d.",
            _date_stage_meta(settings),
        )

    reply = _validate_and_store_date(db, conversation, settings, parsed_date, today_local)
    if reply is not None:
        return reply

    # 'friday morning' answers two questions at once — honor both.
    preference = (
        parse_time_preference(user_text)
        or conversation.booking_time_preference
    )
    return _after_date_stored(
        db, client, conversation, settings, parsed_date, now_utc, preference
    )


def _after_date_stored(db, client, conversation, settings, parsed_date,
                       now_utc, preference) -> BookingReply:
    """The ONE continuation after _validate_and_store_date succeeds
    (Rule 3): store a volunteered time preference when the same message
    carried one ('friday morning'), then offer exact slots. Extracted
    verbatim from _handle_date for C2-A.2 so the visual picker's date
    resolver runs EXACTLY the typed path's transition — the approved plan
    forbids a second date-storage or continuation implementation.

    PACKAGE B: the morning/afternoon question is REMOVED from this
    continuation. Every caller is reachable only through the strict
    booking_enabled entry gates (handle_booking_message,
    begin_booking_after_intake, resolve_calendar_action), so this is
    Calendar-only code by construction and needs no tier check of its own.
    With no known preference the stored value stays None and _offer_slots
    filters with its existing PREF_ANY default — the whole selected day,
    capped by the UNCHANGED max_offered_slots contract — and records the
    effective preference exactly as before (Patch 2C). No state named
    WAITING_FOR_TIME_PREFERENCE is entered here anymore;
    _handle_time_preference remains solely for conversations persisted at
    that state before Package B."""
    if preference is not None:
        conversation.booking_time_preference = preference
    return _offer_slots(db, client, conversation, settings, now_utc)


def _validate_and_store_date(db, conversation, settings, parsed_date, today_local) -> Optional[BookingReply]:
    """Shared range check. On violation the conversation is moved to
    WAITING_FOR_DATE and COMMITTED before the re-ask reply is returned —
    otherwise a flow that started at NONE would be stranded there and
    chat.py would never route the patient's next answer back here.
    On success the date is stored (uncommitted; the caller's next step
    commits) and None is returned."""
    horizon = settings.max_booking_days
    failure_text = None
    if (parsed_date - today_local).days > horizon:
        failure_text = (
            f"The office is currently booking up to {horizon} days ahead. "
            "Could you pick a sooner day?"
        )
    elif parsed_date < today_local:
        failure_text = "That date has already passed — which upcoming day works for you?"

    if failure_text is not None:
        conversation.booking_state = BookingState.WAITING_FOR_DATE
        conversation.booking_selected_slot_id = None
        db.add(conversation)
        db.commit()
        return BookingReply(
            True, failure_text,
            _date_stage_meta(settings),
        )

    conversation.booking_state = BookingState.WAITING_FOR_DATE
    conversation.booking_preferred_date = parsed_date.isoformat()
    return None


def _handle_time_preference(db, client, conversation, settings, user_text, now_utc) -> BookingReply:
    """LEGACY-STATE RESOLVER (Package B v1.0.1). WAITING_FOR_TIME_PREFERENCE
    is reachable only by conversations PERSISTED at the removed
    morning/afternoon stage before Package B deployed — no current flow
    enters it. The Calendar contract applies to that cohort too: the
    question is never asked again. Resolution order:
      1. A brand-new day in the message is honored (patient changed
         direction) through the SAME validated date owner as every other
         path; invalid days get that owner's existing corrections.
      2. An answered preference ("morning", "afternoon", "any time") is
         honored exactly as before — the patient may have SEEN the old
         question immediately pre-deploy, and the answer still counts —
         proceeding to the exact-slot offer (a new same-message day from
         step 1 rides along).
      3. A new day WITHOUT a preference runs the ONE date continuation
         (_after_date_stored): straight to the exact-slot offer with the
         PREF_ANY default — the old "Okay — <day>. Morning or afternoon?"
         re-ask is removed with the stage.
      4. Anything else: the persisted date is already authoritative, so the
         reply IS the exact-slot offer (PREF_ANY default inside
         _offer_slots) — never a re-ask.
      5. Corrupt/missing persisted date with nothing usable in the message:
         fail safely to the existing day question (WAITING_FOR_DATE with
         _date_stage_meta), mirroring the vanished-offer clean restart —
         never morning/afternoon.
    Database effects: date/preference storage and state transitions via the
    existing owners; the corrupt-date fallback commits WAITING_FOR_DATE.
    Possible failures: none beyond the delegated owners' documented ones.
    """
    today_local = client_now(settings).date()
    new_date = parse_preferred_date(user_text, today_local)
    if new_date is not None:
        reply = _validate_and_store_date(db, conversation, settings, new_date, today_local)
        if reply is not None:
            return reply

    preference = parse_time_preference(user_text)
    if preference is None and new_date is not None:
        # Package B v1.0.1 (case E): a new day alone proceeds through the
        # single date continuation to the direct exact-slot offer.
        return _after_date_stored(
            db, client, conversation, settings, new_date, now_utc, None
        )
    if preference is None:
        # Package B v1.0.1 (cases D/G): no re-ask, ever. A readable
        # persisted day is authoritative and gets its offer; a
        # missing/corrupt one fails safely back to the day question.
        if _get_pref_date(conversation) is None:
            conversation.booking_state = BookingState.WAITING_FOR_DATE
            db.add(conversation)
            db.commit()
            return BookingReply(
                True,
                "Let me pull up fresh times. What day works best?",
                _date_stage_meta(settings),
            )
        return _offer_slots(db, client, conversation, settings, now_utc)

    conversation.booking_time_preference = preference
    return _offer_slots(db, client, conversation, settings, now_utc)


def _offer_is_expired(conversation, now_utc) -> bool:
    """
    Purpose: Decide whether the PRE-HOLD offer is still usable (Patch 2C).
    Contract (both sides normalized through ensure_utc):
        normalized_now <  normalized_expiry -> valid
        normalized_now >= normalized_expiry -> expired
        NULL expiry while offered slot IDs exist -> expired (safe: pre-2C
        in-flight conversations self-heal with one fresh offer).
    Database effects: none (pure read of the conversation row).
    """
    expires_at = getattr(conversation, "booking_offer_expires_at", None)
    if expires_at is None:
        return True
    return ensure_utc(now_utc) >= ensure_utc(expires_at)


def _revalidation_preference(conversation) -> str:
    """The preference hold/finalize must revalidate with: the EFFECTIVE
    preference the offer was actually filtered with (PREF_ANY when the offer
    was relaxed), falling back to the stored preference for state written
    before Patch 2C. One reader, used by both call sites (Rule 3)."""
    return (getattr(conversation, "booking_effective_time_preference", None)
            or conversation.booking_time_preference or PREF_ANY)


def _offer_slots(db, client, conversation, settings, now_utc) -> BookingReply:
    """Fetch availability for the stored day+preference and present the
    FIRST PAGE of up to max_offered_slots numbered options - appending the
    'See later times' navigation choice when more eligible slots exist on
    the same day (UX-A) - or suggest other days."""
    day = _get_pref_date(conversation)
    if day is None:  # Corrupt/missing date: fall back to asking the day again.
        conversation.booking_state = BookingState.WAITING_FOR_DATE
        db.add(conversation)
        db.commit()
        return BookingReply(True, "What day would work best for you?",
                            _date_stage_meta(settings))

    preference = conversation.booking_time_preference or PREF_ANY
    # UX-A: fetch the UNCAPPED chronological eligible list through the same
    # single rule owner, so the first page can truthfully know whether later
    # times exist on this day without a second availability computation.
    eligible = availability_service.get_bookable_slots_for_day(
        db, client.id, settings, day, preference, now_utc,
        service_key=(conversation.lead_reason or None),
    )
    relaxed = False
    if not eligible and preference != PREF_ANY:
        # Same day, other times: better than a dead end, and clearly labeled.
        eligible = availability_service.get_bookable_slots_for_day(
            db, client.id, settings, day, PREF_ANY, now_utc,
            service_key=(conversation.lead_reason or None),
        )
        relaxed = bool(eligible)

    if not eligible:
        return _suggest_other_days(db, client, conversation, settings, day, now_utc)

    # UX-A first page: the first max_offered_slots eligible slots - by
    # construction the IDENTICAL membership and order the pre-pagination
    # capped fetch produced (filter_bookable_slots is documented as exactly
    # list_bookable_slots + this same slice, and a pagination test pins the
    # equivalence). has_later is the ONLY new fact derived from the tail;
    # hidden later slot ids are never persisted, exposed, or pre-sent.
    slots = eligible[: settings.max_offered_slots]
    has_later = len(eligible) > len(slots)

    # PATCH 2C: the offer gets an explicit bounded lifetime and records the
    # EFFECTIVE preference it was filtered with (PREF_ANY when relaxed) so
    # hold/finalize revalidate against what was truly offered. The expiry is
    # derived from the ensure_utc-normalized now, never from a possibly
    # naive datetime.
    normalized_now = ensure_utc(now_utc)
    conversation.booking_offered_slot_ids = [str(s.id) for s in slots]
    conversation.booking_offer_expires_at = (
        normalized_now + timedelta(minutes=BOOKING_OFFER_TTL_MINUTES)
    )
    conversation.booking_effective_time_preference = (
        PREF_ANY if relaxed else preference
    )
    conversation.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    db.add(conversation)
    db.commit()

    menu = _slot_menu(slots, settings.timezone_name)
    prefix = (
        f"I don\u2019t have {preference} openings on {_fmt_day(day)}, but I do have: "
        if relaxed else f"Here\u2019s what\u2019s open on {_fmt_day(day)}: "
    )
    reply_meta = {"mode": "booking", "state": conversation.booking_state,
                  "offered_slots": conversation.booking_offered_slot_ids}
    if settings.calendar_actions_enabled:
        # C1-C: tappable versions of the SAME persisted offer. Text replies
        # ("1", "2", a time) keep working identically; buttons are additive.
        reply_meta["calendar_actions"] = _slot_choice_actions(
            slots, settings.timezone_name
        )
        # UX-A: append 'See later times' when - and only when - eligible
        # slots exist beyond this first page. A day whose slots fit one
        # page keeps the action set byte-identical to the frozen
        # pre-pagination behavior (first-page slot chips are unchanged
        # either way; the navigation chip is purely additive).
        if has_later:
            reply_meta["calendar_actions"] += _slot_nav_actions(False, True)
        # C2-A.3: the slot_selection stage signal attaches ONLY alongside
        # slot calendar_actions, and only under the strict triple gate
        # (single owner: _picker_stage_signal). A reply without actions
        # never carries it.
        picker_signal = _picker_stage_signal(
            settings, PICKER_STAGE_SLOT_SELECTION
        )
        if picker_signal is not None:
            reply_meta["calendar_picker"] = picker_signal
    return BookingReply(
        True,
        f"{prefix}{menu}. Which works best?",
        reply_meta,
    )


def _suggest_other_days(db, client, conversation, settings, day, now_utc) -> BookingReply:
    """No openings on the requested day: offer up to 3 LATER days whose
    availability actually matches this patient, and go back to
    WAITING_FOR_DATE.

    The scan is filtered with exactly the preference and service_key the
    rejected day was filtered with, and it starts the day AFTER the rejected
    day. Previously it ran unfiltered from offset 0, so a day with no
    matching slots could be declared unavailable and then offered straight
    back as available.

    BOTH replies ask for a specific day. WAITING_FOR_DATE can only consume
    a date, so neither branch of this owner may ask a yes/no question its
    own state cannot parse (Rule 14) - not the suggestion branch, and not
    the office-help fallback.
    """
    days = availability_service.find_days_with_availability(
        db, client.id, settings, day, now_utc,
        time_preference=(conversation.booking_time_preference or PREF_ANY),
        service_key=(conversation.lead_reason or None),
        skip_start_day=True,
    )
    conversation.booking_state = BookingState.WAITING_FOR_DATE
    conversation.booking_offered_slot_ids = None
    conversation.booking_offer_expires_at = None            # Patch 2C
    conversation.booking_effective_time_preference = None   # Patch 2C
    db.add(conversation)
    db.commit()

    if days:
        lead = (
            f"The nearest day with matching availability is {_fmt_day_list(days)}"
            if len(days) == 1
            else f"The nearest days with matching availability are {_fmt_day_list(days)}"
        )
        text = (f"I don\u2019t see matching openings on {_fmt_day(day)}. "
                f"{lead}. "
                "Which day works best? Please reply with the day.")
    else:
        text = (f"I don\u2019t see matching online openings around {_fmt_day(day)}. "
                "The office can help directly. "
                "What other specific day would you like me to check?")
    return BookingReply(True, text,
                        _date_stage_meta(settings))


def _refresh_expired_offer(db, client, conversation, settings, now_utc) -> BookingReply:
    """PATCH 2C expiry recovery - extracted MECHANICALLY from
    _handle_slot_selection for UX-A so the typed path and the navigation
    action path share the single owner (Rule 3); the moved logic is byte-
    preserved. Clear ALL THREE stale values, then generate a fresh offer
    for the same stored day/preference; the patient re-picks from CURRENT
    times. The stale menu can never place a hold."""
    conversation.booking_offered_slot_ids = None
    conversation.booking_offer_expires_at = None
    conversation.booking_effective_time_preference = None
    db.add(conversation)
    reply = _offer_slots(db, client, conversation, settings, now_utc)
    reply.meta["reason"] = "offer_expired"
    return reply


def _select_slot_page(eligible, prev_rows, page_size, go_later):
    """
    Purpose: Pure UX-A page math - choose the adjacent page of the freshly
             revalidated chronological eligible list, anchored on the start
             times of the rows the patient is navigating FROM.
    Inputs:
        eligible:  the fresh uncapped bookable list (chronological, non-
                   empty - the caller owns the empty-day branch).
        prev_rows: surviving rows of the currently persisted page (may be
                   empty when staff removed every one of them).
        page_size: settings.max_offered_slots (already clamped 1..10).
        go_later:  True for 'See later times', False for 'Back to earlier
                   times'.
    Returns: (page, moved) - page is a non-empty slice of eligible; moved
             is True only when the page really lies in the requested
             direction relative to the anchor (drives truthful wording).
    Database effects: none (pure).
    Possible failures: none - every degenerate input falls back to a real,
        CURRENTLY eligible page (first or last), never an empty offer and
        never an invented or resurrected slot.

    Anchoring is by START TIME, never by list index: availability may have
    changed since the previous page was shown (booked / newly held /
    staff-removed rows), so index arithmetic against the stale list would
    lie. Later = the first page_size eligible slots strictly AFTER the
    previous page's latest start; earlier = the last page_size strictly
    BEFORE its earliest start (the slots immediately preceding it). With
    no surviving anchor the first page is the only honest answer; with
    nothing beyond the anchor the CURRENT last (or first) page is shown
    instead, flagged unmoved so the reply never claims a false direction.
    """
    if not prev_rows:
        return eligible[:page_size], False
    if go_later:
        anchor = max(ensure_utc(r.start_datetime) for r in prev_rows)
        page = [s for s in eligible
                if ensure_utc(s.start_datetime) > anchor][:page_size]
        if page:
            return page, True
        return eligible[-page_size:], False
    anchor = min(ensure_utc(r.start_datetime) for r in prev_rows)
    earlier = [s for s in eligible if ensure_utc(s.start_datetime) < anchor]
    if earlier:
        return earlier[-page_size:], True
    return eligible[:page_size], False


def _offer_adjacent_slot_page(db, client, conversation, settings, now_utc,
                              go_later, entry_offered_ids) -> BookingReply:
    """
    Purpose: Execute ONE UX-A slot-page navigation ('See later times' /
             'Back to earlier times') at WAITING_FOR_SLOT_SELECTION.
    Inputs:  live session, client row, conversation row, settings, now_utc,
             go_later (True = later page, False = earlier page),
             entry_offered_ids (the persisted page ids THIS request entered
             from - the resolver's boundary-reloaded view; the winner gate
             below requires the newest committed row to still equal them).
    Returns: BookingReply presenting the revalidated adjacent page, or the
             truthful current-state reply when this request lost a per-
             conversation race (zero mutation on that path).
    Database effects: rewrites ONLY the Patch 2C offer trio (offered ids,
        display expiry, effective preference) and re-asserts the selection
        state - the SAME fields _offer_slots owns. NO hold is placed or
        released, NO appointment is created, and the stored day is never
        changed here.
    External effects: none - navigation never notifies.
    Possible failures: service/db errors propagate (Rule 4).

    CONCURRENCY (V2/V4 discipline; round-1 audit F1): this mutation may be
    racing a slot selection, another navigation, or a new-date offer that
    committed AFTER this request's V4 boundary reload. So BEFORE any field
    changes: reacquire the tenant-scoped row under SELECT ... FOR UPDATE
    via the existing _lock_conversation_row owner, reload the newest
    committed state, and proceed ONLY while the conversation still awaits
    a selection, no slot has been selected, and the persisted offer still
    equals the page this request entered from. Every loser releases the
    row lock without writing and answers through the existing
    _truthful_current_state_reply owner - a request that began from an
    older ORM snapshot never overwrites a newer committed state. An offer
    that expired in flight, and the corrupt-date self-heal, both run their
    EXISTING recovery owners under this same row lock (their internal
    commits release it atomically with the recovery).

    Every navigation revalidates against CURRENT availability through the
    same uncapped owner the first page uses, filtered with the EFFECTIVE
    preference the live offer was filtered with (_revalidation_preference -
    the Patch 2C reader hold/finalize already trust), so a relaxed PREF_ANY
    offer pages through PREF_ANY times and is never re-narrowed. A day that
    lost ALL availability falls through to the existing _suggest_other_days
    owner (unchanged semantics). Only THIS page's slot ids are persisted
    and exposed; a slot that stopped being eligible between pages can never
    reappear, because membership comes from the fresh list alone.
    """
    # Winner gate (audit F1): lock + reload FIRST, then decide on the
    # newest row - never on the entry snapshot (module V2 header rule).
    locked = _lock_conversation_row(db, client, conversation)
    fresh_selected = getattr(conversation, "booking_selected_slot_id", None)
    if (locked is None
            or _get_state(conversation) != BookingState.WAITING_FOR_SLOT_SELECTION
            or fresh_selected is not None
            or _get_offered_ids(conversation) != list(entry_offered_ids or [])):
        # A selection, another navigation, or a new-date offer won while
        # this request was in flight: PRESERVE it. Release the row lock
        # without writing and answer truthfully for the state that won.
        db.commit()  # No writes: releases the FOR UPDATE row lock only.
        return _truthful_current_state_reply(
            db, client, conversation, settings, now_utc
        )
    if _offer_is_expired(conversation, now_utc):
        # Entry saw a live offer that expired in flight: the EXISTING
        # Patch 2C recovery, now under the row lock taken above (its
        # internal commit releases the lock atomically with the fresh
        # first page). Same behavior the typed path delivers.
        return _refresh_expired_offer(db, client, conversation, settings, now_utc)

    day = _get_pref_date(conversation)
    if day is None:
        # Corrupt/missing date: the SAME self-heal owner _offer_slots uses,
        # under the row lock (its internal commit releases the lock).
        return _offer_slots(db, client, conversation, settings, now_utc)

    preference = _revalidation_preference(conversation)
    prev_rows = _load_offered_slots(db, client, conversation)
    eligible = availability_service.get_bookable_slots_for_day(
        db, client.id, settings, day, preference, now_utc,
        service_key=(conversation.lead_reason or None),
    )
    if not eligible:
        return _suggest_other_days(db, client, conversation, settings, day, now_utc)

    page, moved = _select_slot_page(
        eligible, prev_rows, settings.max_offered_slots, go_later
    )
    has_earlier = (ensure_utc(eligible[0].start_datetime)
                   < ensure_utc(page[0].start_datetime))
    has_later = (ensure_utc(eligible[-1].start_datetime)
                 > ensure_utc(page[-1].start_datetime))

    # Same persisted-offer contract as _offer_slots: THIS page's ids become
    # the ONLY selectable set, with a fresh display TTL; the effective
    # preference is re-asserted with the identical value so the Patch 2C
    # trio stays coherent for hold/finalize revalidation.
    normalized_now = ensure_utc(now_utc)
    conversation.booking_offered_slot_ids = [str(s.id) for s in page]
    conversation.booking_offer_expires_at = (
        normalized_now + timedelta(minutes=BOOKING_OFFER_TTL_MINUTES)
    )
    conversation.booking_effective_time_preference = preference
    conversation.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    db.add(conversation)
    db.commit()

    menu = _slot_menu(page, settings.timezone_name)
    if moved and go_later:
        prefix = f"Here are later times on {_fmt_day(day)}: "
    elif moved:
        prefix = f"Here are earlier times on {_fmt_day(day)}: "
    else:
        # Availability changed underneath the tap (or the previous page's
        # rows all vanished): present what is CURRENTLY open and never
        # claim a direction the page does not actually take (Rule 16).
        prefix = f"Here\u2019s what\u2019s currently open on {_fmt_day(day)}: "
    reply_meta = {"mode": "booking", "state": conversation.booking_state,
                  "offered_slots": conversation.booking_offered_slot_ids}
    if settings.calendar_actions_enabled:
        # Slot chips for THIS page plus the truthful navigation choices;
        # the stage signal keeps its strict triple-gated single owner.
        reply_meta["calendar_actions"] = (
            _slot_choice_actions(page, settings.timezone_name)
            + _slot_nav_actions(has_earlier, has_later)
        )
        picker_signal = _picker_stage_signal(
            settings, PICKER_STAGE_SLOT_SELECTION
        )
        if picker_signal is not None:
            reply_meta["calendar_picker"] = picker_signal
    return BookingReply(
        True,
        f"{prefix}{menu}. Which works best?",
        reply_meta,
    )


def _handle_slot_selection(db, client, conversation, settings, user_text, now_utc) -> BookingReply:
    """WAITING_FOR_SLOT_SELECTION: map the reply to ONE offered slot, place a
    hold, and move to confirmation. A new day restarts availability instead."""
    today_local = client_now(settings).date()
    new_date = parse_preferred_date(user_text, today_local)
    offered = _load_offered_slots(db, client, conversation)

    if new_date is not None and new_date != _get_pref_date(conversation):
        reply = _validate_and_store_date(db, conversation, settings, new_date, today_local)
        if reply is not None:
            return reply
        return _offer_slots(db, client, conversation, settings, now_utc)

    # PATCH 2C offer-expiration gate (Critical #8): a displayed menu is only
    # usable while now < booking_offer_expires_at (NULL counts as expired).
    # Expired -> clear ALL THREE stale values, then generate a fresh offer
    # for the same stored day/preference; the patient re-picks from CURRENT
    # times. The stale menu can never place a hold.
    if getattr(conversation, "booking_offered_slot_ids", None) and _offer_is_expired(
        conversation, now_utc
    ):
        # UX-A: recovery body extracted VERBATIM to _refresh_expired_offer
        # so the navigation action path shares this single owner (Rule 3).
        return _refresh_expired_offer(db, client, conversation, settings, now_utc)

    tz = ZoneInfo(settings.timezone_name)
    pairs: List[Tuple[str, datetime]] = [
        (str(s.id), ensure_utc(s.start_datetime).astimezone(tz)) for s in offered
    ]
    chosen_id = match_slot_selection(user_text, pairs)

    if chosen_id is None:
        menu = _slot_menu(offered, settings.timezone_name) if offered else ""
        text = (f"Just to be sure I pick the right one — {menu}. "
                "You can reply with a number or a time." if menu else
                "Let me pull up fresh times. What day works best?")
        if not menu:  # Offered slots vanished (staff edits); restart cleanly.
            conversation.booking_state = BookingState.WAITING_FOR_DATE
            db.add(conversation)
            db.commit()
            # V2 defect 1: this reply LEAVES the conversation at the date
            # stage, so it must carry the picker signal like every other
            # date-stage reply.
            return BookingReply(True, text, _date_stage_meta(settings))
        return BookingReply(True, text,
                            {"mode": "booking", "state": _get_state(conversation)})

    # C1-C mechanical extraction: the hold-and-advance transition now lives
    # in _hold_offered_slot so the structured-action path can run the SAME
    # owner code (Rule 3) without synthesizing text. Logic byte-preserved.
    return _hold_offered_slot(
        db, client, conversation, settings, chosen_id, offered, now_utc
    )


def _hold_offered_slot(db, client, conversation, settings, chosen_id,
                       offered, now_utc) -> BookingReply:
    """Place the hold on ONE validated member of the persisted offer and
    advance to WAITING_FOR_CONFIRMATION. Extracted MECHANICALLY from
    _handle_slot_selection for C1-C so the text path and the structured-
    action path share the single transition owner; the moved logic is
    byte-preserved apart from recomputing tz locally and the flag-gated
    calendar_actions meta addition at the end."""
    tz = ZoneInfo(settings.timezone_name)
    hold = appointment_hold_service.place_hold(
        db, client.id, uuid.UUID(chosen_id), conversation.id,
        settings=settings,
        time_preference=_revalidation_preference(conversation),
        service_key=(conversation.lead_reason or None),
        now_utc=now_utc,
    )
    if not hold.success:
        # V3 (audit item 1): place_hold completed INTERNALLY (commit or
        # rollback) even on failure, and this request may have entered from
        # a stale WAITING_FOR_SLOT_SELECTION snapshot while a concurrent
        # request already advanced the conversation (e.g. to a live
        # confirmation). Reacquire + reload the tenant-scoped row BEFORE
        # the re-offer decision. No slot release happens on this path:
        # this request acquired no hold.
        locked = _lock_conversation_row(db, client, conversation)
        fresh_selected = getattr(conversation, "booking_selected_slot_id", None)
        if (locked is None
                or _get_state(conversation) != BookingState.WAITING_FOR_SLOT_SELECTION
                or chosen_id not in _get_offered_ids(conversation)
                or _offer_is_expired(conversation, now_utc)
                or fresh_selected is not None):
            # A newer state/selection won while this request was in flight:
            # PRESERVE it — never overwrite a concurrent confirmation or
            # appointment state with a blind re-offer. Release the row lock
            # without writing and answer truthfully for the state that won.
            db.commit()  # No writes: releases the FOR UPDATE row lock only.
            return _truthful_current_state_reply(
                db, client, conversation, settings, now_utc
            )
        # Still the same live selection wait (sequential case): lost the
        # race for THIS slot (Rule 9's two-patients case) OR current policy
        # now rejects it (Patch 2C) — say so accurately, re-offer fresh.
        # The re-offer's writes commit under the row lock taken above.
        return _reoffer_after_conflict(
            db, client, conversation, settings, now_utc,
            ineligible=(hold.reason == "slot_ineligible"),
        )

    # V2 audit item 4 (race A): place_hold COMMITTED internally, so the
    # conversation snapshot that entered this function is no longer
    # authoritative — and any lock taken before place_hold would already
    # have been released by that commit. Reacquire the tenant-scoped row
    # under FOR UPDATE, reload the newest state, and only then decide
    # winner or loser. This request wins only while the conversation still
    # awaits a selection, the submitted choice is still a member of the
    # live persisted offer, and no DIFFERENT slot has already won.
    locked = _lock_conversation_row(db, client, conversation)
    fresh_selected = getattr(conversation, "booking_selected_slot_id", None)
    if (locked is None
            or _get_state(conversation) != BookingState.WAITING_FOR_SLOT_SELECTION
            or chosen_id not in _get_offered_ids(conversation)
            or _offer_is_expired(conversation, now_utc)
            or (fresh_selected is not None and str(fresh_selected) != chosen_id)):
        # Loser: another request advanced this conversation between the
        # hold commit and this lock. Release ONLY the hold THIS request
        # just placed — and only when the surviving selection is a
        # DIFFERENT slot (if the survivor selected this same slot, the
        # hold this request just refreshed IS the surviving hold). The
        # winner's conversation state is never mutated here.
        if fresh_selected is None or str(fresh_selected) != chosen_id:
            appointment_hold_service.release_hold(
                db, client.id, uuid.UUID(chosen_id), conversation.id
            )  # Commits internally: the row lock above is released here.
        else:
            db.commit()  # No writes: releases the FOR UPDATE row lock only.
        # Reload once more (the release committed) before constructing the
        # response, then answer truthfully for the state that won.
        _reload_conversation(db, client, conversation)
        return _truthful_current_state_reply(
            db, client, conversation, settings, now_utc
        )

    conversation.booking_selected_slot_id = uuid.UUID(chosen_id)
    # PATCH 2C: the pre-hold offer is consumed — from here the slot's
    # held_until is the ONLY active expiration authority. The EFFECTIVE
    # preference is deliberately PRESERVED: finalization revalidates against
    # what was truly offered (a relaxed PREF_ANY offer must not be re-judged
    # by the patient's original preference).
    conversation.booking_offered_slot_ids = None
    conversation.booking_offer_expires_at = None
    conversation.booking_state = BookingState.WAITING_FOR_CONFIRMATION
    db.add(conversation)
    db.commit()

    chosen = next(s for s in offered if str(s.id) == chosen_id)
    when = (f"{_fmt_day(ensure_utc(chosen.start_datetime).astimezone(tz).date())} at "
            f"{_fmt_time(chosen.start_datetime, settings.timezone_name)}")
    reply_meta = {"mode": "booking", "state": conversation.booking_state,
                  "held_until": hold.held_until.isoformat() if hold.held_until else None}
    if settings.calendar_actions_enabled:
        # C1-C: tappable yes/no versions of the SAME confirmation question.
        # Text replies keep working identically; buttons are additive.
        reply_meta["calendar_actions"] = _confirm_choice_actions(
            conversation.booking_selected_slot_id
        )
    return BookingReply(
        True,
        f"To confirm: {conversation.lead_name} on {when}. Is that correct?",
        reply_meta,
    )


def _reoffer_after_conflict(db, client, conversation, settings, now_utc,
                            ineligible: bool = False) -> BookingReply:
    """The chosen slot is unavailable: apologize once, accurately, and show
    fresh availability for the same day/preference. Two truthful sentences
    (Patch 2C — approved wording): a race loss is "just taken"; a slot that
    CURRENT policy now rejects (notice/horizon/service/preference) is
    "no longer available" — with no channel claim, because the cause is
    policy, not availability elsewhere."""
    reply = _offer_slots(db, client, conversation, settings, now_utc)
    if reply.meta.get("offered_slots"):
        apology = ("I\u2019m sorry — that time is no longer available. "
                   if ineligible else
                   "I\u2019m sorry — that time was just taken. ")
        reply.text = apology + reply.text
    return reply


def _handle_confirmation(db, client, conversation, settings, user_text, now_utc) -> BookingReply:
    """WAITING_FOR_CONFIRMATION: yes -> finalize + notify; no -> release the
    hold and restart at the day question; a new day counts as 'no'."""
    slot_id = getattr(conversation, "booking_selected_slot_id", None)
    today_local = client_now(settings).date()

    new_date = parse_preferred_date(user_text, today_local)
    decision = parse_yes_no(user_text)

    if new_date is not None and decision is not True:
        return _confirmation_change_day(db, client, conversation, settings,
                                        slot_id, new_date, today_local, now_utc)
    if decision is None:
        reply_meta = {"mode": "booking",
                      "state": BookingState.WAITING_FOR_CONFIRMATION}
        if settings.calendar_actions_enabled and slot_id is not None:
            # C1-C: re-issue the tappable yes/no with the one re-ask.
            reply_meta["calendar_actions"] = _confirm_choice_actions(slot_id)
        return BookingReply(
            True,
            "Should I book that time for you — yes or no?",
            reply_meta,
        )
    if decision is False:
        # C1-C mechanical extraction: shared with the structured-action
        # confirm-no path (Rule 3 single owner); logic byte-preserved.
        return _decline_and_restart(db, client, conversation, settings, slot_id)

    return _finalize_and_reply(db, client, conversation, settings, slot_id, now_utc)


def _confirmation_change_day(db, client, conversation, settings, slot_id,
                             new_date, today_local, now_utc) -> BookingReply:
    """Patient answered the confirmation with a different day: release the
    hold, adopt the new day, and re-offer (Rule 9: changing their answer)."""
    if slot_id is not None:
        appointment_hold_service.release_hold(db, client.id, slot_id, conversation.id)
    reply = _validate_and_store_date(db, conversation, settings, new_date, today_local)
    if reply is not None:
        return reply
    conversation.booking_selected_slot_id = None
    return _offer_slots(db, client, conversation, settings, now_utc)


def _decline_and_restart(db, client, conversation, settings, slot_id) -> BookingReply:
    """The confirmation 'no' transition: release this conversation's hold
    and restart at the day question. C1-C shared single owner (Rule 3) for
    the text path and the structured-action confirm-no path.

    V2 audit item 4 (race B): release_hold COMMITS internally and reports
    success even when the slot is no longer held — including when a racing
    confirm-yes just BOOKED it — so every conversation mutation below
    happens only after reacquiring + reloading the row, and an active
    appointment always outranks the restart. A patient must never be asked
    "what day would work better?" while their appointment already exists.
    Sequential (non-race) behavior is unchanged: the reloaded state matches
    the snapshot, and the restart proceeds exactly as before."""
    if slot_id is not None:
        appointment_hold_service.release_hold(db, client.id, slot_id, conversation.id)

    # Reacquire + reload AFTER the internally committing release; decide on
    # the newest row, never the entry snapshot (V2 item 4).
    locked = _lock_conversation_row(db, client, conversation)
    now_utc = client_now(settings).astimezone(ZoneInfo("UTC"))

    existing = appointment_repository.get_appointment_by_conversation(
        db, client.id, conversation.id
    )
    if existing is not None:
        # A concurrent confirm-yes won the race: clear any stale dialog
        # state and restate the appointment that actually exists.
        _clear_booking_state(conversation)
        db.add(conversation)
        db.commit()
        return _reply_existing_appointment(existing, settings)

    fresh_selected = getattr(conversation, "booking_selected_slot_id", None)
    if (locked is not None
            and _get_state(conversation) == BookingState.WAITING_FOR_CONFIRMATION
            and (slot_id is None
                 or fresh_selected is None
                 or str(fresh_selected) == str(slot_id))):
        # Still the same pending confirmation this 'no' answered: restart
        # at the day question (pre-V2 behavior, now under the row lock).
        conversation.booking_state = BookingState.WAITING_FOR_DATE
        conversation.booking_selected_slot_id = None
        conversation.booking_offered_slot_ids = None
        conversation.booking_offer_expires_at = None            # Patch 2C
        conversation.booking_effective_time_preference = None   # Patch 2C
        db.add(conversation)
        db.commit()
        return BookingReply(
            True,
            "No problem — what day would work better?",
            _date_stage_meta(settings),  # V2 defect 1: date-stage reply
        )

    # Another request already moved this conversation to a different valid
    # state: preserve it (never overwrite the newer row from this older
    # request), release the row lock without writing, and answer truthfully
    # for the state that won.
    db.commit()
    return _truthful_current_state_reply(db, client, conversation, settings, now_utc)


def _finalize_and_reply(db, client, conversation, settings, slot_id, now_utc,
                        *, restate_after_hold_loss: bool = False) -> BookingReply:
    """The single place where WAITING_FOR_CONFIRMATION becomes BOOKED —
    which is why notifications here can never fire twice (Rule 10)."""
    if slot_id is None:  # State corruption: never invent a slot; restart.
        conversation.booking_state = BookingState.WAITING_FOR_DATE
        db.add(conversation)
        db.commit()
        return BookingReply(True, "Let me pull up times again — what day works best?",
                            _date_stage_meta(settings))  # V2 defect 1

    result = booking_service.finalize_booking(
        db, client.id, slot_id, conversation.id,
        settings=settings,
        now_utc=now_utc,
        time_preference=_revalidation_preference(conversation),
        service_key=(conversation.lead_reason or None),
        patient_name=conversation.lead_name or "",
        patient_phone=conversation.lead_phone or "",
        patient_email=getattr(conversation, "lead_email", None),
        new_or_returning=_patient_type(conversation),
        reason=getattr(conversation, "lead_reason", None),
        urgency="priority" if bool(getattr(conversation, "lead_is_priority", False)) else "routine",
    )

    if result.reason == "already_booked_by_conversation" and result.appointment:
        # V2 audit item 4: finalize_booking committed internally — reacquire
        # + reload before the terminal clear so this write never resurrects
        # a stale snapshot over a concurrent request's newer state.
        _lock_conversation_row(db, client, conversation)
        _clear_booking_state(conversation)
        db.add(conversation)
        db.commit()
        return _reply_existing_appointment(result.appointment, settings)

    if not result.success:
        # V2 audit item 4: finalize_booking committed / rolled back
        # internally, so the pre-call conversation snapshot is not
        # authoritative. Reacquire the row under FOR UPDATE and reload
        # BEFORE any recovery decision or conversation write.
        locked = _lock_conversation_row(db, client, conversation)
        # C1-C (approved C-3, ACTION PATH ONLY via the keyword flag): a
        # concurrent same-slot duplicate can lose the race AFTER this
        # conversation's other request already created the appointment
        # (verified: finalize_booking's loser sees status BOOKED and
        # reports hold_lost before reaching the INSERT and its unique-
        # index handler). Re-check and RESTATE instead of re-offering —
        # never a second booking, never a second notification. Text-path
        # parity is a recorded deferred finding.
        if restate_after_hold_loss and result.reason in ("hold_lost", "hold_expired"):
            existing = appointment_repository.get_appointment_by_conversation(
                db, client.id, conversation.id
            )
            if existing is not None:
                _clear_booking_state(conversation)
                db.add(conversation)
                db.commit()
                return _reply_existing_appointment(existing, settings)
        # V2 audit item 4 (race B): if a concurrent request already moved
        # this conversation out of this confirmation (e.g. confirm-no
        # restarted at the day question), PRESERVE that state — never
        # blindly re-offer over it, never overwrite the newer row from
        # this older request. Answer truthfully for the state that won.
        if (locked is None
                or _get_state(conversation) != BookingState.WAITING_FOR_CONFIRMATION
                or (getattr(conversation, "booking_selected_slot_id", None) is not None
                    and str(conversation.booking_selected_slot_id) != str(slot_id))):
            db.commit()  # No writes: releases the FOR UPDATE row lock only.
            return _truthful_current_state_reply(
                db, client, conversation, settings, now_utc
            )
        # hold_lost / hold_expired / slot_missing / slot_ineligible: the
        # under-lock recheck failed — re-offer. On slot_ineligible the hold
        # was already released atomically inside finalize_booking; the
        # effective preference is REPLACED by the fresh offer below.
        conversation.booking_selected_slot_id = None
        db.add(conversation)
        return _reoffer_after_conflict(
            db, client, conversation, settings, now_utc,
            ineligible=(result.reason == "slot_ineligible"),
        )

    # PATCH 6 (Senior Audit Recommended #7): per-channel outcomes are
    # persisted on the appointment row inside send_booking_notifications and
    # surfaced to STAFF through the admin AppointmentView only. Notification
    # internals are no longer placed into the patient-facing reply meta, so
    # the return value is deliberately unused here.
    notification_service.send_booking_notifications(
        db, client, result.appointment, settings
    )
    # V2 audit item 4: the successful finalize committed internally —
    # reacquire + reload before the terminal clear so this write never
    # resurrects a stale snapshot over a concurrent request's newer state.
    # The cleared terminal state is authoritative in EVERY interleaving
    # here, because the appointment now exists (Rule 14 terminal cleanup).
    _lock_conversation_row(db, client, conversation)
    _clear_booking_state(conversation)
    db.add(conversation)
    db.commit()

    when = (f"{_fmt_day(ensure_utc(result.appointment.start_datetime).astimezone(ZoneInfo(settings.timezone_name)).date())} "
            f"at {_fmt_time(result.appointment.start_datetime, settings.timezone_name)}")
    if settings.require_staff_confirmation:
        text = (f"All set, {conversation.lead_name}! Your appointment request for "
                f"{when} has been received — the office will contact you to confirm.")
    else:
        text = f"All set, {conversation.lead_name}! You\u2019re booked for {when}."
    return BookingReply(
        True, text,
        {"mode": "booking", "state": BookingState.NONE, "booked": True,
         "appointment_id": str(result.appointment.id)},
    )


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _patient_type(conversation) -> Optional[str]:
    """Map lead_is_new_patient (True/False/None) to 'new'/'returning'/None."""
    value = getattr(conversation, "lead_is_new_patient", None)
    if value is True:
        return "new"
    if value is False:
        return "returning"
    return None


def _load_offered_slots(db, client, conversation) -> List:
    """Re-read the offered slot rows in display order; missing ids drop out."""
    ids = _get_offered_ids(conversation)
    if not ids:
        return []
    rows = appointment_repository.get_slots_by_ids(
        db, client.id, [uuid.UUID(x) for x in ids]
    )
    by_id = {str(r.id): r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _reply_existing_appointment(appointment, settings: CalendarSettings) -> BookingReply:
    """Restate the appointment this conversation already created."""
    tz = ZoneInfo(settings.timezone_name)
    when = (f"{_fmt_day(ensure_utc(appointment.start_datetime).astimezone(tz).date())} at "
            f"{_fmt_time(appointment.start_datetime, settings.timezone_name)}")
    return BookingReply(
        True,
        f"You already have an appointment request for {when}. "
        "If you need to change it, the office can help with that.",
        {"mode": "booking", "state": BookingState.NONE,
         "existing_appointment_id": str(appointment.id)},
    )


def _restate_or_reset(db, client, conversation, settings) -> BookingReply:
    """BOOKED or unknown state reached the handler: restate if an appointment
    exists, otherwise reset cleanly. Either way the state ends at NONE."""
    existing = appointment_repository.get_appointment_by_conversation(
        db, client.id, conversation.id
    )
    _clear_booking_state(conversation)
    db.add(conversation)
    db.commit()
    if existing is not None:
        return _reply_existing_appointment(existing, settings)
    return BookingReply(True, "What day would work best for your appointment?",
                        {"mode": "booking", "state": BookingState.NONE})


# ---------------------------------------------------------------------------
# C1-C structured-action boundary + execution owner.
# ---------------------------------------------------------------------------

def _conversation_locked_for_actions(conversation) -> bool:
    """Locked check for the Calendar action boundary: abuse_locked_until in
    the future. NOTE (Rule 3, documented duplication): chat.py's
    conversation_is_locked() remains the chat-path owner of this rule;
    importing it here would create a route<->service import cycle, so this
    module reads the SAME single persisted column with the SAME comparison.
    A drift test in calendar_tests/test_chat_action_execution.py pins both
    readers to identical verdicts on the same conversation row."""
    until = getattr(conversation, "abuse_locked_until", None)
    if until is None:
        return False
    try:
        return until > datetime.now(timezone.utc)
    except Exception:
        return False


def booking_boundary_state(conversation) -> str:
    """
    Purpose: The single reason-returning durable-boundary reader for
             structured Calendar actions (C1-C locked decision 6).
    Inputs:  conversation row.
    Returns: one of ALL_BOUNDARY_STATES, evaluated in the SAME relative
             order the chat text path enforces its guards: final_closed,
             then locked, then the durable emergency flag.
    Database effects: none (pure read).
    Possible failures: none; missing columns read as unset.
    """
    if bool(getattr(conversation, "final_closed", False)):
        return BOUNDARY_FINAL_CLOSED
    if _conversation_locked_for_actions(conversation):
        return BOUNDARY_LOCKED
    if bool(getattr(conversation, "lead_is_emergency", False)):
        return BOUNDARY_SAFETY_BLOCKED
    return BOUNDARY_NONE


def handle_booking_action(db, client, conversation, choice_id) -> ActionOutcome:
    """
    Purpose: Execute ONE structured Calendar action (C1-C). The single
             owner of choice resolution, freshness, replay defense, and the
             defense-in-depth boundary re-check; the transitions themselves
             run through the SAME handlers the text path uses (Rule 3).
    Inputs:  live session, client row, conversation row, and the opaque
             choice_id from the validated ChatAction. The request's message
             field is an untrusted display echo and is NEVER passed here
             (locked decision 1).
    Returns: ActionOutcome (closed status vocabulary above).
    Database effects: none for not_active / boundary / stale outcomes
        (state is never mutated on a rejection, EXCEPT the safety-blocked
        Calendar-owned cleanup below, mirroring handle_booking_message);
        executed outcomes perform exactly the text path's transactions via
        the shared handlers.
    External effects: notifications only via _finalize_and_reply's single
        success branch, exactly as the text path. This returns BEFORE the
        route persists any transcript rows, preserving the clean-session
        notification entry contract (locked decision 14).
    Possible failures: service/db exceptions propagate to chat.py's logged
        boundary — no broad except here (Rule 4).
    """
    # V4 audit item 1 — REAL database boundary recheck: the route judged
    # the boundary on its OWN ORM snapshot, and a boundary committed by
    # another PostgreSQL session between that first read and this
    # execution is not necessarily visible on that snapshot. Reload the
    # tenant-scoped row (populate_existing under no_autoflush; no lock
    # taken) so the defense-in-depth check below is judged against the
    # NEWEST committed state. This is a point-in-time read: no claim is
    # made that it spans the internally committing service calls later in
    # the dispatch — the post-service reconciliation (V2 item 4 / V3
    # item 1) owns those windows. An unresolvable tenant-scoped row fails
    # CLOSED with the no-replacement stale rejection (no mutation).
    if _reload_conversation(db, client, conversation) is None:
        return ActionOutcome(ACTION_STALE_CHOICE)

    # V2 audit item 3 — defense-in-depth precedence: the durable boundary
    # is re-checked BEFORE feature availability, so a boundary state change
    # between the route's first read and this execution always wins — even
    # when booking_enabled / calendar_actions_enabled is false. NOT_ACTIVE
    # must never mask LOCKED / SAFETY_BLOCKED / FINAL_CLOSED.
    boundary = booking_boundary_state(conversation)
    if boundary == BOUNDARY_SAFETY_BLOCKED:
        # Defense in depth, mirroring handle_booking_message: an emergency-
        # flagged conversation never books, and any lingering dialog state
        # (a live hold included) is cleaned up by the Calendar-owned reset.
        if (_get_state(conversation) != BookingState.NONE
                or getattr(conversation, "booking_selected_slot_id", None) is not None):
            cancel_active_booking(db, client, conversation)
        return ActionOutcome(ACTION_BOUNDARY, boundary=boundary)
    if boundary != BOUNDARY_NONE:
        return ActionOutcome(ACTION_BOUNDARY, boundary=boundary)

    settings = load_calendar_settings(client)
    if not (settings.booking_enabled and settings.calendar_actions_enabled):
        return ActionOutcome(ACTION_NOT_ACTIVE)

    now_utc = client_now(settings).astimezone(ZoneInfo("UTC"))
    state = _get_state(conversation)
    choice = str(choice_id or "").strip()

    if state == BookingState.WAITING_FOR_SLOT_SELECTION:
        return _resolve_selection_action(
            db, client, conversation, settings, choice, now_utc
        )
    if state == BookingState.WAITING_FOR_CONFIRMATION:
        return _resolve_confirmation_action(
            db, client, conversation, settings, choice, now_utc
        )
    if state == BookingState.WAITING_FOR_DATE:
        # C2-A.2: the visual picker's date-stage resolution (its own gate,
        # shape, and open-date revalidation live in the resolver).
        return _resolve_date_action(
            db, client, conversation, settings, choice, now_utc
        )

    # NONE / BOOKED / preference states issue no choices (the date state
    # accepts ONLY the C2-A.2 picker prefix, resolved above). V2 audit
    # item 1 — ONE deliberate exception: a confirm-yes token provably bound
    # to THIS conversation's active appointment's consumed slot is a
    # response-loss retry of an already-successful booking. Restate the
    # appointment (HTTP 200): never re-finalize, never notify, never touch
    # the notification-attempt ledger, never mutate state. Every other
    # token (mismatched slot, confirm-no, random) stays STALE_CHOICE. No
    # replacement set exists without mutating state, so none is returned
    # (widget falls back).
    if choice.startswith(CONFIRM_YES_CHOICE_PREFIX):
        existing = appointment_repository.get_appointment_by_conversation(
            db, client.id, conversation.id
        )
        if (existing is not None
                and choice == CONFIRM_YES_CHOICE_PREFIX + str(existing.slot_id)):
            return ActionOutcome(
                ACTION_EXECUTED,
                reply=_reply_existing_appointment(existing, settings),
                user_label="Yes — book it",
            )
    return ActionOutcome(ACTION_STALE_CHOICE)


def _resolve_date_action(db, client, conversation, settings, choice,
                         now_utc) -> ActionOutcome:
    """WAITING_FOR_DATE (C2-A.2): resolve ONE visual-picker date choice.

    Acceptance requires ALL of:
      1. calendar_picker_enabled strict true (booking_enabled and
         calendar_actions_enabled were already verified by the caller) —
         otherwise the choice could never have been issued and it resolves
         to the indistinguishable STALE_CHOICE, exactly like a forgery;
      2. the server-defined prefix and an EXACT ISO calendar date suffix
         (YYYY-MM-DD; a malformed or impossible date is STALE_CHOICE);
      3. the ONE availability-preview owner (B1) — called with the SAME
         parameters the public widget preview shows (service_key=None) —
         currently classifies the date "open". "past", "full",
         "unavailable", or anything else is STALE_CHOICE with NO state
         mutation: the browser proposes, the server decides.

    On acceptance the date runs the EXACT typed-date transition —
    _validate_and_store_date then _after_date_stored — so precisely one
    date-storage implementation exists (Rule 3). The transcript user_label
    is SERVER-formatted from the accepted date (locked decision D-3); the
    request's message field is never consulted. C2-A.2 ends at the
    existing time-preference stage: no time UI, no slot, hold, booking, or
    notification behavior is added or changed here.
    Database effects: none for every rejection; acceptance performs the
    typed path's own commits. External effects: none.
    """
    if settings.calendar_picker_enabled is not True:
        return ActionOutcome(ACTION_STALE_CHOICE)
    if not choice.startswith(DATE_SELECT_CHOICE_PREFIX):
        return ActionOutcome(ACTION_STALE_CHOICE)

    raw_day = choice[len(DATE_SELECT_CHOICE_PREFIX):]
    if len(raw_day) != 10 or raw_day[4] != "-" or raw_day[7] != "-":
        return ActionOutcome(ACTION_STALE_CHOICE)
    try:
        picked = date.fromisoformat(raw_day)
    except ValueError:
        return ActionOutcome(ACTION_STALE_CHOICE)

    # Server-authoritative revalidation through the single preview owner.
    # fromisoformat guarantees the model's date fields; start==end makes
    # ordering and the 31-day cap unviolable, so the explicit rejection
    # below is an unreachable-by-construction guard kept for Rule 16
    # visibility rather than a broad except.
    try:
        preview = build_availability_preview(
            db, client,
            AvailabilityPreviewRequest(
                start_day=raw_day, end_day=raw_day,
                selected_day=None, service_key=None,
            ),
            ensure_utc(now_utc),
        )
    except PydanticValidationError:
        return ActionOutcome(ACTION_STALE_CHOICE)
    day_state = preview.days[0].state if preview.days else None
    if day_state != "open":
        return ActionOutcome(ACTION_STALE_CHOICE)

    today_local = client_now(settings).date()
    # Human-readable, SERVER-formatted transcript label ("Wednesday,
    # August 14") — de-padded day, never browser text.
    label = f"{picked.strftime('%A, %B')} {picked.day}"

    reply = _validate_and_store_date(
        db, conversation, settings, picked, today_local
    )
    if reply is not None:
        # A clock-edge race (midnight / horizon boundary between the
        # preview read and the store): the typed path's own re-ask reply
        # is returned as the executed outcome — state stays at
        # WAITING_FOR_DATE and nothing advanced (Rule 16: visible, honest).
        return ActionOutcome(ACTION_EXECUTED, reply=reply, user_label=label)

    continuation = _after_date_stored(
        db, client, conversation, settings, picked, now_utc,
        conversation.booking_time_preference,
    )
    return ActionOutcome(ACTION_EXECUTED, reply=continuation, user_label=label)


def _resolve_selection_action(db, client, conversation, settings, choice,
                              now_utc) -> ActionOutcome:
    """WAITING_FOR_SLOT_SELECTION: the choice is either one of the two
    UX-A navigation literals (resolved FIRST - paging, never selection) or
    a member of THIS
    conversation's persisted offer AND the offer must be unexpired
    (booking_offer_expires_at — the existing Patch 2C authority). A valid
    re-submission after an interrupted transition re-runs the hold (ONE
    legitimate held_until refresh — approved C-5 recovery); everything
    else is STALE_CHOICE, with the still-live offer re-issued as the
    replacement set when it exists (no state mutation on rejection)."""
    offered_ids = _get_offered_ids(conversation)
    offer_live = bool(offered_ids) and not _offer_is_expired(conversation, now_utc)

    # UX-A slot-page navigation. The two fixed literals can never collide
    # with a slot UUID, and they resolve BEFORE the membership check:
    # navigation is not selection. The pager owns the WHOLE transition
    # under the V2/V4 row-lock discipline (round-1 audit F1): a live offer
    # pages; an offer that expired in flight runs the SAME Patch 2C
    # recovery the typed path uses (fresh first page, reason=offer_expired)
    # UNDER the row lock; and a request that lost a per-conversation race
    # gets the truthful current-state reply with ZERO mutation. All of
    # those are executed transitions with the tapped label as the
    # transcript row. Only a conversation with NO persisted offer at all
    # treats the tap like any forged/superseded token: STALE_CHOICE.
    if choice in SLOT_NAV_CHOICE_IDS:
        if not offered_ids:
            return ActionOutcome(ACTION_STALE_CHOICE)
        label = (SLOTS_LATER_LABEL if choice == SLOTS_LATER_CHOICE_ID
                 else SLOTS_EARLIER_LABEL)
        reply = _offer_adjacent_slot_page(
            db, client, conversation, settings, now_utc,
            go_later=(choice == SLOTS_LATER_CHOICE_ID),
            entry_offered_ids=offered_ids,
        )
        return ActionOutcome(ACTION_EXECUTED, reply=reply, user_label=label)

    if offer_live and choice in offered_ids:
        offered = _load_offered_slots(db, client, conversation)
        chosen = next((s for s in offered if str(s.id) == choice), None)
        if chosen is not None:
            reply = _hold_offered_slot(
                db, client, conversation, settings, choice, offered, now_utc
            )
            label = "Selected " + _fmt_time(
                chosen.start_datetime, settings.timezone_name
            )
            return ActionOutcome(ACTION_EXECUTED, reply=reply, user_label=label)
        # The offered row vanished (staff edit): fall through to stale.

    replacement = None
    if offer_live:
        live_rows = _load_offered_slots(db, client, conversation)
        if live_rows:
            replacement = _slot_choice_actions(live_rows, settings.timezone_name)
            # F4: the replacement re-shows the CURRENT page, so it keeps
            # that page's truthful pagination controls (read-only owner).
            replacement += _current_page_nav_actions(
                db, client, conversation, settings, now_utc, live_rows
            )
    return ActionOutcome(ACTION_STALE_CHOICE, calendar_actions=replacement)


def _resolve_confirmation_action(db, client, conversation, settings, choice,
                                 now_utc) -> ActionOutcome:
    """WAITING_FOR_CONFIRMATION resolution. Exactly three live choices
    exist, all bound to the persisted selected slot: the slot id itself (a
    pure replay of the completed selection — approved decision 7: restate,
    never call place_hold, never touch held_until), confirm-yes, and
    confirm-no. Anything else is STALE_CHOICE."""
    selected = getattr(conversation, "booking_selected_slot_id", None)
    if selected is None:
        return ActionOutcome(ACTION_STALE_CHOICE)
    sid = str(selected)

    if choice == sid:
        # V2 audit item 2: a replay restatement is only truthful while the
        # selected slot still has a LIVE hold owned by this conversation.
        # Expired, released, booked, missing, or foreign-owned holds make
        # the choice stale — with NO replacement confirmation buttons,
        # because those buttons would advertise a confirmation that can no
        # longer succeed. held_until is never refreshed or extended here.
        if not _selected_hold_is_live(db, client, conversation, now_utc):
            return ActionOutcome(ACTION_STALE_CHOICE)
        reply = _restate_pending_confirmation(db, client, conversation, settings)
        if reply is None:
            return ActionOutcome(ACTION_STALE_CHOICE)
        return ActionOutcome(
            ACTION_EXECUTED, reply=reply,
            user_label="Selected the offered time",
        )
    if choice == CONFIRM_YES_CHOICE_PREFIX + sid:
        reply = _finalize_and_reply(
            db, client, conversation, settings, selected, now_utc,
            restate_after_hold_loss=True,
        )
        return ActionOutcome(
            ACTION_EXECUTED, reply=reply, user_label="Yes — book it",
        )
    if choice == CONFIRM_NO_CHOICE_PREFIX + sid:
        reply = _decline_and_restart(db, client, conversation, settings, selected)
        return ActionOutcome(
            ACTION_EXECUTED, reply=reply,
            user_label="No — pick another time",
        )

    # V2 audit item 2: replacement confirmation choices are re-issued ONLY
    # over a live owned hold (same rule as the replay above). A valid
    # confirm-yes / confirm-no token above still enters its transition
    # owner unchanged — finalize_booking keeps ownership of the normal
    # hold-expired / hold-lost re-offer outcome.
    if not _selected_hold_is_live(db, client, conversation, now_utc):
        return ActionOutcome(ACTION_STALE_CHOICE)
    return ActionOutcome(
        ACTION_STALE_CHOICE,
        calendar_actions=_confirm_choice_actions(selected),
    )


def _restate_pending_confirmation(db, client, conversation, settings):
    """Replay short-circuit (locked decision 7): restate the pending
    confirmation from persisted state + the slot row. NO service call, NO
    state change, held_until untouched (the fail-first test pins the value
    byte-identical). Returns None when the slot row no longer exists for
    this tenant (the choice is then reported stale)."""
    slot_id = getattr(conversation, "booking_selected_slot_id", None)
    rows = appointment_repository.get_slots_by_ids(db, client.id, [slot_id])
    if not rows:
        return None
    slot = rows[0]
    tz = ZoneInfo(settings.timezone_name)
    when = (f"{_fmt_day(ensure_utc(slot.start_datetime).astimezone(tz).date())} at "
            f"{_fmt_time(slot.start_datetime, settings.timezone_name)}")
    reply_meta = {"mode": "booking",
                  "state": BookingState.WAITING_FOR_CONFIRMATION}
    if settings.calendar_actions_enabled:
        reply_meta["calendar_actions"] = _confirm_choice_actions(slot_id)
    return BookingReply(
        True,
        f"To confirm: {conversation.lead_name} on {when}. Is that correct?",
        reply_meta,
    )


# ---------------------------------------------------------------------------
# V2 (C1-C audit items 2 and 4): confirmation-stage hold validity and per-
# conversation concurrency reconciliation. place_hold / release_hold /
# finalize_booking commit INTERNALLY, so any conversation-row lock taken
# before one of those calls is released by that internal commit and is not
# authoritative afterward. The rule enforced by these helpers: after every
# internally committing service call, reacquire the tenant-scoped row under
# SELECT ... FOR UPDATE, reload its newest state, and revalidate BEFORE
# changing any conversation field. A request that began from an older ORM
# snapshot must never overwrite a newer committed state.
# ---------------------------------------------------------------------------

def _selected_hold_is_live(db, client, conversation, now_utc) -> bool:
    """
    Purpose: Confirmation-stage hold validity (V2 audit item 2). Verifies —
             WITHOUT refreshing or extending anything — that the persisted
             selected slot still exists for this tenant, is HELD, is held
             by THIS conversation, and has held_until in the future.
    Inputs:  live session, client row, conversation row, now_utc.
    Returns: True only when all four conditions hold.
    Database effects: none (pure tenant-scoped read; held_until untouched).
    Possible failures: none; a missing row or unset field reads as not-live.
    """
    slot_id = getattr(conversation, "booking_selected_slot_id", None)
    if slot_id is None:
        return False
    rows = appointment_repository.get_slots_by_ids(db, client.id, [slot_id])
    if not rows:
        return False
    slot = rows[0]
    if slot.status != SlotStatus.HELD:
        return False
    if slot.held_by_conversation_id != conversation.id:
        return False
    if slot.held_until is None:
        return False
    return ensure_utc(slot.held_until) > ensure_utc(now_utc)


def _lock_conversation_row(db, client, conversation):
    """
    Purpose: Reacquire the tenant-scoped conversation row under
             SELECT ... FOR UPDATE and RELOAD its newest committed state
             (V2 audit item 4). Called AFTER an internally committing
             service call and BEFORE any conversation mutation, so the
             mutation decision is made against the newest row — never a
             stale ORM snapshot.
    Inputs:  live session, client row, the in-session conversation object.
    Returns: the SAME identity-mapped conversation object with refreshed
             state, or None if the row no longer exists for this tenant.
    Database effects: takes the PostgreSQL row lock; the CALLER's next
        commit (or rollback) releases it. No writes happen here.
    Possible failures: db exceptions propagate to the caller's boundary.
    """
    # no_autoflush: this helper must never flush a pending stale mutation
    # to the database as a side effect of its own SELECT.
    with db.no_autoflush:
        return (
            db.query(Conversation)
            .filter(
                Conversation.id == conversation.id,
                Conversation.client_id == client.id,
            )
            .with_for_update()
            .populate_existing()
            .first()
        )


def _reload_conversation(db, client, conversation) -> None:
    """
    Purpose: Non-locking populate_existing reload (V2 audit item 4):
             refresh the in-session conversation object to the newest
             committed row. No lock is taken. V4 audit item 1 also uses
             this as the REAL-database boundary reload at action entry.
    Returns: the SAME identity-mapped conversation object with refreshed
             state, or None if the tenant-scoped row can no longer be
             resolved (deleted or cross-tenant) — callers fail closed.
    Database effects: none (SELECT only).
    Possible failures: db exceptions propagate to the caller's boundary.
    """
    with db.no_autoflush:
        return (
            db.query(Conversation)
            .filter(
                Conversation.id == conversation.id,
                Conversation.client_id == client.id,
            )
            .populate_existing()
            .first()
        )


def _truthful_current_state_reply(db, client, conversation, settings,
                                  now_utc) -> BookingReply:
    """
    Purpose: Build a truthful reply for a request that LOST a per-
             conversation race (V2 audit item 4). The caller must have just
             reloaded the conversation row; this function mutates NOTHING —
             the surviving request owns the state.
    Inputs:  live session, client row, freshly reloaded conversation,
             settings, now_utc.
    Returns: BookingReply describing the CURRENT persisted state. An active
             appointment outranks everything; a live owned confirmation is
             restated (confirmation choices only over a live hold — audit
             item 2); a live persisted offer is re-shown; otherwise the
             question matching the persisted state is repeated, with the
             persisted state reported unchanged in the reply meta.
    Database effects: none (reads only).
    Possible failures: db exceptions propagate to the caller's boundary.
    """
    existing = appointment_repository.get_appointment_by_conversation(
        db, client.id, conversation.id
    )
    if existing is not None:
        return _reply_existing_appointment(existing, settings)

    state = _get_state(conversation)
    if state == BookingState.WAITING_FOR_CONFIRMATION:
        if _selected_hold_is_live(db, client, conversation, now_utc):
            reply = _restate_pending_confirmation(db, client, conversation, settings)
            if reply is not None:
                return reply
        # Dead hold: never advertise confirmation choices over it (item 2).
        # The text handler for this state accepts a new day, so the day
        # question is answerable exactly as asked.
        return BookingReply(
            True, "What day would work best for your appointment?",
            {"mode": "booking", "state": state},
        )
    if state == BookingState.WAITING_FOR_TIME_PREFERENCE:
        # PACKAGE B v1.0.1: this state is LEGACY-ONLY (persisted before the
        # morning/afternoon stage was removed), so the restate never
        # advertises that question and never carries the removed
        # time-preference signal. Mutation stays forbidden here (the
        # surviving request owns the state — the contract above), so the
        # reply cannot BE the slot offer (offering records offer state).
        # Instead it truthfully states what the resolver does with ANY next
        # message: with a readable persisted day, the exact-slot offer for
        # that day; without one, the day question — both answerable exactly
        # as asked (the dead-hold precedent above).
        day = _get_pref_date(conversation)
        if day is not None:
            return BookingReply(
                True,
                f"You’re set for {_fmt_day(day)} — reply with anything and "
                "I’ll show the exact open times.",
                {"mode": "booking", "state": state},
            )
        return BookingReply(
            True, "What day would work best for your appointment?",
            {"mode": "booking", "state": state},
        )
    if state == BookingState.WAITING_FOR_SLOT_SELECTION:
        if not _offer_is_expired(conversation, now_utc):
            rows = _load_offered_slots(db, client, conversation)
            if rows:
                # Re-show the SAME persisted live offer (approved wording
                # from _offer_slots, day prefix reused when the stored day
                # is readable); no offer fields are rewritten.
                menu = _slot_menu(rows, settings.timezone_name)
                day = _get_pref_date(conversation)
                prefix = (f"Here’s what’s open on {_fmt_day(day)}: "
                          if day is not None else "")
                reply_meta = {"mode": "booking", "state": state,
                              "offered_slots": _get_offered_ids(conversation)}
                if settings.calendar_actions_enabled:
                    reply_meta["calendar_actions"] = _slot_choice_actions(
                        rows, settings.timezone_name
                    )
                    # F4: restore this page's truthful pagination controls
                    # (single read-only nav-truth owner; recomputed from
                    # CURRENT availability; zero mutation).
                    reply_meta["calendar_actions"] += (
                        _current_page_nav_actions(
                            db, client, conversation, settings, now_utc,
                            rows,
                        )
                    )
                    # C2-A.3: same gated slot_selection signal as
                    # _offer_slots — attached only alongside actions.
                    picker_signal = _picker_stage_signal(
                        settings, PICKER_STAGE_SLOT_SELECTION
                    )
                    if picker_signal is not None:
                        reply_meta["calendar_picker"] = picker_signal
                return BookingReply(
                    True, f"{prefix}{menu}. Which works best?", reply_meta,
                )
    # WAITING_FOR_DATE / NONE / anything else without an appointment: the
    # day question, with the persisted state reported unchanged. V2 defect
    # 1: the picker signal is attached ONLY when the surviving persisted
    # state genuinely is WAITING_FOR_DATE — a race won by another state
    # must not advertise a date picker it cannot consume.
    if state == BookingState.WAITING_FOR_DATE:
        return BookingReply(
            True, "What day would work best for your appointment?",
            _date_stage_meta(settings),
        )
    return BookingReply(
        True, "What day would work best for your appointment?",
        {"mode": "booking", "state": state},
    )
