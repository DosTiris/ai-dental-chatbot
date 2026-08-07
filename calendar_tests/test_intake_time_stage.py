# calendar_tests/test_intake_time_stage.py
#
# C2-A.3 intake-gap regression: the ROUTE-level intake time-window path
# (mode "intake_time_window_capture") must carry the gated
# calendar_picker {"stage": "time_preference"} signal when - and only
# when - this turn's reply is the day-only preference question and all
# three calendar gates are strict Boolean true.
#
# Production defect reproduced here (Dos Tiris controlled tenant): the
# patient completed intake through the optional-email skip, answered a
# day only ("Tuesday"), received "Got it \u2014 do you prefer morning or
# afternoon?" - and the response carried NO calendar_picker field, so
# the widget could not render the Morning/Afternoon buttons. The signal
# owner is intake_time_preference_stage_signal in the booking service
# (single owner; the route only merges its returned metadata).
#
# Sequencing contract (owner-corrected): the defect occurs BEFORE
# patient type is collected. The exact-path test drives
#   reason -> name -> phone -> email skip -> <weekday>
# and asserts at that response boundary with lead_is_new_patient still
# unset; patient type is asked only after a preference is supplied.
#
# Run (PostgreSQL required, as every calendar_tests module):
#   python -m pytest calendar_tests/test_intake_time_stage.py -v

import uuid
from datetime import timedelta

import pytest

from app.models import Conversation
import app.routes.chat as chat_module
from app.services.booking_conversation import (
    INTAKE_TIME_PREFERENCE_PROMPT,
    intake_time_preference_stage_signal,
)

# Importing these registers the autouse `fakes` fixture in this module too.
from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes,
    make_client,
    make_conversation,
    send,
)

TIME_SIGNAL = {"stage": "time_preference"}

# Sentinel meaning "remove this flag entirely" in the gate matrix.
OMIT = object()


def _set_calendar_flags(db, client, *, booking=True, actions=True, picker=True):
    """Author the three C2-A.3 gates on an existing harness client.

    Values pass through UNCHANGED (so the malformed-shape matrix can
    plant "true", 1, None, [], {}), and the OMIT sentinel removes the
    key entirely (the missing-flag matrix). The settings dict is
    reassigned so SQLAlchemy detects the JSON change.
    """
    settings = dict(client.settings or {})
    calendar = dict(settings.get("calendar") or {})
    for key, value in (
        ("booking_enabled", booking),
        ("calendar_actions_enabled", actions),
        ("calendar_picker_enabled", picker),
    ):
        if value is OMIT:
            calendar.pop(key, None)
        else:
            calendar[key] = value
    settings["calendar"] = calendar
    client.settings = settings
    db.add(client)
    db.commit()
    return client


def _gated_client(db, **flags):
    client = make_client(db, calendar_enabled=True)
    return _set_calendar_flags(db, client, **flags)


def _pre_time_window_conversation(db, client):
    """A conversation exactly at the time-window question: reason, name,
    phone captured and email skipped - patient type deliberately UNSET
    (owner sequencing contract: prepopulating it could transfer
    ownership to a later booking path and mask the production defect)."""
    return make_conversation(
        db,
        client,
        lead_time_window=None,
        lead_email_opt_out=True,
        lead_is_new_patient=None,
    )


def _upcoming_weekday_text(db, client):
    """A weekday name 2-6 days ahead in the client's local calendar, so a
    literal 'Tuesday' can never collide with the today-handling edge on
    whichever day the suite runs."""
    today = chat_module.get_client_now(client).date()
    for ahead in (2, 3, 4, 5, 6):
        candidate = today + timedelta(days=ahead)
        if candidate.weekday() < 5:  # Mon-Fri
            return candidate.strftime("%A")
    raise AssertionError("unreachable: a weekday always exists within 6 days")


def _conversation_row(db, resp):
    return db.query(Conversation).filter(
        Conversation.id == uuid.UUID(resp.conversation_id)
    ).one()


# ---------------------------------------------------------------------------
# 1. The exact production sequence, end to end
# ---------------------------------------------------------------------------

