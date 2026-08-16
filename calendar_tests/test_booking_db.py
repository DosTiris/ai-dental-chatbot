# calendar_tests/test_booking_db.py
#
# Database regression tests for the calendar MVP. Requires a throwaway
# Postgres via TEST_DATABASE_URL — see conftest.py header for setup.
#
# Coverage map (Rule 11 checklist items marked ✔ where tested here):
#   ✔ hold placement / hold conflict (two patients, same slot)
#   ✔ expired hold takeover + finalize rejection of expired holds
#   ✔ double-booking prevention under a real concurrent race (threads)
#   ✔ finalize recheck (hold lost / already booked by conversation)
#   ✔ duplicate prevention (one appointment per conversation)
#   ✔ cancellation frees the slot
#   ✔ client isolation (office B cannot touch office A's slot)
#   ✔ end-to-end booking conversation incl. notification failure visibility
#   ✔ emergency conversations cannot book (defense-in-depth layer)
#   ✔ PATCH 1: concurrent same-conversation finalize on two DIFFERENT slots
#     (both provably past the pre-check before either inserts)
#   ✔ PATCH 1: concurrent same-slot finalize (service level, lock path)
#   ✔ PATCH 1: per-slot unique index enforcement when the slot lock is
#     bypassed (sequential database-enforcement test, not a race)
#   ✔ PATCH 1: deterministic IntegrityError -> BookingResult mapping
#   ✔ PATCH 1: cancel -> rebook by a DIFFERENT conversation
#   ✔ PATCH 1: cancel -> rebook by the SAME conversation
#   ✔ PATCH 2B: fall-back day's 25th hour visible to availability queries
#   ✔ PATCH 2B: admin slot + appointment listings use the same DST-safe
#     half-open windows (incl. a multi-day range crossing the transition)
#   ✔ PATCH 2C: current policy revalidated UNDER THE SLOT LOCK at hold and
#     finalization (notice / horizon / service / preference), with atomic
#     hold release and no appointment on finalize-time ineligibility
#   ✔ PATCH 2C: relaxed PREF_ANY offers survive hold through finalization
#   ✔ PATCH 2C: offer expiration boundary (valid before, expired AT and
#     after expires_at, NULL-expiry regression) with safe replacement
#   ✔ PATCH 4: staff confirmation — pending -> confirmed with confirmed_at,
#     idempotent re-confirm (timestamp preserved byte-for-byte), rejection of
#     cancelled/completed/no_show, tenant-isolated 404 behavior, proof every
#     failed transition is mutation-free and NO notification channel is ever
#     invoked, confirmed_at stays NULL for auto-confirmed appointments,
#     confirm->cancel ordering both ways, a genuinely CONCURRENT (threaded)
#     double-confirm with exactly one transition, and the admin confirm
#     route's status mapping + confirmed_at exposure in both views
#   ✔ PATCH 7: cancellation lifecycle allow-list — completed/no_show
#     rejected mutation-free with slot untouched (parametrized), repeat
#     cancel mutation-free, cross-tenant cancel indistinguishable from a
#     missing id against a REAL foreign appointment, threaded double-cancel
#     with exactly one transition, threaded cancel-vs-confirm with only the
#     two legal outcome pairs, commit-failure rollback leaving no partial
#     mutation, and the admin cancel route's exact status/wording mapping

import threading
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from calendar_tests.conftest import make_conversation, requires_db
from app.services.calendar_settings_service import ensure_utc

UTC = ZoneInfo("UTC")
pytestmark = requires_db


def _now():
    return datetime.now(UTC)


def _settings(client):
    from app.services.calendar_settings_service import load_calendar_settings
    return load_calendar_settings(client)


def _make_slot(db, client, hours_from_now=48.0):
    from app.repositories.appointment_repository import create_slot
    start = _now() + timedelta(hours=hours_from_now)
    slot = create_slot(db, client.id, start, start + timedelta(minutes=45))
    db.commit()
    return slot


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------

def test_hold_then_conflict(db, client_row, conversation_row):
    from app.services.appointment_hold_service import place_hold
    slot = _make_slot(db, client_row)
    other = make_conversation(db, client_row)

    first = place_hold(db, client_row.id, slot.id, conversation_row.id,
                       settings=_settings(client_row), time_preference="any",
                       service_key=None, now_utc=_now())
    assert first.success and first.reason == "ok"

    second = place_hold(db, client_row.id, slot.id, other.id,
                       settings=_settings(client_row), time_preference="any",
                       service_key=None, now_utc=_now())
    assert not second.success and second.reason == "slot_taken"


def test_expired_hold_can_be_taken_over_but_not_finalized(db, client_row, conversation_row):
    from app.calendar_models import SlotStatus
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import finalize_booking
    slot = _make_slot(db, client_row)
    other = make_conversation(db, client_row)

    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                       settings=_settings(client_row), time_preference="any",
                       service_key=None, now_utc=_now()).success
    # Force expiry (simulates the patient walking away for 6+ minutes).
    slot.held_until = _now() - timedelta(minutes=1)
    db.commit()

    # The original holder can no longer finalize an expired hold.
    result = finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=_settings(client_row), now_utc=_now(), time_preference="any", service_key=None, patient_name="Kevin", patient_phone="516-555-1234",
        patient_email=None, new_or_returning=None, reason=None, urgency="routine",
    )
    assert not result.success and result.reason == "hold_expired"

    # Another patient can take the slot over (lazy reclaim — Rule 10:
    # a slot can never be left permanently held).
    takeover = place_hold(db, client_row.id, slot.id, other.id,
                       settings=_settings(client_row), time_preference="any",
                       service_key=None, now_utc=_now())
    assert takeover.success
    db.refresh(slot)
    assert slot.status == SlotStatus.HELD
    assert slot.held_by_conversation_id == other.id


def test_release_hold_requires_ownership(db, client_row, conversation_row):
    from app.calendar_models import SlotStatus
    from app.services.appointment_hold_service import place_hold, release_hold
    slot = _make_slot(db, client_row)
    other = make_conversation(db, client_row)

    place_hold(db, client_row.id, slot.id, conversation_row.id,
                       settings=_settings(client_row), time_preference="any",
                       service_key=None, now_utc=_now())
    stolen = release_hold(db, client_row.id, slot.id, other.id)
    assert not stolen.success and stolen.reason == "not_owner"

    freed = release_hold(db, client_row.id, slot.id, conversation_row.id)
    assert freed.success
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE
    assert slot.held_until is None and slot.held_by_conversation_id is None


# ---------------------------------------------------------------------------
# Double-booking: sequential AND genuinely concurrent
# ---------------------------------------------------------------------------

def test_concurrent_hold_race_exactly_one_winner(engine, db, client_row, conversation_row):
    """Two real sessions in two threads race place_hold on the SAME slot.
    Postgres row locking must let exactly one win (Rule 9's core case)."""
    from app.database import SessionLocal
    from app.services.appointment_hold_service import place_hold
    slot = _make_slot(db, client_row)
    other = make_conversation(db, client_row)
    settings = _settings(client_row)
    slot_id, client_id = slot.id, client_row.id
    results = {}

    def attempt(name, conversation_id):
        session = SessionLocal()
        try:
            results[name] = place_hold(session, client_id, slot_id,
                                       conversation_id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now())
        finally:
            session.close()

    threads = [
        threading.Thread(target=attempt, args=("a", conversation_row.id)),
        threading.Thread(target=attempt, args=("b", other.id)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outcomes = sorted([results["a"].success, results["b"].success])
    assert outcomes == [False, True], f"expected one winner, got {results}"


def test_finalize_rechecks_hold_ownership(db, client_row, conversation_row):
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import finalize_booking
    slot = _make_slot(db, client_row)
    other = make_conversation(db, client_row)

    place_hold(db, client_row.id, slot.id, conversation_row.id,
                       settings=_settings(client_row), time_preference="any",
                       service_key=None, now_utc=_now())
    # The NON-holder tries to finalize: must fail the in-lock recheck.
    result = finalize_booking(
        db, client_row.id, slot.id, other.id,
        settings=_settings(client_row), now_utc=_now(),
        time_preference="any", service_key=None,
        patient_name="Mallory", patient_phone="000", patient_email=None,
        new_or_returning=None, reason=None, urgency="routine",
    )
    assert not result.success and result.reason == "hold_lost"


def test_one_appointment_per_conversation(db, client_row, conversation_row):
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import finalize_booking
    slot1 = _make_slot(db, client_row, 48)
    slot2 = _make_slot(db, client_row, 72)
    settings = _settings(client_row)

    place_hold(db, client_row.id, slot1.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now())
    first = finalize_booking(
        db, client_row.id, slot1.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        time_preference="any", service_key=None,
        patient_name="Kevin", patient_phone="516-555-1234", patient_email=None,
        new_or_returning="new", reason="cleaning/checkup", urgency="routine",
    )
    assert first.success
    assert first.appointment.status == AppointmentStatus.PENDING  # staff-confirm ON
    db.refresh(slot1)
    assert slot1.status == SlotStatus.BOOKED

    # A second booking attempt from the SAME conversation returns the
    # existing appointment instead of creating a duplicate (Rule 10).
    place_hold(db, client_row.id, slot2.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now())
    second = finalize_booking(
        db, client_row.id, slot2.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        time_preference="any", service_key=None,
        patient_name="Kevin", patient_phone="516-555-1234", patient_email=None,
        new_or_returning="new", reason="cleaning/checkup", urgency="routine",
    )
    assert not second.success
    assert second.reason == "already_booked_by_conversation"
    assert second.appointment.id == first.appointment.id


# ---------------------------------------------------------------------------
# Cancellation + client isolation
# ---------------------------------------------------------------------------

def test_cancellation_frees_slot(db, client_row, conversation_row):
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import cancel_appointment, finalize_booking
    slot = _make_slot(db, client_row)
    settings = _settings(client_row)

    place_hold(db, client_row.id, slot.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now())
    booked = finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        time_preference="any", service_key=None,
        patient_name="Kevin", patient_phone="516-555-1234", patient_email=None,
        new_or_returning=None, reason=None, urgency="routine",
    )
    result = cancel_appointment(db, client_row.id, booked.appointment.id)
    assert result.success
    assert result.appointment.status == AppointmentStatus.CANCELLED
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE  # rebookable again

    again = cancel_appointment(db, client_row.id, booked.appointment.id)
    assert not again.success and again.reason == "already_cancelled"


def test_client_isolation(db, client_row, conversation_row):
    """Office B must not be able to see, hold, or cancel office A's calendar."""
    from app.models import Client
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import cancel_appointment
    slot = _make_slot(db, client_row)
    office_b = Client(id=uuid.uuid4(), practice_name="Other Dental",
                      api_key=f"key-{uuid.uuid4()}", active=True)
    db.add(office_b)
    db.commit()

    foreign_hold = place_hold(db, office_b.id, slot.id, conversation_row.id,
                       settings=_settings(client_row), time_preference="any",
                       service_key=None, now_utc=_now())
    assert not foreign_hold.success and foreign_hold.reason == "slot_missing"

    foreign_cancel = cancel_appointment(db, office_b.id, uuid.uuid4())
    assert not foreign_cancel.success


# ---------------------------------------------------------------------------
# End-to-end conversation (state machine over a real database)
# ---------------------------------------------------------------------------

def test_full_booking_conversation(db, client_row, conversation_row, monkeypatch):
    """Scripted dialog: date -> preference -> selection -> confirm -> booked.
    Outbound providers are stubbed to FAIL so none can possibly run. Patch 2D
    (Senior Audit Critical #3): patient SMS is disabled by product policy —
    the booking must succeed with NO patient_sms attempt and NO patient_sms
    notify_error entry, while the office channels' honest outcomes are still
    recorded on the appointment (Rule 16)."""
    from app.calendar_models import BookingState
    from app.services import booking_conversation

    slot = _make_slot(db, client_row, hours_from_now=48)
    local_day = slot.start_datetime.astimezone(
        ZoneInfo("America/New_York")).strftime("%B %d").lower()

    def say(text):
        return booking_conversation.handle_booking_message(
            db, client_row, conversation_row, text)

    r1 = say("I'd like to book an appointment")
    assert r1.handled and conversation_row.booking_state == BookingState.WAITING_FOR_DATE

    # PACKAGE B: the stored date proceeds DIRECTLY to the exact-slot offer —
    # the morning/afternoon turn is removed from every Calendar date path.
    r2 = say(local_day)  # e.g. "july 13"
    assert r2.handled and conversation_row.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conversation_row.booking_offered_slot_ids

    r4 = say("the first one")
    assert r4.handled and conversation_row.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    assert "Is that correct?" in r4.text

    # Make every outbound message fail (no Twilio creds in tests anyway).
    monkeypatch.setattr(booking_conversation.notification_service, "_send_sms",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sms down")))
    monkeypatch.setattr(booking_conversation.notification_service, "_send_email",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("email down")))

    r5 = say("yes")
    assert r5.handled and r5.meta.get("booked") is True
    assert "request" in r5.text.lower()  # staff-confirm wording, not "confirmed"
    assert conversation_row.booking_state == BookingState.NONE  # state cleared

    from app.calendar_models import Appointment
    appointment = db.query(Appointment).filter(
        Appointment.conversation_id == conversation_row.id).one()
    assert appointment.patient_name == "Kevin Alvarado"
    assert appointment.patient_sms_sent is False
    # Patch 2D (Senior Audit Critical #3): the patient-SMS channel is policy-
    # disabled and never attempted, so its absence is honest — no patient_sms
    # entry may appear. This fixture client has no office contacts configured,
    # so both office channels record their 'skipped' outcome exactly as before.
    assert appointment.notify_error and "office_sms" in appointment.notify_error
    assert "office_email" in appointment.notify_error
    assert "patient_sms" not in appointment.notify_error

    # Booking again in the same conversation restates, never duplicates.
    r6 = say("book me again")
    assert r6.handled and "already have an appointment" in r6.text.lower()


def test_emergency_conversation_cannot_book(db, client_row, conversation_row):
    from app.calendar_models import BookingState
    from app.services import booking_conversation

    _make_slot(db, client_row)
    conversation_row.booking_state = BookingState.WAITING_FOR_DATE
    conversation_row.lead_is_emergency = True
    db.commit()

    reply = booking_conversation.handle_booking_message(
        db, client_row, conversation_row, "tomorrow")
    assert reply.handled is False                      # chat.py's emergency flow answers
    assert conversation_row.booking_state == BookingState.NONE  # state wiped


# ---------------------------------------------------------------------------
# HOLD-CONFLICT race (Rule 9, dialog level) -- deterministic pinned-clock form.
#
# ROOT CAUSE OF THE PREVIOUS FLAKE (owner-local 2026-08-06 failure): the
# fixture used wall-clock-relative offsets (+48h / +49h). Run between 11 PM
# and midnight America/New_York, those two instants straddle the client-local
# midnight boundary, so the two offered slots land on DIFFERENT local dates.
# After patient A snipes slot A, the re-offer for the STORED preferred date
# (slot A's local date) finds nothing -- slot A is held and slot B belongs to
# the next local day -- so _reoffer_after_conflict's _offer_slots call takes
# the _suggest_other_days path ("I don't see matching openings on ...")
# WITHOUT the "just taken" apology (the apology is prefixed only when the
# re-offer actually carries slots), and the test failed before exercising the
# race it exists to prove. Weekday/office-hours play NO role for this
# fixture: client_row configures no office_hours, and slot eligibility is
# slot-row-based plus notice(60m)/horizon(30d)/preference/service only.
#
# CORRECTION (test-only): reuse the file's existing deterministic owners --
# _fixed_clock (pins booking_conversation.client_now, the dialog's single
# clock read) and _slot_row (a slot at an EXACT aware-UTC instant) -- and
# publish BOTH offered slots on the SAME client-local calendar day: future,
# beyond minimum notice, inside the horizon, AVAILABLE, and thus actually
# offerable, which is what the race requires (after slot A is sniped, slot B
# must remain a valid same-day alternative). The pinned scenarios prove the
# representative boundaries the fixture must be immune to.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["weekday_noon", "weekend_noon",
                                      "late_night_rollover"])
