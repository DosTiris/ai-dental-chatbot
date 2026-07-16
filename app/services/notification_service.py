# app/services/notification_service.py
#
# OWNER OF: sending calendar booking notifications to authorized office
# staff (office SMS + office email) and recording exactly what succeeded or
# failed. As of PATCH 6 this module is additionally the SINGLE OWNER
# (Rule 3) of four cross-cutting notification-output concerns used by BOTH
# the calendar notifications here and chat.py's existing lead notifications:
#
#   1. normalize_notification_field  — the one plain-text field normalizer
#      (control characters, whitespace, output-boundary length limits),
#   2. render_email_html             — the one HTML email renderer (the only
#      place untrusted text is HTML-escaped, and the only owner of the fixed
#      <pre> wrapper markup),
#   3. the fixed stored-error vocabulary for appointments.notify_error and
#      sanitize_stored_notify_error, the API output-boundary gate,
#   4. sanitized_exception_class     — the only exception detail permitted
#      in server logs (class name only, filtered and bounded).
#
# Design rules honored here:
#   - Rule 16: a failed message NEVER pretends to be sent. Each channel's
#     outcome is written onto the appointment row (office_sms_sent,
#     office_email_sent, patient_sms_sent, notify_error) so staff can see
#     "appointment exists, office SMS failed" in the admin view. Channel
#     failures are ALSO logged server-side (safely — see below), making the
#     "logged AND stored" promise true.
#   - Rule 4: no broad exception swallowing — each channel is isolated so one
#     failure can't block the others, but every failure is logged AND stored.
#   - Rule 3: chat.py's existing LEAD notifications remain owned by chat.py.
#     Migrating them into this module is a future refactor (Rule 12 forbids
#     mixing that refactor into a feature patch). This module reuses the
#     SAME environment variables so there is one set of credentials, and
#     (Patch 6) chat.py imports the shared normalizer/renderer/log helpers
#     from here so each concern keeps exactly one owner.
#   - PATCH 2D (Senior Audit Critical #3): patient SMS is DISABLED by current
#     product policy. SMS is for authorized dental-office staff notifications
#     only; Mia collects the patient's phone number so the OFFICE can follow
#     up — that is not consent for automated patient texting. No production
#     path in this module sends any message to the patient.
#     build_patient_sms is retained strictly as FUTURE-ONLY architecture
#     (see its docstring) and has no reachable production call site.
#   - PATCH 6 (Senior Audit Recommended #7): stored/API errors are a CLOSED
#     vocabulary — provider exception text, class names, URLs, headers,
#     credentials, payloads, and stack traces are structurally unstorable.
#     Untrusted text is HTML-escaped exactly once, at the email rendering
#     boundary only: never before database storage, never in JSON API
#     business fields, never in plain-text SMS. Server logs carry only fixed
#     event names, channels, fixed codes, sanitized exception class names,
#     and UUIDs.
#
#   - PATCH 9A (Senior Audit Recommended #1): office-notification duplicate
#     suppression is now a DATABASE invariant. Every provider execution is
#     preceded by an atomic per-channel claim into the notification_attempts
#     ledger (unique per appointment/channel); repeated or concurrent
#     invocations suppress instead of re-sending. The three-state ledger
#     (sending / sent / unknown) is honest: a caught provider exception is
#     recorded as UNKNOWN, never as definite non-delivery, and "sent" means
#     ONLY that the provider API call returned successfully and the outcome
#     transaction committed — never that anything was delivered or opened.
#     Each claimed channel's outcome CAS and the full appointment projection
#     (office_sms_sent / office_email_sent / notify_error) commit atomically
#     under the appointment row lock (lock order everywhere: appointment
#     first, attempt rows second; no notification transaction ever locks a
#     slot). Provider calls run with NO open transaction and NO database
#     lock (immutable scalar snapshot; verified in_transaction() checks).
#     Pre-006 appointments are protected by runtime legacy suppression
#     (approved Option B): a true legacy sent flag or a legacy channel
#     send_failed entry blocks the claim atomically inside the claim SQL,
#     and a malformed legacy notify_error blocks both no-row channels and
#     is never echoed, copied, rewritten, or recomposed. This module remains
#     the single owner of the projection (Rule 3); the repository
#     (notification_attempt_repository) is the single owner of ledger SQL
#     (Rule 15). NOT in 9A (deferred to 9B/9C): retries, recovery,
#     stale-claim processing, workers, cron, provider idempotency keys.
#
# Twilio/Resend are imported lazily inside the send functions so the calendar
# test suite runs on machines without those packages installed.

import html
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.calendar_models import (
    Appointment,
    AppointmentStatus,
    NotificationAttempt,
    NotificationAttemptStatus,
    NotificationChannel,
)
from app.repositories import appointment_repository
from app.repositories import notification_attempt_repository
from app.repositories.notification_attempt_repository import ClaimDisposition
from app.services.calendar_settings_service import CalendarSettings, ensure_utc


# ---------------------------------------------------------------------------
# PATCH 6 — approved output-boundary field limits (Rule 4: named, not magic).
# These bound what leaves the system in notifications; stored business data
# is NEVER modified or truncated. One limit per field kind, applied at build
# time in every staff-notification formatter (calendar office SMS, calendar
# office email, lead office email, lead office SMS).
# ---------------------------------------------------------------------------
FIELD_LIMIT_NAME = 120           # patient_name / lead_name
FIELD_LIMIT_PHONE = 32           # patient_phone / lead_phone
FIELD_LIMIT_EMAIL = 254          # patient_email / lead_email (RFC address max)
FIELD_LIMIT_FREE_TEXT = 300      # reason / reason detail / time window / notes
FIELD_LIMIT_ENUM = 16            # new_or_returning / urgency / status
FIELD_LIMIT_SOURCE = 32          # appointment source
FIELD_LIMIT_PRACTICE_NAME = 120  # office-controlled display name
SUBJECT_MAX_LENGTH = 160         # complete email subject (both emails)


