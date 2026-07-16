# calendar_tests/test_notification_policy.py
#
# PATCH 2D (Senior Audit Critical #3): patient SMS is DISABLED by current
# product policy. This file is the single owner of the notification-policy
# regression matrix (Rule 6): it proves, deterministically and without any
# external provider, that send_booking_notifications
#
#   - never sends (or attempts) a patient SMS, in any scenario,
#   - preserves the existing office SMS + office email behavior exactly,
#   - persists honest per-channel outcomes (patient_sms_sent stays False and
#     the intentional disablement never appears as a failure in notify_error),
#   - keeps the office channels' failures independent of each other.
#
# TEST-SIDE FAKES ONLY: _send_sms/_send_email are replaced with recording
# fakes BELOW the office-channel logic, so the real channel code executes but
# no Twilio/Telnyx/Resend/real SMS/email provider can possibly run. In the
# formatter-unreachability test, build_patient_sms is additionally replaced
# with a trap that raises if it is ever invoked.
#
# Deliberately OUT OF SCOPE here (separate Recommended finding — notification
# idempotency/outbox): whether repeated send invocations re-send OFFICE
# messages. The repeat test below asserts only that the patient channel stays
# disabled on every invocation.

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from calendar_tests.conftest import requires_db

UTC = ZoneInfo("UTC")
pytestmark = requires_db

OFFICE_PHONE = "516-555-0100"
OFFICE_EMAIL = "frontdesk@testdental.example"
PATIENT_PHONE = "516-555-1234"          # matches the conftest lead phone
PATIENT_EMAIL = "kevin@example.com"


def _now():
    return datetime.now(UTC)


def _settings(client):
    from app.services.calendar_settings_service import load_calendar_settings
    return load_calendar_settings(client)


def _set_office_contacts(db, client, phone, email):
    """Configure the office notification contacts for one scenario. The
    conftest client starts with BOTH unset (None), so each test states its
    contact configuration explicitly — no hidden fixture behavior (Rule 4)."""
    client.notification_phone = phone
    client.notification_email = email
    db.add(client)
    db.commit()


def _make_appointment(db, client, status, patient_email=None):
    """One committed appointment on a fresh staff-published slot.
    conversation_id=None keeps these rows exempt from the Patch 1
    per-conversation unique index (staff-style row, same technique as the
    Patch 2B admin-route test) — this file tests notifications, not booking."""
    from app.repositories.appointment_repository import (
        create_appointment_from_slot,
        create_slot,
    )
    start = _now() + timedelta(hours=48)
    slot = create_slot(db, client.id, start, start + timedelta(minutes=45))
    db.commit()
    appointment = create_appointment_from_slot(
        db, slot=slot, conversation_id=None, status=status,
        patient_name="Kevin Alvarado", patient_phone=PATIENT_PHONE,
        patient_email=patient_email, new_or_returning="new",
        reason="cleaning/checkup", urgency="routine",
    )
    db.commit()
    return appointment


def _recording_sms(monkeypatch, fail=False):
    """Replace _send_sms with a fake that RECORDS every (destination, body)
    and optionally raises — installed below the office logic so the real
    channel code runs. Returns the recording list."""
    from app.services import notification_service
    sent = []

    def fake_send_sms(to_phone, body):
        sent.append((to_phone, body))
        if fail:
            raise RuntimeError("sms provider down (test fake)")

    monkeypatch.setattr(notification_service, "_send_sms", fake_send_sms)
    return sent


def _recording_email(monkeypatch, fail=False):
    """Replace _send_email with a fake that RECORDS every (destination,
    subject) and optionally raises. Returns the recording list."""
    from app.services import notification_service
    sent = []

    def fake_send_email(to_email, subject, body_text):
        sent.append((to_email, subject))
        if fail:
            raise RuntimeError("email provider down (test fake)")

    monkeypatch.setattr(notification_service, "_send_email", fake_send_email)
    return sent


def _send(db, client, appointment):
    from app.services import notification_service
    settings = _settings(client)
    db.rollback()  # End test-owned settings-read transaction.
    return notification_service.send_booking_notifications(
        db, client, appointment, settings
    )


# ---------------------------------------------------------------------------
# Required scenario 1 + the approved formatter-unreachability condition
# ---------------------------------------------------------------------------