def test_slot_taken_between_display_and_selection(db, client_row,
                                                  conversation_row,
                                                  monkeypatch, scenario):
    """Patient B picks a slot that patient A held after it was displayed to B:
    Mia apologizes and re-offers instead of failing (Rule 9 race, dialog
    level). Pinned-clock scenarios: weekday noon, weekend noon, and the
    23:30-local day-rollover window that broke the wall-clock fixture."""
    from app.calendar_models import BookingState
    from app.services import booking_conversation
    from app.services.appointment_hold_service import place_hold

    # Pinned aware-UTC "now" per scenario (NY = UTC-4 in August 2026), with
    # slot A at 10:00 NY two local days later and slot B one hour after --
    # one shared NY calendar date by construction (asserted below).
    scenarios = {
        # Wed 2026-08-19 12:00 NY -> slots Fri 2026-08-21 10:00/11:00 NY.
        "weekday_noon": (datetime(2026, 8, 19, 16, 0, tzinfo=UTC),
                         datetime(2026, 8, 21, 14, 0, tzinfo=UTC)),
        # Sat 2026-08-22 12:00 NY -> slots Sun 2026-08-23 10:00/11:00 NY
        # (a weekend RUN and weekend-day slots both stay offerable).
        "weekend_noon": (datetime(2026, 8, 22, 16, 0, tzinfo=UTC),
                         datetime(2026, 8, 23, 14, 0, tzinfo=UTC)),
        # Tue 2026-08-18 23:30 NY -- the exact rollover window in which the
        # old +48h/+49h fixture straddled midnight -> slots Thu 2026-08-20.
        "late_night_rollover": (datetime(2026, 8, 19, 3, 30, tzinfo=UTC),
                                datetime(2026, 8, 20, 14, 0, tzinfo=UTC)),
    }
    pinned, slot_a_start = scenarios[scenario]
    _fixed_clock(monkeypatch, pinned)

    slot_a = _slot_row(db, client_row, slot_a_start)
    slot_b = _slot_row(db, client_row, slot_a_start + timedelta(hours=1))
    ny = ZoneInfo("America/New_York")
    assert (ensure_utc(slot_a.start_datetime).astimezone(ny).date()
            == ensure_utc(slot_b.start_datetime).astimezone(ny).date()),         "fixture invariant: both offered slots share one client-local date"
    other = make_conversation(db, client_row)

    conversation_row.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    conversation_row.booking_preferred_date = ensure_utc(
        slot_a.start_datetime).astimezone(ny).date().isoformat()
    conversation_row.booking_time_preference = "any"
    # Patch 2C: an offer is honored only while its explicit metadata says it
    # is live -- offered IDs with a NULL booking_offer_expires_at are treated
    # as expired (safety contract, tested separately below). This test targets
    # the HOLD-CONFLICT race, not expiration, so the setup must represent a
    # valid, unexpired offer -- relative to the SAME pinned clock the dialog
    # reads.
    conversation_row.booking_offer_expires_at = pinned + timedelta(minutes=30)
    conversation_row.booking_effective_time_preference = "any"
    conversation_row.booking_offered_slot_ids = [str(slot_a.id), str(slot_b.id)]
    db.commit()

    # Contract 1 + 2: patient B holds BOTH slot ids in the live offered set,
    # and the offer expiration is valid at the pinned now.
    db.refresh(conversation_row)
    assert conversation_row.booking_offered_slot_ids == [str(slot_a.id),
                                                         str(slot_b.id)]
    assert ensure_utc(conversation_row.booking_offer_expires_at) > pinned

    # Contract 3: patient A snipes the first slot after it was displayed to
    # our patient, under the SAME pinned clock the dialog will use.
    assert place_hold(db, client_row.id, slot_a.id, other.id,
                       settings=_settings(client_row), time_preference="any",
                       service_key=None, now_utc=pinned).success

    # Contract 4-7: patient B chooses the first offered slot; the server
    # detects the hold conflict, answers with the approved "just taken"
    # wording, and re-offers valid alternatives (slot B) instead of failing.
    reply = booking_conversation.handle_booking_message(
        db, client_row, conversation_row, "the first one")
    assert reply.handled
    assert "just taken" in reply.text.lower()
    offered_after = reply.meta.get("offered_slots") or []
    assert str(slot_a.id) not in offered_after     # sniped slot excluded
    assert str(slot_b.id) in offered_after         # same-day alternative kept

    # Contract 8: conflict recovery leaves a live selection wait with a
    # fresh, unexpired offer (the authoritative recovery contract).
    db.refresh(conversation_row)
    assert conversation_row.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert ensure_utc(conversation_row.booking_offer_expires_at) > pinned


# ---------------------------------------------------------------------------
# PATCH 1 — database-enforced booking invariants (Senior Audit Critical #1)
# ---------------------------------------------------------------------------

def _finalize_kwargs():
    """The patient-detail AND explicit policy-context arguments every
    finalize call in this section uses (Patch 2C: time_preference and
    service_key are required keyword-only inputs — "any"/None here are
    intentional values, not defaults)."""
    return dict(
        time_preference="any", service_key=None,
        patient_name="Kevin", patient_phone="516-555-1234", patient_email=None,
        new_or_returning="new", reason="cleaning/checkup", urgency="routine",
    )


def test_concurrent_finalize_same_conversation_two_slots(
    engine, db, client_row, conversation_row, monkeypatch
):
    """THE Critical #1 race: one conversation, two held slots, two threads
    finalize at the same time.

    The application pre-check (get_appointment_by_conversation) alone cannot
    stop this, because both requests read "no appointment yet" and then lock
    DIFFERENT slot rows. The test wraps the pre-check in a two-party barrier
    so both threads are PROVEN to have completed the pre-check (seeing None)
    before either proceeds to insert — the race cannot degrade into the
    sequential fast path. Only the uq_active_appointment_per_conversation
    index can decide the winner; the loser must get the exact deterministic
    result, not a 500.
    """
    from app.database import SessionLocal
    from app.calendar_models import Appointment, AppointmentStatus
    from app.repositories import appointment_repository
    from app.services import booking_service
    from app.services.appointment_hold_service import place_hold

    settings = _settings(client_row)
    slot1 = _make_slot(db, client_row, 48)
    slot2 = _make_slot(db, client_row, 72)
    assert place_hold(db, client_row.id, slot1.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now()).success
    assert place_hold(db, client_row.id, slot2.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now()).success

    client_id, conversation_id = client_row.id, conversation_row.id
    slot_ids = {"a": slot1.id, "b": slot2.id}

    # Synchronize ONLY each thread's FIRST pre-check call. The loser's
    # post-IntegrityError re-query (inside booking_service's handler) calls
    # this same function again and must NOT wait on the barrier — hence the
    # thread-local one-shot flag. Test-only wrapping, per the approved plan:
    # no synchronization hooks are added to production modules.
    real_precheck = appointment_repository.get_appointment_by_conversation
    barrier = threading.Barrier(2, timeout=15)
    precheck_saw = {}
    first_call = threading.local()

    def synchronized_precheck(session, cid, conv_id):
        result = real_precheck(session, cid, conv_id)
        if not getattr(first_call, "passed", False):
            first_call.passed = True
            precheck_saw[threading.current_thread().name] = result
            barrier.wait()  # Both threads hold here until BOTH pre-checks ran.
        return result

    monkeypatch.setattr(
        booking_service.appointment_repository,
        "get_appointment_by_conversation",
        synchronized_precheck,
    )

    results, appointment_ids, errors = {}, {}, {}

    def attempt(name):
        session = SessionLocal()
        try:
            result = booking_service.finalize_booking(
                session, client_id, slot_ids[name], conversation_id,
        settings=settings, now_utc=_now(), **_finalize_kwargs(),
            )
            results[name] = result
            # Capture the appointment id WHILE the session is open (the ORM
            # instance detaches when the session closes).
            appointment_ids[name] = (
                result.appointment.id if result.appointment is not None else None
            )
        except Exception as exc:  # No exception may escape finalize_booking.
            errors[name] = exc
        finally:
            session.close()

    threads = [
        threading.Thread(target=attempt, args=("a",), name="a"),
        threading.Thread(target=attempt, args=("b",), name="b"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No unhandled exceptions: the loser got a BookingResult, not a crash.
    assert not errors, f"finalize raised instead of returning a result: {errors}"

    # PROOF the race actually happened: both threads completed the pre-check
    # and both saw no existing appointment before either inserted.
    assert set(precheck_saw) == {"a", "b"}
    assert precheck_saw["a"] is None and precheck_saw["b"] is None

    # Exactly one winner.
    successes = [n for n in ("a", "b") if results[n].success]
    assert len(successes) == 1, f"expected exactly one winner, got {results}"
    winner, loser = successes[0], ("a" if successes[0] == "b" else "b")

    # The loser's reason is EXACTLY already_booked_by_conversation, and it
    # carries the WINNING appointment so Mia can restate it.
    assert results[loser].reason == "already_booked_by_conversation"
    assert appointment_ids[loser] is not None
    assert appointment_ids[loser] == appointment_ids[winner]

    # Exactly one non-cancelled appointment exists for this conversation.
    verify = SessionLocal()
    try:
        active_count = (
            verify.query(Appointment)
            .filter(
                Appointment.conversation_id == conversation_id,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .count()
        )
    finally:
        verify.close()
    assert active_count == 1


def test_concurrent_finalize_same_slot_two_conversations(
    engine, db, client_row, conversation_row
):
    """Service-level same-slot race: conversation A holds the slot; A and a
    NON-holder B call finalize concurrently. The slot row lock + in-lock hold
    recheck (not the unique index) must deterministically reject B with
    hold_lost, and the new index must not disturb the winning path.
    """
    from app.database import SessionLocal
    from app.calendar_models import Appointment, AppointmentStatus
    from app.services import booking_service
    from app.services.appointment_hold_service import place_hold

    settings = _settings(client_row)
    slot = _make_slot(db, client_row, 48)
    other = make_conversation(db, client_row)
    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now()).success

    client_id, slot_id = client_row.id, slot.id
    conversation_ids = {"holder": conversation_row.id, "intruder": other.id}
    barrier = threading.Barrier(2, timeout=15)
    results, errors = {}, {}

    def attempt(name):
        session = SessionLocal()
        try:
            barrier.wait()  # Maximize overlap of the two finalize calls.
            results[name] = booking_service.finalize_booking(
                session, client_id, slot_id, conversation_ids[name],
        settings=settings, now_utc=_now(), **_finalize_kwargs(),
            )
        except Exception as exc:
            errors[name] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(n,)) for n in conversation_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"finalize raised instead of returning a result: {errors}"
    assert results["holder"].success is True
    assert results["intruder"].success is False
    assert results["intruder"].reason == "hold_lost"

    verify = SessionLocal()
    try:
        slot_active = (
            verify.query(Appointment)
            .filter(
                Appointment.slot_id == slot_id,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .count()
        )
    finally:
        verify.close()
    assert slot_active == 1


def test_slot_unique_index_enforced_when_lock_bypassed_sequential(
    engine, db, client_row, conversation_row
):
    """SEQUENTIAL database-enforcement test (deliberately NOT a concurrency
    test — the genuine concurrent races are the two threaded tests above).

    Two sessions insert appointments for the SAME slot one after the other,
    directly through the repository, WITHOUT taking the slot lock or using
    booking_service — simulating a future bug or mis-ordered code path.
    Because the first insert has already committed, the ONLY thing that can
    refuse the second is uq_active_appointment_per_slot itself: this proves
    the index holds even when every application defense is bypassed.
    """
    from sqlalchemy.exc import IntegrityError
    from app.database import SessionLocal
    from app.calendar_models import Appointment, AppointmentSlot, AppointmentStatus
    from app.repositories import appointment_repository

    slot = _make_slot(db, client_row, 48)
    other = make_conversation(db, client_row)  # Different conversation, so only
    slot_id, client_id = slot.id, client_row.id   # the SLOT index can fire.

    def bypass_insert(conversation_id):
        session = SessionLocal()
        try:
            raw_slot = session.get(AppointmentSlot, slot_id)  # No FOR UPDATE — on purpose.
            # Repository-direct call: pass ONLY the appointment arguments the
            # repository accepts. _finalize_kwargs() belongs to
            # finalize_booking and now carries Patch 2C policy context
            # (time_preference, service_key) that the repository rightly
            # does not accept — policy lives in the service layer (Rule 3).
            appointment_repository.create_appointment_from_slot(
                session, slot=raw_slot, conversation_id=conversation_id,
                status=AppointmentStatus.PENDING,
                patient_name="Kevin", patient_phone="516-555-1234",
                patient_email=None, new_or_returning="new",
                reason="cleaning/checkup", urgency="routine",
            )
            session.commit()
        finally:
            session.close()

    bypass_insert(conversation_row.id)  # First insert commits.

    with pytest.raises(IntegrityError) as excinfo:
        bypass_insert(other.id)         # Second must be refused by the index.

    driver_error = excinfo.value.orig
    assert getattr(driver_error, "pgcode", None) == "23505"
    assert getattr(driver_error.diag, "constraint_name", None) == (
        "uq_active_appointment_per_slot"
    )

    verify = SessionLocal()
    try:
        slot_active = (
            verify.query(Appointment)
            .filter(
                Appointment.slot_id == slot_id,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .count()
        )
    finally:
        verify.close()
    assert slot_active == 1


def test_integrity_error_maps_conversation_conflict_deterministically(
    engine, db, client_row, conversation_row, monkeypatch
):
    """Single-threaded, deterministic reproduction of the race window: the
    pre-check is made to miss exactly once (as if the other request had not
    committed yet), so the INSERT hits uq_active_appointment_per_conversation.
    finalize_booking must return the exact mapped result carrying the winning
    appointment — never raise.
    """
    from app.repositories import appointment_repository
    from app.services import booking_service
    from app.services.appointment_hold_service import place_hold

    settings = _settings(client_row)
    slot1 = _make_slot(db, client_row, 48)
    slot2 = _make_slot(db, client_row, 72)

    place_hold(db, client_row.id, slot1.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now())
    winner = booking_service.finalize_booking(
        db, client_row.id, slot1.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        **_finalize_kwargs(),
    )
    assert winner.success
    winner_id = winner.appointment.id

    real_precheck = appointment_repository.get_appointment_by_conversation
    calls = {"n": 0}

    def miss_once_precheck(session, cid, conv_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # Simulate the race window: the winner isn't visible yet.
        return real_precheck(session, cid, conv_id)  # Handler's re-query sees truth.

    monkeypatch.setattr(
        booking_service.appointment_repository,
        "get_appointment_by_conversation",
        miss_once_precheck,
    )

    place_hold(db, client_row.id, slot2.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now())
    loser = booking_service.finalize_booking(
        db, client_row.id, slot2.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        **_finalize_kwargs(),
    )
    assert loser.success is False
    assert loser.reason == "already_booked_by_conversation"
    assert loser.appointment is not None
    assert loser.appointment.id == winner_id
    assert calls["n"] >= 2  # The handler really did re-query after the violation.


def test_cancel_then_rebook_different_conversation(engine, db, client_row, conversation_row):
    """Cancellation must reopen the slot for a DIFFERENT conversation: the
    partial predicates (status <> 'cancelled') must not block legitimate
    rebooking (approved plan §6)."""
    from app.database import SessionLocal
    from app.calendar_models import Appointment, AppointmentStatus
    from app.services import booking_service
    from app.services.appointment_hold_service import place_hold

    settings = _settings(client_row)
    slot = _make_slot(db, client_row, 48)
    other = make_conversation(db, client_row)

    place_hold(db, client_row.id, slot.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now())
    first = booking_service.finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        **_finalize_kwargs(),
    )
    assert first.success
    assert booking_service.cancel_appointment(
        db, client_row.id, first.appointment.id).success

    assert place_hold(db, client_row.id, slot.id, other.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now()).success
    rebooked = booking_service.finalize_booking(
        db, client_row.id, slot.id, other.id,
        settings=settings, now_utc=_now(),
        **_finalize_kwargs(),
    )
    assert rebooked.success, f"index wrongly blocked rebooking: {rebooked}"

    verify = SessionLocal()
    try:
        total = verify.query(Appointment).filter(Appointment.slot_id == slot.id).count()
        active = (
            verify.query(Appointment)
            .filter(Appointment.slot_id == slot.id,
                    Appointment.status != AppointmentStatus.CANCELLED)
            .count()
        )
    finally:
        verify.close()
    assert total == 2 and active == 1


def test_cancel_then_rebook_same_conversation(engine, db, client_row, conversation_row):
    """Cancellation must also let the SAME conversation book again — this
    exercises BOTH partial predicates at once (its old row is excluded from
    the conversation index AND from the slot index)."""
    from app.database import SessionLocal
    from app.calendar_models import Appointment, AppointmentStatus
    from app.services import booking_service
    from app.services.appointment_hold_service import place_hold

    settings = _settings(client_row)
    slot = _make_slot(db, client_row, 48)

    place_hold(db, client_row.id, slot.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now())
    first = booking_service.finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        **_finalize_kwargs(),
    )
    assert first.success
    assert booking_service.cancel_appointment(
        db, client_row.id, first.appointment.id).success

    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                       settings=settings, time_preference="any",
                       service_key=None, now_utc=_now()).success
    rebooked = booking_service.finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        **_finalize_kwargs(),
    )
    assert rebooked.success, f"index wrongly blocked same-conversation rebooking: {rebooked}"

    verify = SessionLocal()
    try:
        conv_active = (
            verify.query(Appointment)
            .filter(Appointment.conversation_id == conversation_row.id,
                    Appointment.status != AppointmentStatus.CANCELLED)
            .count()
        )
        slot_active = (
            verify.query(Appointment)
            .filter(Appointment.slot_id == slot.id,
                    Appointment.status != AppointmentStatus.CANCELLED)
            .count()
        )
    finally:
        verify.close()
    assert conv_active == 1 and slot_active == 1


# ---------------------------------------------------------------------------
# PATCH 2B — DST-safe local-day windows, end-to-end against the database.
# ---------------------------------------------------------------------------