# ---------------------------------------------------------------------------
# PATCH 6 — the FIXED stored-error vocabulary for appointments.notify_error.
# These eight values (four entries; four SMS-then-email combinations) are the
# ONLY values this system ever stores or returns. Skipped strings are
# byte-identical to the pre-Patch-6 strings on purpose: they were already
# safe and every existing test assertion keeps passing unchanged.
# ---------------------------------------------------------------------------
SEND_FAILED = "send_failed"  # fixed code, shared with chat.py's lead meta

OFFICE_SMS_SEND_FAILED = f"office_sms: {SEND_FAILED}"
OFFICE_EMAIL_SEND_FAILED = f"office_email: {SEND_FAILED}"
OFFICE_SMS_SKIPPED = "office_sms: skipped (no notification_phone configured)"
OFFICE_EMAIL_SKIPPED = "office_email: skipped (no notification_email configured)"

_NOTIFY_ERROR_SMS_ENTRIES = (OFFICE_SMS_SEND_FAILED, OFFICE_SMS_SKIPPED)
_NOTIFY_ERROR_EMAIL_ENTRIES = (OFFICE_EMAIL_SEND_FAILED, OFFICE_EMAIL_SKIPPED)

# Every valid stored value: one entry alone, or SMS entry + "; " + email
# entry (SMS always first — matching the send order below). Set membership
# structurally rejects duplicates, reversed order, unknown codes, and raw
# exception strings.
VALID_NOTIFY_ERROR_VALUES = frozenset(
    list(_NOTIFY_ERROR_SMS_ENTRIES)
    + list(_NOTIFY_ERROR_EMAIL_ENTRIES)
    + [f"{sms}; {email}"
       for sms in _NOTIFY_ERROR_SMS_ENTRIES
       for email in _NOTIFY_ERROR_EMAIL_ENTRIES]
)

# Longest valid value: both skipped entries joined (54 + 2 + 56). The write
# path can only compose vocabulary entries, so this is a ceiling by
# construction; the API sanitizer re-checks it against legacy data.
NOTIFY_ERROR_MAX_LENGTH = 112

# The ONLY thing AppointmentView returns for a stored value outside the
# approved grammar (e.g. a legacy raw provider exception written before
# Patch 6). Fixed wording approved in the Patch 6 plan.
NOTIFY_ERROR_WITHHELD = "notification_error: detail_withheld"


# ---------------------------------------------------------------------------
# PATCH 9A — fixed controlled server-log event names (Rule 4: named, not
# magic; Patch 6 logging contract: events carry ONLY fixed names, a channel,
# and UUIDs — never patient values, provider text, or stored error content).
# "channel_send_failed" and "outcome_record_failed" predate 9A and keep
# their exact formats.
# ---------------------------------------------------------------------------
EVENT_ENTRY_CONTRACT_VIOLATION = "entry_contract_violation"
EVENT_BOUNDARY_VIOLATION = "transaction_boundary_violation"
EVENT_CLAIM_RECORD_FAILED = "claim_record_failed"
EVENT_IN_FLIGHT_SUPPRESSED = "in_flight_suppressed"
EVENT_PROJECTION_INCONSISTENCY = "projection_inconsistency"
EVENT_LEGACY_ERROR_WITHHELD = "legacy_error_withheld"
EVENT_OUTCOME_APPOINTMENT_MISSING = "outcome_appointment_missing"

# Channel -> its fixed vocabulary entries (Rule 3: composed only from the
# Patch 6 constants above; SMS entry always precedes the email entry in any
# stored two-entry value).
_CHANNEL_SEND_FAILED_ENTRY = {
    NotificationChannel.OFFICE_SMS: OFFICE_SMS_SEND_FAILED,
    NotificationChannel.OFFICE_EMAIL: OFFICE_EMAIL_SEND_FAILED,
}
_CHANNEL_SKIPPED_ENTRY = {
    NotificationChannel.OFFICE_SMS: OFFICE_SMS_SKIPPED,
    NotificationChannel.OFFICE_EMAIL: OFFICE_EMAIL_SKIPPED,
}

# PATCH 9A legacy suppression (approved Option B), enforced ATOMICALLY
# inside the claim SQL: a channel may be claimed only while the stored
# notify_error is NULL or one of these approved values — the subset of the
# closed Patch 6 vocabulary carrying NO send_failed entry for that channel.
# A legacy channel send_failed (an attempt whose true outcome is unknowable)
# therefore blocks the claim, and ANY malformed value blocks both channels
# automatically because it is outside the closed set.
_CLAIM_ALLOWED_STORED_ERRORS = {
    channel: frozenset(
        value for value in VALID_NOTIFY_ERROR_VALUES
        if _CHANNEL_SEND_FAILED_ENTRY[channel] not in value.split("; ")
    )
    for channel in (NotificationChannel.OFFICE_SMS,
                    NotificationChannel.OFFICE_EMAIL)
}