def test_only_office_sms_attempted_when_both_phones_exist(db, client_row, monkeypatch):
    """Office phone AND patient phone both exist: exactly ONE SMS attempt
    occurs, its destination is the OFFICE phone, and the patient phone is
    never used. Additionally (approved condition): build_patient_sms is
    replaced with a trap that raises AssertionError if invoked — the flow
    must complete without ever calling it, proving the formatter is
    unreachable from the production notification path, not merely unused."""
    from app.calendar_models import AppointmentStatus
    from app.services import notification_service

    trap_calls = {"n": 0}

    def trapped_build_patient_sms(*args, **kwargs):
        trap_calls["n"] += 1
        raise AssertionError(
            "build_patient_sms must be unreachable from the production "
            "booking-notification flow (Patch 2D)"
        )

    monkeypatch.setattr(notification_service, "build_patient_sms",
                        trapped_build_patient_sms)
    sms_sent = _recording_sms(monkeypatch)
    _set_office_contacts(db, client_row, phone=OFFICE_PHONE, email=None)
    appointment = _make_appointment(db, client_row, AppointmentStatus.PENDING)

    outcome = _send(db, client_row, appointment)

    # The trap was never invoked — not even via a swallowed exception:
    assert trap_calls["n"] == 0
    assert not any("AssertionError" in e for e in outcome.errors)
    # Exactly one SMS attempt; its destination is the office phone:
    assert len(sms_sent) == 1
    assert sms_sent[0][0] == OFFICE_PHONE
    assert all(dest != PATIENT_PHONE for dest, _ in sms_sent)
    # Honest outcomes:
    assert outcome.office_sms_sent is True
    assert outcome.patient_sms_sent is False
    db.refresh(appointment)
    assert appointment.office_sms_sent is True
    assert appointment.patient_sms_sent is False


# ---------------------------------------------------------------------------
# Required scenario 2
# ---------------------------------------------------------------------------

def test_office_email_attempted_and_recorded(db, client_row, monkeypatch):
    """Office email exists: it is attempted, office_email_sent reflects the
    fake's success, and no patient-email behavior is introduced (the
    appointment HAS a patient email that must never become a destination)."""
    from app.calendar_models import AppointmentStatus

    email_sent = _recording_email(monkeypatch)
    sms_sent = _recording_sms(monkeypatch)
    _set_office_contacts(db, client_row, phone=None, email=OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row, AppointmentStatus.PENDING,
                                    patient_email=PATIENT_EMAIL)

    outcome = _send(db, client_row, appointment)

    assert [dest for dest, _ in email_sent] == [OFFICE_EMAIL]
    assert all(dest != PATIENT_EMAIL for dest, _ in email_sent)
    assert outcome.office_email_sent is True
    assert outcome.patient_sms_sent is False
    assert len(sms_sent) == 0  # no office phone configured, no patient SMS
    db.refresh(appointment)
    assert appointment.office_email_sent is True
    assert appointment.patient_sms_sent is False


# ---------------------------------------------------------------------------
# Required scenario 3
# ---------------------------------------------------------------------------

def test_missing_office_phone_zero_sms_attempts(db, client_row, monkeypatch):
    """Office phone missing but patient phone exists: ZERO SMS attempts
    occur — the patient phone is never used as a fallback office destination.
    The missing office phone is recorded as the existing 'skipped' entry."""
    from app.calendar_models import AppointmentStatus

    sms_sent = _recording_sms(monkeypatch)
    _set_office_contacts(db, client_row, phone=None, email=None)
    appointment = _make_appointment(db, client_row, AppointmentStatus.PENDING)

    outcome = _send(db, client_row, appointment)

    assert len(sms_sent) == 0
    assert outcome.office_sms_sent is False
    assert outcome.patient_sms_sent is False
    assert any(e.startswith("office_sms: skipped") for e in outcome.errors)
    db.refresh(appointment)
    assert appointment.patient_sms_sent is False
    assert "patient_sms" not in (appointment.notify_error or "")


# ---------------------------------------------------------------------------
# Required scenario 4
# ---------------------------------------------------------------------------