def _slot_row(db, client, start_utc):
    """A published slot at an EXACT aware-UTC instant (unlike _make_slot,
    which is relative to now)."""
    from app.repositories.appointment_repository import create_slot
    slot = create_slot(db, client.id, start_utc, start_utc + timedelta(minutes=45))
    db.commit()
    return slot


def test_availability_window_covers_full_fallback_local_day(engine, db, client_row):
    """Fall-back day (2026-11-01 America/New_York, a 25-hour local day),
    end-to-end through get_available_slots: the availability query must use
    the helper's half-open window [04:00Z Nov 1, 05:00Z Nov 2).

    - 23:59 PM local (04:59Z Nov 2) is IMMEDIATELY BEFORE end_utc and sits in
      the 25th hour the old start+24h formula could not see -> included.
    - exactly end_utc (05:00Z Nov 2 = local midnight Nov 2) -> excluded; it
      belongs to the next local date.
    """
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from app.services.availability_service import get_available_slots

    utc = ZoneInfo("UTC")
    settings = _settings(client_row)
    late_slot = _slot_row(db, client_row, datetime(2026, 11, 2, 4, 59, tzinfo=utc))
    boundary_slot = _slot_row(db, client_row, datetime(2026, 11, 2, 5, 0, tzinfo=utc))

    # A fixed "now" comfortably inside notice + 30-day horizon of Nov 1.
    now_utc = datetime(2026, 10, 25, 13, 0, tzinfo=utc)
    offered = get_available_slots(
        db, client_row.id, settings, date(2026, 11, 1), "any", now_utc
    )
    offered_ids = {s.id for s in offered}
    assert late_slot.id in offered_ids       # 25th hour is visible (Critical #7)
    assert boundary_slot.id not in offered_ids  # end_utc itself is the next day


def test_admin_routes_use_dst_safe_windows(engine, db, client_row):
    """Both admin listings must use the SAME helper windows (half-open):

    - list_slots on the spring-forward day (2026-03-08, a 23-hour local day,
      window [05:00Z Mar 8, 04:00Z Mar 9)): a 23:00 local slot (03:00Z Mar 9)
      is included; a slot exactly at end_utc (04:00Z Mar 9 = local midnight
      Mar 9) is excluded — the old start+24h formula wrongly listed it.
    - list_appointments over a MULTI-DAY range CROSSING the spring transition
      (start_day=Mar 7, end_day=Mar 8; end boundary = local midnight Mar 9 =
      04:00Z): an appointment at 03:59Z Mar 9 (23:59 local Mar 8, immediately
      before that midnight) is included; one exactly at 04:00Z is excluded.

    The route functions are invoked directly with the session (dependency
    injection bypassed; authenticated_client is supplied directly, Patch 5 —
    the window logic under test is identical either way).
    """
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from app.calendar_models import AppointmentStatus
    from app.repositories import appointment_repository
    from app.routes.calendar import list_appointments, list_slots

    utc = ZoneInfo("UTC")

    # --- list_slots on the single DST day -------------------------------
    inside = _slot_row(db, client_row, datetime(2026, 3, 9, 3, 0, tzinfo=utc))
    at_end = _slot_row(db, client_row, datetime(2026, 3, 9, 4, 0, tzinfo=utc))
    views = list_slots(client_id=client_row.id, day=date(2026, 3, 8), db=db,
                       authenticated_client=client_row)
    listed = {v.id for v in views}
    assert inside.id in listed        # 23:00 local Mar 8 belongs to Mar 8
    assert at_end.id not in listed    # local midnight Mar 9 does NOT

    # --- list_appointments across the transition -------------------------
    def appointment_at(start_utc, phone):
        slot = _slot_row(db, client_row, start_utc)
        appointment = appointment_repository.create_appointment_from_slot(
            db, slot=slot, conversation_id=None,  # staff-style row: exempt
            status=AppointmentStatus.PENDING,     # from the per-conversation
            patient_name="Kevin", patient_phone=phone,  # unique index
            patient_email=None, new_or_returning=None,
            reason="cleaning/checkup", urgency="routine",
        )
        db.commit()
        return appointment

    included = appointment_at(datetime(2026, 3, 9, 3, 59, tzinfo=utc), "516-555-0001")
    excluded = appointment_at(datetime(2026, 3, 9, 4, 0, tzinfo=utc), "516-555-0002")

    views = list_appointments(
        client_id=client_row.id, start_day=date(2026, 3, 7),
        end_day=date(2026, 3, 8), db=db, authenticated_client=client_row,
    )
    listed = {v.id for v in views}
    assert included.id in listed      # 23:59 local on end_day is inside
    assert excluded.id not in listed  # the following local midnight is not


# ---------------------------------------------------------------------------
# PATCH 2C — stale offered-slot revalidation and offer expiration
# (Senior Audit Critical #8). All timestamps are fixed and injected — no
# sleeps, no real waiting.
# ---------------------------------------------------------------------------

def _utc(y, mo, d, h, mi=0):
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    return _dt(y, mo, d, h, mi, tzinfo=ZoneInfo("UTC"))


# A fixed anchor "now" for the 2C service-level tests: July 20 2026, 9 AM NY.
NOW_2C = None  # set lazily to avoid import-order issues


def _now2c():
    global NOW_2C
    if NOW_2C is None:
        NOW_2C = _utc(2026, 7, 20, 13, 0)
    return NOW_2C


def _shrink_horizon(db, client, days):
    """Simulate an admin edit: rewrite the calendar JSONB with a new
    max_booking_days. Returns FRESHLY loaded settings (the request-level
    snapshot a later message would see)."""
    new_settings = dict(client.settings)
    calendar = dict(new_settings.get("calendar") or {})
    calendar["max_booking_days"] = days
    new_settings["calendar"] = calendar
    client.settings = new_settings
    db.add(client)
    db.commit()
    return _settings(client)


# ----- hold-time revalidation (scenarios 8-13) ------------------------------

def test_hold_rejects_slot_past_minimum_notice(engine, db, client_row, conversation_row):
    """Eligible when displayed, but minimum notice crossed before selection:
    the under-lock revalidation refuses the hold, the slot is NOT mutated,
    and no appointment exists."""
    from app.calendar_models import Appointment, SlotStatus
    from app.services.appointment_hold_service import place_hold

    now = _now2c()
    slot = _slot_row(db, client_row, now + timedelta(minutes=30))  # < 60 min notice

    result = place_hold(db, client_row.id, slot.id, conversation_row.id,
                        settings=_settings(client_row), time_preference="any",
                        service_key=None, now_utc=now)
    assert result.success is False
    assert result.reason == "slot_ineligible"
    assert result.detail == "too_soon"
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE      # not incorrectly mutated
    assert slot.held_until is None and slot.held_by_conversation_id is None
    assert db.query(Appointment).filter(Appointment.slot_id == slot.id).count() == 0


def test_hold_rejects_after_horizon_shrunk(engine, db, client_row, conversation_row):
    """maximum_booking_days shortened after display: the freshly loaded
    settings snapshot rejects the hold under the lock."""
    from app.calendar_models import SlotStatus
    from app.services.appointment_hold_service import place_hold

    now = _now2c()
    slot = _slot_row(db, client_row, now + timedelta(days=20))  # fine at 30 days
    settings7 = _shrink_horizon(db, client_row, 7)

    result = place_hold(db, client_row.id, slot.id, conversation_row.id,
                        settings=settings7, time_preference="any",
                        service_key=None, now_utc=now)
    assert (result.success, result.reason, result.detail) == (
        False, "slot_ineligible", "beyond_horizon")
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE


def test_hold_rejects_service_mismatch(engine, db, client_row, conversation_row):
    """Slot reserved for a different service can no longer be held once the
    conversation's service context differs."""
    from app.repositories.appointment_repository import create_slot
    from app.calendar_models import SlotStatus
    from app.services.appointment_hold_service import place_hold

    now = _now2c()
    start = now + timedelta(hours=48)
    slot = create_slot(db, client_row.id, start, start + timedelta(minutes=45),
                       service_key="implant consult")
    db.commit()

    result = place_hold(db, client_row.id, slot.id, conversation_row.id,
                        settings=_settings(client_row), time_preference="any",
                        service_key="cleaning/checkup", now_utc=now)
    assert (result.success, result.reason, result.detail) == (
        False, "slot_ineligible", "service_mismatch")
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE


def test_hold_rejects_preference_mismatch(engine, db, client_row, conversation_row):
    """A slot outside the offer's effective time preference is refused under
    the lock (the stored preference is the policy input here)."""
    from app.calendar_models import SlotStatus
    from app.services.appointment_hold_service import place_hold

    now = _now2c()
    slot = _slot_row(db, client_row, _utc(2026, 7, 22, 18, 0))  # 2 PM NY = afternoon

    result = place_hold(db, client_row.id, slot.id, conversation_row.id,
                        settings=_settings(client_row), time_preference="morning",
                        service_key=None, now_utc=now)
    assert (result.success, result.reason, result.detail) == (
        False, "slot_ineligible", "preference_mismatch")
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE


def test_hold_succeeds_when_still_eligible(engine, db, client_row, conversation_row):
    """An unchanged eligible slot can still be held — the revalidation adds
    no false rejections (scenario 12 / required scenario 9's first half)."""
    from app.calendar_models import SlotStatus
    from app.services.appointment_hold_service import place_hold

    now = _now2c()
    slot = _slot_row(db, client_row, now + timedelta(hours=48))
    result = place_hold(db, client_row.id, slot.id, conversation_row.id,
                        settings=_settings(client_row), time_preference="any",
                        service_key=None, now_utc=now)
    assert result.success is True and result.reason == "ok"
    db.refresh(slot)
    assert slot.status == SlotStatus.HELD
    assert slot.held_by_conversation_id == conversation_row.id


# ----- finalization-time revalidation (scenarios 14-17) ---------------------

def test_finalize_rejects_when_notice_crossed_after_hold(
    engine, db, client_row, conversation_row
):
    """Held while eligible, but the slot drifts inside minimum notice before
    confirmation (hold still valid): no appointment, and the conversation's
    own hold is released in the same committed transaction."""
    from app.calendar_models import Appointment, SlotStatus
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import finalize_booking

    now0 = _now2c()
    slot = _slot_row(db, client_row, now0 + timedelta(minutes=62))  # eligible: 62 > 60
    settings = _settings(client_row)
    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=settings, time_preference="any",
                      service_key=None, now_utc=now0).success

    now1 = now0 + timedelta(minutes=3)  # hold (5 min) still valid; 59 < 60 notice
    result = finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=now1, **_finalize_kwargs(),
    )
    assert result.success is False
    assert result.reason == "slot_ineligible"
    assert result.detail == "too_soon"
    assert db.query(Appointment).filter(Appointment.slot_id == slot.id).count() == 0
    db.refresh(slot)  # hold released ATOMICALLY inside finalize_booking:
    assert slot.status == SlotStatus.AVAILABLE
    assert slot.held_until is None
    assert slot.held_by_conversation_id is None


def test_finalize_rejects_after_settings_change(engine, db, client_row, conversation_row):
    """Approved condition 5: day-29 slot held at horizon 30; setting changed
    to 7; finalize with the reloaded current settings must refuse with the
    exact reason/detail, insert nothing, and release the hold."""
    from app.calendar_models import Appointment, SlotStatus
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import finalize_booking

    now = _now2c()
    slot = _slot_row(db, client_row, now + timedelta(days=29))
    settings30 = _settings(client_row)
    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=settings30, time_preference="any",
                      service_key=None, now_utc=now).success

    settings7 = _shrink_horizon(db, client_row, 7)  # reload CURRENT settings
    result = finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings7, now_utc=now + timedelta(minutes=1),
        **_finalize_kwargs(),
    )
    assert result.reason == "slot_ineligible"
    assert result.detail == "beyond_horizon"
    assert db.query(Appointment).filter(Appointment.slot_id == slot.id).count() == 0
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE
    assert slot.held_until is None
    assert slot.held_by_conversation_id is None


def test_finalize_succeeds_when_still_eligible(engine, db, client_row, conversation_row):
    """Scenario 9's second half: a slot that remains eligible throughout
    still books normally — appointment created, slot BOOKED."""
    from app.calendar_models import Appointment, SlotStatus
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import finalize_booking

    now = _now2c()
    slot = _slot_row(db, client_row, now + timedelta(hours=48))
    settings = _settings(client_row)
    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=settings, time_preference="any",
                      service_key=None, now_utc=now).success
    result = finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=now + timedelta(minutes=1),
        **_finalize_kwargs(),
    )
    assert result.success is True and result.reason == "ok"
    db.refresh(slot)
    assert slot.status == SlotStatus.BOOKED
    assert db.query(Appointment).filter(Appointment.slot_id == slot.id).count() == 1


# ----- conversation-level: relaxed offer, recovery, offer expiration --------

def _fixed_clock(monkeypatch, start_utc):
    """Deterministic time injection: replaces booking_conversation.client_now
    (the module's single clock read) with a controllable fake. Returns the
    mutable clock dict — set clock[\"now\"] to advance time. Test-side only."""
    from zoneinfo import ZoneInfo
    from app.services import booking_conversation
    clock = {"now": start_utc}
    monkeypatch.setattr(
        booking_conversation, "client_now",
        lambda settings: clock["now"].astimezone(ZoneInfo(settings.timezone_name)),
    )
    return clock


def _fake_notifications(monkeypatch):
    """Replace the single notification entry point with a counting fake so
    no SMS/email/Twilio/Telnyx/Resend provider can possibly run. Test-side
    monkeypatching only — no production hooks."""
    from app.services import booking_conversation
    calls = {"n": 0}

    def fake_send(db, client, appointment, settings):
        calls["n"] += 1
        return SimpleNamespace(errors=[])

    monkeypatch.setattr(
        booking_conversation.notification_service,
        "send_booking_notifications", fake_send,
    )
    return calls


def test_relaxed_offer_holds_and_finalizes(engine, db, client_row, conversation_row, monkeypatch):
    """Approved condition 1 regression: morning preference -> no morning
    slots -> relaxed PREF_ANY afternoon offer -> selection -> hold succeeds
    (offer fields cleared, effective preference preserved) -> finalization
    succeeds (effective preference cleared). The notification fake is called
    exactly once."""
    from app.calendar_models import Appointment, BookingState, SlotStatus
    from app.services import booking_conversation

    now = _utc(2026, 7, 20, 13, 0)                     # 9:00 AM NY
    clock = _fixed_clock(monkeypatch, now)
    calls = _fake_notifications(monkeypatch)

    afternoon = _slot_row(db, client_row, _utc(2026, 7, 24, 18, 0))  # 2 PM NY Jul 24
    conversation_row.booking_state = BookingState.WAITING_FOR_TIME_PREFERENCE
    conversation_row.booking_preferred_date = "2026-07-24"
    db.add(conversation_row)
    db.commit()

    def say(text):
        return booking_conversation.handle_booking_message(
            db, client_row, conversation_row, text)

    offer = say("morning please")
    assert offer.handled
    assert "morning" in offer.text          # relaxed wording names the preference
    assert conversation_row.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conversation_row.booking_offered_slot_ids == [str(afternoon.id)]
    assert conversation_row.booking_effective_time_preference == "any"   # relaxed
    assert ensure_utc(conversation_row.booking_offer_expires_at) == now + timedelta(minutes=30)

    picked = say("the first one")
    assert picked.handled
    assert conversation_row.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    # Condition 1: after a successful hold the pre-hold offer is consumed...
    assert conversation_row.booking_offered_slot_ids is None
    assert conversation_row.booking_offer_expires_at is None
    # ...but the EFFECTIVE preference survives for finalization:
    assert conversation_row.booking_effective_time_preference == "any"
    db.refresh(afternoon)
    assert afternoon.status == SlotStatus.HELD

    clock["now"] = now + timedelta(minutes=2)          # still inside the hold
    done = say("yes")
    assert done.handled and done.meta.get("booked") is True
    assert calls["n"] == 1                             # notified exactly once (fake)
    assert conversation_row.booking_effective_time_preference is None  # cleared
    assert conversation_row.booking_state == BookingState.NONE
    assert db.query(Appointment).filter(
        Appointment.slot_id == afternoon.id).count() == 1