def normalize_notification_field(value: Optional[str], limit: int) -> str:
    """
    Purpose: THE single plain-text field normalizer (Patch 6) for every
        untrusted value placed into a staff notification (SMS or email
        subject/body). Guarantees no control characters and a bounded,
        readable single-line value. Never used on stored data — output
        boundary only.
    Inputs:  value (may be None), limit (approved per-field maximum).
    Returns: normalized string ("" for None), length <= limit.
    Algorithm (approved, deterministic):
        1. Replace CR, LF, TAB, ASCII controls 0-31, DEL 127, and C1
           controls 128-159 with spaces.
        2. Collapse consecutive whitespace to one normal space.
        3. Strip leading/trailing whitespace.
        4. If longer than limit: keep (limit - 1) characters + U+2026
           ellipsis, so the result is exactly bounded and visibly truncated.
    Database effects: none. External effects: none (pure).
    """
    if value is None:
        return ""
    replaced = "".join(
        " " if (ord(ch) <= 31 or ord(ch) == 127 or 128 <= ord(ch) <= 159)
        else ch
        for ch in str(value)
    )
    normalized = " ".join(replaced.split())
    if len(normalized) > limit:
        normalized = normalized[: limit - 1] + "\u2026"
    return normalized


def sanitized_exception_class(exc: BaseException) -> str:
    """
    Purpose: the ONLY exception detail permitted in server logs (Patch 6
        logging contract): the class name, filtered to [A-Za-z0-9_.] and
        bounded to 64 characters, 'unknown' if nothing survives. NEVER
        str(exc)/repr(exc) — provider exception text can carry URLs,
        identifiers, payloads, and SQL statement parameters.
    Returns: safe class-name string for logs only — never stored, never
        returned through any API or patient/office-facing message.
    """
    name = re.sub(r"[^A-Za-z0-9_.]", "", type(exc).__name__)[:64]
    return name or "unknown"


# The fixed <pre> wrapper — byte-identical to the pre-Patch-6 markup, now
# defined ONCE (Rule 3) for both the calendar office email and chat.py's
# lead office email.
_EMAIL_HTML_PRE_OPEN = (
    "<pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>"
)
_EMAIL_HTML_PRE_CLOSE = "</pre>"


def render_email_html(body_text: str) -> str:
    """
    Purpose: THE single HTML email renderer (Patch 6). Input is PLAIN TEXT;
        this function owns HTML encoding: html.escape(..., quote=True) is
        applied exactly once (<, >, &, double AND single quotes), then the
        escaped text is placed inside the fixed <pre> wrapper. The wrapper
        markup itself is never escaped; callers must never pre-escape.
    Inputs:  body_text — the fully formatted plain-text staff message.
    Returns: the complete HTML document body for the email provider.
    Database effects: none. External effects: none (pure).
    """
    return (
        _EMAIL_HTML_PRE_OPEN
        + html.escape(body_text, quote=True)
        + _EMAIL_HTML_PRE_CLOSE
    )


def sanitize_stored_notify_error(value: Optional[str]) -> Optional[str]:
    """
    Purpose: the API output-boundary gate (Patch 6) for
        appointments.notify_error. AppointmentView must never return an
        arbitrary stored value: only the approved closed vocabulary passes
        through; anything else — legacy raw provider exceptions, reversed
        order, duplicate channels, unknown codes, over-length values —
        returns the fixed withheld marker.
    Inputs:  the stored notify_error value (None when notifications
        succeeded or were never attempted).
    Returns: None for None; the value unchanged when it is one of the eight
        approved values; NOTIFY_ERROR_WITHHELD otherwise.
    Database effects: none — stored data is never rewritten (Option B:
        write-time vocabulary + output-boundary withholding).
    """
    if value is None:
        return None
    if len(value) > NOTIFY_ERROR_MAX_LENGTH:
        return NOTIFY_ERROR_WITHHELD
    if value in VALID_NOTIFY_ERROR_VALUES:
        return value
    return NOTIFY_ERROR_WITHHELD


@dataclass
class NotificationOutcome:
    """Per-channel results for one booking. PATCH 6: errors contain ONLY the
    fixed vocabulary entries above — never provider exception text."""
    office_sms_sent: bool = False
    office_email_sent: bool = False
    # Always False in the current MVP: the patient-SMS channel is disabled by
    # product policy (Patch 2D, Senior Audit Critical #3). The field is kept
    # so the persisted appointment flags and the admin view stay stable and
    # honest — "disabled" is not a failure and never appears in errors.
    patient_sms_sent: bool = False
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PATCH 9A — the immutable notification snapshot and session-safety helpers.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NotificationSnapshot:
    """Everything both provider calls and every configuration decision need,
    as SCALARS ONLY (approved 9A contract). Built exactly once, BEFORE any
    claim; after construction no client/appointment/settings ORM attribute
    is read again during claim/provider orchestration, and no ORM object
    ever crosses the provider boundary — so an expired attribute can never
    silently reopen a transaction there. The two UUIDs are the only
    identifiers permitted in controlled logs. client_id is ALWAYS the id of
    the SUPPLIED client whose notification configuration and recipients are
    used — the tenant the atomic claim verifies the appointment against."""
    appointment_id: uuid.UUID
    client_id: uuid.UUID
    office_phone: str
    office_email: str
    sms_configured: bool
    email_configured: bool
    sms_body: str
    email_subject: str
    email_html: str


