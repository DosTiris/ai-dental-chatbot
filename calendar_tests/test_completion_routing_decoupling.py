# -*- coding: utf-8 -*-
"""Regression suite for the completion-routing decoupling (Finding #2).

WHAT THIS PROVES
----------------
Before this change, a completed normal intake was routed into the completed-
lead / Calendar-booking handoff ONLY when the CURRENT message parsed as the
New/Returning (patient-type) answer:

    normal_lead_capture_is_complete(conversation)
    AND detect_new_patient_flag(user_text) is not None      # accidental coupling
    AND lead_status != "completed"

That worked only because patient type happened to be the LAST intake field, so
"the message that completed intake" and "the patient-type answer" always
coincided. The coupling is now removed: routing fires on the turn normal intake
TRANSITIONS incomplete -> complete, regardless of which field completed it, and
at most once.

The user-visible intake order is UNCHANGED by this package (patient type is
still asked last). The field-agnostic behaviour is therefore exercised here by
PRESEEDING lead_is_new_patient (and, for one shape, the time window) before the
final field is captured -- a valid state under the existing intake contract and
the exact foundation a later patient-type reorder (Package A) will stand on. No
production reordering is performed or required by these tests.

Regressions 16-20 from the authorisation (stale-slot recovery, closed-day
revalidation, Night Guard, emergency interruption, Start Over) are owned by
their existing dedicated suites, which remain green in the full calendar_tests
run; the emergency-interruption and Start-Over boundaries are additionally
re-asserted here because they sit directly against the completion-routing gate.
"""

import pytest

from calendar_tests.test_universal_appointment_signal import (
    _gated_client,
    _fresh,
    _publish_future_open_slot,
    _date_message,
)
from calendar_tests.test_chat_integration import fakes, send, make_client, OPEN_ALL_WEEK_HOURS
from app.calendar_models import BookingState
import app.routes.chat as chat_module


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _send(db, c, conv, txt):
    r = send(db, c, conv, txt)
    db.refresh(conv)
    return r


def _drive_to_before_final_field(db, c, conv, target, *, preseed_new_patient):
    """Reason -> (patient type) -> name -> phone -> email, leaving exactly the
    DATE as the final missing field. Package A order asks New/Returning first,
    right after the reason. When preseed_new_patient is True, patient type is
    populated up front (no patient-type turn); otherwise it is answered on its
    turn, right after the reason.

    PACKAGE B: for a Calendar tenant a specific day now COMPLETES intake by
    itself (the exact-slot offer replaces the morning/afternoon question), so
    the completing final field these flows exercise is the DATE message —
    still a message that never parses as a patient-type answer, preserving
    exactly the field-agnostic property this suite pins."""
    if preseed_new_patient:
        conv.lead_is_new_patient = True
        db.add(conv)
        db.commit()
        db.refresh(conv)
    _send(db, c, conv, "I need a cleaning")
    if not preseed_new_patient:
        _send(db, c, conv, "new patient")          # Package A: patient type first
    for t in ["Jordan Rivera", "516-555-0100", "skip email"]:
        _send(db, c, conv, t)


# ---------------------------------------------------------------------------
# 1. current order still routes (regressions 1 + 5)
# ---------------------------------------------------------------------------

def test_standard_order_routes_into_calendar(db, fakes, monkeypatch):
    """Standard Package A order (patient type first, right after the reason;
    time window last) routes into Calendar slot selection on completion. Proves
    the reordered intake still advances the Calendar flow normally."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=False)
    r = _send(db, c, conv, _date_message(target))   # PACKAGE B: the date completes intake
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert (r.meta or {}).get("calendar_actions")


# ---------------------------------------------------------------------------
# 2. field-agnostic: time window completes intake (regressions 2, 3a, 4)
# ---------------------------------------------------------------------------

def test_field_agnostic_date_completion_routes(db, fakes, monkeypatch):
    """Patient type already authoritative; the DATE is the final field
    (PACKAGE B). Completing it must route into Calendar even though the
    message (a date) is not a patient-type answer."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=True)
    assert conv.booking_state == BookingState.NONE          # not yet routed
    r = _send(db, c, conv, _date_message(target))           # completes intake
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert (r.meta or {}).get("calendar_actions")


