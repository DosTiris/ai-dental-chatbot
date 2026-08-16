# app/services/appointment_note_service.py
#
# PHASE 3A Slice 4B1 - APPOINTMENT INTERNAL NOTES: the ONE owner (Rule 3) of
#   (a) internal-note normalization for EVERY authenticated portal write
#       boundary (the staff-booking create path in booking_service.
#       finalize_staff_booking imports normalize_internal_note from here, and
#       the edit path below uses the same function - the rule is never stated
#       twice), and
#   (b) the tenant-scoped edit/clear mutation for one appointment's
#       internal_note.
#
# WHAT AN INTERNAL NOTE IS (owner contract): office-internal administrative
# plain text attached to one appointment. Optional. NEVER patient-facing: it
# must not appear in chatbot responses, patient-facing booking responses,
# patient SMS/email, office booking notifications, notification templates,
# public/widget APIs, transcripts, confirmation wording, or exports. It is
# independent from reason (not repurposed), urgency, and notification state.
#
# WHY A DEDICATED MODULE rather than booking_service: the Slice 4B1 contract
# unfreezes booking_service for the STAFF BOOKING FUNCTION ONLY, and editing a
# note is not a booking - it changes no status, slot, source, timestamp,
# notification state, hold, or availability. Giving the editor its own small
# owner keeps the frozen lifecycle owner's surface minimal while the shared
# normalization helper keeps one definition of "a valid note".
#
# THIS MODULE IMPORTS NO NOTIFICATION CODE, NO CHAT CODE, AND NO PUBLIC
# SCHEMA - by construction it cannot leak a note into any of them.

import uuid
from typing import NamedTuple, Optional

from sqlalchemy.orm import Session

from app.calendar_models import Appointment
from app.repositories import appointment_repository

# The single length rule (mirrors migrations/011_appointment_internal_note_up
# .sql ck_appointments_internal_note_len: char_length(...) <= 2000).
INTERNAL_NOTE_MAX_CHARS = 2000

# The single refusal wording for an over-limit note (the portal_leads_service
# INVALID_NOTE_DETAIL convention). Silent truncation is forbidden by the
# contract: an over-limit note FAILS, loudly, with this exact sentence.
INVALID_INTERNAL_NOTE_DETAIL = (
    "internal_note must contain at most 2000 characters after trimming, "
    "or null to clear."
)


def normalize_internal_note(raw: Optional[str]) -> Optional[str]:
    """
    Purpose: THE single normalization rule for internal notes at every
        authenticated portal write boundary (Rule 3 - both the staff-booking
        create path and the edit path call exactly this function).
    Inputs: raw - the transport value: None, or any string.
    Returns: None for None / "" / whitespace-only ("no note"); otherwise the
        note with meaningless OUTER whitespace trimmed. INNER newlines and
        spacing are preserved - a note is plain text, never interpreted.
    Possible failures: ValueError(INVALID_INTERNAL_NOTE_DETAIL) when the
        TRIMMED note exceeds INTERNAL_NOTE_MAX_CHARS. Over-limit input must
        fail validation, never silently truncate (owner contract).
    Database effects: none. External effects: none.
    """
    if raw is None:
        return None
    trimmed = raw.strip()
    if trimmed == "":
        return None
    if len(trimmed) > INTERNAL_NOTE_MAX_CHARS:
        raise ValueError(INVALID_INTERNAL_NOTE_DETAIL)
    return trimmed


class NoteUpdateResult(NamedTuple):
    """The edit outcome, shaped like booking_service.BookingResult (success /
    reason / appointment) so the actions route's existing fail-closed success
    gate can consume it unchanged - WITHOUT importing booking_service here,
    which would create an import cycle (booking_service imports this module
    for the shared normalization helper)."""
    success: bool
    reason: str
    appointment: Optional[Appointment] = None


def set_appointment_internal_note(
    db: Session,
    client_id: uuid.UUID,
    appointment_id: uuid.UUID,
    *,
    internal_note: Optional[str],
) -> NoteUpdateResult:
    """
    Purpose: replace or clear ONE appointment's office-internal note for the
        authenticated tenant. This is the ONLY internal_note writer besides
        the atomic staff-booking create path.
    Inputs: ids, then the KEYWORD-ONLY raw note (no permissive defaults - the
        Patch 2C convention). internal_note semantics: a string replaces the
        note (after normalization), None clears it, and blank/whitespace
        normalizes to None (clears). The caller passes the TRANSPORT value;
        normalization happens here, once.
    Returns: NoteUpdateResult.
        ok                    - the note is stored (or cleared) and committed;
                                the refreshed appointment row rides along.
        appointment_missing   - no such appointment FOR THIS CLIENT. An
                                unknown id and another office's id are
                                deliberately indistinguishable (Rule 15): the
                                repository lookup filters on both columns.
        invalid_internal_note - the trimmed note exceeds 2000 characters.
                                Nothing was written.
    What this NEVER does (owner contract, proven by tests): change status,
        slot, source, confirmed_at, notification flags/outcome, holds, or
        availability; send any notification; restrict by status or source -
        an office may attach a private note to ANY appointment it can see,
        including a Mia-created one and a cancelled historical one.
    Database effects: ONE transaction - SELECT ... FOR UPDATE on the
        tenant-scoped row (appointment_repository.get_appointment_for_update,
        the SAME lock the cancel path uses, so a note edit and a concurrent
        lifecycle action are serialized), then a single-column UPDATE,
        committed. EVERY refusal path rolls back before returning, so no
        refusal can leave an open transaction holding the row lock.
    External effects: none.
    """
    try:
        appointment = appointment_repository.get_appointment_for_update(
            db, client_id, appointment_id
        )
        if appointment is None:
            db.rollback()
            return NoteUpdateResult(False, "appointment_missing")

        try:
            normalized = normalize_internal_note(internal_note)
        except ValueError:
            db.rollback()
            return NoteUpdateResult(False, "invalid_internal_note")

        appointment.internal_note = normalized
        db.commit()
        return NoteUpdateResult(True, "ok", appointment=appointment)
    except Exception:
        db.rollback()
        raise