def build_notification_snapshot(client, appointment: Appointment,
                                settings: CalendarSettings) -> NotificationSnapshot:
    """
    Purpose: THE single snapshot builder (Patch 9A). Reads the ORM objects
        once and freezes every scalar the rest of the invocation needs:
        recipients are stripped before deriving the configured booleans,
        the SMS body and the email subject are fully built and normalized
        here, and the safe email HTML is rendered here EXACTLY ONCE via
        render_email_html (which remains the single HTML-rendering owner —
        the send boundary transmits, it never re-renders).
    Inputs:  the client row, the committed appointment, request settings.
    Returns: frozen NotificationSnapshot.
    Database effects: SELECTs only, via lazy attribute refresh of expired
        ORM objects (the service-owned read-only transaction this opens is
        ended explicitly by the caller). External effects: none.
    """
    practice_name = (getattr(client, "practice_name", "Dental Office")
                     or "Dental Office")
    office_phone = (getattr(client, "notification_phone", None) or "").strip()
    office_email = (getattr(client, "notification_email", None) or "").strip()
    # PATCH 6 subject contract, unchanged: the patient name is normalized to
    # its field limit first, then the COMPLETE subject passes through the
    # same normalizer (idempotent on already-normal text), so CR/LF can
    # never survive into an email header.
    email_subject = normalize_notification_field(
        "New Mia appointment — "
        + normalize_notification_field(appointment.patient_name,
                                       FIELD_LIMIT_NAME),
        SUBJECT_MAX_LENGTH,
    )
    return NotificationSnapshot(
        appointment_id=appointment.id,
        # TENANT SOURCE (v4 correction): ALWAYS the SUPPLIED client — the
        # office whose notification configuration and recipients are being
        # used. The atomic claim's join (appointments.id = appointment_id
        # AND appointments.client_id = client_id) then genuinely verifies
        # that the appointment belongs to that client; sourcing this from
        # appointment.client_id would let a mismatched call authenticate
        # itself with the appointment's own tenant id (Rule 15).
        client_id=client.id,
        office_phone=office_phone,
        office_email=office_email,
        sms_configured=bool(office_phone),
        email_configured=bool(office_email),
        sms_body=build_office_sms(appointment, practice_name, settings),
        email_subject=email_subject,
        email_html=render_email_html(
            build_office_email_body(appointment, practice_name, settings)
        ),
    )


def _appointment_uuid_without_sql(appointment: Appointment) -> str:
    """The appointment UUID for a controlled log, read from the identity
    map ONLY — sa_inspect(...).identity never emits SQL, unlike touching
    appointment.id on an expired instance. 'unknown' for a transient row."""
    try:
        identity = sa_inspect(appointment).identity
    except Exception:
        return "unknown"
    return str(identity[0]) if identity else "unknown"


def _format_local(appointment: Appointment, settings: CalendarSettings) -> str:
    """Render 'Thursday, July 16 at 1:30 PM' in the CLIENT's timezone."""
    local = ensure_utc(appointment.start_datetime).astimezone(
        ZoneInfo(settings.timezone_name)
    )
    return local.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")


def build_office_sms(appointment: Appointment, practice_name: str,
                     settings: CalendarSettings) -> str:
    """Purpose: the short staff alert. Pure formatting; no side effects.
    PATCH 6: untrusted field VALUES are normalized/bounded (control
    characters can no longer distort the alert); the template's own
    structural newlines, labels, and field order are unchanged. Plain text
    is deliberately NOT HTML-escaped."""
    status_word = (
        "NEEDS CONFIRMATION" if appointment.status == AppointmentStatus.PENDING
        else "Confirmed"
    )
    return (
        f"New Mia Appointment ({status_word})\n"
        f"Patient: {normalize_notification_field(appointment.patient_name, FIELD_LIMIT_NAME)}\n"
        f"Phone: {normalize_notification_field(appointment.patient_phone, FIELD_LIMIT_PHONE)}\n"
        f"Reason: {normalize_notification_field(appointment.reason, FIELD_LIMIT_FREE_TEXT) or 'not stated'}\n"
        f"Patient type: {normalize_notification_field(appointment.new_or_returning, FIELD_LIMIT_ENUM) or 'unknown'}\n"
        f"When: {_format_local(appointment, settings)}\n"
        f"Urgency: {normalize_notification_field(appointment.urgency, FIELD_LIMIT_ENUM)}"
    )


def build_office_email_body(appointment: Appointment, practice_name: str,
                            settings: CalendarSettings) -> str:
    """Purpose: the longer staff email. Pure formatting; no side effects.
    PATCH 6: untrusted field VALUES are normalized/bounded at this output
    boundary (stored values untouched). The result is PLAIN TEXT — HTML
    escaping happens exactly once, in render_email_html at send time."""
    return (
        f"Mia booked an appointment for "
        f"{normalize_notification_field(practice_name, FIELD_LIMIT_PRACTICE_NAME)}.\n\n"
        f"Status: {normalize_notification_field(appointment.status, FIELD_LIMIT_ENUM)}\n"
        f"Patient: {normalize_notification_field(appointment.patient_name, FIELD_LIMIT_NAME)}\n"
        f"Phone: {normalize_notification_field(appointment.patient_phone, FIELD_LIMIT_PHONE)}\n"
        f"Email: {normalize_notification_field(appointment.patient_email, FIELD_LIMIT_EMAIL) or 'not provided'}\n"
        f"Patient type: {normalize_notification_field(appointment.new_or_returning, FIELD_LIMIT_ENUM) or 'unknown'}\n"
        f"Reason: {normalize_notification_field(appointment.reason, FIELD_LIMIT_FREE_TEXT) or 'not stated'}\n"
        f"Urgency: {normalize_notification_field(appointment.urgency, FIELD_LIMIT_ENUM)}\n"
        f"When: {_format_local(appointment, settings)}\n"
        f"Source: {normalize_notification_field(appointment.source, FIELD_LIMIT_SOURCE)}\n"
        f"Appointment ID: {appointment.id}\n"
    )