def test_finalize_rejection_recovers_without_notifying(
    engine, db, client_row, conversation_row, monkeypatch
):
    """Approved condition 6: a finalization rejected by current policy sends
    NO notification, claims nothing, clears the selected slot, replaces the
    stale offer state, releases the hold, and lands in the approved re-offer
    state with the approved accurate wording."""
    from app.calendar_models import Appointment, BookingState, SlotStatus
    from app.services import booking_conversation
    from app.services.appointment_hold_service import place_hold

    now0 = _utc(2026, 7, 20, 13, 0)
    clock = _fixed_clock(monkeypatch, now0)
    calls = _fake_notifications(monkeypatch)
    settings = _settings(client_row)

    doomed = _slot_row(db, client_row, now0 + timedelta(minutes=62))   # 11:02 NY
    fresh = _slot_row(db, client_row, now0 + timedelta(hours=3))       # noon NY, same local day
    assert place_hold(db, client_row.id, doomed.id, conversation_row.id,
                      settings=settings, time_preference="any",
                      service_key=None, now_utc=now0).success

    conversation_row.booking_state = BookingState.WAITING_FOR_CONFIRMATION
    conversation_row.booking_selected_slot_id = doomed.id
    conversation_row.booking_preferred_date = "2026-07-20"
    conversation_row.booking_time_preference = "any"
    conversation_row.booking_effective_time_preference = "any"
    db.add(conversation_row)
    db.commit()

    clock["now"] = now0 + timedelta(minutes=3)   # hold valid; slot now 59 min out
    reply = booking_conversation.handle_booking_message(
        db, client_row, conversation_row, "yes")

    assert calls["n"] == 0                              # NO notification of any kind
    assert reply.meta.get("booked") is not True          # no booked claim
    assert "no longer available" in reply.text           # approved wording
    assert "online" not in reply.text.lower()
    assert conversation_row.booking_selected_slot_id is None
    # Stale offer state REPLACED by a fresh offer (the still-eligible slot):
    assert conversation_row.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conversation_row.booking_offered_slot_ids == [str(fresh.id)]
    assert conversation_row.booking_offer_expires_at is not None
    db.refresh(doomed)                                   # hold released atomically
    assert doomed.status == SlotStatus.AVAILABLE
    assert doomed.held_until is None and doomed.held_by_conversation_id is None
    assert db.query(Appointment).filter(
        Appointment.client_id == client_row.id).count() == 0


def test_offer_valid_immediately_before_expiry(
    engine, db, client_row, conversation_row, monkeypatch
):
    """Boundary (valid side): at expires_at - 1 second the offer is still
    usable — selection proceeds all the way to a real hold."""
    from app.calendar_models import BookingState, SlotStatus
    from app.services import booking_conversation

    expires = _utc(2026, 7, 20, 13, 30)
    clock = _fixed_clock(monkeypatch, expires - timedelta(seconds=1))
    slot = _slot_row(db, client_row, _utc(2026, 7, 20, 18, 0))  # 2 PM NY same day

    conversation_row.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
    conversation_row.booking_preferred_date = "2026-07-20"
    conversation_row.booking_time_preference = "any"
    conversation_row.booking_effective_time_preference = "any"
    conversation_row.booking_offered_slot_ids = [str(slot.id)]
    conversation_row.booking_offer_expires_at = expires
    db.add(conversation_row)
    db.commit()

    reply = booking_conversation.handle_booking_message(
        db, client_row, conversation_row, "the first one")
    assert reply.handled
    assert reply.meta.get("reason") != "offer_expired"
    assert conversation_row.booking_state == BookingState.WAITING_FOR_CONFIRMATION
    db.refresh(slot)
    assert slot.status == SlotStatus.HELD


def test_offer_expired_at_boundary_after_and_null(
    engine, db, client_row, conversation_row, monkeypatch
):
    """Boundary (expired side) in three sub-scenarios inside one function
    (approved): exactly AT expires_at, well AFTER it, and the approved
    condition-2 NULL-expiry regression (offered IDs present, expires NULL).
    Each must refuse selection with reason offer_expired, clear and safely
    REPLACE the stale offer state, and hold NO slot from the stale menu."""
    from app.calendar_models import BookingState, SlotStatus
    from app.services import booking_conversation

    expires = _utc(2026, 7, 20, 13, 30)
    clock = _fixed_clock(monkeypatch, expires)
    slot = _slot_row(db, client_row, _utc(2026, 7, 20, 18, 0))  # stays eligible

    def reset(expiry_value):
        conversation_row.booking_state = BookingState.WAITING_FOR_SLOT_SELECTION
        conversation_row.booking_preferred_date = "2026-07-20"
        conversation_row.booking_time_preference = "any"
        conversation_row.booking_effective_time_preference = "any"
        conversation_row.booking_offered_slot_ids = [str(slot.id)]
        conversation_row.booking_offer_expires_at = expiry_value
        db.add(conversation_row)
        db.commit()

    def assert_expired_and_replaced(reply):
        assert reply.handled
        assert reply.meta.get("reason") == "offer_expired"
        # Stale state cleared and safely REPLACED by a fresh offer:
        assert conversation_row.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
        assert conversation_row.booking_offered_slot_ids == [str(slot.id)]  # fresh menu
        new_expiry = ensure_utc(conversation_row.booking_offer_expires_at)
        assert new_expiry == ensure_utc(clock["now"]) + timedelta(minutes=30)
        assert conversation_row.booking_effective_time_preference == "any"
        # Crucially: NO slot was held from the expired offer.
        db.refresh(slot)
        assert slot.status == SlotStatus.AVAILABLE
        assert slot.held_by_conversation_id is None

    # (a) exactly AT the expiration timestamp -> expired (now >= expires).
    reset(expires)
    clock["now"] = expires
    assert_expired_and_replaced(booking_conversation.handle_booking_message(
        db, client_row, conversation_row, "the first one"))

    # (b) well AFTER the expiration timestamp.
    reset(expires)
    clock["now"] = expires + timedelta(hours=1)
    assert_expired_and_replaced(booking_conversation.handle_booking_message(
        db, client_row, conversation_row, "the first one"))

    # (c) approved condition 2: offered IDs present but expires_at is NULL
    # (pre-2C in-flight state) -> treated as expired, replaced safely.
    reset(None)
    clock["now"] = expires
    assert_expired_and_replaced(booking_conversation.handle_booking_message(
        db, client_row, conversation_row, "the first one"))

# ---------------------------------------------------------------------------
# PATCH 4 — staff confirmation (Senior Audit Critical #4).
# confirm_appointment is the ONLY supported pending -> confirmed transition.
# Every timestamp below is fixed and injected (Approved Condition 1) — the
# service never reads the real clock, so assertions are deterministic.
# ---------------------------------------------------------------------------

# Fixed, unmistakable staff-confirmation instants (aware UTC).
CONFIRM_T1 = None  # set lazily like NOW_2C to avoid import-order issues


def _confirm_t1():
    global CONFIRM_T1
    if CONFIRM_T1 is None:
        CONFIRM_T1 = _utc(2026, 7, 25, 15, 0)
    return CONFIRM_T1


def _booked_pending_appointment(db, client_row, conversation_row,
                                hours_from_now=48.0):
    """One real PENDING appointment produced through the PRODUCTION path
    (place_hold -> finalize_booking with the fixture's staff-confirmation ON).
    Returns (appointment, slot)."""
    from app.calendar_models import AppointmentStatus
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import finalize_booking

    slot = _make_slot(db, client_row, hours_from_now)
    settings = _settings(client_row)
    hold = place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=settings, time_preference="any",
                      service_key=None, now_utc=_now())
    assert hold.success, hold
    booked = finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        time_preference="any", service_key=None,
        patient_name="Kevin", patient_phone="516-555-1234", patient_email=None,
        new_or_returning="new", reason="cleaning/checkup", urgency="routine",
    )
    assert booked.success, booked
    assert booked.appointment.status == AppointmentStatus.PENDING
    assert booked.appointment.confirmed_at is None  # finalize never sets it
    return booked.appointment, slot


def _trap_notification_channels(monkeypatch):
    """Confirmation must never message ANYONE (Approved Condition 2): trap
    both provider send functions so any invocation is counted and fails."""
    from app.services import notification_service
    calls = {"sms": 0, "email": 0}

    def sms_trap(*args, **kwargs):
        calls["sms"] += 1
        raise AssertionError("confirm path invoked _send_sms")

    def email_trap(*args, **kwargs):
        calls["email"] += 1
        raise AssertionError("confirm path invoked _send_email")

    monkeypatch.setattr(notification_service, "_send_sms", sms_trap)
    monkeypatch.setattr(notification_service, "_send_email", email_trap)
    return calls


def _appointment_snapshot(db, appointment):
    """Every field a failed confirmation must leave untouched
    (Approved Condition 2)."""
    db.refresh(appointment)
    return (
        appointment.status,
        appointment.confirmed_at,
        appointment.office_sms_sent,
        appointment.office_email_sent,
        appointment.patient_sms_sent,
        appointment.notify_error,
    )


def _assert_confirm_left_everything_unchanged(db, appointment, slot,
                                              before_snapshot,
                                              before_slot_status, calls):
    """Shared mutation-free proof for every FAILED transition."""
    assert _appointment_snapshot(db, appointment) == before_snapshot
    db.refresh(slot)
    assert slot.status == before_slot_status
    assert calls == {"sms": 0, "email": 0}


def _set_staff_confirmation(db, client, value):
    """Simulate an admin edit: rewrite the calendar JSONB with a new
    require_staff_confirmation. Returns FRESHLY loaded settings."""
    new_settings = dict(client.settings)
    calendar = dict(new_settings.get("calendar") or {})
    calendar["require_staff_confirmation"] = value
    new_settings["calendar"] = calendar
    client.settings = new_settings
    db.add(client)
    db.commit()
    return _settings(client)


def test_confirm_pending_appointment_succeeds(db, client_row, conversation_row):
    """The single supported transition: PENDING -> CONFIRMED, confirmed_at
    set to exactly the injected instant, slot untouched (stays BOOKED)."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.booking_service import confirm_appointment

    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)

    result = confirm_appointment(db, client_row.id, appointment.id,
                                 now_utc=_confirm_t1())
    assert result.success and result.reason == "ok"
    assert result.appointment.id == appointment.id

    db.refresh(appointment)
    assert appointment.status == AppointmentStatus.CONFIRMED
    assert ensure_utc(appointment.confirmed_at) == _confirm_t1()
    db.refresh(slot)
    assert slot.status == SlotStatus.BOOKED   # confirmation never touches it


def test_confirm_repeat_is_idempotent_preserves_confirmed_at(
    db, client_row, conversation_row
):
    """Re-confirming is an idempotent SUCCESS: reason already_confirmed and
    the ORIGINAL confirmed_at preserved byte-for-byte — a later, different
    injected instant must NOT overwrite it (Approved contract)."""
    from app.calendar_models import AppointmentStatus
    from app.services.booking_service import confirm_appointment

    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    first = confirm_appointment(db, client_row.id, appointment.id,
                                now_utc=_confirm_t1())
    assert first.success and first.reason == "ok"
    db.refresh(appointment)
    original_confirmed_at = appointment.confirmed_at

    second = confirm_appointment(db, client_row.id, appointment.id,
                                 now_utc=_confirm_t1() + timedelta(hours=1))
    assert second.success and second.reason == "already_confirmed"
    assert second.appointment.id == appointment.id

    db.refresh(appointment)
    assert appointment.status == AppointmentStatus.CONFIRMED
    assert appointment.confirmed_at == original_confirmed_at  # byte-for-byte


def test_confirm_unknown_appointment_missing(
    db, client_row, conversation_row, monkeypatch
):
    """An id that exists nowhere: appointment_missing, no appointment
    returned, and a bystander appointment + slot provably untouched."""
    from app.calendar_models import SlotStatus
    from app.services.booking_service import confirm_appointment

    calls = _trap_notification_channels(monkeypatch)
    bystander, slot = _booked_pending_appointment(db, client_row, conversation_row)
    before = _appointment_snapshot(db, bystander)

    result = confirm_appointment(db, client_row.id, uuid.uuid4(),
                                 now_utc=_confirm_t1())
    assert not result.success and result.reason == "appointment_missing"
    assert result.appointment is None
    _assert_confirm_left_everything_unchanged(
        db, bystander, slot, before, SlotStatus.BOOKED, calls)


def test_confirm_other_client_appointment_missing(
    db, client_row, conversation_row, monkeypatch
):
    """Office B confirming office A's REAL appointment id gets the exact
    same appointment_missing outcome as an unknown id — cross-tenant ids are
    indistinguishable from nonexistent ones (Rule 15) — and office A's
    appointment is provably untouched."""
    from app.calendar_models import SlotStatus
    from app.models import Client
    from app.services.booking_service import confirm_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    before = _appointment_snapshot(db, appointment)

    office_b = Client(id=uuid.uuid4(), practice_name="Other Dental",
                      api_key=f"key-{uuid.uuid4()}", active=True)
    db.add(office_b)
    db.commit()

    foreign = confirm_appointment(db, office_b.id, appointment.id,
                                  now_utc=_confirm_t1())
    unknown = confirm_appointment(db, office_b.id, uuid.uuid4(),
                                  now_utc=_confirm_t1())
    assert not foreign.success and foreign.reason == "appointment_missing"
    assert foreign.appointment is None
    # Indistinguishable from an id that exists nowhere:
    assert (foreign.success, foreign.reason, foreign.appointment,
            foreign.detail) == (unknown.success, unknown.reason,
                                unknown.appointment, unknown.detail)
    _assert_confirm_left_everything_unchanged(
        db, appointment, slot, before, SlotStatus.BOOKED, calls)


def test_confirm_cancelled_appointment_rejected(
    db, client_row, conversation_row, monkeypatch
):
    """A cancelled appointment (via the PRODUCTION cancel path, which frees
    the slot) is not confirmable: not_confirmable with detail 'cancelled',
    and nothing — appointment, freed slot, notification bookkeeping —
    changes."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.booking_service import cancel_appointment, confirm_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    assert cancel_appointment(db, client_row.id, appointment.id).success
    before = _appointment_snapshot(db, appointment)
    assert before[0] == AppointmentStatus.CANCELLED

    result = confirm_appointment(db, client_row.id, appointment.id,
                                 now_utc=_confirm_t1())
    assert not result.success and result.reason == "not_confirmable"
    assert result.detail == AppointmentStatus.CANCELLED
    _assert_confirm_left_everything_unchanged(
        db, appointment, slot, before, SlotStatus.AVAILABLE, calls)


def test_confirm_completed_appointment_rejected(
    db, client_row, conversation_row, monkeypatch
):
    """COMPLETED is terminal for confirmation purposes. No production
    transition writes 'completed' yet, so the row is put there directly —
    the test targets only confirm_appointment's status gate."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.booking_service import confirm_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    appointment.status = AppointmentStatus.COMPLETED  # direct write on purpose
    db.commit()
    before = _appointment_snapshot(db, appointment)

    result = confirm_appointment(db, client_row.id, appointment.id,
                                 now_utc=_confirm_t1())
    assert not result.success and result.reason == "not_confirmable"
    assert result.detail == AppointmentStatus.COMPLETED
    _assert_confirm_left_everything_unchanged(
        db, appointment, slot, before, SlotStatus.BOOKED, calls)


def test_confirm_no_show_appointment_rejected(
    db, client_row, conversation_row, monkeypatch
):
    """NO_SHOW is likewise not confirmable (same direct-write setup as the
    completed case; only the status gate is under test)."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.booking_service import confirm_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    appointment.status = AppointmentStatus.NO_SHOW  # direct write on purpose
    db.commit()
    before = _appointment_snapshot(db, appointment)

    result = confirm_appointment(db, client_row.id, appointment.id,
                                 now_utc=_confirm_t1())
    assert not result.success and result.reason == "not_confirmable"
    assert result.detail == AppointmentStatus.NO_SHOW
    _assert_confirm_left_everything_unchanged(
        db, appointment, slot, before, SlotStatus.BOOKED, calls)


def test_confirm_sends_no_notifications(
    db, client_row, conversation_row, monkeypatch
):
    """The SUCCESS path also messages no one: both provider send functions
    are trapped, and the notification bookkeeping fields on the appointment
    are byte-identical before and after confirmation."""
    from app.services.booking_service import confirm_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    db.refresh(appointment)
    notify_before = (appointment.office_sms_sent, appointment.office_email_sent,
                     appointment.patient_sms_sent, appointment.notify_error)

    result = confirm_appointment(db, client_row.id, appointment.id,
                                 now_utc=_confirm_t1())
    assert result.success and result.reason == "ok"

    db.refresh(appointment)
    assert (appointment.office_sms_sent, appointment.office_email_sent,
            appointment.patient_sms_sent, appointment.notify_error) == notify_before
    assert calls == {"sms": 0, "email": 0}


def test_confirm_auto_confirmed_appointment_keeps_null_confirmed_at(
    db, client_row, conversation_row
):
    """Approved confirmed_at semantics: an appointment created directly as
    CONFIRMED (require_staff_confirmation=false) has confirmed_at NULL, and
    a staff re-confirm is an idempotent success that KEEPS it NULL — the
    column records staff pending->confirmed actions only."""
    from app.calendar_models import AppointmentStatus
    from app.services.appointment_hold_service import place_hold
    from app.services.booking_service import confirm_appointment, finalize_booking

    settings = _set_staff_confirmation(db, client_row, False)
    slot = _make_slot(db, client_row)
    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=settings, time_preference="any",
                      service_key=None, now_utc=_now()).success
    booked = finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=_now(),
        time_preference="any", service_key=None,
        patient_name="Kevin", patient_phone="516-555-1234", patient_email=None,
        new_or_returning="new", reason="cleaning/checkup", urgency="routine",
    )
    assert booked.success
    appointment = booked.appointment
    assert appointment.status == AppointmentStatus.CONFIRMED
    assert appointment.confirmed_at is None      # auto-confirm never sets it

    result = confirm_appointment(db, client_row.id, appointment.id,
                                 now_utc=_confirm_t1())
    assert result.success and result.reason == "already_confirmed"
    db.refresh(appointment)
    assert appointment.confirmed_at is None      # and re-confirm keeps NULL


