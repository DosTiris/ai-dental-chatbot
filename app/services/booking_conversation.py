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
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.calendar_models import BookingState

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
# Formatting helpers (client-timezone display only; no logic).
# ---------------------------------------------------------------------------

def _fmt_day(d: date) -> str:
    """'Thursday, July 16' — no leading zero on the day."""
    return d.strftime("%A, %B %d").replace(" 0", " ")


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
    return _handle_start(db, client, conversation, settings, user_text, now_utc)


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

def _handle_start(db, client, conversation, settings, user_text, now_utc) -> BookingReply:
    """NONE -> WAITING_FOR_DATE, or straight to WAITING_FOR_TIME_PREFERENCE
    when the opening message already named a day ('anything thursday?')."""
    # Duplicate defense: one appointment per conversation (Rule 10).
    existing = appointment_repository.get_appointment_by_conversation(
        db, client.id, conversation.id
    )
    if existing is not None:
        return _reply_existing_appointment(existing, settings)

    today_local = client_now(settings).date()
    parsed_date = parse_preferred_date(user_text, today_local)

    if parsed_date is not None:
        reply = _validate_and_store_date(
            db, conversation, settings, parsed_date, today_local
        )
        if reply is not None:
            return reply
        conversation.booking_state = BookingState.WAITING_FOR_TIME_PREFERENCE
        db.add(conversation)
        db.commit()
        return BookingReply(
            True,
            f"Great — {_fmt_day(parsed_date)}. Do you prefer morning or afternoon?",
            {"mode": "booking", "state": conversation.booking_state},
        )

    conversation.booking_state = BookingState.WAITING_FOR_DATE
    db.add(conversation)
    db.commit()
    return BookingReply(
        True,
        "What day would work best for your appointment?",
        {"mode": "booking", "state": conversation.booking_state},
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
            {"mode": "booking", "state": BookingState.WAITING_FOR_DATE},
        )

    reply = _validate_and_store_date(db, conversation, settings, parsed_date, today_local)
    if reply is not None:
        return reply

    # 'friday morning' answers two questions at once — honor both.
    preference = parse_time_preference(user_text)
    if preference is not None:
        conversation.booking_time_preference = preference
        return _offer_slots(db, client, conversation, settings, now_utc)

    conversation.booking_state = BookingState.WAITING_FOR_TIME_PREFERENCE
    db.add(conversation)
    db.commit()
    return BookingReply(
        True,
        f"Got it — {_fmt_day(parsed_date)}. Do you prefer morning or afternoon?",
        {"mode": "booking", "state": conversation.booking_state},
    )


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
            {"mode": "booking", "state": BookingState.WAITING_FOR_DATE},
        )

    conversation.booking_state = BookingState.WAITING_FOR_DATE
    conversation.booking_preferred_date = parsed_date.isoformat()
    return None


def _handle_time_preference(db, client, conversation, settings, user_text, now_utc) -> BookingReply:
    """WAITING_FOR_TIME_PREFERENCE: classify morning/afternoon/evening/any.
    A brand-new day in the message is honored (patient changed direction)."""
    today_local = client_now(settings).date()
    new_date = parse_preferred_date(user_text, today_local)
    if new_date is not None:
        reply = _validate_and_store_date(db, conversation, settings, new_date, today_local)
        if reply is not None:
            return reply

    preference = parse_time_preference(user_text)
    if preference is None and new_date is None:
        return BookingReply(
            True,
            "Do you prefer morning or afternoon? You can also say "
            "\u201cany time\u201d.",
            {"mode": "booking", "state": BookingState.WAITING_FOR_TIME_PREFERENCE},
        )
    if preference is None:
        # They gave a new day but no preference; keep asking the one question.
        conversation.booking_state = BookingState.WAITING_FOR_TIME_PREFERENCE
        db.add(conversation)
        db.commit()
        return BookingReply(
            True,
            f"Okay — {_fmt_day(new_date)}. Morning or afternoon?",
            {"mode": "booking", "state": conversation.booking_state},
        )

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
    """Fetch availability for the stored day+preference and present up to
    max_offered_slots numbered options, or suggest other days."""
    day = _get_pref_date(conversation)
    if day is None:  # Corrupt/missing date: fall back to asking the day again.
        conversation.booking_state = BookingState.WAITING_FOR_DATE
        db.add(conversation)
        db.commit()
        return BookingReply(True, "What day would work best for you?",
                            {"mode": "booking", "state": BookingState.WAITING_FOR_DATE})

    preference = conversation.booking_time_preference or PREF_ANY
    slots = availability_service.get_available_slots(
        db, client.id, settings, day, preference, now_utc,
        service_key=(conversation.lead_reason or None),
    )
    relaxed = False
    if not slots and preference != PREF_ANY:
        # Same day, other times: better than a dead end, and clearly labeled.
        slots = availability_service.get_available_slots(
            db, client.id, settings, day, PREF_ANY, now_utc,
            service_key=(conversation.lead_reason or None),
        )
        relaxed = bool(slots)

    if not slots:
        return _suggest_other_days(db, client, conversation, settings, day, now_utc)

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
    return BookingReply(
        True,
        f"{prefix}{menu}. Which works best?",
        {"mode": "booking", "state": conversation.booking_state,
         "offered_slots": conversation.booking_offered_slot_ids},
    )