def test_office_sms_failure_still_no_patient_sms(db, client_row, monkeypatch):
    """Office SMS fails: the failure is recorded honestly, patient SMS is
    STILL not attempted (no retry-to-patient, no fallback), and office email
    proceeds independently."""
    from app.calendar_models import AppointmentStatus

    sms_sent = _recording_sms(monkeypatch, fail=True)
    email_sent = _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, phone=OFFICE_PHONE, email=OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row, AppointmentStatus.PENDING)

    outcome = _send(db, client_row, appointment)

    assert len(sms_sent) == 1                      # the office attempt only
    assert sms_sent[0][0] == OFFICE_PHONE
    assert outcome.office_sms_sent is False        # honest failure
    assert any(e.startswith("office_sms:") for e in outcome.errors)
    assert outcome.office_email_sent is True       # independent channel
    assert [dest for dest, _ in email_sent] == [OFFICE_EMAIL]
    assert outcome.patient_sms_sent is False
    db.refresh(appointment)
    assert appointment.office_sms_sent is False
    assert appointment.office_email_sent is True
    assert appointment.patient_sms_sent is False
    assert "office_sms" in appointment.notify_error
    assert "patient_sms" not in appointment.notify_error


# ---------------------------------------------------------------------------
# Required scenario 5
# ---------------------------------------------------------------------------

def test_office_email_failure_sms_independent(db, client_row, monkeypatch):
    """Office email fails: the failure is recorded honestly, office SMS still
    succeeds independently, and patient SMS is not attempted."""
    from app.calendar_models import AppointmentStatus

    sms_sent = _recording_sms(monkeypatch)
    email_sent = _recording_email(monkeypatch, fail=True)
    _set_office_contacts(db, client_row, phone=OFFICE_PHONE, email=OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row, AppointmentStatus.PENDING)

    outcome = _send(db, client_row, appointment)

    assert outcome.office_email_sent is False      # honest failure
    assert any(e.startswith("office_email:") for e in outcome.errors)
    assert [dest for dest, _ in email_sent] == [OFFICE_EMAIL]
    assert outcome.office_sms_sent is True         # independent channel
    assert [dest for dest, _ in sms_sent] == [OFFICE_PHONE]
    assert all(dest != PATIENT_PHONE for dest, _ in sms_sent)
    assert outcome.patient_sms_sent is False
    db.refresh(appointment)
    assert appointment.office_sms_sent is True
    assert appointment.office_email_sent is False
    assert appointment.patient_sms_sent is False
    assert "office_email" in appointment.notify_error
    assert "patient_sms" not in appointment.notify_error


# ---------------------------------------------------------------------------
# Required scenarios 6 and 7
# ---------------------------------------------------------------------------

def test_pending_appointment_no_patient_sms(db, client_row, monkeypatch):
    """PENDING appointment (staff confirmation required): no patient SMS —
    the old code texted 'request received' here; nothing may be sent now."""
    from app.calendar_models import AppointmentStatus

    sms_sent = _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, phone=OFFICE_PHONE, email=OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row, AppointmentStatus.PENDING)

    outcome = _send(db, client_row, appointment)

    assert [dest for dest, _ in sms_sent] == [OFFICE_PHONE]
    assert all(dest != PATIENT_PHONE for dest, _ in sms_sent)
    assert outcome.patient_sms_sent is False
    db.refresh(appointment)
    assert appointment.patient_sms_sent is False


def test_confirmed_appointment_no_patient_sms(db, client_row, monkeypatch):
    """CONFIRMED appointment: no patient SMS — the old code texted 'your
    appointment is confirmed' here; nothing may be sent now either."""
    from app.calendar_models import AppointmentStatus

    sms_sent = _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, phone=OFFICE_PHONE, email=OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row, AppointmentStatus.CONFIRMED)

    outcome = _send(db, client_row, appointment)

    assert [dest for dest, _ in sms_sent] == [OFFICE_PHONE]
    assert all(dest != PATIENT_PHONE for dest, _ in sms_sent)
    assert outcome.patient_sms_sent is False
    db.refresh(appointment)
    assert appointment.patient_sms_sent is False


# ---------------------------------------------------------------------------
# Required scenario 8
# ---------------------------------------------------------------------------

def test_persisted_flags_honest_no_fake_patient_error(db, client_row, monkeypatch):
    """Persisted appointment fields: patient_sms_sent remains False, the
    office flags keep their accurate values, and notify_error does NOT
    contain a fake patient-SMS failure caused solely by the intentional
    policy disablement — with both office channels succeeding, notify_error
    is None (disabled is not an error)."""
    from app.calendar_models import AppointmentStatus

    _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, phone=OFFICE_PHONE, email=OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row, AppointmentStatus.PENDING)

    outcome = _send(db, client_row, appointment)

    assert outcome.errors == []                # nothing failed, nothing faked
    db.refresh(appointment)
    assert appointment.office_sms_sent is True
    assert appointment.office_email_sent is True
    assert appointment.patient_sms_sent is False
    assert appointment.notify_error is None    # no fake patient_sms failure


