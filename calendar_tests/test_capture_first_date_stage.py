# calendar_tests/test_capture_first_date_stage.py
#
# C2-A.3 capture-first gap: the ROUTE-level capture-first bypass path
# (mode "bypass") must carry the gated calendar_picker {"stage": "date"}
# signal when - and only when - THIS turn's reply is the standard
# capture-first day/time-window question
#   "Great-thanks [name]. What day/time window works best (e.g., Tue morning)?"
# and all three calendar gates are strict Boolean true.
#
# Production defect reproduced here: after reason + name + phone and the
# optional-email skip, Mia returned that day/time-window question with mode
# "bypass" but NO calendar_picker field, so the signal-gated widget rendered
# nothing. The signal owner is intake_date_stage_signal in the booking service
# (single owner; the route only merges its returned metadata). The S3 prompt
# owner receptionist_bypass_reply() is deliberately NOT refactored (Rule 12);
# the helper recognizes the prompt by its name-independent tail.
#
# Run (PostgreSQL required, as every calendar_tests module):
#   python -m pytest calendar_tests/test_capture_first_date_stage.py -v

import uuid
from datetime import timedelta

import pytest

from app.models import Conversation
import app.routes.chat as chat_module
from app.services.booking_conversation import (
    INTAKE_DATE_WINDOW_PROMPT_TAIL,
    INTAKE_TIME_PREFERENCE_PROMPT,
    PICKER_STAGE_DATE,
    intake_date_stage_signal,
)

# Importing these registers the autouse `fakes` fixture in this module too.
from calendar_tests.test_chat_integration import (  # noqa: F401
    fakes,
    make_client,
    make_conversation,
    send,
)

DATE_SIGNAL = {"stage": "date", "submit": "message"}
TIME_SIGNAL = {"stage": "time_preference"}

# Sentinel meaning "remove this flag entirely" in the gate matrix.
OMIT = object()


def _set_calendar_flags(db, client, *, booking=True, actions=True, picker=True):
    """Author the three C2-A.3 gates on an existing harness client.

    Values pass through UNCHANGED (so the malformed-shape matrix can plant
    "true", 1, None, [], {}), and the OMIT sentinel removes the key entirely
    (the missing-flag matrix). The settings dict is reassigned so SQLAlchemy
    detects the JSON change.
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


def _pre_date_question_conversation(db, client):
    """reason + name + phone captured, email STILL PENDING, time window unset,
    patient type unset - exactly one 'skip email' turn before the capture-first
    day/time-window question. (make_conversation defaults email opt-out True and
    a complete time window; we clear both so the next required field is the
    time window.)"""
    return make_conversation(
        db,
        client,
        lead_time_window=None,
        lead_email_opt_out=False,
        lead_is_new_patient=None,
    )


def _upcoming_weekday_text(db, client):
    """A weekday name 2-6 days ahead in the client's local calendar."""
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

def test_exact_capture_first_sequence_carries_date_signal(db, fakes):
    client = _gated_client(db)

    # reason (fresh conversation; the widget sends no conversation_id)
    r1 = send(db, client, None, "I'd like to book a cleaning")
    conversation = _conversation_row(db, r1)
    assert "name" in r1.reply.lower()
    assert "calendar_picker" not in (r1.meta or {})

    # name -> phone prompt
    r2 = send(db, client, conversation, "Kevin Alvarado")
    assert "phone" in r2.reply.lower()
    assert "calendar_picker" not in (r2.meta or {})

    # phone -> optional-email prompt
    r3 = send(db, client, conversation, "516-555-1234")
    assert "email" in r3.reply.lower()
    assert "calendar_picker" not in (r3.meta or {})

    # skip email -> THE capture-first day/time-window question (mode "bypass").
    r4 = send(db, client, conversation, "skip email")
    assert conversation.lead_email_opt_out is True
    assert r4.reply.endswith(INTAKE_DATE_WINDOW_PROMPT_TAIL)
    assert r4.meta.get("mode") == "bypass"
    assert r4.meta.get("calendar_picker") == DATE_SIGNAL
    # Everything else about the bypass turn is preserved.
    assert r4.meta.get("hours_hint")
    assert "show_start_over" in r4.meta
    # Patient type has NOT been collected at this boundary.
    assert conversation.lead_is_new_patient is None


# ---------------------------------------------------------------------------
# 2. Same boundary from the pre-seeded pre-time-window state
# ---------------------------------------------------------------------------

def test_preseeded_date_question_carries_signal(db, fakes):
    client = _gated_client(db)
    conversation = _pre_date_question_conversation(db, client)

    resp = send(db, client, conversation, "skip email")

    assert resp.reply.endswith(INTAKE_DATE_WINDOW_PROMPT_TAIL)
    assert resp.meta.get("mode") == "bypass"
    assert resp.meta.get("calendar_picker") == DATE_SIGNAL
    assert resp.meta.get("hours_hint")
    assert conversation.lead_is_new_patient is None


# ---------------------------------------------------------------------------
# 3. Gate matrix: false and missing flags suppress the signal with
#    byte-identical pre-patch metadata (reply and mode unchanged)
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
    conversation = _pre_date_question_conversation(db, client)

    resp = send(db, client, conversation, "skip email")

    # The reply itself is unchanged; only the signal is gated off.
    assert resp.reply.endswith(INTAKE_DATE_WINDOW_PROMPT_TAIL)
    assert resp.meta.get("mode") == "bypass"
    assert resp.meta.get("hours_hint")
    assert "calendar_picker" not in resp.meta


