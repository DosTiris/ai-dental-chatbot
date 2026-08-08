# calendar_tests/test_package_a_intake_order.py
#
# Package A - New/Returning-first intake reorder + symptom-safety preservation.
#
# Dedicated behavioral coverage for the approved Package A requirements. Every
# test drives the REAL chat() route (PostgreSQL, no network) via the shared
# integration harness. These are proof tests for the reorder invariants and the
# preserved safety/handoff owners; they assert externally observable behavior.

import pytest

from app.calendar_models import BookingState
from calendar_tests.test_chat_integration import fakes, make_client, send  # noqa: F401
from calendar_tests.test_hybrid_capture import make_client as make_hybrid_client  # noqa: F401
from calendar_tests.test_universal_appointment_signal import (
    _gated_client,
    _fresh,
    _publish_future_open_slot,
    _date_message,
)

PT_Q = "new or returning patient"
NAME_Q = "first name"
SAFETY = "seek urgent care right away"


def _last(resp):
    return (resp.reply or "").lower()


# ---------------------------------------------------------------------------
# 1. Standard order: reason -> New/Returning FIRST (before name).
# ---------------------------------------------------------------------------
def test_standard_reason_asks_patient_type_first(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    r = send(db, c, conv, "I need a cleaning")
    assert PT_Q in _last(r)
    assert NAME_Q not in _last(r)


# 2 + 18. Severe symptom opener: FIRST turn carries urgent-care safety AND asks
#         New/Returning (safety is not delayed to a later turn).
def test_severe_symptom_first_turn_is_safety_plus_patient_type(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    r = send(db, c, conv, "I have severe tooth pain and swelling")
    assert SAFETY in (r.reply or "")
    assert PT_Q in _last(r)


# 2 (supporting). The symptom safety introduction appears exactly once.
def test_symptom_safety_appears_exactly_once(db, fakes):
    from app.models import Message
    c = _gated_client(db); conv = _fresh(db, c)
    send(db, c, conv, "I have severe tooth pain and swelling")
    send(db, c, conv, "new")
    send(db, c, conv, "Kyle")
    send(db, c, conv, "516-555-0100")
    msgs = (db.query(Message)
              .filter(Message.conversation_id == conv.id, Message.role == "assistant")
              .all())
    assert sum(1 for m in msgs if SAFETY in (m.content or "")) == 1


# 6. "New" advances to the name question.
def test_new_answer_advances_to_name(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    send(db, c, conv, "I need a cleaning")
    r = send(db, c, conv, "new patient"); db.refresh(conv)
    assert conv.lead_is_new_patient is True
    assert NAME_Q in _last(r)


# 7. "Returning" advances to the name question.
def test_returning_answer_advances_to_name(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    send(db, c, conv, "I need a cleaning")
    r = send(db, c, conv, "returning patient"); db.refresh(conv)
    assert conv.lead_is_new_patient is False
    assert NAME_Q in _last(r)


# 8. Preseeded NEW patient type is not re-asked (goes straight to name).
def test_preseeded_new_not_reasked(db, fakes):
    c = _gated_client(db)
    conv = _fresh(db, c); conv.lead_is_new_patient = True; db.add(conv); db.commit()
    r = send(db, c, conv, "I need a cleaning")
    assert PT_Q not in _last(r)
    assert NAME_Q in _last(r)


# 9. Preseeded RETURNING patient type is not re-asked.
def test_preseeded_returning_not_reasked(db, fakes):
    c = _gated_client(db)
    conv = _fresh(db, c); conv.lead_is_new_patient = False; db.add(conv); db.commit()
    r = send(db, c, conv, "I need a cleaning")
    assert PT_Q not in _last(r)
    assert NAME_Q in _last(r)


# 3. Validated Other detail -> New/Returning (before name).
def test_validated_other_asks_patient_type_before_name(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    send(db, c, conv, "Other")
    r = send(db, c, conv, "sore spot since my last visit")
    assert PT_Q in _last(r)
    assert NAME_Q not in _last(r)


# 14. Unvalidated/bare Other must request the actual dental reason BEFORE any
#     patient-type question.
def test_unvalidated_other_does_not_ask_patient_type(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    r = send(db, c, conv, "Other")
    assert PT_Q not in _last(r)


# 16. Dangerous self-treatment guidance wins over the reorder (no patient-type
#     question fronted onto the safety message).
def test_self_treatment_guard_wins(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    r = send(db, c, conv, "pull a tooth")
    assert ("home-care instructions" in (r.reply or "").lower()
            or "dental professional" in (r.reply or "").lower())
    assert PT_Q not in _last(r)


# 17. Life-threatening emergency wins over the reorder.
def test_life_threatening_emergency_wins(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    r = send(db, c, conv, "I have severe facial swelling and trouble breathing")
    assert PT_Q not in _last(r)
    assert (conv.booking_state or "none") == BookingState.NONE


# 19. An emergency interrupts WHILE at the patient-type turn.
def test_emergency_interrupts_at_patient_type(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    r0 = send(db, c, conv, "I need a cleaning")
    assert PT_Q in _last(r0)
    r = send(db, c, conv, "actually I have trouble breathing and facial swelling now")
    assert PT_Q not in _last(r)


# 20. An emergency interrupts AFTER patient type was answered.
def test_emergency_interrupts_after_patient_type(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    send(db, c, conv, "I need a cleaning")
    send(db, c, conv, "new patient"); db.refresh(conv)
    assert conv.lead_is_new_patient is True
    r = send(db, c, conv, "I now have trouble breathing and severe facial swelling")
    assert NAME_Q not in _last(r)


# 23. Ordinary hybrid external handoff is UNCHANGED: name + phone only, no
#     New/Returning question inserted before the booking link.
def test_ordinary_hybrid_external_handoff_unchanged(db, fakes):
    c = make_hybrid_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conv = _fresh(db, c)
    r = send(db, c, conv, "I need a cleaning")
    # Ordinary hybrid asks the name first (its own capture, en route to the
    # external booking link), NOT New/Returning - the handoff is unchanged.
    assert PT_Q not in _last(r)
    assert "online booking" in _last(r) and NAME_Q in _last(r)


# 24. A Mia-owned (priority) hybrid uses the FULL intake with the new order.
def test_priority_hybrid_uses_new_order(db, fakes):
    c = make_hybrid_client(db, booking_mode="hybrid", booking_url="https://book.example.com")
    conv = _fresh(db, c); conv.lead_is_priority = True; db.add(conv); db.commit()
    r = send(db, c, conv, "I need a cleaning")
    # Priority hybrid uses Mia-owned FULL intake (not the ordinary-hybrid
    # shortcut), so New/Returning is asked first.
    assert PT_Q in _last(r)


# 30-32. Full standard flow completes and routes into the Calendar slot offering
#        (patient type first, time window last).
def test_full_flow_completes_into_calendar_slots(db, fakes, monkeypatch):
    c = _gated_client(db)
    _, target = _publish_future_open_slot(db, c, monkeypatch)
    conv = _fresh(db, c)
    send(db, c, conv, "I need a cleaning")
    send(db, c, conv, "new patient")
    send(db, c, conv, "Jordan Rivera")
    send(db, c, conv, "516-555-0100")
    send(db, c, conv, "skip email")
    # PACKAGE B: the date turn completes intake and offers slots directly —
    # the morning/afternoon turn is removed from the Calendar flow.
    r = send(db, c, conv, _date_message(target)); db.refresh(conv)
    assert conv.booking_state == BookingState.WAITING_FOR_SLOT_SELECTION
    assert (r.meta or {}).get("calendar_actions")
    assert "our team will reach out" not in _last(r)


# ---------------------------------------------------------------------------
# ChatGPT audit finding #1 regressions (Package A v1.1).
#
# The rejected v1 gated the name-branch safety emission on np_known_at_entry,
# a same-scope copy of np_known that is always True once the name branch is
# reachable, so a severe-symptom entry with an ALREADY-authoritative patient
# type received a plain name question with the urgent-care guidance omitted.
# Cases B / C / B2 below therefore FAIL against the v1 chat.py and pass only
# with the v1.1 delivered-once guard (assistant-message sentinel scan).
# ---------------------------------------------------------------------------

SEVERE_OPENER = "I have severe tooth pain and swelling"


# A. Patient type unknown + severe symptom: the FIRST reply carries the
#    existing urgent-care guidance AND asks New/Returning (never the name).
def test_audit1_A_unknown_patient_type_first_reply_safety_plus_patient_type(db, fakes):
    c = _gated_client(db); conv = _fresh(db, c)
    r = send(db, c, conv, SEVERE_OPENER)
    assert SAFETY in (r.reply or "")
    assert PT_Q in _last(r)
    assert NAME_Q not in _last(r)


# B. lead_is_new_patient=True PRESEEDED + severe symptom + name missing:
#    the FIRST severe-symptom reply carries the urgent-care guidance, does
#    NOT re-ask New/Returning, and asks the name.
def test_audit1_B_preseeded_new_severe_symptom_first_reply_safety_then_name(db, fakes):
    c = _gated_client(db)
    conv = _fresh(db, c); conv.lead_is_new_patient = True; db.add(conv); db.commit()
    r = send(db, c, conv, SEVERE_OPENER)
    assert SAFETY in (r.reply or "")
    assert PT_Q not in _last(r)
    assert NAME_Q in _last(r)


# C. lead_is_new_patient=False PRESEEDED: same first-contact guarantee, no
#    duplicate patient-type question, name asked next.
def test_audit1_C_preseeded_returning_severe_symptom_first_reply_safety_then_name(db, fakes):
    c = _gated_client(db)
    conv = _fresh(db, c); conv.lead_is_new_patient = False; db.add(conv); db.commit()
    r = send(db, c, conv, SEVERE_OPENER)
    assert SAFETY in (r.reply or "")
    assert PT_Q not in _last(r)
    assert NAME_Q in _last(r)


# B2 (required contract case 4). Resumed / partially preseeded intake with the
#    name still missing: the guidance was delivered on the FIRST severe-symptom
#    turn, an unrelated interruption follows, and the resumed intake turn asks
#    the name WITHOUT repeating the guidance - the delivered-once scan reads
#    the whole assistant history, not merely the immediately preceding turn.
def test_audit1_B2_resumed_preseeded_intake_safety_exactly_once(db, fakes):
    from app.models import Message
    c = _gated_client(db)
    conv = _fresh(db, c); conv.lead_is_new_patient = True; db.add(conv); db.commit()
    r1 = send(db, c, conv, SEVERE_OPENER)
    assert SAFETY in (r1.reply or "")
    send(db, c, conv, "what are your office hours?")
    r3 = send(db, c, conv, "ok - I still need that appointment")
    assert NAME_Q in _last(r3)
    assert PT_Q not in _last(r3)
    msgs = (db.query(Message)
              .filter(Message.conversation_id == conv.id, Message.role == "assistant")
              .all())
    assert sum(1 for m in msgs if SAFETY in (m.content or "")) == 1


# D. Normal severe-symptom flow: safety + New/Returning, answer, name - and
#    the safety wording appears EXACTLY ONCE across the whole conversation,
#    with the patient-type question asked exactly once as well.
def test_audit1_D_normal_flow_safety_and_patient_type_exactly_once(db, fakes):
    from app.models import Message
    c = _gated_client(db); conv = _fresh(db, c)
    r1 = send(db, c, conv, SEVERE_OPENER)
    assert SAFETY in (r1.reply or "")
    assert PT_Q in _last(r1)
    r2 = send(db, c, conv, "new"); db.refresh(conv)
    assert conv.lead_is_new_patient is True
    assert NAME_Q in _last(r2)
    assert SAFETY not in (r2.reply or "")
    send(db, c, conv, "Jordan Rivera")
    msgs = (db.query(Message)
              .filter(Message.conversation_id == conv.id, Message.role == "assistant")
              .all())
    assert sum(1 for m in msgs if SAFETY in (m.content or "")) == 1
    assert sum(1 for m in msgs if PT_Q in (m.content or "").lower()) == 1



# ---------------------------------------------------------------------------
# ChatGPT audit ROUND 2 regression (Package A v1.1.2).
#
# v1.1/v1.1.1 keyed the delivered-marker on SYMPTOM_TEAM_ACK, which BOTH
# builder branches emit, so a prior MILD-symptom acknowledgement (which
# contains no urgent-care wording) wrongly read as "safety delivered" and
# suppressed the urgent-care guidance when the authoritative symptom later
# became severe. This test FAILS against v1.1.1 and passes only with the
# v1.1.2 rule: urgent-care wording previously emitted => safety delivered;
# a generic acknowledgement alone never marks it.
# ---------------------------------------------------------------------------

MILD_OPENER = "I have a bad toothache"
ACK = "help send this to the team"


def test_audit2_E_generic_ack_never_suppresses_urgent_on_escalation(db, fakes):
    from app.models import Message
    c = _gated_client(db)
    conv = _fresh(db, c); conv.lead_is_new_patient = True; db.add(conv); db.commit()
    # Turn 1: MILD symptom -> generic acknowledgement only (no urgent-care
    # wording), then the name question (patient type preseeded, name branch).
    r1 = send(db, c, conv, MILD_OPENER)
    assert ACK in _last(r1)
    assert SAFETY not in (r1.reply or "")
    assert NAME_Q in _last(r1)
    # The authoritative symptom source escalates to SEVERE (resumed state:
    # the stored reason text now names severe swelling/bleeding).
    db.refresh(conv)
    conv.lead_reason_source_text = "toothache and now my face is swollen and bleeding"
    db.add(conv); db.commit()
    # Turn 2: FIRST CONTACT WITH THE SEVERE SYMPTOM must carry the urgent-care
    # guidance; the stored generic acknowledgement must not suppress it.
    r2 = send(db, c, conv, "ok - I still need that appointment")
    assert SAFETY in (r2.reply or "")
    assert NAME_Q in _last(r2)
    assert PT_Q not in _last(r2)
    # Turn 3: once actually delivered, the guidance appears exactly once.
    r3 = send(db, c, conv, "sorry - I still need the appointment")
    assert SAFETY not in (r3.reply or "")
    msgs = (db.query(Message)
              .filter(Message.conversation_id == conv.id, Message.role == "assistant")
              .all())
    assert sum(1 for m in msgs if SAFETY in (m.content or "")) == 1