# ---------------------------------------------------------------------------
# Required scenario 9
# ---------------------------------------------------------------------------

def test_repeated_invocation_patient_sms_stays_disabled(db, client_row, monkeypatch):
    """send_booking_notifications invoked repeatedly: patient SMS remains
    disabled on EVERY invocation — the patient phone never appears among the
    recorded SMS destinations, and patient_sms_sent stays False each time.
    Deliberately NOT asserted: office-channel idempotency across repeats —
    notification idempotency/outbox is a separate Recommended finding and
    remains out of Patch 2D's scope."""
    from app.calendar_models import AppointmentStatus

    sms_sent = _recording_sms(monkeypatch)
    _recording_email(monkeypatch)
    _set_office_contacts(db, client_row, phone=OFFICE_PHONE, email=OFFICE_EMAIL)
    appointment = _make_appointment(db, client_row, AppointmentStatus.PENDING)

    first = _send(db, client_row, appointment)
    second = _send(db, client_row, appointment)

    assert first.patient_sms_sent is False
    assert second.patient_sms_sent is False
    assert all(dest == OFFICE_PHONE for dest, _ in sms_sent)
    assert all(dest != PATIENT_PHONE for dest, _ in sms_sent)
    db.refresh(appointment)
    assert appointment.patient_sms_sent is False
    assert "patient_sms" not in (appointment.notify_error or "")


# ===========================================================================
# PATCH 6 (Senior Audit Recommended #7) — HTML escaping, stored-error
# vocabulary, plain-text normalization, and safe logging.
#
# The fixed <pre> wrapper literal below is asserted BYTE-FOR-BYTE on purpose:
# it must stay identical to the pre-Patch-6 email markup.
# ===========================================================================

EMAIL_PRE_OPEN = "<pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>"


def _make_appointment_custom(db, client, **overrides):
    """PATCH 6: like _make_appointment, but with hostile/edge field values.
    Stored values are written AS GIVEN (storage is never escaped or
    normalized — the output boundary is where hardening happens)."""
    from app.calendar_models import AppointmentStatus
    from app.repositories.appointment_repository import (
        create_appointment_from_slot,
        create_slot,
    )
    start = _now() + timedelta(hours=48)
    slot = create_slot(db, client.id, start, start + timedelta(minutes=45))
    db.commit()
    fields = dict(
        conversation_id=None, status=AppointmentStatus.PENDING,
        patient_name="Kevin Alvarado", patient_phone=PATIENT_PHONE,
        patient_email=None, new_or_returning="new",
        reason="cleaning/checkup", urgency="routine",
    )
    fields.update(overrides)
    appointment = create_appointment_from_slot(db, slot=slot, **fields)
    db.commit()
    return appointment


def test_email_html_escapes_patient_name_markup(db, client_row):
    """A patient name containing HTML tags/attributes cannot create markup in
    the staff email; the stored value stays exactly as typed."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.services.notification_service import (
        build_office_email_body, render_email_html,
    )
    hostile = '<b onclick="steal()">Evil</b> Alvarado'
    appointment = _make_appointment_custom(db, client, patient_name=hostile)
    doc = render_email_html(
        build_office_email_body(appointment, "Test Dental", _settings(client))
    )
    assert "&lt;b onclick=&quot;steal()&quot;&gt;Evil&lt;/b&gt; Alvarado" in doc
    assert "<b" not in doc.replace(EMAIL_PRE_OPEN, "")
    assert 'onclick="' not in doc
    db.refresh(appointment)
    assert appointment.patient_name == hostile  # storage never escaped


def test_email_html_escapes_reason_script_and_event_handler_text(db, client_row):
    """Script tags and event-handler text in the free-typed reason are inert."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.services.notification_service import (
        build_office_email_body, render_email_html,
    )
    appointment = _make_appointment_custom(
        db, client,
        reason='<script>alert(1)</script><img src=x onerror=alert(2)>',
    )
    doc = render_email_html(
        build_office_email_body(appointment, "Test Dental", _settings(client))
    )
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in doc
    assert "<script" not in doc and "<img" not in doc