def test_exact_production_sequence_day_only_carries_signal(db, fakes):
    client = _gated_client(db)

    # reason (fresh conversation; the widget sends no conversation_id)
    r1 = send(db, client, None, "I'd like to book a cleaning")
    conversation = _conversation_row(db, r1)
    # Package A: New/Returning is asked first, right after the reason.
    assert "new or returning" in r1.reply.lower()

    # patient type -> name
    r_pt = send(db, client, conversation, "new patient")
    assert "name" in r_pt.reply.lower()

    # name
    r2 = send(db, client, conversation, "Casey Patient")
    assert "phone" in r2.reply.lower()

    # phone
    r3 = send(db, client, conversation, "516-555-1234")
    assert "email" in r3.reply.lower()

    # optional email skip
    r4 = send(db, client, conversation, "skip email")
    assert conversation.lead_email_opt_out is True

    # day-only time-window answer - THE production response boundary.
    day_text = _upcoming_weekday_text(db, client)
    r5 = send(db, client, conversation, day_text)
    assert r5.reply == INTAKE_TIME_PREFERENCE_PROMPT
    assert r5.meta.get("mode") == "intake_time_window_capture"
    assert r5.meta.get("calendar_picker") == TIME_SIGNAL
    # Package A: patient type was collected FIRST (right after the reason).
    assert conversation.lead_is_new_patient is not None

    # Typed preference completes the time window; since patient type is already
    # collected, this is the LAST field and the flow advances into the existing
    # Calendar path rather than re-asking New/Returning here.
    r6 = send(db, client, conversation, "morning")
    assert "new or returning" not in r6.reply.lower()
    assert (conversation.lead_time_window or "").endswith("morning")


# ---------------------------------------------------------------------------
# 2. Same boundary from the pre-seeded pre-time-window state
# ---------------------------------------------------------------------------

def test_preseeded_day_only_boundary_carries_signal(db, fakes):
    client = _gated_client(db)
    conversation = _pre_time_window_conversation(db, client)

    resp = send(db, client, conversation, _upcoming_weekday_text(db, client))

    assert resp.reply == INTAKE_TIME_PREFERENCE_PROMPT
    assert resp.meta.get("mode") == "intake_time_window_capture"
    assert resp.meta.get("calendar_picker") == TIME_SIGNAL
    assert conversation.lead_is_new_patient is None


# ---------------------------------------------------------------------------
# 3. Gate matrix: false, missing, and malformed flags all suppress the
#    signal with byte-identical pre-patch metadata (no calendar_picker key)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flags", [
    {"booking": False},
    {"actions": False},
    {"picker": False},
    {"booking": OMIT},
    {"actions": OMIT},
    {"picker": OMIT},
], ids=["booking-false", "actions-false", "picker-false",
        "booking-missing", "actions-missing", "picker-missing"])
def test_disabled_or_missing_flag_suppresses_signal(db, fakes, flags):
    client = _gated_client(db, **flags)
    conversation = _pre_time_window_conversation(db, client)

    resp = send(db, client, conversation, _upcoming_weekday_text(db, client))

    # The reply itself is unchanged; only the signal is gated off.
    assert resp.reply == INTAKE_TIME_PREFERENCE_PROMPT
    assert resp.meta.get("mode") == "intake_time_window_capture"
    assert "calendar_picker" not in resp.meta


@pytest.mark.parametrize("flag", ["booking", "actions", "picker"])
@pytest.mark.parametrize("malformed", ["true", 1, None, [], {}],
                         ids=["string-true", "int-1", "null", "array", "object"])
def test_malformed_flag_suppresses_signal(db, fakes, flag, malformed):
    # Independent malformed-shape coverage for ALL THREE strict gates
    # (booking_enabled, calendar_actions_enabled, calendar_picker_enabled):
    # only strict Boolean True passes; every truthy-but-wrong shape and
    # null suppresses with the reply itself unchanged.
    client = _gated_client(db, **{flag: malformed})
    conversation = _pre_time_window_conversation(db, client)

    resp = send(db, client, conversation, _upcoming_weekday_text(db, client))

    assert resp.reply == INTAKE_TIME_PREFERENCE_PROMPT
    assert "calendar_picker" not in resp.meta


# ---------------------------------------------------------------------------
# 4. Negative paths: replies that are not the day-only preference
#    question never advertise the preference stage
# ---------------------------------------------------------------------------

def test_day_plus_preference_does_not_advertise_stage(db, fakes):
    client = _gated_client(db)
    conversation = _pre_time_window_conversation(db, client)

    day_text = _upcoming_weekday_text(db, client)
    resp = send(db, client, conversation, f"{day_text} morning")

    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert "calendar_picker" not in (resp.meta or {})


def test_exact_time_does_not_advertise_stage(db, fakes):
    client = _gated_client(db)
    conversation = _pre_time_window_conversation(db, client)

    day_text = _upcoming_weekday_text(db, client)
    resp = send(db, client, conversation, f"{day_text} at 10am")

    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert "calendar_picker" not in (resp.meta or {})