def test_cancel_then_confirm_rejected(db, client_row, conversation_row):
    """Sequential ordering, cancel-last-wins direction: staff confirms
    (confirmed_at = T1), then cancels, then tries to confirm again. The
    re-confirm is rejected AND the earlier confirmed_at survives on the
    cancelled row — rejection must not wipe recorded history."""
    from app.calendar_models import AppointmentStatus
    from app.services.booking_service import cancel_appointment, confirm_appointment

    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    assert confirm_appointment(db, client_row.id, appointment.id,
                               now_utc=_confirm_t1()).success
    assert cancel_appointment(db, client_row.id, appointment.id).success

    result = confirm_appointment(db, client_row.id, appointment.id,
                                 now_utc=_confirm_t1() + timedelta(hours=2))
    assert not result.success and result.reason == "not_confirmable"
    assert result.detail == AppointmentStatus.CANCELLED

    db.refresh(appointment)
    assert appointment.status == AppointmentStatus.CANCELLED
    assert ensure_utc(appointment.confirmed_at) == _confirm_t1()  # preserved


def test_confirm_then_cancel_allowed_preserves_confirmed_at(
    db, client_row, conversation_row
):
    """Sequential ordering, the other direction: confirmed -> cancelled is a
    legal EXISTING transition; the slot is freed exactly as for a pending
    cancellation and confirmed_at survives for the audit trail."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.booking_service import cancel_appointment, confirm_appointment

    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    assert confirm_appointment(db, client_row.id, appointment.id,
                               now_utc=_confirm_t1()).success

    result = cancel_appointment(db, client_row.id, appointment.id)
    assert result.success
    db.refresh(appointment)
    assert appointment.status == AppointmentStatus.CANCELLED
    assert ensure_utc(appointment.confirmed_at) == _confirm_t1()  # preserved
    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE   # rebookable again


def test_concurrent_confirm_same_appointment_single_transition(
    engine, db, client_row, conversation_row
):
    """Two real sessions in two threads race confirm_appointment on the SAME
    appointment with DIFFERENT injected instants (so an overwrite would be
    visible). Approved Condition 6: both calls succeed, exactly one is
    reason 'ok' and one 'already_confirmed', exactly one timestamp is
    written, the loser observes exactly the winner's timestamp, and the slot
    stays BOOKED."""
    from app.calendar_models import SlotStatus
    from app.database import SessionLocal
    from app.services.booking_service import confirm_appointment

    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    appointment_id, client_id = appointment.id, client_row.id
    injected = {"a": _confirm_t1(), "b": _confirm_t1() + timedelta(minutes=5)}
    barrier = threading.Barrier(2, timeout=15)
    results = {}

    def attempt(name):
        session = SessionLocal()
        try:
            barrier.wait()  # both threads in flight before either commits
            outcome = confirm_appointment(session, client_id, appointment_id,
                                          now_utc=injected[name])
            # Read the persisted value INSIDE this session (post-rollback
            # access re-queries committed state), then keep only plain data —
            # ORM objects must not cross the session boundary.
            observed = ensure_utc(outcome.appointment.confirmed_at)
            results[name] = (outcome.success, outcome.reason, observed)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == {"a", "b"}
    assert all(success for success, _, _ in results.values())        # both succeed
    reasons = sorted(reason for _, reason, _ in results.values())
    assert reasons == ["already_confirmed", "ok"], results           # exactly one each

    (winner_name,) = [n for n, r in results.items() if r[1] == "ok"]
    (loser_name,) = [n for n, r in results.items() if r[1] == "already_confirmed"]

    # Exactly ONE timestamp was written — the winner's injected instant —
    # and the loser observed exactly that same value.
    db.refresh(appointment)
    stored = ensure_utc(appointment.confirmed_at)
    assert stored == injected[winner_name]
    assert results[winner_name][2] == stored
    assert results[loser_name][2] == stored

    db.refresh(slot)
    assert slot.status == SlotStatus.BOOKED


def test_confirm_route_status_mapping(engine, db, client_row, conversation_row,
                                      monkeypatch):
    """The admin route end-to-end (route functions invoked directly with the
    session, the established pattern): 200 for fresh AND repeated
    confirmation with identical confirmed_at; 404 with IDENTICAL wording for
    unknown and cross-tenant ids, mutation-free; 409 with the EXACT
    controlled wording for cancelled, completed, and no_show; nullable
    confirmed_at exposed consistently by BOTH the confirm response and the
    appointment-list response; and (PATCH 8, mirroring the cancel route's
    correction pass 1) a stored status OUTSIDE AppointmentStatus.ALL is
    rejected with exactly "Appointment is unsupported and cannot be
    confirmed." — the raw stored value is never echoed and never rewritten.
    Every rejected call is mutation-free (row, confirmed_at, notification
    bookkeeping, slot, hold fields) and messages no one."""
    from datetime import date as _date
    from fastapi import HTTPException
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.models import Client
    from app.services.booking_service import cancel_appointment
    from app.routes.calendar import confirm_appointment as confirm_route
    from app.routes.calendar import list_appointments

    calls = _trap_notification_channels(monkeypatch)

    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)

    # Fresh confirmation -> 200-equivalent AppointmentView.
    view = confirm_route(appointment_id=appointment.id,
                         client_id=client_row.id, db=db,
                         authenticated_client=client_row)
    assert view.status == "confirmed"
    assert view.confirmed_at is not None
    first_ts = view.confirmed_at

    # Repeat -> idempotent 200, byte-identical confirmed_at.
    view2 = confirm_route(appointment_id=appointment.id,
                          client_id=client_row.id, db=db,
                          authenticated_client=client_row)
    assert view2.status == "confirmed"
    assert view2.confirmed_at == first_ts

    # Unknown id -> 404.
    with pytest.raises(HTTPException) as unknown_excinfo:
        confirm_route(appointment_id=uuid.uuid4(),
                      client_id=client_row.id, db=db,
                      authenticated_client=client_row)
    assert unknown_excinfo.value.status_code == 404

    # Cross-tenant id -> 404 with IDENTICAL wording (no existence leak).
    # Patch 5 rendering of the same scenario: office B authenticates AS
    # ITSELF and probes office A's appointment id — the B-filtered lookup
    # misses, giving the same 404 as an unknown id.
    office_b = Client(id=uuid.uuid4(), practice_name="Other Dental",
                      api_key=f"key-{uuid.uuid4()}", active=True)
    db.add(office_b)
    db.commit()
    confirmed_before = _appointment_snapshot(db, appointment)
    with pytest.raises(HTTPException) as foreign_excinfo:
        confirm_route(appointment_id=appointment.id,
                      client_id=office_b.id, db=db,
                      authenticated_client=office_b)
    assert foreign_excinfo.value.status_code == 404
    assert foreign_excinfo.value.detail == unknown_excinfo.value.detail
    # The probed appointment is untouched (tenant isolation, mutation-free).
    assert _appointment_snapshot(db, appointment) == confirmed_before

    # Cancelled -> 409 with the EXACT controlled wording (PATCH 8: the
    # controlled status word passes through the sanitizer unchanged).
    assert cancel_appointment(db, client_row.id, appointment.id).success
    with pytest.raises(HTTPException) as conflict_excinfo:
        confirm_route(appointment_id=appointment.id,
                      client_id=client_row.id, db=db,
                      authenticated_client=client_row)
    assert conflict_excinfo.value.status_code == 409
    assert conflict_excinfo.value.detail == (
        "Appointment is cancelled and cannot be confirmed.")

    # The conversation is free again after cancellation (proven by the Patch
    # 1 rebooking tests) — book a second, still-PENDING appointment so the
    # list exposes BOTH shapes of the nullable field.
    pending, _pending_slot = _booked_pending_appointment(
        db, client_row, conversation_row, hours_from_now=72.0)

    views = list_appointments(
        client_id=client_row.id,
        start_day=(_now() - timedelta(days=1)).date(),
        end_day=(_now() + timedelta(days=6)).date(),
        db=db, authenticated_client=client_row,
    )
    by_id = {v.id: v for v in views}
    assert by_id[appointment.id].confirmed_at == first_ts   # set, survives cancel
    assert by_id[pending.id].confirmed_at is None           # honest NULL

    # ------------------------------------------------------------------
    # PATCH 8 — response sanitization coverage. The still-PENDING second
    # appointment is reused now that the list assertions above are done.
    # Every rejected call below must be mutation-free (row incl.
    # confirmed_at and notification bookkeeping, slot status, hold
    # fields) and must message no one.
    # ------------------------------------------------------------------

    # Controlled terminal statuses -> 409 with the EXACT wording; the
    # controlled status word passes through the sanitizer unchanged.
    for terminal_status in (AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW):
        pending.status = terminal_status  # direct write on purpose
        db.commit()
        terminal_before = _appointment_snapshot(db, pending)
        terminal_slot_before = _slot_snapshot(db, _pending_slot)
        assert terminal_slot_before[0] == SlotStatus.BOOKED

        with pytest.raises(HTTPException) as terminal_excinfo:
            confirm_route(appointment_id=pending.id,
                          client_id=client_row.id, db=db,
                          authenticated_client=client_row)
        assert terminal_excinfo.value.status_code == 409
        assert terminal_excinfo.value.detail == (
            f"Appointment is {terminal_status} and cannot be confirmed.")
        # Privacy: only the controlled status word — no patient, slot,
        # tenant, or database information.
        assert "Kevin" not in terminal_excinfo.value.detail
        assert "516" not in terminal_excinfo.value.detail
        assert str(pending.id) not in terminal_excinfo.value.detail
        assert _appointment_snapshot(db, pending) == terminal_before
        assert _slot_snapshot(db, _pending_slot) == terminal_slot_before

    # PATCH 8: a stored status OUTSIDE AppointmentStatus.ALL (malformed /
    # legacy / manually edited row — the column has no CHECK constraint).
    # The lifecycle owner still rejects it by default, and the boundary
    # exposes ONLY the fixed sentinel "unsupported": the raw stored value
    # is never echoed and never repaired or rewritten.
    from app.services.booking_service import confirm_appointment as confirm_service
    malformed = "zz_bad"
    assert malformed not in AppointmentStatus.ALL
    pending.status = malformed  # direct write on purpose (no CHECK constraint)
    db.commit()
    malformed_before = _appointment_snapshot(db, pending)
    malformed_slot_before = _slot_snapshot(db, _pending_slot)
    assert malformed_slot_before[0] == SlotStatus.BOOKED

    # Service-level proof: BookingResult.detail is exactly the sentinel,
    # never the stored value, and the rejection is mutation-free
    # (appointment snapshot incl. confirmed_at and notification fields,
    # slot status, and hold fields all unchanged).
    service_result = confirm_service(db, client_row.id, pending.id,
                                     now_utc=_confirm_t1())
    assert not service_result.success
    assert service_result.reason == "not_confirmable"
    assert service_result.detail == "unsupported"
    assert _appointment_snapshot(db, pending) == malformed_before
    assert _slot_snapshot(db, _pending_slot) == malformed_slot_before

    # Route-level proof: exact wording, HTTP 409, raw value absent, no
    # private data, and still mutation-free — the malformed stored value
    # is NOT repaired.
    with pytest.raises(HTTPException) as malformed_excinfo:
        confirm_route(appointment_id=pending.id,
                      client_id=client_row.id, db=db,
                      authenticated_client=client_row)
    assert malformed_excinfo.value.status_code == 409
    assert malformed_excinfo.value.detail == (
        "Appointment is unsupported and cannot be confirmed.")
    assert malformed not in malformed_excinfo.value.detail
    assert "Kevin" not in malformed_excinfo.value.detail
    assert "516" not in malformed_excinfo.value.detail
    assert str(pending.id) not in malformed_excinfo.value.detail
    assert _appointment_snapshot(db, pending) == malformed_before
    assert _appointment_snapshot(db, pending)[0] == malformed  # not rewritten
    assert _slot_snapshot(db, _pending_slot) == malformed_slot_before

    # No notification channel executed on ANY path in this test —
    # confirmation (success or rejection) messages no one.
    assert calls == {"sms": 0, "email": 0}


# ===========================================================================
# PATCH 6 (Senior Audit Recommended #7) — AppointmentView output boundary
# for notify_error, and removal of notification internals from the
# patient-facing booking reply meta.
# ===========================================================================

def test_appointment_view_passes_approved_vocabulary_through(db, client_row, conversation_row):
    """Every approved stored notify_error value is returned by the admin
    view UNCHANGED — no double-redaction of valid values."""
    from app.routes.calendar import _appointment_view
    from app.services.notification_service import VALID_NOTIFY_ERROR_VALUES
    appointment, _slot = _booked_pending_appointment(db, client_row, conversation_row)
    assert _appointment_view(appointment).notify_error is None  # None -> None
    for value in sorted(VALID_NOTIFY_ERROR_VALUES):
        appointment.notify_error = value
        db.add(appointment)
        db.commit()
        db.refresh(appointment)
        assert _appointment_view(appointment).notify_error == value


def test_appointment_view_withholds_malformed_and_legacy_values(db, client_row, conversation_row):
    """Anything outside the approved grammar — a legacy raw provider
    exception, reversed channel order, a duplicated channel, an unknown
    code, an over-length value — returns EXACTLY the fixed withheld marker.
    The stored value itself is never rewritten (Option B)."""
    from app.routes.calendar import _appointment_view
    appointment, _slot = _booked_pending_appointment(db, client_row, conversation_row)
    legacy_raw = ("office_sms: TwilioRestException('HTTP 401: Unable to create record: "
                  "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages')")
    for bad in (
        legacy_raw,
        "office_email: send_failed; office_sms: send_failed",   # reversed order
        "office_sms: send_failed; office_sms: send_failed",     # duplicate channel
        "office_fax: send_failed",                              # unknown code
        "x" * 113,                                              # over-length
        "office_sms: send_failed ",                             # trailing space
    ):
        appointment.notify_error = bad
        db.add(appointment)
        db.commit()
        db.refresh(appointment)
        view = _appointment_view(appointment)
        assert view.notify_error == "notification_error: detail_withheld", bad
        assert "TwilioRestException" not in (view.notify_error or "")
        assert appointment.notify_error == bad  # storage untouched


def test_booked_reply_meta_has_no_notify_errors_key(db, client_row, conversation_row, monkeypatch):
    """PATCH 6: notification internals never reach the patient-facing reply.
    The office has BOTH channels configured and BOTH providers execute and
    FAIL (counted): booking still succeeds, the honest send_failed outcome
    is persisted for staff, the wording and the other meta keys are
    unchanged — and the notify_errors key is gone."""
    from app.calendar_models import Appointment, BookingState
    from app.services import booking_conversation

    # Real contacts so the provider paths actually execute (a client with no
    # contacts would bypass both fakes and record only 'skipped' entries).
    client_row.notification_phone = "+15550001111"
    client_row.notification_email = "office@example.com"
    db.add(client_row)
    db.commit()

    slot = _make_slot(db, client_row, hours_from_now=48)
    local_day = slot.start_datetime.astimezone(
        ZoneInfo("America/New_York")).strftime("%B %d").lower()

    def say(text):
        return booking_conversation.handle_booking_message(
            db, client_row, conversation_row, text)

    calls = {"sms": 0, "email": 0}

    def sms_boom(*args, **kwargs):
        calls["sms"] += 1
        raise RuntimeError("sms down")

    def email_boom(*args, **kwargs):
        calls["email"] += 1
        raise RuntimeError("email down")

    monkeypatch.setattr(booking_conversation.notification_service, "_send_sms", sms_boom)
    monkeypatch.setattr(booking_conversation.notification_service, "_send_email", email_boom)

    say("I'd like to book an appointment")
    say(local_day)
    say("any time works")
    say("the first one")
    reply = say("yes")

    # Both providers were INVOKED exactly once and raised.
    assert calls == {"sms": 1, "email": 1}

    assert reply.handled and reply.meta.get("booked") is True
    assert "notify_errors" not in reply.meta
    assert reply.meta.get("mode") == "booking"
    assert reply.meta.get("state") == BookingState.NONE
    assert reply.meta.get("appointment_id")
    assert "request" in reply.text.lower()
    assert "sms down" not in reply.text and "email down" not in reply.text

    appointment = db.query(Appointment).filter(
        Appointment.conversation_id == conversation_row.id).one()
    # Staff visibility: the exact two-failure vocabulary value, SMS first.
    assert appointment.notify_error == "office_sms: send_failed; office_email: send_failed"
    assert appointment.patient_sms_sent is False


# ===========================================================================
# PATCH 7 (Senior Audit Recommended #6) — appointment-cancellation lifecycle
# allow-list. Only pending and confirmed may be cancelled; cancelled is a
# mutation-free already_cancelled rejection (approved D1); completed and
# no_show are mutation-free not_cancellable rejections whose historical
# slot is never reopened. No cancellation path messages anyone.
# ===========================================================================

def _slot_snapshot(db, slot):
    """Every slot field a rejected or repeated cancellation must leave
    untouched (status, hold bookkeeping)."""
    db.refresh(slot)
    return (slot.status, slot.held_until, slot.held_by_conversation_id)


@pytest.mark.parametrize("terminal_status", ["completed", "no_show"])
def test_cancel_terminal_status_rejected(
    db, client_row, conversation_row, monkeypatch, terminal_status
):
    """T1: COMPLETED and NO_SHOW are terminal for cancellation. No
    production transition writes these statuses yet, so the row is put
    there directly (same setup as the Patch 4 confirm-gate tests) — the
    test targets only cancel_appointment's allow-list. The rejection is
    mutation-free: appointment snapshot (incl. confirmed_at and every
    notification field), slot status, and hold fields are all unchanged,
    and no notification channel executes."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.booking_service import cancel_appointment

    # Keep the parametrized literals pinned to the single status owner.
    assert terminal_status in AppointmentStatus.ALL

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    appointment.status = terminal_status  # direct write on purpose
    db.commit()
    before = _appointment_snapshot(db, appointment)
    slot_before = _slot_snapshot(db, slot)
    assert slot_before[0] == SlotStatus.BOOKED

    result = cancel_appointment(db, client_row.id, appointment.id)
    assert not result.success and result.reason == "not_cancellable"
    assert result.detail == terminal_status
    assert result.appointment.id == appointment.id

    # Mutation-free on BOTH rows: nothing rewritten, slot NOT reopened.
    assert _appointment_snapshot(db, appointment) == before
    assert _slot_snapshot(db, slot) == slot_before
    assert calls == {"sms": 0, "email": 0}