def test_email_html_escapes_practice_name_markup(db, client_row):
    """Office-controlled display text is escaped at the same boundary."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.services.notification_service import (
        build_office_email_body, render_email_html,
    )
    appointment = _make_appointment_custom(db, client)
    doc = render_email_html(
        build_office_email_body(appointment, "<i>Sneaky</i> Dental", _settings(client))
    )
    assert "&lt;i&gt;Sneaky&lt;/i&gt; Dental" in doc
    assert "<i>" not in doc


def test_email_html_ampersands_and_quotes_escaped_readable(db, client_row):
    """&, single and double quotes are escaped (quote=True) exactly once and
    ordinary values remain byte-readable."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.services.notification_service import (
        build_office_email_body, render_email_html,
    )
    appointment = _make_appointment_custom(
        db, client, patient_name='O\'Brien & "Smith"',
    )
    doc = render_email_html(
        build_office_email_body(appointment, "Test Dental", _settings(client))
    )
    assert "O&#x27;Brien &amp; &quot;Smith&quot;" in doc
    # Ordinary values pass through readable:
    assert "cleaning/checkup" in doc and "Test Dental" in doc


def test_email_fixed_pre_wrapper_intact_single_escape_pass(db, client_row):
    """The fixed <pre> wrapper appears exactly once and is never escaped;
    already-escaped-LOOKING input is escaped exactly once more (proof there
    is a single escaping pass, no double escaping of the document)."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.services.notification_service import (
        build_office_email_body, render_email_html,
    )
    appointment = _make_appointment_custom(
        db, client, patient_name="Kevin &amp; Co",
    )
    doc = render_email_html(
        build_office_email_body(appointment, "Test Dental", _settings(client))
    )
    assert doc.startswith(EMAIL_PRE_OPEN) and doc.endswith("</pre>")
    assert doc.count("<pre") == 1 and doc.count("</pre>") == 1
    # The literal input "&amp;" becomes "&amp;amp;" — one pass, no more.
    assert "Kevin &amp;amp; Co" in doc


def test_email_subject_control_chars_normalized_and_bounded(db, client_row, monkeypatch):
    """CR/LF (header-injection shaped) input cannot survive into the email
    subject; the complete subject stays within 160 characters."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.calendar_models import AppointmentStatus
    _set_office_contacts(db, client, None, OFFICE_EMAIL)
    email_sent = _recording_email(monkeypatch)
    appointment = _make_appointment_custom(
        db, client,
        patient_name="Kevin\r\nBcc: attacker@example.com " + "x" * 300,
    )
    _send(db, client, appointment)
    assert len(email_sent) == 1
    _, subject = email_sent[0]
    assert "\r" not in subject and "\n" not in subject
    assert len(subject) <= 160
    assert subject.startswith("New Mia appointment — Kevin Bcc: attacker@example.com")
    assert subject.endswith("\u2026")  # name hit its 120-char field limit


def test_field_normalization_contract_exact():
    """The approved deterministic normalization algorithm, verbatim:
    controls (0-31, 127, 128-159) -> spaces; whitespace collapsed; stripped;
    truncation to (limit - 1) + U+2026 only when over the limit."""
    from app.services.notification_service import normalize_notification_field as norm
    assert norm(None, 120) == ""
    assert norm("a\r\nb\tc\x00d\x7fe\x9ff", 120) == "a b c d e f"
    assert norm("  spaced   out  ", 120) == "spaced out"
    out = norm("x" * 400, 300)
    assert len(out) == 300 and out == "x" * 299 + "\u2026"
    assert norm("x" * 300, 300) == "x" * 300  # exactly at the limit: untouched
    assert norm("Kevin Alvarado", 120) == "Kevin Alvarado"


def test_office_sms_plain_readable_not_html_escaped(db, client_row):
    """The staff SMS stays plain readable text: field values are flattened
    and bounded, the 7-line template structure is intact, and markup in a
    value is passed through UNescaped (plain text is never HTML-escaped)."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.services.notification_service import build_office_sms
    appointment = _make_appointment_custom(
        db, client,
        patient_name="<b>Kevin</b>\r\nAlvarado",
        reason="r" * 400,
    )
    body = build_office_sms(appointment, "Test Dental", _settings(client))
    lines = body.split("\n")
    assert len(lines) == 7 and lines[0].startswith("New Mia Appointment")
    assert lines[1] == "Patient: <b>Kevin</b> Alvarado"  # flattened, not escaped
    assert "&lt;" not in body and "\r" not in body
    assert lines[3] == "Reason: " + "r" * 299 + "\u2026"


def test_provider_exception_never_persisted_verbatim(db, client_row, monkeypatch):
    """A provider failure stores the FIXED vocabulary entry — never the
    exception message and never the exception class name."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.calendar_models import AppointmentStatus
    _set_office_contacts(db, client, OFFICE_PHONE, None)
    _recording_sms(monkeypatch, fail=True)  # raises RuntimeError(...)
    appointment = _make_appointment(db, client, AppointmentStatus.PENDING)
    outcome = _send(db, client, appointment)
    db.refresh(appointment)
    assert appointment.notify_error == (
        "office_sms: send_failed; "
        "office_email: skipped (no notification_email configured)"
    )
    assert "provider down" not in appointment.notify_error
    assert "RuntimeError" not in appointment.notify_error
    assert outcome.errors == [
        "office_sms: send_failed",
        "office_email: skipped (no notification_email configured)",
    ]