# ---------------------------------------------------------------------------
# 4. Malformed-shape matrix for ALL THREE strict gates: "true", 1, None,
#    [], {} each suppress (only strict Boolean True passes)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["booking", "actions", "picker"])
@pytest.mark.parametrize("malformed", ["true", 1, None, [], {}],
                         ids=["string-true", "int-1", "null", "array", "object"])
def test_malformed_flag_suppresses_signal(db, fakes, flag, malformed):
    client = _gated_client(db, **{flag: malformed})
    conversation = _pre_date_question_conversation(db, client)

    resp = send(db, client, conversation, "skip email")

    assert resp.reply.endswith(INTAKE_DATE_WINDOW_PROMPT_TAIL)
    assert "calendar_picker" not in resp.meta


# ---------------------------------------------------------------------------
# 5. No OTHER bypass stage advertises the date signal
# ---------------------------------------------------------------------------

def test_other_bypass_stages_do_not_carry_date_signal(db, fakes):
    client = _gated_client(db)

    # reason -> name prompt
    r1 = send(db, client, None, "I'd like to book a cleaning")
    conversation = _conversation_row(db, r1)
    assert not r1.reply.endswith(INTAKE_DATE_WINDOW_PROMPT_TAIL)
    assert "calendar_picker" not in (r1.meta or {})

    # name -> phone prompt
    r2 = send(db, client, conversation, "Kevin Alvarado")
    assert "calendar_picker" not in (r2.meta or {})

    # phone -> email prompt
    r3 = send(db, client, conversation, "516-555-1234")
    assert "calendar_picker" not in (r3.meta or {})


# ---------------------------------------------------------------------------
# 6. After a manually entered day, the EXISTING time_preference signal still
#    appears exactly as before (unchanged path, mode intake_time_window_capture)
# ---------------------------------------------------------------------------

def test_typed_day_after_date_question_still_carries_time_preference(db, fakes):
    client = _gated_client(db)
    conversation = _pre_date_question_conversation(db, client)

    r_date = send(db, client, conversation, "skip email")
    assert r_date.meta.get("calendar_picker") == DATE_SIGNAL

    # Typing a day advances the existing flow unchanged: the morning/afternoon
    # question carries the time_preference signal via its own owner.
    day_text = _upcoming_weekday_text(db, client)
    r_pref = send(db, client, conversation, day_text)
    assert r_pref.reply == INTAKE_TIME_PREFERENCE_PROMPT
    assert r_pref.meta.get("mode") == "intake_time_window_capture"
    assert r_pref.meta.get("calendar_picker") == TIME_SIGNAL


# ---------------------------------------------------------------------------
# 7. Emergency flow emits no date signal (its reply is never the date prompt)
# ---------------------------------------------------------------------------

def test_emergency_reply_carries_no_date_signal(db, fakes):
    client = _gated_client(db)

    resp = send(
        db, client, None,
        "I have severe facial swelling and trouble breathing",
    )

    assert not resp.reply.endswith(INTAKE_DATE_WINDOW_PROMPT_TAIL)
    assert "calendar_picker" not in (resp.meta or {})


# ---------------------------------------------------------------------------
# 8. Drift guard: the route's capture-first date prompt ends with the tail the
#    service owns (the two must never diverge)
# ---------------------------------------------------------------------------

def test_route_date_prompt_matches_owned_tail(db, fakes):
    client = _gated_client(db)
    conversation = _pre_date_question_conversation(db, client)

    resp = send(db, client, conversation, "skip email")

    assert resp.reply.endswith(INTAKE_DATE_WINDOW_PROMPT_TAIL)
    assert resp.reply.startswith("Great")
    assert conversation.lead_name in resp.reply


# ---------------------------------------------------------------------------
# 9. Direct unit tests of the single-owner helper
# ---------------------------------------------------------------------------

def test_helper_emits_only_for_date_prompt_when_gated(db, fakes):
    client = _gated_client(db)
    date_prompt = "Great-thanks Kevin. " + INTAKE_DATE_WINDOW_PROMPT_TAIL

    # entered_time_window_stage=True is the genuine-transition fact from the route.
    assert intake_date_stage_signal(client, date_prompt, True) == DATE_SIGNAL
    # Every non-date reply -> None, even fully gated and entered.
    assert intake_date_stage_signal(client, INTAKE_TIME_PREFERENCE_PROMPT, True) is None
    assert intake_date_stage_signal(
        client, "Thanks Kevin. What day/time window works best?", True) is None
    assert intake_date_stage_signal(client, "", True) is None
    assert intake_date_stage_signal(client, None, True) is None
    # Transition gate: the exact date prompt, fully gated, but NOT a genuine
    # entry into time_window (invalid date re-asks the same prompt) -> None.
    assert intake_date_stage_signal(client, date_prompt, False) is None
    for not_true in (None, 1, "true", [], {}):
        assert intake_date_stage_signal(client, date_prompt, not_true) is None


@pytest.mark.parametrize("flags", [
    {"booking": False}, {"actions": False}, {"picker": False},
    {"booking": OMIT}, {"actions": OMIT}, {"picker": OMIT},
    {"booking": "true"}, {"actions": 1}, {"picker": None},
    {"booking": []}, {"actions": {}},
])
def test_helper_suppresses_when_any_gate_not_strict_true(db, fakes, flags):
    client = _gated_client(db, **flags)
    date_prompt = "Great-thanks Kevin. " + INTAKE_DATE_WINDOW_PROMPT_TAIL
    # entered=True isolates the FEATURE-GATE suppression under test.
    assert intake_date_stage_signal(client, date_prompt, True) is None


def test_date_stage_vocabulary_reused(db, fakes):
    assert PICKER_STAGE_DATE == "date"