def test_repeat_cancel_is_mutation_free(db, client_row, conversation_row, monkeypatch):
    """T2: cancelled -> cancelled is a mutation-free already_cancelled
    rejection (approved D1: success stays False). The second call changes
    NOTHING: the appointment snapshot and the released slot's snapshot are
    byte-identical to their post-first-cancellation values, and no
    notification channel ever executes."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.booking_service import cancel_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)

    first = cancel_appointment(db, client_row.id, appointment.id)
    assert first.success and first.reason == "ok"
    after_first = _appointment_snapshot(db, appointment)
    slot_after_first = _slot_snapshot(db, slot)
    assert after_first[0] == AppointmentStatus.CANCELLED
    assert slot_after_first == (SlotStatus.AVAILABLE, None, None)  # released once

    second = cancel_appointment(db, client_row.id, appointment.id)
    assert not second.success and second.reason == "already_cancelled"
    assert second.appointment.id == appointment.id

    assert _appointment_snapshot(db, appointment) == after_first
    assert _slot_snapshot(db, slot) == slot_after_first
    assert calls == {"sms": 0, "email": 0}


def test_cancel_other_client_appointment_indistinguishable(
    db, client_row, conversation_row, monkeypatch
):
    """T3: office B cancelling office A's REAL appointment id gets the
    exact same slot_missing outcome as an unknown id — cross-tenant ids
    are indistinguishable from nonexistent ones (Rule 15) — and office A's
    appointment AND slot are provably untouched."""
    from app.calendar_models import SlotStatus
    from app.models import Client
    from app.services.booking_service import cancel_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    before = _appointment_snapshot(db, appointment)
    slot_before = _slot_snapshot(db, slot)
    assert slot_before[0] == SlotStatus.BOOKED

    office_b = Client(id=uuid.uuid4(), practice_name="Other Dental",
                      api_key=f"key-{uuid.uuid4()}", active=True)
    db.add(office_b)
    db.commit()

    foreign = cancel_appointment(db, office_b.id, appointment.id)
    unknown = cancel_appointment(db, office_b.id, uuid.uuid4())
    assert not foreign.success and foreign.reason == "slot_missing"
    assert foreign.appointment is None
    # Indistinguishable from an id that exists nowhere:
    assert (foreign.success, foreign.reason, foreign.appointment,
            foreign.detail) == (unknown.success, unknown.reason,
                                unknown.appointment, unknown.detail)

    assert _appointment_snapshot(db, appointment) == before
    assert _slot_snapshot(db, slot) == slot_before
    assert calls == {"sms": 0, "email": 0}


def test_concurrent_cancel_same_appointment_single_transition(
    engine, db, client_row, conversation_row, monkeypatch
):
    """T4: two real sessions in two threads race cancel_appointment on the
    SAME appointment. Exactly one result is ok and one already_cancelled,
    the final status is CANCELLED, the slot is released exactly once, and
    no partial state or notification exists. Bounded joins: a deadlock
    fails the test deterministically instead of hanging the suite."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.database import SessionLocal
    from app.services.booking_service import cancel_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    appointment_id, client_id = appointment.id, client_row.id
    barrier = threading.Barrier(2, timeout=15)
    results = {}

    def attempt(name):
        session = SessionLocal()
        try:
            barrier.wait()  # both threads in flight before either commits
            outcome = cancel_appointment(session, client_id, appointment_id)
            results[name] = (outcome.success, outcome.reason)  # plain data only
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "cancel/cancel race hung"

    assert set(results) == {"a", "b"}
    reasons = sorted(reason for _, reason in results.values())
    assert reasons == ["already_cancelled", "ok"], results   # exactly one each
    by_reason = {reason: success for success, reason in results.values()}
    assert by_reason["ok"] is True
    assert by_reason["already_cancelled"] is False           # approved D1

    db.refresh(appointment)
    assert appointment.status == AppointmentStatus.CANCELLED
    assert _slot_snapshot(db, slot) == (SlotStatus.AVAILABLE, None, None)
    assert calls == {"sms": 0, "email": 0}


def test_concurrent_cancel_and_confirm_deterministic(
    engine, db, client_row, conversation_row, monkeypatch
):
    """T5: cancel and confirm race on the same PENDING appointment. The
    appointment row lock serializes them, so ONLY two outcome pairs are
    legal:
      A. confirm wins:  confirm ok, then cancel ok — final CANCELLED with
         confirmed_at populated (preserved on the cancelled row).
      B. cancel wins:   cancel ok, confirm rejected not_confirmable with
         detail 'cancelled' — final CANCELLED with confirmed_at NULL.
    Either way the slot is released by the successful cancellation, and no
    impossible mixed state, duplicate side effect, or notification exists."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.database import SessionLocal
    from app.services.booking_service import cancel_appointment, confirm_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    appointment_id, client_id = appointment.id, client_row.id
    barrier = threading.Barrier(2, timeout=15)
    results = {}

    def do_confirm():
        session = SessionLocal()
        try:
            barrier.wait()
            outcome = confirm_appointment(session, client_id, appointment_id,
                                          now_utc=_confirm_t1())
            results["confirm"] = (outcome.success, outcome.reason, outcome.detail)
        finally:
            session.close()

    def do_cancel():
        session = SessionLocal()
        try:
            barrier.wait()
            outcome = cancel_appointment(session, client_id, appointment_id)
            results["cancel"] = (outcome.success, outcome.reason, outcome.detail)
        finally:
            session.close()

    threads = [threading.Thread(target=do_confirm),
               threading.Thread(target=do_cancel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "cancel/confirm race hung"

    assert set(results) == {"confirm", "cancel"}
    # The cancellation succeeds in BOTH legal orderings (pending and
    # confirmed are both cancellable).
    assert results["cancel"] == (True, "ok", None)

    db.refresh(appointment)
    assert appointment.status == AppointmentStatus.CANCELLED
    assert _slot_snapshot(db, slot) == (SlotStatus.AVAILABLE, None, None)

    if results["confirm"] == (True, "ok", None):
        # Ordering A: pending -> confirmed -> cancelled. The staff
        # confirmation instant survives on the cancelled row (audit trail).
        assert ensure_utc(appointment.confirmed_at) == _confirm_t1()
    else:
        # Ordering B: cancel won; the confirm loser deterministically saw
        # CANCELLED. No timestamp was ever written.
        assert results["confirm"] == (False, "not_confirmable",
                                      AppointmentStatus.CANCELLED)
        assert appointment.confirmed_at is None

    assert calls == {"sms": 0, "email": 0}


def test_cancel_commit_failure_rolls_back_cleanly(
    db, client_row, conversation_row, monkeypatch
):
    """T6: if the cancellation transaction's commit raises, the exception
    PROPAGATES (Rule 16 — no hidden failure, no false already-cancelled
    claim) and the rollback leaves no partial mutation: a FRESH session
    proves the appointment status, slot status, hold fields, and
    confirmed_at are all unchanged, and no notification executed."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.database import SessionLocal
    from app.services.booking_service import cancel_appointment

    calls = _trap_notification_channels(monkeypatch)
    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)
    appointment_id, slot_id = appointment.id, slot.id
    before = _appointment_snapshot(db, appointment)
    assert before[0] == AppointmentStatus.PENDING

    def commit_boom():
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(db, "commit", commit_boom)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        cancel_appointment(db, client_row.id, appointment_id)
    # The patched commit is left in place on purpose: verification below
    # reads committed truth through an INDEPENDENT session and never needs
    # this session to commit again (fixture teardown only rolls back).

    # Committed truth, read through an INDEPENDENT session: nothing changed.
    fresh = SessionLocal()
    try:
        from app.calendar_models import Appointment, AppointmentSlot
        persisted = fresh.get(Appointment, appointment_id)
        assert (persisted.status, persisted.confirmed_at,
                persisted.office_sms_sent, persisted.office_email_sent,
                persisted.patient_sms_sent, persisted.notify_error) == before
        persisted_slot = fresh.get(AppointmentSlot, slot_id)
        assert persisted_slot.status == SlotStatus.BOOKED
        assert persisted_slot.held_until is None
        assert persisted_slot.held_by_conversation_id is None
    finally:
        fresh.close()
    assert calls == {"sms": 0, "email": 0}


def test_cancel_route_status_mapping(engine, db, client_row, conversation_row):
    """T7: the admin cancel route end-to-end (route functions invoked
    directly with the session, the established pattern): 200 for a valid
    cancellation with the slot released; 404 with IDENTICAL wording for
    unknown and REAL cross-tenant ids; 409 for already-cancelled; 409 with
    the exact controlled wording for completed and no_show; and (correction
    pass 1) a stored status OUTSIDE AppointmentStatus.ALL is rejected with
    exactly "Appointment is unsupported and cannot be cancelled." — the raw
    stored value is never echoed and never rewritten. Every rejected call
    is mutation-free with no private data in the detail."""
    from fastapi import HTTPException
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.models import Client
    from app.routes.calendar import cancel_appointment as cancel_route

    appointment, slot = _booked_pending_appointment(db, client_row, conversation_row)

    # Valid cancellation -> 200-equivalent AppointmentView; slot released.
    view = cancel_route(appointment_id=appointment.id,
                        client_id=client_row.id, db=db,
                        authenticated_client=client_row)
    assert view.status == AppointmentStatus.CANCELLED
    assert _slot_snapshot(db, slot) == (SlotStatus.AVAILABLE, None, None)

    # Already cancelled -> 409 (unchanged wording, approved D1).
    with pytest.raises(HTTPException) as already_excinfo:
        cancel_route(appointment_id=appointment.id,
                     client_id=client_row.id, db=db,
                     authenticated_client=client_row)
    assert already_excinfo.value.status_code == 409
    assert already_excinfo.value.detail == "Appointment is already cancelled."

    # Unknown id -> 404.
    with pytest.raises(HTTPException) as unknown_excinfo:
        cancel_route(appointment_id=uuid.uuid4(),
                     client_id=client_row.id, db=db,
                     authenticated_client=client_row)
    assert unknown_excinfo.value.status_code == 404

    # REAL cross-tenant id -> 404 with IDENTICAL wording (no existence
    # leak). The conversation is free again after cancellation (Patch 1
    # rebooking guarantee), so a second live appointment is created for
    # office A and probed by office B authenticating AS ITSELF.
    second, second_slot = _booked_pending_appointment(
        db, client_row, conversation_row, hours_from_now=72.0)
    second_before = _appointment_snapshot(db, second)
    office_b = Client(id=uuid.uuid4(), practice_name="Other Dental",
                      api_key=f"key-{uuid.uuid4()}", active=True)
    db.add(office_b)
    db.commit()
    with pytest.raises(HTTPException) as foreign_excinfo:
        cancel_route(appointment_id=second.id,
                     client_id=office_b.id, db=db,
                     authenticated_client=office_b)
    assert foreign_excinfo.value.status_code == 404
    assert foreign_excinfo.value.detail == unknown_excinfo.value.detail
    assert _appointment_snapshot(db, second) == second_before  # untouched

    # Terminal statuses -> 409 with the EXACT controlled wording, and the
    # rejected call is mutation-free (row unchanged, slot NOT reopened).
    for terminal_status in (AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW):
        second.status = terminal_status  # direct write on purpose
        db.commit()
        terminal_before = _appointment_snapshot(db, second)
        slot_before = _slot_snapshot(db, second_slot)
        assert slot_before[0] == SlotStatus.BOOKED

        with pytest.raises(HTTPException) as terminal_excinfo:
            cancel_route(appointment_id=second.id,
                         client_id=client_row.id, db=db,
                         authenticated_client=client_row)
        assert terminal_excinfo.value.status_code == 409
        assert terminal_excinfo.value.detail == (
            f"Appointment is {terminal_status} and cannot be cancelled.")
        # Privacy: only the controlled status word — no patient, slot,
        # tenant, or database information.
        assert "Kevin" not in terminal_excinfo.value.detail
        assert "516" not in terminal_excinfo.value.detail
        assert str(second.id) not in terminal_excinfo.value.detail
        assert _appointment_snapshot(db, second) == terminal_before
        assert _slot_snapshot(db, second_slot) == slot_before

    # Correction pass 1: a stored status OUTSIDE AppointmentStatus.ALL
    # (malformed / legacy / manually edited row — the column has no CHECK
    # constraint). The lifecycle owner still rejects it by default, and
    # the boundary exposes ONLY the fixed sentinel "unsupported": the raw
    # stored value is never echoed and never repaired or rewritten.
    from app.services.booking_service import cancel_appointment as cancel_service
    malformed = "zz_bad"
    assert malformed not in AppointmentStatus.ALL
    second.status = malformed  # direct write on purpose (no CHECK constraint)
    db.commit()
    malformed_before = _appointment_snapshot(db, second)
    malformed_slot_before = _slot_snapshot(db, second_slot)
    assert malformed_slot_before[0] == SlotStatus.BOOKED

    # Service-level proof: BookingResult.detail is exactly the sentinel,
    # never the stored value, and the rejection is mutation-free
    # (appointment snapshot incl. confirmed_at and notification fields,
    # slot status, and hold fields all unchanged).
    service_result = cancel_service(db, client_row.id, second.id)
    assert not service_result.success
    assert service_result.reason == "not_cancellable"
    assert service_result.detail == "unsupported"
    assert _appointment_snapshot(db, second) == malformed_before
    assert _slot_snapshot(db, second_slot) == malformed_slot_before

    # Route-level proof: exact wording, HTTP 409, raw value absent, no
    # private data, and still mutation-free — the malformed stored value
    # is NOT repaired.
    with pytest.raises(HTTPException) as malformed_excinfo:
        cancel_route(appointment_id=second.id,
                     client_id=client_row.id, db=db,
                     authenticated_client=client_row)
    assert malformed_excinfo.value.status_code == 409
    assert malformed_excinfo.value.detail == (
        "Appointment is unsupported and cannot be cancelled.")
    assert malformed not in malformed_excinfo.value.detail
    assert "Kevin" not in malformed_excinfo.value.detail
    assert "516" not in malformed_excinfo.value.detail
    assert str(second.id) not in malformed_excinfo.value.detail
    assert _appointment_snapshot(db, second) == malformed_before
    assert _appointment_snapshot(db, second)[0] == malformed  # not rewritten
    assert _slot_snapshot(db, second_slot) == malformed_slot_before


# ---------------------------------------------------------------------------
# PHASE 3A SLICE 1 - staff booking (booking_service.finalize_staff_booking)
#
# The staff path books ONE existing authoritative slot for an authenticated
# office employee: no conversation, no conversational hold, no public policy,
# always CONFIRMED, always source portal_staff, never a notification.
#
# These tests use the SAME fixtures, the SAME _make_slot / _settings helpers
# and the SAME threading.Barrier + SessionLocal convention as the frozen
# concurrency tests above, so the staff path is judged by the rules that
# already protect patient bookings rather than by a second set of its own.
#
# The frozen chatbot tests above are untouched: finalize_booking's signature
# and body are unchanged by this slice.
# ---------------------------------------------------------------------------

STAFF_SOURCE = "portal_staff"


def _staff_kwargs(name="Kevin", phone="516-555-1234", email=None):
    """The patient details every staff-booking call in this section uses.
    Mirrors _finalize_kwargs above, minus the chatbot-only policy context
    (time_preference / service_key) that the staff path deliberately does
    not accept."""
    return dict(
        patient_name=name, patient_phone=phone, patient_email=email,
        new_or_returning="new", reason="cleaning/checkup", urgency="routine",
    )


def _staff_book(db, client, slot, now=None, **overrides):
    from app.services import booking_service
    kwargs = _staff_kwargs()
    kwargs.update(overrides)
    return booking_service.finalize_staff_booking(
        db, client.id, slot.id, now_utc=now or _now(), **kwargs
    )