def build_patient_sms(appointment: Appointment, practice_name: str,
                      settings: CalendarSettings) -> str:
    """
    FUTURE-ONLY (Patch 2D, Senior Audit Critical #3): this formatter has NO
    production call site. Patient SMS is disabled by current product policy —
    SMS is for authorized dental-office staff notifications, and collecting a
    patient's phone number for office follow-up is not consent for automated
    texts. Re-enabling patient SMS requires a separately approved
    consent-enabled feature (stored opt-in with timestamp and source, widget
    consent wording, STOP/HELP handling, messaging-provider configuration
    covering patient appointment notifications, and tests proving no text is
    sent without consent). Do NOT wire this back into
    send_booking_notifications before that feature exists and is approved.

    Purpose (for that future feature): patient-facing confirmation text.
    When staff confirmation is required, the wording deliberately says
    REQUEST RECEIVED — Mia must not claim a confirmation the office hasn't
    made (Rule 16: no false success).
    """
    when = _format_local(appointment, settings)
    if appointment.status == AppointmentStatus.PENDING:
        return (
            f"{practice_name}: your appointment request for {when} has been "
            f"received. The office will contact you to confirm."
        )
    return f"{practice_name}: your appointment is confirmed for {when}."


def _send_sms(to_phone: str, body: str) -> None:
    """One Twilio send. Raises on failure — callers record the error."""
    from twilio.rest import Client as TwilioClient  # Lazy: see module header.
    client = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"],
                          os.environ["TWILIO_AUTH_TOKEN"])
    client.messages.create(body=body, from_=os.environ["TWILIO_FROM_PHONE"], to=to_phone)


def _send_email(to_email: str, subject: str, email_html: str) -> None:
    """One Resend send. Raises on failure — callers record the error.
    PATCH 9A contract: email_html is the FINAL rendered document, produced
    by render_email_html exactly once at snapshot-build time (render_email_html
    remains the single HTML-rendering owner; this boundary TRANSMITS and
    never renders or re-escapes). Callers must never pass raw plain text
    here — build_notification_snapshot is the only production composer."""
    import resend  # Lazy: see module header.
    resend.api_key = os.environ["RESEND_API_KEY"]
    resend.Emails.send({
        "from": os.environ["RESEND_FROM_EMAIL"],
        "to": [to_email],
        "subject": subject,
        "html": email_html,
    })