def _suggest_other_days(db, client, conversation, settings, day, now_utc) -> BookingReply:
    """No openings on the requested day: offer up to 3 nearby days that have
    real availability, and go back to WAITING_FOR_DATE."""
    days = availability_service.find_days_with_availability(
        db, client.id, settings, day, now_utc
    )
    conversation.booking_state = BookingState.WAITING_FOR_DATE
    conversation.booking_offered_slot_ids = None
    conversation.booking_offer_expires_at = None            # Patch 2C
    conversation.booking_effective_time_preference = None   # Patch 2C
    db.add(conversation)
    db.commit()

    if days:
        options = ", ".join(_fmt_day(d) for d in days)
        text = (f"I don\u2019t see openings on {_fmt_day(day)}. "
                f"The nearest days with availability are: {options}. "
                "Would any of those work?")
    else:
        text = (f"I don\u2019t see online openings around {_fmt_day(day)}. "
                "The office can help directly — would you like to try a "
                "different week?")
    return BookingReply(True, text,
                        {"mode": "booking", "state": BookingState.WAITING_FOR_DATE})


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
        conversation.booking_offered_slot_ids = None
        conversation.booking_offer_expires_at = None
        conversation.booking_effective_time_preference = None
        db.add(conversation)
        reply = _offer_slots(db, client, conversation, settings, now_utc)
        reply.meta["reason"] = "offer_expired"
        return reply

    tz = ZoneInfo(settings.timezone_name)
    pairs: List[Tuple[str, datetime]] = [
        (str(s.id), ensure_utc(s.start_datetime).astimezone(tz)) for s in offered
    ]
    chosen_id = match_slot_selection(user_text, pairs)

    if chosen_id is None:
        menu = _slot_menu(offered, settings.timezone_name) if offered else ""
        text = (f"Just to be sure I pick the right one — {menu}. "
                "You can reply 1, 2, or 3." if menu else
                "Let me pull up fresh times. What day works best?")
        if not menu:  # Offered slots vanished (staff edits); restart cleanly.
            conversation.booking_state = BookingState.WAITING_FOR_DATE
            db.add(conversation)
            db.commit()
        return BookingReply(True, text,
                            {"mode": "booking", "state": _get_state(conversation)})

    hold = appointment_hold_service.place_hold(
        db, client.id, uuid.UUID(chosen_id), conversation.id,
        settings=settings,
        time_preference=_revalidation_preference(conversation),
        service_key=(conversation.lead_reason or None),
        now_utc=now_utc,
    )
    if not hold.success:
        # Lost the race (Rule 9's two-patients case) OR current policy now
        # rejects the slot (Patch 2C): say so accurately, re-offer fresh.
        return _reoffer_after_conflict(
            db, client, conversation, settings, now_utc,
            ineligible=(hold.reason == "slot_ineligible"),
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
    return BookingReply(
        True,
        f"To confirm: {conversation.lead_name} on {when}. Is that correct?",
        {"mode": "booking", "state": conversation.booking_state,
         "held_until": hold.held_until.isoformat() if hold.held_until else None},
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
        return BookingReply(
            True,
            "Should I book that time for you — yes or no?",
            {"mode": "booking", "state": BookingState.WAITING_FOR_CONFIRMATION},
        )
    if decision is False:
        if slot_id is not None:
            appointment_hold_service.release_hold(db, client.id, slot_id, conversation.id)
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
            {"mode": "booking", "state": BookingState.WAITING_FOR_DATE},
        )

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


def _finalize_and_reply(db, client, conversation, settings, slot_id, now_utc) -> BookingReply:
    """The single place where WAITING_FOR_CONFIRMATION becomes BOOKED —
    which is why notifications here can never fire twice (Rule 10)."""
    if slot_id is None:  # State corruption: never invent a slot; restart.
        conversation.booking_state = BookingState.WAITING_FOR_DATE
        db.add(conversation)
        db.commit()
        return BookingReply(True, "Let me pull up times again — what day works best?",
                            {"mode": "booking", "state": BookingState.WAITING_FOR_DATE})

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
        _clear_booking_state(conversation)
        db.add(conversation)
        db.commit()
        return _reply_existing_appointment(result.appointment, settings)

    if not result.success:
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