def _active_appointments_for_slot(slot_id):
    """Count via an INDEPENDENT session, the frozen convention for verifying
    a race outcome without reading a participant's own identity map."""
    from app.database import SessionLocal
    from app.calendar_models import Appointment, AppointmentStatus
    verify = SessionLocal()
    try:
        return (
            verify.query(Appointment)
            .filter(
                Appointment.slot_id == slot_id,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .count()
        )
    finally:
        verify.close()


def _active_appointment_conversation_id(slot_id):
    """Read the surviving active appointment's PERSISTED conversation_id
    through an INDEPENDENT session.

    A BookingResult returned from a worker thread carries an ORM instance
    bound to that thread's SessionLocal(), which the thread closes in its
    finally block. Touching a lazy attribute on it afterwards raises
    DetachedInstanceError - a test-lifetime bug, not a service defect.
    Querying the committed row instead is also the stronger assertion: it
    proves what the DATABASE holds rather than what an object remembers.
    """
    from app.database import SessionLocal
    from app.calendar_models import Appointment, AppointmentStatus
    verify = SessionLocal()
    try:
        row = (
            verify.query(Appointment)
            .filter(
                Appointment.slot_id == slot_id,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .one()
        )
        return row.conversation_id
    finally:
        verify.close()


def _slot_state(slot_id):
    """Read a slot's post-race state through an independent session."""
    from app.database import SessionLocal
    from app.calendar_models import AppointmentSlot
    verify = SessionLocal()
    try:
        row = verify.get(AppointmentSlot, slot_id)
        return (row.status, row.held_until, row.held_by_conversation_id)
    finally:
        verify.close()


# ----- S1: the normal staff booking ----------------------------------------

def test_staff_booking_creates_one_confirmed_appointment(db, client_row):
    """S1 - every authoritative property of a staff-created appointment."""
    from app.calendar_models import Appointment, AppointmentStatus, SlotStatus
    slot = _make_slot(db, client_row, 48)

    result = _staff_book(db, client_row, slot)
    assert result.success and result.reason == "ok"

    appointment = result.appointment
    assert appointment is not None
    # THE conversation invariant for staff bookings.
    assert appointment.conversation_id is None
    assert appointment.source == STAFF_SOURCE
    assert appointment.status == AppointmentStatus.CONFIRMED
    # D5: confirmed_at records STAFF CONFIRMATIONS only. A directly-confirmed
    # appointment keeps NULL, exactly as an auto-confirmed one already does.
    assert appointment.confirmed_at is None
    # Tenant and times are DERIVED from the locked slot, never supplied.
    assert appointment.client_id == client_row.id
    assert appointment.slot_id == slot.id
    assert appointment.start_datetime == slot.start_datetime
    assert appointment.end_datetime == slot.end_datetime

    db.expire_all()
    refreshed = db.get(type(slot), slot.id)
    assert refreshed.status == SlotStatus.BOOKED
    assert refreshed.held_until is None
    assert refreshed.held_by_conversation_id is None
    assert _active_appointments_for_slot(slot.id) == 1
    assert db.query(Appointment).filter(
        Appointment.slot_id == slot.id).count() == 1


# ----- S2 / S3: staff bypasses the PUBLIC policy rules ----------------------

def test_staff_bypasses_public_minimum_notice(db, client_row):
    """S2 - the public rule genuinely refuses this slot; staff books it."""
    from app.services.availability_rules import evaluate_slot_policy
    # The fixture configures minimum_notice_minutes = 60.
    slot = _make_slot(db, client_row, 0.5)      # 30 minutes from now

    policy = evaluate_slot_policy(
        slot, now_utc=_now(), settings=_settings(client_row),
        time_preference="any", service_key=None,
    )
    assert not policy.eligible and policy.reason == "too_soon"

    result = _staff_book(db, client_row, slot)
    assert result.success and result.reason == "ok"


def test_staff_bypasses_public_booking_horizon(db, client_row):
    """S3 - beyond max_booking_days for the public, bookable by staff."""
    from app.services.availability_rules import evaluate_slot_policy
    settings = _shrink_horizon(db, client_row, 1)   # public horizon: tomorrow
    slot = _make_slot(db, client_row, 24 * 45)      # ~45 days out

    policy = evaluate_slot_policy(
        slot, now_utc=_now(), settings=settings,
        time_preference="any", service_key=None,
    )
    assert not policy.eligible and policy.reason == "beyond_horizon"

    result = _staff_book(db, client_row, slot)
    assert result.success and result.reason == "ok"


def test_staff_ignores_service_and_preference_filters(db, client_row):
    """S3b - a service-keyed slot outside any patient preference bucket is
    still bookable by staff: D3 removes those filters entirely, and the
    slot's own provider/service remain authoritative and untouched."""
    from app.repositories.appointment_repository import create_slot
    start = _now() + timedelta(hours=48)
    slot = create_slot(db, client_row.id, start, start + timedelta(minutes=45),
                       provider_name="Dr. Sorrentino", service_key="implant")
    db.commit()

    result = _staff_book(db, client_row, slot)
    assert result.success and result.reason == "ok"
    db.expire_all()
    refreshed = db.get(type(slot), slot.id)
    assert refreshed.provider_name == "Dr. Sorrentino"
    assert refreshed.service_key == "implant"


# ----- S4: the one time rule staff may NOT bypass --------------------------

def test_staff_cannot_book_a_started_or_past_slot(db, client_row):
    """S4 - notice and horizon are bypassed; the past is not. Mutation-free."""
    from app.calendar_models import Appointment, SlotStatus
    past = _make_slot(db, client_row, -2)          # started two hours ago

    result = _staff_book(db, client_row, past)
    assert not result.success and result.reason == "slot_started"
    assert result.appointment is None
    assert db.query(Appointment).filter(
        Appointment.slot_id == past.id).count() == 0
    db.expire_all()
    assert db.get(type(past), past.id).status == SlotStatus.AVAILABLE

    # Boundary: a slot whose start is exactly "now" has begun.
    boundary = _make_slot(db, client_row, 6)
    exact = _staff_book(db, client_row, boundary,
                        now=boundary.start_datetime)
    assert not exact.success and exact.reason == "slot_started"
    assert _active_appointments_for_slot(boundary.id) == 0


# ----- S5: an ACTIVE chatbot hold is never stolen ---------------------------

def test_staff_cannot_steal_an_active_chatbot_hold(db, client_row,
                                                   conversation_row):
    """S5 - a patient mid-booking owns the slot; staff loses safely and the
    hold is left exactly as it was."""
    from app.calendar_models import Appointment, SlotStatus
    from app.services.appointment_hold_service import place_hold
    slot = _make_slot(db, client_row, 48)

    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=_settings(client_row), time_preference="any",
                      service_key=None, now_utc=_now()).success
    db.commit()
    held_until_before = db.get(type(slot), slot.id).held_until

    result = _staff_book(db, client_row, slot)
    assert not result.success and result.reason == "slot_taken"
    assert db.query(Appointment).filter(
        Appointment.slot_id == slot.id).count() == 0

    status, held_until, held_by = _slot_state(slot.id)
    assert status == SlotStatus.HELD
    assert held_by == conversation_row.id
    assert held_until == held_until_before


def test_staff_may_reclaim_an_expired_hold(db, client_row, conversation_row):
    """S5b - D4: an EXPIRED hold is treated as available EVERYWHERE (the
    established lazy-reclaim rule that place_hold already applies). Staff
    reuses hold_is_active rather than defining 'expired' a second time, and
    clears the stale owner when it books."""
    from app.calendar_models import SlotStatus
    from app.services.appointment_hold_service import place_hold
    slot = _make_slot(db, client_row, 48)

    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=_settings(client_row), time_preference="any",
                      service_key=None, now_utc=_now()).success
    slot.held_until = _now() - timedelta(minutes=1)   # walked away
    db.commit()

    result = _staff_book(db, client_row, slot)
    assert result.success and result.reason == "ok"
    status, held_until, held_by = _slot_state(slot.id)
    assert status == SlotStatus.BOOKED
    assert held_until is None and held_by is None


# ----- S6: tenant isolation -------------------------------------------------

def test_staff_booking_is_tenant_isolated(db, client_row):
    """S6 - office B booking office A's slot is INDISTINGUISHABLE from a
    missing slot, and mutates nothing."""
    from app.models import Client
    from app.calendar_models import Appointment, SlotStatus
    slot = _make_slot(db, client_row, 48)
    office_b = Client(id=uuid.uuid4(), practice_name="Other Dental",
                      api_key=f"key-{uuid.uuid4()}", active=True)
    db.add(office_b)
    db.commit()

    foreign = _staff_book(db, office_b, slot)
    assert not foreign.success and foreign.reason == "slot_missing"
    assert db.query(Appointment).filter(
        Appointment.slot_id == slot.id).count() == 0
    db.expire_all()
    assert db.get(type(slot), slot.id).status == SlotStatus.AVAILABLE

    # An id that does not exist AT ALL yields the SAME reason - the two are
    # deliberately not distinguishable to a caller.
    class _Missing:
        id = uuid.uuid4()
    missing = _staff_book(db, client_row, _Missing())
    assert not missing.success and missing.reason == foreign.reason


# ----- S7: sequential duplicate + occupied slot -----------------------------

def test_staff_second_booking_of_same_slot_refused(db, client_row):
    """S7 - the slot is already BOOKED; the second attempt is refused with
    slot_taken and exactly one active appointment survives."""
    first = _make_slot(db, client_row, 48)
    assert _staff_book(db, client_row, first).success

    second = _staff_book(db, client_row, first, patient_name="Second Patient")
    assert not second.success and second.reason == "slot_taken"
    assert _active_appointments_for_slot(first.id) == 1


def test_staff_refuses_a_blocked_slot(db, client_row):
    """S7b - staff-blocked inventory is not bookable by staff either; the
    refusal is the frozen slot_blocked word, not slot_taken."""
    from app.calendar_models import Appointment, SlotStatus
    slot = _make_slot(db, client_row, 48)
    slot.status = SlotStatus.BLOCKED
    db.commit()

    result = _staff_book(db, client_row, slot)
    assert not result.success and result.reason == "slot_blocked"
    assert db.query(Appointment).filter(
        Appointment.slot_id == slot.id).count() == 0
    db.expire_all()
    assert db.get(type(slot), slot.id).status == SlotStatus.BLOCKED


# ----- S8: REAL concurrency, staff vs staff --------------------------------

def test_concurrent_staff_booking_same_slot_exactly_one_winner(
    engine, db, client_row
):
    """S8 - two receptionists claim the same slot at the same instant.

    Genuine PostgreSQL concurrency, using the frozen convention: two
    SessionLocal sessions in two threads meeting at a Barrier. The slot row
    lock serializes them; the loser must resolve to the CONTRACT reason
    slot_taken rather than raising, and exactly one appointment may exist.
    """
    from app.database import SessionLocal
    from app.calendar_models import SlotStatus
    from app.services import booking_service

    slot = _make_slot(db, client_row, 48)
    client_id, slot_id = client_row.id, slot.id
    barrier = threading.Barrier(2, timeout=15)
    results, errors = {}, {}

    def attempt(name):
        session = SessionLocal()
        try:
            barrier.wait()   # Maximize overlap of the two claims.
            results[name] = booking_service.finalize_staff_booking(
                session, client_id, slot_id, now_utc=_now(),
                **_staff_kwargs(name=name),
            )
        except Exception as exc:
            errors[name] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(n,))
               for n in ("Receptionist A", "Receptionist B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"staff booking raised instead of returning: {errors}"
    winners = [n for n, r in results.items() if r.success]
    losers = [n for n, r in results.items() if not r.success]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    # THE contract assertion: the loser is classified, not surfaced raw.
    assert results[losers[0]].reason == "slot_taken", results
    assert results[losers[0]].appointment is None

    assert _active_appointments_for_slot(slot_id) == 1
    status, held_until, held_by = _slot_state(slot_id)
    assert status == SlotStatus.BOOKED
    assert held_until is None and held_by is None


# ----- S9: REAL concurrency, staff vs chatbot ------------------------------

def test_concurrent_chat_finalize_beats_staff_on_already_held_slot(
    engine, db, client_row, conversation_row
):
    """S9 - concurrent, but from an ALREADY-HELD starting state.

    The hold is placed and COMMITTED before the Barrier, so when both
    threads start an active conversational hold already owns the slot.
    The outcome is therefore DETERMINISTIC, not a coin toss: the holder
    finalizes and staff must refuse. Claiming staff could legitimately win
    from this state would be claiming staff can steal a live hold.

    What this proves is that the two paths still serialize correctly on
    the same row lock when they collide - not who wins a fair race. The
    genuine cross-path race starts from an AVAILABLE slot and is the
    separate test below.
    """
    from app.database import SessionLocal
    from app.calendar_models import SlotStatus
    from app.services import booking_service
    from app.services.appointment_hold_service import place_hold

    settings = _settings(client_row)
    slot = _make_slot(db, client_row, 48)
    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=settings, time_preference="any",
                      service_key=None, now_utc=_now()).success
    db.commit()

    client_id, slot_id, conversation_id = (
        client_row.id, slot.id, conversation_row.id)
    barrier = threading.Barrier(2, timeout=15)
    results, errors = {}, {}

    def chat_attempt():
        session = SessionLocal()
        try:
            barrier.wait()
            results["chat"] = booking_service.finalize_booking(
                session, client_id, slot_id, conversation_id,
                settings=settings, now_utc=_now(), **_finalize_kwargs(),
            )
        except Exception as exc:
            errors["chat"] = exc
        finally:
            session.close()

    def staff_attempt():
        session = SessionLocal()
        try:
            barrier.wait()
            results["staff"] = booking_service.finalize_staff_booking(
                session, client_id, slot_id, now_utc=_now(),
                **_staff_kwargs(name="Walk-in Patient"),
            )
        except Exception as exc:
            errors["staff"] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=chat_attempt),
               threading.Thread(target=staff_attempt)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"a booking raised instead of returning: {errors}"
    # DETERMINISTIC: the holder owns the slot before either thread runs.
    assert results["chat"].success is True, results
    assert results["chat"].reason == "ok", results
    assert results["staff"].success is False, results
    assert results["staff"].reason == "slot_taken", results
    assert results["staff"].appointment is None

    assert _active_appointments_for_slot(slot_id) == 1
    # Verified against the COMMITTED row through an independent session -
    # the worker thread has already closed the session its BookingResult's
    # ORM instance was bound to.
    assert _active_appointment_conversation_id(slot_id) == conversation_id

    status, held_until, held_by = _slot_state(slot_id)
    assert status == SlotStatus.BOOKED
    assert held_until is None and held_by is None



def test_concurrent_place_hold_versus_staff_booking_on_available_slot(
    engine, db, client_row, conversation_row
):
    """S9c - THE cross-path race, and the primary proof that the new staff
    path and the existing patient hold path share ONE inventory lock.

    Both threads start against an initially AVAILABLE slot and meet at the
    Barrier. Thread A calls appointment_hold_service.place_hold; thread B
    calls booking_service.finalize_staff_booking. Neither is pre-committed,
    so EITHER may legitimately win - and exactly one must.

    Forbidden final states, asserted explicitly below:
      - both operations reporting success
      - a BOOKED appointment while the slot is still HELD
      - an active appointment coexisting with an active foreign hold
      - more than one active appointment
      - an uncaught exception from either participant
    """
    from app.database import SessionLocal
    from app.calendar_models import SlotStatus
    from app.services import booking_service
    from app.services.appointment_hold_service import place_hold

    settings = _settings(client_row)
    slot = _make_slot(db, client_row, 48)
    db.commit()
    assert _slot_state(slot.id)[0] == SlotStatus.AVAILABLE

    client_id, slot_id, conversation_id = (
        client_row.id, slot.id, conversation_row.id)
    barrier = threading.Barrier(2, timeout=15)
    results, errors = {}, {}

    def hold_attempt():
        session = SessionLocal()
        try:
            barrier.wait()
            results["hold"] = place_hold(
                session, client_id, slot_id, conversation_id,
                settings=settings, time_preference="any",
                service_key=None, now_utc=_now(),
            )
        except Exception as exc:
            errors["hold"] = exc
        finally:
            session.close()

    def staff_attempt():
        session = SessionLocal()
        try:
            barrier.wait()
            results["staff"] = booking_service.finalize_staff_booking(
                session, client_id, slot_id, now_utc=_now(),
                **_staff_kwargs(name="Walk-in Patient"),
            )
        except Exception as exc:
            errors["staff"] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=hold_attempt),
               threading.Thread(target=staff_attempt)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # FORBIDDEN: an uncaught exception from either participant.
    assert not errors, f"a participant raised instead of returning: {errors}"

    # FORBIDDEN: both reporting success. Exactly one claim wins.
    winners = [name for name, r in results.items() if r.success]
    assert len(winners) == 1, results

    status, held_until, held_by = _slot_state(slot_id)
    active = _active_appointments_for_slot(slot_id)

    if winners == ["hold"]:
        assert results["hold"].success is True
        assert results["staff"].success is False, results
        assert results["staff"].reason == "slot_taken", results
        assert results["staff"].appointment is None
        # The patient owns the slot: no appointment exists yet at all.
        assert active == 0
        assert status == SlotStatus.HELD
        assert held_by == conversation_id
        assert held_until is not None
    else:
        assert winners == ["staff"], results
        assert results["staff"].success is True
        assert results["hold"].success is False, results
        assert results["hold"].reason == "slot_taken", results
        assert active == 1
        assert status == SlotStatus.BOOKED
        # FORBIDDEN: a booked slot still carrying hold bookkeeping.
        assert held_until is None
        assert held_by is None

    # FORBIDDEN in BOTH branches, restated as unconditional invariants:
    #   a BOOKED slot with no appointment, or an appointment on a HELD slot,
    #   or an active appointment beside an active foreign hold.
    assert not (status == SlotStatus.BOOKED and active != 1), (status, active)
    assert not (status == SlotStatus.HELD and active != 0), (status, active)
    assert not (active >= 1 and held_by is not None), (active, held_by)
    assert active <= 1