def send_booking_notifications(
    db: Session,
    client,
    appointment: Appointment,
    settings: CalendarSettings,
) -> NotificationOutcome:
    """
    Purpose: Send the office booking notifications — office SMS and office
        email, the only approved channels — with database-enforced duplicate
        suppression (PATCH 9A): each configured channel is atomically
        CLAIMED into the notification_attempts ledger before its single
        provider execution, so repeated or concurrent invocations suppress
        instead of re-sending, and legacy pre-006 outcomes are protected
        (approved Option B) without any data migration. Patient SMS remains
        DISABLED by product policy (Patch 2D, Senior Audit Critical #3) and
        is never attempted; no patient channel is even representable in the
        ledger.
    Inputs:  committed appointment (the documented caller contract: the one
        production caller invokes this immediately after finalize_booking's
        commit and BEFORE any conversation mutation, so the session arrives
        with no pending state and no caller-owned transaction), the client
        row (contacts + practice name, read once into the snapshot), settings.
    Returns: NotificationOutcome reporting the RECONCILED post-invocation
        projection: a channel sent in a prior invocation reports True rather
        than re-sending; errors mirrors the recomputed notify_error entries
        ([] when a malformed legacy value is being preserved). Success of
        the BOOKING is never affected. patient_sms_sent is always False.
    Database effects: per configured claimable channel, one committed claim
        row (status 'sending'); per claimed channel, ONE atomic transaction
        committing the outcome CAS (sending -> sent/unknown) together with
        the full recomputed appointment projection under the appointment
        row lock (lock order: appointment first, attempt rows second; no
        slot is ever locked here). When zero outcome transactions commit —
        or a transaction-boundary violation aborts the invocation — one
        final reconciliation transaction recomputes the projection. A
        malformed legacy notify_error is never recomposed, rewritten, or
        echoed (flags-only updates; fixed withheld marker at the API).
    External effects: at most 1 Twilio SMS + 1 Resend email, both to the
        OFFICE, both executed with NO open database transaction and NO row
        or table lock, from immutable snapshot scalars only. Never any
        message to the patient.
    Possible failures (Patch 6 logging contract, unchanged): a provider
        exception is isolated per channel, recorded as the honest ledger
        state UNKNOWN (never asserted as definite non-delivery), projected
        as the FIXED vocabulary entry, and logged with controlled fields
        only. Entry-contract or boundary violations abstain safely: nothing
        is sent, no caller state is rolled back, one controlled event is
        logged, and any already-created claim stays honestly 'sending'.
        SMS is attempted first, then email; a two-entry notify_error is
        always SMS-then-email — the approved grammar.
    """
    outcome = NotificationOutcome()

    # ---- MANDATORY ENTRY SESSION CONTRACT (approved 9A, strict) ---------
    # ALL of: new/dirty/deleted empty AND no open transaction. The identity
    # map cannot see raw session.execute DML, which is exactly why the
    # active-transaction check is mandatory and unconditional: ANY open
    # transaction at entry — regardless of what it might contain — causes
    # a safe abstention. Nothing is classified, nothing is rolled back,
    # nothing caller-owned is touched; one controlled event carries only
    # the fixed name and the appointment UUID (read from the identity map,
    # never via SQL). The one production caller satisfies this contract:
    # it invokes immediately after finalize_booking's commit and before
    # any conversation mutation (documented in docs/INTEGRATION.md §9 and
    # proven by test).
    if db.new or db.dirty or db.deleted or db.in_transaction():
        print(
            f"[CALENDAR NOTIFY] event={EVENT_ENTRY_CONTRACT_VIOLATION} "
            f"appointment={_appointment_uuid_without_sql(appointment)}"
        )
        return outcome

    # ---- IMMUTABLE SNAPSHOT (before any claim) ---------------------------
    snapshot = build_notification_snapshot(client, appointment, settings)
    # End the service-owned read-only snapshot transaction explicitly.
    # Entry proved the session held no caller work, and snapshot building
    # performs reads only, so this rollback provably discards nothing.
    db.rollback()
    # From here on, no client/appointment/settings ORM attribute is read
    # again during claim/provider orchestration; the outcome/reconciliation
    # transactions read ONLY the freshly locked appointment row they own.

    outcome_commits = 0
    boundary_violated = False
    final_projection: Optional[_Projection] = None

    for channel in (NotificationChannel.OFFICE_SMS,
                    NotificationChannel.OFFICE_EMAIL):
        configured = (snapshot.sms_configured
                      if channel == NotificationChannel.OFFICE_SMS
                      else snapshot.email_configured)
        if not configured:
            continue  # Reconciliation projects the fixed skipped entry.

        # ---- ATOMIC CLAIM (committed before provider execution) ---------
        # ISOLATED (correction pass 1): a database failure in the claim or
        # its commit must never propagate out of this function — a
        # successfully committed booking cannot become a failed patient
        # response because notification BOOKKEEPING failed (Rule 16: the
        # failure is logged with controlled fields, never hidden, never
        # fatal). On failure: rollback the notification transaction, do
        # NOT call this channel's provider (never send without a committed
        # claim), record no false outcome, and continue to the other
        # channel from a clean, transaction-free session.
        try:
            claim = notification_attempt_repository.claim_channel_attempt(
                db, snapshot.client_id, snapshot.appointment_id, channel,
                _CLAIM_ALLOWED_STORED_ERRORS[channel],
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            print(
                f"[CALENDAR NOTIFY] event={EVENT_CLAIM_RECORD_FAILED} "
                f"channel={channel} "
                f"exc_class={sanitized_exception_class(exc)} "
                f"appointment={snapshot.appointment_id}"
            )
            continue

        if claim.disposition == ClaimDisposition.EXISTING:
            # Duplicate suppression: sent/unknown need nothing here (the
            # reconciliation projects them); an in-flight claim is logged.
            if claim.existing_status == NotificationAttemptStatus.SENDING:
                print(
                    f"[CALENDAR NOTIFY] event={EVENT_IN_FLIGHT_SUPPRESSED} "
                    f"channel={channel} "
                    f"appointment={snapshot.appointment_id}"
                )
            continue
        if claim.disposition == ClaimDisposition.NOT_CLAIMABLE:
            continue  # Legacy-protected / malformed / foreign / missing.

        # ---- TRANSACTION-FREE PROVIDER BOUNDARY (approved 9A) ------------
        # The claim just committed, so an open transaction here is a coding
        # regression. Approved response: do NOT call this provider, do NOT
        # continue to the remaining channel, roll back only the
        # service-owned unexpected transaction (entry + the explicit-commit
        # design prove it can contain only our own reads), leave the claim
        # honestly 'sending', log, reconcile, return.
        if db.in_transaction():
            db.rollback()
            print(
                f"[CALENDAR NOTIFY] event={EVENT_BOUNDARY_VIOLATION} "
                f"channel={channel} "
                f"appointment={snapshot.appointment_id}"
            )
            boundary_violated = True
            break

        # ---- SINGLE PROVIDER EXECUTION (snapshot scalars only) -----------
        terminal_status = NotificationAttemptStatus.SENT
        try:
            if channel == NotificationChannel.OFFICE_SMS:
                _send_sms(snapshot.office_phone, snapshot.sms_body)
            else:
                _send_email(snapshot.office_email, snapshot.email_subject,
                            snapshot.email_html)
        except Exception as exc:
            # HONEST semantics (approved): a caught provider exception is
            # UNKNOWN — a timeout after provider acceptance is
            # indistinguishable from a rejection, so definite non-delivery
            # is never claimed. The projection still carries the fixed
            # send_failed vocabulary entry ("the provider call did not
            # complete successfully").
            terminal_status = NotificationAttemptStatus.UNKNOWN
            print(
                f"[CALENDAR NOTIFY] event=channel_send_failed "
                f"channel={channel} code={SEND_FAILED} "
                f"exc_class={sanitized_exception_class(exc)} "
                f"appointment={snapshot.appointment_id}"
            )

        # ---- ATOMIC OUTCOME CAS + PROJECTION (one commit) ----------------
        projection = _commit_outcome_and_projection(
            db, snapshot, claim.attempt_id, terminal_status
        )
        if projection is not None:
            outcome_commits += 1
            final_projection = projection

    # ---- FINAL RECONCILIATION (approved timing) ---------------------------
    # Runs when zero outcome transactions committed (zero claims, existing
    # rows, missing recipients, legacy/malformed protection, outcome-commit
    # failure, or any mix) and ALWAYS after a boundary violation. When an
    # outcome transaction committed, it already recomputed both channels
    # atomically; repeating after a boundary violation is idempotent and
    # can make no incorrect change.
    if outcome_commits == 0 or boundary_violated:
        projection = _reconcile_projection(db, snapshot)
        if projection is not None:
            final_projection = projection

    # ---- RECONCILED OUTCOME (backward-compatible shape) -------------------
    # PATCH 2D unchanged: patient SMS is disabled product policy —
    # outcome.patient_sms_sent stays False, nothing patient-related enters
    # errors, and appointment.patient_sms_sent is deliberately never
    # touched by the 9A projection (its server default is False; a
    # hypothetical pre-2D True would be honest history, not ours to erase).
    if final_projection is not None:
        outcome.office_sms_sent = final_projection.office_sms_sent
        outcome.office_email_sent = final_projection.office_email_sent
        if final_projection.malformed_stored_error:
            # The stored value is untouchable and must never be echoed or
            # replaced by a newly composed value: errors is EMPTY (approved).
            outcome.errors = []
            print(
                f"[CALENDAR NOTIFY] event={EVENT_LEGACY_ERROR_WITHHELD} "
                f"appointment={snapshot.appointment_id}"
            )
        else:
            outcome.errors = list(final_projection.entries)
    return outcome


# ---------------------------------------------------------------------------
# PATCH 9A — the single appointment-projection owner (Rule 3). Replaces the
# pre-9A _record_outcome: the projection is now RECOMPUTED from committed
# ledger state under the appointment row lock instead of overwritten from a
# per-invocation outcome object, so concurrent split-channel workers can
# never erase each other's results and a true sent flag can never be reset.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Projection:
    """One deterministic recomputation of the appointment notification
    projection. entries is the SMS-first/email-second fixed-vocabulary
    composition; when malformed_stored_error is True the stored
    notify_error is preserved byte-for-byte (flags-only update) and entries
    must not be written or returned."""
    office_sms_sent: bool
    office_email_sent: bool
    entries: Tuple[str, ...]
    malformed_stored_error: bool
    inconsistent_channels: Tuple[str, ...]


def _compute_projection(
    appointment: Appointment,
    attempts: Dict[str, NotificationAttempt],
    snapshot: NotificationSnapshot,
) -> _Projection:
    """
    Purpose: THE monotonic projection formula (approved 9A), pure and
        deterministic. Per channel:
            final_sent = stored_flag OR (attempt_status == 'sent')
        so an existing true flag can never become false. An attempt row
        owns the channel's LEDGER state; the projected flag is governed by
        the formula, and a true flag alongside a sending/unknown attempt is
        preserved true with a controlled inconsistency log (never an
        error entry). With no attempt row, the approved legacy and
        recipient rules apply: a legacy true flag stays true; a legacy
        channel send_failed entry is preserved (legacy unknown — its true
        outcome is unknowable); a missing recipient projects the fixed
        skipped entry ONLY when no attempt and no protected legacy outcome
        exist; otherwise no entry (claimable / honest anomaly). A malformed
        stored value forces flags-only mode: flags still move monotonically
        (a sent attempt raises its flag) but no entry composition may
        replace the untouchable stored text.
    Inputs:  the LOCKED appointment row (committed state), both channel
        attempts (0-2), the snapshot (current recipient configuration).
    Returns: frozen _Projection. Database effects: none (pure).
    """
    stored_error = appointment.notify_error
    malformed = (stored_error is not None
                 and stored_error not in VALID_NOTIFY_ERROR_VALUES)
    stored_entries = (tuple(stored_error.split("; "))
                      if stored_error is not None and not malformed else ())

    flags: Dict[str, bool] = {}
    entries: List[str] = []
    inconsistent: List[str] = []

    for channel in (NotificationChannel.OFFICE_SMS,
                    NotificationChannel.OFFICE_EMAIL):
        stored_flag = bool(
            appointment.office_sms_sent
            if channel == NotificationChannel.OFFICE_SMS
            else appointment.office_email_sent
        )
        configured = (snapshot.sms_configured
                      if channel == NotificationChannel.OFFICE_SMS
                      else snapshot.email_configured)
        attempt = attempts.get(channel)
        entry: Optional[str] = None

        if attempt is not None:
            final_sent = (stored_flag
                          or attempt.status == NotificationAttemptStatus.SENT)
            if final_sent:
                if attempt.status != NotificationAttemptStatus.SENT:
                    inconsistent.append(channel)
            elif attempt.status == NotificationAttemptStatus.UNKNOWN:
                entry = _CHANNEL_SEND_FAILED_ENTRY[channel]
            # sending (not final_sent): honest in-flight — no entry.
        else:
            final_sent = stored_flag
            if not final_sent:
                if _CHANNEL_SEND_FAILED_ENTRY[channel] in stored_entries:
                    entry = _CHANNEL_SEND_FAILED_ENTRY[channel]
                elif not configured:
                    entry = _CHANNEL_SKIPPED_ENTRY[channel]
                # else: claimable / anomaly — no entry, honestly false.

        flags[channel] = final_sent
        if entry is not None:
            entries.append(entry)  # Loop order guarantees SMS-then-email.

    return _Projection(
        office_sms_sent=flags[NotificationChannel.OFFICE_SMS],
        office_email_sent=flags[NotificationChannel.OFFICE_EMAIL],
        entries=tuple(entries),
        malformed_stored_error=malformed,
        inconsistent_channels=tuple(inconsistent),
    )


def _apply_and_commit_projection(
    db: Session,
    snapshot: NotificationSnapshot,
    locked_appointment: Appointment,
) -> _Projection:
    """
    Purpose: shared tail of the outcome and reconciliation transactions:
        read both ledger rows, recompute, write the projection onto the
        LOCKED appointment row, commit ONCE, then emit any controlled
        inconsistency logs. The malformed-value rule is enforced here: the
        notify_error column is recomposed ONLY when its committed stored
        value is NULL or approved vocabulary; otherwise the update is
        flags-only and the stored text stays byte-identical.
    Database effects: UPDATE appointments (+ any pending attempt CAS in the
        same transaction) + ONE commit. Exceptions propagate to the caller,
        which owns the rollback (so a terminal attempt state can never
        commit while its matching projection update rolls back).
    """
    attempts = notification_attempt_repository.get_attempts_by_appointment(
        db, snapshot.client_id, snapshot.appointment_id
    )
    projection = _compute_projection(locked_appointment, attempts, snapshot)
    locked_appointment.office_sms_sent = projection.office_sms_sent
    locked_appointment.office_email_sent = projection.office_email_sent
    if not projection.malformed_stored_error:
        locked_appointment.notify_error = (
            "; ".join(projection.entries) or None
        )
    db.add(locked_appointment)
    db.commit()
    for channel in projection.inconsistent_channels:
        print(
            f"[CALENDAR NOTIFY] event={EVENT_PROJECTION_INCONSISTENCY} "
            f"channel={channel} "
            f"appointment={snapshot.appointment_id}"
        )
    return projection


def _commit_outcome_and_projection(
    db: Session,
    snapshot: NotificationSnapshot,
    attempt_id: uuid.UUID,
    terminal_status: str,
) -> Optional[_Projection]:
    """
    Purpose: THE atomic per-channel outcome transaction (approved 9A):
        appointment row locked FIRST, then the status-guarded CAS
        (sending -> sent/unknown; a stale or repeated writer changes
        nothing — sent->unknown, unknown->sent, and terminal resolved_at
        rewrites are unrepresentable), then the full two-channel projection
        recompute, all committed with ONE commit — so the ledger and the
        staff-visible AppointmentView can never diverge across a crash.
    Returns: the committed _Projection, or None when the transaction failed
        (rolled back: the attempt stays honestly 'sending', the projection
        stays at its prior committed state, no false sent flag can exist,
        and per-channel independence is preserved — the caller continues
        with a clean, transaction-free session).
    Database effects: one transaction, one commit (or one rollback).
    """
    try:
        locked = appointment_repository.get_appointment_for_update(
            db, snapshot.client_id, snapshot.appointment_id
        )
        if locked is None:  # Missing or foreign tenant: mutation-free.
            db.rollback()
            print(
                f"[CALENDAR NOTIFY] event={EVENT_OUTCOME_APPOINTMENT_MISSING} "
                f"appointment={snapshot.appointment_id}"
            )
            return None
        transitioned = notification_attempt_repository.cas_attempt_to_terminal(
            db, snapshot.client_id, attempt_id, terminal_status,
            datetime.now(timezone.utc),
        )
        if transitioned == 0:
            # Deterministic disambiguation (approved): already terminal, or
            # missing/foreign tenant. Either way nothing was changed by this
            # writer; the projection recompute below reads committed truth.
            notification_attempt_repository.get_attempt_for_tenant(
                db, snapshot.client_id, attempt_id
            )
        return _apply_and_commit_projection(db, snapshot, locked)
    except Exception as exc:
        db.rollback()
        print(
            f"[CALENDAR NOTIFY] event=outcome_record_failed "
            f"exc_class={sanitized_exception_class(exc)} "
            f"appointment={snapshot.appointment_id}"
        )
        return None


def _reconcile_projection(
    db: Session,
    snapshot: NotificationSnapshot,
) -> Optional[_Projection]:
    """
    Purpose: THE final reconciliation transaction (approved 9A timing): runs
        when zero outcome transactions committed during the invocation, and
        always after a transaction-boundary violation. Same lock order
        (appointment first, attempts second), same monotonic formula, same
        malformed-value preservation, same fixed entry order — it may only
        repeat the deterministic computation and can make no incorrect
        change; a true sent flag can never be reset.
    Returns: the committed _Projection, or None on failure (rolled back,
        controlled log, prior committed projection untouched).
    """
    try:
        locked = appointment_repository.get_appointment_for_update(
            db, snapshot.client_id, snapshot.appointment_id
        )
        if locked is None:  # Missing or foreign tenant: mutation-free.
            db.rollback()
            print(
                f"[CALENDAR NOTIFY] event={EVENT_OUTCOME_APPOINTMENT_MISSING} "
                f"appointment={snapshot.appointment_id}"
            )
            return None
        return _apply_and_commit_projection(db, snapshot, locked)
    except Exception as exc:
        db.rollback()
        print(
            f"[CALENDAR NOTIFY] event=outcome_record_failed "
            f"exc_class={sanitized_exception_class(exc)} "
            f"appointment={snapshot.appointment_id}"
        )
        return None