def test_secret_header_url_shaped_text_absent_everywhere(db, client_row, monkeypatch):
    """Secret/header/URL-shaped exception text appears NOWHERE: not in
    outcome.errors, not in notify_error, not in the AppointmentView output.
    The patient-SMS channel stays untouched throughout."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.calendar_models import AppointmentStatus
    from app.routes.calendar import _appointment_view
    from app.services import notification_service
    _set_office_contacts(db, client, OFFICE_PHONE, OFFICE_EMAIL)
    secret = ("Authorization: Bearer sk_live_SECRET123 at "
              "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages")

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(notification_service, "_send_sms", boom)
    monkeypatch.setattr(notification_service, "_send_email", boom)
    appointment = _make_appointment(db, client, AppointmentStatus.PENDING)
    outcome = _send(db, client, appointment)
    db.refresh(appointment)
    everything = appointment.notify_error + " ".join(outcome.errors)
    for fragment in ("Authorization", "Bearer", "sk_live", "https://", "AC123"):
        assert fragment not in everything
    view = _appointment_view(appointment)
    assert view.notify_error == "office_sms: send_failed; office_email: send_failed"
    assert appointment.patient_sms_sent is False
    assert outcome.patient_sms_sent is False


def test_two_failures_exact_order_and_vocabulary_max_length(db, client_row, monkeypatch):
    """Both office channels failing produces the exact deterministic
    SMS-then-email value; the complete vocabulary is eight values whose
    proven maximum length is exactly 112, and every one passes the API
    sanitizer through unchanged."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.calendar_models import AppointmentStatus
    from app.services.notification_service import (
        NOTIFY_ERROR_MAX_LENGTH,
        VALID_NOTIFY_ERROR_VALUES,
        sanitize_stored_notify_error,
    )
    _set_office_contacts(db, client, OFFICE_PHONE, OFFICE_EMAIL)
    _recording_sms(monkeypatch, fail=True)
    _recording_email(monkeypatch, fail=True)
    appointment = _make_appointment(db, client, AppointmentStatus.PENDING)
    _send(db, client, appointment)
    db.refresh(appointment)
    assert appointment.notify_error == "office_sms: send_failed; office_email: send_failed"
    assert len(VALID_NOTIFY_ERROR_VALUES) == 8
    assert max(len(v) for v in VALID_NOTIFY_ERROR_VALUES) == NOTIFY_ERROR_MAX_LENGTH == 112
    for value in VALID_NOTIFY_ERROR_VALUES:
        assert sanitize_stored_notify_error(value) == value


def test_channel_failure_log_controlled_fields_only(db, client_row, monkeypatch, capsys):
    """The channel-failure server log carries ONLY the controlled fields
    (event, channel, fixed code, sanitized exception class, appointment
    UUID) — never the exception message or any patient value."""
    # Fixture-name correction (local run 1): the shared conftest
    # fixture is client_row; the body below keeps its existing name.
    client = client_row
    from app.calendar_models import AppointmentStatus
    _set_office_contacts(db, client, OFFICE_PHONE, None)
    _recording_sms(monkeypatch, fail=True)
    appointment = _make_appointment(db, client, AppointmentStatus.PENDING)
    _send(db, client, appointment)
    out = capsys.readouterr().out
    assert "[CALENDAR NOTIFY] event=channel_send_failed" in out
    assert "channel=office_sms" in out and "code=send_failed" in out
    assert "exc_class=RuntimeError" in out
    assert str(appointment.id) in out
    for forbidden in ("Kevin", PATIENT_PHONE, "provider down"):
        assert forbidden not in out