def test_chatbot_hold_refused_after_staff_books_the_slot(db, client_row,
                                                         conversation_row):
    """S9d - the staff-first serial ordering, mirroring S5's chatbot-first.

    S5 proves a chatbot hold blocks staff. This proves the reverse: once
    staff has booked, a later place_hold is refused with the frozen
    slot_taken word, the slot stays BOOKED, and no hold bookkeeping appears
    on a slot that already has an appointment.
    """
    from app.calendar_models import SlotStatus
    from app.services.appointment_hold_service import place_hold

    slot = _make_slot(db, client_row, 48)
    booked = _staff_book(db, client_row, slot, patient_name="Walk-in")
    assert booked.success and booked.reason == "ok"

    later_hold = place_hold(db, client_row.id, slot.id, conversation_row.id,
                            settings=_settings(client_row),
                            time_preference="any", service_key=None,
                            now_utc=_now())
    assert not later_hold.success
    assert later_hold.reason == "slot_taken"
    assert later_hold.held_until is None

    status, held_until, held_by = _slot_state(slot.id)
    assert status == SlotStatus.BOOKED
    assert held_until is None and held_by is None
    assert _active_appointments_for_slot(slot.id) == 1

def test_staff_wins_when_chatbot_hold_already_expired(
    engine, db, client_row, conversation_row
):
    """S9b - the OTHER meaningful ordering, made deterministic: the patient
    walked away and the hold expired, so staff legitimately reclaims the slot
    and the patient's later finalize is refused by its own frozen rule."""
    from app.calendar_models import SlotStatus
    from app.services import booking_service
    from app.services.appointment_hold_service import place_hold

    settings = _settings(client_row)
    slot = _make_slot(db, client_row, 48)
    assert place_hold(db, client_row.id, slot.id, conversation_row.id,
                      settings=settings, time_preference="any",
                      service_key=None, now_utc=_now()).success
    slot.held_until = _now() - timedelta(minutes=1)
    db.commit()

    staff = _staff_book(db, client_row, slot, patient_name="Walk-in")
    assert staff.success and staff.reason == "ok"

    late = booking_service.finalize_booking(
        db, client_row.id, slot.id, conversation_row.id,
        settings=settings, now_utc=_now(), **_finalize_kwargs(),
    )
    assert not late.success
    assert late.reason == "hold_lost"
    assert _active_appointments_for_slot(slot.id) == 1
    assert _slot_state(slot.id)[0] == SlotStatus.BOOKED


# ----- S10: the index is still the final arbiter ---------------------------

def test_staff_slot_unique_index_enforced_when_lock_bypassed_sequential(
    engine, db, client_row
):
    """S10 - database correctness must not depend on the service pre-checks.

    A staff-shaped appointment (conversation_id NULL) is inserted directly
    through the repository WITHOUT the slot lock, twice. The first commits;
    only uq_active_appointment_per_slot can refuse the second. This also
    proves the partial index still fires when conversation_id IS NULL, where
    uq_active_appointment_per_conversation cannot apply at all.
    """
    from sqlalchemy.exc import IntegrityError
    from app.database import SessionLocal
    from app.calendar_models import AppointmentSlot, AppointmentStatus
    from app.repositories import appointment_repository
    from app.services import booking_service

    slot = _make_slot(db, client_row, 48)
    slot_id = slot.id

    def bypass_insert(patient):
        session = SessionLocal()
        try:
            raw_slot = session.get(AppointmentSlot, slot_id)  # No FOR UPDATE.
            appointment_repository.create_appointment_from_slot(
                session, slot=raw_slot, conversation_id=None,
                status=AppointmentStatus.CONFIRMED,
                patient_name=patient, patient_phone="516-555-1234",
                patient_email=None, new_or_returning="new",
                reason="cleaning/checkup", urgency="routine",
                source=booking_service.STAFF_BOOKING_SOURCE,
            )
            session.commit()
        finally:
            session.close()

    bypass_insert("First")
    with pytest.raises(IntegrityError) as excinfo:
        bypass_insert("Second")

    driver_error = excinfo.value.orig
    assert getattr(driver_error, "pgcode", None) == "23505"
    assert getattr(driver_error.diag, "constraint_name", None) == (
        "uq_active_appointment_per_slot"
    )
    assert _active_appointments_for_slot(slot_id) == 1



def test_staff_booking_maps_slot_index_violation_to_slot_taken(
    engine, db, client_row
):
    """S10c - ADVERSARIAL: prove the unique index is the FINAL arbiter even
    when slot state and appointment state have been left inconsistent, and
    that finalize_staff_booking CLASSIFIES the resulting IntegrityError
    rather than letting it escape.

    Why this test has to exist. In a normal staff-vs-staff race the row lock
    serializes the two callers, so the loser reads slot.status == BOOKED and
    returns slot_taken from the pre-check - the IntegrityError handler is
    never reached. That is correct behaviour, but it means the handler needs
    its own proof, because a mutation that deletes it survives every ordinary
    race test.

    The adversarial state is manufactured deliberately: an independent
    session inserts and COMMITS one active appointment for the slot through
    the repository primitive WITHOUT updating the slot to BOOKED - exactly
    what a future bug, a mis-ordered code path, or a manual database edit
    would leave behind. Because the locked slot still reads AVAILABLE, every
    service pre-check passes and the staff path reaches its INSERT. From
    there, only uq_active_appointment_per_slot can refuse it.

    Both pre-existing and new appointments carry conversation_id NULL, so
    uq_active_appointment_per_conversation (partial on IS NOT NULL) cannot
    fire: the SLOT index is provably the constraint under test.
    """
    from sqlalchemy.exc import IntegrityError
    from app.database import SessionLocal
    from app.calendar_models import (
        Appointment, AppointmentSlot, AppointmentStatus, SlotStatus,
    )
    from app.repositories import appointment_repository
    from app.services import booking_service

    slot = _make_slot(db, client_row, 48)
    slot_id = slot.id

    # Manufacture the inconsistency: an ACTIVE appointment for this slot,
    # committed by another writer, while the slot row is left AVAILABLE.
    planted = SessionLocal()
    try:
        raw_slot = planted.get(AppointmentSlot, slot_id)   # No FOR UPDATE.
        appointment_repository.create_appointment_from_slot(
            planted, slot=raw_slot, conversation_id=None,
            status=AppointmentStatus.CONFIRMED,
            patient_name="Planted Patient", patient_phone="516-555-0000",
            patient_email=None, new_or_returning="new",
            reason="cleaning/checkup", urgency="routine",
            source=booking_service.STAFF_BOOKING_SOURCE,
        )
        planted.commit()
    finally:
        planted.close()

    # PRECONDITIONS - these are what force the INSERT branch. If either ever
    # stopped holding, this test would silently stop proving anything.
    status_before, held_until_before, held_by_before = _slot_state(slot_id)
    assert status_before == SlotStatus.AVAILABLE, (
        "the slot must still read AVAILABLE, otherwise the staff path would "
        "refuse from the pre-check and never reach the INSERT")
    assert held_until_before is None and held_by_before is None
    assert _active_appointments_for_slot(slot_id) == 1

    # The staff path now runs completely normally against that slot.
    db.expire_all()
    try:
        result = _staff_book(db, client_row, slot, patient_name="Walk-in")
    except IntegrityError:
        pytest.fail(
            "finalize_staff_booking let an IntegrityError ESCAPE instead of "
            "classifying uq_active_appointment_per_slot as slot_taken",
            pytrace=False,
        )

    # THE assertion M8 exists to break: the violation is classified, not raised.
    assert result.success is False
    assert result.reason == "slot_taken"
    assert result.appointment is None

    # The refused transaction rolled back completely: no second appointment,
    # and the slot was NOT left mutated to BOOKED by the failed attempt.
    assert _active_appointments_for_slot(slot_id) == 1
    status_after, held_until_after, held_by_after = _slot_state(slot_id)
    assert status_after == SlotStatus.AVAILABLE
    assert held_until_after is None and held_by_after is None

    # The surviving appointment is the PLANTED one, untouched.
    verify = SessionLocal()
    try:
        survivor = (
            verify.query(Appointment)
            .filter(
                Appointment.slot_id == slot_id,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .one()
        )
        assert survivor.patient_name == "Planted Patient"
        assert survivor.conversation_id is None
    finally:
        verify.close()

def test_staff_booking_after_cancellation_is_allowed(db, client_row):
    """S10b - the per-slot index is PARTIAL on status <> 'cancelled', so
    cancelled history never blocks rebooking the same slot. This is what lets
    the Phase 2B cancelled ghost and a new live appointment coexist."""
    from app.calendar_models import AppointmentStatus, SlotStatus
    from app.services.booking_service import cancel_appointment
    slot = _make_slot(db, client_row, 48)

    first = _staff_book(db, client_row, slot, patient_name="Maria Lopez")
    assert first.success
    cancelled = cancel_appointment(db, client_row.id, first.appointment.id)
    assert cancelled.success
    db.expire_all()
    assert db.get(type(slot), slot.id).status == SlotStatus.AVAILABLE

    second = _staff_book(db, client_row, slot, patient_name="New Patient")
    assert second.success and second.reason == "ok"
    assert _active_appointments_for_slot(slot.id) == 1
    # The cancelled row is preserved, not rewritten.
    db.expire_all()
    assert db.get(type(first.appointment), first.appointment.id).status == (
        AppointmentStatus.CANCELLED)


# ----- S11 / S12: patient data --------------------------------------------

def test_staff_booking_email_is_optional(db, client_row):
    """S11 - email is optional and stays optional. None and blank both
    persist as NULL; nothing in this path requires an email."""
    from app.calendar_models import Appointment
    none_slot = _make_slot(db, client_row, 48)
    none_result = _staff_book(db, client_row, none_slot, patient_email=None)
    assert none_result.success
    assert none_result.appointment.patient_email is None

    blank_slot = _make_slot(db, client_row, 50)
    blank_result = _staff_book(db, client_row, blank_slot,
                               patient_email="   ")
    assert blank_result.success
    assert blank_result.appointment.patient_email is None

    typed_slot = _make_slot(db, client_row, 52)
    typed = _staff_book(db, client_row, typed_slot,
                        patient_email="rosa@example.test")
    assert typed.success
    assert typed.appointment.patient_email == "rosa@example.test"
    assert db.query(Appointment).filter(
        Appointment.patient_email.is_(None)).count() >= 2


@pytest.mark.parametrize("name,phone", [
    ("   ", "516-555-1234"),
    ("Kevin", "   "),
    ("", ""),
])
def test_staff_booking_requires_name_and_phone(db, client_row, name, phone):
    """S12 - the SHARED insert primitive's validation still applies, and a
    refusal is mutation-free."""
    from app.calendar_models import Appointment, SlotStatus
    slot = _make_slot(db, client_row, 48)

    result = _staff_book(db, client_row, slot,
                         patient_name=name, patient_phone=phone)
    assert not result.success and result.reason == "invalid_patient_data"
    assert result.appointment is None
    assert db.query(Appointment).filter(
        Appointment.slot_id == slot.id).count() == 0
    db.expire_all()
    assert db.get(type(slot), slot.id).status == SlotStatus.AVAILABLE


# ----- S13: no notification side effects ------------------------------------

def test_staff_booking_sends_no_notification(db, client_row, monkeypatch):
    """S13 - D7: staff booking notifies nobody. Proven at the boundary by
    making every notification entry point explode; the booking must still
    succeed, which it can only do by never calling them."""
    from app.services import notification_service

    def _explode(*args, **kwargs):
        raise AssertionError("staff booking must not send any notification")

    monkeypatch.setattr(notification_service, "send_booking_notifications",
                        _explode)
    monkeypatch.setattr(notification_service, "_send_sms", _explode)
    monkeypatch.setattr(notification_service, "_send_email", _explode)

    slot = _make_slot(db, client_row, 48)
    result = _staff_book(db, client_row, slot)
    assert result.success and result.reason == "ok"

    # And the per-channel bookkeeping is untouched: nothing was attempted.
    appointment = result.appointment
    assert appointment.office_sms_sent is False
    assert appointment.office_email_sent is False
    assert appointment.patient_sms_sent is False
    assert appointment.notify_error is None


def test_staff_booking_records_no_notification_attempt_rows(db, client_row):
    """S13b - the migration-006 ledger is the durable record of any send
    attempt. A staff booking must leave it completely empty."""
    from sqlalchemy import text
    slot = _make_slot(db, client_row, 48)
    result = _staff_book(db, client_row, slot)
    assert result.success

    attempts = db.execute(
        text("SELECT count(*) FROM notification_attempts "
             "WHERE appointment_id = :appointment_id"),
        {"appointment_id": str(result.appointment.id)},
    ).scalar()
    assert attempts == 0


# ----- S14: structural reuse + the chatbot freeze ---------------------------

def test_staff_path_reuses_the_shared_primitives_structurally(db, client_row):
    """S14 - the staff path must go THROUGH the shared locking and insert
    primitives, not beside them. Proven by monkeypatching each shared owner
    and asserting the staff path observes it.

    This is the source-level companion to the concurrency tests above: those
    prove the invariant holds, this proves it holds for the RIGHT reason.
    """
    from app.repositories import appointment_repository
    from app.services import booking_service
    calls = {"lock": 0, "insert": 0}

    real_lock = appointment_repository.get_slot_for_update
    real_insert = appointment_repository.create_appointment_from_slot

    def counting_lock(*args, **kwargs):
        calls["lock"] += 1
        return real_lock(*args, **kwargs)

    def counting_insert(*args, **kwargs):
        calls["insert"] += 1
        return real_insert(*args, **kwargs)

    slot = _make_slot(db, client_row, 48)
    original_lock = booking_service.appointment_repository.get_slot_for_update
    original_insert = (
        booking_service.appointment_repository.create_appointment_from_slot)
    booking_service.appointment_repository.get_slot_for_update = counting_lock
    booking_service.appointment_repository.create_appointment_from_slot = (
        counting_insert)
    try:
        result = _staff_book(db, client_row, slot)
    finally:
        booking_service.appointment_repository.get_slot_for_update = (
            original_lock)
        booking_service.appointment_repository.create_appointment_from_slot = (
            original_insert)

    assert result.success
    assert calls["lock"] == 1, "staff booking must take the SHARED row lock"
    assert calls["insert"] == 1, "staff booking must use the SHARED insert"


def test_frozen_chatbot_finalize_signature_is_unchanged(db, client_row):
    """S14b - Slice 1 must not have touched the chatbot contract. The exact
    positional/keyword shape the frozen suite and booking_conversation both
    depend on is pinned here."""
    import inspect
    from app.services.booking_service import finalize_booking, \
        finalize_staff_booking

    frozen = list(inspect.signature(finalize_booking).parameters)
    assert frozen == [
        "db", "client_id", "slot_id", "conversation_id",
        "settings", "now_utc", "time_preference", "service_key",
        "patient_name", "patient_phone", "patient_email",
        "new_or_returning", "reason", "urgency",
    ]
    positional = [
        name for name, p in
        inspect.signature(finalize_booking).parameters.items()
        if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    assert positional == ["db", "client_id", "slot_id", "conversation_id"]

    # And the staff path deliberately accepts NONE of the server-owned or
    # chatbot-only inputs - it cannot be told who to be or when to be.
    staff = inspect.signature(finalize_staff_booking).parameters
    for forbidden in ("conversation_id", "source", "status", "settings",
                      "time_preference", "service_key", "start_datetime",
                      "end_datetime", "provider_name", "service"):
        assert forbidden not in staff, forbidden


def test_staff_booking_source_constant_is_server_owned(db, client_row):
    """S14c - D6: the source value is a named service constant. A caller
    cannot choose it, and the constant is the one thing a typo would have to
    get past."""
    from app.services import booking_service
    assert booking_service.STAFF_BOOKING_SOURCE == STAFF_SOURCE

    slot = _make_slot(db, client_row, 48)
    result = _staff_book(db, client_row, slot)
    assert result.appointment.source == booking_service.STAFF_BOOKING_SOURCE
    # The chatbot default is a DIFFERENT value; the two provenances stay
    # distinguishable in the appointments table.
    assert booking_service.STAFF_BOOKING_SOURCE != "mia_widget"