def test_completing_message_need_not_parse_as_patient_type(db, fakes, monkeypatch):
    """Explicit: the message that completes intake ("morning") does NOT satisfy
    detect_new_patient_flag, yet routing still happens."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=True)
    assert chat_module.detect_new_patient_flag(_date_message(target)) is None
    r = _send(db, c, conv, _date_message(target))
    assert (r.meta or {}).get("calendar_actions")


# ---------------------------------------------------------------------------
# 3. second final-field shape: email skip completes intake (regression 3b)
# ---------------------------------------------------------------------------

def test_email_skip_final_field_routes(db, fakes, monkeypatch):
    """A DIFFERENT final-field shape: patient type and time window preseeded,
    so the optional-email step is the last transition. Completing it via
    "skip email" must also route into Calendar."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    conv.lead_is_new_patient = True
    # DATE-ROT FIX: this fixture used to hard-code "Mon 2026-08-10 morning",
    # which fell into the past on 2026-08-11 and rerouted the flow to
    # waiting_for_date. Build the SAME canonical stored shape
    # ("Www YYYY-MM-DD morning") from the future weekday `target` the
    # helper above already published a 10:00 (morning) slot for — so the
    # window is deterministic on any run day and always bookable.
    conv.lead_time_window = "%s %s morning" % (
        ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[target.weekday()],
        target.isoformat(),
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    for t in ["I need a cleaning", "Jordan Rivera", "516-555-0100"]:
        _send(db, c, conv, t)
    assert conv.booking_state == BookingState.NONE
    r = _send(db, c, conv, "skip email")
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert (r.meta or {}).get("calendar_actions")


# ---------------------------------------------------------------------------
# 4. architectural future-coupling proof (authorisation "test the future
#    coupling without implementing Package A")
# ---------------------------------------------------------------------------

def test_preseed_new_patient_before_final_field_enters_calendar(db, fakes, monkeypatch):
    """The property Package A depends on: if lead_is_new_patient is populated
    BEFORE the final intake field is captured, completing that final field
    still enters the Calendar booking path. Proven WITHOUT reordering
    production intake."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    assert conv.lead_is_new_patient is None
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=True)
    assert conv.lead_is_new_patient is True                 # populated up front
    r = _send(db, c, conv, _date_message(target))           # final field (PACKAGE B: the date)
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert (r.meta or {}).get("calendar_actions")


# ---------------------------------------------------------------------------
# 5. exactly-once (regressions 6, 7, 8, 9)
# ---------------------------------------------------------------------------

def test_already_completed_lead_status_blocks_reroute(db, fakes, monkeypatch):
    """A lead already marked completed must not be routed a second time by an
    ordinary later message, even though normal_lead_capture_is_complete stays
    true."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=True)
    _send(db, c, conv, _date_message(target))
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert (conv.lead_status or "").strip().lower() == "completed"
    # An ordinary message after completion must not re-enter completion routing.
    before = conv.booking_state
    _send(db, c, conv, "thanks")
    assert conv.booking_state in (before, BookingState.WAITING_FOR_CONFIRMATION)


def test_affirmative_after_completion_no_second_route(db, fakes, monkeypatch):
    """An affirmative ("yes") after completion must not trigger a second
    completion routing / duplicate booking start."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=False)
    _send(db, c, conv, _date_message(target))   # PACKAGE B: the date completes intake
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    r = _send(db, c, conv, "yes")
    assert conv.booking_state in (BookingState.WAITING_FOR_SLOT_SELECTION,
                                  BookingState.WAITING_FOR_CONFIRMATION)
    assert (r.meta or {}).get("mode") != "lead_complete_after_patient_type"


def test_faq_after_completion_no_reroute(db, fakes, monkeypatch):
    """A FAQ-style question after completion is answered without a duplicate
    completion routing."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=True)
    _send(db, c, conv, _date_message(target))
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    r = _send(db, c, conv, "where are you located?")
    assert (r.meta or {}).get("mode") != "lead_complete_after_patient_type"


# ---------------------------------------------------------------------------
# 6. Calendar-disabled vs Calendar-enabled behaviour (regressions 10, 11, 12,
#    13) and exactly-one office notification (regression 15)
# ---------------------------------------------------------------------------

def test_calendar_disabled_preserves_generic_completion_and_notifies(db, fakes, monkeypatch):
    """With Calendar disabled, completing intake must fall through to the
    existing generic completed-lead behaviour: a completion reply and exactly
    one office notification path -- NOT a Calendar slot offer."""
    c = make_client(db, calendar_enabled=False, office_hours=OPEN_ALL_WEEK_HOURS)
    conv = _fresh(db, c)
    # Package A order: patient type first (right after reason), then demographics;
    # the time window is the last field and completes intake.
    for t in ["I need a cleaning", "new patient", "Jordan Rivera", "516-555-0100",
              "skip email"]:
        _send(db, c, conv, t)
    r = _send(db, c, conv, "Monday morning")
    assert not (r.meta or {}).get("calendar_actions")
    assert conv.booking_state == BookingState.NONE
    assert (conv.lead_status or "").strip().lower() == "completed"
    notifications = len(fakes.lead_sms) + len(fakes.lead_email)
    assert notifications >= 1                                # office was told
    # A follow-up message must NOT produce another office notification.
    _send(db, c, conv, "thanks")
    assert len(fakes.lead_sms) + len(fakes.lead_email) == notifications


def test_calendar_enabled_offers_slots_and_requires_confirmation(db, fakes, monkeypatch):
    """With Calendar enabled, completion offers slots (authoritative) and does
    NOT auto-book: confirmation/selection is still required (state stops at
    slot selection, not BOOKED)."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=False)
    r = _send(db, c, conv, _date_message(target))   # PACKAGE B: the date completes intake
    assert (r.meta or {}).get("calendar_actions")
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert conv.booking_state != BookingState.BOOKED


def test_no_duplicate_booking_notification_on_completion_turn(db, fakes, monkeypatch):
    """Routing into Calendar on the completion turn must not fire a duplicate
    generic office lead notification (native Calendar owns its own path)."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=True)
    _send(db, c, conv, _date_message(target))
    # routine native Calendar sends no generic lead alert on completion
    assert len(fakes.lead_sms) == 0
    assert len(fakes.lead_email) == 0


# ---------------------------------------------------------------------------
# 7. safety / UX boundaries adjacent to the routing gate (regressions 19, 20)
# ---------------------------------------------------------------------------

def test_emergency_interruption_preempts_before_completion(db, fakes, monkeypatch):
    """An emergency mid-intake must preempt: it is not swallowed by the
    completion-routing gate and does not start booking."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    conv.lead_is_new_patient = True
    db.add(conv)
    db.commit()
    db.refresh(conv)
    for t in ["I need a cleaning", "Jordan Rivera", "516-555-0100", "skip email"]:
        _send(db, c, conv, t)
    r = _send(db, c, conv, "I have uncontrolled bleeding from my mouth")
    assert conv.booking_state == BookingState.NONE
    assert not (r.meta or {}).get("calendar_actions")


def test_start_over_exposed_on_routed_completion(db, fakes, monkeypatch):
    """Start Over remains exposed through the response contract on the routed
    completion turn (show_start_over threaded through the routing owner)."""
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    _drive_to_before_final_field(db, c, conv, target, preseed_new_patient=True)
    r = _send(db, c, conv, _date_message(target))
    assert "show_start_over" in (r.meta or {})


# ---------------------------------------------------------------------------
# 8. ROUTE REGISTRATION (v1.1) - prove the FastAPI POST /chat route is owned by
#    chat(), NOT by the internal helper. This catches the exact defect the
#    behavior suite missed: those tests call chat() directly and never exercise
#    router registration, so a decorator accidentally attached to the helper
#    would go undetected. Prefer real router inspection over textual grep.
# ---------------------------------------------------------------------------

def _post_chat_routes():
    from fastapi.routing import APIRoute
    return [r for r in chat_module.router.routes
            if isinstance(r, APIRoute) and r.path == "/chat" and "POST" in r.methods]


def test_exactly_one_post_chat_route_registered():
    """Exactly one POST /chat APIRoute exists in this router."""
    assert len(_post_chat_routes()) == 1


def test_post_chat_endpoint_is_chat_not_helper():
    """The POST /chat route's endpoint callable is the real chat function, and
    NOT the internal completion helper."""
    r = _post_chat_routes()[0]
    assert r.endpoint is chat_module.chat
    assert r.endpoint is not chat_module._complete_and_route_normal_lead
    assert r.endpoint.__name__ == "chat"


def test_helper_is_undecorated_and_never_a_route_endpoint():
    """The completion helper is a plain undecorated function and is not the
    endpoint of ANY registered route."""
    import inspect
    from fastapi.routing import APIRoute
    assert inspect.isfunction(chat_module._complete_and_route_normal_lead)
    for r in chat_module.router.routes:
        if isinstance(r, APIRoute):
            assert r.endpoint is not chat_module._complete_and_route_normal_lead


def test_post_chat_preserves_body_and_response_contract():
    """The POST /chat route still declares response_model=ChatResponse and a
    ChatRequest body parameter."""
    import inspect
    r = _post_chat_routes()[0]
    assert r.response_model is chat_module.ChatResponse
    sig = inspect.signature(chat_module.chat)
    assert sig.parameters["req"].annotation is chat_module.ChatRequest


def test_smoke_post_chat_reaches_real_chat_handler(db, fakes, monkeypatch):
    """Lightweight ASGI smoke test: a real POST /chat over the router reaches the
    chat() contract (returns reply + conversation_id). If the decorator were on
    the helper, this request would not produce a ChatResponse."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    c = make_client(db, calendar_enabled=False)
    app = FastAPI()
    app.include_router(chat_module.router)
    app.dependency_overrides[chat_module.get_db] = lambda: db
    with TestClient(app) as http:
        resp = http.post("/chat", json={"message": "hi", "client_key": c.api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert "reply" in body and "conversation_id" in body