def test_weekend_day_input_does_not_advertise_stage(db, fakes):
    # Owner-verified behavior note: this harness client has NO
    # office_hours, so "Saturday" is NOT rejected as a weekend day; it
    # takes a different (non-preference-prompt) intake path. This test
    # deliberately pins only the signal contract for that path - the
    # reply is not the day-only acceptance prompt and must carry no
    # stage signal. A true weekend-REJECTION negative would require an
    # office_hours-configured client and is intentionally out of scope
    # here (recorded deviation in the package report).
    client = _gated_client(db)
    conversation = _pre_time_window_conversation(db, client)

    resp = send(db, client, conversation, "Saturday")

    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert "calendar_picker" not in (resp.meta or {})


def test_past_day_correction_does_not_advertise_stage(db, fakes):
    client = _gated_client(db)
    conversation = _pre_time_window_conversation(db, client)

    resp = send(db, client, conversation, "yesterday")

    assert resp.reply != INTAKE_TIME_PREFERENCE_PROMPT
    assert "calendar_picker" not in (resp.meta or {})


def test_unrelated_intake_prompt_does_not_advertise_stage(db, fakes):
    # Mid-intake (name stage): the phone question is not a time-stage
    # reply and must carry no signal.
    client = _gated_client(db)
    r1 = send(db, client, None, "I'd like to book a cleaning")
    conversation = _conversation_row(db, r1)
    # Package A: New/Returning is asked first; answer it, then the name turn.
    send(db, client, conversation, "new patient")

    r2 = send(db, client, conversation, "Casey Patient")

    assert "phone" in r2.reply.lower()
    assert "calendar_picker" not in (r2.meta or {})


def test_exact_time_outside_office_hours_correction_does_not_advertise_stage(
        db, fakes):
    # GENUINE office-hours negative for THIS early intake route: the
    # capture owner validates through build_time_window_issue_reply,
    # which requires an EXACT time before checking office hours
    # (req_minutes None -> no correction), so a day-only input for a
    # closed weekday saves normally here - the audited defect in the
    # previous version of this test. The deterministic trigger is an
    # exact time unambiguously outside open hours: the weekday is open
    # 09:00-17:00 and "8pm" is outside it, so the existing correction
    # owner executes, stores nothing, and must not advertise the stage.
    client = make_client(
        db,
        calendar_enabled=True,
        office_hours={
            day: {"open": True, "start": "09:00", "end": "17:00"}
            for day in ("mon", "tue", "wed", "thu", "fri")
        },
    )
    _set_calendar_flags(db, client)
    conversation = _pre_time_window_conversation(db, client)

    day_text = _upcoming_weekday_text(db, client)
    resp = send(db, client, conversation, f"{day_text} at 8pm")

    assert resp.reply == (
        "That time is outside normal office hours. "
        "What day/time works better for you?"
    )
    assert resp.meta.get("mode") == "intake_time_window_capture"
    assert "calendar_picker" not in resp.meta
    assert not (conversation.lead_time_window or "").strip()


def test_raw_prompt_literal_absent_from_chat_source(db, fakes):
    # STRUCTURAL single-owner proof: INTAKE_TIME_PREFERENCE_PROMPT (the
    # service-layer constant) is the only wording owner. The raw quoted
    # literal must not exist anywhere in app/routes/chat.py source - in
    # either quote style - while the imported constant name must.
    import inspect
    source = inspect.getsource(chat_module)
    assert '"' + INTAKE_TIME_PREFERENCE_PROMPT + '"' not in source
    assert "'" + INTAKE_TIME_PREFERENCE_PROMPT + "'" not in source
    assert "INTAKE_TIME_PREFERENCE_PROMPT" in source


# ---------------------------------------------------------------------------
# 5. Single-owner unit contract (no route involvement)
# ---------------------------------------------------------------------------

def test_signal_owner_requires_exact_prompt_text(db, fakes):
    client = _gated_client(db)
    assert intake_time_preference_stage_signal(
        client, INTAKE_TIME_PREFERENCE_PROMPT
    ) == TIME_SIGNAL
    # Any other text - including the weekday-rejection re-ask that also
    # mentions morning/afternoon - returns None.
    assert intake_time_preference_stage_signal(
        client,
        "Please choose a weekday (Mon\u2013Fri). Do you prefer morning or afternoon?",
    ) is None
    assert intake_time_preference_stage_signal(client, "") is None
